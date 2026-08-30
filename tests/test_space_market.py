import auth
from auth import get_current_user
from main import app
from models import SpaceMarketResource, SpaceMarketResourceLike, User


def _user(db, user_id: str = "market-user", email: str | None = None):
    user = User(
        id=user_id,
        email=email or f"{user_id}@example.com",
        username=f"Player {user_id}",
        minecraft_skin_url="https://cdn.entropydrop.com/skin.png",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _blockset(name: str = "Signal tower", color: int = 0xF2A93B):
    return {
        "type": "space-blockset",
        "version": 2,
        "name": name,
        "blockCount": 2,
        "blocks": [
            {"dx": 1, "dy": 0, "dz": 0, "block": 1, "color": color},
            {"dx": 0, "dy": 0, "dz": 0, "mx": 1, "my": 2, "mz": 3, "block": 1, "color": 0x48DBFB},
        ],
    }


def _entity(name: str = "Walker"):
    return {
        "type": "space-entity",
        "version": 2,
        "name": name,
        "rootId": "root",
        "nodeCount": 2,
        "blockCount": 2,
        "blocks": [
            {"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 0xF2A93B, "entityId": "root"},
            {"dx": 1, "dy": 0, "dz": 0, "block": 1, "color": 0x48DBFB, "entityId": "arm"},
        ],
        "childEntities": [{
            "id": "arm",
            "parentId": "root",
            "kind": "child",
            "pivot": [1.5, 0.5, 0.5],
            "bodyType": "kinematic",
        }],
        "scripts": [{"id": "arm", "code": "self.setLocalSpin([0,1,0], 8);"}],
        "enabled": [{"id": "arm", "enabled": True}],
        "constraints": [],
        "mode": "programmable",
        "bodyType": "dynamic",
        "useGravity": True,
        "bearingAxis": [0, 1, 0],
        "bearingRpm": 16,
        "pistonAxis": [0, 1, 0],
        "pistonDistance": 4,
        "pistonSpeed": 2,
        "cockpitPosition": [0, 1, 0],
        "isVehicle": True,
    }


def _colorset(name: str = "Sunset", variant: int = 0):
    colors = [
        "#f1c40f", "#ff6b81", "#a55eea", "#48dbfb", "#2ed573",
        "#eb4d4b", "#f5f6fa", "#2f3542", f"#{variant:06x}",
    ]
    return {"type": "space-colorset", "version": 2, "name": name, "colors": colors}


def _publish(client, kind: str, payload: dict):
    return client.post("/space/api/v2/market/resources", json={"kind": kind, "payload": payload})


def test_market_publishes_strict_canonical_resources_with_agpl_and_digest(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user

    response = _publish(client, "entity", _entity())

    assert response.status_code == 201, response.text
    data = response.json()
    assert data["resource"]["license"] == "AGPL-3.0-only"
    assert len(data["resource"]["digest"]) == 64
    assert data["resource"]["block_count"] == 2
    assert data["resource"]["node_count"] == 2
    assert data["resource"]["script_count"] == 1
    assert data["quota"] == {"daily_limit": 10, "published_today": 1, "remaining_today": 9}

    stored = db.query(SpaceMarketResource).one()
    assert stored.content["blocks"][0]["entityId"] == "arm"
    assert stored.content["useGravity"] is True
    assert stored.content["bearingAxis"] == [0, 1, 0]


def test_market_digest_rejects_renamed_and_reordered_duplicate_content(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    first = _blockset("First name")
    assert _publish(client, "blockset", first).status_code == 201

    duplicate = _blockset("Different name")
    duplicate["blocks"].reverse()
    response = _publish(client, "blockset", duplicate)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RESOURCE_ALREADY_PUBLISHED"
    assert db.query(SpaceMarketResource).count() == 1


def test_market_enforces_ten_successful_publications_per_utc_day(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    for index in range(10):
        response = _publish(client, "colorset", _colorset(f"Palette {index}", index + 1))
        assert response.status_code == 201, response.text

    blocked = _publish(client, "colorset", _colorset("Palette 11", 11))
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "DAILY_PUBLISH_LIMIT_REACHED"
    assert db.query(SpaceMarketResource).count() == 10


def test_market_validates_hierarchy_and_rejects_unknown_fields(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    cyclic = _entity()
    cyclic["childEntities"] = [
        {"id": "arm", "parentId": "hand"},
        {"id": "hand", "parentId": "arm"},
    ]
    cyclic["blocks"][1]["entityId"] = "hand"
    response = _publish(client, "entity", cyclic)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_MARKET_RESOURCE"

    extra = _blockset()
    extra["executable"] = "not allowed"
    response = _publish(client, "blockset", extra)
    assert response.status_code == 422
    assert db.query(SpaceMarketResource).count() == 0


def test_market_download_like_and_rankings(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    first = _publish(client, "blockset", _blockset("Popular", 0x111111)).json()["resource"]
    second = _publish(client, "blockset", _blockset("Liked", 0x222222)).json()["resource"]

    for _ in range(2):
        download = client.get(f"/space/api/v2/market/resources/{first['id']}/download")
        assert download.status_code == 200
        assert download.json()["license"] == "AGPL-3.0-only"
        assert download.json()["payload"]["type"] == "space-blockset"
    liked = client.post(f"/space/api/v2/market/resources/{second['id']}/like")
    assert liked.json() == {"is_liked": True, "likes_count": 1}
    assert db.query(SpaceMarketResourceLike).count() == 1

    downloads = client.get("/space/api/v2/market/resources?kind=blockset&sort=downloads").json()
    likes = client.get("/space/api/v2/market/resources?kind=blockset&sort=likes").json()
    latest = client.get("/space/api/v2/market/resources?kind=blockset&sort=latest").json()
    assert downloads["items"][0]["id"] == first["id"]
    assert downloads["items"][0]["downloads_count"] == 2
    assert likes["items"][0]["id"] == second["id"]
    assert likes["items"][0]["is_liked"] is True
    assert latest["items"][0]["id"] == second["id"]

    unliked = client.post(f"/space/api/v2/market/resources/{second['id']}/like")
    assert unliked.json() == {"is_liked": False, "likes_count": 0}


def test_only_admin_can_soft_delete_market_resource(client, db, monkeypatch):
    user = _user(db, "ordinary")
    admin = _user(db, "market-admin", "market-admin@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    resource = _publish(client, "colorset", _colorset()).json()["resource"]

    denied = client.delete(f"/space/api/v2/market/resources/{resource['id']}")
    assert denied.status_code == 403

    monkeypatch.setattr(auth.settings, "ADMIN_EMAILS", admin.email)
    app.dependency_overrides[get_current_user] = lambda: admin
    deleted = client.delete(f"/space/api/v2/market/resources/{resource['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert db.query(SpaceMarketResource).one().deleted_at is not None
    assert client.get(f"/space/api/v2/market/resources/{resource['id']}/download").status_code == 404
    assert client.get("/space/api/v2/market/resources").json()["total"] == 0

    # Soft deletion hides content but does not erase its digest or today's quota usage.
    app.dependency_overrides[get_current_user] = lambda: user
    duplicate = _publish(client, "colorset", _colorset("Renamed after deletion"))
    assert duplicate.status_code == 409
    market = client.get("/space/api/v2/market/resources").json()
    assert market["quota"] == {"daily_limit": 10, "published_today": 1, "remaining_today": 9}
