"""Resumable skin deletion. Success means origin AND CDN are withdrawn."""
import copy
import logging
from functools import lru_cache

import boto3
from fastapi import HTTPException

import models
from config import settings
from s3_utils import s3_client

logger = logging.getLogger(__name__)
ASSET_FIELDS = ("source", "result", "edited_result", "image_to_skin_edited_result")


@lru_cache(maxsize=1)
def cloudfront_client():
    return boto3.client("cloudfront", aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                        region_name=settings.AWS_REGION or None)


def _save(db, log, state):
    log.withdrawal = copy.deepcopy(state)
    db.flush()


def begin(db, log):
    log = db.query(models.GenerationLog).filter_by(id=log.id).with_for_update().populate_existing().one()
    if log.withdrawal:
        if log.withdrawal.get("action") != "delete":
            raise HTTPException(status_code=409, detail="A previous file withdrawal is still pending")
        return
    keys = sorted({getattr(log, field) for field in ASSET_FIELDS if getattr(log, field)})
    if any(key.startswith(("http:", "https:")) for key in keys):
        raise HTTPException(status_code=409, detail="Legacy external assets require storage migration before withdrawal")
    # In-flight workers consult this tombstone before publishing results.
    log.is_deleted = True
    _save(db, log, {"action": "delete", "public": log.is_public, "keys": keys,
                    "deleted": [], "invalidation": None})
    db.commit()


def resume(db, log):
    log = db.query(models.GenerationLog).filter_by(id=log.id).with_for_update().populate_existing().one()
    state = copy.deepcopy(log.withdrawal)
    if not state:
        return
    if state.get("action") != "delete":
        raise HTTPException(status_code=409, detail="Unsupported file withdrawal action")
    public = state["public"]
    try:
        bucket = settings.AWS_BUCKET_NAME if public else settings.AWS_PRIVATE_BUCKET_NAME
        for key in state["keys"]:
            if key not in state["deleted"]:
                s3_client.delete_object(Bucket=bucket, Key=key)
                state["deleted"].append(key)
                _save(db, log, state)

        if public and state["keys"] and settings.AWS_CDN_DOMAIN.strip():
            distribution_id = settings.AWS_CLOUDFRONT_DISTRIBUTION_ID.strip()
            if not distribution_id:
                raise RuntimeError("CloudFront distribution ID is required to withdraw public files")
            cdn = cloudfront_client()
            if not state["invalidation"]:
                result = cdn.create_invalidation(
                    DistributionId=distribution_id,
                    InvalidationBatch={
                        # Include query-string variants cached by a distribution.
                        "Paths": {"Quantity": len(state["keys"]), "Items": [f"/{key.lstrip('/')}*" for key in state["keys"]]},
                        "CallerReference": f"skin-withdrawal-{log.id}-{state['action']}",
                    },
                )
                state["invalidation"] = result["Invalidation"]["Id"]
                _save(db, log, state)
            result = cdn.get_invalidation(DistributionId=distribution_id, Id=state["invalidation"])
            if result["Invalidation"]["Status"] != "Completed":
                raise HTTPException(status_code=503, detail="CDN withdrawal is pending; retry shortly")

        # No new URL is signed while a manifest exists. References are removed
        # only after storage succeeds.
        items = db.query(models.CollectionItem).filter(models.CollectionItem.log_id == log.id)
        for item in items.all():
            db.delete(item)
        likes = db.query(models.UserLike).filter_by(log_id=log.id)
        likes.delete(synchronize_session=False)
        log.likes_count = 0
        for key in state["keys"]:
            # Escape LIKE wildcards in object keys.
            escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            db.query(models.User).filter(models.User.skin_url.like(f"%{escaped}%", escape="\\")).update({"skin_url": None}, synchronize_session=False)
        db.query(models.UserFeedback).filter_by(log_id=log.id).delete(synchronize_session=False)
        log.prompt = None
        log.name = "Deleted"
        log.status = "deleted"
        for field in ASSET_FIELDS:
            setattr(log, field, None)
        log.withdrawal = None
        db.commit()
    except HTTPException:
        _save(db, log, state)
        db.commit()
        raise
    except Exception:
        try:
            _save(db, log, state)
            db.commit()
        except Exception:
            db.rollback()
        logger.exception("Skin withdrawal remains pending for %s", log.id)
        raise HTTPException(status_code=503, detail="File withdrawal is incomplete; it will be retried")


def retry_pending():
    from database import SessionLocal
    with SessionLocal() as db:
        ids = [row.id for row in db.query(models.GenerationLog.id).filter(models.GenerationLog.withdrawal.isnot(None))]
        for log_id in ids:
            try:
                log = db.query(models.GenerationLog).filter_by(id=log_id).with_for_update().populate_existing().first()
                if log:
                    resume(db, log)
            except HTTPException:
                pass


async def start_withdrawal_job():
    import asyncio
    while True:
        await asyncio.to_thread(retry_pending)
        await asyncio.sleep(30)
