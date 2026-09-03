import datetime
import struct
import time
import uuid

import msgpack
import pytest
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from auth import get_current_user
from main import app
from rate_limit import limiter
from routers import space as space_router
from routers import space_realtime
from models import (
    SpaceChunkSnapshot,
    SpacePlayerSnapshot,
    SpaceTerrainMutationBatch,
    SpaceWorld,
    SpaceWorldPlayerProfile,
    SpaceSurfaceZoneSnapshot,
    User,
)
import space_surface


def _user(db, user_id: str, skin_url: str | None):
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        username="Space Tester",
        skin_url=skin_url,
        skin_type="slim",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_space_ping_returns_ok_without_authentication(client):
    response = client.get("/space/api/v2/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_space_ping_is_exempt_from_rate_limit(client):
    for _ in range(70):
        response = client.get("/space/api/v2/ping")
        assert response.status_code == 200


def test_space_realtime_rate_limit_policy_matches_long_lived_sessions():
    heartbeat_endpoint = (
        f"{space_router.space_heartbeat.__module__}."
        f"{space_router.space_heartbeat.__name__}"
    )
    assert heartbeat_endpoint in limiter._exempt_routes
    assert "day" not in space_router.SPACE_POSITION_RATE_LIMIT.lower()
    assert "minute" in space_router.SPACE_POSITION_RATE_LIMIT.lower()
    assert "hour" in space_router.SPACE_POSITION_RATE_LIMIT.lower()


def test_space_bootstrap_requires_shared_login(client):
    response = client.post(
        "/space/api/v2/bootstrap",
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code in (401, 403)
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_space_bootstrap_blocks_user_without_skin(client, db):
    user = _user(db, "space-no-skin", None)
    app.dependency_overrides[get_current_user] = lambda: user

    response = client.post("/space/api/v2/bootstrap")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "SKIN_REQUIRED",
        "message": "You must set a character skin before entering Space.",
        "action_url": "/skin/collection",
    }
    assert db.query(SpaceWorldPlayerProfile).count() == 0


def test_space_bootstrap_returns_ephemeral_world_wide_random_start_without_persisting_it(
    client, db, monkeypatch
):
    skin_url = "https://cdn.entropydrop.com/skins/immutable-player.png"
    user = _user(db, "space-user-001", skin_url)
    app.dependency_overrides[get_current_user] = lambda: user
    starts = iter([
        {"x_cm": 0, "y_cm": 3200, "z_cm": 204799, "yaw_q15": -32767},
        {"x_cm": 1638399, "y_cm": 3200, "z_cm": 0, "yaw_q15": 32767},
    ])
    monkeypatch.setattr(space_router, "_random_initial_position", lambda _world: next(starts))

    first = client.post("/space/api/v2/bootstrap")
    second = client.post("/space/api/v2/bootstrap")

    assert first.status_code == 200
    assert second.status_code == 200
    first_data = first.json()
    second_data = second.json()
    assert first_data["max_online_players"] == 32
    assert first_data["queue_enabled"] is True
    assert first_data["world"]["terrain_generator_version"] == 1
    assert first_data["world"]["surface_snapshot_url"].endswith(
        f'/worlds/{first_data["world"]["id"]}/surface-zones'
    )
    assert first_data["player"]["user_id"] == user.id
    assert first_data["player"]["is_admin"] is False
    assert first_data["player"]["skin_url"] == skin_url
    assert first_data["player"]["skin_type"] == "slim"
    assert first_data["player"]["player_entity_id"] == second_data["player"]["player_entity_id"]
    assert first_data["player"]["resumed"] is False
    assert second_data["player"]["resumed"] is False
    assert {
        "x_cm": first_data["player"]["start_x_cm"],
        "y_cm": first_data["player"]["start_y_cm"],
        "z_cm": first_data["player"]["start_z_cm"],
        "yaw_q15": first_data["player"]["start_yaw_q15"],
    } == {"x_cm": 0, "y_cm": 3200, "z_cm": 204799, "yaw_q15": -32767}
    assert {
        "x_cm": second_data["player"]["start_x_cm"],
        "y_cm": second_data["player"]["start_y_cm"],
        "z_cm": second_data["player"]["start_z_cm"],
        "yaw_q15": second_data["player"]["start_yaw_q15"],
    } == {"x_cm": 1638399, "y_cm": 3200, "z_cm": 0, "yaw_q15": 32767}
    assert db.query(SpaceWorldPlayerProfile).count() == 1
    assert db.query(SpacePlayerSnapshot).count() == 0
    assert not any(column.name.startswith("spawn_") for column in SpaceWorldPlayerProfile.__table__.columns)


def test_space_random_start_covers_both_wrapped_world_seams(monkeypatch):
    world = SpaceWorld(width_chunks=1024, length_chunks=128)
    requested_bounds = []

    def last_value(bound):
        requested_bounds.append(bound)
        return bound - 1

    monkeypatch.setattr(space_router.secrets, "randbelow", last_value)
    assert space_router._random_initial_position(world) == {
        "x_cm": 1638399,
        "y_cm": 3200,
        "z_cm": 204799,
        "yaw_q15": 32767,
    }
    assert requested_bounds == [1638400, 204800, 65535]

    monkeypatch.setattr(space_router.secrets, "randbelow", lambda _bound: 0)
    assert space_router._random_initial_position(world) == {
        "x_cm": 0,
        "y_cm": 3200,
        "z_cm": 0,
        "yaw_q15": -32767,
    }


def test_space_admission_queues_fifo_supports_cancel_and_promotes(client, db, monkeypatch):
    def redis_unavailable(*_args, **_kwargs):
        raise RuntimeError("redis unavailable in admission test")

    monkeypatch.setattr(space_realtime._ticket_redis, "eval", redis_unavailable)
    space_realtime._fallback_admission.reset()

    first_user = _user(db, "space-queue-01", "https://cdn.entropydrop.com/skins/queue-1.png")
    second_user = _user(db, "space-queue-02", "https://cdn.entropydrop.com/skins/queue-2.png")
    third_user = _user(db, "space-queue-03", "https://cdn.entropydrop.com/skins/queue-3.png")

    app.dependency_overrides[get_current_user] = lambda: first_user
    bootstrap = client.post("/space/api/v2/bootstrap").json()
    world_id = bootstrap["world"]["id"]
    world = db.query(SpaceWorld).filter(SpaceWorld.id == world_id).one()
    world.max_online_players = 1
    db.commit()

    first = client.post(f"/space/api/v2/worlds/{world_id}/admission")
    assert first.json() == {"state": "admitted", "position": None, "poll_after_ms": 2000}
    previous_expiry = time.time() + 1
    space_realtime._fallback_admission.worlds[world_id]["active"][first_user.id] = previous_expiry
    renewed = client.post(f"/space/api/v2/worlds/{world_id}/admission")
    assert renewed.json()["state"] == "admitted"
    assert (
        space_realtime._fallback_admission.worlds[world_id]["active"][first_user.id]
        > previous_expiry + 30
    )

    app.dependency_overrides[get_current_user] = lambda: second_user
    assert client.post("/space/api/v2/bootstrap").status_code == 200
    second = client.post(f"/space/api/v2/worlds/{world_id}/admission")
    assert second.json() == {"state": "queued", "position": 1, "poll_after_ms": 2000}
    blocked_ticket = client.post(f"/space/api/v2/worlds/{world_id}/join-ticket")
    assert blocked_ticket.status_code == 409
    assert blocked_ticket.json()["detail"] == {"code": "SPACE_QUEUE_WAIT", "position": 1}

    app.dependency_overrides[get_current_user] = lambda: third_user
    assert client.post("/space/api/v2/bootstrap").status_code == 200
    third = client.post(f"/space/api/v2/worlds/{world_id}/admission")
    assert third.json()["position"] == 2
    cancelled = client.delete(f"/space/api/v2/worlds/{world_id}/admission")
    assert cancelled.json()["state"] == "cancelled"

    space_realtime._release_admission(world_id, first_user.id)
    app.dependency_overrides[get_current_user] = lambda: second_user
    promoted = client.post(f"/space/api/v2/worlds/{world_id}/admission")
    assert promoted.json() == {"state": "admitted", "position": None, "poll_after_ms": 2000}


def test_space_realtime_ticket_and_binary_pose_stream(client, db, monkeypatch):
    alice = _user(db, "space-ws-alice", "https://cdn.entropydrop.com/skins/alice.png")
    bob = _user(db, "space-ws-bob", "https://cdn.entropydrop.com/skins/bob.png")

    app.dependency_overrides[get_current_user] = lambda: alice
    alice_bootstrap = client.post("/space/api/v2/bootstrap").json()
    world_id = alice_bootstrap["world"]["id"]
    alice_ticket = client.post(f"/space/api/v2/worlds/{world_id}/join-ticket")
    app.dependency_overrides[get_current_user] = lambda: bob
    client.post("/space/api/v2/bootstrap")
    bob_ticket = client.post(f"/space/api/v2/worlds/{world_id}/join-ticket")

    assert alice_ticket.status_code == 200
    assert bob_ticket.status_code == 200
    assert alice_ticket.json()["expires_in_seconds"] == 30
    assert alice_ticket.json()["websocket_url"].endswith("/space/ws/v2")

    realtime_session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=db.get_bind(),
    )
    monkeypatch.setattr(space_realtime, "SessionLocal", realtime_session_factory)
    monkeypatch.setattr(space_realtime.settings, "SPACE_REALTIME_REDIS_FANOUT_ENABLED", False)

    websocket_options = {
        "headers": {"origin": "http://localhost:5173"},
        "subprotocols": ["space-relay-v1"],
    }
    with client.websocket_connect("/space/ws/v2", **websocket_options) as alice_socket:
        alice_socket.send_bytes(msgpack.packb({
            "type": "hello",
            "ticket": alice_ticket.json()["ticket"],
        }, use_bin_type=True))
        alice_hello = msgpack.unpackb(alice_socket.receive_bytes(), raw=False)
        assert alice_hello["type"] == "hello"
        assert alice_hello["input_hz"] == 20
        assert alice_hello["snapshot_hz"] == 10

        with client.websocket_connect("/space/ws/v2", **websocket_options) as bob_socket:
            bob_socket.send_bytes(msgpack.packb({
                "type": "hello",
                "ticket": bob_ticket.json()["ticket"],
            }, use_bin_type=True))
            assert msgpack.unpackb(bob_socket.receive_bytes(), raw=False)["type"] == "hello"

            alice_socket.send_bytes(msgpack.packb({
                "type": "pose",
                "sequence": 1,
                "x_cm": 123456,
                "y_cm": 4321,
                "z_cm": 65432,
                "yaw_q15": 1234,
                "pitch_q15": -4321,
            }, use_bin_type=True))
            bob_socket.send_bytes(msgpack.packb({
                "type": "pose",
                "sequence": 1,
                "x_cm": 123556,
                "y_cm": 4321,
                "z_cm": 65532,
                "yaw_q15": -1234,
                "pitch_q15": 4321,
            }, use_bin_type=True))

            alice_state = {"players": []}
            while {player["user_id"] for player in alice_state["players"]} != {alice.id, bob.id}:
                alice_state = msgpack.unpackb(alice_socket.receive_bytes(), raw=False)
            bob_state = {"players": []}
            while {player["user_id"] for player in bob_state["players"]} != {alice.id, bob.id}:
                bob_state = msgpack.unpackb(bob_socket.receive_bytes(), raw=False)

            assert alice_state["type"] == "state"
            assert bob_state["type"] == "state"
            alice_view = {player["user_id"]: player for player in alice_state["players"]}
            bob_view = {player["user_id"]: player for player in bob_state["players"]}
            assert alice_view[alice.id]["is_self"] is True
            assert alice_view[bob.id]["is_self"] is False
            assert bob_view[bob.id]["is_self"] is True
            assert alice_view[alice.id]["x_cm"] == 123456

            bob_socket.send_bytes(msgpack.packb({"type": "leave"}, use_bin_type=True))
            with pytest.raises(WebSocketDisconnect):
                bob_socket.receive_bytes()

        alice_socket.send_bytes(msgpack.packb({"type": "leave"}, use_bin_type=True))
        with pytest.raises(WebSocketDisconnect):
            alice_socket.receive_bytes()

    db.rollback()
    db.expire_all()
    snapshot = db.query(SpacePlayerSnapshot).filter(
        SpacePlayerSnapshot.world_id == world_id,
        SpacePlayerSnapshot.user_id == alice.id,
    ).one()
    world = db.query(SpaceWorld).filter(SpaceWorld.id == world_id).one()
    saved_position = space_router._decode_player_snapshot(
        snapshot,
        world,
    )
    assert saved_position == {
        "x_cm": 123456,
        "y_cm": 4321,
        "z_cm": 65432,
        "yaw_q15": 1234,
        "pitch_q15": -4321,
    }


