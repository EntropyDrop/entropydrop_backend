from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, BackgroundTasks, Query
import io
import json
import os
from PIL import Image
from typing import Optional
from sqlalchemy.orm import Session
import models
import schemas
import auth
from database import get_db
from s3_utils import get_cdn_url, get_s3_url, delete_from_s3, upload_to_s3, generate_presigned_url_get
import backend_utils
from config import settings
from redis import Redis
from rq import Queue, Retry
import httpx
from rate_limit import limiter

redis_conn = Redis.from_url(
    settings.REDIS_URL,
    health_check_interval=20,
    socket_timeout=12,
    socket_connect_timeout=12,
    retry_on_timeout=True
)

router = APIRouter(prefix="/api", tags=["generate"])
ACTIVE_GENERATION_STATUSES = ["pending", "processing", "pending_skin", "processing_skin"]
RECOVERABLE_GENERATION_STATUSES = ACTIVE_GENERATION_STATUSES
SECOND_STAGE_STATUSES = {"pending_skin", "processing_skin"}
TWO_STAGE_GENERATION_MODES = {"aigc_text_to_skin", "aigc_image_edit_to_skin"}
RESULT_QUEUE_KEY = os.getenv("GENERATE_RESULT_QUEUE_KEY", "generate_results")
RESULT_PROCESSING_QUEUE_KEY = os.getenv("GENERATE_RESULT_PROCESSING_QUEUE_KEY", "generate_results_processing")
GENERATION_RECOVERY_MIN_AGE_SECONDS = int(os.getenv("GENERATION_RECOVERY_MIN_AGE_SECONDS", "300"))

import random
import time
import asyncio
from database import SessionLocal

def make_generation_job_id(log_id: str, stage: str) -> str:
    return f"generation_{log_id}_{stage}"


def get_generation_retry_policy() -> Retry:
    return Retry(max=99999, interval=[5, 10, 30, 60])


def enqueue_image_to_skin_task(log: models.GenerationLog, is_pro_active: bool, content_type: str = "image/png"):
    prefix = "high_" if is_pro_active else ""
    q_skin = Queue(f'{prefix}queue_image_to_skin', connection=redis_conn)
    retry_policy = get_generation_retry_policy()

    source = log.source
    skin_content_type = content_type
    intermediate_filename = None

    if log.mode in TWO_STAGE_GENERATION_MODES:
        source = log.edited_result
        skin_content_type = "image/jpeg"
        intermediate_filename = log.edited_result

    if not source:
        raise Exception(f"Cannot enqueue image_to_skin for {log.id}: missing source image")

    kwargs = {
        "model_version": log.model_version,
        "seed": log.seed,
        "n_step": log.n_step,
        "guidance": log.guidance
    }
    if intermediate_filename:
        kwargs["intermediate_filename"] = intermediate_filename

    return q_skin.enqueue(
        "worker_tasks.task_image_to_skin",
        args=(log.id, log.is_public, source, skin_content_type, log.prompt),
        kwargs=kwargs,
        job_timeout='120s',
        retry=retry_policy,
        result_ttl=10,
        job_id=make_generation_job_id(log.id, "image_to_skin")
    )


def enqueue_generation_task(log: models.GenerationLog, is_pro_active: bool, content_type: str = "image/png"):
    prefix = "high_" if is_pro_active else ""
    
    q_t2i = Queue(f'{prefix}queue_text_to_image', connection=redis_conn)
    q_edit = Queue(f'{prefix}queue_image_edit', connection=redis_conn)
    
    retry_policy = get_generation_retry_policy()

    if log.mode == "aigc_text_to_skin":
        q_t2i.enqueue(
            "worker_tasks.task_text_to_image",
            args=(log.id, log.is_public, log.prompt, log.model_version, log.seed, log.n_step, log.guidance),
            job_timeout='120s',
            retry=retry_policy,
            result_ttl=10,
            job_id=make_generation_job_id(log.id, "text_to_image")
        )
    elif log.mode == "aigc_image_edit_to_skin":
        q_edit.enqueue(
            "worker_tasks.task_image_edit",
            args=(log.id, log.is_public, log.source, content_type, log.prompt, log.model_version, log.seed, log.n_step, log.guidance),
            job_timeout='120s',
            retry=retry_policy,
            result_ttl=10,
            job_id=make_generation_job_id(log.id, "image_edit")
        )
    elif log.mode == "aigc_image_to_skin":
        enqueue_image_to_skin_task(log, is_pro_active, content_type)
    else:
        raise Exception("Unsupported mode")


