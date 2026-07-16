import datetime
import pytest
from unittest.mock import MagicMock, patch
from main import app
from auth import get_current_admin
import models


def test_monitor_stats_includes_seven_day_active_users(client, db):
    admin_user = models.User(
        id="ADMINSTATS000001",
        email="admin-stats@entropydrop.com",
        username="StatsAdmin",
    )
    db.add(admin_user)

    now = datetime.datetime.now(datetime.timezone.utc)
    today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    login_logs = [
        # Duplicate logs for one user must count as one active user.
        models.CreditLog(user_id="ACTIVEUSER000001", amount=1, action="daily_login", created_at=today_noon),
        models.CreditLog(user_id="ACTIVEUSER000001", amount=1, action="daily_login", created_at=today_noon),
        # Two distinct users logged in yesterday.
        models.CreditLog(user_id="ACTIVEUSER000001", amount=1, action="daily_login", created_at=today_noon - datetime.timedelta(days=1)),
        models.CreditLog(user_id="ACTIVEUSER000002", amount=1, action="daily_login", created_at=today_noon - datetime.timedelta(days=1)),
        # Other credit actions are not logins.
        models.CreditLog(user_id="ACTIVEUSER000003", amount=20, action="monthly_login", created_at=today_noon - datetime.timedelta(days=1)),
        # Logins outside the seven-day window are excluded.
        models.CreditLog(user_id="ACTIVEUSER000004", amount=1, action="daily_login", created_at=today_noon - datetime.timedelta(days=7)),
    ]
    db.add_all(login_logs)
    db.commit()

    app.dependency_overrides[get_current_admin] = lambda: admin_user

    queue = MagicMock()
    queue.count = 0
    queue.started_job_registry.count = 0
    queue.deferred_job_registry.count = 0
    queue.finished_job_registry.count = 0
    queue.failed_job_registry.count = 0
    queue.scheduled_job_registry.count = 0

    with patch("routers.monitor.Queue", return_value=queue), \
         patch("routers.monitor.Worker.all", return_value=[]):
        response = client.get("/skin/api/monitor/stats")

    assert response.status_code == 200
    history = response.json()["history"]
    assert len(history) == 7
    assert [day["active_users"] for day in history[-2:]] == [2, 1]
    assert all(day["active_users"] == 0 for day in history[:-2])

def test_unfinished_logs_non_admin(client):
    # If get_current_admin is not overridden, it will try to decode token and fail or raise 403/401
    response = client.get("/skin/api/monitor/unfinished")
    assert response.status_code in (401, 403)

