from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, BackgroundTasks, Query, Response
import io
import json
import os
from PIL import Image
from typing import Literal, Optional
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session
import models
import schemas
import auth
from database import get_db
from s3_utils import get_cdn_url, get_s3_url, delete_from_s3, upload_to_s3, generate_presigned_url_get
import backend_utils
from config import settings
from pipeline_registry import (
    MODEL_PIPELINES,
    get_pipeline,
    is_sking_ddj_model,
)
from redis import Redis
from rq import Queue, Retry
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Callback, Job
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
TWO_STAGE_GENERATION_MODES = {
    "aigc_text_to_skin",
    "aigc_image_edit_to_skin",
    "aigc_image_to_skin",
}
LEGACY_SKIN_MODEL_VERSION = "sking_v73_flux_4b_000027000"
RENDER_TO_UV_JOB_TIMEOUT_SECONDS = int(
    os.getenv("RENDER_TO_UV_JOB_TIMEOUT_SECONDS", "120")
)
RENDER_TO_UV_RETRY_MAX = int(os.getenv("RENDER_TO_UV_RETRY_MAX", "5"))
RENDER_TO_UV_RETRY_INTERVALS_SECONDS = [
    int(item.strip())
    for item in os.getenv(
        "RENDER_TO_UV_RETRY_INTERVALS_SECONDS",
        "5,15,30,60,120",
    ).split(",")
    if item.strip()
]
RESULT_QUEUE_KEY = os.getenv("GENERATE_RESULT_QUEUE_KEY", "generate_results")
RESULT_PROCESSING_QUEUE_KEY = os.getenv("GENERATE_RESULT_PROCESSING_QUEUE_KEY", "generate_results_processing")
GENERATION_RECOVERY_MIN_AGE_SECONDS = int(os.getenv("GENERATION_RECOVERY_MIN_AGE_SECONDS", "300"))
GENERATION_CANCELLATION_TTL_SECONDS = int(
    os.getenv("GENERATION_CANCELLATION_TTL_SECONDS", str(7 * 24 * 3600))
)
# image_to_skin performs its own infinite S3 failover/retry loop. RQ's
# timeout must therefore be disabled, otherwise it interrupts that loop and
# advances the worker to the next queued job while S3 is temporarily down.
IMAGE_TO_SKIN_JOB_TIMEOUT = -1

import random
import time
import asyncio
from database import SessionLocal

def make_generation_job_id(log_id: str, stage: str) -> str:
    return f"generation_{log_id}_{stage}"


def generation_cancellation_key(log_id: str) -> str:
    return f"generation:cancelled:{log_id}"


def cancel_generation_jobs(log_id: str) -> list[str]:
    """Stop queued retries and signal an in-flight GPU task to exit safely."""
    try:
        redis_conn.set(
            generation_cancellation_key(log_id),
            "1",
            ex=GENERATION_CANCELLATION_TTL_SECONDS,
        )
    except Exception as exc:
        print(f"[!] Failed to mark generation {log_id} as cancelled: {exc}")

    job_ids = {
        make_generation_job_id(log_id, stage)
        for stage in (
            "text_to_image",
            "image_edit",
            "image_to_skin",
            "real_to_render",
            "real_to_render_resume",
            "render_to_uv",
        )
    }

    # Provider polling uses numbered job IDs, so discover any outstanding
    # poll jobs belonging to this generation as well.
    try:
        redis_prefix = "rq:job:"
        for raw_key in redis_conn.scan_iter(
            match=f"{redis_prefix}real_to_render_poll_{log_id}_*"
        ):
            key = (
                raw_key.decode("utf-8")
                if isinstance(raw_key, bytes)
                else str(raw_key)
            )
            if key.startswith(redis_prefix):
                job_ids.add(key[len(redis_prefix):])
    except Exception as exc:
        print(f"[!] Failed to discover poll jobs for generation {log_id}: {exc}")

    cancelled = []
    for job_id in sorted(job_ids):
        try:
            job = Job.fetch(job_id, connection=redis_conn)
            job.cancel()
            cancelled.append(job_id)
        except NoSuchJobError:
            continue
        except InvalidJobOperation:
            # Already cancelled is the desired state.
            continue
        except Exception as exc:
            print(f"[!] Failed to cancel RQ job {job_id}: {exc}")

    if cancelled:
        print(f"[*] Cancelled generation jobs for {log_id}: {cancelled}")
    return cancelled


