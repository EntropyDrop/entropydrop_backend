import base64
import hashlib
import json
import uuid

import pytest
from auth import get_current_user
from main import app
from space import models
from space.inventory_codec import encode_inventory_resource
from tests.test_space_entities import _entity, _user


@pytest.fixture
def configured(client, db):
    owner = _user(db, 'config-owner')
    app.dependency_overrides[get_current_user] = lambda: owner
    world = client.post('/space/api/v2/bootstrap').json()['world']['id']
    headers = {}
    for name, scopes in {
        'basic': ['space:entity:create'],
        'edit': ['space:entity:create', 'space:entity:edit'],
        'full': ['space:entity:create', 'space:entity:edit', 'space:entity:run'],
    }.items():
        result = client.post('/space/api/v2/api-keys', json={'name': name, 'scopes': scopes})
        assert result.status_code == 201, result.text
        # Simulate keys stored before permissions were unified.
        db.get(models.SpaceApiKey, result.json()['id']).scopes = scopes
        db.commit()
        headers[name] = {'Authorization': 'Bearer '+result.json()['api_key']}
    app.dependency_overrides.pop(get_current_user)
    created = client.post(f'/space/api/v2/worlds/{world}/entities', headers=headers['full'], json={
        'operation_id': str(uuid.uuid4()),
        'definition_base64': base64.b64encode(encode_inventory_resource('entity', _entity())).decode(),
        'position': {'x_cm': 100, 'y_cm': 3200, 'z_cm': 100},
    })
    assert created.status_code == 201, created.text
    return f'/space/api/v2/worlds/{world}/entities/{created.json()["id"]}', headers, owner


def command(state, revision):
    return {'operation_id': str(uuid.uuid4()), 'desired_run_state': state, 'expected_revision': revision}


def edit(revision, components=None):
    return {'operation_id': str(uuid.uuid4()), 'expected_revision': revision, 'components': components or [
        {'id': 'root', 'script': 'self.state.changed = true;', 'body': {'mass': 82, 'useGravity': False}},
    ]}


def test_legacy_keys_can_edit_code_defaults_and_control_execution(client, db, configured):
    url, keys, _owner = configured
    before = client.get(url+'/configuration', headers=keys['basic'])
    assert before.status_code == 200, before.text
    assert 'no-store' in before.headers['cache-control']
    assert before.json()['definition']['root']['body']['useGravity'] is True
    patch = edit(1)
    changed = client.patch(url+'/configuration', headers=keys['basic'], json=patch)
    assert changed.status_code == 200, changed.text
    assert changed.json()['revision'] == 2
    definition = client.get(url+'/configuration', headers=keys['full']).json()['definition']
    assert definition['root']['script'] == patch['components'][0]['script']
    assert definition['root']['body']['mass'] == 82
    assert definition['root']['body']['useGravity'] is False
    assert definition['root']['blocks'] == before.json()['definition']['root']['blocks']
    assert client.patch(url+'/configuration', headers=keys['full'], json=edit(1)).json()['detail']['code'] == 'ENTITY_REVISION_CONFLICT'
    assert client.patch(url+'/configuration', headers=keys['full'], json=patch).json() == changed.json()
    start = command('running', 2)
    started = client.put(url+'/run-state', headers=keys['edit'], json=start)
    assert started.status_code == 200, started.text
    blocked = client.patch(url+'/configuration', headers=keys['full'], json=edit(3))
    assert blocked.status_code == 409
    assert blocked.json()['detail']['code'] == 'ENTITY_MUST_BE_STOPPED'
    stopped = client.put(url+'/run-state', headers=keys['basic'], json=command('stopped', 3))
    assert stopped.status_code == 200, stopped.text
    # Delayed retries return their original acknowledgement without undoing Stop.
    assert client.put(url+'/run-state', headers=keys['full'], json=start).json() == started.json()
    assert client.patch(url+'/configuration', headers=keys['full'], json=patch).json() == changed.json()
    latest = client.get(url+'/configuration', headers=keys['full']).json()['entity']
    assert latest['revision'] == 4 and latest['desired_run_state'] == 'stopped'
    reused = client.put(url+'/run-state', headers=keys['full'], json={**start, 'desired_run_state': 'stopped'})
    assert reused.status_code == 409 and reused.json()['detail']['code'] == 'ENTITY_OPERATION_ID_REUSED'
    patch['components'][0]['script'] = ''
    assert client.patch(url+'/configuration', headers=keys['full'], json=patch).status_code == 409
    cleared = client.patch(url+'/configuration', headers=keys['full'], json=edit(4, [{'id': 'root', 'script': ''}]))
    assert cleared.status_code == 200, cleared.text
    assert client.get(url+'/configuration', headers=keys['full']).json()['definition']['root']['script'] == ''


