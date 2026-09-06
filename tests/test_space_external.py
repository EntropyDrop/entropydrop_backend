import base64
import time
import uuid

import pytest
from auth import get_current_user
from main import app
import models
from routers import space
from space.inventory_codec import encode_inventory_resource
from tests.test_space_entities import _user


def setup(client, db, *, build=True):
    owner = _user(db, 'external-builder')
    owner.credits = 7
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: owner
    world_id = client.post('/space/api/v2/bootstrap').json()['world']['id']
    scopes = ['space:entity:create'] + (['space:blockset:build'] if build else [])
    key = client.post('/space/api/v2/api-keys', json={'name': 'Builder', 'scopes': scopes}).json()
    app.dependency_overrides.pop(get_current_user)
    return owner, world_id, {'Authorization': f"Bearer {key['api_key']}"}, key['id']


def body(blocks=None, **kwargs):
    resource = {'type': 'space-blockset', 'version': 5, 'name': 'API platform',
                'blocks': blocks or [{'dx': 0, 'dy': 0, 'dz': 0, 'block': 1, 'color': 0xABCDEF}]}
    return {'operation_id': str(uuid.uuid4()), 'created_at_ms': int(time.time() * 1000),
            'definition_base64': base64.b64encode(encode_inventory_resource('blockset', resource)).decode(),
            'position': {'x_cm': 16000, 'y_cm': 5000, 'z_cm': 16000}, **kwargs}


def post(client, world, headers, payload):
    return client.post(f'/space/api/v2/worlds/{world}/blocksets/build', headers=headers, json=payload)


def test_build_works_without_browser_presence_and_updates_live_allowance(client, db):
    owner, world, headers, _ = setup(client, db)
    db.query(models.SpacePlayerSnapshot).filter_by(user_id=owner.id).delete()
    db.commit()
    payload = body()
    before = client.get(f'/space/api/v2/worlds/{world}/api-usage', headers=headers).json()
    first = post(client, world, headers, payload)
    assert first.status_code == 201, first.text
    assert first.json()['built_blocks'] == 1
    assert first.json()['credits_charged'] == 0
    assert first.json()['effective_changes'] == 1
    repeated = post(client, world, headers, payload)
    assert repeated.json() == first.json()
    assert db.query(models.SpaceTerrainMutationBatch).count() == 1
    snapshot = db.query(models.SpaceChunkSnapshot).one()
    decoded = space._decode_chunk_overlay(snapshot)
    assert [160, 50, 160, 1, 0xABCDEF] in decoded['standard']
    after = client.get(f'/space/api/v2/worlds/{world}/api-usage', headers=headers).json()
    assert after['credits'] == 7
    assert after['features']['entity_hosting'] is False
    assert 'hosting_credits_per_hour' not in after['pricing']
    assert 'hosted_entities_world' not in after['quotas']
    assert after['quotas']['terrain']['day']['used'] == before['quotas']['terrain']['day']['used'] + 1
    assert after['quotas']['api_keys']['used'] == 1
    assert post(client, world, headers, {**payload, 'yaw_quarter_turns': 1}).status_code == 409


@pytest.mark.parametrize('turn,expected', [(0, (160, 160)), (1, (160, 159)), (2, (159, 159)), (3, (159, 160))])
def test_rotates_voxel_volumes_about_origin(client, db, turn, expected):
    _, world, headers, _ = setup(client, db)
    result = post(client, world, headers, body(yaw_quarter_turns=turn))
    assert result.status_code == 201, result.text
    decoded = space._decode_chunk_overlay(db.query(models.SpaceChunkSnapshot).one())
    assert decoded['standard'][0][:3] == [expected[0], 50, expected[1]]


def test_micro_build_replaces_touched_cell_and_retains_neighbors(client, db):
    _, world, headers, _ = setup(client, db)
    assert post(client, world, headers, body()).status_code == 201
    blocks = [{'dx': 0, 'dy': 0, 'dz': 0, 'mx': 1, 'my': 2, 'mz': 3, 'color': 123}]
    result = post(client, world, headers, body(blocks))
    assert result.status_code == 201, result.text
    decoded = space._decode_chunk_overlay(db.query(models.SpaceChunkSnapshot).one())
    assert decoded['standard'] == [[160, 50, 160, 0, 0]]
    assert decoded['micro'] == [[801, 252, 803, 123]]
    rotated = post(client, world, headers, body(blocks, yaw_quarter_turns=1))
    assert rotated.status_code == 201, rotated.text
    micro = [edit for row in db.query(models.SpaceChunkSnapshot).all() for edit in space._decode_chunk_overlay(row)['micro']]
    assert [803, 252, 798, 123] in micro
    assert [801, 252, 803, 123] in micro


def test_legacy_key_builds_with_membership_and_revocation_enforced(client, db):
    owner, world, headers, key_id = setup(client, db, build=False)
    key = db.query(models.SpaceApiKey).filter_by(id=key_id).one()
    key.scopes = ['space:entity:create']
    db.commit()
    built = post(client, world, headers, body())
    assert built.status_code == 201, built.text
    db.query(models.SpaceWorldPlayerProfile).filter_by(user_id=owner.id).delete()
    db.commit()
    assert post(client, world, headers, body()).status_code == 403
    db.delete(key)
    db.commit()
    assert post(client, world, headers, body()).status_code == 401
    assert client.get(f'/space/api/v2/worlds/{world}/api-usage', headers=headers).status_code == 401


def test_build_failure_leaves_no_partial_terrain_or_quota(client, db, monkeypatch):
    _, world, headers, _ = setup(client, db)
    monkeypatch.setattr(space, 'SPACE_TERRAIN_DAILY_LIMIT', 1)
    blocks = [{'dx': x, 'dy': 0, 'dz': 0, 'color': 1} for x in range(2)]
    failed = post(client, world, headers, body(blocks))
    assert failed.status_code == 429, failed.text
    assert db.query(models.SpaceChunkSnapshot).count() == 0
    assert db.query(models.SpaceTerrainMutationBatch).count() == 0
    assert db.query(models.SpaceUsageBucket).count() == 0


@pytest.mark.parametrize('changes', [
    {'position': {'x_cm': 1, 'y_cm': 5000, 'z_cm': 16000}},
    {'position': {'x_cm': 0, 'y_cm': 5000, 'z_cm': 0}, 'yaw_quarter_turns': 1},
    {'definition_base64': 'invalid!'},
    {'created_at_ms': 0},
    {'created_at_ms': int(time.time() * 1000) + 600000},
])
def test_invalid_builds_never_mutate(client, db, changes):
    _, world, headers, _ = setup(client, db)
    failed = post(client, world, headers, body(**changes))
    assert failed.status_code in (409, 422), failed.text
    assert db.query(models.SpaceChunkSnapshot).count() == 0
    assert db.query(models.SpaceTerrainMutationBatch).count() == 0


def test_build_larger_than_browser_batch_commits_as_one_revision(client, db):
    _, world, headers, _ = setup(client, db)
    blocks = [{'dx': x, 'dy': 0, 'dz': z, 'color': 9} for x in range(20) for z in range(20)]
    result = post(client, world, headers, body(blocks))
    assert result.status_code == 201, result.text
    assert result.json()['built_blocks'] == 400
    assert result.json()['effective_changes'] == 400
    assert len(result.json()['chunks']) == 4
    assert db.query(models.SpaceTerrainMutationBatch).count() == 1
