import uuid
import hashlib
import time
from datetime import datetime, timezone
import auth
import jwt
import pytest
from config import settings
from credit_balance import available_balance, lock_balance
from models import User, SpaceApiKey, SpaceCreditReservation, CreditLog


@pytest.fixture
def account(client, db, monkeypatch):
    monkeypatch.setattr(settings, 'SPACE_ACCOUNT_SERVICE_TOKEN', 'service-test-token')
    user = User(id='billing-owner', email='billing@example.com', username='billing', credits=2)
    token = 'edapi_billing_test'
    db.add(user)
    db.add(SpaceApiKey(id=str(uuid.uuid4()), user_id=user.id, name='test', key_prefix='edapi_',
        token_hash=hashlib.sha256(token.encode()).digest(), scopes=['space:entity:create']))
    db.commit()
    def post(path, body):
        return client.post('/internal/space/' + path, headers={'X-Space-Service-Token': 'service-test-token'}, json=body)
    authorization = {'credential': token, 'world_id': str(uuid.uuid4()), 'entity_id': str(uuid.uuid4()),
        'operation_id': str(uuid.uuid4()), 'enabled': True, 'max_credits': 2}
    response = post('authorizations', authorization)
    assert response.status_code == 200, response.text
    return user, post, authorization, response.json()['id']


def test_hold_capture_and_retry_are_one_debit(account, db):
    user, post, payload, aid = account
    assert post('authorizations', payload).json()['id'] == aid
    reservation = {'id': str(uuid.uuid4()), 'authorization_id': aid}
    for _ in range(2):
        assert post('reservations', reservation).json()['state'] == 'reserved'
    assert user.credits == 2
    assert lock_balance(db, user) == 1, 'other credit consumers must respect the hold'
    db.commit()
    for _ in range(2):
        assert post('reservations/capture', reservation).json()['state'] == 'captured'
    assert user.credits == 1
    assert db.query(CreditLog).count() == 1
    assert post('reservations/release', reservation).status_code == 409


def test_release_before_delayed_reserve_and_budget_cap(account, db):
    user, post, _, aid = account
    cancelled = {'id': str(uuid.uuid4()), 'authorization_id': aid}
    assert post('reservations/release', cancelled).json()['state'] == 'released'
    assert post('reservations', cancelled).json()['state'] == 'released'
    assert available_balance(db, user) == 2
    for _ in range(2):
        assert post('reservations', {'id': str(uuid.uuid4()), 'authorization_id': aid}).status_code == 200
    assert post('reservations', {'id': str(uuid.uuid4()), 'authorization_id': aid}).status_code == 402
    assert user.credits == 2
    assert available_balance(db, user) == 0
    assert db.query(CreditLog).count() == 0


def test_revocation_and_request_binding(account, db, client):
    user, post, payload, aid = account
    assert post('authorizations', {**payload, 'max_credits': 1}).status_code == 409
    assert post('authorizations/revoke', {'id': aid}).json()['revoked']
    assert post('reservations', {'id': str(uuid.uuid4()), 'authorization_id': aid}).status_code == 409
    assert client.post('/internal/space/identity', json={'credential': payload['credential']}).status_code == 401
    assert post('authorizations', {**payload, 'credential': 'edapi_forged'}).status_code == 401
    key = db.query(SpaceApiKey).one()
    db.delete(key)
    db.commit()
    assert post('identity', {'credential': payload['credential']}).status_code == 401


def test_legacy_key_identity_has_full_space_access_without_admin_access(account, db, monkeypatch):
    from routers.space_accounts import SPACE_API_KEY_SCOPES
    user, post, payload, _ = account
    monkeypatch.setattr(settings, "ADMIN_EMAILS", user.email)
    assert user.is_admin
    db.query(SpaceApiKey).one().scopes = []
    db.commit()
    response = post('identity', {'credential': payload['credential']})
    assert response.status_code == 200, response.text
    assert response.json()['scopes'] == list(SPACE_API_KEY_SCOPES)
    assert response.json()['is_admin'] is False


