import datetime as dt
import hashlib

import pytest
from auth import get_current_user
from main import app
from models import SpaceAgentAuthorization, SpaceApiKey, User
from routers import space_agent_auth as flow, space_accounts

BASE = '/space/api/v2/agent-authorizations'


@pytest.fixture
def pairing(client, db, monkeypatch):
    owner = User(id='agent-owner', email='owner@example.test')
    db.add(owner)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: owner
    monkeypatch.setattr(flow.settings, 'SPACE_AGENT_VERIFICATION_URI', 'https://site.example.test/space/authorize')
    response = client.post(BASE + '/requests', json={'name': 'My agent'})
    assert response.status_code == 201
    assert response.headers['cache-control'] == 'no-store'
    return response.json(), owner


def ready(db):
    grant = db.query(SpaceAgentAuthorization).one()
    grant.next_poll_at = flow.now() - dt.timedelta(seconds=1)
    db.commit()
    return grant


def poll(client, pairing):
    return client.post(BASE + '/token', json={'device_code': pairing[0]['device_code']})


def decide(client, pairing, approve=True):
    return client.post(BASE + '/decision', json={'user_code': pairing[0]['user_code'], 'approve': approve})


def test_pairing_approval_redemption_retry_and_revocation(client, db, pairing):
    data, owner = pairing
    grant = db.query(SpaceAgentAuthorization).one()
    assert grant.device_hash == hashlib.sha256(data['device_code'].encode()).digest()
    assert grant.user_code_hash == hashlib.sha256(data['user_code'].replace('-', '').encode()).digest()
    assert data['device_code'] not in data['verification_uri_complete']
    assert data['verification_uri_complete'].endswith('#code=' + data['user_code'])
    inspected = client.post(BASE + '/inspect', json={'user_code': data['user_code']}).json()
    assert inspected['account']['email'] == owner.email
    assert inspected['status'] == 'pending'
    assert 'api_key' not in inspected and 'device_code' not in inspected
    ready(db)
    assert poll(client, pairing).json()['error'] == 'authorization_pending'
    assert db.query(SpaceApiKey).count() == 0
    assert decide(client, pairing).json() == {'status': 'approved'}
    assert db.query(SpaceApiKey).count() == 0
    ready(db)
    response = poll(client, pairing)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    key = response.json()
    assert db.query(SpaceApiKey).one().token_hash == hashlib.sha256(key['api_key'].encode()).digest()
    assert db.query(SpaceApiKey).one().user_id == owner.id
    assert set(key['scopes']) == set(space_accounts.SPACE_API_KEY_SCOPES)
    # Response-loss retries cannot mint a second key.
    ready(db)
    retry = poll(client, pairing).json()
    assert retry['api_key'] == key['api_key'] and retry['id'] == key['id']
    assert db.query(SpaceApiKey).count() == 1
    listed = client.get('/space/api/v2/api-keys').json()['items']
    assert listed[0]['id'] == key['id'] and 'api_key' not in listed[0]
    assert client.delete('/space/api/v2/api-keys/' + key['id']).status_code == 200
    ready(db)
    assert poll(client, pairing).json()['error'] == 'access_denied'
    assert db.query(SpaceApiKey).count() == 0


def test_consent_requires_browser_login_and_never_accepts_device_code(client, db, pairing):
    app.dependency_overrides.pop(get_current_user)
    payload = {'user_code': pairing[0]['user_code'], 'approve': True}
    for headers in [{}, {'Authorization': 'Bearer edapi_fake_key'}]:
        assert client.post(BASE + '/decision', json=payload, headers=headers).status_code in (401, 403)
    assert db.query(SpaceAgentAuthorization).one().status == 'pending'
    assert db.query(SpaceApiKey).count() == 0


def test_public_code_cannot_redeem_key_and_invalid_private_code_is_rejected(client, db, pairing):
    assert client.post(BASE + '/token', json={'device_code': pairing[0]['user_code']}).status_code == 422
    assert client.post(BASE + '/token', json={'device_code': 'x' * 43}).json()['error'] == 'invalid_grant'
    assert db.query(SpaceApiKey).count() == 0


