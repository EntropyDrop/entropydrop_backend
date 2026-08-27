import uuid

from auth import get_current_user
from main import app
from models import SpaceChunkSnapshot, SpaceTerrainMutationBatch, SpaceWorldPlayerProfile, User


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


def test_space_bootstrap_reuses_user_skin_and_stable_random_spawn(client, db):
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
    assert {
        key: first_data["player"][key]
        for key in ("spawn_x_cm", "spawn_y_cm", "spawn_z_cm", "spawn_yaw_q15")
    } == {
        key: second_data["player"][key]
        for key in ("spawn_x_cm", "spawn_y_cm", "spawn_z_cm", "spawn_yaw_q15")
    }
    assert db.query(SpaceWorldPlayerProfile).count() == 1


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