def get_queue_position(db: Session, log_id: str) -> int:
    """
    Calculate the position of a given generation log in its corresponding queue
    """
    log = db.query(models.GenerationLog).filter(models.GenerationLog.id == log_id).first()
    if not log:
        return 0
    if log.status in ["success", "failed"]:
        return 0
        
    if log.status in ["pending_skin", "processing_skin"]:
        # Stage 2: waiting in image_to_skin queue.
        # This queue processes both direct image-to-skin tasks, and multi-stage tasks in Stage 2.
        count = db.query(models.GenerationLog).filter(
            models.GenerationLog.created_at < log.created_at,
            (
                (models.GenerationLog.mode == "aigc_image_to_skin") & models.GenerationLog.status.in_(["pending", "processing_skin"])
            ) | (
                (models.GenerationLog.mode.in_(["aigc_text_to_skin", "aigc_image_edit_to_skin"])) & models.GenerationLog.status.in_(["pending_skin", "processing_skin"])
            )
        ).count()
        return count
    else:
        # Stage 1: waiting in the first queue (queue_text_to_image, queue_image_edit, or queue_image_to_skin for single stage).
        if log.mode == "aigc_text_to_skin":
            count = db.query(models.GenerationLog).filter(
                models.GenerationLog.created_at < log.created_at,
                models.GenerationLog.mode == "aigc_text_to_skin",
                models.GenerationLog.status.in_(["pending", "processing"])
            ).count()
        elif log.mode == "aigc_image_edit_to_skin":
            count = db.query(models.GenerationLog).filter(
                models.GenerationLog.created_at < log.created_at,
                models.GenerationLog.mode == "aigc_image_edit_to_skin",
                models.GenerationLog.status.in_(["pending", "processing"])
            ).count()
        else: # "aigc_image_to_skin"
            count = db.query(models.GenerationLog).filter(
                models.GenerationLog.created_at < log.created_at,
                models.GenerationLog.mode == "aigc_image_to_skin",
                models.GenerationLog.status.in_(["pending", "processing_skin"])
            ).count()
        return count


def delete_s3_files_task(files: list[tuple[Optional[str], bool]]):
    """
    Background task: batch delete S3 files
    files: [(key, is_public), ...]
    """
    for key, is_public in files:
        if key:
            delete_from_s3(key, is_public)

def display_log_name(log):
    if log.name:
        return log.name
    if log.prompt:
        return log.prompt[:100]
    return "Untitled"


AVAILABLE_LORAS = [
    'sking_v37_flux_4b_000018000',
    'sking_v38_flux_4b_000018000',
    'sking_v38_flux_4b_000021000',
    'sking_v39_flux_4b_000011000',
    'sking_v39_flux_4b_000028000',
    'sking_v40_flux_4b_000009000',
    'sking_v40_flux_4b_000011000',
    'sking_v40_flux_4b_000013000',
    'sking_v55_flux_4b_000015000',
    'sking_v50_flux_4b_000020000',
    'sking_v51_flux_4b_000020000',
    'sking_v52_flux_4b_000020000',
    'sking_v53_flux_4b_000020000',
    'sking_v54_flux_4b_000020000',
    'sking_v55_flux_4b_000020000',
    'sking_v56_flux_4b_000020000',
    'sking_v57_flux_4b_000020000',
    'sking_v58_flux_4b_000018000',
    'sking_v58_flux_4b_000020000',
    'sking_v59_flux_4b_000017000',
    'sking_v59_flux_4b_000020000',
    'sking_v62_flux_4b_000020000',
    'sking_v63_flux_4b_000020000',
    'sking_v70_flux_4b_000022000',
    'sking_v71_flux_4b_000022000',
    'sking_v72_flux_4b_000027000',
    'sking_v73_flux_4b_000027000',
]


@router.get("/models")
async def get_models(current_user: models.User = Depends(auth.get_current_user)):
    """
    Get the list of available models, grouped by generation mode
    """
    loras = [f.replace(".safetensors", "") for f in AVAILABLE_LORAS]
    loras.reverse()
    
    return {
        "aigc_image_to_skin": loras,
        "aigc_text_to_skin": [
            "z_image + " + lora for lora in loras
        ],
        "aigc_image_edit_to_skin": [
            "flux_4b + " + lora for lora in loras
        ]
    }


