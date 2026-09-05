# Space entity hosting — 1 credit per hour

Hosting is currently **disabled by default** in the backend (`SPACE_HOSTING_ENABLED=false`)
and hidden in the frontend (`SPACE_HOSTING_UI_ENABLED=false`). Disabled GET/PUT hosting
requests return `503 HOSTING_DISABLED` before database/auth lookups; the worker refuses
startup and cannot commit simulation or credit charges. API usage omits hosting prices
and allowances and reports `features.entity_hosting: false`. Ordinary entity creation,
blockset building, and browser execution remain available.

The additive hosting schema migration remains part of the migration chain; disabling
hosting does not remove model columns or skip migrations. The separate v5 inventory
migration is not controlled by this switch and currently discards old entity/Market
records. Review it before deploying to a database with data that must be retained.

The instructions below describe explicitly enabled development deployments. Set
`SPACE_HOSTING_ENABLED=true` in **both** the API and worker environments only after the
worker and database have been validated. The existing AWS deployment script deploys the
ordinary API/background images and does not start this worker.

An owner can run an existing entity without keeping a browser open. The service executes
the same QuickJS scripts and 20 Hz entity / 60 Hz physics code as Space. API creation and
ordinary `run-state` requests do not purchase hosting.

## External API

Use a Space API key with `space:entity:create` and `space:entity:run`, or the owner's normal
login token. Both endpoints require world membership and entity ownership; knowing the
UUID is not authorization. An administrator cannot bill another owner's account.

```http
PUT /space/api/v2/worlds/{world_id}/entities/{entity_id}/hosting
Authorization: Bearer edapi_<key-id>_<secret>
Content-Type: application/json

{
  "operation_id": "44517535-13b2-4457-ad16-63d0872927e9",
  "enabled": true,
  "max_credits": 1
}
```

`max_credits` is the maximum number of NEW credits this request authorizes, from 0 to 168.
The default is 1: run for one purchased hour and then pause. A new operation replaces the
remaining spending authorization; it does not add to it. Set 0 to use only prepaid time.
There is no unlimited automatic renewal. Retry uncertain requests with the same operation
UUID and body. Reusing a UUID for different content returns `409 ENTITY_OPERATION_ID_REUSED`.
Receipts survive stop/restart; retrying an older start returns its original receipt without
restarting anything. GET returns the current state.

```http
GET /space/api/v2/worlds/{world_id}/entities/{entity_id}/hosting
Authorization: Bearer edapi_<key-id>_<secret>
```

Responses include `state`, `enabled`, `execution_mode`, `reason`, `error`,
`credits_per_hour: 1`, `remaining_ms`, `budget_remaining_credits`, `billed_hours`,
`last_tick_at`, and `activity_radius_chunks`. GET also includes the current position,
tick count, script error and the latest 100 script log entries. `enabled` is intent;
`running` requires a recent committed tick. A worker outage appears as `starting` until
execution resumes. No time is deducted while the worker cannot commit simulation.

Pause with a new operation ID and `enabled: false`. This removes authorization for new
charges and retains unused prepaid time, the latest pose and component `self.state`.
The ordinary owner/admin Stop endpoint also pauses hosting. Resume through the hosting
endpoint. While hosted execution is active, browser checkpoints and browser execution
leases are rejected. To edit or repair an entity, pause with `release_to_browser: true` to explicitly transfer
execution back to the browser, then edit using normal owner login. Unused prepaid time
is retained. This explicit transfer prevents a viewer's periodic checkpoint from
overwriting a paused server snapshot.
Deleting an entity permanently deletes its remaining time together with the entity.

## Billing and recovery

- At the first successful simulation commit, atomically deduct 1 credit, add 3,600,000 ms
  of prepaid time, and consume the committed simulation duration. Subsequent commits
  consume prepaid time. An hour of successfully advanced simulation costs exactly 1 credit.
- Queueing, startup, a rejected transaction, a failed sandbox invocation and downtime do
  not consume credits or prepaid time. The initial implementation commits up to one second
  at a time; if overloaded, simulation slows instead of billing missed wall-clock time.
- Further hours are purchased only within the authorized budget and available balance.
  Exhaustion pauses with `budget_exhausted` or `insufficient_credits`. Adding credits alone
  does not restart a paused job; explicitly resume it.
- World edits, event cursor, entity definitions/snapshots, remaining time, balance and the
  `space_entity_hosting` credit log are committed in one database transaction. Fenced world
  leases and revision checks reject duplicate results and obsolete workers.