def get_generation_retry_policy() -> Retry:
    return Retry(max=99999, interval=[5, 10, 30, 60])


def get_generation_retry_policy_for_log(log: models.GenerationLog) -> Optional[Retry]:
    """Return the ordinary RQ retry policy for a generation."""
    if not log.recoverable:
        return None
    return get_generation_retry_policy()


def get_render_to_uv_retry_policy() -> Retry:
    return Retry(
        max=RENDER_TO_UV_RETRY_MAX,
        interval=RENDER_TO_UV_RETRY_INTERVALS_SECONDS,
    )


def uses_real_to_render_pipeline(log: models.GenerationLog) -> bool:
    return (
        log.mode == "aigc_image_to_skin"
        and is_sking_ddj_model(log.model_version)
    )


def sking_ddj_model_filter():
    return models.GenerationLog.model_version.in_(tuple(MODEL_PIPELINES))


def uses_image_to_skin_intermediate(log: models.GenerationLog) -> bool:
    if log.mode not in TWO_STAGE_GENERATION_MODES:
        return False
    return log.mode != "aigc_image_to_skin" or uses_real_to_render_pipeline(log)


def enqueue_image_to_skin_task(log: models.GenerationLog, is_pro_active: bool, content_type: str = "image/png"):
    prefix = "high_" if is_pro_active else ""
    q_skin = Queue(f'{prefix}queue_image_to_skin', connection=redis_conn)
    retry_policy = get_generation_retry_policy_for_log(log)

    source = log.source
    skin_content_type = content_type
    intermediate_filename = None

    if uses_image_to_skin_intermediate(log):
        source = log.edited_result
        skin_content_type = "image/jpeg"
        intermediate_filename = log.edited_result

    if not source:
        raise Exception(f"Cannot enqueue image_to_skin for {log.id}: missing source image")

    kwargs = {
        "model_version": log.model_version,
        "aux_model_version": log.aux_model_version,
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
        job_timeout=IMAGE_TO_SKIN_JOB_TIMEOUT,
        retry=retry_policy,
        result_ttl=10,
        job_id=make_generation_job_id(log.id, "image_to_skin")
    )


def enqueue_real_to_render_task(
    log: models.GenerationLog,
    is_pro_active: bool,
    content_type: str,
):
    if not uses_real_to_render_pipeline(log):
        raise ValueError(
            f"Generation {log.id} is not a SKING_DDJ "
            "image-to-skin task"
        )
    if not log.source:
        raise ValueError(
            f"Cannot enqueue real_to_render for {log.id}: missing source image"
        )

    pipeline = get_pipeline(log.model_version)

    prefix = "high_" if is_pro_active else ""
    queue = Queue(f"{prefix}queue_real_to_render", connection=redis_conn)
    return queue.enqueue(
        "tasks.submit_real_to_render",
        args=(
            log.id,
            log.is_public,
            log.source,
            content_type,
            prefix,
            log.model_version,
            pipeline.to_task_payload(),
        ),
        job_timeout="60s",
        retry=None,
        on_failure=Callback(
            "tasks.real_to_render_job_failure",
            timeout=20,
        ),
        on_stopped=Callback(
            "tasks.real_to_render_job_stopped",
            timeout=20,
        ),
        result_ttl=10,
        failure_ttl=86400,
        job_id=make_generation_job_id(log.id, "real_to_render"),
    )