def test_space_bootstrap_restores_latest_position_as_start_state(client, db):
    user = _user(db, "pos-user-001", "https://cdn.entropydrop.com/skins/position.png")
    app.dependency_overrides[get_current_user] = lambda: user
    first = client.post("/space/api/v2/bootstrap")
    first_player = first.json()["player"]
    world_id = first.json()["world"]["id"]

    assert first.status_code == 200
    assert first_player["resumed"] is False
    assert all(first_player[field] is not None for field in (
        "start_x_cm", "start_y_cm", "start_z_cm", "start_yaw_q15"
    ))

    position = {"x_cm": 123456, "y_cm": 4587, "z_cm": 65432, "yaw_q15": -12345}
    saved = client.put(
        f"/space/api/v2/worlds/{world_id}/players/me/position",
        json=position,
    )
    latest_position = {"x_cm": 123499, "y_cm": 4601, "z_cm": 65480, "yaw_q15": 2345}
    saved_latest = client.put(
        f"/space/api/v2/worlds/{world_id}/players/me/position",
        json=latest_position,
    )
    resumed = client.post("/space/api/v2/bootstrap")

    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert saved_latest.status_code == 200
    assert saved_latest.json()["revision"] == 2
    assert resumed.status_code == 200
    resumed_player = resumed.json()["player"]
    assert {
        "x_cm": resumed_player["start_x_cm"],
        "y_cm": resumed_player["start_y_cm"],
        "z_cm": resumed_player["start_z_cm"],
        "yaw_q15": resumed_player["start_yaw_q15"],
    } == latest_position
    assert resumed_player["resumed"] is True
    assert db.query(SpacePlayerSnapshot).count() == 1

    second_user = _user(db, "pos-user-002", "https://cdn.entropydrop.com/skins/position-2.png")
    app.dependency_overrides[get_current_user] = lambda: second_user
    second_player = client.post("/space/api/v2/bootstrap").json()["player"]
    assert second_player["resumed"] is False
    assert all(second_player[field] is not None for field in (
        "start_x_cm", "start_y_cm", "start_z_cm", "start_yaw_q15"
    ))


