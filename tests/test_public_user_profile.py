import pytest

from auth import get_current_user
from main import app
from models import User


@pytest.fixture
def signed_in(db):
    viewer = User(id="viewer", email="viewer@example.test", username="Viewer", terms_agreed=True)
    owner = User(
        id="owner", email="private@example.test", username="Collection Owner",
        picture="https://cdn.example.test/avatar.png", skin_url="https://cdn.example.test/skin.png",
        google_id="private-google-id", credits=123, terms_agreed=True,
    )
    db.add_all([viewer, owner])
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: viewer
    yield owner
    app.dependency_overrides.pop(get_current_user, None)


def test_profile_returns_only_public_identity(client, signed_in):
    response = client.get("/api/users/owner/profile")
    assert response.status_code == 200
    assert response.json() == {
        "id": "owner", "username": "Collection Owner",
        "picture": "https://cdn.example.test/avatar.png",
        "skin_url": "https://cdn.example.test/skin.png",
    }


def test_profile_supports_users_without_creations_or_avatar(client, db, signed_in):
    signed_in.username = None
    signed_in.picture = None
    signed_in.skin_url = None
    db.commit()
    response = client.get("/api/users/owner/profile")
    assert response.status_code == 200
    assert response.json() == {"id": "owner", "username": None, "picture": None, "skin_url": None}


def test_unknown_profile_returns_not_found(client, signed_in):
    assert client.get("/api/users/missing/profile").status_code == 404


def test_profile_requires_login(client):
    assert client.get("/api/users/owner/profile").status_code == 401
