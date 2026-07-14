import pytest
from models import User
from auth import get_current_user, get_current_user_optional

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
        minecraft_skin_url=None
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user

    # Invalid payload should fail
    response = client.post("/skin/api/users/me/minecraft_skin", json={"minecraft_skin_url": ""})
    assert response.status_code == 422

    # Valid payload should succeed
    new_skin_url = "https://s3.amazonaws.com/mybucket/skins/123.png"
    response = client.post("/skin/api/users/me/minecraft_skin", json={"minecraft_skin_url": new_skin_url})
    assert response.status_code == 200
    data = response.json()
    assert data["minecraft_skin_url"] == new_skin_url

    db.refresh(user)
    assert user.minecraft_skin_url == new_skin_url

    # Null payload should reset the character
    response = client.post("/skin/api/users/me/minecraft_skin", json={"minecraft_skin_url": None})
    assert response.status_code == 200
    data = response.json()
    assert data["minecraft_skin_url"] is None

    db.refresh(user)
    assert user.minecraft_skin_url is None

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
