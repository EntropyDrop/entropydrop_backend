# spaceAPI — Encode and create an entity

[spaceAPI](../spaceAPI.md) · [entityAPI](../entityAPI.md)

This guide sends HTTP requests through spaceAPI. Component scripts inside the submitted definition use entityAPI (`self` / `ctx`), executed by the entity runtime.

Use [inventory.proto](inventory.proto) as the binary schema. Generate language bindings with your Protobuf toolchain, or use a library that loads `.proto` files. For Python:

```sh
python -m pip install protobuf grpcio-tools
curl --fail-with-body "$SPACE_BASE_URL/space/agent/references/inventory.proto" -o inventory.proto
python -m grpc_tools.protoc -I . --python_out=. inventory.proto
```

The following example prepares **one stopped orange cube** six metres east of the player. Replace the component geometry and scripts with the user's requested construction. It refuses stale coordinates, saves a secret-free idempotent request file, and does not submit automatically. If the request file already exists, it preserves it for retry.

```python
import base64, json, os, pathlib, urllib.request, uuid
import inventory_pb2 as pb

base = os.environ['SPACE_BASE_URL'].rstrip('/')
key = os.environ['SPACE_API_KEY']
request_path = pathlib.Path('entity-request.json')
if not request_path.exists():
    request = urllib.request.Request(base + '/space/api/v2/players/me/position',
        headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(request, timeout=20) as response:
        pose = json.load(response)
    if pose['stale']:
        raise SystemExit('Player position is stale. Refresh it before building nearby.')
    resource = pb.InventoryResource(schema_version=5)
    root = resource.entity.root
    root.id, root.name = 'chassis', 'Orange cube'
    root.body.type = pb.BODY_TYPE_DYNAMIC
    root.body.mass = 10
    root.body.use_gravity = True
    root.body.collision_enabled = True
    root.blocks.add(dx=0, dy=0, dz=0, color=0xE58024)
    position = pose['position']
    target_y = position['y_cm'] + 300
    if not 0 <= target_y <= 25500:
        raise SystemExit('Choose a placement within the buildable vertical bounds.')
    body = {
        'operation_id': str(uuid.uuid4()),
        'definition_base64': base64.b64encode(resource.SerializeToString(deterministic=True)).decode(),
        'position': {
            'x_cm': (position['x_cm'] + 600) % 1638400,
            'y_cm': target_y,
            'z_cm': position['z_cm'] % 204800,
        },
        'yaw_quarter_turns': 0,
        'desired_run_state': 'stopped',
    }
    request_path.write_text(json.dumps({'world_id': pose['world_id'], 'body': body}))
print('Prepared:', request_path)
```

Once the request implements the user's authorized build, submit it:

```python
saved = json.loads(request_path.read_text())
request = urllib.request.Request(
    base + '/space/api/v2/worlds/' + saved['world_id'] + '/entities',
    data=json.dumps(saved['body']).encode(), method='POST',
    headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=30) as response:
    result = json.load(response)
print(json.dumps(result, ensure_ascii=False, indent=2))
```

Retry uncertain submissions from the same saved file, without generating a new operation ID. To prepare a different object, use a different request file. A transport failure is not evidence that creation failed.

`yaw_quarter_turns` is 0–3 and the supplied position is the construction origin. Its quarter-turn rotation is applied around that origin. The backend canonicalizes the bytes before storing them and returns the canonical SHA-256 digest. Limits include 8 MiB per definition, 64 components and 65,536 voxels; use the live `api-usage` endpoint for account/world allowances.

For blocksets, encode `InventoryResource.block_set` instead of `entity`, and submit to `/space/api/v2/worlds/{world_id}/blocksets/build` with `operation_id`, `created_at_ms`, `definition_base64`, `position`, and `yaw_quarter_turns`. Origins must be whole metres (each centimetre coordinate divisible by 100), and each build is capped at 1024 voxels. All API keys include blockset building; modifying terrain must be part of the user's request.