- A previous browser execution lease must expire before hosted execution starts.
  Reconstructing from the durable snapshot preserves component state and runtime pose;
  uncommitted simulation after a crash is discarded.

## Initial resource limits

At most 4 hosted entities run per world. Each has at most 512 voxels and 8 components;
local voxel coordinates, component offsets and pivots are limited to 16 m in magnitude.
Its voxel bounds stay within 32 m in X/Z of the initial hosting anchor and inside the
world height; its centre must have Y in [0,255].
The worker loads a three-chunk margin around that anchor for collision/query data, and
pauses on leaving the allowed area. It admits at most 36 total entity snapshots in the
combined region; these must meet the same definition complexity limits. Other entities
are static collision/query proxies. Hosted entities in the
same world are advanced together. This is the transitional multiplayer model: it does not
provide fully authoritative player movement, driving, or two-way physics against a
browser-executed entity.

QuickJS retains its per-entity memory/stack/interrupt budgets. The worker bounds each IPC
message to 32 MiB, each simulation response to 10 seconds, and aggregate world writes to
256 commands per commit. Existing per-user terrain quotas, chunk/zone batch limits and
entity storage quotas also apply. A violation pauses execution and preserves unused time.
Selection/assembly/spawning new entities are not exposed in hosted scripts. Guest scripts
receive no filesystem, networking, database credentials or LLM access; LLM calls are not
included in the hourly price. This tier needs load testing before increasing its limits.

Browser viewers poll the latest durable snapshot using the existing two-second entity
sync path. They show the current server pose without running its scripts locally; updates
are discrete snapshots rather than a high-frequency motion stream. The editor's Authority
display identifies server hosting and its hourly price.

## Run locally

The Node worker source belongs to the backend under `space/runtime/`. Its build imports
`@entropydrop/space-engine` from the sibling `entropydrop_space_engine` repository and bundles it
into `space/runtime/dist/hosting-runtime.mjs`. There is no separate copy of the engine.
The running worker needs only this bundle, the runtime's production npm dependencies
(including QuickJS/WASM), Node **24+**, and the normal Python backend dependencies.
It does not need a frontend checkout, a browser, or a renderer at runtime.

For a local build, keep the backend and `entropydrop_space_engine` repositories under
the same parent. Run `npm ci` in the engine repository first, then from the backend
directory run (no frontend checkout or frontend dependencies are required):

```sh
npm ci --prefix space/runtime
npm run build --prefix space/runtime
# Offline smoke test: does not enable hosting or connect to a database.
DATABASE_URL=sqlite:///:memory: python -m space.hosting_smoke
```

Rebuild after changing either the shared engine or the hosting TypeScript source.
`SPACE_HOSTING_RUNTIME_PATH` can override the absolute path to a **built** runtime bundle;
`SPACE_HOSTING_NODE` can select an absolute Node executable. The normal API deployment
does not need these build steps: hosting remains disabled by default, and the normal
Python image excludes `space/runtime/`.

1. Apply `alembic upgrade head` against the intended backend database.
2. Run the API normally and bootstrap a Space world using the existing login flow.
3. From the backend directory, run `python -m space.hosting_worker` with the same database
   and Redis configuration as the API.
4. Create a stopped entity and call the hosting endpoint with its ID.

By default the worker serves `SPACE_DEFAULT_WORLD_ID`. Set `SPACE_HOSTING_WORLD_IDS` to
a comma-separated list of up to four existing world UUIDs. Each world has an isolated Node
process and a database lease; another worker process can take over after expiry. The worker
probes the Node/QuickJS runtime before advertising availability. With no live worker,
enabling returns `503 HOSTING_WORKER_UNAVAILABLE` and makes no charge.

## Container deployment

The optional compose service uses the backend and engine repositories as build inputs. Its Node build stage
typechecks and bundles the backend runtime with the shared engine. The final image copies
only the built JavaScript and production Node dependencies, along with the Python backend;
it contains no frontend checkout or frontend development dependencies. It runs as a non-root
user, exposes no port, and has CPU, memory and process limits. Secrets are passed only to
the Python service; the child simulation process receives a minimal environment.

```sh
docker compose --profile tools run --rm migrate
docker compose up -d app
# Bootstrap the world before starting the worker.
docker compose --profile space-hosting up -d --build space-hosting
```

For a custom deployment build from the common parent directory:

```sh
docker build -f entropydrop_backend/Dockerfile.hosting -t entropydrop-space-hosting .
```

Deploy the matching backend migration/API and frontend build. Both runtimes use the
same engine source, so changing physics/script contracts requires updating the frontend
and rebuilding the worker image. This feature does not run migrations or change
production services automatically.