@pytest.mark.parametrize('path', ['identity', 'authorizations'])
@pytest.mark.parametrize('session_backed', [False, True])
def test_jwt_credentials_receive_request_context(account, db, monkeypatch, path, session_backed):
    user, post, authorization, _ = account
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', user.email)
    user.last_login_date = datetime.now(timezone.utc).date()
    db.commit()
    expires = int(time.time()) + 20
    claims = {'sub': user.id, 'type': 'access', 'exp': expires}
    if session_backed:
        session, _ = auth.create_auth_session(db, user.id)
        claims['sid'] = session.id
    token = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    authenticate = auth.get_current_user
    authenticated_requests = []

    def track_authentication(*args, **kwargs):
        result = authenticate(*args, **kwargs)
        request = kwargs['request']
        authenticated_requests.append((request.url.path, request.state.rate_limit_principal))
        return result

    monkeypatch.setattr(auth, 'get_current_user', track_authentication)
    payload = {'credential': token}
    if path == 'authorizations':
        payload = {**authorization, **payload, 'operation_id': str(uuid.uuid4())}
    response = post(path, payload)
    assert response.status_code == 200, response.text
    assert authenticated_requests == [(f'/internal/space/{path}', f'user:{user.id}')]
    if path == 'identity':
        assert response.json()['id'] == user.id
        assert response.json()['scopes'] is None
        assert response.json()['is_admin'] is True
        assert response.json()['expires_at'] == expires
    else:
        assert response.json()['user_id'] == user.id


@pytest.mark.parametrize('path', ['identity', 'authorizations'])
@pytest.mark.parametrize('invalid', ['malformed', 'expired', 'wrong_type', 'revoked_session', 'unknown_user'])
def test_invalid_jwt_credentials_are_rejected(account, db, path, invalid):
    user, post, authorization, _ = account
    claims = {'sub': user.id, 'type': 'access', 'exp': int(time.time()) + 60}
    if invalid == 'expired':
        claims['exp'] = int(time.time()) - 60
    elif invalid == 'wrong_type':
        claims['type'] = 'refresh'
    elif invalid == 'revoked_session':
        session, cookie = auth.create_auth_session(db, user.id)
        claims['sid'] = session.id
        auth.revoke_auth_session(db, cookie)
    elif invalid == 'unknown_user':
        claims['sub'] = 'missing-billing-user'
    token = ('not-a-jwt' if invalid == 'malformed' else
             jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM))
    payload = {'credential': token}
    if path == 'authorizations':
        payload = {**authorization, **payload, 'operation_id': str(uuid.uuid4())}
    response = post(path, payload)
    assert response.status_code == 401, response.text


def test_later_entity_operator_funds_hosting_from_their_own_account(account, db):
    from models import SpaceCreditAuthorization
    author, post, payload, old_aid = account
    operator = User(id='billing-operator', email='operator@example.com', username='operator', credits=3)
    token = 'edapi_operator_test'
    db.add(operator)
    db.add(SpaceApiKey(id=str(uuid.uuid4()), user_id=operator.id, name='operator', key_prefix='edapi_',
        token_hash=hashlib.sha256(token.encode()).digest(), scopes=['space:entity:create']))
    db.commit()
    # Exclusive world occupation is validated by the service before this RPC.
    response = post('authorizations', {**payload, 'credential': token, 'operation_id': str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    aid = response.json()['id']
    assert response.json()['user_id'] == operator.id
    assert db.get(SpaceCreditAuthorization, aid).user_id == operator.id
    assert not db.get(SpaceCreditAuthorization, old_aid).enabled
    assert post('reservations', {'id': str(uuid.uuid4()), 'authorization_id': old_aid}).status_code == 409
    reservation = {'id': str(uuid.uuid4()), 'authorization_id': aid}
    assert post('reservations', reservation).json()['state'] == 'reserved'
    assert post('reservations/capture', reservation).json()['state'] == 'captured'
    assert author.credits == 2, 'must never debit the entity creator or historical payer'
    assert operator.credits == 2
