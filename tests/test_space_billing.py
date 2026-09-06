import uuid
import hashlib
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
        token_hash=hashlib.sha256(token.encode()).digest(), scopes=['space:entity:create', 'space:entity:run']))
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
