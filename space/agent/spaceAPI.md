# spaceAPI — Agent HTTP interface

[spaceAPI](spaceAPI.md) · [entityAPI](entityAPI.md)

**spaceAPI** is used by agents and clients for authenticated HTTP requests. **entityAPI** is called only by entity component code through `self` and `ctx` inside the runtime. To program an entity, generate entityAPI code and submit it as part of the entity definition through spaceAPI. Reading entityAPI documentation does not grant direct runtime access.

Use the backend origin and `edapi_…` API key supplied by the user. A complete handoff contains **backend origin + API key + this document URL + the requested build**. Documentation is public; player positions and writes require authentication. Send the key only to the user-approved backend in `Authorization: Bearer <API_KEY>`, never in a URL or a public artifact.

`localhost` means the machine executing the request. A remote Agent needs a backend address it can reach. Preserve the user's intended server; do not substitute another server when a connection fails.

All valid spaceAPI keys, including existing keys, have full Space permissions. Create keys under **Space → API Keys** or **Settings → API** using only a name; no permission selection or reissue is needed. World membership, entity ownership, quotas and any hosting budget still apply.

## Find the player's position first

```sh
curl --fail-with-body "$SPACE_BASE_URL/space/api/v2/players/me/position" \
  -H "Authorization: Bearer $SPACE_API_KEY"
```

This reads the key owner's latest saved position in the server's default world. No world ID is needed beforehand. To target a known world the owner has joined, use `GET /space/api/v2/worlds/{world_id}/players/me/position`.

```json
{
  "world_id": "00000000-0000-4000-8000-000000000002",
  "position": {"x_cm": 744710, "y_cm": 1500, "z_cm": 55290},
  "yaw_q15": 0,
  "pitch_q15": 0,
  "updated_at": "2026-09-06T12:00:00Z",
  "age_seconds": 1.2,
  "stale": false,
  "stale_after_seconds": 30,
  "source": "checkpoint"
}
```

These coordinates are **integer centimetres**, not metres. `yaw_q15 / 32767 * π` converts yaw to radians; yaw 0 faces -Z. The position is the player's physics/feet position, not the camera eye. After the player moves or turns, the browser normally saves the changed pose within about five seconds. A stationary player may retain an older checkpoint; this is not a live tracking stream. To refresh an old position, move slightly in the online world and retry after a few seconds.

- `stale: true`: the checkpoint is older than 30 seconds or has an invalid future timestamp. For a request to build "near me", obtain a fresh checkpoint or ask the user to confirm using the old position.
- `404 PLAYER_POSITION_UNAVAILABLE`: there is no usable checkpoint. Ask the user to enter the online world and retry. Never use bootstrap's random fallback as their real location.
- `403 WORLD_MEMBERSHIP_REQUIRED`: the player has not joined that world.
- `401`: missing, invalid, or revoked credentials. Do not retry with someone else's credential.

Both position routes are self-only. All existing keys work without reissuing them. They cannot read another player's position or use the general login APIs.

## Create the requested object

1. Read [entity encoding and a complete request example](references/entity-create.md).
2. Encode a canonical **InventoryResource Protobuf v5** using [inventory.proto](references/inventory.proto). Raw JSON in `definition_base64` is not accepted.
3. For programmable parts, read the [entityAPI reference](entityAPI.md). Only use its documented methods.
4. Read `GET /space/api/v2/worlds/{world_id}/api-usage` with the same key for current quotas and pricing.
5. Place the object a few metres away from the player's coordinates, leaving room for its full bounds. Player Y is not a terrain-height query. Account for terrain clearance, gravity, and wrapped coordinates.
6. Submit `POST /space/api/v2/worlds/{world_id}/entities`. Retain the request and `operation_id`; retry an uncertain submission with exactly the same body. A successful response includes the entity ID, requested run state and execution mode.

All keys can create entities (stopped or running), read their owner's position, read and edit owned entity configurations, start/stop entities, and stamp blocksets through `POST /space/api/v2/worlds/{world_id}/blocksets/build`.

Creation and blockset building are free within quotas. `running` uses the owner's browser execution lease; it does not buy hosting and cannot run while that browser is absent. Ordinary nearby browsers discover entities through a roughly two-second poll. If the owner is not viewing the area, creation can succeed before the object is visible or simulating.

## Read and edit an existing entity

Use the entity ID returned by creation or copied from the Entity Editor. The following paths share the prefix `/space/api/v2/worlds/{world_id}/entities/{entity_id}`. They accept a player login token or a spaceAPI key and enforce world membership and entity ownership (administrators may also operate).

| Request | Purpose |
| --- | --- |
| `GET /configuration` | Read `{ "entity": <metadata>, "definition": <decoded InventoryResource v5 JSON> }`, including component code and authored body defaults |
| `PATCH /configuration` | Modify selected components' code, names and body defaults while stopped |
| `PUT /run-state` | Set `desired_run_state` to `running` or `stopped` |

Configuration reads are private and not cached. Binary definition/snapshot, AOI listing, checkpoint and execution-lease endpoints remain browser login interfaces. Use the JSON configuration endpoint for Agent reads; a spaceAPI key is not a general login credential.

