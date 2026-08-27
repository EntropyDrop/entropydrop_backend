from auth import get_current_user
from main import app
from models import SpaceWorldPlayerProfile, User


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
