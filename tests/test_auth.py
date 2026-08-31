import hashlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from config import settings
from models import AuthSession, GenerationLog, User
from auth import get_current_user, get_current_user_optional


def test_user_skin_columns_use_domain_names():
    columns = set(User.__table__.columns.keys())
    assert {"skin_url", "skin_type"} <= columns
    assert "minecraft_skin_url" not in columns
    assert "minecraft_skin_model" not in columns


def test_get_users_me_unauthorized(client):
    response = client.get("/skin/api/users/me")
    assert response.status_code in [401, 403]

def test_get_users_me_authorized(client, db):
    user = User(
        id="1",
        email="test@example.com",
        username="Test User",
        picture="http://example.com/pic.jpg"
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user

    response = client.get("/skin/api/users/me", headers={"Authorization": "Bearer fake_token"})
    
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["username"] == "Test User"

    app.dependency_overrides.clear()

def test_agree_terms(client, db):
    user = User(
        id="2",
        email="test2@example.com",
        username="Test User 2",
        terms_agreed=False
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user

    response = client.post("/skin/api/users/agree_terms", headers={"Authorization": "Bearer fake_token"})
    
    assert response.status_code == 200
    data = response.json()
    # According to UserResponse structure, check terms_agreed
    assert data["terms_agreed"] is True

    db.refresh(user)
    assert user.terms_agreed is True

    app.dependency_overrides.clear()

def test_update_username(client, db):
    user = User(
        id="3",
        email="test3@example.com",
        username="Old Name"
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user

    # Invalid payloads should fail
    response = client.post("/skin/api/users/me/username", json={"username": ""})
    assert response.status_code == 422

    # Valid payload should succeed
    response = client.post("/skin/api/users/me/username", json={"username": "New Cool Name"})
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "New Cool Name"

    db.refresh(user)
    assert user.username == "New Cool Name"

    app.dependency_overrides.clear()

def test_update_minecraft_skin(client, db):
    user = User(
        id="4",
        email="test4@example.com",
        username="Test User 4",
        skin_url=None
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user

    # Invalid payload should fail
    response = client.post("/skin/api/users/me/minecraft_skin", json={"skin_url": ""})
    assert response.status_code == 422

    # Valid payload should succeed with slim model
    new_skin_url = "https://s3.amazonaws.com/mybucket/skins/123.png"
    skin_log = GenerationLog(
        id="authskinlog123",
        prompt="owned skin",
        mode="text",
        result=new_skin_url,
        is_public=True,
        user_id=user.id,
        status="success",
    )
    db.add(skin_log)
    db.commit()
    response = client.post("/skin/api/users/me/minecraft_skin", json={"skin_url": new_skin_url, "skin_type": "slim"})
    assert response.status_code == 200
    data = response.json()
    assert data["skin_url"] == new_skin_url
    assert data["skin_type"] == "slim"
    assert "minecraft_skin_url" not in data
    assert "minecraft_skin_model" not in data

    db.refresh(user)
    assert user.skin_url == new_skin_url
    assert user.skin_type == "slim"

    legacy = client.post(
        "/skin/api/users/me/minecraft_skin",
        json={"minecraft_skin_url": new_skin_url, "minecraft_skin_model": "slim"},
    )
    assert legacy.status_code == 422

    response = client.post(
        "/skin/api/users/me/minecraft_skin",
        json={"skin_url": "https://example.com/missing-skin.png"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Skin not found"

    # Valid payload should succeed with strong model
    response = client.post("/skin/api/users/me/minecraft_skin", json={"skin_url": new_skin_url, "skin_type": "strong"})
    assert response.status_code == 200
    data = response.json()
    assert data["skin_type"] == "strong"

    db.refresh(user)
    assert user.skin_type == "strong"

    # Null payload should reset the character
    response = client.post("/skin/api/users/me/minecraft_skin", json={"skin_url": None})
    assert response.status_code == 200
    data = response.json()
    assert data["skin_url"] is None

    db.refresh(user)
    assert user.skin_url is None

    app.dependency_overrides.clear()


def test_google_login_keeps_username(client, db):
    # Create an existing user with a custom nickname
    user = User(
        id="google_test_user_id",
        email="test_google@example.com",
        username="My Custom Nickname",
        picture="http://example.com/old.jpg",
        google_id="12345"
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Mock auth.verify_google_token to return user info with the same email
    import auth as auth_module
    original_verify = auth_module.verify_google_token
    auth_module.verify_google_token = lambda token: {
        "email": "test_google@example.com",
        "sub": "12345",
        "email_verified": True,
        "name": "Google Name Overwrite Attempt",
        "picture": "http://example.com/new.jpg"
    }

    try:
        response = client.post("/skin/api/auth/google", json={"token": "mock_token"})
        assert response.status_code == 200
        data = response.json()
        # The username should remain "My Custom Nickname", NOT be overwritten by "Google Name Overwrite Attempt"
        assert data["user"]["username"] == "My Custom Nickname"
        assert data["user"]["picture"] == "http://example.com/new.jpg"  # picture can be updated

        # Verify in DB
        db.refresh(user)
        assert user.username == "My Custom Nickname"
    finally:
        # Restore mock
        auth_module.verify_google_token = original_verify


def test_google_login_creates_secure_server_session(client, db, monkeypatch):
    import auth as auth_module

    monkeypatch.setattr(auth_module, "verify_google_token", lambda token: {
        "email": "sliding-session@example.com",
        "sub": "google-sliding-session",
        "email_verified": True,
        "name": "Sliding Session",
    })

    response = client.post(
        "/skin/api/auth/google",
        json={"token": "mock-token"},
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["expires_in_seconds"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    claims = jwt.decode(
        body["access_token"],
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )
    assert claims["type"] == "access"
    assert claims["sid"]
    assert claims["jti"]

    set_cookie = response.headers["set-cookie"]
    assert f"{settings.AUTH_SESSION_COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/skin/api/auth" in set_cookie

    raw_session = client.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)
    session_id, secret = raw_session.split(".", 1)
    session = db.query(AuthSession).filter(AuthSession.id == session_id).one()
    assert session.user_id == claims["sub"]
    assert session.token_hash == hashlib.sha256(secret.encode("utf-8")).digest()
    assert secret.encode("utf-8") not in session.token_hash


def test_refresh_slides_session_and_keeps_cookie_stable(client, db, monkeypatch):
    import auth as auth_module

    monkeypatch.setattr(auth_module, "verify_google_token", lambda token: {
        "email": "refresh-session@example.com",
        "sub": "google-refresh-session",
        "email_verified": True,
        "name": "Refresh Session",
    })
    login = client.post("/skin/api/auth/google", json={"token": "mock-token"})
    assert login.status_code == 200
    raw_session = client.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)
    session_id = raw_session.split(".", 1)[0]
    session = db.query(AuthSession).filter(AuthSession.id == session_id).one()
    session.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    old_expiry = session.expires_at
    db.commit()

    response = client.post(
        "/skin/api/auth/refresh",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 200
    assert client.cookies.get(settings.AUTH_SESSION_COOKIE_NAME) == raw_session
    db.refresh(session)
    refreshed_expiry = session.expires_at
    if refreshed_expiry.tzinfo is None:
        refreshed_expiry = refreshed_expiry.replace(tzinfo=timezone.utc)
    if old_expiry.tzinfo is None:
        old_expiry = old_expiry.replace(tzinfo=timezone.utc)
    assert refreshed_expiry > old_expiry
    assert refreshed_expiry <= datetime.now(timezone.utc) + timedelta(
        days=settings.AUTH_SESSION_IDLE_DAYS,
        seconds=5,
    )
    claims = jwt.decode(
        response.json()["access_token"],
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )
    assert claims["sid"] == session_id
    assert claims["sub"] == session.user_id


def test_logout_revokes_session_and_refresh_cannot_reuse_it(client, db, monkeypatch):
    import auth as auth_module

    monkeypatch.setattr(auth_module, "verify_google_token", lambda token: {
        "email": "logout-session@example.com",
        "sub": "google-logout-session",
        "email_verified": True,
        "name": "Logout Session",
    })
    login = client.post("/skin/api/auth/google", json={"token": "mock-token"})
    assert login.status_code == 200
    access_token = login.json()["access_token"]
    raw_session = client.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)
    session_id = raw_session.split(".", 1)[0]

    before_logout = client.get(
        "/skin/api/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert before_logout.status_code == 200

    response = client.post(
        "/skin/api/auth/logout",
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200
    assert client.cookies.get(settings.AUTH_SESSION_COOKIE_NAME) is None
    session = db.query(AuthSession).filter(AuthSession.id == session_id).one()
    assert session.revoked_at is not None

    after_logout = client.get(
        "/skin/api/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert after_logout.status_code == 401

    client.cookies.set(settings.AUTH_SESSION_COOKIE_NAME, raw_session, path="/skin/api/auth")
    refresh = client.post("/skin/api/auth/refresh")
    assert refresh.status_code == 401
    assert refresh.json()["detail"]["code"] == "SESSION_EXPIRED"
    assert "Max-Age=0" in refresh.headers["set-cookie"]


def test_refresh_rejects_tampered_cookie_and_cross_origin(client, db, monkeypatch):
    import auth as auth_module

    monkeypatch.setattr(auth_module, "verify_google_token", lambda token: {
        "email": "session-security@example.com",
        "sub": "google-session-security",
        "email_verified": True,
        "name": "Session Security",
    })
    login = client.post("/skin/api/auth/google", json={"token": "mock-token"})
    assert login.status_code == 200
    raw_session = client.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)

    rejected_origin = client.post(
        "/skin/api/auth/refresh",
        headers={"Origin": "https://attacker.example"},
    )
    assert rejected_origin.status_code == 403
    assert rejected_origin.json()["detail"]["code"] == "SESSION_ORIGIN_REJECTED"

    session_id = raw_session.split(".", 1)[0]
    client.cookies.clear()
    client.cookies.set(
        settings.AUTH_SESSION_COOKIE_NAME,
        f"{session_id}.{'x' * 43}",
        path="/skin/api/auth",
    )
    tampered = client.post("/skin/api/auth/refresh")
    assert tampered.status_code == 401
    assert tampered.json()["detail"]["code"] == "SESSION_INVALID"


def test_google_login_rejects_unverified_email(client, monkeypatch):
    import auth as auth_module

    monkeypatch.setattr(auth_module, "verify_google_token", lambda token: {
        "email": "unverified@example.com",
        "sub": "google-sub-unverified",
        "email_verified": False,
        "name": "Unverified User",
    })

    response = client.post("/skin/api/auth/google", json={"token": "mock_token"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Google email is not verified"


def test_google_login_rejects_google_id_mismatch(client, db, monkeypatch):
    user = User(
        id="google_mismatch_user_id",
        email="mismatch@example.com",
        username="Mismatch User",
        google_id="original-google-sub"
    )
    db.add(user)
    db.commit()

    import auth as auth_module

    monkeypatch.setattr(auth_module, "verify_google_token", lambda token: {
        "email": "mismatch@example.com",
        "sub": "different-google-sub",
        "email_verified": True,
        "name": "Attacker",
    })

    response = client.post("/skin/api/auth/google", json={"token": "mock_token"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Google account does not match this user"

    db.refresh(user)
    assert user.google_id == "original-google-sub"

def test_get_my_credit_history(client, db):
    user = User(
        id="credit_test_user",
        email="credit@example.com",
        username="Credit User",
        credits=50
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Add some mock CreditLogs
    from models import CreditLog
    log1 = CreditLog(user_id=user.id, amount=6, action="daily_login", source="Daily Login Reward")
    log2 = CreditLog(user_id=user.id, amount=-1, action="generation", source="Skin Generation: abc")
    db.add_all([log1, log2])
    db.commit()

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user

    response = client.get("/skin/api/users/me/credits/history?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["amount"] == -1
    assert data["items"][0]["action"] == "generation"
    assert data["items"][1]["amount"] == 6
    assert data["items"][1]["action"] == "daily_login"

    app.dependency_overrides.clear()


def test_update_minecraft_skin_rejects_another_creators_skin(client, db):
    from models import GenerationLog, User
    from main import app

    current_user = User(
        id="skin_current_user",
        email="skin_current@example.com",
        username="Current User",
    )
    other_user = User(
        id="skin_other_user",
        email="skin_other@example.com",
        username="Other Skin Creator",
    )
    other_pub_log = GenerationLog(
        id="otherpublog123",
        prompt="another user's public skin",
        mode="text",
        result="generations/other_public_skin.png",
        is_public=True,
        user_id=other_user.id,
        status="success"
    )
    db.add_all([current_user, other_user, other_pub_log])
    db.commit()

    def mock_get_current_user():
        return current_user

    app.dependency_overrides[get_current_user] = mock_get_current_user
    try:
        response = client.post(
            "/skin/api/users/me/minecraft_skin",
            json={"skin_url": "generations/other_public_skin.png"},
        )
        assert response.status_code == 403
        assert "Only the skin creator" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_update_minecraft_skin_restrictions(client, db):
    from datetime import datetime, timezone, timedelta
    from models import GenerationLog, User
    from main import app
    
    # 1. Create a user who is pro-active so they can call make_private
    user = User(
        id="test_restrict_user",
        email="test_restrict@example.com",
        username="Test Restrict User",
        skin_url=None,
        pro_expires_at=datetime.now(timezone.utc) + timedelta(days=10),
        pro_level="pro-plus"
    )
    db.add(user)
    db.commit()

    # 2. Mock logged-in user
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user

    # 3. Create a public log and a private log
    pub_log = GenerationLog(
        id="publog123",
        prompt="public skin prompt",
        mode="text",
        result="generations/public_skin.png",
        is_public=True,
        user_id=user.id,
        status="success"
    )
    priv_log = GenerationLog(
        id="privlog123",
        prompt="private skin prompt",
        mode="text",
        result="generations/private_skin.png",
        is_public=False,
        user_id=user.id,
        status="success"
    )
    db.add(pub_log)
    db.add(priv_log)
    db.commit()

    # Try setting private skin -> should fail (400)
    response = client.post("/skin/api/users/me/minecraft_skin", json={"skin_url": "generations/private_skin.png"})
    assert response.status_code == 400
    assert "Cannot set a private skin" in response.json()["detail"]

    # Try setting public skin -> should succeed
    response = client.post("/skin/api/users/me/minecraft_skin", json={"skin_url": "generations/public_skin.png"})
    assert response.status_code == 200
    assert response.json()["skin_url"] == "generations/public_skin.png"

    # Make the public skin private -> should clear the character
    import s3_utils
    original_copy_object = s3_utils.s3_client.copy_object
    original_delete_object = s3_utils.s3_client.delete_object
    s3_utils.s3_client.copy_object = lambda **kwargs: {}
    s3_utils.s3_client.delete_object = lambda **kwargs: {}

    try:
        response = client.post("/skin/api/logs/publog123/make_private")
        assert response.status_code == 200
        
        # Verify user character is cleared in DB
        db.refresh(user)
        assert user.skin_url is None
    finally:
        s3_utils.s3_client.copy_object = original_copy_object
        s3_utils.s3_client.delete_object = original_delete_object

    # Set user character back to public skin (we temporarily make pub_log public again)
    pub_log.is_public = True
    db.commit()
    response = client.post("/skin/api/users/me/minecraft_skin", json={"skin_url": "generations/public_skin.png"})
    assert response.status_code == 200
    assert response.json()["skin_url"] == "generations/public_skin.png"

    # Soft-delete the log -> should clear the character
    from unittest.mock import patch
    with patch("routers.generate.delete_from_s3"):
        response = client.delete("/skin/api/logs/publog123")
    assert response.status_code == 200
    db.refresh(user)
    assert user.skin_url is None

    app.dependency_overrides.clear()