def test_space_position_rejects_out_of_bounds_checkpoint(client, db):
    user = _user(db, "pos-limit-001", "https://cdn.entropydrop.com/skins/position-limit.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]

    response = client.put(
        f"/space/api/v2/worlds/{world_id}/players/me/position",
        json={"x_cm": -1, "y_cm": 3200, "z_cm": 100, "yaw_q15": 0},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "PLAYER_POSITION_OUT_OF_BOUNDS"}
    assert db.query(SpacePlayerSnapshot).count() == 0


def test_space_heartbeat_exchanges_two_active_players(client, db):
    alice = _user(db, "live-alice-001", "https://cdn.entropydrop.com/skins/alice.png")
    app.dependency_overrides[get_current_user] = lambda: alice
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    alice_heartbeat = client.post(
        f"/space/api/v2/worlds/{world_id}/heartbeat",
        json={"x_cm": 10000, "y_cm": 1800, "z_cm": 10000, "yaw_q15": 100, "since_terrain_revision": 0},
    )
    assert alice_heartbeat.status_code == 200

    bob = _user(db, "live-bob-0001", "https://cdn.entropydrop.com/skins/bob.png")
    app.dependency_overrides[get_current_user] = lambda: bob
    assert client.post("/space/api/v2/bootstrap").status_code == 200
    bob_heartbeat = client.post(
        f"/space/api/v2/worlds/{world_id}/heartbeat",
        json={"x_cm": 10100, "y_cm": 1800, "z_cm": 10000, "yaw_q15": -100, "since_terrain_revision": 0},
    )

    assert bob_heartbeat.status_code == 200
    players = {player["user_id"]: player for player in bob_heartbeat.json()["players"]}
    assert set(players) == {alice.id, bob.id}
    assert players[alice.id]["is_self"] is False
    assert players[bob.id]["is_self"] is True
    assert players[alice.id]["x"] == 100.0

    terrain_only = client.post(
        f"/space/api/v2/worlds/{world_id}/heartbeat",
        json={"since_terrain_revision": 0, "include_players": False},
    )
    assert terrain_only.status_code == 200
    assert terrain_only.json()["players"] == []


def test_space_heartbeat_cursor_does_not_miss_first_edit_in_another_chunk(client, db):
    user = _user(db, "live-block-001", "https://cdn.entropydrop.com/skins/live-block.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]

    first_apply = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "mutations": [{"kind": "set_standard", "x": 1, "y": 20, "z": 1, "block": 1, "color": 1}],
        },
    )
    assert first_apply.status_code == 200
    first_poll = client.post(
        f"/space/api/v2/worlds/{world_id}/heartbeat",
        json={"since_terrain_revision": 0},
    )
    cursor = first_poll.json()["max_terrain_revision"]
    assert [(chunk["chunk_x"], chunk["chunk_z"]) for chunk in first_poll.json()["terrain_chunks"]] == [(0, 0)]

    second_apply = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "mutations": [{"kind": "set_standard", "x": 33, "y": 20, "z": 1, "block": 1, "color": 2}],
        },
    )
    assert second_apply.status_code == 200
    second_poll = client.post(
        f"/space/api/v2/worlds/{world_id}/heartbeat",
        json={"since_terrain_revision": cursor},
    )

    assert [(chunk["chunk_x"], chunk["chunk_z"]) for chunk in second_poll.json()["terrain_chunks"]] == [(2, 0)]
    assert second_poll.json()["max_terrain_revision"] > cursor


