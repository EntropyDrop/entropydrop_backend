import pytest
from auth import get_current_user
from fastapi.security import HTTPAuthorizationCredentials
from main import app
from models import SpaceApiKey
from routers.space_accounts import SPACE_API_KEY_SCOPES
from models import User

def _user(db, user_id):
    user = User(id=user_id, email=user_id + "@example.test", username="Test")
    db.add(user); db.commit()
    return user


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
