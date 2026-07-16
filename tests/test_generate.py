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
    assert "aigc_image_to_skin" in data
    assert "sking_v39_flux_4b_000028000" in data["aigc_image_to_skin"]


@patch("routers.generate.backend_utils.get_generation_credit_cost", return_value=5)
def test_get_generation_credit_cost(mock_credit_cost, client):
    response = client.get("/skin/api/generation_credit_cost")
    assert response.status_code == 200
    assert response.json() == {"credits": 5}
    mock_credit_cost.assert_called_once()

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

@patch("rq.Queue.enqueue")
@patch("routers.generate.backend_utils.get_generation_credit_cost", return_value=1)
def test_submit_generate_text_to_skin(mock_credit_cost, mock_enqueue, client, db):
    user = db.query(User).filter(User.id == "test_user_generate").one()
    user.credits = 1
    db.commit()

    payload = {
        "prompt": "cute girl with hoodie",
        "is_public": True,
        "model_version": "z_image + sking_v39_flux_4b_000028000",
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
        "model_version": "z_image + sking_v39_flux_4b_000028000",
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

@patch("routers.generate.BackgroundTasks.add_task")
def test_delete_log(mock_add_task, client, db):
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
    payload = {"prompt": "test", "guidance": 20.0}  # Too large
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 400
    assert "Guidance must be between" in response.json()["detail"]

def test_generate_validation_fail_n_step(client):
    payload = {"prompt": "test", "n_step": 10}  # Too small
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 400
    assert "n_step must be between" in response.json()["detail"]

def test_generate_private_non_pro(client, db):
    # Change status to non-Pro user
    user = db.query(User).filter(User.id == "test_user_generate").first()
    user.pro_expires_at = None
    db.commit()

    payload = {"prompt": "test", "is_public": False}
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

    payload = {"prompt": "test"}
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

    response = client.post("/skin/api/generate", data={"prompt": "blocked"})
    assert response.status_code == 429
    assert "task(s) in the queue" in response.json()["detail"]
    mock_enqueue.assert_not_called()


def test_re_enqueue_if_missing_recovers_pending_skin(monkeypatch, db):
    user = User(id="recover_user", email="recover@example.com", username="Recover")
    log = GenerationLog(
        id="recover_log",
        prompt="recover",
        user_id=user.id,
        mode="aigc_text_to_skin",
        status="pending_skin",
        edited_result="edited/recover.jpg",
        model_version="z_image + sking_v73_flux_4b_000027000",
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
    assert kwargs["job_id"] == "generation_recover_log_image_to_skin"

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
    mock_s3_client.put_object.assert_called_once()

@patch("s3_utils.s3_client")
@patch("s3_utils.settings")
def test_upload_to_s3_private(mock_settings, mock_s3_client):
    mock_settings.AWS_BUCKET_NAME = "pub-bucket"
    mock_settings.AWS_PRIVATE_BUCKET_NAME = "priv-bucket"
    
    from s3_utils import upload_to_s3
    res = upload_to_s3(b"data", "key", is_public=False)
    assert res == "key"
    mock_s3_client.put_object.assert_called_once()

# test_process_generation_no_images_fail is removed as process_generation is no longer in routers/generate.py


# ----------------- Discovery Interface Sorting and Search Tests -----------------

def test_get_discovery_random(client, db):
    # Clear cache to ensure update_discovery_cache is triggered
    import routers.generate
    routers.generate.discovery_cache_items = []

    for i in range(3):
        log = GenerationLog(prompt=f"discover {i}", is_public=True, user_id="test_user_generate", mode="edit", result=f"res_{i}.png", status="success")
        db.add(log)
    db.commit()

    response = client.get("/skin/api/discovery")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 3
    assert "creator" in data[0]
    assert "likes_count" in data[0]


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
        "mode": "aigc_text_to_skin"
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
        "mode": "aigc_text_to_skin"
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
        "model_version": "invalid_model_version_name",
        "mode": "aigc_text_to_skin"
    }
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 400
    assert "Invalid model version" in response.json()["detail"]


@patch("rq.Queue.enqueue")
def test_generate_default_model_version(mock_enqueue, client, db):
    payload = {
        "prompt": "cute girl with hoodie",
        "is_public": True,
        "mode": "aigc_text_to_skin"
    }
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 200
    data = response.json()
    
    log = db.query(GenerationLog).filter(GenerationLog.id == data["id"]).first()
    assert log is not None
    assert log.model_version == "z_image + sking_v73_flux_4b_000027000"


@patch("routers.generate.backend_utils.is_text_to_skin_enabled", return_value=False)
def test_generate_text_to_skin_maintenance_block(mock_is_enabled, client, db):
    payload = {
        "prompt": "cute girl with hoodie",
        "is_public": True,
        "mode": "aigc_text_to_skin"
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
        "model_version": "flux_4b + sking_v73_flux_4b_000027000"
    }
    response = client.post(
        "/skin/api/generate",
        data=payload,
        files={"file": ("test.png", img_data, "image/png")}
    )
    assert response.status_code == 403
    assert "Image edit to skin generation is temporarily under maintenance." in response.json()["detail"]