def test_space_heartbeat_filters_incremental_chunks_to_player_aoi(client, db):
    user = _user(db, "live-aoi-0001", "https://cdn.entropydrop.com/skins/live-aoi.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]

    near_apply = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "mutations": [
                {"kind": "set_standard", "x": 1, "y": 20, "z": 1, "block": 1, "color": 1}
            ],
        },
    )
    far_apply = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "mutations": [
                {"kind": "set_standard", "x": 10 * 16, "y": 20, "z": 1, "block": 1, "color": 2}
            ],
        },
    )
    assert near_apply.status_code == 200
    assert far_apply.status_code == 200

    poll = client.post(
        f"/space/api/v2/worlds/{world_id}/heartbeat",
        json={
            "since_terrain_revision": 0,
            "include_players": False,
            "center_chunk_x": 0,
            "center_chunk_z": 0,
            "terrain_radius_chunks": 1,
        },
    )

    assert poll.status_code == 200
    assert [(chunk["chunk_x"], chunk["chunk_z"]) for chunk in poll.json()["terrain_chunks"]] == [
        (0, 0)
    ]
    assert poll.json()["max_terrain_revision"] == near_apply.json()["terrain_revision"]


def test_space_terrain_edits_are_durable_and_visible_to_another_browser_user(client, db):
    first_user = _user(db, "space-editor-001", "https://cdn.entropydrop.com/skins/editor.png")
    app.dependency_overrides[get_current_user] = lambda: first_user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    batch_id = str(uuid.uuid4())
    payload = {
        "batch_id": batch_id,
        "mutations": [
            {"kind": "set_standard", "x": 100, "y": 50, "z": 100, "block": 1, "color": 0x123456},
            {"kind": "set_standard", "x": 101, "y": 50, "z": 100, "block": 0, "color": 0xF2A93B},
            {"kind": "set_micro", "mx": 506, "my": 251, "mz": 503, "color": 0xABCDEF, "part": "tip"},
        ],
    }

    first_apply = client.post(f"/space/api/v2/worlds/{world_id}/terrain-edits/batches", json=payload)
    duplicate_apply = client.post(f"/space/api/v2/worlds/{world_id}/terrain-edits/batches", json=payload)

    assert first_apply.status_code == 200
    assert duplicate_apply.status_code == 200
    assert duplicate_apply.json() == first_apply.json()
    assert first_apply.json()["applied"] == 3
    assert db.query(SpaceChunkSnapshot).count() == 1
    assert db.query(SpaceTerrainMutationBatch).count() == 1

    second_user = _user(db, "space-viewer-01", "https://cdn.entropydrop.com/skins/viewer.png")
    app.dependency_overrides[get_current_user] = lambda: second_user
    assert client.post("/space/api/v2/bootstrap").status_code == 200

    loaded = client.get(f"/space/api/v2/worlds/{world_id}/terrain-edits")
    assert loaded.status_code == 200
    chunks = loaded.json()["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["standard"] == [
        [100, 50, 100, 1, 0x123456],
        [101, 50, 100, 0, 0xF2A93B],
    ]
    assert chunks[0]["micro"] == [[506, 251, 503, 0xABCDEF, "tip"]]


