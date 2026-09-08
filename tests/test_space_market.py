import math

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
        "version": 6,
        "name": name,
        "blocks": [
            {"dx": 1, "dy": 0, "dz": 0, "block": 1, "color": color},
            {"dx": 0, "dy": 0, "dz": 0, "mx": 1, "my": 2, "mz": 3, "block": 1, "color": 0x48DBFB},
        ],
    }


def _entity(name: str = "Walker"):
    return {
        "type": "space-entity",
        "version": 6,
        "root": {
            "name": name,
            "id": "root",
            "anchorRotation": [0, 0, math.sqrt(0.5), math.sqrt(0.5)],
            "body": {"type": "dynamic", "useGravity": True},
            "blocks": [
                {"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 0xF2A93B},
            ],
            "seats": [{"position": [0, 1, 0]}],
            "children": [{
                "id": "arm",
                "name": "Arm module",
                "pivot": [1.5, 0.5, 0.5],
                "localPosition": [0.5, 0, 0],
                "localRotation": [0, 1, 0, 0],
                "anchorRotation": [math.sqrt(0.5), 0, 0, math.sqrt(0.5)],
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
    return {"type": "space-colorset", "version": 6, "name": name, "colors": colors}


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
    assert "name" not in canonical
    assert canonical["root"]["name"] == "Walker"
    assert data["resource"]["name"] == canonical["root"]["name"]
    assert canonical["root"]["children"][0]["name"] == "Arm module"
    assert canonical["root"]["children"][0]["id"] == "arm"
    assert canonical["root"]["body"]["useGravity"] is True
    assert canonical["root"]["anchorRotation"] == [0, 0, math.sqrt(0.5), math.sqrt(0.5)]
    assert canonical["root"]["children"][0]["localPosition"] == [0.5, 0, 0]
    assert canonical["root"]["children"][0]["localRotation"] == [0, 1, 0, 0]
    assert canonical["root"]["children"][0]["anchorRotation"] == [math.sqrt(0.5), 0, 0, math.sqrt(0.5)]
    assert len(canonical["root"]["children"][0]["seats"]) == 2

    # Listing exposes the original CDN object for preview without touching the
    # counted /download endpoint.
    listed = client.get("/space/api/v2/market/resources?kind=entity").json()["items"][0]
    db.refresh(stored)
    assert listed["content_url"] == data["resource"]["content_url"]
    assert "preview" not in listed
    assert listed["downloads_count"] == 0
    assert stored.downloads_count == 0


def test_market_validation_normalizes_signed_zero_doubles():
    entity = _entity("Canonical zero")
    root = entity["root"]
    root["anchorRotation"] = [-0.0, -0.0, math.sqrt(0.5), math.sqrt(0.5)]
    root["body"].update({"restitution": -0.0, "friction": -0.0})
    root["seats"][0]["position"] = [-0.0, 1.0, -0.0]
    child = root["children"][0]
    child["pivot"] = [1.5, 0.5, 0.5]
    child["localPosition"] = [0.5, -0.0, -0.0]
    child["localRotation"] = [-0.0, 1.0, -0.0, -0.0]
    child["anchorRotation"] = [math.sqrt(0.5), -0.0, -0.0, math.sqrt(0.5)]
    child["body"].update({"restitution": -0.0, "friction": -0.0})
    entity["constraints"] = [{
        "id": "joint",
        "type": "point",
        "bodyA": "root",
        "bodyB": "arm",
        "anchorA": [-0.0, -0.0, -0.0],
        "anchorB": [-0.0, -0.0, -0.0],
        "axisA": [-0.0, -0.0, -0.0],
        "axisB": [-0.0, -0.0, -0.0],
        "referenceA": [-0.0, -0.0, -0.0],
        "referenceB": [-0.0, -0.0, -0.0],
        "limits": {"min": -0.0, "max": -0.0},
        "stiffness": -0.0,
        "collideConnected": False,
    }]

    canonical = space_market.validate_inventory_resource_payload("entity", entity)

    def assert_no_negative_zero(value):
        if isinstance(value, float) and value == 0.0:
            assert math.copysign(1.0, value) == 1.0
        elif isinstance(value, (list, tuple)):
            for item in value:
                assert_no_negative_zero(item)
        elif isinstance(value, dict):
            for item in value.values():
                assert_no_negative_zero(item)

    assert_no_negative_zero(canonical)


def test_market_name_limit_counts_unicode_code_points():
    accepted = space_market.validate_inventory_resource_payload(
        "blockset",
        _blockset("🧱" * 80),
    )
    assert accepted["name"] == "🧱" * 80
    assert space_market.validate_inventory_resource_payload(
        "blockset",
        _blockset("\ufeffSignal"),
    )["name"] == "\ufeffSignal"

    with pytest.raises(ValueError):
        space_market.validate_inventory_resource_payload(
            "blockset",
            _blockset("🧱" * 81),
        )
    with pytest.raises(ValueError):
        space_market.validate_inventory_resource_payload("blockset", _blockset("\x1c"))


def test_market_stopped_grid_uses_the_explicit_root_pivot():
    def entity_with_root_pivot(local_position):
        return {
            "type": "space-entity",
            "version": 6,
            "root": {
                "name": "Pivot",
                "id": "root",
                "pivot": [0.6, 0.5, 0.5],
                "body": {"type": "dynamic"},
                "blocks": [{"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 0}],
                "seats": [],
                "children": [{
                    "id": "arm",
                    "pivot": [2.5, 0.5, 0.5],
                    "localPosition": local_position,
                    "localRotation": [0, 0, 0, 1],
                    "body": {"type": "kinematic"},
                    "blocks": [{"dx": 2, "dy": 0, "dz": 0, "block": 1, "color": 0}],
                    "seats": [],
                    "children": [],
                }],
            },
            "constraints": [],
        }

    accepted = space_market.validate_inventory_resource_payload(
        "entity",
        entity_with_root_pivot([1.9, 0, 0]),
    )
    assert accepted["root"]["pivot"] == (0.6, 0.5, 0.5)

    with pytest.raises(ValueError, match="construction grid"):
        space_market.validate_inventory_resource_payload(
            "entity",
            entity_with_root_pivot([1, 0, 0]),
        )


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

    entity = _entity("First entity name")
    entity["root"]["children"].append({
        "id": "antenna",
        "body": {"type": "kinematic"},
        "blocks": [],
        "seats": [],
        "children": [],
    })
    assert _publish(client, "entity", entity).status_code == 201

    entity["root"]["name"] = "Renamed entity"
    entity["root"]["children"][0]["name"] = "Renamed child"
    entity["root"]["children"].reverse()
    response = _publish(client, "entity", entity)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RESOURCE_ALREADY_PUBLISHED"
    assert db.query(SpaceMarketResource).count() == 2


def test_component_names_are_optional_non_unique_unicode_and_root_metadata(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user
    entity = _entity("")
    entity["root"]["children"][0]["name"] = "🧱" * 80
    canonical = space_market.validate_inventory_resource_payload("entity", entity)
    assert canonical["root"]["name"] == ""
    assert canonical["root"]["children"][0]["name"] == "🧱" * 80
    published = _publish(client, "entity", entity)
    assert published.status_code == 201, published.text
    assert published.json()["resource"]["name"] == entity["root"]["id"]

    entity["root"]["name"] = "🧱" * 80
    assert space_market.validate_inventory_resource_payload("entity", entity)["root"]["name"] == "🧱" * 80
    entity["root"]["children"][0]["name"] += "🧱"
    with pytest.raises(ValueError):
        space_market.validate_inventory_resource_payload("entity", entity)
    entity["root"]["children"][0]["name"] = 123
    with pytest.raises(ValueError):
        space_market.validate_inventory_resource_payload("entity", entity)
    with pytest.raises(ValueError):
        space_market.validate_inventory_resource_payload("entity", {**_entity(), "name": "obsolete"})


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


def test_market_enforces_live_resource_count_and_storage_quotas(client, db, monkeypatch):
    user = _user(db, "market-aggregate-owner")
    app.dependency_overrides[get_current_user] = lambda: user
    first_payload = _colorset("Aggregate one", 1)
    first_size = len(encode_inventory_resource("colorset", first_payload))
    monkeypatch.setattr(space_market, "SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER", first_size)

    first = _publish(client, "colorset", first_payload)
    storage_blocked = _publish(client, "colorset", _colorset("Aggregate two", 2))
    assert first.status_code == 201, first.text
    assert storage_blocked.status_code == 429
    assert storage_blocked.json()["detail"]["code"] == "MARKET_STORAGE_QUOTA_REACHED"

    monkeypatch.setattr(space_market, "SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER", 1024 * 1024)
    monkeypatch.setattr(space_market, "SPACE_MARKET_MAX_RESOURCES_PER_OWNER", 1)
    count_blocked = _publish(client, "colorset", _colorset("Aggregate three", 3))
    assert count_blocked.status_code == 429
    assert count_blocked.json()["detail"]["code"] == "MARKET_RESOURCE_COUNT_QUOTA_REACHED"
    assert db.query(SpaceMarketResource).count() == 1


def test_market_daily_upload_bytes_are_not_refunded_by_delete(
    client,
    db,
    monkeypatch,
    market_object_storage,
):
    user = _user(db, "market-upload-owner")
    app.dependency_overrides[get_current_user] = lambda: user
    first_payload = _colorset("Upload budget one", 1)
    first_size = len(encode_inventory_resource("colorset", first_payload))
    monkeypatch.setattr(space_market, "SPACE_MARKET_DAILY_UPLOAD_BYTES", first_size)

    first = _publish(client, "colorset", first_payload)
    assert first.status_code == 201, first.text
    resource_id = first.json()["resource"]["id"]
    assert client.delete(f"/space/api/v2/market/resources/{resource_id}").status_code == 200

    blocked = _publish(client, "colorset", _colorset("Upload budget two", 2))
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "MARKET_DAILY_UPLOAD_QUOTA_REACHED"
    assert db.query(SpaceMarketResource).count() == 0


def test_market_validates_entity_hierarchy(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user

    opaque_ids = _entity("Opaque component ids")
    opaque_ids["root"]["id"] = "world"
    opaque_ids["root"]["children"][0]["id"] = "root"
    opaque_ids["constraints"] = [
        {
            "id": "root",
            "type": "point",
            "bodyA": None,
            "bodyB": "root",
            "stiffness": 0.9,
        },
        {
            "id": "world",
            "type": "point",
            "bodyA": "world",
            "bodyB": "root",
            "stiffness": 0.9,
        },
    ]
    canonical = space_market.validate_inventory_resource_payload("entity", opaque_ids)
    assert canonical["root"]["id"] == "world"
    assert canonical["root"]["children"][0]["id"] == "root"
    assert canonical["constraints"][0]["bodyA"] is None
    assert canonical["constraints"][1]["bodyA"] == "world"

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


def test_market_rejects_invalid_component_transforms(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user

    root_transform = _entity("Root transform")
    root_transform["root"]["localPosition"] = [1, 0, 0]
    assert _publish(client, "entity", root_transform).status_code == 422

    invalid_position = _entity("Invalid position")
    invalid_position["root"]["children"][0]["localPosition"] = [129, 0, 0]
    assert _publish(client, "entity", invalid_position).status_code == 422

    invalid_rotation = _entity("Invalid rotation")
    invalid_rotation["root"]["children"][0]["localRotation"] = [0, 0, 0, 0]
    assert _publish(client, "entity", invalid_rotation).status_code == 422

    off_grid_rotation = _entity("Off-grid rotation")
    off_grid_rotation["root"]["children"][0]["localRotation"] = [
        0,
        0,
        math.sin(math.pi / 8),
        math.cos(math.pi / 8),
    ]
    response = _publish(client, "entity", off_grid_rotation)
    assert response.status_code == 422
    assert "24 axis-aligned" in response.json()["detail"]["message"]

    off_grid_anchor = _entity("Off-grid anchor")
    off_grid_anchor["root"]["anchorRotation"] = [
        0,
        0,
        math.sin(math.pi / 8),
        math.cos(math.pi / 8),
    ]
    assert _publish(client, "entity", off_grid_anchor).status_code == 422


def test_market_rejects_off_grid_and_overlapping_stopped_entity_pose(client, db):
    user = _user(db)
    app.dependency_overrides[get_current_user] = lambda: user

    off_grid = _entity("Off-grid stopped pose")
    off_grid["root"]["children"][0]["localPosition"] = [0.6, 0, 0]
    response = _publish(client, "entity", off_grid)
    assert response.status_code == 422
    assert "0.125-unit construction grid" in response.json()["detail"]["message"]

    overlapping = _entity("Overlapping stopped pose")
    overlapping["root"]["children"][0]["localPosition"] = [-0.5, 0, 0]
    response = _publish(client, "entity", overlapping)
    assert response.status_code == 422
    assert "overlapping voxels" in response.json()["detail"]["message"]


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
        "version": 6,
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
    assert all(item["can_delete"] is True for item in latest["items"])

    unliked = client.post(f"/space/api/v2/market/resources/{second['id']}/like")
    assert unliked.json() == {"is_liked": False, "likes_count": 0}


def test_market_can_filter_current_users_publications(client, db):
    first_user = _user(db, "first-publisher")
    second_user = _user(db, "second-publisher")
    app.dependency_overrides[get_current_user] = lambda: first_user
    first = _publish(client, "blockset", _blockset("First", 0x111111)).json()["resource"]
    app.dependency_overrides[get_current_user] = lambda: second_user
    second = _publish(client, "blockset", _blockset("Second", 0x222222)).json()["resource"]

    mine = client.get("/space/api/v2/market/resources?kind=blockset&mine=true").json()
    assert mine["total"] == 1
    assert [item["id"] for item in mine["items"]] == [second["id"]]
    assert mine["items"][0]["can_delete"] is True

    community = client.get("/space/api/v2/market/resources?kind=blockset").json()
    can_delete = {item["id"]: item["can_delete"] for item in community["items"]}
    assert can_delete == {first["id"]: False, second["id"]: True}


def test_author_can_permanently_delete_market_resource(client, db, market_object_storage):
    author = _user(db, "author")
    outsider = _user(db, "outsider")
    app.dependency_overrides[get_current_user] = lambda: author
    resource = _publish(client, "colorset", _colorset()).json()["resource"]
    assert resource["can_delete"] is True
    stored = db.query(SpaceMarketResource).one()
    object_key = stored.object_key
    assert object_key in market_object_storage["objects"]

    app.dependency_overrides[get_current_user] = lambda: outsider
    liked = client.post(f"/space/api/v2/market/resources/{resource['id']}/like")
    assert liked.status_code == 200
    assert db.query(SpaceMarketResourceLike).count() == 1
    denied = client.delete(f"/space/api/v2/market/resources/{resource['id']}")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "MARKET_DELETE_FORBIDDEN"

    app.dependency_overrides[get_current_user] = lambda: author
    deleted = client.delete(f"/space/api/v2/market/resources/{resource['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["cdn_object_deleted"] is True
    assert deleted.json()["cdn_invalidation_requested"] is True
    assert object_key not in market_object_storage["objects"]
    assert market_object_storage["invalidations"] == [object_key]
    assert db.query(SpaceMarketResource).count() == 0
    assert db.query(SpaceMarketResourceLike).count() == 0
    assert client.get(f"/space/api/v2/market/resources/{resource['id']}/download").status_code == 404
    market = client.get("/space/api/v2/market/resources").json()
    assert market["total"] == 0
    assert market["quota"] == {"daily_limit": 10, "published_today": 1, "remaining_today": 9}

    # Removing the row releases the canonical digest but never refunds today's quota.
    app.dependency_overrides[get_current_user] = lambda: author
    republished = _publish(client, "colorset", _colorset("Renamed after deletion"))
    assert republished.status_code == 201
    market = client.get("/space/api/v2/market/resources").json()
    assert market["quota"] == {"daily_limit": 10, "published_today": 2, "remaining_today": 8}


def test_admin_can_permanently_delete_another_publishers_resource(client, db, monkeypatch):
    publisher = _user(db, "publisher")
    admin = _user(db, "market-admin", "market-admin@example.com")
    app.dependency_overrides[get_current_user] = lambda: publisher
    resource = _publish(client, "colorset", _colorset()).json()["resource"]

    monkeypatch.setattr(auth.settings, "ADMIN_EMAILS", admin.email)
    app.dependency_overrides[get_current_user] = lambda: admin
    listed = client.get("/space/api/v2/market/resources?kind=colorset").json()["items"]
    assert listed[0]["can_delete"] is True

    deleted = client.delete(f"/space/api/v2/market/resources/{resource['id']}")
    assert deleted.status_code == 200
    assert db.query(SpaceMarketResource).count() == 0


def test_delete_keeps_market_row_when_cdn_cleanup_fails(
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
    assert db.query(SpaceMarketResource).count() == 1
    assert object_key in market_object_storage["objects"]
