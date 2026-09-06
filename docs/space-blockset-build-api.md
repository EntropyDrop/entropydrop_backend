# spaceAPI — External blockset building

[spaceAPI](../space/agent/spaceAPI.md) · [entityAPI](../space/agent/entityAPI.md)

spaceAPI handles Agent/client HTTP requests; entityAPI is called by entity component code (`self` / `ctx`) inside the runtime.

External agents can stamp a validated blockset directly into world terrain without an
open browser or a nearby player. This creates terrain, not a physics entity. Use the
[spaceAPI entity creation](space-entity-create-api.md) for scripted objects.

## Permission and pricing

Create an account API key under **Space → API Keys** or **Settings → API**. All keys, including existing keys, have full Space permissions and can build blocksets without selecting extra permissions. A normal login token can also call this endpoint. World membership is always required.

Creating entities and building blocksets currently cost **0 credits**, within the
existing allowances. Hosted entity execution is currently disabled. The future enabled tier costs **1 credit
per prepaid simulation hour**; see [hosting and budget rules](space-entity-hosting.md). Building does not enable
hosting or authorize credit spending.

## Request

```http
POST /space/api/v2/worlds/{world_id}/blocksets/build
Authorization: Bearer edapi_<key-id>_<secret>
Content-Type: application/json
```

```json
{
  "operation_id": "9157b55e-92e9-4dc5-bc52-13b79bcc8134",
  "created_at_ms": 1788580800000,
  "definition_base64": "<base64 InventoryResource Protobuf v5, kind blockset>",
  "position": {"x_cm": 16000, "y_cm": 5000, "z_cm": 16000},
  "yaw_quarter_turns": 0
}
```

Generate a fresh UUID and current Unix millisecond timestamp for a new operation; the
example timestamp is illustrative. `definition_base64` uses the same portable v5
InventoryResource format as Backpack exports and the Market. Publishing to the Market
is not required. The decoded blockset contains `name` and `blocks`, for example:

```json
{
  "type": "space-blockset",
  "version": 5,
  "name": "Platform",
  "blocks": [
    {"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 11259375},
    {"dx": 1, "dy": 0, "dz": 0, "mx": 0, "my": 0, "mz": 0, "block": 1, "color": 16777215}
  ]
}
```

- Origin coordinates are integer centimetres and must be multiples of 100 (the 1 m
  grid). Standard block offsets are metres; optional `mx/my/mz` in 0–4 identify 20 cm
  micro voxels within each local metre cell.
- `yaw_quarter_turns` is an integer from 0 to 3, default 0, rotating about +Y around the
  origin. A standard block at local `(0,0,0)` becomes `(-1,0,-1)` after two turns because
  the entire voxel volume rotates, rather than only its lower corner.
- Every resulting voxel must lie inside canonical world X/Z bounds and Y `[0,256)` m.
  Builds crossing an edge fail atomically; coordinates do not silently wrap.
- Stamping **replaces occupied terrain in touched 1 m cells**. Standard voxels replace
  any micro voxels in their cell. A cell receiving micro voxels is cleared first, then
  filled with exactly the submitted micro voxels. Other cells are preserved. This
  allows building into procedural terrain and makes the stamp independent of browser
  terrain loading.
- A request accepts at most **1,024 voxels** and **1 MiB** of decoded Protobuf. Existing
  portable resource bounds and overlap validation still apply. The ordinary browser
  terrain endpoint retains its 256-command cap.

## Response and retries

The `201` receipt includes `operation_id`, `world_id`, `batch_id`, `name`, `built_blocks`,
`applied` (terrain commands), `effective_changes`, `terrain_revision`, changed `chunks`,
`quota` (daily terrain usage), and `credits_charged: 0`.

Retry **the same operation ID, timestamp, canonical definition, and transform** to get
its original receipt without reapplying or consuming more allowance. Operations are
scoped to the owning account and world. Reusing an operation ID with different input
returns `409 BLOCKSET_OPERATION_ID_REUSED`. Receipts use the existing bounded terrain
retry window (default 30 days); expired timestamps return `409 TERRAIN_BATCH_EXPIRED`,
even after receipts are pruned. Future timestamps beyond the clock-skew allowance fail.

World locking serializes builds with other terrain writers and hosted simulation.
Terrain, usage counters, world revision, and receipt commit in one transaction. Errors
leave no partial build. Connected clients receive the normal terrain revision
notification and stream the changed chunks.

## Allowances

The API shares the account's manual/hosted terrain allowances. Defaults are:

| Limit | Default |
| --- | ---: |
| Build endpoint requests | 30/minute, 300/hour |
| Submitted terrain commands | 5,000/10 seconds |
| Effective terrain changes | 80,000/UTC hour, 100,000/UTC day |
| Touched chunks / zones per build | 16 / 4 |
| World submitted terrain commands | 5,000/second |

A micro cell can require a standard-air command and a micro clear in addition to its
new voxels. Replacing existing micro voxels also counts their removal as effective
changes. Thus a voxel count is not always equal to consumed terrain allowance.
Quota exhaustion returns `429` with structured usage and `Retry-After`. Size/chunk/zone
limits return `413`. An unchanged retry returns the original receipt, whose quota
snapshot may now be old.

Read current data with either a login token or a valid spaceAPI key:

```http
GET /space/api/v2/worlds/{world_id}/api-usage
Authorization: Bearer <token>
```

The response supplies `credits`, `pricing`, `quotas`, `limits`, `updated_at` and
`admin_quota_exemptions` and `features.entity_hosting`. Hosting-specific pricing and
allowances are omitted while the feature is disabled. Quotas include account API keys, this world's owned entities,
entity storage, owned running entities, all hosted entities in the world, and account
terrain changes per UTC hour/day, including reset timestamps. Terrain and credits are
shared across worlds; entity quotas are world-specific. Account API keys remain capped
at 20. Some administrator quota exemptions apply, as indicated in Settings.

**Settings → API** fetches this data on entry, every 30 seconds while visible, on window
focus, after key changes, and on manual refresh. Request-rate ceilings are displayed as
limits, not as remaining counters.
