import pytest
import uuid
import datetime
from PIL import Image
import io
import base64
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import BackgroundTasks

from main import app
from auth import get_current_user, get_current_user_optional
from models import User, GenerationLog, UserFeedback, CreditLog
import routers.generate

pytestmark = pytest.mark.usefixtures("mock_auth", "mock_db_session")

# 1. Mock User/Permissions
@pytest.fixture()
def mock_auth(db):
    import datetime
    user = User(
        id="test_user_generate",
        email="test_generate@example.com",
        username="Tester",
        terms_agreed=True,
        pro_level="pro-plus",
        pro_expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365), # Enable Pro for private asset testing
        credits=100
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user
    app.dependency_overrides[get_current_user_optional] = mock_get_current_user
    yield
    # Clear overrides
    app.dependency_overrides.clear()

# 2. Override Routers Database Connection Pool
@pytest.fixture()
def mock_db_session(db):
    # Point routers.generate.SessionLocal to test database
    with patch("routers.generate.SessionLocal", return_value=db):
        yield

# 3. Dummy Image Data Generation
def get_dummy_base64_image():
    img = Image.new('RGB', (128, 128), color = 'red')
    img_io = io.BytesIO()
    img.save(img_io, format='PNG')
    return base64.b64encode(img_io.getvalue()).decode('utf-8')

# ----------------- API Layer Tests -----------------

def test_get_models(client):
    response = client.get("/skin/api/models")
    assert response.status_code == 200
    data = response.json()
    assert "image_to_skin_models" in data
    assert "sking_v73_flux_4b_000027000" in data["image_to_skin_models"]
    assert "SKING_DDJ_v54" in data["image_to_skin_models"]
    assert "SkingDDJ_v1" not in data["image_to_skin_models"]
    assert "text_to_image_models" in data
    assert "image_edit_models" in data


@patch("routers.generate.backend_utils.get_generation_credit_cost", return_value=5)
def test_get_generation_credit_cost(mock_credit_cost, client):
    response = client.get("/skin/api/generation_credit_cost")
    assert response.status_code == 200
    assert response.json() == {"credits": 5, "is_pro": False}
    mock_credit_cost.assert_called_once()

def test_get_generation_credit_cost_with_params(client):
    import backend_utils
    backend_utils.redis_conn.set("config:model_price:SKING_DDJ_v54", "3")
    backend_utils.redis_conn.set("config:model_price:z_image", "2")

    try:
        # Test model_version only
        response = client.get("/skin/api/generation_credit_cost?model_version=SKING_DDJ_v54")
        assert response.status_code == 200
        assert response.json() == {"credits": 3, "is_pro": False}

        # Test aux_model_version + model_version
        response = client.get("/skin/api/generation_credit_cost?aux_model_version=z_image&model_version=SKING_DDJ_v54")
        assert response.status_code == 200
        assert response.json() == {"credits": 5, "is_pro": False}
    finally:
        backend_utils.redis_conn.delete("config:model_price:SKING_DDJ_v54")
        backend_utils.redis_conn.delete("config:model_price:z_image")

def test_get_generation_credit_cost_pro_exclusive(client):
    import backend_utils
    backend_utils.redis_conn.set("config:model_price:SKING_DDJ_v54", "3")
    backend_utils.redis_conn.set("config:model_pro:SKING_DDJ_v54", "1")

    try:
        response = client.get("/skin/api/generation_credit_cost?model_version=SKING_DDJ_v54")
        assert response.status_code == 200
        assert response.json() == {"credits": 3, "is_pro": True}
    finally:
        backend_utils.redis_conn.delete("config:model_price:SKING_DDJ_v54")
        backend_utils.redis_conn.delete("config:model_pro:SKING_DDJ_v54")

def test_get_active_generation_none(client, db):
    response = client.get("/skin/api/generate/active")
    assert response.status_code == 200
    assert response.json() == {"has_active_task": False}