def test_space_terrain_height_is_256_metres(client, db):
    user = _user(db, "space-height-01", "https://cdn.entropydrop.com/skins/height.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]

    top = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "mutations": [
                {"kind": "set_standard", "x": 1, "y": 255, "z": 1, "block": 1, "color": 1},
                {"kind": "set_micro", "mx": 10, "my": 1279, "mz": 10, "color": 2},
            ],
        },
    )
    assert top.status_code == 200, top.text

    for mutation in (
        {"kind": "set_standard", "x": 1, "y": 256, "z": 1, "block": 1, "color": 1},
        {"kind": "set_micro", "mx": 5, "my": 1280, "mz": 5, "color": 2},
    ):
        rejected = client.post(
            f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
            json={"batch_id": str(uuid.uuid4()), "mutations": [mutation]},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "TERRAIN_POSITION_OUT_OF_BOUNDS"


def test_space_surface_zone_snapshot_matches_browser_generator_and_serves_immutable_bytes(client, db):
    user = _user(db, "space-surface-1", "https://cdn.entropydrop.com/skins/surface.png")
    app.dependency_overrides[get_current_user] = lambda: user
    bootstrap = client.post("/space/api/v2/bootstrap").json()
    world = db.query(SpaceWorld).filter_by(id=bootstrap["world"]["id"]).one()

    generator = space_surface.TerrainSurfaceGenerator(world.seed, 16384, 2048)
    assert [generator.sample_height(x, z) for x, z in (
        (0, 0), (8192, 1024), (16382, 2046), (1234, 567)
    )] == [17, 16, 18, 15]

    row = space_surface.generate_surface_zone(db, world, 0, 0)
    assert row is not None
    assert row.uncompressed_size == 32 + 32 * 32 * 8 * 8 * 5
    manifest = client.get(bootstrap["world"]["surface_snapshot_url"])
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["samples_per_chunk_axis"] == 8
    assert body["complete"] is False
    assert [(zone["zone_x"], zone["zone_z"]) for zone in body["zones"]] == [(0, 0)]

    downloaded = client.get(body["zones"][0]["url"])
    assert downloaded.status_code == 200
    assert downloaded.content[:4] == b"EDSZ"
    assert len(downloaded.content) == row.uncompressed_size
    assert downloaded.headers["etag"] == f'"{row.content_hash.hex()}"'
    assert "immutable" in downloaded.headers["cache-control"]


def test_terrain_edits_mark_their_surface_zone_snapshot_dirty(client, db):
    user = _user(db, "space-surface-2", "https://cdn.entropydrop.com/skins/surface-dirty.png")
    app.dependency_overrides[get_current_user] = lambda: user
    bootstrap = client.post("/space/api/v2/bootstrap").json()
    world = db.query(SpaceWorld).filter_by(id=bootstrap["world"]["id"]).one()
    space_surface.generate_surface_zone(db, world, 0, 0)

    applied = client.post(
        f'/space/api/v2/worlds/{world.id}/terrain-edits/batches',
        json={
            "batch_id": str(uuid.uuid4()),
            "mutations": [
                {"kind": "set_standard", "x": 3, "y": 255, "z": 3, "block": 1, "color": 0x123456},
            ],
        },
    )
    assert applied.status_code == 200
    row = db.query(SpaceSurfaceZoneSnapshot).filter_by(
        world_id=world.id, zone_x=0, zone_z=0
    ).one()
    assert row.dirty is True
    assert client.get(bootstrap["world"]["surface_snapshot_url"]).json()["zones"] == []

    rebuilt = space_surface.generate_surface_zone(db, world, 0, 0)
    assert rebuilt is not None
    assert rebuilt.dirty is False
    raw = space_surface.decode_surface_zone_row(rebuilt)
    record_index = 1 * space_surface.SURFACE_SAMPLES_PER_CHUNK_AXIS + 1
    assert struct.unpack_from(
        "<HBBB",
        raw,
        space_surface.SURFACE_HEADER_BYTES + record_index * space_surface.SURFACE_RECORD_BYTES,
    ) == (
        1280, 0x12, 0x34, 0x56
    )


def test_space_terrain_batch_rejects_more_than_256_mutations(client, db):
    user = _user(db, "space-limit-001", "https://cdn.entropydrop.com/skins/limit.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    mutations = [
        {"kind": "set_standard", "x": index, "y": 80, "z": 20, "block": 1, "color": 0x123456}
        for index in range(257)
    ]

    response = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={"batch_id": str(uuid.uuid4()), "mutations": mutations},
    )

    assert response.status_code == 422
    assert db.query(SpaceChunkSnapshot).count() == 0
    assert db.query(SpaceTerrainMutationBatch).count() == 0