def test_denial_is_terminal_and_idempotent(client, db, pairing):
    assert decide(client, pairing, False).json() == {'status': 'denied'}
    assert decide(client, pairing, False).status_code == 200
    assert decide(client, pairing, True).status_code == 409
    assert poll(client, pairing).json()['error'] == 'access_denied'
    assert db.query(SpaceApiKey).count() == 0


def test_consent_cannot_switch_accounts_or_change_after_decision(client, db, pairing):
    assert decide(client, pairing).status_code == 200
    assert decide(client, pairing).status_code == 200
    other = User(id='other-agent-user', email='other@example.test')
    db.add(other)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: other
    assert decide(client, pairing).status_code == 403
    assert client.post(BASE + '/inspect', json={'user_code': pairing[0]['user_code']}).status_code == 403
    assert db.query(SpaceAgentAuthorization).one().user_id == pairing[1].id


@pytest.mark.parametrize('approve', [False, True])
def test_expiry_prevents_both_consent_and_redemption(client, db, pairing, approve):
    if approve:
        assert decide(client, pairing).status_code == 200
    grant = ready(db)
    grant.expires_at = flow.now() - dt.timedelta(seconds=1)
    db.commit()
    assert decide(client, pairing).status_code == 410
    assert client.post(BASE + '/inspect', json={'user_code': pairing[0]['user_code']}).status_code == 410
    assert poll(client, pairing).json()['error'] == 'expired_token'
    assert db.query(SpaceApiKey).count() == 0
    assert client.post(BASE + '/requests', json={'name': 'Next agent'}).status_code == 201
    assert db.query(SpaceAgentAuthorization).count() == 1


def test_polling_enforces_interval_and_slowdown(client, db, pairing):
    assert poll(client, pairing).json() == {'error': 'slow_down', 'interval': 10}
    assert poll(client, pairing).json() == {'error': 'slow_down', 'interval': 15}
    ready(db)
    assert poll(client, pairing).json() == {'error': 'authorization_pending', 'interval': 15}


def test_key_quota_failure_can_retry_without_losing_consent(client, db, pairing, monkeypatch):
    decide(client, pairing)
    monkeypatch.setattr(space_accounts, 'SPACE_API_KEY_MAX_PER_USER', 0)
    ready(db)
    assert poll(client, pairing).json()['error'] == 'SPACE_API_KEY_LIMIT_REACHED'
    assert db.query(SpaceAgentAuthorization).one().status == 'approved'
    assert db.query(SpaceApiKey).count() == 0
    monkeypatch.setattr(space_accounts, 'SPACE_API_KEY_MAX_PER_USER', 20)
    assert poll(client, pairing).status_code == 200


@pytest.mark.parametrize('uri', ['http://evil.test/consent', 'https://u:p@site.test/consent', 'https://site.test/consent?next=evil', 'javascript:alert(1)'])
def test_verification_url_must_be_safe_server_configuration(client, monkeypatch, uri):
    monkeypatch.setattr(flow.settings, 'SPACE_AGENT_VERIFICATION_URI', uri)
    assert client.post(BASE + '/requests', json={'name': 'Agent'}).status_code == 503


@pytest.mark.parametrize('payload', [{'name': ' '}, {'name': 'Agent\nSpoof'}, {'name': 'Agent', 'redirect_uri': 'https://evil.test'}, {'name': 'Agent', 'user_id': 'victim'}])
def test_agent_cannot_choose_account_redirect_or_invalid_name(client, payload):
    assert client.post(BASE + '/requests', json=payload).status_code == 422


def test_real_browser_login_can_approve_and_issued_key_can_authenticate(client, db, pairing, monkeypatch):
    from auth import create_access_token
    from routers.space_billing import settings
    app.dependency_overrides.pop(get_current_user)
    login = create_access_token({"sub": pairing[1].id})
    response = client.post(BASE + '/decision', headers={"Authorization": "Bearer " + login},
                           json={"user_code": pairing[0]['user_code'], "approve": True})
    assert response.status_code == 200, response.text
    ready(db)
    key = poll(client, pairing).json()['api_key']
    monkeypatch.setattr(settings, 'SPACE_ACCOUNT_SERVICE_TOKEN', 'test-service-token')
    response = client.post('/internal/space/identity', json={"credential": key},
                           headers={"X-Space-Service-Token": 'test-service-token'})
    assert response.status_code == 200, response.text
    assert response.json()['id'] == pairing[1].id
