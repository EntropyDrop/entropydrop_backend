import pytest
from auth import get_current_user
from fastapi.security import HTTPAuthorizationCredentials
from main import app
from models import SpaceApiKey
from routers import space_entities
from routers.space_accounts import SPACE_API_KEY_SCOPES
from tests.test_space_entities import _user


@pytest.mark.parametrize('scopes', [None, [], ['space:entity:create'], ['space:entity:edit']])
def test_new_and_legacy_key_metadata_always_has_full_permissions(client, db, scopes):
    owner = _user(db, 'full-access-owner')
    app.dependency_overrides[get_current_user] = lambda: owner
    payload = {'name': 'Agent'}
    if scopes is not None:
        payload['scopes'] = scopes
    created = client.post('/space/api/v2/api-keys', json=payload)
    assert created.status_code == 201, created.text
    assert created.json()['scopes'] == list(SPACE_API_KEY_SCOPES)
    key = db.get(SpaceApiKey, created.json()['id'])
    assert key.scopes == list(SPACE_API_KEY_SCOPES)
    key.scopes = scopes or []
    db.commit()
    listed = client.get('/space/api/v2/api-keys').json()['items'][0]
    assert listed['scopes'] == list(SPACE_API_KEY_SCOPES)
    assert 'api_key' not in listed


@pytest.mark.parametrize('cached_scopes', [[], ['space:entity:create'], None])
def test_standalone_identity_normalizes_legacy_key_permissions(db, monkeypatch, cached_scopes):
    owner = _user(db, 'standalone-key-owner')
    monkeypatch.setattr(space_entities.settings, 'SPACE_STANDALONE', True)
    monkeypatch.setattr(space_entities.auth, 'resolve_identity',
        lambda *_args, **_kwargs: (owner, cached_scopes), raising=False)
    creator = space_entities._entity_creator(
        HTTPAuthorizationCredentials(scheme='Bearer', credentials='verified-by-cloud'), db)
    assert creator.user is owner
    assert creator.api_key_scopes == (None if cached_scopes is None else frozenset(SPACE_API_KEY_SCOPES))