def test_space_terrain_snapshot_pages_use_a_stable_chunk_cursor(client, db):
    user = _user(db, "space-pages-001", "https://cdn.entropydrop.com/skins/pages.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    response = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "mutations": [
                {"kind": "set_standard", "x": 1, "y": 80, "z": 1, "block": 1, "color": 1},
                {"kind": "set_standard", "x": 33, "y": 80, "z": 1, "block": 1, "color": 2},
            ],
        },
    )
    assert response.status_code == 200

    first = client.get(f"/space/api/v2/worlds/{world_id}/terrain-edits?limit=1").json()
    second = client.get(
        f"/space/api/v2/worlds/{world_id}/terrain-edits",
        params={"limit": 1, "cursor": first["next_cursor"]},
    ).json()

    assert [(chunk["chunk_x"], chunk["chunk_z"]) for chunk in first["chunks"]] == [(0, 0)]
    assert first["next_cursor"] == "0,0"
    assert [(chunk["chunk_x"], chunk["chunk_z"]) for chunk in second["chunks"]] == [(2, 0)]
    assert second["next_cursor"] is None


def test_space_terrain_snapshot_aoi_wraps_at_world_edges(client, db):
    user = _user(db, "space-aoi-0001", "https://cdn.entropydrop.com/skins/aoi.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    response = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "mutations": [
                {"kind": "set_standard", "x": 1, "y": 80, "z": 1, "block": 1, "color": 1},
                {"kind": "set_standard", "x": 1023 * 16, "y": 80, "z": 1, "block": 1, "color": 2},
                {"kind": "set_standard", "x": 10 * 16, "y": 80, "z": 1, "block": 1, "color": 3},
            ],
        },
    )
    assert response.status_code == 200

    loaded = client.get(
        f"/space/api/v2/worlds/{world_id}/terrain-edits",
        params={"center_chunk_x": 0, "center_chunk_z": 0, "radius_chunks": 1},
    )

    assert loaded.status_code == 200
    assert [(chunk["chunk_x"], chunk["chunk_z"]) for chunk in loaded.json()["chunks"]] == [
        (0, 0),
        (1023, 0),
    ]


