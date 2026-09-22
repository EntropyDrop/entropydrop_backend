from credit_balance import lock_balance
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Request
from redis import Redis
from rq import Worker, Queue
from config import settings
import json
import math
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from auth import get_current_admin
from models import User, GenerationLog, Order, CollectionItem, UserLike, UserFeedback, Collection, ShippingAddress, OrderItem, ForumPost, ForumComment, ForumPostLike, ForumNotification, CreditLog
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from rate_limit import limiter, get_authenticated_or_remote_address
from instance_monitor import list_backend_instance_history, list_backend_instance_metrics

router = APIRouter(
    prefix="/api/monitor",
    tags=["monitor"],
)

# Use the same connection params as in worker_tasks.py
redis_conn = Redis.from_url(settings.REDIS_URL)
MONITOR_READ_RATE_LIMIT = "30/minute; 2000/hour"
MONITOR_WRITE_RATE_LIMIT = "2/minute; 20/hour"
MONITOR_CONFIG_RATE_LIMIT = "10/minute; 100/hour"
MONITOR_BULK_GIFT_RATE_LIMIT = "1/5minutes; 10/day"


@router.get("/backend-instances/history")
@limiter.limit(MONITOR_READ_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def get_backend_instance_history(
    request: Request,
    admin: User = Depends(get_current_admin),
):
    return list_backend_instance_history(
        redis_conn,
        history_hours=max(1, settings.BACKEND_METRICS_HISTORY_HOURS),
        bucket_seconds=max(60, settings.BACKEND_METRICS_HISTORY_BUCKET_SECONDS),
    )


@router.get("/backend-instances")
@limiter.limit(MONITOR_READ_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def get_backend_instances(
    request: Request,
    admin: User = Depends(get_current_admin),
):
    return list_backend_instance_metrics(
        redis_conn,
        stale_after_seconds=max(1, settings.BACKEND_METRICS_STALE_AFTER_SECONDS),
    )

@router.get("/stats")
@limiter.limit(MONITOR_READ_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def get_monitor_stats(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    # 1. Get queue stats
    queues_to_check = [
        'queue_text_to_image', 'high_queue_text_to_image',
        'queue_image_edit', 'high_queue_image_edit',
        'queue_image_to_skin', 'high_queue_image_to_skin'
    ]
    
    queue_stats = {}
    for q_name in queues_to_check:
        q = Queue(q_name, connection=redis_conn)
        queue_stats[q_name] = {
            "count": q.count,
            "started_count": q.started_job_registry.count,
            "deferred_count": q.deferred_job_registry.count,
            "finished_count": q.finished_job_registry.count,
            "failed_count": q.failed_job_registry.count,
            "scheduled_count": q.scheduled_job_registry.count,
        }
    
    # 2. Get worker info
    all_workers = Worker.all(connection=redis_conn)
    worker_info = []
    
    # Get all workers and mark them as active/inactive
    now = datetime.now(timezone.utc)
    
    busy_per_queue = {q: 0 for q in queues_to_check}
    processed_busy_workers = 0
    active_workers_count = 0
    idle_workers_count = 0
    
    for w in all_workers:
        # Check activity
        is_active = False
        if w.last_heartbeat:
            hb = w.last_heartbeat
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            if abs((now - hb).total_seconds()) < 600: # Increase to 10 minutes for safety
                is_active = True

        # Basic worker info
        info = {
            "name": w.name,
            "queues": w.queue_names(),
            "state": w.state,
            "is_active": is_active,
            "current_job_id": w.get_current_job_id(),
            "last_heartbeat": w.last_heartbeat.isoformat() if w.last_heartbeat else None,
            "birth_date": w.birth_date.isoformat() if w.birth_date else None,
        }
        
        # Only count in summary if truly active
        if is_active:
            active_workers_count += 1
            if w.state == 'idle':
                idle_workers_count += 1
            
            # If it's busy, try to get more job info
            if w.state == 'busy':
                processed_busy_workers += 1
                job = w.get_current_job()
                if job:
                    # Track busy count per queue for more accurate "started_count"
                    if job.origin in busy_per_queue:
                        busy_per_queue[job.origin] += 1
                    
                    info["current_job"] = {
                        "id": job.id,
                        "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
                        "description": job.description,
                    }
        
        worker_info.append(info)

    # 3. Finalize queue stats with worker-reported busy counts
    for q_name in queues_to_check:
        # If worker reports more busy jobs than the registry, use worker count
        queue_stats[q_name]["started_count"] = max(queue_stats[q_name]["started_count"], busy_per_queue[q_name])
    
    # 5. Get Historical Stats (last 7 days)
    history = []
    for i in range(6, -1, -1):
        day_date = now - timedelta(days=i)
        day_start = day_date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Cumulative users up to this day
        total_users_day = db.query(func.count(User.id)).filter(User.created_at <= day_end).scalar()
        
        # Pro users on this day (proxy using Order history)
        # Count unique users who had a paid subscription order before or on this day 
        # and it hasn't "expired" (assuming 31 days for simplicity)
        total_pro_day = db.query(func.count(func.distinct(Order.user_id)))\
            .filter(
                Order.order_type == 'subscription',
                Order.status == 'paid',
                Order.paid_at <= day_end,
                Order.paid_at >= (day_end - timedelta(days=31))
            ).scalar()

        # Generation counts for this day
        gen_reg = db.query(func.count(GenerationLog.id))\
            .filter(
                GenerationLog.created_at >= day_start,
                GenerationLog.created_at <= day_end,
                GenerationLog.is_pro == False
            ).scalar()
            
        gen_pro = db.query(func.count(GenerationLog.id))\
            .filter(
                GenerationLog.created_at >= day_start,
                GenerationLog.created_at <= day_end,
                GenerationLog.is_pro == True
            ).scalar()

        # A daily_login credit log is created on a user's first login of each
        # UTC day. Count distinct users to keep the metric correct even if
        # legacy or duplicate logs exist.
        active_users = db.query(func.count(func.distinct(CreditLog.user_id)))\
            .filter(
                CreditLog.action == "daily_login",
                CreditLog.created_at >= day_start,
                CreditLog.created_at <= day_end
            ).scalar()

        history.append({
            "date": day_start.strftime("%m-%d"),
            "total_users": total_users_day,
            "total_pro": total_pro_day,
            "active_users": active_users,
            "gen_regular": gen_reg,
            "gen_pro": gen_pro
        })

    # 6. Get 24h Hourly Stats
    history_24h = []
    for i in range(23, -1, -1):
        hour_date = now - timedelta(hours=i)
        hour_start = hour_date.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_date.replace(minute=59, second=59, microsecond=999999)
        
        gen_reg = db.query(func.count(GenerationLog.id))\
            .filter(
                GenerationLog.created_at >= hour_start,
                GenerationLog.created_at <= hour_end,
                GenerationLog.is_pro == False
            ).scalar()
            
        gen_pro = db.query(func.count(GenerationLog.id))\
            .filter(
                GenerationLog.created_at >= hour_start,
                GenerationLog.created_at <= hour_end,
                GenerationLog.is_pro == True
            ).scalar()

        history_24h.append({
            "time": hour_start.strftime("%H:00"),
            "gen_regular": gen_reg,
            "gen_pro": gen_pro
        })

    # 7. Get Current User Stats (for the summary)
    total_users = db.query(func.count(User.id)).scalar()
    total_members = db.query(func.count(User.id)).filter(User.pro_expires_at > now).scalar()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "queue_stats": queue_stats,
        "workers": worker_info,
        "summary": {
            "total_workers": active_workers_count,
            "idle_workers": idle_workers_count,
            "busy_workers": processed_busy_workers,
            "total_queued_tasks": sum(q["count"] for q in queue_stats.values()),
            "total_processing_tasks": sum(q["started_count"] for q in queue_stats.values()),
            "total_users": total_users,
            "total_members": total_members,
        },
        "history": history,
        "history_24h": history_24h
    }


import math

@router.get("/unfinished")
@limiter.limit(MONITOR_READ_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def get_unfinished_logs(
    request: Request,
    page: int = 1,
    page_size: int = 10,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 10
    elif page_size > 100:
        page_size = 100

    offset = (page - 1) * page_size
    unfinished_statuses = ["pending", "processing", "pending_skin", "processing_skin", "failed"]

    # Total count query
    total_count = db.query(GenerationLog).filter(
        GenerationLog.status.in_(unfinished_statuses),
        GenerationLog.is_deleted == False
    ).count()

    # Query logs with joined User info
    results = db.query(
        GenerationLog.id,
        GenerationLog.prompt,
        GenerationLog.mode,
        GenerationLog.status,
        GenerationLog.error_msg,
        GenerationLog.model_version,
        GenerationLog.aux_model_version,
        GenerationLog.created_at,
        GenerationLog.user_id,
        GenerationLog.provider_submission_state,
        User.email,
        User.username
    ).outerjoin(
        User, GenerationLog.user_id == User.id
    ).filter(
        GenerationLog.status.in_(unfinished_statuses),
        GenerationLog.is_deleted == False
    ).order_by(
        GenerationLog.created_at.desc()
    ).offset(offset).limit(page_size).all()

    items = []
    for r in results:
        # Mask email: u***e@domain.com
        masked_email = None
        if r.email:
            parts = r.email.split("@")
            if len(parts) == 2:
                name, domain = parts
                if len(name) <= 2:
                    masked_name = name[0] + "*" * (len(name) - 1)
                else:
                    masked_name = name[0] + "*" * (len(name) - 2) + name[-1]
                masked_email = f"{masked_name}@{domain}"
            else:
                masked_email = r.email[:2] + "***"

        # Mask username: J***e
        masked_username = None
        if r.username:
            if len(r.username) <= 2:
                masked_username = r.username[0] + "*"
            else:
                masked_username = r.username[0] + "*" * (len(r.username) - 2) + r.username[-1]

        # Mask User ID: u_12***
        masked_user_id = None
        if r.user_id:
            if len(r.user_id) <= 4:
                masked_user_id = r.user_id[:2] + "**"
            else:
                masked_user_id = r.user_id[:4] + "***"

        items.append({
            "id": r.id,
            "prompt": r.prompt,
            "mode": r.mode,
            "status": r.status,
            "error_msg": r.error_msg,
            "model_version": r.model_version,
            "aux_model_version": r.aux_model_version,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "user_id": masked_user_id,
            "user_email": masked_email,
            "user_username": masked_username,
            "provider_submission_state": r.provider_submission_state,
        })

    total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1

    return {
        "items": items,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }


@router.delete("/logs/{id}")
@limiter.limit(MONITOR_WRITE_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
def admin_delete_log(
    request: Request,
    id: str,
    background_tasks: BackgroundTasks,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Admin-only: Delete any skin generation record and associated data (soft delete + S3 cleaning)"""
    log = db.query(GenerationLog).filter(GenerationLog.id == id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
        
    from sqlalchemy import or_
    from routers.generate import cancel_generation_jobs
    from skin_withdrawal import begin, resume, ASSET_FIELDS

    # Check and reset any user who used this skin as their personal character
    candidate_keys = [getattr(log, field) for field in ASSET_FIELDS if getattr(log, field)]
    filters = []
    for key in candidate_keys:
        escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(User.skin_url.like(f"%{escaped}%", escape="\\"))
        filters.append(User.skin_url == key)
    if log.id:
        escaped_id = log.id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(User.skin_url.like(f"%{escaped_id}%", escape="\\"))

    users_with_this_skin = db.query(User).filter(or_(*filters)).all() if filters else []
    reset_users_count = len(users_with_this_skin)
    for u in users_with_this_skin:
        u.skin_url = None
        u.skin_type = "strong"
    if reset_users_count > 0:
        db.flush()

    begin(db, log)
    cancel_generation_jobs(log.id)
    resume(db, log, allow_pending_cdn=True)

    return {
        "message": f"Creation {id} deleted and public files withdrawn by admin",
        "character_reset": reset_users_count > 0,
        "reset_users_count": reset_users_count
    }


@router.delete("/failed-tasks")
@limiter.limit("1/minute; 10/hour", key_func=get_authenticated_or_remote_address, override_defaults=False)
def admin_delete_all_failed_tasks(
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Admin-only: Soft-delete all failed generation tasks and clean their associated files and data"""
    failed_logs = db.query(GenerationLog).filter(
        GenerationLog.status == "failed",
        GenerationLog.is_deleted == False
    ).all()
    
    if not failed_logs:
        return {"message": "No failed tasks to delete", "deleted_count": 0}
        
    from routers.generate import cancel_generation_jobs
    from skin_withdrawal import begin, resume
    count = 0
    for log in failed_logs:
        begin(db, log)
        cancel_generation_jobs(log.id)
        resume(db, log, allow_pending_cdn=True)
        count += 1
    return {"message": "Failed tasks deleted and public files withdrawn", "deleted_count": count}


from pydantic import BaseModel


class ModeMaintenanceToggleRequest(BaseModel):
    enabled: bool


@router.get("/mode_status")
@limiter.limit(MONITOR_READ_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def get_modes_status(
    request: Request,
    admin: User = Depends(get_current_admin)
):
    from backend_utils import is_text_to_skin_enabled, is_image_to_skin_enabled, is_image_edit_to_skin_enabled
    return {
        "text_to_skin_enabled": is_text_to_skin_enabled(),
        "image_to_skin_enabled": is_image_to_skin_enabled(),
        "image_edit_to_skin_enabled": is_image_edit_to_skin_enabled(),
    }

@router.post("/mode_status/{mode_name}")
@limiter.limit(MONITOR_CONFIG_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def toggle_mode_status(
    request: Request,
    mode_name: str,
    req: ModeMaintenanceToggleRequest,
    admin: User = Depends(get_current_admin)
):
    if mode_name not in ("text_to_skin", "image_to_skin", "image_edit_to_skin"):
        raise HTTPException(status_code=400, detail="Invalid mode name")
    try:
        redis_conn.set(f"config:{mode_name}_enabled", "1" if req.enabled else "0")
        return {"mode": mode_name, "enabled": req.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update Redis settings: {e}")





class SetModelPriceRequest(BaseModel):
    model_name: str
    credits: int
    is_pro: bool = False
    under_maintenance: bool = False


@router.get("/model_prices")
@limiter.limit(MONITOR_READ_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def get_model_prices_endpoint(
    request: Request,
    admin: User = Depends(get_current_admin)
):
    from routers.generate import (
        AVAILABLE_IMAGE_TO_SKIN_MODELS,
        AVAILABlE_TEXT_TO_IMAGE_MODELS,
        AVAILABLE_IMAGE_EDIT_MODELS
    )
    from backend_utils import get_model_credit_cost, is_model_pro_exclusive, is_model_under_maintenance
    
    all_models = (
        [m.replace(".safetensors", "") for m in AVAILABLE_IMAGE_TO_SKIN_MODELS] +
        AVAILABlE_TEXT_TO_IMAGE_MODELS +
        AVAILABLE_IMAGE_EDIT_MODELS
    )
    prices = {}
    for m in all_models:
        prices[m] = {
            "credits": get_model_credit_cost(m),
            "is_pro": is_model_pro_exclusive(m),
            "under_maintenance": is_model_under_maintenance(m)
        }
    return prices


@router.post("/model_prices")
@limiter.limit(MONITOR_CONFIG_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def set_model_price_endpoint(
    request: Request,
    req: SetModelPriceRequest,
    admin: User = Depends(get_current_admin)
):
    if req.credits < 0:
        raise HTTPException(status_code=400, detail="Credits cannot be negative")
    try:
        redis_conn.set(f"config:model_price:{req.model_name}", str(req.credits))
        redis_conn.set(f"config:model_pro:{req.model_name}", "1" if req.is_pro else "0")
        redis_conn.set(f"config:model_maintenance:{req.model_name}", "1" if req.under_maintenance else "0")
        return {
            "model_name": req.model_name,
            "credits": req.credits,
            "is_pro": req.is_pro,
            "under_maintenance": req.under_maintenance
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update Redis settings: {e}")




@router.delete("/users/by-email")
@limiter.limit("1/minute; 10/hour", key_func=get_authenticated_or_remote_address, override_defaults=False)
def admin_delete_user_by_email(
    request: Request,
    email: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    """Admin-only: Permanently delete a user account and all their associated data (S3 and DB cleanup)"""
    target_email = email.strip().lower()
    user = db.query(User).filter(func.lower(User.email) == target_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    user_id = user.id

    # 1. Collect S3 files of all generation logs belonging to this user for deletion
    logs = db.query(GenerationLog).filter(GenerationLog.user_id == user_id).all()
    log_ids = [log.id for log in logs]
    
    from routers.generate import cancel_generation_jobs
    from skin_withdrawal import begin, resume
    for log in logs:
        begin(db, log)
        cancel_generation_jobs(log.id)
        resume(db, log, allow_pending_cdn=True)

    # 2. Delete Collection Items and Collections
    collections = db.query(Collection).filter(Collection.user_id == user_id).all()
    col_ids = [c.id for c in collections]
    if col_ids:
        db.query(CollectionItem).filter(CollectionItem.collection_id.in_(col_ids)).delete(synchronize_session=False)
    db.query(Collection).filter(Collection.user_id == user_id).delete(synchronize_session=False)

    # 3. Clean up references to user's generation logs in CollectionItem, UserLike, UserFeedback
    if log_ids:
        db.query(CollectionItem).filter(CollectionItem.log_id.in_(log_ids)).delete(synchronize_session=False)
        db.query(UserLike).filter(UserLike.log_id.in_(log_ids)).delete(synchronize_session=False)
        db.query(UserFeedback).filter(UserFeedback.log_id.in_(log_ids)).delete(synchronize_session=False)
    
    # 4. Delete the generation logs
    db.query(GenerationLog).filter(GenerationLog.user_id == user_id).delete(synchronize_session=False)

    # 5. Delete User's Likes, Feedbacks, and Shipping Addresses
    db.query(UserLike).filter(UserLike.user_id == user_id).delete(synchronize_session=False)
    db.query(UserFeedback).filter(UserFeedback.user_id == user_id).delete(synchronize_session=False)
    db.query(ShippingAddress).filter(ShippingAddress.user_id == user_id).delete(synchronize_session=False)

    # 6. Delete Orders and Order Items
    orders = db.query(Order).filter(Order.user_id == user_id).all()
    order_ids = [o.id for o in orders]
    if order_ids:
        db.query(OrderItem).filter(OrderItem.order_id.in_(order_ids)).delete(synchronize_session=False)
    db.query(Order).filter(Order.user_id == user_id).delete(synchronize_session=False)

    # 7. Delete Forum Posts, Forum Comments, Forum Post Likes, and Notifications
    # First, comments and likes on the user's posts
    posts = db.query(ForumPost).filter(ForumPost.user_id == user_id).all()
    post_ids = [p.id for p in posts]
    if post_ids:
        db.query(ForumComment).filter(ForumComment.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(ForumPostLike).filter(ForumPostLike.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(ForumNotification).filter(ForumNotification.post_id.in_(post_ids)).delete(synchronize_session=False)
    
    # Delete user's own posts
    db.query(ForumPost).filter(ForumPost.user_id == user_id).delete(synchronize_session=False)
    
    # Delete user's comments on other posts
    db.query(ForumComment).filter(ForumComment.user_id == user_id).delete(synchronize_session=False)
    
    # Delete user's post likes
    db.query(ForumPostLike).filter(ForumPostLike.user_id == user_id).delete(synchronize_session=False)
    
    # Delete notifications sent to user or by user
    db.query(ForumNotification).filter(
        (ForumNotification.user_id == user_id) | (ForumNotification.sender_id == user_id)
    ).delete(synchronize_session=False)

    # 8. Finally delete the user
    db.delete(user)
    db.commit()

    return {"message": f"User with email {email} and all associated data have been permanently deleted."}


class GiftActiveUsersRequest(BaseModel):
    amount: int
    message: str


@router.post("/gift_active_users")
@limiter.limit(MONITOR_BULK_GIFT_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def gift_credits_to_seven_day_active_users(
    request: Request,
    req: GiftActiveUsersRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message is required")
        
    try:
        now_utc = datetime.now(timezone.utc)
        active_since = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
        users = db.query(User)\
            .join(CreditLog, CreditLog.user_id == User.id)\
            .filter(
                CreditLog.action == "daily_login",
                CreditLog.created_at >= active_since,
                CreditLog.created_at <= now_utc
            )\
            .distinct()\
            .all()
        print(f"Admin gifting {req.amount} credits to {len(users)} seven-day active users...")
        
        for u in sorted(users, key=lambda item: item.id):
            lock_balance(db, u)
            u.credits = (u.credits or 0) + req.amount
            
            # Generate a new CreditLog
            gift_log = CreditLog(
                user_id=u.id,
                amount=req.amount,
                action="system_gift",
                source=req.message.strip()
            )
            db.add(gift_log)
            db.flush() # Flush to get gift_log.id
            
            # Create system_gift ForumNotification
            notif = ForumNotification(
                user_id=u.id,
                sender_id=None,
                type="system_gift",
                post_id=gift_log.id,
                comment_id=str(req.amount),
                is_read=False
            )
            db.add(notif)
            
        db.commit()
        return {
            "status": "success",
            "gifted_users": len(users),
            "message": f"Successfully gifted {req.amount} credits to {len(users)} seven-day active users"
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error during gifting: {e}")


@router.post("/gift_pro_users")
@limiter.limit(MONITOR_BULK_GIFT_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def gift_credits_to_pro_users(
    request: Request,
    req: GiftActiveUsersRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message is required")
        
    try:
        all_users = db.query(User).filter(
            User.pro_level != "free",
            User.pro_expires_at.isnot(None)
        ).all()
        users = [u for u in all_users if u.is_pro]
        print(f"Admin gifting {req.amount} credits to {len(users)} Pro users...")
        
        for u in sorted(users, key=lambda item: item.id):
            lock_balance(db, u)
            u.credits = (u.credits or 0) + req.amount
            
            # Generate a new CreditLog
            gift_log = CreditLog(
                user_id=u.id,
                amount=req.amount,
                action="system_gift",
                source=req.message.strip()
            )
            db.add(gift_log)
            db.flush() # Flush to get gift_log.id
            
            # Create system_gift ForumNotification
            notif = ForumNotification(
                user_id=u.id,
                sender_id=None,
                type="system_gift",
                post_id=gift_log.id,
                comment_id=str(req.amount),
                is_read=False
            )
            db.add(notif)
            
        db.commit()
        return {
            "status": "success",
            "gifted_users": len(users),
            "message": f"Successfully gifted {req.amount} credits to {len(users)} Pro users"
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error during gifting: {e}")


class GiftSpecificUserRequest(BaseModel):
    email: str
    amount: int
    message: str


@router.post("/gift_specific_user")
@limiter.limit(MONITOR_BULK_GIFT_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def gift_credits_to_specific_user(
    request: Request,
    req: GiftSpecificUserRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message is required")
    if not req.email.strip():
        raise HTTPException(status_code=400, detail="Email is required")

    target_email = req.email.strip().lower()
    user = db.query(User).filter(func.lower(User.email) == target_email).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User with email {req.email} not found")

    try:
        lock_balance(db, user)
        user.credits = (user.credits or 0) + req.amount

        # Generate a new CreditLog
        gift_log = CreditLog(
            user_id=user.id,
            amount=req.amount,
            action="system_gift",
            source=req.message.strip()
        )
        db.add(gift_log)
        db.flush() # Flush to get gift_log.id

        # Create system_gift ForumNotification
        notif = ForumNotification(
            user_id=user.id,
            sender_id=None,
            type="system_gift",
            post_id=gift_log.id,
            comment_id=str(req.amount),
            is_read=False
        )
        db.add(notif)
        
        db.commit()
        return {
            "status": "success",
            "user_id": user.id,
            "username": user.username,
            "email": user.email,
            "new_credits": user.credits,
            "message": f"Successfully gifted {req.amount} credits to user {user.username} ({user.email})"
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error during gifting: {e}")



@router.get("/sking_ddj_generations")
@limiter.limit(MONITOR_READ_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def get_sking_ddj_generations(
    request: Request,
    page: int = 1,
    page_size: int = 10,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 10
    elif page_size > 100:
        page_size = 100

    offset = (page - 1) * page_size

    # Query logs where model_version starts with "SKING_DDJ" and is_deleted is False
    query = db.query(GenerationLog).filter(
        GenerationLog.model_version.like("SKING_DDJ%"),
        GenerationLog.is_deleted == False
    )

    total_count = query.count()

    results = query.order_by(
        GenerationLog.created_at.desc()
    ).offset(offset).limit(page_size).all()

    items = []
    for log in results:
        items.append({
            "id": log.id,
            "prompt": log.prompt,
            "mode": log.mode,
            "status": log.status,
            "model_version": log.model_version,
            "provider_task_id": log.provider_task_id,
            "provider_submission_state": log.provider_submission_state,
            "error_msg": log.error_msg,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "source_url": log.source_url if log.source else None,
            "edited_image_url": log.edited_image_url if log.edited_result else None,
            "image_to_skin_edited_image_url": (
                log.image_to_skin_edited_image_url
                if log.image_to_skin_edited_result
                else None
            ),
            "result_url": log.result_url if log.result else None,
        })

    total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1

    return {
        "items": items,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }


def _match_subscription_credit_log(
    order: Order,
    user: Optional[User],
    user_credit_logs: List[CreditLog],
    expected_credits: int,
) -> tuple:
    """
    Match a paid subscription order to its corresponding CreditLog.
    Returns (matched_credit_log, grant_status, status_message).
    """
    order_paid_time = order.paid_at or order.created_at
    if order_paid_time and order_paid_time.tzinfo is None:
        order_paid_time = order_paid_time.replace(tzinfo=timezone.utc)

    # Filter logs belonging to this user
    user_logs = [l for l in user_credit_logs if l.user_id == order.user_id and l.action == "subscription_grant"]

    matched_log: Optional[CreditLog] = None

    # Priority 0: Explicit compensation log for this order
    for log in user_logs:
        if log.idempotency_key and f"compensation:{order.id}:" in log.idempotency_key:
            matched_log = log
            break
        if log.source and f"Order {order.id}" in log.source:
            matched_log = log
            break

    # Priority 1: Match by idempotency key containing paid_at ISO string (payment cycle key)
    if not matched_log and order.paid_at:
        paid_iso = order.paid_at.astimezone(timezone.utc).isoformat()
        paid_date_str = paid_iso[:19]
        for log in user_logs:
            if log.idempotency_key and paid_date_str in log.idempotency_key:
                matched_log = log
                break

    # Priority 2: Match by PayPal sale ID or subscription ID in idempotency_key or source
    if not matched_log:
        identifiers = []
        if order.paypal_order_id:
            identifiers.append(order.paypal_order_id)
        if user and user.paypal_subscription_id:
            identifiers.append(user.paypal_subscription_id)

        for ident in identifiers:
            for log in user_logs:
                if (log.idempotency_key and ident in log.idempotency_key) or (log.source and ident in log.source):
                    if order_paid_time and log.created_at:
                        log_time = log.created_at if log.created_at.tzinfo else log.created_at.replace(tzinfo=timezone.utc)
                        # Within 35 days (a billing cycle)
                        if abs((log_time - order_paid_time).total_seconds()) <= 35 * 86400:
                            matched_log = log
                            break
                    else:
                        matched_log = log
                        break
            if matched_log:
                break

    # Priority 3: Proximity in time within 48 hours of payment
    if not matched_log and order_paid_time:
        best_diff = None
        for log in user_logs:
            if log.created_at:
                log_time = log.created_at if log.created_at.tzinfo else log.created_at.replace(tzinfo=timezone.utc)
                diff = abs((log_time - order_paid_time).total_seconds())
                if diff <= 48 * 3600:
                    if best_diff is None or diff < best_diff:
                        best_diff = diff
                        matched_log = log

    if not matched_log:
        return None, "missing", "扣款已确认，但缺少积分发放记录"
    elif matched_log.amount >= expected_credits:
        return matched_log, "success", "积分已成功发放"
    else:
        return matched_log, "mismatch", f"实充积分({matched_log.amount})低于方案应充额度({expected_credits})"


@router.get("/subscription-credit-audits")
@limiter.limit(MONITOR_READ_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def get_subscription_credit_audits(
    request: Request,
    page: int = 1,
    page_size: int = 15,
    status_filter: str = "all",
    search: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 15
    elif page_size > 100:
        page_size = 100

    query = db.query(Order).filter(
        Order.order_type == "subscription",
        Order.status == "paid"
    )

    if search and search.strip():
        search_term = search.strip()
        query = query.outerjoin(User, User.id == Order.user_id).filter(
            (Order.id.ilike(f"%{search_term}%")) |
            (Order.paypal_order_id.ilike(f"%{search_term}%")) |
            (Order.user_id == search_term) |
            (User.email.ilike(f"%{search_term}%")) |
            (User.username.ilike(f"%{search_term}%")) |
            (User.paypal_subscription_id.ilike(f"%{search_term}%"))
        )

    # Sort descending by payment time
    query = query.order_by(func.coalesce(Order.paid_at, Order.created_at).desc())
    matched_orders = query.all()

    if not matched_orders:
        return {
            "summary": {
                "total_orders": 0,
                "success_count": 0,
                "anomaly_count": 0,
                "total_credits_granted": 0,
                "total_revenue": 0.0,
            },
            "items": [],
            "total_count": 0,
            "page": page,
            "page_size": page_size,
            "total_pages": 1,
        }

    # Batch fetch related users, order items, and credit logs
    order_ids = [o.id for o in matched_orders]
    user_ids = list({o.user_id for o in matched_orders})

    users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_map = {u.id: u for u in users}

    order_items = db.query(OrderItem).filter(OrderItem.order_id.in_(order_ids)).all()
    order_item_map = {item.order_id: item for item in order_items}

    credit_logs = db.query(CreditLog).filter(
        CreditLog.user_id.in_(user_ids),
        CreditLog.action == "subscription_grant"
    ).order_by(CreditLog.created_at.desc()).all()

    logs_by_user: Dict[str, List[CreditLog]] = {}
    for log in credit_logs:
        logs_by_user.setdefault(log.user_id, []).append(log)

    all_audited_items = []
    total_orders = len(matched_orders)
    success_count = 0
    anomaly_count = 0
    total_credits_granted = 0
    total_revenue = 0.0

    for order in matched_orders:
        user = user_map.get(order.user_id)
        item = order_item_map.get(order.id)

        # Plan type and expected credits
        is_pro_max = (item and item.model_type == "pro-max") or (user and user.pro_level == "pro-max")
        expected_credits = 200 if is_pro_max else 80
        plan_type = "pro-max" if is_pro_max else "pro-plus"
        plan_name = "Pro Max" if is_pro_max else "Pro Plus"

        user_logs = logs_by_user.get(order.user_id, [])
        matched_log, grant_status, status_message = _match_subscription_credit_log(
            order, user, user_logs, expected_credits
        )

        granted_credits = matched_log.amount if matched_log else 0
        total_revenue += float(order.price or 0.0)

        if grant_status == "success":
            success_count += 1
            total_credits_granted += granted_credits
        else:
            anomaly_count += 1
            total_credits_granted += granted_credits

        audit_record = {
            "order_id": order.id,
            "user_id": order.user_id,
            "user_email": user.email if user else "Unknown/Deleted User",
            "user_username": user.username if user else None,
            "user_current_credits": user.credits if user else 0,
            "user_pro_level": user.pro_level if user else None,
            "paypal_subscription_id": user.paypal_subscription_id if user else None,
            "paypal_order_id": order.paypal_order_id,
            "plan_type": plan_type,
            "plan_name": plan_name,
            "price": float(order.price or 0.0),
            "paid_at": order.paid_at.isoformat() if order.paid_at else None,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "expected_credits": expected_credits,
            "granted_credits": granted_credits,
            "grant_status": grant_status,
            "status_message": status_message,
            "credit_log": {
                "id": matched_log.id,
                "amount": matched_log.amount,
                "action": matched_log.action,
                "source": matched_log.source,
                "idempotency_key": matched_log.idempotency_key,
                "created_at": matched_log.created_at.isoformat() if matched_log.created_at else None,
            } if matched_log else None,
        }
        all_audited_items.append(audit_record)

    # Filter by status
    if status_filter == "anomaly":
        filtered_items = [item for item in all_audited_items if item["grant_status"] in ("missing", "mismatch")]
    elif status_filter == "success":
        filtered_items = [item for item in all_audited_items if item["grant_status"] == "success"]
    else:
        filtered_items = all_audited_items

    total_filtered_count = len(filtered_items)
    total_pages = math.ceil(total_filtered_count / page_size) if total_filtered_count > 0 else 1
    offset = (page - 1) * page_size
    paged_items = filtered_items[offset : offset + page_size]

    return {
        "summary": {
            "total_orders": total_orders,
            "success_count": success_count,
            "anomaly_count": anomaly_count,
            "total_credits_granted": total_credits_granted,
            "total_revenue": round(total_revenue, 2),
        },
        "items": paged_items,
        "total_count": total_filtered_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.post("/subscription-credit-audits/{order_id}/compensate")
@limiter.limit(MONITOR_WRITE_RATE_LIMIT, key_func=get_authenticated_or_remote_address, override_defaults=False)
async def compensate_subscription_credits(
    request: Request,
    order_id: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.order_type != "subscription":
        raise HTTPException(status_code=400, detail="Order is not a subscription order")
    if order.status != "paid":
        raise HTTPException(status_code=400, detail=f"Order status is '{order.status}', cannot compensate unpaid order")

    user = db.query(User).filter(User.id == order.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Associated user not found")

    item = db.query(OrderItem).filter(OrderItem.order_id == order.id).first()
    expected_credits = 200 if ((item and item.model_type == "pro-max") or user.pro_level == "pro-max") else 80

    # Fetch user's credit logs
    user_logs = db.query(CreditLog).filter(
        CreditLog.user_id == user.id,
        CreditLog.action == "subscription_grant",
    ).all()

    matched_log, grant_status, _ = _match_subscription_credit_log(order, user, user_logs, expected_credits)
    if grant_status == "success" and matched_log and matched_log.amount >= expected_credits:
        raise HTTPException(
            status_code=400,
            detail=f"Order already has full credits ({matched_log.amount}) granted in log {matched_log.id}"
        )

    already_granted = matched_log.amount if matched_log else 0
    missing_credits = expected_credits - already_granted

    if missing_credits <= 0:
        raise HTTPException(status_code=400, detail="No missing credits to compensate")

    try:
        lock_balance(db, user)
        user.credits = (user.credits or 0) + missing_credits

        sub_id_ref = user.paypal_subscription_id or order.paypal_order_id or "Direct"
        comp_log = CreditLog(
            user_id=user.id,
            amount=missing_credits,
            action="subscription_grant",
            source=f"Admin Compensation for Order {order.id}: {sub_id_ref}",
            idempotency_key=f"compensation:{order.id}:{int(datetime.now(timezone.utc).timestamp())}",
        )
        db.add(comp_log)
        db.flush()

        notif = ForumNotification(
            user_id=user.id,
            sender_id=None,
            type="subscription_grant",
            post_id=None,
            comment_id=str(missing_credits),
            is_read=False,
        )
        db.add(notif)
        db.commit()

        return {
            "status": "success",
            "order_id": order.id,
            "user_id": user.id,
            "user_email": user.email,
            "compensated_credits": missing_credits,
            "new_user_credits": user.credits,
            "credit_log_id": comp_log.id,
            "message": f"Successfully compensated {missing_credits} credits to {user.email} for order {order.id}",
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to compensate credits: {e}")
