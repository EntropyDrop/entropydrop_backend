from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from redis import Redis
from rq import Worker, Queue
from config import settings
import json
from datetime import datetime, timezone, timedelta
from auth import get_current_admin
from models import User, GenerationLog, Order, CollectionItem, UserLike, UserFeedback, Collection, ShippingAddress, OrderItem, ForumPost, ForumComment, ForumPostLike, ForumNotification, CreditLog
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from rate_limit import limiter
from instance_monitor import list_backend_instance_history, list_backend_instance_metrics

router = APIRouter(
    prefix="/api/monitor",
    tags=["monitor"],
)

# Use the same connection params as in worker_tasks.py
redis_conn = Redis.from_url(settings.REDIS_URL)


@router.get("/backend-instances/history")
@limiter.exempt
async def get_backend_instance_history(
    admin: User = Depends(get_current_admin),
):
    return list_backend_instance_history(
        redis_conn,
        history_hours=max(1, settings.BACKEND_METRICS_HISTORY_HOURS),
        bucket_seconds=max(60, settings.BACKEND_METRICS_HISTORY_BUCKET_SECONDS),
    )


@router.get("/backend-instances")
@limiter.exempt
async def get_backend_instances(
    admin: User = Depends(get_current_admin),
):
    return list_backend_instance_metrics(
        redis_conn,
        stale_after_seconds=max(1, settings.BACKEND_METRICS_STALE_AFTER_SECONDS),
    )

@router.get("/stats")
@limiter.exempt
async def get_monitor_stats(
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
@limiter.exempt
async def get_unfinished_logs(
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
@limiter.exempt
async def admin_delete_log(
    id: str,
    background_tasks: BackgroundTasks,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Admin-only: Delete any skin generation record and associated data (soft delete + S3 cleaning)"""
    log = db.query(GenerationLog).filter(GenerationLog.id == id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
        
    # 1. Collect S3 files that need cleaning
    files_to_delete = []
    if log.source:
        files_to_delete.append((log.source, log.is_public))
    if log.result:
        files_to_delete.append((log.result, log.is_public))
    if log.edited_result:
        files_to_delete.append((log.edited_result, log.is_public))

    # 2. Trigger background cleaning task
    from routers.generate import cancel_generation_jobs, delete_s3_files_task
    cancel_generation_jobs(log.id)
    if files_to_delete:
        background_tasks.add_task(delete_s3_files_task, files_to_delete)

    # 3. Clean database attributes (soft delete)
    log.is_deleted = True
    log.prompt = None
    log.name = "Deleted"
    log.source = None
    log.result = None
    log.edited_result = None
    log.status = "deleted"

    # 4. Delete associated collection items
    db.query(CollectionItem).filter(CollectionItem.log_id == id).delete()
    
    # 5. Delete associated likes
    db.query(UserLike).filter(UserLike.log_id == id).delete()
    
    # 6. Delete associated feedback
    db.query(UserFeedback).filter(UserFeedback.log_id == id).delete()
    
    db.commit()
    
    return {"message": f"Creation {id} soft-deleted, properties cleared, and files queued for S3 deletion by admin"}


from pydantic import BaseModel



class ModeMaintenanceToggleRequest(BaseModel):
    enabled: bool

@router.get("/mode_status")
@limiter.exempt
async def get_modes_status(
    admin: User = Depends(get_current_admin)
):
    from backend_utils import is_text_to_skin_enabled, is_image_to_skin_enabled, is_image_edit_to_skin_enabled
    return {
        "text_to_skin_enabled": is_text_to_skin_enabled(),
        "image_to_skin_enabled": is_image_to_skin_enabled(),
        "image_edit_to_skin_enabled": is_image_edit_to_skin_enabled(),
    }

@router.post("/mode_status/{mode_name}")
@limiter.exempt
async def toggle_mode_status(
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



class DailyFreeCreditsRequest(BaseModel):
    credits: int


class SetModelPriceRequest(BaseModel):
    model_name: str
    credits: int
    is_pro: bool = False

@router.get("/daily_free_credits")
@limiter.exempt
async def get_daily_free_credits_endpoint(
    admin: User = Depends(get_current_admin)
):
    from backend_utils import get_daily_free_credits
    return {"credits": get_daily_free_credits()}

@router.post("/daily_free_credits")
@limiter.exempt
async def set_daily_free_credits_endpoint(
    req: DailyFreeCreditsRequest,
    admin: User = Depends(get_current_admin)
):
    if req.credits < 0:
        raise HTTPException(status_code=400, detail="Credits cannot be negative")
    try:
        redis_conn.set("config:daily_free_credits", str(req.credits))
        return {"credits": req.credits}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update Redis settings: {e}")


@router.get("/model_prices")
@limiter.exempt
async def get_model_prices_endpoint(
    admin: User = Depends(get_current_admin)
):
    from routers.generate import (
        AVAILABLE_IMAGE_TO_SKIN_MODELS,
        AVAILABlE_TEXT_TO_IMAGE_MODELS,
        AVAILABLE_IMAGE_EDIT_MODELS
    )
    from backend_utils import get_model_credit_cost, is_model_pro_exclusive
    
    all_models = (
        [m.replace(".safetensors", "") for m in AVAILABLE_IMAGE_TO_SKIN_MODELS] +
        AVAILABlE_TEXT_TO_IMAGE_MODELS +
        AVAILABLE_IMAGE_EDIT_MODELS
    )
    prices = {}
    for m in all_models:
        prices[m] = {
            "credits": get_model_credit_cost(m),
            "is_pro": is_model_pro_exclusive(m)
        }
    return prices


@router.post("/model_prices")
@limiter.exempt
async def set_model_price_endpoint(
    req: SetModelPriceRequest,
    admin: User = Depends(get_current_admin)
):
    if req.credits < 0:
        raise HTTPException(status_code=400, detail="Credits cannot be negative")
    try:
        redis_conn.set(f"config:model_price:{req.model_name}", str(req.credits))
        redis_conn.set(f"config:model_pro:{req.model_name}", "1" if req.is_pro else "0")
        return {"model_name": req.model_name, "credits": req.credits, "is_pro": req.is_pro}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update Redis settings: {e}")




@router.delete("/users/by-email")
@limiter.exempt
async def admin_delete_user_by_email(
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
    files_to_delete = []
    for log in logs:
        cancel_generation_jobs(log.id)
        if log.source:
            files_to_delete.append((log.source, log.is_public))
        if log.result:
            files_to_delete.append((log.result, log.is_public))
        if log.edited_result:
            files_to_delete.append((log.edited_result, log.is_public))

    if files_to_delete:
        from routers.generate import delete_s3_files_task
        background_tasks.add_task(delete_s3_files_task, files_to_delete)

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
@limiter.exempt
async def gift_credits_to_seven_day_active_users(
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
        
        for u in users:
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


class GiftSpecificUserRequest(BaseModel):
    email: str
    amount: int
    message: str


@router.post("/gift_specific_user")
@limiter.exempt
async def gift_credits_to_specific_user(
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
@limiter.exempt
async def get_sking_ddj_generations(
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
            "pipeline_version": log.pipeline_version,
            "error_msg": log.error_msg,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "source_url": log.source_url if log.source else None,
            "edited_image_url": log.edited_image_url if log.edited_result else None,
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
