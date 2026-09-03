# Space external entity-create API

This is the deliberately narrow API for an external agent. Its credential has
exactly one capability: create an entity from an already-published Space market
entity at a specified world transform. It cannot read the world, move or edit an
entity, change terrain, use the market, or Start/Stop an entity.

## Runtime and ownership model

- The API process never executes entity code or physics. It validates and copies
  the immutable Protobuf definition, records the placement and returns an entity ID.
- Nearby browsers discover records through the authenticated AOI endpoint. A short
  execution lease ensures that at most one of the owner's browser sessions advances
  one entity at a time. Other players render the entity in its stopped construction
  pose with collision/query shapes still enabled.
- The durable `desired_run_state` belongs to the entity owner. Only that owner or an
  administrator may change it with the internal Wrench control endpoint. An ordinary
  player therefore cannot stop another player's entity.
- A market-created instance is read-only to browser tools: changes must be made to the
  source resource and published as a new entity. A browser-authored instance is editable
  only by its owner or an administrator and is checkpointed back to this API. The entity's
  own `self.*` script actions remain available to the browser holding its execution lease.
- `self.stop()` remains an entity-local action. It stops physics and scripts, clears
  runtime state/motion and restores the stopped grid pose without granting the browser
  any owner authority.
- This transitional browser-execution slice does not yet relay live transforms from
  the lease holder to observers. Non-owner browsers display the stopped pose. The
  future authoritative worker design in `space-backend.md` replaces this limitation.

## 1. Create a scoped credential

Token management uses the player's normal EntropyDrop Bearer login and requires world
membership. The plaintext is returned only once; PostgreSQL stores only SHA-256.

```http
POST /space/api/v2/worlds/{world_id}/entity-create-tokens
Authorization: Bearer <player-login-token>
Content-Type: application/json

{"name":"my-build-agent"}
```

The response contains `scope: "entity:create"` and a secret beginning with `edsp_`.
Use the listing endpoint to inspect token metadata and hard-delete a token to revoke it:

```text
GET    /space/api/v2/worlds/{world_id}/entity-create-tokens
DELETE /space/api/v2/worlds/{world_id}/entity-create-tokens/{token_id}
```

A user may keep at most 20 active create tokens in one world. Never put the full player
login token in an agent configuration.

## 2. Create an entity

The resource must currently exist in the market and have `kind=entity`. Coordinates are
integer centimetres. X and Z use the canonical wrapped-world range rather than negative
aliases; Y is bounded by the Space position contract. `yaw_quarter_turns` is 0, 1, 2 or
3 and rotates about world Y in exact 90-degree steps, preserving the stopped construction
grid. The position is the entity's construction origin, not its centre of mass.

```http
POST /space/api/v2/worlds/{world_id}/entities
Authorization: Bearer edsp_<secret>
Content-Type: application/json

{
  "operation_id": "a9b3593c-e029-4a9f-a021-d9534709db9e",
  "resource_id": "MarketEntityId",
  "position": {"x_cm": 12050, "y_cm": 3400, "z_cm": 8290},
  "yaw_quarter_turns": 1,
  "desired_run_state": "running"
}
```

`operation_id` is required idempotency. Retrying the identical body returns the same
entity. Reusing the ID for a different request returns `409 ENTITY_OPERATION_ID_REUSED`.
Each user may own at most 256 durable world entities of either source kind per world.

Creation downloads the market object, decodes and canonicalizes Protobuf v3 again,
validates its semantic digest, then stores a private byte-for-byte copy and an exact-byte
SHA-256. Consequently, permanently deleting the market publication does not break an
entity that has already been created in the world.

## 3. Browser-only endpoints and online persistence

The following endpoints require a normal player login token. An `edsp_` credential is
rejected, so an external create-only agent cannot use them.

```text
GET /space/api/v2/worlds/{world_id}/entities
    ?center_x_cm=...&center_z_cm=...&radius_cm=...&limit=...
GET /space/api/v2/worlds/{world_id}/entities/{entity_id}/definition
GET /space/api/v2/worlds/{world_id}/entities/{entity_id}/snapshot
POST /space/api/v2/worlds/{world_id}/entities/browser
PUT /space/api/v2/worlds/{world_id}/entities/{entity_id}/checkpoint
DELETE /space/api/v2/worlds/{world_id}/entities/{entity_id}
PUT /space/api/v2/worlds/{world_id}/entities/execution-leases
PUT /space/api/v2/worlds/{world_id}/entities/{entity_id}/run-state
```

AOI distance uses the toroidal X/Z seams. Definitions are capped at 8 MiB, returned as
`application/x-protobuf`, and protected by size plus exact SHA-256 validation in the
browser. Run-state updates use `expected_revision`; stale writers receive
`409 ENTITY_REVISION_CONFLICT`. Execution leases last eight seconds and can be renewed
only by the owner.

In online mode these records are the sole durable source for world entities. The browser
does not load or save `entropydrop_space_entities.*`; it removes the current world's legacy
value on entry. Browser creation sends a validated Protobuf definition plus a JSON runtime
snapshot (maximum 4 MiB). Checkpoints use `expected_revision`, update the AOI position and
optionally replace the definition when its digest changes. Definitions and snapshots have
independent exact-byte SHA-256 values. Deletion is owner/admin-only and permanently removes
the row, definition and snapshot without an audit copy. Offline mode uses browser storage
and does not use any of these endpoints.