@router.get("/generate/active")
async def get_active_generation(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Get the user's current pending or processing generation tasks
    """
    log = db.query(models.GenerationLog).filter(
        models.GenerationLog.user_id == current_user.id,
        models.GenerationLog.status.in_(ACTIVE_GENERATION_STATUSES)
    ).order_by(models.GenerationLog.created_at.desc()).first()
    
    if not log:
        return {"has_active_task": False}
        
    queue_pos = get_queue_position(db, log.id)
    
    return {
        "has_active_task": True,
        "task": {
            "id": log.id,
            "status": log.status,
            "queue_position": queue_pos,
            "prompt": log.prompt,
            "mode": log.mode,
            "timestamp": log.created_at.replace(tzinfo=None).isoformat() + "Z"
        }
    }

@router.post("/generate")
async def generate_image(
    background_tasks: BackgroundTasks,
    prompt: Optional[str] = Form(None, max_length=500),
    is_public: bool = Form(True),
    file: UploadFile = File(None),
    version: Optional[str] = Form(None, alias="model_version", max_length=50),
    mode: Optional[str] = Form(None, max_length=50),
    parent: Optional[str] = Form(None),
    seed: Optional[int] = Form(None),
    n_step: Optional[int] = Form(None),
    guidance: Optional[float] = Form(None),
    edit_source_type: Optional[str] = Form(None, max_length=50),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    ALLOWED_MODES = {"aigc_image_to_skin", "aigc_text_to_skin", "aigc_image_edit_to_skin"}
    log_id = models.generate_base58_id()
    if not mode:
        mode = "aigc_image_to_skin" if file else "aigc_text_to_skin"
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid mode. Allowed: {', '.join(ALLOWED_MODES)}")

    if mode == "aigc_text_to_skin" and not backend_utils.is_text_to_skin_enabled():
        raise HTTPException(status_code=403, detail="Text to skin generation is temporarily under maintenance.")
    if mode == "aigc_image_to_skin" and not backend_utils.is_image_to_skin_enabled():
        raise HTTPException(status_code=403, detail="Image to skin generation is temporarily under maintenance.")
    if mode == "aigc_image_edit_to_skin" and not backend_utils.is_image_edit_to_skin_enabled():
        raise HTTPException(status_code=403, detail="Image edit to skin generation is temporarily under maintenance.")

    # Model Version Validation
    loras = [f.replace(".safetensors", "") for f in AVAILABLE_LORAS]
    loras.reverse()

    if mode == "aigc_image_to_skin":
        allowed_versions = loras
    elif mode == "aigc_text_to_skin":
        allowed_versions = [f"z_image + {lora}" for lora in loras]
    elif mode == "aigc_image_edit_to_skin":
        allowed_versions = [f"flux_4b + {lora}" for lora in loras]
    else:
        allowed_versions = []

    if not version:
        version = allowed_versions[0] if allowed_versions else None
    elif version not in allowed_versions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model version '{version}' for mode '{mode}'."
        )
        
    parent_log = None
    if parent:
        parent_log = db.query(models.GenerationLog).filter(models.GenerationLog.id == parent).first()
        if parent_log:
            is_public = parent_log.is_public

    # Pro Quota Check
    if not is_public:
        if not current_user.is_pro_active:
             raise HTTPException(status_code=403, detail="Free users have no private quota, please subscribe to Pro")
        total_private_files = db.query(models.GenerationLog).filter(
            models.GenerationLog.user_id == current_user.id,
            models.GenerationLog.is_public == False,
            models.GenerationLog.is_deleted == False
        ).count()
        private_limit = 5000 if current_user.pro_level == "pro-max" else 1000
        if total_private_files >= private_limit:
             raise HTTPException(status_code=400, detail=f"Total private assets limit reached ({private_limit} items)")

    # Queue Limit Check
    user_queue_count = db.query(models.GenerationLog).filter(
        models.GenerationLog.user_id == current_user.id,
        models.GenerationLog.status.in_(ACTIVE_GENERATION_STATUSES)
    ).count()

    if not current_user.is_admin:
        if current_user.is_pro_active:
            queue_max_len = 2
        else:
            queue_max_len = 1
            
        if user_queue_count >= queue_max_len:
            raise HTTPException(status_code=429, detail=f"You already have {queue_max_len} task(s) in the queue. Please wait for them to finish.")

    global_queue_count = db.query(models.GenerationLog).filter(
        models.GenerationLog.status.in_(ACTIVE_GENERATION_STATUSES)
    ).count()
    if global_queue_count > 10000:
        raise HTTPException(status_code=429, detail="Server is busy. The queue is full, please try again later.")

    # Quota Check
    if not current_user.is_admin:
        remaining = current_user.credits if current_user.credits is not None else 0
        if remaining < 3:
            raise HTTPException(status_code=403, detail="Insufficient credits")
        
        # Deduct credit
        current_user.credits = max(0, (current_user.credits or 0) - 3)
        # Record credit log
        log_entry = models.CreditLog(
            user_id=current_user.id,
            amount=-3,
            action="generation",
            source=f"Skin Generation: {log_id}"
        )
        db.add(log_entry)
    # Limit removed for default collections

    # Validation and Defaults

    if guidance is None:
        guidance = 4.0
    if not (0.1 <= guidance <= 15.0):
        raise HTTPException(status_code=400, detail="Guidance must be between 0.1 and 15.0")
    if n_step is not None and not (20 <= n_step <= 120):
        raise HTTPException(status_code=400, detail="n_step must be between 20 and 120")

    file_content = None
    content_type = None
    if file:
        file_content = await file.read()
        if len(file_content) > 512 * 1024:
            raise HTTPException(status_code=400, detail="File too large (Max 512KB)")
        content_type = file.content_type
        # if size !== 768 768, raise
        img = Image.open(io.BytesIO(file_content))
        if img.width != 768 or img.height != 768:
            raise HTTPException(status_code=400, detail="File size must be 768x768")

    source_filename = None
    
    if file_content:
        source_filename = f"uploads/{log_id}.png"
        try:
            upload_to_s3(file_content, source_filename, is_public, content_type or "image/png")
        except Exception as e:
            import traceback
            err_detail = traceback.format_exc()
            print(f"S3 upload error for {log_id}: {err_detail}")
            raise HTTPException(status_code=500, detail="Image upload failed, please try again later")

    log = models.GenerationLog(
        id=log_id,
        prompt=prompt,
        name= f"{display_log_name(parent_log)[:20]}...{(prompt or '')[:20]} " if mode == "aigc_image_edit_to_skin" and parent_log else (prompt[:100] if prompt else "Untitled"),
        mode=mode,
        user_id=current_user.id,
        is_public=is_public,
        model_version=version,
        parent=parent,
        seed=seed,
        n_step=n_step,
        guidance=guidance,
        status="pending",
        is_pro=current_user.is_pro_active,
        edit_source_type=edit_source_type,
        source=source_filename
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    try:
        enqueue_generation_task(log, current_user.is_pro_active, content_type or "image/png")
    except Exception as e:
        log.status = "failed"
        log.error_msg = f"Failed to enqueue: {e}"
        db.commit()
        raise HTTPException(status_code=500, detail="Failed to push to queue")

    return {
        "id": log.id,
        "status": "pending",
        "message": "Task queued"
    }

def decode_result_message(raw_message):
    if isinstance(raw_message, bytes):
        return raw_message.decode("utf-8")
    return raw_message


def recover_inflight_result_messages():
    """Move unacked result messages back to the main queue after a listener restart."""
    recovered = 0
    while True:
        raw_message = redis_conn.rpop(RESULT_PROCESSING_QUEUE_KEY)
        if not raw_message:
            break
        redis_conn.rpush(RESULT_QUEUE_KEY, raw_message)
        recovered += 1
    if recovered:
        print(f"[*] Recovered {recovered} unacked generation result message(s).")


def ack_result_message(raw_message):
    redis_conn.lrem(RESULT_PROCESSING_QUEUE_KEY, 1, raw_message)


def requeue_result_message(raw_message):
    with redis_conn.pipeline() as pipe:
        pipe.lrem(RESULT_PROCESSING_QUEUE_KEY, 1, raw_message)
        pipe.rpush(RESULT_QUEUE_KEY, raw_message)
        pipe.execute()


def should_apply_generation_status(log: models.GenerationLog, data: dict) -> bool:
    incoming_status = data.get("status")
    current_status = log.status
    incoming_stage = data.get("stage")

    if not incoming_status:
        return False
    if current_status == "deleted":
        return False
    if current_status == "success":
        return incoming_status == "success" and not log.result

    if incoming_status == "success":
        return True

    if incoming_status == "failed":
        if incoming_stage in {"text_to_image", "image_edit"}:
            return current_status in {"pending", "processing", "failed"}
        if incoming_stage == "image_to_skin":
            return current_status in {"pending", "processing", "pending_skin", "processing_skin", "failed"}
        return current_status not in {"success", "deleted"}

    if current_status == "failed":
        return True

    status_rank = {
        "pending": 0,
        "processing": 1,
        "pending_skin": 2,
        "processing_skin": 3,
    }
    return status_rank.get(incoming_status, -1) >= status_rank.get(current_status, -1)


def apply_generation_result_update(log: models.GenerationLog, data: dict) -> bool:
    if not should_apply_generation_status(log, data):
        return False

    status = data.get("status")
    log.status = status
    if "result" in data:
        log.result = data["result"]
    if "edited_result" in data:
        log.edited_result = data["edited_result"]
    if "error_msg" in data:
        log.error_msg = data["error_msg"]
    elif status != "failed":
        log.error_msg = None
    return True


async def start_result_listener():
    """Listen to Redis results in the background and write to the database."""
    await asyncio.to_thread(recover_inflight_result_messages)

    while True:
        raw_message = None
        try:
            raw_message = await asyncio.to_thread(
                redis_conn.brpoplpush,
                RESULT_QUEUE_KEY,
                RESULT_PROCESSING_QUEUE_KEY,
                timeout=10,
            )
            if not raw_message:
                continue

            data_str = decode_result_message(raw_message)
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError as json_error:
                print(f"Invalid generation result payload discarded: {json_error}")
                await asyncio.to_thread(ack_result_message, raw_message)
                continue

            log_id = data.get("log_id")
            status = data.get("status")

            db = SessionLocal()
            try:
                log = db.query(models.GenerationLog).filter(models.GenerationLog.id == log_id).first()
                if log:
                    updated = apply_generation_result_update(log, data)
                    db.commit()
                    if updated:
                        print(f"[*] Task {log_id} status updated to {status}")
                    else:
                        print(f"[*] Task {log_id} stale status {status} ignored")
                await asyncio.to_thread(ack_result_message, raw_message)
            except Exception as dbe:
                db.rollback()
                print(f"Result write error: {dbe}")
                await asyncio.to_thread(requeue_result_message, raw_message)
                await asyncio.sleep(1)
            finally:
                db.close()
        except Exception as e:
            print(f"Result Listener loop error: {e}")
            if raw_message is not None:
                try:
                    await asyncio.to_thread(requeue_result_message, raw_message)
                except Exception as requeue_error:
                    print(f"Result requeue error: {requeue_error}")
            await asyncio.sleep(1)

@router.post("/like/{log_id}")
async def toggle_like(
    log_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    log = db.query(models.GenerationLog).filter(
        models.GenerationLog.id == log_id,
        models.GenerationLog.is_deleted == False
    ).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    if not log.is_public and log.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Permission denied")
    
    like = db.query(models.UserLike).filter(
        models.UserLike.user_id == current_user.id,
        models.UserLike.log_id == log_id
    ).first()
    
    if like:
        db.delete(like)
        log.likes_count = max(0, (log.likes_count or 0) - 1)
        action = "unliked"
    else:
        new_like = models.UserLike(user_id=current_user.id, log_id=log_id)
        db.add(new_like)
        log.likes_count = (log.likes_count or 0) + 1
        action = "liked"
    
    db.commit()
    db.refresh(log)
    
    return {"status": "success", "action": action, "likes_count": log.likes_count}

@router.get("/history")
@limiter.exempt
async def get_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    skip = (page - 1) * page_size
    query = db.query(models.GenerationLog).filter(
        models.GenerationLog.user_id == current_user.id,
        models.GenerationLog.is_deleted == False,
        models.GenerationLog.mode.notin_(["human_edit", "human_upload"])
    )
    total = query.count()
    logs = query.order_by(models.GenerationLog.created_at.desc()).offset(skip).limit(page_size).all()
    
    results = []
    for log in logs:
        result_url = log.result_url
        source_url = log.source_url
        edited_image_url = log.edited_image_url
        
        queue_pos = get_queue_position(db, log.id) if log.status in ACTIVE_GENERATION_STATUSES else 0
        
        results.append({
            "id": log.id,
            "name": display_log_name(log),
            "prompt": log.prompt,
            "mode": log.mode,
            "edit_source_type": log.edit_source_type,
            "source": source_url,
            "result": result_url,
            "edited_image_url": edited_image_url,
            "is_public": log.is_public,
            "status": log.status or "success",
            "error_msg": log.error_msg,
            "queue_position": queue_pos,
            "creator": {
                "id": log.user_id,
                "username": current_user.username,
                "avatar_url": current_user.picture,
                "minecraft_skin_url": current_user.minecraft_skin_url
            },
            "timestamp": log.created_at.replace(tzinfo=None).isoformat() + "Z",
            "likes_count": log.likes_count or 0,
            "is_liked": db.query(models.UserLike).filter(models.UserLike.user_id == current_user.id, models.UserLike.log_id == log.id).first() is not None,
            "model_version": log.model_version,
            "parent": log.parent,
            "seed": log.seed,
            "n_step": log.n_step,
            "guidance": log.guidance,
            "is_pro": log.is_pro
        })

        
    return backend_utils.paginate_response(results, total, page, page_size)

# Discovery page cache is written to Redis by the singleton background service
# and mirrored locally in each API process for cheap reads.
DISCOVERY_CACHE_KEY = os.getenv("DISCOVERY_CACHE_KEY", "ed:discovery:cache:v1")
DISCOVERY_CACHE_TTL_SECONDS = int(os.getenv("DISCOVERY_CACHE_TTL_SECONDS", "900"))
DISCOVERY_LOCAL_CACHE_MAX_AGE_SECONDS = int(os.getenv("DISCOVERY_LOCAL_CACHE_MAX_AGE_SECONDS", "60"))
discovery_cache_items = []
discovery_cache_last_updated = 0.0

def set_local_discovery_cache(items):
    global discovery_cache_items, discovery_cache_last_updated
    discovery_cache_items = items
    discovery_cache_last_updated = time.time()

def write_discovery_cache_to_redis(items):
    try:
        redis_conn.set(
            DISCOVERY_CACHE_KEY,
            json.dumps(items),
            ex=DISCOVERY_CACHE_TTL_SECONDS,
        )
    except Exception as e:
        print("Discovery cache Redis write error:", e)

def read_discovery_cache_from_redis():
    try:
        cached = redis_conn.get(DISCOVERY_CACHE_KEY)
        if not cached:
            return []

        items = json.loads(cached)
        if not isinstance(items, list):
            return []

        set_local_discovery_cache(items)
        return items
    except Exception as e:
        print("Discovery cache Redis read error:", e)
        return []

def get_discovery_cache_items():
    local_cache_fresh = (
        discovery_cache_items
        and time.time() - discovery_cache_last_updated < DISCOVERY_LOCAL_CACHE_MAX_AGE_SECONDS
    )
    if local_cache_fresh:
        return discovery_cache_items

    items = read_discovery_cache_from_redis()
    if items:
        return items

    update_discovery_cache()
    return discovery_cache_items

def update_discovery_cache():
    """Active refresh of discovery page cache"""
    db = SessionLocal()
    try:
        # 1. Query IDs of all eligible records first
        eligible_ids_query = db.query(models.GenerationLog.id).filter(
            models.GenerationLog.is_public == True,
            models.GenerationLog.is_deleted == False,
            models.GenerationLog.status == "success"
        )
        all_ids = [row[0] for row in eligible_ids_query.all()]
        
        logs_with_users = []
        if all_ids:
            # 2. Randomly pick up to 180 IDs in Python
            sample_size = min(180, len(all_ids))
            sampled_ids = random.sample(all_ids, sample_size)

            # 3. Batch query details based on selected IDs
            query = db.query(models.GenerationLog, models.User.username, models.User.id, models.User.picture, models.User.minecraft_skin_url).join(
                models.User, models.GenerationLog.user_id == models.User.id, isouter=True
            ).filter(
                models.GenerationLog.id.in_(sampled_ids)
            )
            logs_with_users = query.all()
        
        results = []
        for log, username, user_id, picture, minecraft_skin_url in logs_with_users:
            result_url = get_cdn_url(log.result, bucket=settings.AWS_BUCKET_NAME)
                
            results.append({
                "id": log.id,
                "prompt": log.prompt or "",
                "name": display_log_name(log),
                "result": result_url,
                "is_public": log.is_public,
                "likes_count": log.likes_count or 0,
                "creator": {
                    "id": user_id,
                    "username": username or "Unknown",
                    "avatar_url": picture,
                    "minecraft_skin_url": minecraft_skin_url
                }
            })
            
        if results and len(results) < 180:
            needed = 180 - len(results)
            more = [random.choice(results) for _ in range(needed)]
            results.extend(more)

        random.shuffle(results)

        set_local_discovery_cache(results)
        write_discovery_cache_to_redis(results)
        return results
    finally:
        db.close()

async def start_discovery_cache_job():
    """Periodic task to trigger discovery cache update"""
    while True:
        try:
            await asyncio.to_thread(update_discovery_cache)
        except Exception as e:
            print("Discovery cache job error:", e)
        await asyncio.sleep(300)

@router.get("/discovery")
async def get_discovery_logs(
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    return get_discovery_cache_items()

from fastapi import Request

@router.get("/discovery/search")
@limiter.limit("1/second")
async def search_discovery_logs(
    request: Request,
    q: Optional[str] = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=30),
    sort_by: str = Query("created_at"),
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    import re
    query = db.query(models.GenerationLog).filter(
        models.GenerationLog.is_public == True,
        models.GenerationLog.is_deleted == False,
        models.GenerationLog.status == "success"
    )

    if q:
        q_stripped = q.strip()
        if q_stripped:
            has_chinese = bool(re.search(r"[\u4e00-\u9fa5]", q_stripped))
            min_len = 1 if has_chinese else 3
            if len(q_stripped) < min_len:
                raise HTTPException(
                    status_code=400,
                    detail=f"Search query must be at least {min_len} character(s)"
                )
            safe_q = q_stripped.replace('%', '\\%').replace('_', '\\_')
            query = query.filter(models.GenerationLog.name.ilike(f"%{safe_q}%"))

    # Sorting
    if sort_by == "likes":
        query = query.order_by(models.GenerationLog.likes_count.desc(), models.GenerationLog.created_at.desc())
    else:
        query = query.order_by(models.GenerationLog.created_at.desc())

    skip = (page - 1) * page_size
    total = query.count()
    logs = query.offset(skip).limit(page_size).all()
    
    results = []
    for log in logs:
        result_url = log.result_url
        
        user = db.query(models.User).filter(models.User.id == log.user_id).first()
        username = user.username if user else "Unknown"
        avatar_url = user.picture if user else None
        minecraft_skin_url = user.minecraft_skin_url if user else None
        
        is_liked = False
        if current_user:
            is_liked = db.query(models.UserLike).filter(
                models.UserLike.user_id == current_user.id,
                models.UserLike.log_id == log.id
            ).first() is not None
        
        results.append({
            "id": log.id,
            "prompt": log.prompt,
            "name": display_log_name(log),
            "result": result_url,
            "is_public": log.is_public,
            "likes_count": log.likes_count or 0,
            "is_liked": is_liked,
            "creator": {
                "id": log.user_id,
                "username": username,
                "avatar_url": avatar_url,
                "minecraft_skin_url": minecraft_skin_url
            },
            "timestamp": log.created_at.replace(tzinfo=None).isoformat() + "Z"
        })
        
    return backend_utils.paginate_response(results, total, page, page_size)



@router.get("/logs/{id}")
async def get_log(
    id: str,
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    """Get details for a single generation log"""
    log = db.query(models.GenerationLog).filter(
        models.GenerationLog.id == id,
        models.GenerationLog.is_deleted == False
    ).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
        
    if not log.is_public:
        if not current_user or log.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Permission denied")
            
    # Get creator info
    user = db.query(models.User).filter(models.User.id == log.user_id).first()
    username = user.username if user else "Unknown"
    
    result_url = log.result_url
    source_url = log.source_url
    edited_image_url = log.edited_image_url
            
    queue_pos = get_queue_position(db, log.id) if log.status in ACTIVE_GENERATION_STATUSES else 0
    has_feedback = False
    if current_user:
        has_feedback = db.query(models.UserFeedback.id).filter(
            models.UserFeedback.user_id == current_user.id,
            models.UserFeedback.log_id == log.id,
        ).first() is not None

    return {
        "id": log.id,
        "name": display_log_name(log),
        "prompt": log.prompt,
        "mode": log.mode,
        "edit_source_type": log.edit_source_type,
        "result": result_url,
        "source": source_url,
        "edited_image_url": edited_image_url,
        "is_public": log.is_public,
        "status": log.status or "success",
        "error_msg": log.error_msg,
        "creator": {
            "id": log.user_id,
            "username": username,
            "avatar_url": user.picture if user else None,
            "minecraft_skin_url": user.minecraft_skin_url if user else None
        },
        "timestamp": log.created_at.replace(tzinfo=None).isoformat() + "Z",
        "likes_count": log.likes_count or 0,
        "is_liked": db.query(models.UserLike).filter(models.UserLike.user_id == current_user.id, models.UserLike.log_id == log.id).first() is not None if current_user else False,
        "model_version": log.model_version,
        "parent": log.parent,
        "seed": log.seed,
        "n_step": log.n_step,
        "guidance": log.guidance,
        "queue_position": queue_pos,
        "is_pro": log.is_pro,
        "has_feedback": has_feedback
    }

@router.get("/logs/{id}/derived")
async def get_derived_logs(
    id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Get all public skins derived from this skin (includes private if you are the owner)"""
    logs = db.query(models.GenerationLog).filter(
        models.GenerationLog.parent == id,
        models.GenerationLog.is_deleted == False,
        models.GenerationLog.status == "success"
    ).all()
    results = []
    for log in logs:
        if not log.is_public:
            if not current_user or log.user_id != current_user.id:
                continue
                
        result_url = log.result_url
            
        results.append({
            "id": log.id,
            "log_id": log.id,
            "name": display_log_name(log),
            "type": "image",
            "data": {
                "id": log.id,
                "result": result_url
            }
        })
    return {"items": results}

@router.delete("/logs/{id}")
async def delete_log(
    id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Delete user generation records and associated data (soft delete + S3 cleaning)"""
    log = db.query(models.GenerationLog).filter(models.GenerationLog.id == id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
        
    if log.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Permission denied")

    if not current_user.is_pro_active:
        import datetime
        now = datetime.datetime.now()
        day_key = f"delete_quota:{current_user.id}:{now.date()}"
        
        count = redis_conn.get(day_key)
        if count and int(count) >= 1:
            raise HTTPException(status_code=403, detail="Free users can only delete 1 skin per day. Please subscribe to Pro for unlimited deletions.")
            
        redis_conn.incr(day_key)
        if not count:
            redis_conn.expire(day_key, 2 * 24 * 3600)
        
    # 1. Collect S3 files that need cleaning
    files_to_delete = []
    if log.source:
        files_to_delete.append((log.source, log.is_public))
    if log.result:
        files_to_delete.append((log.result, log.is_public))
    if log.edited_result:
        files_to_delete.append((log.edited_result, log.is_public))

    # 2. Trigger background cleaning task
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
    db.query(models.CollectionItem).filter(models.CollectionItem.log_id == id).delete()
    
    # 5. Delete associated likes
    db.query(models.UserLike).filter(models.UserLike.log_id == id).delete()
    
    # 6. Delete associated feedback
    db.query(models.UserFeedback).filter(models.UserFeedback.log_id == id).delete()
    
    db.commit()
    
    return {"message": "Creation soft-deleted, properties cleared, and files queued for S3 deletion"}

@router.patch("/logs/{id}/name")
async def update_log_name(
    id: str,
    request: schemas.LogNameUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    log = db.query(models.GenerationLog).filter(models.GenerationLog.id == id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    if log.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Permission denied")
    
    log.name = request.name
    db.commit()
    return {"message": "Name updated successfully"}

@router.post("/logs/{id}/make_private")
async def make_log_private(
    id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    log = db.query(models.GenerationLog).filter(models.GenerationLog.id == id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
        
    if log.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Permission denied")

    if not current_user.is_pro_active:
        raise HTTPException(status_code=403, detail="Pro subscription required to make skins private")

    if not log.is_public:
        return {"message": "Already private"}

    from s3_utils import s3_client
    from config import settings
    public_bucket = settings.AWS_BUCKET_NAME
    private_bucket = settings.AWS_PRIVATE_BUCKET_NAME

    def move_file(key):
        if not key or key.startswith("http"):
            return
        try:
            s3_client.copy_object(
                Bucket=private_bucket,
                CopySource={'Bucket': public_bucket, 'Key': key},
                Key=key
            )
            s3_client.delete_object(Bucket=public_bucket, Key=key)
        except Exception as e:
            print(f"Failed to move S3 object {key} to private bucket: {e}")

    move_file(log.source)
    move_file(log.result)
    move_file(log.edited_result)

    # Remove from any public collections since private skins cannot be in public collections
    public_col_items = db.query(models.CollectionItem).join(
        models.Collection, models.CollectionItem.collection_id == models.Collection.id
    ).filter(
        models.CollectionItem.log_id == id,
        models.Collection.is_public == True
    ).all()

    for item in public_col_items:
        db.delete(item)

    # Clear parent reference from any skins derived from this one
    # so that the relationship is completely severed and not confusing
    db.query(models.GenerationLog).filter(
        models.GenerationLog.parent == id
    ).update({"parent": None}, synchronize_session=False)

    # Also clear the parent of this skin itself
    log.parent = None

    log.is_public = False
    db.commit()

    return {"message": "Skin made private successfully"}

@router.post("/logs/{id}/feedback")
async def create_log_feedback(
    id: str,
    request: schemas.FeedbackCreate,
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    log = db.query(models.GenerationLog).filter(
        models.GenerationLog.id == id,
        models.GenerationLog.is_deleted == False
    ).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")

    if current_user:
        existing_feedback = db.query(models.UserFeedback).filter(
            models.UserFeedback.user_id == current_user.id,
            models.UserFeedback.log_id == id,
        ).first()
        if existing_feedback:
            return {
                "status": "success",
                "message": "Feedback already submitted",
                "already_submitted": True
            }

    feedback = models.UserFeedback(
        user_id=current_user.id if current_user else None,
        log_id=id,
        is_good=request.is_good
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    
    print(f"[*] Quality Feedback received for log {id}: is_good={request.is_good}")
    return {"status": "success", "message": "Feedback submitted successfully"}


def collect_active_generation_log_ids(queues, registry_classes, job_class):
    active_log_ids = set()
    for q in queues:
        for job in q.jobs:
            try:
                if job and job.args:
                    active_log_ids.add(job.args[0])
            except Exception:
                pass

        for RegistryCls in registry_classes:
            registry = RegistryCls(name=q.name, connection=redis_conn)
            for job_id in registry.get_job_ids():
                try:
                    job = job_class.fetch(job_id, connection=redis_conn)
                    if job and job.args:
                        active_log_ids.add(job.args[0])
                except Exception:
                    pass
    return active_log_ids


def enqueue_recovered_generation_task(log: models.GenerationLog, is_pro_active: bool):
    if log.status in SECOND_STAGE_STATUSES:
        if log.mode in TWO_STAGE_GENERATION_MODES and log.edited_result:
            return enqueue_image_to_skin_task(log, is_pro_active, "image/jpeg")
        if log.mode == "aigc_image_to_skin" and log.source:
            return enqueue_image_to_skin_task(log, is_pro_active, "image/png")

        # The DB says stage 2, but we do not have the stage-2 input. Fall back
        # to the persisted source and rerun stage 1 so the pipeline can rebuild it.
        log.status = "pending"

    return enqueue_generation_task(log, is_pro_active, "image/png")


def re_enqueue_if_missing():
    import datetime
    from rq.job import Job
    from rq.registry import StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry
    
    db = SessionLocal()
    try:
        stale_before = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=GENERATION_RECOVERY_MIN_AGE_SECONDS)
        )
        stale_logs = db.query(models.GenerationLog).filter(
            models.GenerationLog.status.in_(RECOVERABLE_GENERATION_STATUSES),
            models.GenerationLog.created_at < stale_before
        ).all()
        
        if not stale_logs:
            return
            
        print(f"[*] Found {len(stale_logs)} stale active task(s). Verifying in Redis...")
        
        queues = [
            Queue("queue_text_to_image", connection=redis_conn),
            Queue("high_queue_text_to_image", connection=redis_conn),
            Queue("queue_image_edit", connection=redis_conn),
            Queue("high_queue_image_edit", connection=redis_conn),
            Queue("queue_image_to_skin", connection=redis_conn),
            Queue("high_queue_image_to_skin", connection=redis_conn),
        ]
        
        active_log_ids = collect_active_generation_log_ids(
            queues,
            [StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry],
            Job,
        )
                        
        re_enqueued_count = 0
        for log in stale_logs:
            if log.id not in active_log_ids:
                print(f"[*] Task {log.id} missing from Redis. Re-enqueueing...")
                user = db.query(models.User).filter(models.User.id == log.user_id).first()
                
                try:
                    enqueue_recovered_generation_task(log, bool(user and user.is_pro_active))
                    db.commit()
                    re_enqueued_count += 1
                except Exception as e:
                    db.rollback()
                    print(f"[*] Failed to re-enqueue {log.id}: {e}")
                    
        if re_enqueued_count > 0:
            print(f"[*] Successfully recovered {re_enqueued_count} tasks.")
            
    except Exception as e:
        print("Re-enqueue logic error:", e)
    finally:
        db.close()

async def start_pending_recovery_job():
    import asyncio
    while True:
        try:
            await asyncio.to_thread(re_enqueue_if_missing)
        except Exception as e:
            print("Recovery job loop error:", e)
        await asyncio.sleep(60 * 2) # Check every two minutes
