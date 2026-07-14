import pytest
from unittest.mock import patch
from models import User, GenerationLog
from auth import get_current_user, get_current_user_optional

pytestmark = pytest.mark.usefixtures("mock_auth", "mock_db_session")

@pytest.fixture()
def mock_auth(db):
    import datetime
    user = User(
        id="test_user_len",
        email="test_len@example.com",
        username="Tester",
        terms_agreed=True,
        pro_level="pro-plus",
        pro_expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365),
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
    app.dependency_overrides.clear()

@pytest.fixture()
def mock_db_session(db):
    with patch("routers.generate.SessionLocal", return_value=db):
        yield

def test_collection_name_length_limit(client):
    # Limit is 100
    long_name = "a" * 101
    response = client.post("/skin/api/collections", json={"name": long_name, "is_public": True})
    assert response.status_code == 422
    assert "at most 100 characters" in response.json()["detail"][0]["msg"]

def test_log_name_update_length_limit(client, db):
    log = GenerationLog(prompt="test", user_id="test_user_len", mode="edit")
    db.add(log)
    db.commit()
    db.refresh(log)

    long_name = "a" * 101
    response = client.patch(f"/skin/api/logs/{log.id}/name", json={"name": long_name})
    assert response.status_code == 422
    assert "at most 100 characters" in response.json()["detail"][0]["msg"]

def test_generate_prompt_length_limit(client):
    # Limit is 500
    long_prompt = "a" * 501
    payload = {"prompt": long_prompt}
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 422
    assert "at most 500 characters" in response.json()["detail"][0]["msg"]

@patch("rq.Queue.enqueue")
def test_generate_name_truncation(mock_enqueue, client, db):
    # Prompt is long (e.g. 200), name should be truncated to 100
    prompt = "a" * 200
    payload = {"prompt": prompt}
    response = client.post("/skin/api/generate", data=payload)
    assert response.status_code == 200
    
    log_id = response.json()["id"]
    log = db.query(GenerationLog).filter(GenerationLog.id == log_id).first()
    assert len(log.name) == 100
    assert log.name == prompt[:100]

def test_get_log_name_truncation(client, db):
    # Case where log.name is None but prompt is long
    prompt = "b" * 150
    log = GenerationLog(prompt=prompt, name=None, user_id="test_user_len", mode="edit", is_public=True)
    db.add(log)
    db.commit()
    db.refresh(log)

    response = client.get(f"/skin/api/logs/{log.id}")
    assert response.status_code == 200
    data = response.json()
    assert len(data["name"]) == 100
    assert data["name"] == prompt[:100]

def test_shipping_address_length_limits(client):
    payload = {
        "country": "a" * 101,
        "phone": "1" * 51,
        "zip_code": "z" * 21,
        "state": "s" * 101,
        "city": "c" * 101,
        "detail_address": "d" * 1001
    }
    response = client.post("/skin/api/addresses", json=payload)
    # router is /api/addresses
    assert response.status_code == 422
    errors = response.json()["detail"]
    fields_with_errors = [e["loc"][-1] for e in errors]
    assert "country" in fields_with_errors
    assert "phone" in fields_with_errors
    assert "zip_code" in fields_with_errors
    assert "state" in fields_with_errors
    assert "city" in fields_with_errors
    assert "detail_address" in fields_with_errors


def test_discovery_search_query_length_limit(client):
    # Under 3 characters (non-Chinese) should return 400
    response = client.get("/skin/api/discovery/search?q=ab")
    assert response.status_code == 400
    assert "Search query must be at least 3 character(s)" in response.json()["detail"]

    # 3 characters or more (non-Chinese) should pass validation (returns 200 or list result)
    response = client.get("/skin/api/discovery/search?q=abc")
    assert response.status_code == 200

    # Chinese queries of 1 or 2 characters should pass validation (returns 200 or list result)
    response = client.get("/skin/api/discovery/search?q=猫")
    assert response.status_code == 200

    response = client.get("/skin/api/discovery/search?q=末影")
    assert response.status_code == 200