def test_unfinished_logs_admin_success(client, db):
    # 1. Create a mock admin user
    admin_user = models.User(
        id="ADMIN00000000001",
        email="admin@entropydrop.com",
        username="AdminUser",
    )
    db.add(admin_user)
    
    # 2. Create normal users to associate with generation logs
    user1 = models.User(
        id="USER000000000001",
        email="john.doe@example.com",
        username="JohnDoe",
    )
    user2 = models.User(
        id="USER000000000002",
        email="an@entropydrop.com",
        username="An",
    )
    db.add(user1)
    db.add(user2)
    db.commit()

    # 3. Create mock generation logs (some finished, some unfinished with different creation dates)
    now = datetime.datetime.now(datetime.timezone.utc)
    
    # Unfinished logs
    log_pending = models.GenerationLog(
        id="LOGPENDING000001",
        prompt="A beautiful sunset skin",
        mode="aigc_text_to_image",
        status="pending",
        user_id="USER000000000001",
        created_at=now - datetime.timedelta(minutes=10)
    )
    log_processing = models.GenerationLog(
        id="LOGPROC000000001",
        prompt="Red neon style",
        mode="aigc_image_to_skin",
        status="processing_skin",
        user_id="USER000000000002",
        created_at=now - datetime.timedelta(minutes=5)
    )
    
    # Finished logs
    log_success = models.GenerationLog(
        id="LOGSUCCESS000001",
        prompt="Success skin",
        mode="aigc_text_to_image",
        status="success",
        user_id="USER000000000001",
        created_at=now - datetime.timedelta(minutes=2)
    )
    log_failed = models.GenerationLog(
        id="LOGFAILED0000001",
        prompt="Failed skin",
        mode="aigc_text_to_image",
        status="failed",
        user_id="USER000000000001",
        created_at=now - datetime.timedelta(minutes=1)
    )

    db.add(log_pending)
    db.add(log_processing)
    db.add(log_success)
    db.add(log_failed)
    db.commit()

    # 4. Mock admin check to return the admin user
    def mock_get_current_admin():
        return admin_user
    
    app.dependency_overrides[get_current_admin] = mock_get_current_admin

    # 5. Fetch the unfinished logs
    response = client.get("/skin/api/monitor/unfinished?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()

    # 6. Verify unfinished/failed statuses are present, but successful are not
    assert "items" in data
    assert data["total_count"] == 3
    assert len(data["items"]) == 3
    
    # Verify sorting: DESC by created_at (so log_failed is first, log_processing is second, log_pending is third)
    items = data["items"]
    assert items[0]["id"] == "LOGFAILED0000001"
    assert items[1]["id"] == "LOGPROC000000001"
    assert items[2]["id"] == "LOGPENDING000001"

    # Verify status fields
    assert items[0]["status"] == "failed"
    assert items[1]["status"] == "processing_skin"
    assert items[2]["status"] == "pending"

    # Verify data masking for the first item (USER000000000001 / john.doe@example.com / JohnDoe)
    assert items[0]["user_email"] == "j******e@example.com"
    assert items[0]["user_username"] == "J*****e"
    assert items[0]["user_id"] == "USER***"

    # Verify data masking for the second item (USER000000000002 / an@entropydrop.com / An)
    assert items[1]["user_email"] == "a*@entropydrop.com"
    assert items[1]["user_username"] == "A*"
    assert items[1]["user_id"] == "USER***"

    # 7. Verify pagination boundaries
    response_p2 = client.get("/skin/api/monitor/unfinished?page=2&page_size=1")
    assert response_p2.status_code == 200
    data_p2 = response_p2.json()
    assert len(data_p2["items"]) == 1
    assert data_p2["items"][0]["id"] == "LOGPROC000000001"
    assert data_p2["page"] == 2
    assert data_p2["total_pages"] == 3

    # Clean up overrides
    app.dependency_overrides.clear()


@patch("routers.generate.BackgroundTasks.add_task")
def test_delete_log_admin_success(mock_add_task, client, db):
    # 1. Create mock admin user
    admin_user = models.User(
        id="ADMIN_DEL_000001",
        email="admin-del@entropydrop.com",
        username="AdminDel",
    )
    db.add(admin_user)
    
    # 2. Create target log
    log = models.GenerationLog(
        id="LOGDELADMIN00001",
        prompt="Delete me",
        user_id="USER000000000001",
        mode="aigc_text_to_image",
        status="pending",
        source="uploads/source.png",
        result="generations/result.png",
        is_public=True
    )
    db.add(log)
    db.commit()

    # 3. Create related entities
    col_item = models.CollectionItem(
        id="COLITEM000000001",
        collection_id="COL0000000000001",
        log_id=log.id
    )
    like = models.UserLike(
        id="LIKE0000000000001",
        user_id="USER000000000001",
        log_id=log.id
    )
    feedback = models.UserFeedback(
        id="FB000000000000001",
        log_id=log.id,
        is_good=True
    )
    db.add(col_item)
    db.add(like)
    db.add(feedback)
    db.commit()

    # 4. Verify relations exist
    assert db.query(models.CollectionItem).filter(models.CollectionItem.log_id == log.id).count() == 1
    assert db.query(models.UserLike).filter(models.UserLike.log_id == log.id).count() == 1
    assert db.query(models.UserFeedback).filter(models.UserFeedback.log_id == log.id).count() == 1

    # 5. Mock admin authorization
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin

    # 6. Execute delete request
    response = client.delete(f"/skin/api/monitor/logs/{log.id}")
    assert response.status_code == 200
    assert "soft-deleted" in response.json()["message"]

    # 7. Verify soft deletion & cleared attributes in DB
    db.refresh(log)
    assert log.is_deleted is True
    assert log.prompt is None
    assert log.name == "Deleted"
    assert log.status == "deleted"
    assert log.source is None
    assert log.result is None

    # 8. Verify S3 cleanup background task was scheduled
    mock_add_task.assert_called_once()
    args = mock_add_task.call_args[0]
    assert args[0].__name__ == "delete_s3_files_task"
    files_list = args[1]
    assert ("uploads/source.png", True) in files_list
    assert ("generations/result.png", True) in files_list

    # 9. Verify relations are deleted
    assert db.query(models.CollectionItem).filter(models.CollectionItem.log_id == log.id).count() == 0
    assert db.query(models.UserLike).filter(models.UserLike.log_id == log.id).count() == 0
    assert db.query(models.UserFeedback).filter(models.UserFeedback.log_id == log.id).count() == 0

    app.dependency_overrides.clear()


def test_delete_log_non_admin_rejects(client, db):
    # No dependency overrides set for get_current_admin, should reject
    response = client.delete("/skin/api/monitor/logs/LOGDELADMIN00001")
    assert response.status_code in (401, 403)


def test_daily_free_credits_endpoints(client, db):
    admin_user = models.User(
        id="ADMIN_FREE_CREDITS_001",
        email="admin-free-credits@entropydrop.com",
        username="AdminFreeCredits",
    )
    db.add(admin_user)
    db.commit()

    # 1. Non-admin get should be rejected
    response = client.get("/skin/api/monitor/daily_free_credits")
    assert response.status_code in (401, 403)

    # 2. Non-admin post should be rejected
    response = client.post("/skin/api/monitor/daily_free_credits", json={"credits": 10})
    assert response.status_code in (401, 403)

    # 3. Admin authentication override
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin

    # 4. Admin GET (should default to 1)
    response = client.get("/skin/api/monitor/daily_free_credits")
    assert response.status_code == 200
    assert response.json() == {"credits": 1}

    # 5. Admin POST update to 12
    response = client.post("/skin/api/monitor/daily_free_credits", json={"credits": 12})
    assert response.status_code == 200
    assert response.json() == {"credits": 12}

    # 6. Admin GET (should now be 12)
    response = client.get("/skin/api/monitor/daily_free_credits")
    assert response.status_code == 200
    assert response.json() == {"credits": 12}

    # 7. Admin POST negative credits should be rejected
    response = client.post("/skin/api/monitor/daily_free_credits", json={"credits": -1})
    assert response.status_code == 400

    app.dependency_overrides.clear()


@patch("routers.generate.BackgroundTasks.add_task")
def test_delete_user_by_email_admin_success(mock_add_task, client, db):
    # 1. Create admin user
    admin_user = models.User(
        id="ADMIN_U_DEL_001",
        email="admin-user-del@entropydrop.com",
        username="AdminUserDel",
    )
    db.add(admin_user)
    
    # 2. Create target user to delete
    target_user = models.User(
        id="USER_TO_DELETE01",
        email="target@example.com",
        username="TargetUser",
    )
    db.add(target_user)
    db.commit()

    # 3. Create mock entities belonging to target user
    log = models.GenerationLog(
        id="LOG_T_DEL_000001",
        user_id=target_user.id,
        mode="aigc_text_to_image",
        status="success",
        source="uploads/target_src.png",
        result="generations/target_res.png",
        is_public=True
    )
    collection = models.Collection(
        id="COL_T_DEL_000001",
        name="Target Collection",
        user_id=target_user.id
    )
    db.add(log)
    db.add(collection)
    db.commit()

    col_item = models.CollectionItem(
        id="CI_T_DEL_00000001",
        collection_id=collection.id,
        log_id=log.id
    )
    shipping = models.ShippingAddress(
        id="SA_T_DEL_00000001",
        user_id=target_user.id,
        country="US",
        phone="123456",
        zip_code="10001",
        state="NY",
        city="NYC",
        detail_address="123 St"
    )
    order = models.Order(
        id="ORD_T_DEL_0000001",
        user_id=target_user.id,
        price=10.0,
        total_price=10.0
    )
    db.add(col_item)
    db.add(shipping)
    db.add(order)
    db.commit()

    order_item = models.OrderItem(
        id="OI_T_DEL_00000001",
        order_id=order.id,
        price=10.0
    )
    db.add(order_item)
    db.commit()

    # 4. Mock admin authentication
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin

    target_user_id = target_user.id
    col_item_id = col_item.id
    order_item_id = order_item.id

    # 5. Execute DELETE request
    response = client.delete(f"/skin/api/monitor/users/by-email?email=target@example.com")
    assert response.status_code == 200
    assert "permanently deleted" in response.json()["message"]

    # 6. Verify target user is deleted
    assert db.query(models.User).filter(models.User.id == target_user_id).count() == 0

    # 7. Verify cascade deletions
    assert db.query(models.GenerationLog).filter(models.GenerationLog.user_id == target_user_id).count() == 0
    assert db.query(models.Collection).filter(models.Collection.user_id == target_user_id).count() == 0
    assert db.query(models.CollectionItem).filter(models.CollectionItem.id == col_item_id).count() == 0
    assert db.query(models.ShippingAddress).filter(models.ShippingAddress.user_id == target_user_id).count() == 0
    assert db.query(models.Order).filter(models.Order.user_id == target_user_id).count() == 0
    assert db.query(models.OrderItem).filter(models.OrderItem.id == order_item_id).count() == 0

    # 8. Verify S3 cleanup task was called
    mock_add_task.assert_called_once()
    
    app.dependency_overrides.clear()


def test_delete_user_by_email_not_found(client, db):
    admin_user = models.User(
        id="ADMIN_U_DEL_002",
        email="admin-user-del2@entropydrop.com",
        username="AdminUserDel2",
    )
    db.add(admin_user)
    db.commit()

    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin

    response = client.delete("/skin/api/monitor/users/by-email?email=nonexistent@example.com")
    assert response.status_code == 404
    assert "User not found" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_delete_user_by_email_non_admin_rejects(client, db):
    response = client.delete("/skin/api/monitor/users/by-email?email=target@example.com")
    assert response.status_code in (401, 403)


def test_mode_status_endpoints(client, db):
    admin_user = models.User(
        id="ADMIN_MODE_STATUS_001",
        email="admin-mode-status@entropydrop.com",
        username="AdminModeStatus",
    )
    db.add(admin_user)
    db.commit()

    # 1. Non-admin GET should be rejected
    response = client.get("/skin/api/monitor/mode_status")
    assert response.status_code in (401, 403)

    # 2. Non-admin POST should be rejected
    response = client.post("/skin/api/monitor/mode_status/text_to_skin", json={"enabled": False})
    assert response.status_code in (401, 403)

    # 3. Admin authentication override
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin

    # 4. Admin GET (should default to True for all 3)
    response = client.get("/skin/api/monitor/mode_status")
    assert response.status_code == 200
    assert response.json() == {
        "text_to_skin_enabled": True,
        "image_to_skin_enabled": True,
        "image_edit_to_skin_enabled": True,
    }

    # 5. Admin POST turn OFF image_to_skin
    response = client.post("/skin/api/monitor/mode_status/image_to_skin", json={"enabled": False})
    assert response.status_code == 200
    assert response.json() == {"mode": "image_to_skin", "enabled": False}

    # 6. Admin GET (should reflect change)
    response = client.get("/skin/api/monitor/mode_status")
    assert response.status_code == 200
    assert response.json() == {
        "text_to_skin_enabled": True,
        "image_to_skin_enabled": False,
        "image_edit_to_skin_enabled": True,
    }

    # 7. Admin POST invalid mode name
    response = client.post("/skin/api/monitor/mode_status/invalid_mode", json={"enabled": False})
    assert response.status_code == 400

    app.dependency_overrides.clear()


def test_gift_credits_to_seven_day_active_users_non_admin_rejects(client):
    response = client.post("/skin/api/monitor/gift_active_users", json={"amount": 10, "message": "Test Gift"})
    assert response.status_code in (401, 403)


def test_gift_all_endpoint_is_removed(client):
    response = client.post("/skin/api/monitor/gift_all", json={"amount": 10, "message": "Test Gift"})
    assert response.status_code == 404


def test_gift_credits_to_seven_day_active_users_success(client, db):
    # 1. Create admin user
    admin_user = models.User(
        id="ADMIN_GIFT_001",
        email="admin-gift@entropydrop.com",
        username="AdminGift",
    )
    db.add(admin_user)

    # 2. Create target users
    user1 = models.User(
        id="GIFT_USER_001",
        email="user1@example.com",
        username="UserOne",
        credits=5
    )
    user2 = models.User(
        id="GIFT_USER_002",
        email="user2@example.com",
        username="UserTwo",
        credits=10
    )
    db.add(user1)
    db.add(user2)
    now = datetime.datetime.now(datetime.timezone.utc)
    db.add(models.CreditLog(
        user_id=user1.id,
        amount=1,
        action="daily_login",
        created_at=now - datetime.timedelta(days=2),
    ))
    db.add(models.CreditLog(
        user_id=user2.id,
        amount=1,
        action="daily_login",
        created_at=now - datetime.timedelta(days=8),
    ))
    db.commit()

    # 3. Mock admin override
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin

    # 4. Attempt validation errors
    # Negative amount
    response = client.post("/skin/api/monitor/gift_active_users", json={"amount": -5, "message": "Test"})
    assert response.status_code == 400

    # Empty message
    response = client.post("/skin/api/monitor/gift_active_users", json={"amount": 15, "message": "   "})
    assert response.status_code == 400

    # 5. Execute successful gift request
    response = client.post("/skin/api/monitor/gift_active_users", json={"amount": 15, "message": "Maintenance Gift"})
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["gifted_users"] == 1

    # 6. Verify credits updated in database
    db.refresh(user1)
    db.refresh(user2)
    assert user1.credits == 20
    assert user2.credits == 10

    # 7. Verify only the seven-day active user gets a credit log.
    logs = db.query(models.CreditLog).filter(models.CreditLog.action == "system_gift").all()
    assert len(logs) == 1
    assert {l.user_id for l in logs} == {"GIFT_USER_001"}
    assert all(l.amount == 15 for l in logs)
    assert all(l.source == "Maintenance Gift" for l in logs)

    # 8. Verify mailbox notifications created
    notifs = db.query(models.ForumNotification).filter(models.ForumNotification.type == "system_gift").all()
    assert len(notifs) == 1
    assert {n.user_id for n in notifs} == {"GIFT_USER_001"}
    assert all(n.comment_id == "15" for n in notifs)
    assert all(n.sender_id is None for n in notifs)

    # Clean up
    app.dependency_overrides.clear()