def test_get_active_generation_includes_pending_skin(client, db):
    log = GenerationLog(
        prompt="stage two",
        user_id="test_user_generate",
        mode="aigc_text_to_skin",
        status="pending_skin",
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    response = client.get("/skin/api/generate/active")
    assert response.status_code == 200
    data = response.json()
    assert data["has_active_task"] is True
    assert data["task"]["id"] == log.id
    assert data["task"]["status"] == "pending_skin"


def test_generation_result_update_ignores_stale_stage1_failure_after_stage2_started():
    log = GenerationLog(
        id="result_guard",
        prompt="guard",
        user_id="test_user_generate",
        mode="aigc_text_to_skin",
        status="processing_skin",
        edited_result="edited/current.jpg",
    )

    updated = routers.generate.apply_generation_result_update(
        log,
        {
            "log_id": log.id,
            "status": "failed",
            "stage": "text_to_image",
            "error_msg": "late first-stage failure",
        },
    )

    assert updated is False
    assert log.status == "processing_skin"
    assert log.error_msg is None


def test_generation_result_update_does_not_downgrade_success():
    log = GenerationLog(
        id="success_guard",
        prompt="guard",
        user_id="test_user_generate",
        mode="aigc_image_to_skin",
        status="success",
        result="generations/final.png",
    )

    updated = routers.generate.apply_generation_result_update(
        log,
        {
            "log_id": log.id,
            "status": "failed",
            "stage": "image_to_skin",
            "error_msg": "late failure",
        },
    )

    assert updated is False
    assert log.status == "success"
    assert log.result == "generations/final.png"


def test_generation_result_update_clears_retry_error():
    log = GenerationLog(
        id="retry_guard",
        prompt="guard",
        user_id="test_user_generate",
        mode="aigc_image_to_skin",
        status="failed",
        error_msg="transient",
    )

    updated = routers.generate.apply_generation_result_update(
        log,
        {
            "log_id": log.id,
            "status": "processing_skin",
            "stage": "image_to_skin",
        },
    )

    assert updated is True
    assert log.status == "processing_skin"
    assert log.error_msg is None


def test_failed_generation_refund_is_idempotent(db):
    user = db.query(User).filter(User.id == "test_user_generate").one()
    user.credits = 7
    log = GenerationLog(
        id="refund_once",
        prompt="refund",
        user_id=user.id,
        mode="aigc_image_to_skin",
        status="failed",
        credits_charged=3,
        credits_refunded=False,
    )
    db.add(log)
    db.commit()

    first = routers.generate.refund_generation_credits(db, log)
    db.commit()
    second = routers.generate.refund_generation_credits(db, log)
    db.commit()

    db.refresh(user)
    db.refresh(log)
    refund_logs = db.query(CreditLog).filter(
        CreditLog.user_id == user.id,
        CreditLog.action == "refund",
        CreditLog.source == f"Skin Generation Refund: {log.id}",
    ).all()
    assert first == 3
    assert second == 0
    assert user.credits == 10
    assert log.credits_refunded is True
    assert len(refund_logs) == 1
    assert refund_logs[0].amount == 3


def test_generation_result_update_persists_stage1_provider_metadata():
    log = GenerationLog(
        id="provider_metadata",
        prompt="metadata",
        user_id="test_user_generate",
        mode="aigc_image_to_skin",
        status="processing",
    )

    updated = routers.generate.apply_generation_result_update(
        log,
        {
            "log_id": log.id,
            "status": "processing",
            "stage": "real_to_render",
            "provider_task_id": "provider-task-123",
            "pipeline_version": "sample-v0",
        },
    )

    assert updated is True
    assert log.provider_task_id == "provider-task-123"
    assert log.pipeline_version == "sample-v0"


def test_unrecoverable_generation_has_no_rq_retry():
    log = GenerationLog(recoverable=False)
    assert routers.generate.get_generation_retry_policy_for_log(log) is None


def test_recoverable_generation_keeps_rq_retry():
    log = GenerationLog(recoverable=True)
    retry = routers.generate.get_generation_retry_policy_for_log(log)
    assert retry is not None
    assert retry.max == 99999


def test_dense_uv_model_routes_to_real_to_render_without_retry(monkeypatch):
    enqueued = []

    class FakeQueue:
        def __init__(self, name, connection=None):
            self.name = name

        def enqueue(self, *args, **kwargs):
            enqueued.append((self.name, args, kwargs))
            return object()

    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    log = GenerationLog(
        id="dense-route",
        mode="aigc_image_to_skin",
        model_version="SKING_DDJ_v54",
        source="uploads/dense-route.png",
        is_public=False,
        recoverable=False,
    )

    routers.generate.enqueue_generation_task(
        log,
        is_pro_active=True,
        content_type="image/png",
    )

    assert len(enqueued) == 1
    queue_name, args, kwargs = enqueued[0]
    assert queue_name == "high_queue_real_to_render"
    assert args[0] == "tasks.submit_real_to_render"
    assert kwargs["args"] == (
        "dense-route",
        False,
        "uploads/dense-route.png",
        "image/png",
        "high_",
    )
    assert kwargs["retry"] is None
    assert kwargs["job_id"] == "generation_dense-route_real_to_render"
    assert kwargs["on_failure"].func == (
        "tasks.real_to_render_job_failure"
    )
    assert kwargs["on_stopped"].func == (
        "tasks.real_to_render_job_stopped"
    )


def test_dense_uv_model_is_rejected_for_text_to_skin(client):
    response = client.post(
        "/skin/api/generate",
        data={
            "prompt": "not a direct image pipeline",
            "mode": "aigc_text_to_skin",
            "aux_model_version": "z_image",
            "model_version": "SKING_DDJ_v54",
        },
    )

    assert response.status_code == 400
    assert "Invalid model version combination" in response.json()["detail"]


def test_dense_uv_generation_is_unrecoverable(
    monkeypatch,
    client,
    db,
):
    image = Image.new("RGB", (768, 768), color="red")
    image_file = io.BytesIO()
    image.save(image_file, format="PNG")
    monkeypatch.setattr(
        routers.generate,
        "upload_to_s3",
        lambda *args, **kwargs: "uploads/dense.png",
    )
    monkeypatch.setattr(
        routers.generate,
        "enqueue_generation_task",
        lambda *args, **kwargs: object(),
    )

    response = client.post(
        "/skin/api/generate",
        data={
            "mode": "aigc_image_to_skin",
            "model_version": "SKING_DDJ_v54",
        },
        files={
            "file": (
                "source.png",
                image_file.getvalue(),
                "image/png",
            )
        },
    )

    assert response.status_code == 200
    log = db.query(GenerationLog).filter(
        GenerationLog.id == response.json()["id"]
    ).one()
    assert log.model_version == "SKING_DDJ_v54"
    assert log.source == f"uploads/{log.id}.png"
    assert log.edited_result is None
    assert log.recoverable is False


@patch("rq.Queue.enqueue", side_effect=RuntimeError("redis unavailable"))
@patch("routers.generate.backend_utils.get_model_credit_cost", return_value=2)
def test_enqueue_failure_refunds_credits_and_records_history(
    mock_credit_cost,
    mock_enqueue,
    client,
    db,
):
    user = db.query(User).filter(User.id == "test_user_generate").one()
    starting_credits = user.credits

    response = client.post(
        "/skin/api/generate",
        data={
            "prompt": "refund enqueue failure",
            "is_public": True,
            "aux_model_version": "z_image",
            "model_version": "sking_v73_flux_4b_000027000",
            "mode": "aigc_text_to_skin",
        },
    )

    assert response.status_code == 500
    db.refresh(user)
    log = db.query(GenerationLog).filter(
        GenerationLog.prompt == "refund enqueue failure"
    ).one()
    refund = db.query(CreditLog).filter(
        CreditLog.user_id == user.id,
        CreditLog.action == "refund",
        CreditLog.source == f"Skin Generation Refund: {log.id}",
    ).one()
    assert log.status == "failed"
    assert log.credits_charged == 4
    assert log.credits_refunded is True
    assert refund.amount == 4
    assert user.credits == starting_credits
    assert mock_credit_cost.call_count == 2
    mock_enqueue.assert_called_once()


@patch("rq.Queue.enqueue")
@patch("routers.generate.backend_utils.get_generation_credit_cost", return_value=1)
def test_submit_generate_text_to_skin(mock_credit_cost, mock_enqueue, client, db):
    user = db.query(User).filter(User.id == "test_user_generate").one()
    user.credits = 1
    db.commit()

    payload = {
        "prompt": "cute girl with hoodie",
        "is_public": True,
        "aux_model_version": "z_image",
        "model_version": "sking_v73_flux_4b_000027000",
        "mode": "aigc_text_to_skin"
    }
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert "id" in data

    log = db.query(GenerationLog).filter(GenerationLog.id == data["id"]).first()
    assert log is not None
    assert log.prompt == "cute girl with hoodie"
    assert log.status == "pending"
    db.refresh(user)
    assert user.credits == 0
    credit_log = db.query(CreditLog).filter(
        CreditLog.user_id == user.id,
        CreditLog.action == "generation",
    ).one()
    assert credit_log.amount == -1
    assert credit_log.source == f"Skin Generation: {log.id}"
    mock_credit_cost.assert_called_once()
    mock_enqueue.assert_called_once()


@patch("rq.Queue.enqueue")
@patch("routers.generate.backend_utils.get_generation_credit_cost", return_value=4)
def test_submit_generate_uses_dynamic_credit_cost(mock_credit_cost, mock_enqueue, client, db):
    user = db.query(User).filter(User.id == "test_user_generate").one()
    user.credits = 4
    db.commit()

    response = client.post("/skin/api/generate", data={
        "prompt": "dynamic cost",
        "is_public": True,
        "aux_model_version": "z_image",
        "model_version": "sking_v73_flux_4b_000027000",
        "mode": "aigc_text_to_skin",
    })

    assert response.status_code == 200
    db.refresh(user)
    assert user.credits == 0
    credit_log = db.query(CreditLog).filter(
        CreditLog.user_id == user.id,
        CreditLog.action == "generation",
    ).one()
    assert credit_log.amount == -4
    mock_credit_cost.assert_called_once()
    mock_enqueue.assert_called_once()


@patch("rq.Queue.enqueue")
def test_submit_generate_uses_model_specific_credit_cost(mock_enqueue, client, db):
    user = db.query(User).filter(User.id == "test_user_generate").one()
    user.credits = 10
    db.commit()

    import backend_utils
    backend_utils.redis_conn.set("config:model_price:sking_v73_flux_4b_000027000", "7")

    try:
        response = client.post("/skin/api/generate", data={
            "prompt": "custom model price test",
            "is_public": True,
            "aux_model_version": "z_image",
            "model_version": "sking_v73_flux_4b_000027000",
            "mode": "aigc_text_to_skin",
        })

        assert response.status_code == 200
        db.refresh(user)
        assert user.credits == 3  # 10 - 7 = 3
        credit_log = db.query(CreditLog).filter(
            CreditLog.user_id == user.id,
            CreditLog.action == "generation",
        ).order_by(CreditLog.created_at.desc()).first()
        assert credit_log.amount == -7
    finally:
        backend_utils.redis_conn.delete("config:model_price:sking_v73_flux_4b_000027000")


# ----------------- Background Worker Task Tests -----------------

# from routers.generate import process_generation
# process_generation is removed in favor of worker_tasks.py


# Tests for process_generation are disabled as it's no longer in the router


# ----------------- More API Tests (Coverage) -----------------

@patch("routers.generate.get_cdn_url")
@patch("routers.generate.generate_presigned_url_get")
def test_get_history(mock_presigned, mock_cdn, client, db):
    mock_cdn.return_value = "http://cdn.com/test.png"
    mock_presigned.return_value = "http://s3.com/test_priv.png"

    # Create test data
    for i in range(3):
        log = GenerationLog(
            prompt=f"item {i}", 
            user_id="test_user_generate", 
            mode="aigc_text_to_skin",
            is_public=True,
            result=f"res_{i}.png"
        )
        db.add(log)
    db.commit()

    response = client.get("/skin/api/history")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3

@patch("routers.generate.get_cdn_url")
@patch("routers.generate.generate_presigned_url_get")
def test_get_log_public(mock_presigned, mock_cdn, client, db):
    log = GenerationLog(prompt="public_log", is_public=True, user_id="test_user_generate", mode="edit")
    db.add(log)
    db.commit()
    db.refresh(log)

    mock_cdn.return_value = "http://cdn.com/test.png"

    response = client.get(f"/skin/api/logs/{log.id}")
    assert response.status_code == 200
    assert response.json()["prompt"] == "public_log"
    assert response.json()["has_feedback"] is False


@patch("routers.generate.get_cdn_url")
@patch("routers.generate.generate_presigned_url_get")
def test_get_log_includes_existing_feedback(mock_presigned, mock_cdn, client, db):
    log = GenerationLog(prompt="feedback_log", is_public=True, user_id="test_user_generate", mode="aigc_text_to_skin")
    db.add(log)
    db.commit()
    db.refresh(log)
    db.add(UserFeedback(user_id="test_user_generate", log_id=log.id, is_good=True))
    db.commit()

    mock_cdn.return_value = "http://cdn.com/test.png"

    response = client.get(f"/skin/api/logs/{log.id}")
    assert response.status_code == 200
    assert response.json()["has_feedback"] is True

@patch("routers.generate.get_cdn_url")
@patch("routers.generate.generate_presigned_url_get")
def test_get_log_private_owner(mock_presigned, mock_cdn, client, db):
    log = GenerationLog(prompt="private_log", is_public=False, user_id="test_user_generate", mode="edit", result="private.png")
    db.add(log)
    db.commit()
    db.refresh(log)

    mock_presigned.return_value = "http://s3.com/test_priv.png"

    response = client.get(f"/skin/api/logs/{log.id}")
    assert response.status_code == 200
    assert response.json()["prompt"] == "private_log"

@patch("routers.generate.cancel_generation_jobs")
@patch("routers.generate.BackgroundTasks.add_task")
def test_delete_log(mock_add_task, mock_cancel_jobs, client, db):
    log = GenerationLog(
        prompt="to_delete", 
        user_id="test_user_generate", 
        mode="edit",
        source="uploads/source.png",
        result="generations/result.png",
        is_public=True
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    response = client.delete(f"/skin/api/logs/{log.id}")
    assert response.status_code == 200
    
    # Verify soft delete status and attribute clearing in DB
    db.refresh(log)
    assert log.is_deleted is True
    assert log.prompt is None
    assert log.name == "Deleted"
    assert log.status == "deleted"
    assert log.source is None
    assert log.result is None
    mock_cancel_jobs.assert_called_once_with(log.id)

    # Verify S3 cleanup background task triggered
    mock_add_task.assert_called_once()
    args = mock_add_task.call_args[0]
    assert args[0].__name__ == "delete_s3_files_task"
    files_list = args[1]
    assert ("uploads/source.png", True) in files_list
    assert ("generations/result.png", True) in files_list

def test_delete_log_quota_limit(client, db):
    # Change status to non-Pro user
    user = db.query(User).filter(User.id == "test_user_generate").first()
    user.pro_expires_at = None
    db.commit()

    # Create logs to delete
    log1 = GenerationLog(prompt="log1", user_id="test_user_generate", mode="edit", is_public=True)
    log2 = GenerationLog(prompt="log2", user_id="test_user_generate", mode="edit", is_public=True)
    db.add_all([log1, log2])
    db.commit()

    # Delete first log - should succeed
    response = client.delete(f"/skin/api/logs/{log1.id}")
    assert response.status_code == 200

    # Delete second log - should fail due to daily quota limit
    response = client.delete(f"/skin/api/logs/{log2.id}")
    assert response.status_code == 403
    assert "Free users can only delete 1 skin per day" in response.json()["detail"]

    # Restore user to Pro
    import datetime
    user.pro_expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    db.commit()

def test_toggle_like(client, db):
    log = GenerationLog(prompt="to_like", user_id="test_user_generate", mode="edit")
    db.add(log)
    db.commit()
    db.refresh(log)

    response = client.post(f"/skin/api/like/{log.id}")
    assert response.status_code == 200
    assert response.json()["action"] == "liked"
    
    db.refresh(log)
    assert log.likes_count == 1

    # Repeat request to unlike
    response = client.post(f"/skin/api/like/{log.id}")
    assert response.status_code == 200
    assert response.json()["action"] == "unliked"
    
    db.refresh(log)
    assert log.likes_count == 0

def test_toggle_like_rejects_other_user_private_log(client, db):
    other_user = User(id="private_owner", email="private-owner@example.com", username="Private Owner")
    log = GenerationLog(
        prompt="secret",
        user_id=other_user.id,
        mode="edit",
        is_public=False,
        status="success",
    )
    db.add_all([other_user, log])
    db.commit()

    response = client.post(f"/skin/api/like/{log.id}")
    assert response.status_code == 403
    db.refresh(log)
    assert log.likes_count == 0

def test_toggle_like_rejects_deleted_log(client, db):
    log = GenerationLog(
        prompt="deleted",
        user_id="test_user_generate",
        mode="edit",
        is_deleted=True,
        status="deleted",
    )
    db.add(log)
    db.commit()

    response = client.post(f"/skin/api/like/{log.id}")
    assert response.status_code == 404

# ----------------- More Logic Branches and Error Tests -----------------

def test_generate_validation_fail_guidance(client):
    payload = {"prompt": "test", "guidance": 20.0, "aux_model_version": "z_image", "model_version": "sking_v73_flux_4b_000027000"}  # Too large
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 400
    assert "Guidance must be between" in response.json()["detail"]

def test_generate_validation_fail_n_step(client):
    payload = {"prompt": "test", "n_step": 10, "aux_model_version": "z_image", "model_version": "sking_v73_flux_4b_000027000"}  # Too small
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 400
    assert "n_step must be between" in response.json()["detail"]

def test_generate_private_non_pro(client, db):
    # Change status to non-Pro user
    user = db.query(User).filter(User.id == "test_user_generate").first()
    user.pro_expires_at = None
    db.commit()

    payload = {"prompt": "test", "is_public": False, "aux_model_version": "z_image", "model_version": "sking_v73_flux_4b_000027000"}
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 403
    assert "Free users have no private quota" in response.json()["detail"]

    # Restore status to avoid interference with subsequent tests
    import datetime
    user.pro_expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    db.commit()

def test_generate_queue_full(client, db):
    # Fill queue: exceed limit (for non-Pro users)
    user = db.query(User).filter(User.id == "test_user_generate").first()
    user.pro_expires_at = None
    db.commit()

    for i in range(4):
        log = GenerationLog(status="pending", user_id="test_user_generate", mode="edit")
        db.add(log)
    db.commit()

    payload = {"prompt": "test", "aux_model_version": "z_image", "model_version": "sking_v73_flux_4b_000027000"}
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 429
    assert "task(s) in the queue" in response.json()["detail"]

    # Restore status
    import datetime
    user.pro_expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    db.commit()

@patch("rq.Queue.enqueue")
def test_generate_queue_limit_counts_pending_skin(mock_enqueue, client, db):
    user = db.query(User).filter(User.id == "test_user_generate").first()
    user.pro_expires_at = None
    db.add(GenerationLog(status="pending_skin", user_id="test_user_generate", mode="aigc_text_to_skin"))
    db.commit()

    response = client.post("/skin/api/generate", data={"prompt": "blocked", "aux_model_version": "z_image", "model_version": "sking_v73_flux_4b_000027000"})
    assert response.status_code == 429
    assert "task(s) in the queue" in response.json()["detail"]
    mock_enqueue.assert_not_called()


def test_enqueue_image_to_skin_disables_rq_timeout(monkeypatch):
    enqueued = []

    class FakeQueue:
        def __init__(self, name, connection=None):
            self.name = name

        def enqueue(self, *args, **kwargs):
            enqueued.append((self.name, args, kwargs))
            return object()

    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    log = GenerationLog(
        id="no_timeout",
        prompt="retry S3 forever",
        mode="aigc_image_to_skin",
        status="pending",
        source="uploads/no_timeout.png",
        model_version="sking_v73_flux_4b_000027000",
        recoverable=True,
        is_public=True,
    )

    routers.generate.enqueue_image_to_skin_task(log, False)

    assert len(enqueued) == 1
    queue_name, args, kwargs = enqueued[0]
    assert queue_name == "queue_image_to_skin"
    assert args[0] == "worker_tasks.task_image_to_skin"
    assert kwargs["job_timeout"] == -1


def test_cancel_generation_jobs_sets_tombstone_and_cancels_all_job_types(
    monkeypatch,
):
    log_id = "cancel_all_jobs"
    set_calls = []
    fetched_job_ids = []
    cancelled_job_ids = []

    class FakeRedisConnection:
        def set(self, key, value, ex=None):
            set_calls.append((key, value, ex))
            return True

        def scan_iter(self, match):
            assert match == f"rq:job:real_to_render_poll_{log_id}_*"
            return [
                f"rq:job:real_to_render_poll_{log_id}_3".encode("utf-8")
            ]

    existing_job_ids = {
        f"generation_{log_id}_image_to_skin",
        f"generation_{log_id}_real_to_render",
        f"real_to_render_poll_{log_id}_3",
    }

    class FakeJob:
        def __init__(self, job_id):
            self.id = job_id

        @classmethod
        def fetch(cls, job_id, connection):
            assert connection is fake_redis
            fetched_job_ids.append(job_id)
            if job_id not in existing_job_ids:
                from rq.exceptions import NoSuchJobError
                raise NoSuchJobError
            return cls(job_id)

        def cancel(self):
            cancelled_job_ids.append(self.id)

    fake_redis = FakeRedisConnection()
    monkeypatch.setattr(routers.generate, "redis_conn", fake_redis)
    monkeypatch.setattr(routers.generate, "Job", FakeJob)

    cancelled = routers.generate.cancel_generation_jobs(log_id)

    assert set_calls == [
        (
            routers.generate.generation_cancellation_key(log_id),
            "1",
            routers.generate.GENERATION_CANCELLATION_TTL_SECONDS,
        )
    ]
    assert set(cancelled) == existing_job_ids
    assert set(cancelled_job_ids) == existing_job_ids
    assert f"generation_{log_id}_text_to_image" in fetched_job_ids
    assert f"generation_{log_id}_render_to_uv" in fetched_job_ids


def test_re_enqueue_if_missing_recovers_pending_skin(monkeypatch, db):
    user = User(id="recover_user", email="recover@example.com", username="Recover")
    log = GenerationLog(
        id="recover_log",
        prompt="recover",
        user_id=user.id,
        mode="aigc_text_to_skin",
        status="pending_skin",
        edited_result="edited/recover.jpg",
        model_version="sking_v73_flux_4b_000027000",
        aux_model_version="z_image",
        recoverable=True,
        created_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=11),
    )
    db.add_all([user, log])
    db.commit()

    class FakeQueue:
        enqueued = []

        def __init__(self, name, connection=None):
            self.name = name
            self.jobs = []

        def enqueue(self, *args, **kwargs):
            self.enqueued.append((self.name, args, kwargs))
            return object()

    class FakeRegistry:
        def __init__(self, name, connection=None):
            self.name = name

        def get_job_ids(self):
            return []

    import rq.registry

    FakeQueue.enqueued.clear()
    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    monkeypatch.setattr(routers.generate, "SessionLocal", lambda: db)
    monkeypatch.setattr(rq.registry, "StartedJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "DeferredJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "ScheduledJobRegistry", FakeRegistry)

    routers.generate.re_enqueue_if_missing()

    assert len(FakeQueue.enqueued) == 1
    queue_name, args, kwargs = FakeQueue.enqueued[0]
    assert queue_name == "queue_image_to_skin"
    assert args[0] == "worker_tasks.task_image_to_skin"
    assert kwargs["args"][0] == "recover_log"
    assert kwargs["args"][2] == "edited/recover.jpg"
    assert kwargs["kwargs"]["intermediate_filename"] == "edited/recover.jpg"
    assert kwargs["job_timeout"] == -1
    assert kwargs["job_id"] == "generation_recover_log_image_to_skin"


@pytest.mark.parametrize("status", ["pending_skin", "processing_skin"])
def test_re_enqueue_if_missing_recovers_dense_uv_second_stage(
    monkeypatch,
    db,
    status,
):
    user = User(id="unrecover_user", email="unrecover@example.com", username="Unrecover")
    log = GenerationLog(
        id="unrecover_log",
        prompt="unrecover",
        user_id=user.id,
        mode="aigc_image_to_skin",
        status=status,
        edited_result="real_to_render_intermediate/unrecover.png",
        model_version="SKING_DDJ_v54",
        pipeline_version="dense-pipeline-v1",
        aux_model_version="z_image",
        recoverable=False,
        created_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=11),
    )
    db.add_all([user, log])
    db.commit()

    class FakeQueue:
        enqueued = []

        def __init__(self, name, connection=None):
            self.name = name
            self.jobs = []

        def enqueue(self, *args, **kwargs):
            self.enqueued.append((self.name, args, kwargs))
            return object()

    class FakeRegistry:
        def __init__(self, name, connection=None):
            self.name = name

        def get_job_ids(self):
            return []

    import rq.registry

    FakeQueue.enqueued.clear()
    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    monkeypatch.setattr(routers.generate, "SessionLocal", lambda: db)
    monkeypatch.setattr(rq.registry, "StartedJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "DeferredJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "ScheduledJobRegistry", FakeRegistry)

    routers.generate.re_enqueue_if_missing()

    assert len(FakeQueue.enqueued) == 1
    queue_name, args, kwargs = FakeQueue.enqueued[0]
    assert queue_name == "queue_render_to_uv"
    assert args[0] == "worker_tasks.task_render_to_uv"
    assert kwargs["args"] == (
        "unrecover_log",
        True,
        "real_to_render_intermediate/unrecover.png",
        "image/png",
        "dense-pipeline-v1",
    )
    assert kwargs["job_timeout"] == 120
    assert kwargs["retry"] is None
    assert kwargs["job_id"] == "generation_unrecover_log_render_to_uv"


