# spaceAPI — External entity creation

[spaceAPI](../space/agent/spaceAPI.md) · [entityAPI](../space/agent/entityAPI.md)

spaceAPI handles Agent/client HTTP requests; entityAPI is called by entity component code (`self` / `ctx`) inside the runtime.

External agents use a long-lived, account-level spaceAPI key to submit an entity
definition directly to any world the account can access. Market publication is not part
of entity creation.

## Agent entry point and own position

Read the public Markdown guide at `/space/agent/SKILL.md` without authentication.
It links the Protobuf schema, entityAPI V2, and a complete encoding/submission example.

Existing keys with `space:entity:create` may read their owner’s saved position with
`GET /space/api/v2/players/me/position` (default world) or
`GET /space/api/v2/worlds/{world_id}/players/me/position`. Both require world membership,
return centimetres, yaw/pitch, `updated_at`, `age_seconds`, and `stale`, and send
`Cache-Control: no-store`. Positions older than 30 seconds are stale; missing or invalid
checkpoints return `404 PLAYER_POSITION_UNAVAILABLE`, never a random spawn.

## Security and ownership

- A normal EntropyDrop login creates, lists, and revokes API keys.
- A key is shown once. It begins with `edapi_`; PostgreSQL stores only its SHA-256 hash.
- All keys, including existing keys, have full Space permissions: read their owner's position, create/read/edit owned entities, start/stop them, and build blocksets. No permission selection or key reissue is needed.
- The API key resolves to its owning user. World membership, per-user entity quota, request
  rate limits, coordinate limits, definition size, Protobuf structure, schema version,
  scripts, component counts, and voxel limits are all checked by the backend.
- Created entities belong to the key owner. There is no market/browser source type; an
  owner or administrator can edit, checkpoint, control, or delete any entity they own.
- API keys are not login tokens. They cannot list the world, read binary definitions or snapshots,
  call browser checkpoints, general terrain edits, or the market API. The dedicated JSON configuration endpoint reads owned definitions and permits code/default edits.
- All keys can stamp blocksets at specified world coordinates;
  see [Blockset building and API allowances](space-blockset-build-api.md).
- Hosting is currently disabled (`503 HOSTING_DISABLED`). When explicitly enabled,
  all keys can start/pause/query paid hosting of their
  owner's entities. Hosting costs **1 credit/hour** and requires a spending budget;
  see [Entity hosting](space-entity-hosting.md).

## 1. Create an API key

The main site’s Space → API Keys page and in-game Settings → API tab manage the same keys; the game also shows current pricing and live allowances. A user may keep at most 20 active keys.

```http
POST /space/api/v2/api-keys
Authorization: Bearer <player-login-token>
Content-Type: application/json

{
  "name": "my-build-agent"
}
```

The legacy `scopes` request field is accepted for compatibility but does not restrict permissions. The response’s `scopes` metadata always lists every supported Space permission, including when listing existing keys. The response returns metadata plus `api_key` once:

```json
{
  "id": "AbCdEfGhJkMnPqRs",
  "name": "my-build-agent",
  "key_prefix": "edapi_AbCdEfGhJkMnPqRs_",
  "scopes": ["space:entity:create", "space:entity:run", "space:blockset:build", "space:entity:edit"],
  "created_at": "2026-09-03T00:00:00+00:00",
  "last_used_at": null,
  "api_key": "edapi_AbCdEfGhJkMnPqRs_<one-time-secret>"
}
```

List metadata or hard-revoke a key with normal login authentication:

```text
GET    /space/api/v2/api-keys
DELETE /space/api/v2/api-keys/{api_key_id}
```

## 2. Create an entity directly

`definition_base64` is a base64-encoded canonical `InventoryResource` Protobuf v5 whose
kind is `entity`. Coordinates are integer centimetres. X and Z must be inside the world's
canonical wrapped coordinate range; Y uses the Space vertical bounds. `yaw_quarter_turns`
is 0 through 3. The position is the entity construction origin.

The display name belongs to `Component.name` at every level. `Entity` contains only
`root` and `constraints`; API list metadata derives its name from `root.name` or, when
empty, `root.id`. Names can repeat, and changing a name does not change component IDs.

```http
POST /space/api/v2/worlds/{world_id}/entities
Authorization: Bearer edapi_<key-id>_<secret>
Content-Type: application/json

{
  "operation_id": "a9b3593c-e029-4a9f-a021-d9534709db9e",
  "definition_base64": "CAMaLi4u",
  "position": {"x_cm": 12050, "y_cm": 3400, "z_cm": 8290},
  "yaw_quarter_turns": 1,
  "desired_run_state": "stopped"
}
```

`operation_id` is the idempotency key. Retrying the same canonical definition, transform,
and run state returns the same entity. Reusing it for a different request returns
`409 ENTITY_OPERATION_ID_REUSED`. The default run state is `stopped`; every valid API key can also request `running`.

The backend base64-decodes the definition, requires the entity resource kind, validates
and canonicalizes every field, re-encodes it, and stores the canonical bytes plus an exact
SHA-256. Definitions are capped at 8 MiB. Each user may own at most 256 entities and
128 MiB of definition/snapshot data per world. By default, at most 8 entities may be in
the requested `running` state for one user, 64 for one world, and 16 in one chunk.
Browser checkpoints are additionally limited to 16 MiB per minute and 512 MiB per UTC
day for one user/world. These limits are enforced after idempotency lookup, so retrying
the same successful operation does not consume the allowance twice.

## Browser synchronization

The following endpoints require a normal player login token, never a spaceAPI key:

```text
GET /space/api/v2/worlds/{world_id}/entities
GET /space/api/v2/worlds/{world_id}/entities/{entity_id}/definition
GET /space/api/v2/worlds/{world_id}/entities/{entity_id}/snapshot
POST /space/api/v2/worlds/{world_id}/entities/browser
PUT /space/api/v2/worlds/{world_id}/entities/{entity_id}/checkpoint
DELETE /space/api/v2/worlds/{world_id}/entities/{entity_id}
PUT /space/api/v2/worlds/{world_id}/entities/execution-leases
```

Nearby browsers discover an API-created entity through the same AOI list as a browser-created
entity. Ordinary entities use one owner browser with the existing eight-second execution lease; observers render its stopped collision pose. Once the owner
browser checkpoints the entity, its bounded runtime snapshot is stored beside the definition.
Online mode does not use browser local storage for durable world entities.

Explicitly hosted entities instead execute in the independent server worker even without
nearby players. Browsers install their current runtime snapshots as static collision
poses and do not acquire execution leases. Creating with `running` alone never buys hosting.

## Agent configuration and run state

`GET /entities/{entity_id}/configuration`, `PATCH /entities/{entity_id}/configuration`, and `PUT /entities/{entity_id}/run-state` (under the world prefix) accept login tokens or API keys. See the [spaceAPI guide](../space/agent/spaceAPI.md#read-and-edit-an-existing-entity) for full key access, stopped-only editing, version checks, JSON examples and durable retry receipts. Entity states are running/stopped; there is no entity Pause action. Component code switches remain independent.

## Maintaining public Agent references

The schema and entityAPI references served under `/space/agent/` are checked-in copies of the shared engine documentation. After updating the engine, run `python space/sync_agent_docs.py` from the backend root. Use `python space/sync_agent_docs.py --check` to detect drift. Serving the public guide does not require an engine source checkout.