def enqueue_real_to_render_resume_task(
    log: models.GenerationLog,
    is_pro_active: bool,
):
    """Resume an accepted provider task without issuing another POST."""
    if not log.provider_task_id:
        raise ValueError(
            f"Cannot resume real_to_render for {log.id}: missing provider task id"
        )
    pipeline = get_pipeline(log.model_version)
    prefix = "high_" if is_pro_active else ""
    queue = Queue(f"{prefix}queue_real_to_render", connection=redis_conn)
    return queue.enqueue(
        "tasks.resume_real_to_render",
        args=(
            log.id,
            log.is_public,
            log.source or "",
            "image/png",
            prefix,
            log.provider_task_id,
            log.model_version,
            pipeline.to_task_payload(),
        ),
        job_timeout="60s",
        retry=Retry(max=5, interval=[5, 15, 30, 60, 120]),
        result_ttl=10,
        failure_ttl=86400,
        job_id=make_generation_job_id(log.id, "real_to_render_resume"),
    )


def enqueue_render_to_uv_task(
    log: models.GenerationLog,
    is_pro_active: bool,
):
    """Resume Dense UV from the persisted stage-1 image without resubmitting."""
    if not uses_real_to_render_pipeline(log):
        raise ValueError(
            f"Generation {log.id} is not a SKING_DDJ "
            "image-to-skin task"
        )
    if not log.edited_result:
        raise ValueError(
            f"Cannot recover render_to_uv for {log.id}: "
            "missing edited_result"
        )

    pipeline = get_pipeline(log.model_version)

    prefix = "high_" if is_pro_active else ""
    queue = Queue(
        f"{prefix}queue_render_to_uv",
        connection=redis_conn,
    )
    return queue.enqueue(
        "worker_tasks.task_render_to_uv",
        args=(
            log.id,
            log.is_public,
            log.edited_result,
            "image/png",
            log.model_version,
            pipeline.dense_uv_checkpoint_file,
            pipeline.DMR_mappings_dir,
        ),
        job_timeout=RENDER_TO_UV_JOB_TIMEOUT_SECONDS,
        retry=get_render_to_uv_retry_policy(),
        result_ttl=60,
        failure_ttl=86400,
        job_id=make_generation_job_id(log.id, "render_to_uv"),
    )


