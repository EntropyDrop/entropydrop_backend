"""Durable Pro queue upgrades, reconciled without recreating RQ jobs."""
import asyncio
import logging
from datetime import datetime, timezone

from redis import Redis

import models
from config import settings
from database import SessionLocal

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = ("pending", "processing", "pending_skin", "processing_skin")
GENERATION_MODES = ("aigc_text_to_skin", "aigc_image_edit_to_skin", "aigc_image_to_skin")
STAGE_QUEUES = {
    "text_to_image": "queue_text_to_image",
    "image_edit": "queue_image_edit",
    "image_to_skin": "queue_image_to_skin",
    "real_to_render": "queue_real_to_render",
    "real_to_render_resume": "queue_real_to_render",
    "render_to_uv": "queue_render_to_uv",
}
# Refreshed while active; terminal jobs do not leave permanent Redis keys.
PRIORITY_TTL = 7 * 24 * 3600
RECONCILE_INTERVAL = 10
redis_conn = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=3, socket_timeout=3)

# Checking status alone is unsafe: RQ pops before it records "started".
# LREM must succeed in this same script before any job metadata is changed.
# Keep the existing hash (arguments, retries, callbacks, timestamps) intact.
PROMOTE_JOB = """
if redis.call('exists', KEYS[4]) == 1 then return 0 end
if redis.call('hget', KEYS[1], 'status') ~= 'queued' then return 0 end
if redis.call('hget', KEYS[1], 'origin') ~= ARGV[2] then return 0 end
if redis.call('lrem', KEYS[2], 0, ARGV[1]) == 0 then return 0 end
redis.call('hset', KEYS[1], 'origin', ARGV[3])
redis.call('rpush', KEYS[3], ARGV[1])
redis.call('sadd', KEYS[5], KEYS[3])
return 1
"""


def active_generations(db):
    return db.query(models.GenerationLog).filter(
        models.GenerationLog.status.in_(ACTIVE_STATUSES),
        models.GenerationLog.mode.in_(GENERATION_MODES),
        models.GenerationLog.is_deleted.is_(False),
        models.GenerationLog.withdrawal.is_(None),
    )


def grant_pro_priority(db, user):
    """Called in the verified payment transaction, without touching billing.

    Result processing locks generation then user when refunding. Skip locked
    generations to avoid reversing that lock order; reconciliation picks them up.
    """
    if not user.is_pro_active:
        return
    for log in active_generations(db).filter(
        models.GenerationLog.user_id == user.id,
        models.GenerationLog.pro_priority.is_(False),
    ).order_by(models.GenerationLog.created_at, models.GenerationLog.id).with_for_update(skip_locked=True).all():
        log.pro_priority = True


def promote_job(connection, log_id, job_id, queue_name):
    return connection.eval(
        PROMOTE_JOB, 5,
        f"rq:job:{job_id}", f"rq:queue:{queue_name}",
        f"rq:queue:high_{queue_name}", f"generation:cancelled:{log_id}", "rq:queues",
        job_id, queue_name, f"high_{queue_name}",
    )


def sync_pro_priority(db, user_id=None):
    query = active_generations(db).filter(models.GenerationLog.pro_priority.is_(True))
    if user_id is not None:
        query = query.filter(models.GenerationLog.user_id == user_id)
    logs = query.order_by(models.GenerationLog.created_at, models.GenerationLog.id).all()
    if not logs:
        return
    # Use the normal queue itself as the poll index, avoiding a Redis-wide
    # key scan for every active generation on every reconciliation pass.
    polls = {}
    for raw_id in redis_conn.lrange("rq:queue:queue_real_to_render", 0, -1):
        job_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        if job_id.startswith("real_to_render_poll_"):
            log_id, _, number = job_id.removeprefix("real_to_render_poll_").rpartition("_")
            if number.isdigit():
                polls.setdefault(log_id, []).append(job_id)
    for log in logs:
        # Workers consult this on each handoff, including scheduled provider
        # polls. A running stage is never interrupted or resubmitted.
        redis_conn.set(f"generation:pro_priority:{log.id}", "1", ex=PRIORITY_TTL)
        for stage, queue_name in STAGE_QUEUES.items():
            promote_job(redis_conn, log.id, f"generation_{log.id}_{stage}", queue_name)
        # Polls have numbered IDs. Move only polls actually queued, preserving
        # delayed retries in their existing RQ scheduler until they become due.
        for job_id in polls.get(log.id, ()):
            promote_job(redis_conn, log.id, job_id, "queue_real_to_render")


def sync_after_payment(db, user_id):
    """Payment is already committed; a Redis outage must not fail checkout."""
    try:
        sync_pro_priority(db, user_id)
    except Exception:
        logger.exception("Pro queue promotion deferred for user %s", user_id)


def reconcile_pro_priority():
    with SessionLocal() as db:
        # Also repair skipped locks, deployments with preexisting subscribers,
        # and a generation submitted concurrently with subscription activation.
        user_ids = active_generations(db).filter(
            models.GenerationLog.pro_priority.is_(False),
        ).with_entities(models.GenerationLog.user_id).distinct()
        users = db.query(models.User).filter(
            models.User.id.in_(user_ids),
            models.User.pro_level != "free",
            models.User.pro_expires_at > datetime.now(timezone.utc),
        ).all()
        for user in users:
            grant_pro_priority(db, user)
        db.commit()
        sync_pro_priority(db)


async def start_pro_priority_job():
    while True:
        try:
            await asyncio.to_thread(reconcile_pro_priority)
        except Exception:
            logger.exception("Pro queue priority reconciliation failed")
        await asyncio.sleep(RECONCILE_INTERVAL)