def test_space_chunk_snapshots_use_zstd_when_it_reduces_payload(client, db):
    user = _user(db, "space-zstd-001", "https://cdn.entropydrop.com/skins/zstd.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    mutations = [
        {
            "kind": "set_standard",
            "x": index % 16,
            "y": 40 + index // 16,
            "z": 1,
            "block": 1,
            "color": 0x123456,
        }
        for index in range(192)
    ]

    applied = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={"batch_id": str(uuid.uuid4()), "mutations": mutations},
    )
    assert applied.status_code == 200
    snapshot = db.query(SpaceChunkSnapshot).one()
    assert snapshot.codec == space_router.SPACE_CHUNK_CODEC_ZSTD
    assert len(snapshot.payload) < snapshot.uncompressed_size

    loaded = client.get(f"/space/api/v2/worlds/{world_id}/terrain-edits").json()
    assert len(loaded["chunks"][0]["standard"]) == len(mutations)


def test_space_epoch_one_batch_receipts_are_compact_and_reject_expired_replays(client, db):
    user = _user(db, "space-dedupe-01", "https://cdn.entropydrop.com/skins/dedupe.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    batch_id = str(uuid.uuid4())
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    payload = {
        "batch_id": batch_id,
        "dedupe_epoch": 1,
        "created_at_ms": now_ms,
        "mutations": [
            {"kind": "set_standard", "x": 1, "y": 80, "z": 1, "block": 1, "color": 1},
        ],
    }

    first = client.post(f"/space/api/v2/worlds/{world_id}/terrain-edits/batches", json=payload)
    duplicate = client.post(f"/space/api/v2/worlds/{world_id}/terrain-edits/batches", json=payload)
    assert first.status_code == 200
    assert duplicate.json() == first.json()
    receipt = db.query(SpaceTerrainMutationBatch).filter_by(batch_id=batch_id).one()
    assert receipt.dedupe_epoch == 1
    assert receipt.client_created_at is not None
    assert "world_id" not in receipt.result
    assert "batch_id" not in receipt.result

    expired_ms = int((
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)
    ).timestamp() * 1000)
    expired = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={**payload, "batch_id": str(uuid.uuid4()), "created_at_ms": expired_ms},
    )
    assert expired.status_code == 409
    assert expired.json()["detail"]["code"] == "TERRAIN_BATCH_EXPIRED"


