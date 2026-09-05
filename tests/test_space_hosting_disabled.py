"""Disabled hosting must fail before database access, runtime startup, or billing."""
import asyncio
import uuid
from unittest.mock import Mock

import pytest
from config import Settings, settings
from database import get_db
from main import app
from space import hosting_worker


@pytest.fixture(autouse=True)
def disabled_hosting(monkeypatch):
    monkeypatch.setattr(settings, 'SPACE_HOSTING_ENABLED', False)


def test_hosting_is_disabled_by_default():
    assert Settings.model_fields['SPACE_HOSTING_ENABLED'].default is False


@pytest.mark.parametrize('method', ['get', 'put'])
def test_disabled_endpoint_does_not_access_database_or_auth(client, method):
    def no_database():
        raise AssertionError('disabled hosting must not open the database')
    app.dependency_overrides[get_db] = no_database
    path = f'/space/api/v2/worlds/{uuid.uuid4()}/entities/{uuid.uuid4()}/hosting'
    response = getattr(client, method)(path, **({'json': {'operation_id': str(uuid.uuid4()), 'enabled': True}} if method == 'put' else {}))
    assert response.status_code == 503, response.text
    assert response.json()['detail']['code'] == 'HOSTING_DISABLED'


def test_disabled_worker_never_opens_database_or_starts_node(monkeypatch):
    database = Mock(side_effect=AssertionError('database opened'))
    node = Mock(side_effect=AssertionError('Node started'))
    monkeypatch.setattr(hosting_worker, 'SessionLocal', database)
    monkeypatch.setattr(hosting_worker, 'NodeRuntime', node)
    with pytest.raises(RuntimeError, match='hosting is disabled'):
        asyncio.run(hosting_worker.main())
    assert hosting_worker.prepare(None, 'world', 'instance') is None
    assert hosting_worker.commit_result(None, 'world', 'instance', {}, {}) is False
    with pytest.raises(RuntimeError, match='hosting is disabled'):
        hosting_worker.charge_time(None, None, None, 1000)
    database.assert_not_called()
    node.assert_not_called()


def test_disabling_rejects_inflight_results_and_saved_receipt_retries(client, db, monkeypatch):
    from tests.test_space_hosting import setup, enable, simulate
    from models import CreditLog, SpaceWorldEntity
    monkeypatch.setattr(settings, 'SPACE_HOSTING_ENABLED', True)
    owner, world, entity_id, base = setup(client, db)
    operation = str(uuid.uuid4())
    assert enable(client, base, operation_id=operation).status_code == 200
    payload = hosting_worker.prepare(db, world, 'worker-a')
    result = simulate(payload)
    revision = db.get(SpaceWorldEntity, (world, entity_id)).revision
    monkeypatch.setattr(settings, 'SPACE_HOSTING_ENABLED', False)
    assert not hosting_worker.commit_result(db, world, 'worker-a', payload, result)
    assert enable(client, base, operation_id=operation).json()['detail']['code'] == 'HOSTING_DISABLED'
    assert owner.credits == 3
    assert db.query(CreditLog).filter_by(action='space_entity_hosting').count() == 0
    assert db.get(SpaceWorldEntity, (world, entity_id)).revision == revision
