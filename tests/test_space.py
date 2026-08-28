import uuid

from auth import get_current_user
from main import app
from rate_limit import limiter
from routers import space as space_router
from models import (
    SpaceChunkSnapshot,
    SpacePlayerSnapshot,
    SpaceTerrainMutationBatch,
    SpaceWorldPlayerProfile,
    User,
)


def _user(db, user_id: str, skin_url: str | None):
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        username="Space Tester",
        minecraft_skin_url=skin_url,
        minecraft_skin_model="slim",
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
    response = client.post("/space/api/v2/bootstrap")
    assert response.status_code in (401, 403)


def test_space_bootstrap_blocks_user_without_skin(client, db):
    user = _user(db, "space-no-skin", None)
    app.dependency_overrides[get_current_user] = lambda: user

    response = client.post("/space/api/v2/bootstrap")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "SKIN_REQUIRED",
        "message": "进入 Space 前需要先设置角色皮肤。",
        "action_url": "/skin/edit",
    }
    assert db.query(SpaceWorldPlayerProfile).count() == 0


def test_space_bootstrap_reuses_identity_without_persisting_random_start(client, db):
    skin_url = "https://cdn.entropydrop.com/skins/immutable-player.png"
    user = _user(db, "space-user-001", skin_url)
    app.dependency_overrides[get_current_user] = lambda: user

    first = client.post("/space/api/v2/bootstrap")
    second = client.post("/space/api/v2/bootstrap")

    assert first.status_code == 200
    assert second.status_code == 200
    first_data = first.json()
    second_data = second.json()
    assert first_data["max_online_players"] == 32
    assert first_data["queue_enabled"] is True
    assert first_data["world"]["terrain_generator_version"] == 1
    assert first_data["player"]["user_id"] == user.id
    assert first_data["player"]["minecraft_skin_url"] == skin_url
    assert first_data["player"]["minecraft_skin_model"] == "slim"
    assert first_data["player"]["player_entity_id"] == second_data["player"]["player_entity_id"]
    assert first_data["player"]["resumed"] is False
    assert second_data["player"]["resumed"] is False
    assert first_data["player"]["start_y_cm"] is None
    assert first_data["player"]["start_x_cm"] is None
    assert first_data["player"]["start_z_cm"] is None
    assert db.query(SpaceWorldPlayerProfile).count() == 1
    assert not any(column.name.startswith("spawn_") for column in SpaceWorldPlayerProfile.__table__.columns)


def test_space_bootstrap_restores_latest_position_as_start_state(client, db):
    user = _user(db, "pos-user-001", "https://cdn.entropydrop.com/skins/position.png")
    app.dependency_overrides[get_current_user] = lambda: user
    first = client.post("/space/api/v2/bootstrap")
    first_player = first.json()["player"]
    world_id = first.json()["world"]["id"]

    assert first.status_code == 200
    assert first_player["resumed"] is False
    assert first_player["start_x_cm"] is None

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
    assert second_player["start_y_cm"] is None


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