def test_space_expired_receipt_cleanup_is_bounded(client, db, monkeypatch):
    user = _user(db, "space-cleanup01", "https://cdn.entropydrop.com/skins/cleanup.png")
    app.dependency_overrides[get_current_user] = lambda: user
    world_id = client.post("/space/api/v2/bootstrap").json()["world"]["id"]
    expired_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)
    db.add_all([
        SpaceTerrainMutationBatch(
            world_id=world_id,
            batch_id=str(uuid.uuid4()),
            actor_user_id=user.id,
            dedupe_epoch=1,
            client_created_at=expired_at,
            result={"applied": 1},
        )
        for _ in range(space_router.TERRAIN_RECEIPT_CLEANUP_BATCH_SIZE + 1)
    ])
    db.commit()
    monkeypatch.setattr(space_router, "_last_receipt_cleanup_at", 0.0)

    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    fresh = client.post(
        f"/space/api/v2/worlds/{world_id}/terrain-edits/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "dedupe_epoch": 1,
            "created_at_ms": now_ms,
            "mutations": [
                {"kind": "set_standard", "x": 1, "y": 80, "z": 1, "block": 1, "color": 1},
            ],
        },
    )

    assert fresh.status_code == 200
    assert db.query(SpaceTerrainMutationBatch).filter(
        SpaceTerrainMutationBatch.client_created_at < datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=30)
    ).count() == 1