def enqueue_generation_task(log: models.GenerationLog, is_pro_active: bool, content_type: str = "image/png"):
    prefix = "high_" if is_pro_active else ""

    if is_sking_ddj_model(log.model_version):
        return enqueue_real_to_render_task(
            log,
            is_pro_active,
            content_type,
        )
    
    q_t2i = Queue(f'{prefix}queue_text_to_image', connection=redis_conn)
    q_edit = Queue(f'{prefix}queue_image_edit', connection=redis_conn)
    
    retry_policy = get_generation_retry_policy_for_log(log)

    if log.mode == "aigc_text_to_skin":
        q_t2i.enqueue(
            "worker_tasks.task_text_to_image",
            args=(log.id, log.is_public, log.prompt, log.model_version, log.aux_model_version, log.seed, log.n_step, log.guidance),
            job_timeout='400s',
            retry=retry_policy,
            result_ttl=10,
            job_id=make_generation_job_id(log.id, "text_to_image")
        )
    elif log.mode == "aigc_image_edit_to_skin":
        q_edit.enqueue(
            "worker_tasks.task_image_edit",
            args=(log.id, log.is_public, log.source, content_type, log.prompt, log.model_version, log.aux_model_version, log.seed, log.n_step, log.guidance),
            job_timeout='400s',
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
                (models.GenerationLog.mode == "aigc_image_to_skin") & models.GenerationLog.status.in_(["pending", "pending_skin", "processing_skin"])
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

ALLOWED_MODES = {"aigc_image_to_skin", "aigc_text_to_skin", "aigc_image_edit_to_skin"}

AVAILABLE_IMAGE_TO_SKIN_MODELS = [
    *MODEL_PIPELINES,
    LEGACY_SKIN_MODEL_VERSION,
]

AVAILABlE_TEXT_TO_IMAGE_MODELS = [
    'z_image'
]

AVAILABLE_IMAGE_EDIT_MODELS = [
    'flux_4b'
]

@router.get("/models")
async def get_models(current_user: models.User = Depends(auth.get_current_user)):
    """
    Get the list of available models, grouped by generation mode
    """
    
    return {
        "text_to_image_models": AVAILABlE_TEXT_TO_IMAGE_MODELS,
        "image_edit_models": AVAILABLE_IMAGE_EDIT_MODELS,
        "image_to_skin_models": AVAILABLE_IMAGE_TO_SKIN_MODELS
    }


@router.get("/generation_credit_cost")
async def get_generation_credit_cost(
    model_version: Optional[str] = Query(None),
    aux_model_version: Optional[str] = Query(None),
    current_user: models.User = Depends(auth.get_current_user)
):
    is_pro_exclusive = False
    if model_version:
        cost = backend_utils.get_model_credit_cost(model_version)
        if backend_utils.is_model_pro_exclusive(model_version):
            is_pro_exclusive = True
        if aux_model_version:
            cost += backend_utils.get_model_credit_cost(aux_model_version)
            if backend_utils.is_model_pro_exclusive(aux_model_version):
                is_pro_exclusive = True
        return {"credits": cost, "is_pro": is_pro_exclusive}
    return {"credits": backend_utils.get_generation_credit_cost(), "is_pro": False}


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
    model_version: str = Form(..., alias="model_version", max_length=50),
    aux_model_version: Optional[str] = Form(None, alias="aux_model_version", max_length=50),
    mode: Optional[str] = Form(None, max_length=50),
    parent: Optional[str] = Form(None),
    seed: Optional[int] = Form(None),
    n_step: Optional[int] = Form(None),
    guidance: Optional[float] = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
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

    aux = aux_model_version if aux_model_version else None

    # Calculate allowed combinations set of (aux, skin) pairs
    if mode == "aigc_image_to_skin":
        allowed_combinations = {(None, skin) for skin in AVAILABLE_IMAGE_TO_SKIN_MODELS}
    elif mode == "aigc_text_to_skin":
        allowed_combinations = {
            (base, LEGACY_SKIN_MODEL_VERSION)
            for base in AVAILABlE_TEXT_TO_IMAGE_MODELS
        }
    elif mode == "aigc_image_edit_to_skin":
        allowed_combinations = {
            (base, LEGACY_SKIN_MODEL_VERSION)
            for base in AVAILABLE_IMAGE_EDIT_MODELS
        }
    else:
        allowed_combinations = set()

    if (aux, model_version) not in allowed_combinations:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model version combination: aux_model_version='{aux}', model_version='{model_version}' for mode '{mode}'."
        )

    version = f"{aux_model_version} + {model_version}" if aux_model_version else model_version
        
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

    generation_credit_cost = 0

    # Pro Exclusive Check
    is_pro_exclusive = backend_utils.is_model_pro_exclusive(model_version) or (
        aux_model_version and backend_utils.is_model_pro_exclusive(aux_model_version)
    )
    if is_pro_exclusive and not current_user.is_pro_active:
        raise HTTPException(
            status_code=403,
            detail="The selected model is exclusive to Pro users. Please upgrade your subscription to access this model."
        )

    # Quota Check
    generation_credit_cost = backend_utils.get_model_credit_cost(model_version)
    if aux_model_version:
        generation_credit_cost += backend_utils.get_model_credit_cost(aux_model_version)
    remaining = current_user.credits if current_user.credits is not None else 0
    if remaining < generation_credit_cost:
        raise HTTPException(status_code=403, detail="Insufficient credits")
    
    # Deduct credit
    current_user.credits = max(0, (current_user.credits or 0) - generation_credit_cost)
    # Record credit log
    log_entry = models.CreditLog(
        user_id=current_user.id,
        amount=-generation_credit_cost,
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
        model_version=model_version,
        aux_model_version=aux_model_version,
        credits_charged=generation_credit_cost,
        recoverable=(model_version == LEGACY_SKIN_MODEL_VERSION),
        provider_submission_state=(
            "not_started"
            if is_sking_ddj_model(model_version)
            else None
        ),
        parent=parent,
        seed=seed,
        n_step=n_step,
        guidance=guidance,
        status="pending",
        is_pro=current_user.is_pro_active,
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
        refund_generation_credits(db, log)
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
        if incoming_stage in {"text_to_image", "image_edit", "real_to_render"}:
            return current_status in {"pending", "processing", "failed"}
        if incoming_stage in {"image_to_skin", "render_to_uv"}:
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
    incoming_model_version = data.get("model_version")
    if (
        incoming_model_version is not None
        and incoming_model_version != log.model_version
    ):
        return False

    if not should_apply_generation_status(log, data):
        return False

    status = data.get("status")
    log.status = status
    if "result" in data:
        log.result = data["result"]
    if "edited_result" in data:
        log.edited_result = data["edited_result"]
    if "provider_task_id" in data:
        log.provider_task_id = data["provider_task_id"]
    if "provider_submission_state" in data:
        log.provider_submission_state = data[
            "provider_submission_state"
        ]
    if "error_msg" in data:
        log.error_msg = data["error_msg"]
    elif status != "failed":
        log.error_msg = None
    return True


def _charged_credits_from_history(db: Session, log: models.GenerationLog) -> int:
    """Compatibility fallback for rows created before credits_charged existed."""
    debit = (
        db.query(models.CreditLog)
        .filter(
            models.CreditLog.user_id == log.user_id,
            models.CreditLog.action == "generation",
            models.CreditLog.source == f"Skin Generation: {log.id}",
            models.CreditLog.amount < 0,
        )
        .first()
    )
    return abs(debit.amount) if debit else 0


def refund_generation_credits(db: Session, log: models.GenerationLog) -> int:
    """Refund one failed generation exactly once in the caller's transaction."""
    if log.credits_refunded:
        return 0

    charged = log.credits_charged or _charged_credits_from_history(db, log)
    if not log.user_id or charged <= 0:
        log.credits_refunded = True
        return 0

    user = (
        db.query(models.User)
        .filter(models.User.id == log.user_id)
        .with_for_update()
        .first()
    )
    if not user:
        raise RuntimeError(
            f"Cannot refund generation {log.id}: user {log.user_id} was not found"
        )

    refund_source = f"Skin Generation Refund: {log.id}"
    existing_refund = (
        db.query(models.CreditLog)
        .filter(
            models.CreditLog.user_id == log.user_id,
            models.CreditLog.action == "refund",
            models.CreditLog.source == refund_source,
        )
        .first()
    )
    if existing_refund:
        log.credits_refunded = True
        return 0

    user.credits = (user.credits or 0) + charged
    db.add(
        models.CreditLog(
            user_id=log.user_id,
            amount=charged,
            action="refund",
            source=refund_source,
        )
    )
    log.credits_refunded = True
    return charged


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
                log = (
                    db.query(models.GenerationLog)
                    .filter(models.GenerationLog.id == log_id)
                    .with_for_update()
                    .first()
                )
                if log:
                    updated = apply_generation_result_update(log, data)
                    refunded = (
                        refund_generation_credits(db, log)
                        if updated and status == "failed" and not log.recoverable
                        else 0
                    )
                    db.commit()
                    if updated:
                        print(f"[*] Task {log_id} status updated to {status}")
                        if refunded:
                            print(f"[*] Task {log_id} refunded {refunded} credits")
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
            "model_version": f"{log.aux_model_version} + {log.model_version}" if log.aux_model_version else log.model_version,
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
            models.GenerationLog.status == "success",
            sking_ddj_model_filter()
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
async def get_discovery_logs(response: Response):
    response.headers["Cache-Control"] = (
        "public, max-age=60, s-maxage=300, stale-while-revalidate=600"
    )
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
    model_series: Literal["", "SKING_DDJ", "sking"] = Query(""),
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    import re
    query = db.query(models.GenerationLog).filter(
        models.GenerationLog.is_public == True,
        models.GenerationLog.is_deleted == False,
        models.GenerationLog.status == "success"
    )

    if model_series:
        query = query.filter(
            func.substr(
                models.GenerationLog.model_version,
                1,
                len(model_series),
            ) == model_series
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
        "model_version": f"{log.aux_model_version} + {log.model_version}" if log.aux_model_version else log.model_version,
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

    # Signal an in-flight worker first and remove queued/scheduled retries
    # before deleting the task's source objects.
    cancel_generation_jobs(log.id)
        
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

    # Clear user character settings if they set this skin as their character
    if log.result or log.edited_result:
        from sqlalchemy import or_
        filters = []
        if log.result:
            filters.append(models.User.minecraft_skin_url.like(f"%{log.result}%"))
        if log.edited_result:
            filters.append(models.User.minecraft_skin_url.like(f"%{log.edited_result}%"))
        if filters:
            db.query(models.User).filter(or_(*filters)).update(
                {"minecraft_skin_url": None},
                synchronize_session=False
            )

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

    # Clear user character settings if they set this skin as their character
    if log.result or log.edited_result:
        from sqlalchemy import or_
        filters = []
        if log.result:
            filters.append(models.User.minecraft_skin_url.like(f"%{log.result}%"))
        if log.edited_result:
            filters.append(models.User.minecraft_skin_url.like(f"%{log.edited_result}%"))
        if filters:
            db.query(models.User).filter(or_(*filters)).update(
                {"minecraft_skin_url": None},
                synchronize_session=False
            )

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

    def add_job_log_id(job):
        try:
            if job and job.args:
                active_log_ids.add(job.args[0])
        except Exception:
            pass

    for q in queues:
        for job in q.jobs:
            add_job_log_id(job)

        # RQ moves a dequeued job through an intermediate list before it is
        # registered as started. Treat that list as active too, otherwise a
        # recovery pass can duplicate a job during this short transition.
        intermediate_queue = getattr(q, "intermediate_queue", None)
        if intermediate_queue:
            try:
                for job_id in intermediate_queue.get_job_ids():
                    add_job_log_id(
                        job_class.fetch(job_id, connection=redis_conn)
                    )
            except Exception:
                pass

        for RegistryCls in registry_classes:
            registry = RegistryCls(name=q.name, connection=redis_conn)
            try:
                registry_job_ids = registry.get_job_ids(cleanup=False)
            except TypeError:
                registry_job_ids = registry.get_job_ids()
            for job_id in registry_job_ids:
                try:
                    add_job_log_id(
                        job_class.fetch(job_id, connection=redis_conn)
                    )
                except Exception:
                    pass
    return active_log_ids


def requeue_existing_dense_uv_stage_one_job(log: models.GenerationLog) -> bool:
    """Put an orphaned queued job hash back on its original RQ queue.

    Returns False only when the job hash no longer exists, allowing the caller
    to recreate the deterministic job from the database record. Existing jobs
    in any non-queued state are deliberately not resubmitted because stage one
    calls a billable external provider.
    """
    job_id = make_generation_job_id(log.id, "real_to_render")
    try:
        job = Job.fetch(job_id, connection=redis_conn)
    except NoSuchJobError:
        return False

    status = job.get_status(refresh=True)
    status_value = getattr(status, "value", status)
    if status_value != "queued":
        raise RuntimeError(
            f"Refusing to resubmit {job_id}: existing job status is "
            f"{status_value!r}"
        )
    if job.func_name != "tasks.submit_real_to_render":
        raise RuntimeError(
            f"Refusing to resubmit {job_id}: unexpected function "
            f"{job.func_name!r}"
        )
    if job.origin not in {
        "queue_real_to_render",
        "high_queue_real_to_render",
    }:
        raise RuntimeError(
            f"Refusing to resubmit {job_id}: unexpected queue "
            f"{job.origin!r}"
        )

    Queue(job.origin, connection=redis_conn).enqueue_job(job)
    print(f"[*] Requeued orphaned RQ job {job_id} on {job.origin}.")
    return True


def enqueue_recovered_generation_task(log: models.GenerationLog, is_pro_active: bool):
    if log.status in SECOND_STAGE_STATUSES:
        if uses_real_to_render_pipeline(log):
            return enqueue_render_to_uv_task(log, is_pro_active)
        if uses_image_to_skin_intermediate(log) and log.edited_result:
            return enqueue_image_to_skin_task(log, is_pro_active, "image/jpeg")
        if log.mode == "aigc_image_to_skin" and log.source:
            return enqueue_image_to_skin_task(log, is_pro_active, "image/png")

        # The DB says stage 2, but we do not have the stage-2 input. Fall back
        # to the persisted source and rerun stage 1 so the pipeline can rebuild it.
        log.status = "pending"

    if uses_real_to_render_pipeline(log) and log.provider_task_id:
        return enqueue_real_to_render_resume_task(log, is_pro_active)

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
        sking_ddj_second_stage = and_(
            sking_ddj_model_filter(),
            models.GenerationLog.mode == "aigc_image_to_skin",
            models.GenerationLog.status.in_(SECOND_STAGE_STATUSES),
        )
        sking_ddj_pending_stage_one = and_(
            sking_ddj_model_filter(),
            models.GenerationLog.mode == "aigc_image_to_skin",
            models.GenerationLog.status.in_({"pending", "processing"}),
            models.GenerationLog.provider_task_id.is_(None),
            or_(
                models.GenerationLog.provider_submission_state.is_(None),
                models.GenerationLog.provider_submission_state
                == "not_started",
            ),
            models.GenerationLog.edited_result.is_(None),
            models.GenerationLog.result.is_(None),
        )
        sking_ddj_accepted_stage_one = and_(
            sking_ddj_model_filter(),
            models.GenerationLog.mode == "aigc_image_to_skin",
            models.GenerationLog.status.in_({"pending", "processing"}),
            models.GenerationLog.provider_task_id.is_not(None),
            models.GenerationLog.edited_result.is_(None),
            models.GenerationLog.result.is_(None),
        )
        stale_logs = db.query(models.GenerationLog).filter(
            models.GenerationLog.status.in_(RECOVERABLE_GENERATION_STATUSES),
            or_(
                models.GenerationLog.recoverable == True,
                sking_ddj_pending_stage_one,
                sking_ddj_accepted_stage_one,
                sking_ddj_second_stage,
            ),
            models.GenerationLog.created_at < stale_before,
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
            Queue("queue_real_to_render", connection=redis_conn),
            Queue("high_queue_real_to_render", connection=redis_conn),
            Queue("queue_render_to_uv", connection=redis_conn),
            Queue("high_queue_render_to_uv", connection=redis_conn),
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
                    reused_job = False
                    if (
                        is_sking_ddj_model(log.model_version)
                        and log.mode == "aigc_image_to_skin"
                        and log.status in {"pending", "processing"}
                        and not log.provider_task_id
                        and not log.edited_result
                        and not log.result
                    ):
                        reused_job = requeue_existing_dense_uv_stage_one_job(log)

                    if not reused_job:
                        enqueue_recovered_generation_task(
                            log,
                            bool(user and user.is_pro_active),
                        )
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
