import hashlib
import math
import uuid
import datetime
import base64

import pytest

from auth import get_current_user
from main import app
from models import SpaceEntityCreateToken, SpaceMarketResource, SpaceWorldEntity, User
from routers import space_entities, space_market
from space.inventory_codec import decode_inventory_resource, encode_inventory_resource


@pytest.fixture
def entity_object_storage(monkeypatch):
    objects: dict[str, bytes] = {}

    def upload(file_content, key, is_public, content_type="image/png"):
        assert is_public is True
        objects[key] = bytes(file_content)
        return key

    def download(key, is_public):
        assert is_public is True
        return objects[key]

    def delete(key, is_public):
        assert is_public is True
        objects.pop(key, None)

    monkeypatch.setattr(space_market.s3_utils, "upload_to_s3", upload)
    monkeypatch.setattr(space_market.s3_utils, "download_from_s3", download)
    monkeypatch.setattr(space_market.s3_utils, "delete_from_s3_strict", delete)
    monkeypatch.setattr(space_market.s3_utils, "invalidate_cdn_object", lambda _key: True)
    monkeypatch.setattr(space_market.s3_utils, "get_cdn_url", lambda key: f"https://cdn.test/{key}")
    # Both routers import the same s3_utils module, but keeping this assertion
    # explicit protects the test if either router later wraps storage.
    monkeypatch.setattr(space_entities.s3_utils, "download_from_s3", download)
    return objects


