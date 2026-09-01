import pytest

import auth
from auth import get_current_user
from main import app
from models import SpaceMarketResource, SpaceMarketResourceLike, User
from routers import space_market
from space.inventory_codec import decode_inventory_resource, encode_inventory_resource


@pytest.fixture(autouse=True)
def market_object_storage(monkeypatch):
    objects: dict[str, bytes] = {}
    invalidations: list[str] = []

    def upload(file_content, key, is_public, content_type="image/png"):
        assert is_public is True
        assert content_type == "application/x-protobuf"
        objects[key] = bytes(file_content)
        return key

    def download(key, is_public):
        assert is_public is True
        return objects[key]

    def delete(key, is_public):
        assert is_public is True
        objects.pop(key, None)

    def invalidate(key):
        invalidations.append(key)
        return True

    monkeypatch.setattr(space_market.s3_utils, "upload_to_s3", upload)
    monkeypatch.setattr(space_market.s3_utils, "download_from_s3", download)
    monkeypatch.setattr(space_market.s3_utils, "delete_from_s3_strict", delete)
    monkeypatch.setattr(space_market.s3_utils, "invalidate_cdn_object", invalidate)
    monkeypatch.setattr(
        space_market.s3_utils,
        "get_cdn_url",
        lambda key: f"https://cdn.example.test/{key}",
    )
    return {"objects": objects, "invalidations": invalidations}