def test_re_enqueue_if_missing_does_not_resubmit_dense_uv_stage_one(
    monkeypatch,
    db,
):
    log = GenerationLog(
        id="dense_stage_one",
        prompt="do not resubmit",
        user_id="test_user_generate",
        mode="aigc_image_to_skin",
        status="processing",
        model_version="SKING_DDJ_v54",
        provider_task_id="provider-task",
        recoverable=False,
        created_at=(
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=11)
        ),
    )
    db.add(log)
    db.commit()

    class FakeQueue:
        enqueued = []

        def __init__(self, name, connection=None):
            self.name = name
            self.jobs = []

        def enqueue(self, *args, **kwargs):
            self.enqueued.append((self.name, args, kwargs))
            return object()

    class FakeRegistry:
        def __init__(self, name, connection=None):
            self.name = name

        def get_job_ids(self):
            return []

    import rq.registry

    FakeQueue.enqueued.clear()
    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    monkeypatch.setattr(routers.generate, "SessionLocal", lambda: db)
    monkeypatch.setattr(rq.registry, "StartedJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "DeferredJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "ScheduledJobRegistry", FakeRegistry)

    routers.generate.re_enqueue_if_missing()

    assert FakeQueue.enqueued == []


def test_re_enqueue_if_missing_requeues_dense_uv_stage_one_ghost(
    monkeypatch,
    db,
):
    log = GenerationLog(
        id="dense_stage_one_ghost",
        prompt="recover queued ghost",
        user_id="test_user_generate",
        mode="aigc_image_to_skin",
        status="pending",
        source="uploads/dense_stage_one_ghost.png",
        model_version="SKING_DDJ_v54",
        recoverable=False,
        created_at=(
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=11)
        ),
    )
    db.add(log)
    db.commit()

    class ExistingJob:
        id = "generation_dense_stage_one_ghost_real_to_render"
        args = ("dense_stage_one_ghost",)
        func_name = "tasks.submit_real_to_render"
        origin = "queue_real_to_render"

        def get_status(self, refresh=False):
            return "queued"

    existing_job = ExistingJob()

    class FakeQueue:
        enqueued = []
        requeued = []

        def __init__(self, name, connection=None):
            self.name = name
            self.jobs = []

        def enqueue(self, *args, **kwargs):
            self.enqueued.append((self.name, args, kwargs))
            return object()

        def enqueue_job(self, job):
            self.requeued.append((self.name, job))
            return job

    class FakeRegistry:
        def __init__(self, name, connection=None):
            self.name = name

        def get_job_ids(self):
            return []

    import rq.registry

    FakeQueue.enqueued.clear()
    FakeQueue.requeued.clear()
    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    monkeypatch.setattr(routers.generate, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        routers.generate.Job,
        "fetch",
        staticmethod(lambda job_id, connection=None: existing_job),
    )
    monkeypatch.setattr(rq.registry, "StartedJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "DeferredJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "ScheduledJobRegistry", FakeRegistry)

    routers.generate.re_enqueue_if_missing()

    assert FakeQueue.enqueued == []
    assert FakeQueue.requeued == [
        ("queue_real_to_render", existing_job),
    ]


