import datetime as dt
import re
import uuid
from urllib.parse import urljoin, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import auth
import models
from main import app
from config import settings
from routers import space, space_agent, space_entities
from tests.test_space_external import setup


def save_position(db, user_id, world_id, *, age=1, state=None):
    position = {"x_cm": 744710, "y_cm": 1500, "z_cm": 55290,
                "yaw_q15": -12345, "pitch_q15": 678}
    snapshot = models.SpacePlayerSnapshot(world_id=world_id, user_id=user_id,
        revision=1, last_event_id=0, state_version=1,
        state=state if state is not None else space._encode_player_snapshot(position),
        updated_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age))
    db.add(snapshot)
    db.commit()
    return position


def test_existing_key_reads_only_its_owner_and_preserves_checkpoint(client, db):
    owner, world, headers, key_id = setup(client, db)
    position = save_position(db, owner.id, world)
    # An unrelated caller-supplied identifier must never select another user.
    for path in ['/space/api/v2/players/me/position', f'/space/api/v2/worlds/{world}/players/me/position']:
        response = client.get(path + '?user_id=someone-else', headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['world_id'] == world
        assert body['position'] == {k: position[k] for k in ('x_cm', 'y_cm', 'z_cm')}
        assert body['yaw_q15'] == position['yaw_q15']
        assert body['pitch_q15'] == position['pitch_q15']
        assert body['source'] == 'checkpoint'
        assert body['stale'] is False and 0 <= body['age_seconds'] < 30
        assert body['updated_at'].endswith('Z')
        assert response.headers['cache-control'] == 'no-store'
        assert not {'username', 'email', 'user_id', 'api_key'} & body.keys()
    assert db.query(models.SpacePlayerSnapshot).one().revision == 1
    assert db.get(models.SpaceApiKey, key_id).last_used_at is not None


def test_login_token_reads_own_position(client, db):
    owner, world, _, _ = setup(client, db)
    save_position(db, owner.id, world)
    token = auth.create_access_token({'sub': owner.id})
    response = client.get('/space/api/v2/players/me/position', headers={'Authorization': 'Bearer ' + token})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('age', [31, 7200, -60])
def test_stale_or_future_pose_is_explicit(client, db, age):
    owner, world, headers, _ = setup(client, db)
    save_position(db, owner.id, world, age=age)
    body = client.get('/space/api/v2/players/me/position', headers=headers).json()
    assert body['stale'] is True
    assert body['stale_after_seconds'] == 30
    assert body['age_seconds'] >= 0


@pytest.mark.parametrize('state', [None, b'not-json', b'{"position":{}}'])
def test_missing_or_corrupt_pose_never_spawns_or_returns_someone_else(client, db, state):
    owner, world, headers, _ = setup(client, db)
    if state is not None:
        save_position(db, owner.id, world, state=state)
    from tests.test_space_entities import _user
    other = _user(db, 'another-player')
    save_position(db, other.id, world)
    response = client.get('/space/api/v2/players/me/position?user_id=another-player', headers=headers)
    assert response.status_code == 404
    assert response.json()['detail']['code'] == 'PLAYER_POSITION_UNAVAILABLE'
    assert response.headers['cache-control'] == 'no-store'
    assert db.query(models.SpacePlayerSnapshot).filter_by(user_id=owner.id).count() == int(state is not None)


def test_legacy_key_credentials_membership_and_revocation(client, db):
    owner, world, headers, key_id = setup(client, db)
    url = '/space/api/v2/players/me/position'
    assert client.get(url).status_code == 401
    assert client.get(url, headers={'Authorization': 'Bearer edapi_invalid'}).status_code == 401
    assert client.get(f'/space/api/v2/worlds/{uuid.uuid4()}/players/me/position', headers=headers).status_code == 404
    key = db.get(models.SpaceApiKey, key_id)
    key.scopes = ['space:entity:run']
    db.commit()
    save_position(db, owner.id, world)
    assert client.get(url, headers=headers).status_code == 200
    key.scopes = []
    db.commit()
    assert client.get(url, headers=headers).status_code == 200
    db.query(models.SpaceWorldPlayerProfile).filter_by(user_id=owner.id).delete()
    db.commit()
    denied = client.get(url, headers=headers)
    assert denied.status_code == 403
    assert denied.json()['detail']['code'] == 'WORLD_MEMBERSHIP_REQUIRED'
    db.delete(key)
    db.commit()
    assert client.get(url, headers=headers).status_code == 401


def test_public_markdown_links_work_without_auth_and_do_not_expose_files(client):
    def no_auth():
        raise AssertionError('Public docs must not authenticate')
    app.dependency_overrides[space_entities._entity_creator] = no_auth
    app.dependency_overrides[auth.get_current_user] = no_auth
    for name, (_, content_type) in space_agent.PUBLIC_FILES.items():
        response = client.get('/space/agent/' + name)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith(content_type)
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['cache-control'].startswith('public')
        assert len(response.content) > 100
    for name, (_, content_type) in space_agent.PUBLIC_FILES.items():
        if content_type != 'text/markdown':
            continue
        url = '/space/agent/' + name
        document = client.get(url).text
        assert '[spaceAPI](' in document and '[entityAPI](' in document
        for target in re.findall(r'\]\(<?([^)>]+)>?\)', document):
            resolved = urlsplit(urljoin(url, target)).path
            if not target.startswith(('http:', 'https:')):
                assert resolved.startswith('/space/agent/'), (url, target)
                assert client.get(resolved).status_code == 200, (url, target)
    legacy = client.get('/space/agent/references/script-api-v2.md', follow_redirects=False)
    assert legacy.status_code == 308
    assert legacy.headers['location'] == '/space/agent/entityAPI.md'
    assert client.get(legacy.headers['location']).text.startswith('# entityAPI V2')
    for name in ['.env', 'references/../../.env', '%2e%2e%2f.env', 'references/private.py', 'missing.md']:
        assert client.get('/space/agent/' + name).status_code == 404


def test_standalone_identity_uses_remote_key_owner(client, db, monkeypatch):
    owner, world, headers, _ = setup(client, db)
    save_position(db, owner.id, world)
    calls = []
    def identity(db, credential, *, allow_api_key):
        calls.append((credential, allow_api_key))
        return owner, ['space:entity:create']
    monkeypatch.setattr(settings, 'SPACE_STANDALONE', True)
    monkeypatch.setattr(space_entities.auth, 'resolve_identity', identity, raising=False)
    response = client.get('/space/api/v2/players/me/position', headers=headers)
    assert response.status_code == 200
    assert calls == [(headers['Authorization'].removeprefix('Bearer '), True)]