**There are only two entity states: running and stopped.** Start enables physics and all component scripts. Stop disables both, restores authored BodyConfig defaults and child construction poses, and clears state, clocks, forces and velocities. It preserves the last saved root position and orientation. Individual component code switches do not create an entity Pause state. Browser execution still needs the owner's online browser and lease.

Read the current configuration, stop the entity if running, edit it, then start it if requested. Each write returns entity metadata with a new `revision`; use that returned revision for the next write. Stop and edits invalidate old execution leases and prevent stale browser checkpoints from overwriting the result.

```sh
SPACE_ENTITY_URL="$SPACE_BASE_URL/space/api/v2/worlds/$WORLD_ID/entities/$ENTITY_ID"
curl --fail-with-body "$SPACE_ENTITY_URL/configuration" \
  -H "Authorization: Bearer $SPACE_API_KEY"
```

Stop with `PUT /run-state` and a JSON body like the following (replace the UUID and revision with your operation ID and current revision):

```json
{"operation_id":"43b0697b-62fc-43f2-a572-2c74551a9df7","expected_revision":1,"desired_run_state":"stopped"}
```

If Stop returns revision 2, send this JSON to `PATCH /configuration`, with `Content-Type: application/json` and the same Authorization header:

```json
{
  "operation_id": "70a509c3-3421-4d93-b2c4-25627f5eff23",
  "expected_revision": 2,
  "components": [
    {
      "id": "root",
      "script": "self.state.frames = (self.state.frames || 0) + 1;",
      "body": {"type": "dynamic", "mass": 80, "friction": 0.8, "useGravity": true}
    }
  ]
}
```

Only named fields change; other components, voxels, constraints and properties remain intact. `components` contains 1–64 unique, existing component IDs. Each patch accepts `name`, `script`, and/or `body`. Use `script: ""` to clear code. Null values, duplicate IDs, unknown fields and empty patches are rejected. No owner, permission, runtime-state or arbitrary object-path updates are accepted.

`body` accepts `type` (`dynamic` / `kinematic`), `mass` (0.1–10¹²), `restitution` / `friction` (0–1), and Boolean `useGravity` / `collisionEnabled`. It merges into persisted defaults, which are also restored on Stop. Script bodies are limited to 64 KiB per component and 512 KiB per entity. Stored-definition validation checks the schema and limits; JavaScript compilation and execution errors are reported by the runtime.

If the edit returns revision 3, start with another `PUT /run-state`:

```json
{"operation_id":"be2a45b6-c45f-486a-8559-f64d7f1fd0bb","expected_revision":3,"desired_run_state":"running"}
```

`expected_revision` is required for configuration edits; always supply it for run-state commands as well. `409 ENTITY_REVISION_CONFLICT` requires reading the current configuration and reconsidering the edit. `409 ENTITY_MUST_BE_STOPPED` requires stopping before editing. Hosted definitions cannot be edited through this endpoint (`ENTITY_HOSTED_EDIT_FORBIDDEN`); hosting has its separate API and must be explicitly enabled. A normal run-state request never purchases hosting.

Keep a distinct `operation_id` for each new command. Retry an uncertain write with the exact same ID **and body**. Successful edits and run-state commands have durable receipts: a delayed retry returns its original acknowledgement and cannot undo a newer Stop or edit. That acknowledgement can contain an older revision, so read configuration again when current state matters. Reusing the ID with a different command returns `409 ENTITY_OPERATION_ID_REUSED`.

HTTP success means the backend saved the definition or desired state. It does not assert that an online runtime has already applied it. Browsers discover changes through the existing poll.


## Geometry and physics rules

- Right-handed, Y-up; +X right, -Z forward. X wraps into `[0,16384)` metres and Z into `[0,2048)`. Buildable Y is `[0,256)` metres.
- Standard voxels are 1 metre; micro voxels are 0.2 metres. A micro voxel uses `dx/dy/dz` plus offsets `mx/my/mz` in 0–4, encoded as `micro_index = 1 + mx + 5*my + 25*mz`.
- Components form one tree. IDs are unique; display names belong to each component's `name`, including `root.name`.
- Authored Stop poses must align to the 0.2-metre grid and **must not overlap between components**, even when collisions are disabled. Reserve wheel and joint clearances in the chassis.
- Dynamic bodies use force/torque. Direct pose setters work only on kinematic bodies. Persist default body settings in the definition; script setters are runtime changes.
- Scripts run at 20 Hz in bounded QuickJS. Only the mounted entity receives keyboard input. Add a seat for drivable vehicles; V mounts/unmounts. W/A/S/D and Space are available to scripts.
- For suspension, the current constraints are point/hinge/weld. There is no built-in spring or prismatic constraint; use a tested force-based controller (for example, raycast spring/damper suspension) or supported articulated mechanisms.

## Failure handling

`422 ENTITY_DEFINITION_INVALID` means the Protobuf, grid, hierarchy, names, scripts or body data failed validation. Correct the definition before retrying; do not silently create an unrelated fallback. `409 ENTITY_OPERATION_ID_REUSED` means a prior successful operation used a different request body. `429` means a quota/rate limit: inspect the response and usage rather than duplicating submissions. Preserve entity IDs and do not delete unrelated objects to free quota.