def test_re_enqueue_if_missing_recreates_missing_dense_uv_stage_one_job(
    monkeypatch,
    db,
):
    log = GenerationLog(
        id="dense_stage_one_missing",
        prompt="recover missing job",
        user_id="test_user_generate",
        mode="aigc_image_to_skin",
        status="pending",
        source="uploads/dense_stage_one_missing.png",
        model_version="SKING_DDJ_v54",
        recoverable=False,
        created_at=(
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=11)
        ),
    )
    db.add(log)
    db.commit()

    class FakeQueue:
        enqueued = []

        def __init__(self, name, connection=None):
            self.name = name
            self.jobs = []

        def enqueue(self, *args, **kwargs):
            self.enqueued.append((self.name, args, kwargs))
            return object()

    class FakeRegistry:
        def __init__(self, name, connection=None):
            self.name = name

        def get_job_ids(self):
            return []

    import rq.registry

    FakeQueue.enqueued.clear()
    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    monkeypatch.setattr(routers.generate, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        routers.generate.Job,
        "fetch",
        staticmethod(
            lambda job_id, connection=None: (_ for _ in ()).throw(
                routers.generate.NoSuchJobError()
            )
        ),
    )
    monkeypatch.setattr(rq.registry, "StartedJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "DeferredJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "ScheduledJobRegistry", FakeRegistry)

    routers.generate.re_enqueue_if_missing()

    assert len(FakeQueue.enqueued) == 1
    queue_name, args, kwargs = FakeQueue.enqueued[0]
    assert queue_name == "high_queue_real_to_render"
    assert args[0] == "tasks.submit_real_to_render"
    assert kwargs["job_id"] == (
        "generation_dense_stage_one_missing_real_to_render"
    )


