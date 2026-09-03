import hashlib
import math
import uuid
import datetime
import base64

from auth import get_current_user
from main import app
from models import SpaceApiKey, SpaceWorldEntity, User
from space.inventory_codec import decode_inventory_resource, encode_inventory_resource


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


def _create_api_key(client, *, allow_run: bool = False) -> tuple[str, str]:
    scopes = ["space:entity:create"]
    if allow_run:
        scopes.append("space:entity:run")
    response = client.post(
        "/space/api/v2/api-keys",
        json={"name": "build-agent", "scopes": scopes},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], response.json()["api_key"]


def test_external_create_is_idempotent_and_stores_validated_definition(client, db):
    owner = _user(db, "entity-owner")
    app.dependency_overrides[get_current_user] = lambda: owner
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    api_key_id, api_key = _create_api_key(client, allow_run=True)
    listed_keys = client.get("/space/api/v2/api-keys")
    assert listed_keys.status_code == 200
    assert listed_keys.json()["items"][0]["key_prefix"].startswith("edapi_")
    assert "api_key" not in listed_keys.json()["items"][0]
    assert client.post(
        f"/space/api/v2/worlds/{world_id}/entity-create-tokens",
        json={"name": "removed-old-token"},
    ).status_code == 404
    definition = encode_inventory_resource("entity", _entity())
    operation_id = str(uuid.uuid4())
    body = {
        "operation_id": operation_id,
        "definition_base64": base64.b64encode(definition).decode(),
        "position": {"x_cm": 0, "y_cm": 3200, "z_cm": 204799},
        "yaw_quarter_turns": 3,
        "desired_run_state": "running",
    }

    app.dependency_overrides.pop(get_current_user)
    create_headers = {"Authorization": f"Bearer {api_key}"}
    created = client.post(f"/space/api/v2/worlds/{world_id}/entities", json=body, headers=create_headers)
    repeated = client.post(
        f"/space/api/v2/worlds/{world_id}/entities", json=body, headers=create_headers
    )

    assert created.status_code == 201, created.text
    assert repeated.status_code == 201, repeated.text
    assert created.json()["id"] == repeated.json()["id"]
    assert created.json()["can_control"] is True
    assert created.json()["yaw_quarter_turns"] == 3
    assert "source_kind" not in created.json()
    assert created.json()["can_edit"] is True
    assert db.query(SpaceWorldEntity).count() == 1
    assert "source_kind" not in SpaceWorldEntity.__table__.c
    assert "source_resource_id" not in SpaceWorldEntity.__table__.c
    stored = db.query(SpaceWorldEntity).one()
    assert bytes(stored.content_digest) == hashlib.sha256(bytes(stored.definition)).digest()
    stored_key = db.query(SpaceApiKey).one()
    assert stored_key.id == api_key_id
    assert api_key.startswith(stored_key.key_prefix)
    assert api_key.encode() not in bytes(stored_key.token_hash)
    assert stored_key.last_used_at is not None

    reused = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={**body, "position": {"x_cm": 1, "y_cm": 3200, "z_cm": 204799}},
        headers=create_headers,
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "ENTITY_OPERATION_ID_REUSED"

    # The API key is not a general login token and is immediately
    # invalid after the owner revokes it.
    not_general_auth = client.get(
        f"/space/api/v2/worlds/{world_id}/entities"
        "?center_x_cm=0&center_z_cm=0&radius_cm=100",
        headers=create_headers,
    )
    assert not_general_auth.status_code == 401
    app.dependency_overrides[get_current_user] = lambda: owner
    revoked = client.delete(
        f"/space/api/v2/api-keys/{api_key_id}"
    )
    assert revoked.json() == {"revoked": True, "api_key_id": api_key_id}
    app.dependency_overrides.pop(get_current_user)
    after_revoke = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={**body, "operation_id": str(uuid.uuid4())},
        headers=create_headers,
    )
    assert after_revoke.status_code == 401

    app.dependency_overrides[get_current_user] = lambda: owner
    definition = client.get(
        f"/space/api/v2/worlds/{world_id}/entities/{created.json()['id']}/definition"
    )
    assert definition.status_code == 200
    assert definition.headers["content-type"].startswith("application/x-protobuf")
    assert hashlib.sha256(definition.content).hexdigest() == created.json()["definition_digest"]
    kind, decoded = decode_inventory_resource(definition.content)
    assert kind == "entity"
    assert decoded["name"] == "External Walker"


def test_api_key_scope_and_entity_validation_are_enforced(client, db):
    owner = _user(db, "scope-owner")
    app.dependency_overrides[get_current_user] = lambda: owner
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    _key_id, api_key = _create_api_key(client)
    app.dependency_overrides.pop(get_current_user)
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "operation_id": str(uuid.uuid4()),
        "definition_base64": base64.b64encode(
            encode_inventory_resource("entity", _entity("Validated Agent Entity"))
        ).decode(),
        "position": {"x_cm": 100, "y_cm": 3200, "z_cm": 100},
        "desired_run_state": "running",
    }

    forbidden = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json=body,
        headers=headers,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == {
        "code": "SPACE_API_KEY_SCOPE_REQUIRED",
        "required_scope": "space:entity:run",
    }

    legacy_market_request = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={
            "operation_id": str(uuid.uuid4()),
            "resource_id": "removed-contract",
            "position": {"x_cm": 100, "y_cm": 3200, "z_cm": 100},
        },
        headers=headers,
    )
    assert legacy_market_request.status_code == 422

    invalid = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={
            **body,
            "operation_id": str(uuid.uuid4()),
            "definition_base64": base64.b64encode(b"not-an-inventory-resource").decode(),
            "desired_run_state": "stopped",
        },
        headers=headers,
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "ENTITY_DEFINITION_INVALID"

    created = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={**body, "operation_id": str(uuid.uuid4()), "desired_run_state": "stopped"},
        headers=headers,
    )
    assert created.status_code == 201, created.text


def test_list_wraps_aoi_and_only_owner_can_change_run_state(client, db):
    owner = _user(db, "entity-owner")
    other = _user(db, "entity-other")
    app.dependency_overrides[get_current_user] = lambda: owner
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    _api_key_id, api_key = _create_api_key(client, allow_run=True)
    app.dependency_overrides.pop(get_current_user)
    created = client.post(
        f"/space/api/v2/worlds/{world_id}/entities",
        json={
            "operation_id": str(uuid.uuid4()),
            "definition_base64": base64.b64encode(
                encode_inventory_resource("entity", _entity())
            ).decode(),
            "position": {"x_cm": 0, "y_cm": 3200, "z_cm": 100},
            "desired_run_state": "running",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    ).json()

    app.dependency_overrides[get_current_user] = lambda: owner
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
    assert "source_kind" not in record
    assert "source_resource_id" not in record
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
