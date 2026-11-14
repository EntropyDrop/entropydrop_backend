import pytest
from models import User, CreditLog, ForumNotification
from auth import get_current_user, get_current_user_optional

@pytest.fixture(autouse=True)
def mock_auth(db):
    user = User(
        id="user_task_test",
        email="task_user@example.com",
        username="Task User",
        credits=50
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_user_optional] = lambda: user
    yield user
    app.dependency_overrides.clear()

def test_get_youtube_task_status_initially_unclaimed(client):
    response = client.get("/skin/api/credits/tasks/youtube-subscription")
    assert response.status_code == 200
    data = response.json()
    assert data["claimed"] is False
    assert data["reward_credits"] == 5
    assert "youtube.com" in data["channel_url"]

def test_claim_youtube_subscription_success(client, db, mock_auth):
    response = client.post("/skin/api/credits/tasks/youtube-subscription/claim")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["credits_awarded"] == 5
    assert data["new_balance"] == 55

    # Verify user credit updated
    db.refresh(mock_auth)
    assert mock_auth.credits == 55

    # Verify CreditLog created with unique idempotency_key
    log = db.query(CreditLog).filter(CreditLog.user_id == "user_task_test").first()
    assert log is not None
    assert log.amount == 5
    assert log.action == "system_gift"
    assert log.source == "YouTube: @EntropyDrop"
    assert log.idempotency_key == "reward:youtube_subscribe:user_task_test"

    # Verify ForumNotification created
    notif = db.query(ForumNotification).filter(ForumNotification.user_id == "user_task_test").first()
    assert notif is not None
    assert notif.type == "system_gift"
    assert notif.post_id == log.id
    assert notif.comment_id == "5"
    assert "youtube.com" in notif.action_url

    # Status check should now report claimed=True
    status_res = client.get("/skin/api/credits/tasks/youtube-subscription")
    assert status_res.status_code == 200
    assert status_res.json()["claimed"] is True

def test_claim_youtube_subscription_duplicate_rejected(client):
    # First claim
    res1 = client.post("/skin/api/credits/tasks/youtube-subscription/claim")
    assert res1.status_code == 200

    # Second claim should be rejected
    res2 = client.post("/skin/api/credits/tasks/youtube-subscription/claim")
    assert res2.status_code == 400
    assert "already been claimed" in res2.json()["detail"]