def test_re_enqueue_if_missing_treats_intermediate_job_as_active(
    monkeypatch,
    db,
):
    log = GenerationLog(
        id="dense_stage_one_intermediate",
        prompt="already dequeued",
        user_id="test_user_generate",
        mode="aigc_image_to_skin",
        status="pending",
        source="uploads/dense_stage_one_intermediate.png",
        model_version="SKING_DDJ_v54",
        recoverable=False,
        created_at=(
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=11)
        ),
    )
    db.add(log)
    db.commit()

    class ActiveJob:
        args = ("dense_stage_one_intermediate",)

    active_job = ActiveJob()

    class FakeIntermediateQueue:
        def __init__(self, job_ids):
            self.job_ids = job_ids

        def get_job_ids(self):
            return self.job_ids

    class FakeQueue:
        enqueued = []

        def __init__(self, name, connection=None):
            self.name = name
            self.jobs = []
            self.intermediate_queue = FakeIntermediateQueue(
                ["generation_dense_stage_one_intermediate_real_to_render"]
                if name == "queue_real_to_render"
                else []
            )

        def enqueue(self, *args, **kwargs):
            self.enqueued.append((self.name, args, kwargs))
            return object()

    class FakeRegistry:
        def __init__(self, name, connection=None):
            self.name = name

        def get_job_ids(self):
            return []

    import rq.registry

    FakeQueue.enqueued.clear()
    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    monkeypatch.setattr(routers.generate, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        routers.generate.Job,
        "fetch",
        staticmethod(lambda job_id, connection=None: active_job),
    )
    monkeypatch.setattr(rq.registry, "StartedJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "DeferredJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "ScheduledJobRegistry", FakeRegistry)

    routers.generate.re_enqueue_if_missing()

    assert FakeQueue.enqueued == []


