"""Run separately: SPACE_STANDALONE=true DATABASE_URL=sqlite:///:memory: pytest space/tests."""
import os
import uuid
import base64
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from config import settings
if not settings.SPACE_STANDALONE:
    pytest.skip("Run standalone tests in a separate SPACE_STANDALONE=true process", allow_module_level=True)
from space.database import Base, get_db
from space import models, billing
from space.main import app
from space.auth import get_current_user
from routers.space_entities import EntityCreator, _entity_creator
from space.inventory_codec import encode_inventory_resource


@pytest.fixture
def local(monkeypatch):
    assert settings.SPACE_STANDALONE
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(billing, 'SessionLocal', sessions)
    monkeypatch.setattr(settings, 'SPACE_HOSTING_ENABLED', True)
    app.state.limiter.enabled = False
    with sessions() as db:
        user = models.User(id='local-owner', username='local', skin_type='strong')
        user.credits = 2
        db.add(user)
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[_entity_creator] = lambda: EntityCreator(user=user, credential='user-proof')
        client = TestClient(app)
        world = client.post('/space/api/v2/bootstrap').json()['world']['id']
        definition = {'type': 'space-entity', 'version': 5, 'root': {'id': 'root', 'name': 'Local',
            'body': {'type': 'dynamic', 'useGravity': False}, 'blocks': [{'dx':0,'dy':0,'dz':0,'block':1,'color':1}],
            'children': [], 'seats': []}, 'constraints': []}
        result = client.post(f'/space/api/v2/worlds/{world}/entities', json={'operation_id': str(uuid.uuid4()),
            'definition_base64': base64.b64encode(encode_inventory_resource('entity', definition)).decode(),
            'position': {'x_cm':8000,'y_cm':22000,'z_cm':8000}})
        assert result.status_code == 201, result.text
        entity = db.get(models.SpaceWorldEntity, (world, result.json()['id']))
        entity.execution_mode = "hosted"
        entity.desired_run_state = "running"
        entity.hosting_anchor = [8000, 8000]
        entity.hosting_enabled = True
        entity.hosting_budget_remaining = 2
        entity.hosting_authorization_id = str(uuid.uuid4())
        db.add(models.SpaceHostingAuthorization(id=entity.hosting_authorization_id,world_id=world,entity_id=entity.id))
        db.commit()
        yield db, entity, world, sessions
    app.dependency_overrides.clear()
    engine.dispose()


def test_schema_contains_no_cloud_secrets_or_balance(local):
    assert not {'users', 'space_api_keys', 'credit_logs', 'auth_sessions'} & set(Base.metadata.tables)
    assert set(models.User.__table__.columns.keys()) == {'id','username','skin_url','skin_type','updated_at'}


def test_lost_reserve_ack_tick_rollback_and_capture_retry(local, monkeypatch):
    db, entity, world, sessions = local
    remote = {}
    requests = []
    lost_ack = True
    def rpc(path, body):
        nonlocal lost_ack
        assert not db.in_transaction(), 'test observer must not hold a transaction across RPC'
        gid = body['id']
        requests.append((path, gid))
        if path == 'reservations':
            remote.setdefault(gid, 'reserved')
            if lost_ack:
                lost_ack = False
                raise HTTPException(503)
        else:
            remote[gid] = 'captured'
        return {'state': remote[gid]}
    monkeypatch.setattr(billing, 'call', rpc)
    billing.reconcile(world)
    billing.reconcile(world)
    assert len(remote) == 1
    db.refresh(entity)
    billing.consume(db, entity, 1000)
    db.rollback()
    assert remote[next(iter(remote))] == 'reserved'
    db.refresh(entity)
    assert entity.hosting_remaining_ms == 0
    billing.consume(db, entity, 1000)
    db.commit()
    billing.reconcile(world)
    billing.reconcile(world)
    db.refresh(entity)
    assert entity.hosting_remaining_ms == 3599000
    assert entity.hosting_billed_hours == 1
    assert list(remote.values()) == ['captured']
    # An already funded hour needs no account RPC in its simulation transaction.
    billing.consume(db, entity, 1000)
    db.commit()
    assert entity.hosting_remaining_ms == 3598000


def test_delete_while_reserving_releases_hold(local, monkeypatch):
    db, entity, world, sessions = local
    eid = entity.id
    db.commit()
    calls=[]
    def rpc(path, payload):
        calls.append(path)
        if path == 'reservations':
            with sessions() as concurrent:
                concurrent.delete(concurrent.get(models.SpaceWorldEntity, (world, eid)))
                concurrent.commit()
            return {'state': 'reserved'}
        return {'state': 'released', 'revoked': True}
    monkeypatch.setattr(billing, 'call', rpc)
    billing.reconcile(world)
    billing.reconcile(world)
    with sessions() as observer:
        grant=observer.query(models.SpaceHostingGrant).one()
        assert grant.state == 'cancelled' and grant.settled
    assert 'reservations/release' in calls and 'authorizations/revoke' in calls


def test_market_local_storage_atomic_and_path_safe(tmp_path, monkeypatch):
    from space import object_store
    monkeypatch.setattr(settings, 'SPACE_OBJECT_DIR', str(tmp_path))
    key='space-market/resources/example/abc.pb'
    object_store.upload_to_s3(b'protobuf', key)
    assert object_store.download_from_s3(key) == b'protobuf'
    with pytest.raises(ValueError):
        object_store.object_path('space-market/resources/../../private')
    object_store.delete_from_s3_strict(key)
    assert not object_store.object_path(key).exists()


def test_actual_worker_commit_consumes_grant_with_snapshot(local, monkeypatch):
    import asyncio
    from space.hosting_worker import prepare, commit_result, NodeRuntime
    db, entity, world, sessions = local
    eid = entity.id
    db.commit()
    monkeypatch.setattr(billing, 'call', lambda path, body: {'state':'reserved' if path=='reservations' else 'captured'})
    billing.reconcile(world)
    payload = prepare(db, world, 'standalone-test-worker')
    assert payload and payload['entities']
    async def simulate():
        runtime = NodeRuntime()
        try:
            return await runtime.step(payload)
        finally:
            await runtime.close()
    result = asyncio.run(simulate())
    assert not result.get('error') and not result.get('faults'), result
    assert commit_result(db, world, 'standalone-test-worker', payload, result)
    db.refresh(entity)
    assert entity.hosting_remaining_ms == 3599000
    assert entity.hosting_billed_hours == 1
    grant = db.query(models.SpaceHostingGrant).one()
    assert grant.state == 'consumed' and grant.settlement == 'capture'
    assert not commit_result(db, world, 'standalone-test-worker', payload, result), 'a duplicate simulation result is fenced out'
    db.rollback()
