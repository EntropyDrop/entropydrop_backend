import asyncio
import base64
import datetime as dt
import json
import uuid

import pytest
from config import settings
from auth import get_current_user
from main import app
from models import User, SpaceWorldEntity, SpaceHostingWorker, CreditLog, SpaceChunkSnapshot
from routers.space import _decode_chunk_overlay
from routers.space_entities import EntityCreator, _entity_creator
from space.hosting_worker import prepare, commit_result, NodeRuntime, HOUR_MS
from space.inventory_codec import encode_inventory_resource


@pytest.fixture(autouse=True)
def enabled_hosting_for_runtime_tests(monkeypatch):
    monkeypatch.setattr(settings, "SPACE_HOSTING_ENABLED", True)


def setup(client, db, script="self.state.ticks = (self.state.ticks || 0) + 1;", credits=3):
    user = User(id="host-owner", email="host@example.com", username="host", credits=credits)
    db.add(user)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[_entity_creator] = lambda: EntityCreator(user=user)
    world_id = client.post('/space/api/v2/bootstrap').json()['world']['id']
    definition = {"type": "space-entity", "version": 5, "root": {"name": "Hosted Robot","id": "root", "body": {"type": "dynamic", "useGravity": False},
                 "blocks": [{"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 0x123456}],
                 "script": script, "children": [], "seats": []}, "constraints": []}
    response = client.post(f'/space/api/v2/worlds/{world_id}/entities', json={
        "operation_id": str(uuid.uuid4()),
        "definition_base64": base64.b64encode(encode_inventory_resource("entity", definition)).decode(),
        "position": {"x_cm": 8000, "y_cm": 22000, "z_cm": 8000}})
    assert response.status_code == 201, response.text
    entity_id = response.json()['id']
    prepare(db, world_id, 'worker-a')
    return user, world_id, entity_id, f'/space/api/v2/worlds/{world_id}/entities/{entity_id}'


def enable(client, base, budget=1, operation_id=None):
    return client.put(base + '/hosting', json={"operation_id": operation_id or str(uuid.uuid4()),
                                              "enabled": True, "max_credits": budget})


def simulate(payload):
    async def run():
        runtime = NodeRuntime()
        try:
            return await asyncio.wait_for(runtime.step(payload), 15)
        finally:
            await runtime.close()
    return asyncio.run(run())


def test_offline_execution_commits_world_state_and_one_credit_atomically(client, db):
    user, world, entity_id, base = setup(client, db, script='''
self.state.ticks = (self.state.ticks || 0) + 1;
if (self.state.ticks === 1) ctx.world.voxels.set([82, 220, 80], {color: 0x123456});
''')
    assert enable(client, base).status_code == 200
    assert db.get(User, user.id).credits == 3, 'queueing is not billable'
    payload = prepare(db, world, 'worker-a')
    result = simulate(payload)
    assert not result.get('error'), result
    assert not result.get('faults'), result
    assert result['entities'][0]['snapshot']['states']['root']['ticks'] == 20
    assert commit_result(db, world, 'worker-a', payload, result)
    entity = db.get(SpaceWorldEntity, (world, entity_id))
    assert entity.hosting_remaining_ms == HOUR_MS - 1000
    assert db.get(User, user.id).credits == 2
    assert db.query(CreditLog).filter_by(action='space_entity_hosting').count() == 1
    overlay = _decode_chunk_overlay(db.query(SpaceChunkSnapshot).filter_by(world_id=world, chunk_x=5, chunk_z=5).one())
    assert [82, 220, 80, 1, 0x123456] in overlay['standard']
    assert not commit_result(db, world, 'worker-a', payload, result), 'duplicate result is stale'
    db.rollback()
    second = prepare(db, world, 'worker-a')
    recovered = simulate(second)  # new Node process: restore durable self.state
    assert recovered['entities'][0]['snapshot']['states']['root']['ticks'] == 40
    assert commit_result(db, world, 'worker-a', second, recovered)
    assert db.get(User, user.id).credits == 2


def test_hosting_idempotency_pause_and_browser_exclusion(client, db):
    user, world, entity_id, base = setup(client, db)
    operation = str(uuid.uuid4())
    first = enable(client, base, operation_id=operation)
    assert first.status_code == 200
    assert enable(client, base, operation_id=operation).json() == first.json()
    assert enable(client, base, budget=2, operation_id=operation).status_code == 409
    lease = client.put(f'/space/api/v2/worlds/{world}/entities/execution-leases', json={
        "instance_id": str(uuid.uuid4()), "entity_ids": [entity_id]})
    assert lease.json()['items'][0]['granted'] is False
    assert client.put(base + '/run-state', json={"operation_id": str(uuid.uuid4()), "desired_run_state": "running"}).status_code == 409
    paused = client.put(base + '/hosting', json={"operation_id": str(uuid.uuid4()), "enabled": False})
    assert paused.json()['state'] == 'paused'
    assert enable(client, base, operation_id=operation).json() == first.json()
    assert db.get(SpaceWorldEntity, (world, entity_id)).hosting_enabled is False


def test_stale_worker_cannot_charge_after_pause_or_takeover(client, db):
    user, world, entity_id, base = setup(client, db)
    enable(client, base)
    payload = prepare(db, world, 'worker-a')
    result = simulate(payload)
    lease = db.get(SpaceHostingWorker, world)
    lease.lease_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.commit()
    replacement = prepare(db, world, 'worker-b')
    assert replacement['epoch'] > payload['epoch']
    assert not commit_result(db, world, 'worker-a', payload, result)
    db.rollback()
    client.put(base + '/hosting', json={"operation_id": str(uuid.uuid4()), "enabled": False})
    assert not commit_result(db, world, 'worker-b', replacement, result)
    assert db.get(User, user.id).credits == 3


def test_script_failure_does_not_spend_credit(client, db):
    user, world, entity_id, base = setup(client, db, script='while (true) {}')
    enable(client, base)
    payload = prepare(db, world, 'worker-a')
    result = simulate(payload)
    assert result['faults'][0]['reason'] == 'script_error'
    assert commit_result(db, world, 'worker-a', payload, result)
    assert db.get(User, user.id).credits == 3
    assert db.get(SpaceWorldEntity, (world, entity_id)).hosting_reason == 'script_error'


def test_zero_balance_and_budget_exhaustion_pause_without_debt(client, db):
    user, world, entity_id, base = setup(client, db, credits=0)
    assert enable(client, base).status_code == 402
    entity = db.get(SpaceWorldEntity, (world, entity_id))
    entity.hosting_remaining_ms = 1000
    db.commit()
    assert enable(client, base, budget=0).status_code == 200
    payload = prepare(db, world, 'worker-a')
    assert commit_result(db, world, 'worker-a', payload, simulate(payload))
    assert entity.hosting_remaining_ms == 0
    assert entity.hosting_enabled is False
    assert entity.hosting_reason == 'budget_exhausted'
    assert user.credits == 0


def test_scope_ownership_and_worker_readiness(client, db):
    user, world, entity_id, base = setup(client, db)
    app.dependency_overrides[_entity_creator] = lambda: EntityCreator(user=user, api_key_scopes=frozenset(['space:entity:create']))
    assert enable(client, base).status_code == 403
    other = User(id='other-host', email='other-host@example.com', credits=100)
    db.add(other)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: other
    client.post('/space/api/v2/bootstrap')
    app.dependency_overrides[_entity_creator] = lambda: EntityCreator(user=other)
    assert enable(client, base).status_code == 403
    app.dependency_overrides[_entity_creator] = lambda: EntityCreator(user=user)
    db.delete(db.get(SpaceHostingWorker, world))
    db.commit()
    assert enable(client, base).status_code == 503


def test_remaining_time_survives_pause_release_and_hourly_renewal(client, db):
    user, world, entity_id, base = setup(client, db)
    enable(client, base, budget=2)
    entity = db.get(SpaceWorldEntity, (world, entity_id))
    payload = prepare(db, world, 'worker-a')
    commit_result(db, world, 'worker-a', payload, simulate(payload))
    assert user.credits == 2
    entity.hosting_remaining_ms = 1000  # last second of the already purchased hour
    db.commit()
    payload = prepare(db, world, 'worker-a')
    commit_result(db, world, 'worker-a', payload, simulate(payload))
    assert entity.hosting_enabled
    assert user.credits == 2
    payload = prepare(db, world, 'worker-a')
    commit_result(db, world, 'worker-a', payload, simulate(payload))
    assert entity.hosting_billed_hours == 2
    assert user.credits == 1
    left = entity.hosting_remaining_ms
    response = client.put(base + '/hosting', json={"operation_id": str(uuid.uuid4()),
        "enabled": False, "release_to_browser": True})
    assert response.json()['execution_mode'] == 'browser'
    assert entity.hosting_remaining_ms == left
    assert entity.hosting_budget_remaining == 0
    assert enable(client, base, budget=0).status_code == 200


def test_browser_handoff_waits_for_previous_lease(client, db):
    _, world, entity_id, base = setup(client, db)
    entity = db.get(SpaceWorldEntity, (world, entity_id))
    entity.execution_lease_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=8)
    db.commit()
    assert enable(client, base).status_code == 200
    assert prepare(db, world, 'worker-a') is None
    entity.execution_lease_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.commit()
    assert prepare(db, world, 'worker-a') is not None


def test_failed_world_edit_rolls_back_snapshot_and_charge(client, db, monkeypatch):
    from routers import space as terrain
    from fastapi import HTTPException
    user, world, entity_id, base = setup(client, db, script='ctx.world.voxels.set([82, 220, 80]);')
    enable(client, base)
    payload = prepare(db, world, 'worker-a')
    result = simulate(payload)
    assert result['mutations']
    def reject(*args, **kwargs):
        raise HTTPException(429, detail={"code": "TERRAIN_EDIT_QUOTA_REACHED"})
    monkeypatch.setattr(terrain, '_apply_terrain_mutation_batch', reject)
    with pytest.raises(HTTPException):
        commit_result(db, world, 'worker-a', payload, result)
    db.rollback()
    entity = db.get(SpaceWorldEntity, (world, entity_id))
    assert entity.hosting_remaining_ms == 0
    assert entity.snapshot is None
    assert user.credits == 3
    assert db.query(CreditLog).filter_by(action='space_entity_hosting').count() == 0


def test_real_external_key_can_host_and_revocation_blocks_access(client, db):
    _, _, _, base = setup(client, db)
    key = client.post('/space/api/v2/api-keys', json={"name": "host",
        "scopes": ["space:entity:create", "space:entity:run"]}).json()
    app.dependency_overrides.pop(_entity_creator)
    headers = {"Authorization": "Bearer " + key['api_key']}
    started = client.put(base + '/hosting', headers=headers, json={
        "operation_id": str(uuid.uuid4()), "enabled": True, "max_credits": 1})
    assert started.status_code == 200, started.text
    assert started.json()['state'] == 'starting'
    assert client.get(base + '/hosting', headers=headers).status_code == 200
    client.delete('/space/api/v2/api-keys/' + key['id'])
    assert client.get(base + '/hosting', headers=headers).status_code == 401


def test_script_stop_consumes_only_the_executed_tick(client, db):
    user, world, entity_id, base = setup(client, db, script='self.stop();')
    enable(client, base)
    payload = prepare(db, world, 'worker-a')
    result = simulate(payload)
    assert result['entities'][0]['elapsed_ms'] == 50
    assert commit_result(db, world, 'worker-a', payload, result)
    entity = db.get(SpaceWorldEntity, (world, entity_id))
    assert entity.hosting_remaining_ms == HOUR_MS - 50
    assert entity.hosting_reason == 'script_stopped'
    assert not entity.hosting_enabled
    assert user.credits == 2


def test_hosted_physics_moves_and_recovers_velocity_without_a_browser(client, db):
    _, world, entity_id, base = setup(client, db, script='self.applyForce([1, 0, 0]);')
    enable(client, base)
    payload = prepare(db, world, 'worker-a')
    result = simulate(payload)
    assert not result.get('faults'), result
    state = result['entities'][0]['snapshot']
    assert state['position'][0] > 80.5
    assert state['velocity'][0] > 0
    assert commit_result(db, world, 'worker-a', payload, result)
    recovered = simulate(prepare(db, world, 'worker-a'))['entities'][0]['snapshot']
    assert recovered['position'][0] > state['position'][0]
    assert recovered['velocity'][0] > state['velocity'][0]