def test_re_enqueue_if_missing_does_not_duplicate_active_dense_uv_job(
    monkeypatch,
    db,
):
    log = GenerationLog(
        id="dense_active",
        prompt="already queued",
        user_id="test_user_generate",
        mode="aigc_image_to_skin",
        status="pending_skin",
        edited_result="real_to_render_intermediate/dense_active.png",
        model_version="SKING_DDJ_v54",
        pipeline_version="dense-pipeline-v1",
        recoverable=False,
        created_at=(
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=11)
        ),
    )
    db.add(log)
    db.commit()

    class ActiveJob:
        args = ("dense_active",)

    class FakeQueue:
        enqueued = []

        def __init__(self, name, connection=None):
            self.name = name
            self.jobs = (
                [ActiveJob()]
                if name == "queue_render_to_uv"
                else []
            )

        def enqueue(self, *args, **kwargs):
            self.enqueued.append((self.name, args, kwargs))
            return object()

    class FakeRegistry:
        def __init__(self, name, connection=None):
            self.name = name

        def get_job_ids(self):
            return []

    import rq.registry

    FakeQueue.enqueued.clear()
    monkeypatch.setattr(routers.generate, "Queue", FakeQueue)
    monkeypatch.setattr(routers.generate, "SessionLocal", lambda: db)
    monkeypatch.setattr(rq.registry, "StartedJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "DeferredJobRegistry", FakeRegistry)
    monkeypatch.setattr(rq.registry, "ScheduledJobRegistry", FakeRegistry)

    routers.generate.re_enqueue_if_missing()

    assert FakeQueue.enqueued == []


def test_get_log_not_found(client):
    response = client.get("/skin/api/logs/non_existent_id")
    assert response.status_code == 404

def test_get_log_private_denied(client, db):
    # Create another user
    other_user = User(id="other_user", email="other@ex.com", username="Other")
    db.add(other_user)
    db.commit()

    # Private record created by them
    log = GenerationLog(prompt="secret", is_public=False, user_id="other_user", mode="edit")
    db.add(log)
    db.commit()

    # Current logged-in Tester tries to access it
    response = client.get(f"/skin/api/logs/{log.id}")
    assert response.status_code == 403
    assert "Permission denied" in response.json()["detail"]

def test_get_derived_logs(client, db):
    # Create parent record
    parent_log = GenerationLog(prompt="parent", is_public=True, user_id="test_user_generate", mode="edit")
    db.add(parent_log)
    db.commit()
    db.refresh(parent_log)

    # Create derived child record
    child_log = GenerationLog(prompt="child", is_public=True, parent=parent_log.id, user_id="test_user_generate", mode="edit", result="res_child.png", status="success")
    db.add(child_log)
    db.commit()

    response = client.get(f"/skin/api/logs/{parent_log.id}/derived")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert len(data["items"]) == 1
    assert data["items"][0]["log_id"] == child_log.id

def test_update_log_name(client, db):
    log = GenerationLog(prompt="before", name="before", user_id="test_user_generate", mode="edit")
    db.add(log)
    db.commit()
    db.refresh(log)

    payload = {"name": "after"}
    response = client.patch(f"/skin/api/logs/{log.id}/name", json=payload)
    assert response.status_code == 200
    
    db.refresh(log)
    assert log.name == "after"

# ----------------- Background Task Modes Coverage -----------------

# Background task tests are disabled



# ----------------- Utility Function Unit Tests (No Mock) -----------------


@patch("s3_utils.s3_client")
@patch("s3_utils.settings")
def test_upload_to_s3_public(mock_settings, mock_s3_client):
    mock_settings.AWS_BUCKET_NAME = "pub-bucket"
    mock_settings.AWS_PRIVATE_BUCKET_NAME = "priv-bucket"
    
    from s3_utils import upload_to_s3
    res = upload_to_s3(b"data", "key", is_public=True)
    assert res == "key"
    mock_s3_client.put_object.assert_called_once_with(
        Bucket="pub-bucket",
        Key="key",
        Body=b"data",
        ContentType="image/png",
        ACL="public-read",
        CacheControl="public, max-age=31536000, immutable",
    )

@patch("s3_utils.s3_client")
@patch("s3_utils.settings")
def test_upload_to_s3_private(mock_settings, mock_s3_client):
    mock_settings.AWS_BUCKET_NAME = "pub-bucket"
    mock_settings.AWS_PRIVATE_BUCKET_NAME = "priv-bucket"
    
    from s3_utils import upload_to_s3
    res = upload_to_s3(b"data", "key", is_public=False)
    assert res == "key"
    mock_s3_client.put_object.assert_called_once_with(
        Bucket="priv-bucket",
        Key="key",
        Body=b"data",
        ContentType="image/png",
    )

# test_process_generation_no_images_fail is removed as process_generation is no longer in routers/generate.py


# ----------------- Discovery Interface Sorting and Search Tests -----------------

def test_get_discovery_random(client, db):
    # Clear cache to ensure update_discovery_cache is triggered
    import routers.generate
    routers.generate.discovery_cache_items = []

    for i in range(3):
        log = GenerationLog(
            prompt=f"discover {i}",
            is_public=True,
            user_id="test_user_generate",
            mode="edit",
            result=f"res_{i}.png",
            status="success",
            model_version=f"SKING_DDJ_v{i}",
        )
        db.add(log)
    db.add(GenerationLog(
        prompt="excluded discovery model",
        is_public=True,
        user_id="test_user_generate",
        mode="edit",
        result="excluded.png",
        status="success",
        model_version="sking_v73_flux_4b_000027000",
    ))
    db.commit()

    response = client.get("/skin/api/discovery")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60, s-maxage=300, stale-while-revalidate=600"
    assert response.headers["content-encoding"] == "gzip"
    data = response.json()
    assert len(data) >= 3
    assert all(item["prompt"].startswith("discover ") for item in data)
    assert "creator" in data[0]
    assert "likes_count" in data[0]


def test_discovery_search_filters_by_model_series(client, db):
    logs = [
        GenerationLog(
            prompt="ddj series",
            name="ddj series",
            is_public=True,
            user_id="test_user_generate",
            mode="edit",
            result="ddj.png",
            status="success",
            model_version="SKING_DDJ_v54",
        ),
        GenerationLog(
            prompt="sking series",
            name="sking series",
            is_public=True,
            user_id="test_user_generate",
            mode="edit",
            result="sking.png",
            status="success",
            model_version="sking_v73_flux_4b_000027000",
        ),
        GenerationLog(
            prompt="other series",
            name="other series",
            is_public=True,
            user_id="test_user_generate",
            mode="edit",
            result="other.png",
            status="success",
            model_version="other_v1",
        ),
    ]
    db.add_all(logs)
    db.commit()

    response = client.get(
        "/skin/api/discovery/search",
        params={"model_series": "SKING_DDJ"},
    )
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {logs[0].id}

    response = client.get(
        "/skin/api/discovery/search",
        params={"model_series": "sking"},
    )
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {logs[1].id}

    response = client.get(
        "/skin/api/discovery/search",
        params={"model_series": ""},
    )
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {
        log.id for log in logs
    }

    response = client.get(
        "/skin/api/discovery/search",
        params={"model_series": "invalid"},
    )
    assert response.status_code == 422


# (Obsolete sort/search tests removed)

# ----------------- Background Scheduled Task Coverage -----------------

@pytest.mark.anyio
async def test_start_discovery_cache_job():
    from routers.generate import start_discovery_cache_job
    import asyncio
    
    with patch("routers.generate.update_discovery_cache") as mock_update:
        # Mock sleep to raise CancelledError immediately on first call to stop the while true loop
        with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await start_discovery_cache_job()
        mock_update.assert_called_once()

@pytest.mark.anyio
async def test_start_discovery_cache_job_error():
    from routers.generate import start_discovery_cache_job
    import asyncio
    
    with patch("routers.generate.update_discovery_cache", side_effect=Exception("mock_error")):
        # Sleep raises CancelledError on first call to escape loop
        with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await start_discovery_cache_job()
            # print should cover the except block output





@patch("rq.Queue.enqueue")
@patch("rq.Queue.__init__", return_value=None)
def test_generate_pro_priority(mock_q_init, mock_enqueue, client, db):
    # Default mock_auth user is Pro
    payload = {
        "prompt": "pro task",
        "mode": "aigc_text_to_skin",
        "aux_model_version": "z_image",
        "model_version": "sking_v73_flux_4b_000027000"
    }
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 200
    
    # Verify Queue was initialized with 'high_' prefix
    # Need to check the call arguments of Queue.__init__
    queue_names = [call.args[0] for call in mock_q_init.call_args_list]
    assert "high_queue_text_to_image" in queue_names

@patch("rq.Queue.enqueue")
@patch("rq.Queue.__init__", return_value=None)
def test_generate_normal_priority(mock_q_init, mock_enqueue, client, db):
    # Manually modify user to be non-Pro
    user = db.query(User).filter(User.id == "test_user_generate").first()
    user.pro_expires_at = None
    db.commit()
    
    payload = {
        "prompt": "normal task",
        "mode": "aigc_text_to_skin",
        "aux_model_version": "z_image",
        "model_version": "sking_v73_flux_4b_000027000"
    }
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 200
    
    # Verify Queue was initialized WITHOUT 'high_' prefix
    queue_names = [call.args[0] for call in mock_q_init.call_args_list]
    assert "queue_text_to_image" in queue_names
    assert "high_queue_text_to_image" not in queue_names


@patch("routers.generate.BackgroundTasks.add_task")
def test_delete_log_deletes_feedback(mock_add_task, client, db):
    # 1. Create a log
    log = GenerationLog(
        id="test_log_fb_del",
        prompt="to_delete_with_feedback",
        user_id="test_user_generate",
        mode="aigc_text_to_skin",
        is_public=True
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    # 2. Create feedback for this log
    from models import UserFeedback
    feedback = UserFeedback(
        log_id=log.id,
        is_good=True
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)

    # Verify feedback exists
    assert db.query(UserFeedback).filter(UserFeedback.log_id == log.id).count() == 1

    # 3. Call deletion endpoint using the correct '/skin' prefix
    response = client.delete(f"/skin/api/logs/{log.id}")
    assert response.status_code == 200

    # 4. Verify feedback is deleted
    assert db.query(UserFeedback).filter(UserFeedback.log_id == log.id).count() == 0


def test_log_feedback_is_idempotent_for_user(client, db):
    log = GenerationLog(
        id="test_log_feedback_once",
        prompt="feedback_once",
        user_id="test_user_generate",
        mode="aigc_text_to_skin",
        is_public=True
    )
    db.add(log)
    db.commit()

    response = client.post(f"/skin/api/logs/{log.id}/feedback", json={"is_good": True})
    assert response.status_code == 200
    response = client.post(f"/skin/api/logs/{log.id}/feedback", json={"is_good": False})
    assert response.status_code == 200
    assert response.json()["already_submitted"] is True
    assert db.query(UserFeedback).filter(
        UserFeedback.user_id == "test_user_generate",
        UserFeedback.log_id == log.id,
    ).count() == 1


def test_generate_validation_fail_invalid_model_version(client):
    payload = {
        "prompt": "cute girl with hoodie",
        "is_public": True,
        "aux_model_version": "z_image",
        "model_version": "invalid_model_version_name",
        "mode": "aigc_text_to_skin"
    }
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 400
    assert "Invalid model version" in response.json()["detail"]


def test_generate_missing_model_version(client):
    payload = {
        "prompt": "cute girl with hoodie",
        "is_public": True,
        "mode": "aigc_text_to_skin"
    }
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 422

@patch("routers.generate.backend_utils.is_text_to_skin_enabled", return_value=False)
def test_generate_text_to_skin_maintenance_block(mock_is_enabled, client, db):
    payload = {
        "prompt": "cute girl with hoodie",
        "is_public": True,
        "mode": "aigc_text_to_skin",
        "aux_model_version": "z_image",
        "model_version": "sking_v73_flux_4b_000027000"
    }
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 403
    assert "Text to skin generation is temporarily under maintenance." in response.json()["detail"]


@patch("routers.generate.backend_utils.is_image_to_skin_enabled", return_value=False)
def test_generate_image_to_skin_maintenance_block(mock_is_enabled, client, db):
    # Create a dummy 768x768 image
    img = Image.new('RGB', (768, 768), color='red')
    img_io = io.BytesIO()
    img.save(img_io, format='PNG')
    img_data = img_io.getvalue()

    payload = {
        "prompt": "image task",
        "mode": "aigc_image_to_skin",
        "model_version": "sking_v73_flux_4b_000027000"
    }
    response = client.post(
        "/skin/api/generate",
        data=payload,
        files={"file": ("test.png", img_data, "image/png")}
    )
    assert response.status_code == 403
    assert "Image to skin generation is temporarily under maintenance." in response.json()["detail"]


@patch("routers.generate.backend_utils.is_image_edit_to_skin_enabled", return_value=False)
def test_generate_image_edit_to_skin_maintenance_block(mock_is_enabled, client, db):
    # Create a dummy 768x768 image
    img = Image.new('RGB', (768, 768), color='red')
    img_io = io.BytesIO()
    img.save(img_io, format='PNG')
    img_data = img_io.getvalue()

    payload = {
        "prompt": "edit task",
        "mode": "aigc_image_edit_to_skin",
        "aux_model_version": "flux_4b",
        "model_version": "sking_v73_flux_4b_000027000"
    }
    response = client.post(
        "/skin/api/generate",
        data=payload,
        files={"file": ("test.png", img_data, "image/png")}
    )
    assert response.status_code == 403
    assert "Image edit to skin generation is temporarily under maintenance." in response.json()["detail"]