def _user(db, user_id: str):
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        username=user_id,
        skin_url="https://cdn.entropydrop.com/skin.png",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _entity(name="External Walker"):
    return {
        "type": "space-entity",
        "version": 3,
        "name": name,
        "root": {
            "id": "root",
            "anchorRotation": [0, 0, math.sqrt(0.5), math.sqrt(0.5)],
            "body": {"type": "dynamic", "useGravity": True},
            "blocks": [{"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 0xF2A93B}],
            "script": "self.setLocalSpin([0,1,0], 2);",
            "seats": [],
            "children": [],
        },
        "constraints": [],
    }


def _publish_entity(client):
    response = client.post(
        "/space/api/v2/market/resources",
        content=encode_inventory_resource("entity", _entity()),
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 201, response.text
    return response.json()["resource"]


def _create_scope_token(client, world_id: str) -> tuple[str, str]:
    response = client.post(
        f"/space/api/v2/worlds/{world_id}/entity-create-tokens",
        json={"name": "build-agent"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], response.json()["token"]


def test_external_create_is_idempotent_and_copies_definition(client, db, entity_object_storage):
    owner = _user(db, "entity-owner")
    app.dependency_overrides[get_current_user] = lambda: owner
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    resource = _publish_entity(client)
    token_id, create_token = _create_scope_token(client, world_id)
    operation_id = str(uuid.uuid4())
    body = {
        "operation_id": operation_id,
        "resource_id": resource["id"],
        "position": {"x_cm": 0, "y_cm": 3200, "z_cm": 204799},
        "yaw_quarter_turns": 3,
        "desired_run_state": "running",
    }

    create_headers = {"Authorization": f"Bearer {create_token}"}
    created = client.post(f"/space/api/v2/worlds/{world_id}/entities", json=body, headers=create_headers)
    repeated = client.post(
        f"/space/api/v2/worlds/{world_id}/entities", json=body, headers=create_headers
    )

    assert created.status_code == 201, created.text
    assert repeated.status_code == 201, repeated.text
    assert created.json()["id"] == repeated.json()["id"]
    assert created.json()["can_control"] is True
    assert created.json()["yaw_quarter_turns"] == 3
    assert db.query(SpaceWorldEntity).count() == 1
    stored = db.query(SpaceWorldEntity).one()
    assert stored.source_resource_id == resource["id"]
    assert bytes(stored.content_digest) == hashlib.sha256(bytes(stored.definition)).digest()
    stored_token = db.query(SpaceEntityCreateToken).one()
    assert stored_token.id == token_id
    assert create_token.encode() not in bytes(stored_token.token_hash)
    assert stored_token.last_used_at is not None

    reused = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={**body, "position": {"x_cm": 1, "y_cm": 3200, "z_cm": 204799}},
        headers=create_headers,
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "ENTITY_OPERATION_ID_REUSED"

    # The scoped secret is not a general login token and is immediately
    # invalid after the owner revokes it.
    app.dependency_overrides.pop(get_current_user)
    not_general_auth = client.get(
        f"/space/api/v2/worlds/{world_id}/entities"
        "?center_x_cm=0&center_z_cm=0&radius_cm=100",
        headers=create_headers,
    )
    assert not_general_auth.status_code == 401
    app.dependency_overrides[get_current_user] = lambda: owner
    revoked = client.delete(
        f"/space/api/v2/worlds/{world_id}/entity-create-tokens/{token_id}"
    )
    assert revoked.json() == {"revoked": True, "token_id": token_id}
    after_revoke = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={**body, "operation_id": str(uuid.uuid4())},
        headers=create_headers,
    )
    assert after_revoke.status_code == 401

    # The world owns a canonical copy. A publisher hard-delete removes both the
    # market row and object but cannot invalidate an already-created entity.
    deleted = client.delete(f"/space/api/v2/market/resources/{resource['id']}")
    assert deleted.status_code == 200
    assert db.query(SpaceMarketResource).count() == 0
    assert entity_object_storage == {}
    definition = client.get(
        f"/space/api/v2/worlds/{world_id}/entities/{created.json()['id']}/definition"
    )
    assert definition.status_code == 200
    assert definition.headers["content-type"].startswith("application/x-protobuf")
    assert hashlib.sha256(definition.content).hexdigest() == created.json()["definition_digest"]
    kind, decoded = decode_inventory_resource(definition.content)
    assert kind == "entity"
    assert decoded["name"] == "External Walker"


def test_list_wraps_aoi_and_only_owner_can_change_run_state(client, db, entity_object_storage):
    owner = _user(db, "entity-owner")
    other = _user(db, "entity-other")
    app.dependency_overrides[get_current_user] = lambda: owner
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    resource = _publish_entity(client)
    _token_id, create_token = _create_scope_token(client, world_id)
    created = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={
            "operation_id": str(uuid.uuid4()),
            "resource_id": resource["id"],
            "position": {"x_cm": 0, "y_cm": 3200, "z_cm": 100},
            "desired_run_state": "running",
        },
        headers={"Authorization": f"Bearer {create_token}"},
    ).json()

    first_instance = str(uuid.uuid4())
    first_lease = client.put(
        f"/space/api/v2/worlds/{world_id}/entities/execution-leases",
        json={"instance_id": first_instance, "entity_ids": [created["id"]]},
    )
    assert first_lease.status_code == 200, first_lease.text
    assert first_lease.json()["items"][0]["granted"] is True
    assert first_lease.json()["items"][0]["execution_epoch"] == 1
    blocked_lease = client.put(
        f"/space/api/v2/worlds/{world_id}/entities/execution-leases",
        json={"instance_id": str(uuid.uuid4()), "entity_ids": [created["id"]]},
    )
    assert blocked_lease.json()["items"][0]["granted"] is False

    stored_entity = db.query(SpaceWorldEntity).one()
    stored_entity.execution_lease_expires_at = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    )
    db.commit()
    takeover_lease = client.put(
        f"/space/api/v2/worlds/{world_id}/entities/execution-leases",
        json={"instance_id": str(uuid.uuid4()), "entity_ids": [created["id"]]},
    )
    assert takeover_lease.json()["items"][0]["granted"] is True
    assert takeover_lease.json()["items"][0]["execution_epoch"] == 2

    # X=0 is in range from the opposite side of the toroidal world seam.
    listed = client.get(
        f"/space/api/v2/worlds/{world_id}/entities"
        "?center_x_cm=1638399&center_z_cm=100&radius_cm=200"
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [created["id"]]

    app.dependency_overrides[get_current_user] = lambda: other
    assert client.post("/space/api/v2/bootstrap").status_code == 200
    forbidden = client.put(
        f"/space/api/v2/worlds/{world_id}/entities/{created['id']}/run-state",
        json={
            "operation_id": str(uuid.uuid4()),
            "desired_run_state": "stopped",
            "expected_revision": 1,
        },
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "ENTITY_CONTROL_FORBIDDEN"

    app.dependency_overrides[get_current_user] = lambda: owner
    stopped = client.put(
        f"/space/api/v2/worlds/{world_id}/entities/{created['id']}/run-state",
        json={
            "operation_id": str(uuid.uuid4()),
            "desired_run_state": "stopped",
            "expected_revision": 1,
        },
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["desired_run_state"] == "stopped"
    assert stopped.json()["revision"] == 2
    db.refresh(stored_entity)
    assert stored_entity.execution_instance_id is None
    assert stored_entity.execution_lease_expires_at is None

    conflict = client.put(
        f"/space/api/v2/worlds/{world_id}/entities/{created['id']}/run-state",
        json={
            "operation_id": str(uuid.uuid4()),
            "desired_run_state": "running",
            "expected_revision": 1,
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ENTITY_REVISION_CONFLICT"


def test_browser_entities_are_backend_snapshotted_updated_and_hard_deleted(client, db):
    owner = _user(db, "browser-owner")
    other = _user(db, "browser-other")
    app.dependency_overrides[get_current_user] = lambda: owner
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    definition = encode_inventory_resource("entity", _entity("Browser Builder"))
    snapshot = {
        "constructorOrigin": [12, 20, 34],
        "position": [12.5, 20.5, 34.5],
        "quaternion": [0, 0, 0, 1],
        "velocity": [0, 0, 0],
        "angularVelocity": [0, 0, 0],
        "physicsSimulationEnabled": False,
        "scriptStatus": "stopped",
    }
    create_body = {
        "operation_id": str(uuid.uuid4()),
        "definition_base64": base64.b64encode(definition).decode(),
        "snapshot": snapshot,
        "position": {"x_cm": 1250, "y_cm": 2050, "z_cm": 3450},
        "desired_run_state": "stopped",
    }
    created = client.post(
        f"/space/api/v2/worlds/{world_id}/entities/browser",
        json=create_body,
    )
    repeated = client.post(
        f"/space/api/v2/worlds/{world_id}/entities/browser",
        json=create_body,
    )
    assert created.status_code == 201, created.text
    record = created.json()
    assert repeated.status_code == 201
    assert repeated.json()["id"] == record["id"]
    assert record["source_kind"] == "browser"
    assert record["source_resource_id"] is None
    assert record["can_edit"] is True
    assert record["snapshot_size_bytes"] > 0
    assert db.query(SpaceWorldEntity).count() == 1

    downloaded = client.get(
        f"/space/api/v2/worlds/{world_id}/entities/{record['id']}/snapshot"
    )
    assert downloaded.status_code == 200
    assert downloaded.json() == snapshot
    assert hashlib.sha256(downloaded.content).hexdigest() == record["snapshot_digest"]

    moved_snapshot = {**snapshot, "position": [13.5, 21.5, 35.5]}
    updated = client.put(
        f"/space/api/v2/worlds/{world_id}/entities/{record['id']}/checkpoint",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_revision": 1,
            "snapshot": moved_snapshot,
            "position": {"x_cm": 1350, "y_cm": 2150, "z_cm": 3550},
            "desired_run_state": "stopped",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["position"]["x_cm"] == 1350

    app.dependency_overrides[get_current_user] = lambda: other
    assert client.post("/space/api/v2/bootstrap").status_code == 200
    forbidden = client.delete(
        f"/space/api/v2/worlds/{world_id}/entities/{record['id']}"
    )
    assert forbidden.status_code == 403

    app.dependency_overrides[get_current_user] = lambda: owner
    deleted = client.delete(
        f"/space/api/v2/worlds/{world_id}/entities/{record['id']}"
    )
    assert deleted.json() == {"deleted": True, "entity_id": record["id"]}
    assert db.query(SpaceWorldEntity).count() == 0