def _user(db, user_id: str = "market-user", email: str | None = None):
    user = User(
        id=user_id,
        email=email or f"{user_id}@example.com",
        username=f"Player {user_id}",
        skin_url="https://cdn.entropydrop.com/skin.png",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _blockset(name: str = "Signal tower", color: int = 0xF2A93B):
    return {
        "type": "space-blockset",
        "version": 3,
        "name": name,
        "blocks": [
            {"dx": 1, "dy": 0, "dz": 0, "block": 1, "color": color},
            {"dx": 0, "dy": 0, "dz": 0, "mx": 1, "my": 2, "mz": 3, "block": 1, "color": 0x48DBFB},
        ],
    }


def _entity(name: str = "Walker"):
    return {
        "type": "space-entity",
        "version": 3,
        "name": name,
        "root": {
            "id": "root",
            "body": {"type": "dynamic", "useGravity": True},
            "blocks": [
                {"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 0xF2A93B},
            ],
            "seats": [{"position": [0, 1, 0]}],
            "children": [{
                "id": "arm",
                "pivot": [1.5, 0.5, 0.5],
                "body": {"type": "kinematic"},
                "blocks": [
                    {"dx": 1, "dy": 0, "dz": 0, "block": 1, "color": 0x48DBFB},
                ],
                "script": "self.setLocalSpin([0,1,0], 8);",
                "seats": [{"position": [0, 0, 0]}, {"position": [1, 0, 0]}],
                "children": [],
            }],
        },
        "constraints": [],
    }


def _colorset(name: str = "Sunset", variant: int = 0):
    colors = [
        "#f1c40f", "#ff6b81", "#a55eea", "#48dbfb", "#2ed573",
        "#eb4d4b", "#f5f6fa", "#2f3542", f"#{variant:06x}",
    ]
    return {"type": "space-colorset", "version": 3, "name": name, "colors": colors}


def _publish(client, kind: str, payload: dict):
    return client.post(
        "/space/api/v2/market/resources",
        content=encode_inventory_resource(kind, payload),
        headers={"content-type": "application/x-protobuf"},
    )


def test_market_publishes_strict_canonical_resources_with_agpl_and_digest(client, db, market_object_storage):
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
    assert "preview" not in data["resource"]
    assert data["quota"] == {"daily_limit": 10, "published_today": 1, "remaining_today": 9}

    stored = db.query(SpaceMarketResource).one()
    assert stored.object_key.startswith(f"space-market/resources/{stored.id}/")
    assert data["resource"]["content_url"] == f"https://cdn.example.test/{stored.object_key}"
    kind, canonical = decode_inventory_resource(market_object_storage["objects"][stored.object_key])
    assert kind == "entity"
    assert canonical["root"]["children"][0]["id"] == "arm"
    assert canonical["root"]["body"]["useGravity"] is True
    assert len(canonical["root"]["children"][0]["seats"]) == 2

    # Listing exposes the original CDN object for preview without touching the
    # counted /download endpoint.
    listed = client.get("/space/api/v2/market/resources?kind=entity").json()["items"][0]
    db.refresh(stored)
    assert listed["content_url"] == data["resource"]["content_url"]
    assert "preview" not in listed
    assert listed["downloads_count"] == 0
    assert stored.downloads_count == 0


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


def test_market_validates_entity_hierarchy(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    duplicate = _entity()
    duplicate["root"]["children"].append({
        "id": "arm",
        "body": {"type": "kinematic"},
        "blocks": [],
        "seats": [],
        "children": [],
    })
    response = _publish(client, "entity", duplicate)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_MARKET_RESOURCE"

    assert db.query(SpaceMarketResource).count() == 0


def test_market_uses_one_explicit_root_and_preserves_child_collision_flags(client, db, market_object_storage):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    entity = _entity()
    entity["root"]["children"][0]["body"]["collisionEnabled"] = False

    response = _publish(client, "entity", entity)

    assert response.status_code == 201, response.text
    stored = db.query(SpaceMarketResource).one()
    kind, canonical = decode_inventory_resource(market_object_storage["objects"][stored.object_key])
    assert kind == "entity"
    assert canonical["root"]["id"] == "root"
    assert canonical["root"]["children"][0]["body"]["collisionEnabled"] is False

    too_many_children = _entity("Too many children")
    too_many_children["root"]["children"] = [
        {
            "id": f"node_{index}",
            "body": {"type": "kinematic"},
            "blocks": [],
            "seats": [],
            "children": [],
        }
        for index in range(64)
    ]
    assert _publish(client, "entity", too_many_children).status_code == 422


def test_market_canonicalizes_the_only_block_id_and_rejects_standard_micro_overlap(client, db, market_object_storage):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    omitted = _blockset("Implicit block id")
    for block in omitted["blocks"]:
        block.pop("block")
    assert _publish(client, "blockset", omitted).status_code == 201
    stored = db.query(SpaceMarketResource).one()
    kind, canonical = decode_inventory_resource(market_object_storage["objects"][stored.object_key])
    assert kind == "blockset"
    assert all(block["block"] == 1 for block in canonical["blocks"])

    explicit = _blockset("Explicit block id")
    duplicate = _publish(client, "blockset", explicit)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "RESOURCE_ALREADY_PUBLISHED"

    overlap = _blockset("Overlap")
    overlap["blocks"] = [
        {"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 0x111111},
        {"dx": 0, "dy": 0, "dz": 0, "mx": 0, "my": 0, "mz": 0, "block": 1, "color": 0x222222},
    ]
    assert _publish(client, "blockset", overlap).status_code == 422


def test_market_publish_body_limit_allows_large_valid_protobuf_resources(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    blocks = []
    for index in range(50_000):
        blocks.append({
            "dx": index % 64,
            "dy": (index // 64) % 64,
            "dz": index // (64 * 64),
            "block": 1,
            "color": 0x123456,
        })
    payload = {
        "type": "space-blockset",
        "version": 3,
        "name": "Large valid shape",
        "blocks": blocks,
    }

    response = _publish(client, "blockset", payload)

    assert response.status_code == 201, response.text
    assert response.json()["resource"]["size_bytes"] > 512 * 1024
    assert response.json()["resource"]["block_count"] == 50_000


def test_market_publish_body_limit_counts_bytes_when_content_length_lies(client):
    body = b"x" * (9 * 1024 * 1024 + 1)

    response = client.post(
        "/space/api/v2/market/resources",
        content=body,
        headers={"content-type": "application/x-protobuf", "content-length": "1"},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "MARKET_RESOURCE_TOO_LARGE"


def test_market_does_not_create_a_row_when_cdn_upload_fails(client, db, monkeypatch):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user

    def fail_upload(*_args, **_kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(space_market.s3_utils, "upload_to_s3", fail_upload)
    response = _publish(client, "colorset", _colorset())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MARKET_STORAGE_UNAVAILABLE"
    assert db.query(SpaceMarketResource).count() == 0


def test_market_download_like_and_rankings(client, db, market_object_storage):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    first = _publish(client, "blockset", _blockset("Popular", 0x111111)).json()["resource"]
    second = _publish(client, "blockset", _blockset("Liked", 0x222222)).json()["resource"]

    for _ in range(2):
        download = client.get(f"/space/api/v2/market/resources/{first['id']}/download")
        assert download.status_code == 200
        assert download.json()["license"] == "AGPL-3.0-only"
        assert download.json()["download_url"].startswith("https://cdn.example.test/space-market/resources/")
        object_key = download.json()["download_url"].removeprefix("https://cdn.example.test/")
        kind, payload = decode_inventory_resource(market_object_storage["objects"][object_key])
        assert kind == "blockset"
        assert payload["type"] == "space-blockset"
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


def test_only_admin_can_soft_delete_market_resource(client, db, monkeypatch, market_object_storage):
    user = _user(db, "ordinary")
    admin = _user(db, "market-admin", "market-admin@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    resource = _publish(client, "colorset", _colorset()).json()["resource"]
    stored = db.query(SpaceMarketResource).one()
    object_key = stored.object_key
    assert object_key in market_object_storage["objects"]

    denied = client.delete(f"/space/api/v2/market/resources/{resource['id']}")
    assert denied.status_code == 403

    monkeypatch.setattr(auth.settings, "ADMIN_EMAILS", admin.email)
    app.dependency_overrides[get_current_user] = lambda: admin
    deleted = client.delete(f"/space/api/v2/market/resources/{resource['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["cdn_object_deleted"] is True
    assert deleted.json()["cdn_invalidation_requested"] is True
    assert object_key not in market_object_storage["objects"]
    assert market_object_storage["invalidations"] == [object_key]
    assert db.query(SpaceMarketResource).one().deleted_at is not None
    assert client.get(f"/space/api/v2/market/resources/{resource['id']}/download").status_code == 404
    assert client.get("/space/api/v2/market/resources").json()["total"] == 0

    # The row remains as an audit/tombstone while CDN content is gone. Its digest
    # and today's quota usage remain reserved.
    app.dependency_overrides[get_current_user] = lambda: user
    duplicate = _publish(client, "colorset", _colorset("Renamed after deletion"))
    assert duplicate.status_code == 409
    market = client.get("/space/api/v2/market/resources").json()
    assert market["quota"] == {"daily_limit": 10, "published_today": 1, "remaining_today": 9}


def test_admin_delete_keeps_market_row_active_when_cdn_cleanup_fails(
    client,
    db,
    monkeypatch,
    market_object_storage,
):
    user = _user(db, "publisher")
    admin = _user(db, "cleanup-admin", "cleanup-admin@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    resource = _publish(client, "colorset", _colorset()).json()["resource"]
    stored = db.query(SpaceMarketResource).one()
    object_key = stored.object_key

    monkeypatch.setattr(auth.settings, "ADMIN_EMAILS", admin.email)
    app.dependency_overrides[get_current_user] = lambda: admin
    monkeypatch.setattr(
        space_market.s3_utils,
        "invalidate_cdn_object",
        lambda _key: (_ for _ in ()).throw(RuntimeError("invalidation denied")),
    )

    response = client.delete(f"/space/api/v2/market/resources/{resource['id']}")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MARKET_STORAGE_UNAVAILABLE"
    db.refresh(stored)
    assert stored.deleted_at is None
    assert object_key in market_object_storage["objects"]