@pytest.mark.parametrize('components', [
    [{'id': 'root', 'script': None}], [{'id': 'root', 'body': {}}],
    [{'id': 'root', 'body': {'mass': 0}}], [{'id': 'root', 'body': {'useGravity': 'false'}}],
    [{'id': 'root', 'owner_user_id': 'other'}], [{'id': 'absent', 'script': ''}],
    [{'id': 'root', 'body': {'friction': 2}}], [{'id': 'root', 'script': '中'*65536}],
    [{'id': 'root', 'script': ''}, {'id': 'root', 'script': 'oops'}],
])
def test_configuration_validation_is_atomic(client, db, configured, components):
    url, keys, _ = configured
    result = client.patch(url+'/configuration', headers=keys['full'], json=edit(1, components))
    assert result.status_code == 422, result.text
    assert db.query(models.SpaceWorldEntity).one().revision == 1
    assert db.query(models.SpaceEntityOperation).count() == 0


def test_configuration_rejects_unauthorized_users_and_hosted_entities(client, db, configured):
    url, keys, _ = configured
    other = _user(db, 'config-other')
    app.dependency_overrides[get_current_user] = lambda: other
    client.post('/space/api/v2/bootstrap')
    key = client.post('/space/api/v2/api-keys', json={'name': 'other', 'scopes': ['space:entity:create', 'space:entity:edit', 'space:entity:run']}).json()['api_key']
    app.dependency_overrides.pop(get_current_user)
    headers = {'Authorization': 'Bearer '+key}
    assert client.get(url+'/configuration').status_code == 401
    assert client.get(url+'/configuration', headers=headers).status_code == 403
    assert client.patch(url+'/configuration', headers=headers, json=edit(1)).status_code == 403
    assert client.put(url+'/run-state', headers=headers, json=command('stopped', 1)).status_code == 403
    entity = db.query(models.SpaceWorldEntity).one()
    entity.execution_mode = 'hosted'
    db.commit()
    assert client.patch(url+'/configuration', headers=keys['full'], json=edit(1)).json()['detail']['code'] == 'ENTITY_HOSTED_EDIT_FORBIDDEN'
    assert client.put(url+'/run-state', headers=keys['full'], json=command('running', 1)).json()['detail']['code'] == 'USE_ENTITY_HOSTING_API'
    assert client.put(url+'/run-state', headers=keys['full'], json=command('paused', 1)).status_code == 422


def test_stop_and_edit_discard_runtime_overrides_but_preserve_placement(client, db, configured):
    url, keys, owner = configured
    entity = db.query(models.SpaceWorldEntity).one()
    snapshot = {'position': [1, 32, 1], 'constructorOrigin': [0.5, 31.5, 0.5],
                'quaternion': [0, 0.6, 0, 0.8], 'localCenter': [0.5, 0.5, 0.5],
                'states': {'root': {'old': True}}, 'velocity': [5, 10, 15],
                'bodies': [{'id': 'root', 'mass': 999}], 'scriptRuntime': 99}
    entity.snapshot = json.dumps(snapshot).encode()
    entity.snapshot_digest = hashlib.sha256(entity.snapshot).digest()
    entity.snapshot_size_bytes = len(entity.snapshot)
    entity.execution_instance_id = str(uuid.uuid4())
    entity.execution_epoch = 7
    entity.desired_run_state = 'running'
    db.commit()
    stopped = client.put(url+'/run-state', headers=keys['basic'], json=command('stopped', 1))
    assert stopped.status_code == 200, stopped.text
    db.refresh(entity)
    reset = json.loads(entity.snapshot)
    assert reset['position'] == snapshot['position'] and reset['quaternion'] == snapshot['quaternion']
    assert reset['states'] == {} and reset['resetRuntime'] is True
    assert 'mass' not in reset['bodies'][0] and 'velocity' not in reset
    assert entity.execution_instance_id is None and entity.execution_epoch == 8
    assert client.patch(url+'/configuration', headers=keys['full'], json=edit(2)).status_code == 200
    # The old browser's save cannot restore old code/state after a remote edit.
    app.dependency_overrides[get_current_user] = lambda: owner
    stale = client.put(url+'/checkpoint', json={
        'operation_id': str(uuid.uuid4()), 'expected_revision': 1, 'snapshot': snapshot,
        'position': {'x_cm': 100, 'y_cm': 3200, 'z_cm': 100}, 'desired_run_state': 'running',
    })
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'ENTITY_REVISION_CONFLICT'


def test_edit_reuses_entity_storage_and_write_quotas(client, db, configured, monkeypatch):
    from routers import space_entities
    url, keys, _ = configured
    entity = db.query(models.SpaceWorldEntity).one()
    before = bytes(entity.definition)
    monkeypatch.setattr(space_entities, 'SPACE_ENTITY_MAX_TOTAL_BYTES_PER_OWNER', entity.size_bytes)
    result = client.patch(url+'/configuration', headers=keys['full'], json=edit(1, [{'id': 'root', 'script': 'x'*1000}]))
    assert result.status_code == 429, result.text
    db.refresh(entity)
    assert bytes(entity.definition) == before and entity.revision == 1
    monkeypatch.setattr(space_entities, 'SPACE_ENTITY_MAX_TOTAL_BYTES_PER_OWNER', 10000000)
    monkeypatch.setattr(space_entities, 'SPACE_ENTITY_CHECKPOINT_MINUTE_BYTES', 1)
    result = client.patch(url+'/configuration', headers=keys['full'], json=edit(1))
    assert result.status_code == 429 and result.json()['detail']['code'] == 'WORLD_ENTITY_CHECKPOINT_QUOTA_REACHED'
