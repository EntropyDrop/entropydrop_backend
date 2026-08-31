# Space Multiplayer V2: Real-Time Backend and Persistence Design

> Status: shared-user bootstrap, durable latest-player snapshots, paginated authored chunk
> AOI-paged Zstd chunk overlays, bounded epoch-1 idempotent terrain mutation batches,
> and a transitional realtime player relay
> are implemented. The relay uses one-use tickets, binary MessagePack, 20 Hz changed-pose
> input, 10 Hz AOI snapshots, Redis cross-instance fanout, and five-second PostgreSQL
> checkpoints. A Redis-backed 32-slot FIFO admission lease now gates join tickets; queued
> browsers play in an isolated offline world while polling only their one-based position.
> Terrain remains a durable REST cursor with realtime invalidation. The authoritative
> simulation gateway/worker, protobuf input protocol, event stream, and PostgreSQL-backed
> admission design below remain the target real-time architecture.

The browser persists only unacknowledged terrain batches while connected to the
authoritative backend. Acknowledged chunk snapshots are fetched in overlapping AOI
windows and are not copied into every player's IndexedDB. New epoch-1 batches carry a
stable creation timestamp; after the configured retry window they are rejected rather
than reapplied, allowing their receipts to be removed safely. Legacy epoch-0 receipts
remain indefinitely for already-persisted clients.
>
> Contracts: see [`space/contracts/schema.sql`](../space/contracts/schema.sql) for the
> database and [`space/contracts/protocol.proto`](../space/contracts/protocol.proto)
> for the real-time binary protocol.

## 1. Decision and Non-Negotiable Boundaries

Multiplayer uses an **authoritative server, one writer per zone, in-memory hot
state, and snapshot-plus-event persistence**.

- PostgreSQL never participates in the 60 Hz tick, stores no per-frame positions,
  and does not accept hot writes for individual microblocks.
- Clients send input and intent only. The server decides the final player, entity,
  script, collision, and terrain state.
- Exactly one authoritative worker may write a zone at a time. Leases and fencing
  tokens prevent split brain.
- Real-time traffic uses binary WebSocket. REST is limited to authentication,
  world administration, and large offline jobs.
- Identity is exclusively the existing EntropyDrop `users` row and Bearer token.
  Space creates no user/auth mapping table. `users.skin_url` and
  `users.skin_type` are the only player-skin source of truth.
- Each world admits at most **32 occupied session slots**. Reserved handshakes,
  active players, and reconnect-grace sessions consume a slot; excess players
  wait in a FIFO admission queue without creating hot world state.
- The three-category backpack is browser-local (`space.backpack.v2`). PostgreSQL
  and the real-time protocol store no backpack slots, selection, names, or quantities.
- The server's in-memory model and atomic commands enforce standard-block and
  microblock coexistence rules; client state is never trusted.
- User scripts never execute through `new Function` in the gateway or main tick thread.

The release target is one large persistent world with at most 32 occupied player slots;
many worlds scale horizontally across gateways/workers. Load tests determine whether the
target ships; theoretical concurrency is not a promise.

## 2. Performance Targets

These engineering targets exclude public-network RTT between the user and the region:

| Metric | Target |
|---|---|
| Authoritative simulation | Fixed 60 Hz; 16.67 ms tick budget |
| Client input | 30 Hz; each batch redundantly carries the latest three frames |
| Network state frames | 20 Hz; local-player input acknowledgment and remote-entity deltas |
| Online admission | Maximum 32 occupied session slots per world; FIFO queue above the cap |
| Entity activation | AOI-triggered asynchronous wake; no storage I/O on the tick thread |
| Terrain command latency | Same-zone p99 < 10 ms; durable acknowledgment p99 < 50 ms |
| Database queries in a tick | 0 |
| Active chunk reads | Worker memory hit; asynchronous snapshot/event load on miss |
| Failure takeover | Reassign and begin recovery within 2 seconds of worker failure |
| Reconnect | Resume by `event_id` within retention, otherwise fall back to chunk snapshots |
| Horizontal scaling | Stateless gateways; add workers by world and zone |

Any feature that requires synchronous PostgreSQL writes every frame is an
architectural error.

## 3. Service Topology

```text
Browser
  | Binary WebSocket (protobuf envelope + packed snapshots)
  v
Gateway ------- Authentication / 32-slot admission queue / rate limits / routing
  |
  v
Zone Worker (authoritative actor; one writer per zone)
  |- 60 Hz player and entity physics
  |- AOI / snapshots / client reconciliation
  |- active chunk overlay and microblocks
  |- sandboxed script workers
  `- durable command batch
       |
       |- PostgreSQL: control plane, ordered events, recovery snapshots
       `- Object storage: large entity definitions and optional archives
```

### 3.1 Gateway

- Validate short-lived access tokens and issue join tickets restricted to one world.
- Reserve one of 32 world session slots before allocating a player actor. When all
  permitted slots are occupied, hold only a small queued WebSocket record and send
  `QueueStatus`; do not subscribe AOI or load a player snapshot.
- Read membership permissions and apply first-stage rate limits. Workers revalidate
  every sensitive permission.
- Own client connections but no authoritative state. A resume token allows recovery
  after a gateway restart.
- Route by `(world_id, zone_x, zone_z)`. Crossing a zone changes the backend route,
  not the public connection.
- Enforce one occupied session per `(world_id, user_id)`. Only the matching,
  hashed resume token may rebind a reconnect-grace slot.

### 3.2 Zone Worker

- A zone is an actor. One thread mutates its authoritative state in order, avoiding
  fine-grained locks between blocks and entities.
- The default zone contains `32x32` chunks, or `512x512` standard cells. Several zone
  actors may share a process.
- Active chunks, admitted players, awake entities, and script command buffers stay in memory.
- Each tick consumes only commands that have passed basic validation and deterministic
  ordering.
- Adjacent zones keep read-only ghosts for cross-border AOI and collision. Only the
  home zone can mutate an entity.

### 3.3 PostgreSQL

- Control plane: references to existing users, worlds, membership, admission
  slots/queue, and leases.
- Durable data plane: compressed chunk snapshots, world events, and entity/player
  recovery snapshots.
- It is not a broadcast bus, a live position service, or per-frame physics history.

## 4. Toroidal World Partitioning

The logical world remains `1024x128` chunks and wraps on both X and Z. With the
default `zone_size_chunks=32`:

- X has 32 zones and Z has 4, for 128 logical zones.
- `zone_x = floor(wrapChunkX(cx) / 32)`, with the same rule for Z.
- AOI, collision neighbors, rays, and entity migration use the shortest wrapped
  distance, never ordinary absolute distance.
- Zone `(31,z)` is adjacent to `(0,z)`; Z wraps identically.

Interactive commands must finish in one zone. Multi-zone STL imports and large fills
run as offline jobs so they do not consume the real-time tick. An entity may extend
across a border, but its pivot defines the authoritative home zone; neighbors hold
only ghost collision proxies.

### 4.1 Single Writer and Fencing

`zone_leases` stores `owner_instance`, `fence_token`, and `lease_until`:

1. A worker acquires or renews a zone lease. Every reassignment increments the token.
2. Every durable event carries that token.
3. The `world_events_validate_fence` trigger holds a shared lock on the matching
   lease until commit and rejects stale tokens, closing the validate/reassign race.
4. A recovered old worker therefore cannot overwrite its successor.

Leases exist for takeover and split-brain prevention, not per-physics-tick updates.
Start with one-second renewal and three-second expiration, then tune with fault injection.

## 5. Authoritative Tick

The fixed 60 Hz order cannot vary with asynchronous callback timing:

1. Collect input batches and deduplicate/order them by session `input_sequence`.
2. Collect terrain, entity, and script-management commands; validate permission,
   distance, budget, and expected chunk revisions.
3. Apply completed entity lifecycle transitions (`WAKING`, `QUIESCING`, and
   checkpoint completion) at the tick boundary; asynchronous reads/writes themselves
   never run here.
4. Run sandboxed scripts for `ACTIVE`/`COOLING` entities. They write only to bounded
   command buffers.
5. Integrate player/entity physics and resolve local plus neighboring-ghost collision.
6. Atomically apply accepted structural mutations at the tick boundary; increment
   entity and chunk revisions.
7. Build 20 Hz AOI state frames and reliable world events.
8. Queue durable events for batched persistence; background workers create snapshots.

The tick thread never waits for ordinary snapshot writes. Commands that must be
durable, including terrain edits, solidification, and script saves, return `ACCEPTED`
only after their event batch commits. Events from one tick use a single transaction
for group commit.

When the persistence queue exceeds its threshold, structural mutations receive
backpressure while movement and heartbeats continue. The queue must never grow without
a bound.

## 6. Network Synchronization

### 6.1 Transport

- Binary WebSocket over TLS (`wss://`) is the baseline because browsers support it and
  it preserves order. The gateway rejects a non-allowlisted `Origin`, an invalid one-use
  join ticket, non-binary frames, and protocol envelopes above the applicable size limit.
- [`protocol.proto`](../backend/protocol.proto) defines the outer envelope. High-rate
  entity state and chunk snapshots use a dedicated bit-packed codec inside `bytes`.
- The real-time channel never sends JSON, arrays of block objects, or Base64.
- Large chunk snapshots use the `snapshot_id`, zero-based `fragment_index`,
  `fragment_count`, `compressed_size`, and full-payload SHA-256 fields in
  `ChunkSnapshot`. Fragments are sent in separate envelopes and scheduled against a
  per-connection budget, so one snapshot cannot block a state frame. Receivers reject
  duplicate/out-of-range fragments, inconsistent metadata, size overruns, and hash
  mismatches before decompression.
- TypeScript maps `uint64` to `bigint` or Long, never `number`.
- Disable WebSocket `permessage-deflate`: chunk/build payloads already use explicit Zstd,
  and implicit compression adds unpredictable CPU/memory and compression-bomb surface.
- Initial limits are 64 KiB for ordinary inbound packets, 2 MiB compressed / 16 MiB
  expanded for `LocalBuildPlacement`, and 64 KiB payload per outbound snapshot fragment.
  Larger imports use the offline job API.

Every connection has four bounded scheduler classes:

1. reliable control: presence, world events, command results, interest resets, pong and disconnect;
2. the newest local correction and nearby player/entity state;
3. current-AOI chunk fragments and immutable-definition readiness;
4. distant/decorative data.

Reliable class-1 data is ordered and never silently dropped. State frames are supersedable:
only the newest unsent frame per observer survives. A snapshot may be cancelled only when
its revision is obsolete or the matching `interest_sequence` has left AOI; re-entry starts
a new `snapshot_id`. Application-level fragments are separate WebSocket messages so a
large snapshot cannot become an uninterruptible head-of-line message.

At 256 KiB queued bytes or 500 ms estimated drain time, stop class 3/4, coalesce class 2,
and keep class 1. At 2 MiB or five continuous seconds above the soft threshold, send
`DISCONNECT_SLOW_CLIENT` if possible and close. The browser also watches
`WebSocket.bufferedAmount` and stops nonessential sends. Reassembly is bounded by count,
declared compressed/expanded bytes, hash, and a 10-second timeout.

WebTransport datagrams may be added later without changing message semantics. WebSocket
remains the required compatibility baseline.

### 6.2 Admission, Queueing, and Session Leases

`worlds.max_online_players` defaults to and is capped at 32. A slot is occupied in
`RESERVED`, `ACTIVE`, or `RECONNECT_GRACE`; waiting sockets do not count as players.
Admission is a low-frequency control-plane transaction:

1. The client obtains a short-lived, world-scoped join ticket and sends `ClientHello`.
2. The gateway starts a transaction with `lock_world_admission_user(world, user)`
   before reading either admission table. It then looks for the user's existing slot.
   A valid resume token may rebind `RECONNECT_GRACE`; a second ordinary connection
   receives `ALREADY_CONNECTED`.
3. Otherwise the gateway deletes expired leases/queue rows, then selects one free
   `world_session_slots` row with `slot_number < max_online_players` using
   `FOR UPDATE SKIP LOCKED`. It increments `lease_epoch`, writes a hash of the resume
   token, and enters `RESERVED` for at most 10 seconds.
4. If no slot is free, an idempotent `(world_id, user_id)` FIFO row is inserted in
   `world_join_queue`. The socket receives `QueueStatus` with a one-based position,
   count, limit, and heartbeat interval. It has no player entity, snapshot, or AOI.
5. Queued clients send rate-limited `Ping` heartbeats to refresh a 30-second queue lease.
   Disconnect/expiry deletes the row.
   Promotion locks the oldest live row and a free slot in one transaction; only then
   does the gateway send `ServerHello` and begin world loading.
6. The first valid post-hello packet changes `RESERVED` to `ACTIVE`. Gateway heartbeats
   renew the active lease only when `(session_id, connection_id, lease_epoch)` all match.
7. Unexpected loss changes `ACTIVE` to `RECONNECT_GRACE` for 20 seconds and still
   consumes the slot. A valid resume reuses it; expiry snapshots the player, frees the
   slot, and promotes the next queued user. Explicit `LeaveWorld` may free it at once.

Queue position is advisory: expired tickets and reconnects can move it non-linearly.
Promotion order is `(queue_sequence, queue_ticket_id)`, never a client timestamp.
Transactions and the unique user index prevent both capacity overflow and two
connections controlling one player. A queued connection is rate-limited and capped in
memory just like a login connection, so the queue cannot become a denial-of-service path.

After reservation, every join reads `world_player_profiles` for the stable
`player_entity_id`, but that table deliberately contains no birth coordinates. Bootstrap
first validates `player_snapshots`; when no valid snapshot exists, the server samples a
bounded random X/Z candidate and returns it only as this entry's ephemeral start. The
browser immediately checkpoints that start, then saves wrapped position and yaw every two
seconds and with a small keepalive request before page suspension. Later bootstraps use the
latest per-user snapshot directly. A missing or invalid snapshot simply produces another
safe random start; no permanent birth point exists in PostgreSQL.

### 6.3 Input Prediction and Reconciliation

- The client immediately predicts the local player and retains unacknowledged input
  in a ring buffer.
- `StateFrame.acknowledged_input_sequence` identifies the last processed input.
- On authoritative state, the client rolls back and replays later inputs. Small errors
  are visually smoothed; large errors snap immediately.
- Remote players and entities use roughly 100 ms of interpolation and do not run full
  client-side physics. `RemotePlayerDelta` carries position, velocity, body/look
  orientation, and pose/animation. Skin bytes use the immutable HTTPS URL from
  `users.skin_url`; they are sent neither per frame nor through WebSocket.
- Client position, velocity, and collision results are never authoritative input.

### 6.4 Reliable Player and Entity Presence

Lossy/coalesced state frames cannot be the only source of object existence. Each observer
therefore owns independent player/entity presence epochs:

- On admission, resume, or `InterestReset`, the server sends `PlayerPresenceBatch` and
  `EntityPresenceBatch` with `full_reset=true`; the client clears the old set, installs
  every `ENTER_AOI`, then accepts only state frames carrying those epochs.
- Later AOI entry/leave, identity/skin-URL change, entity definition change, and entity
  run-state change are reliable class-1 presence events. `removed_*_ids` in a state frame
  are only latency hints; reliable presence is authoritative.
- Player `ENTER_AOI` carries stable player id, display name, immutable
  `skin_url`/`skin_type`, and
  one full initial `RemotePlayerDelta`. Subsequent changed-mask deltas synchronize position,
  velocity, body rotation, look yaw/pitch, pose and animation at 20 Hz. The skin
  fields are included only on entry/identity change, not in ordinary deltas.
- Entity `ENTER_AOI` carries entity id/revision, desired run state, runtime health,
  immutable definition id/hash, and one full initial runtime delta. Definition changes are
  reliable; ordinary transform/velocity changes remain coalescible.
- Entity-definition bytes are fetched through authenticated HTTPS and cached by id
  plus content hash. Skin PNGs are fetched directly from the immutable URL already
  authorized and stored by the main site. Neither competes with 20 Hz state frames.

The REST bootstrap refuses entry with `SKIN_REQUIRED` when
`users.skin_url` is null or empty. The browser downloads and decodes that
64x64 PNG before constructing the game scene or opening WSS. There is no bundled
default, Space skin upload, appearance table, appearance command, or client-supplied
skin URL. Changing character skin happens only through the existing `/skin/edit`
flow; a changed immutable URL is announced by reliable player presence.

### 6.5 Area of Interest

- The default subscription radius is eight chunks, matching near-field rendering.
- The server advertises default/max radii in `ServerHello`; V2 accepts radius 1..8 and
  rejects larger values rather than silently granting more visibility or wake load.
- Every `InterestUpdate` has a strictly increasing `interest_sequence` and a bounded list
  of cached `(chunk, revision)` pairs. Snapshots, resets and state frames echo the accepted
  sequence; clients discard stale asynchronous output after moving.
- A chunk entering AOI receives a full `ChunkSnapshot`; an already loaded chunk receives
  only `WorldEvent` deltas.
- A zone-local spatial hash selects entity deltas for each observer.
- Leaving AOI sends entity removals. Clients may cache chunks but must verify revision
  before reuse.
- Entity AOI membership is reference-counted against `entity_chunk_coverage`. The first
  admitted observer of any covered chunk creates a wake reference; overlapping AOIs do
  not start duplicate entity runtimes.

### 6.6 Reconnection

- The gateway issues an expiring, session-bound resume token.
- Reconnect grace occupies the original admission slot; queued users cannot take it
  until the 20-second grace expires. A resume token is stored only as a server-side hash.
- Reconnect submits the last event/tick in `ClientHello`, then resends unacknowledged input
  and commands. Presence epochs are acknowledged separately and always receive a full reset
  when the gateway cannot prove continuity.
- If events remain in retention, the server sends deltas and current entity state.
  Otherwise it sends `InterestReset`, and the client discards relevant revisions before
  requesting snapshots again.
- `packet_sequence` detects connection-level gaps. `operation_id` provides idempotency
  across reconnects; they are not interchangeable.

## 7. Terrain Memory Model and Chunk Codec

V2 removes per-cell rows from `voxel_edits` and `micro_cells`. A chunk stores one
compressed overlay:

1. The `16x128x16=32768` standard cells use two-bit states:
   - `00`: inherit procedural terrain;
   - `01`: explicit AIR tombstone;
   - `10`: player-authored solid color block;
   - `11`: reserved.
2. Solid colors use a chunk palette and bit-packed palette indices, falling back to a
   24-bit RGB stream when color diversity is too high.
3. Microblocks group by parent cell: parent index, 125-bit occupancy, and palette indices
   for occupied cells.
4. A standard solid and a microblock group cannot coexist in one parent cell. Both the
   encoder and command validator enforce this invariant.
5. Encode before Zstd compression. `content_hash` is SHA-256 of the canonical uncompressed
   payload.

Untouched chunks have no `chunk_snapshots` row. Workers generate base terrain from
`seed + terrain_generator_version`, then apply the overlay.

The transitional FastAPI slice stores the same logical standard/micro overlay as
canonical raw JSON (`codec=0`) and verifies its SHA-256 on read. This makes cross-browser
persistence available before the worker and packed codec ship without introducing
per-cell database rows. The packed codec can replace payload encoding in place while
preserving the table, revisions, and REST response model.

This is logically complete persistence of every block, including microblocks: procedural
cells are reproduced exactly by immutable seed/generator version, while every authored
standard AIR/solid state and every 5x5x5 micro occupancy/color is present in snapshots plus
ordered events. “All blocks in the database” does not mean billions of per-cell rows; it
means the database contains everything required to reconstruct the same authoritative cell.

At runtime, decoded data uses SoA and TypedArray layouts with no per-cell JavaScript
objects in hot loops. Edits mark chunks dirty; background threads compress and hash.

### 7.1 Distant Toroidal LOD Bootstrap Cache

The first playable slice now ships a build-time immutable base artifact for the default
world: a `512x64` height lattice plus a `1024x256` RGBA albedo texture whose base level is
exactly 1 MiB (1,048,576 bytes). Vite gives the binary a content-hashed URL, so normal
browser/CDN HTTP caching shares it across entrants. Bootstrap returns the authoritative
seed and `terrain_generator_version`; the client validates both plus the cache schema,
dimensions and expanded byte count before installing it. Any mismatch, corruption or
fetch failure falls back to the same deterministic local generation. The previous
uncached path measured roughly 180 ms on a representative development machine.

This implemented base cache does not represent authored changes outside the player's
AOI. Before persistent multiplayer world edits ship, add revisioned authored LOD tiles
over the immutable base; never regenerate or query the full torus for each joining
player.

The complete multiplayer form is a shared **versioned distant-LOD snapshot**, not an
on-demand PostgreSQL scan for each joining player:

- The immutable base key is `(world_id, seed, terrain_generator_version,
  lod_schema_version)`. The client must use the seed returned by bootstrap; a local
  default seed is never authoritative.
- Store compressed/quantized height and albedo artifacts in object storage/CDN with a
  content hash, immutable URL, long-lived HTTP caching and bounded expanded size.
  PostgreSQL stores only metadata/pointers when needed, not the texture or vertex blob.
- Maintain authored distant summaries as dirty LOD tiles aligned with zones. Each tile
  carries its own revision, height maxima, dominant authored color and edit mask.
  Workers rebuild dirty tiles asynchronously from committed chunk events/snapshots.
- Bootstrap returns a manifest revision and immutable artifact URLs. The client installs
  the snapshot, then applies reliable WebSocket tile deltas newer than that revision;
  stale snapshots may never overwrite newer deltas.
- The browser may retain validated artifacts in its normal HTTP cache or IndexedDB,
  keyed by content hash. A cache miss falls back to deterministic base generation while
  current authored far-field data loads at the lowest scheduler priority.
- Generator or LOD codec changes create a new immutable base key. World edits invalidate
  only affected tiles, never the whole torus.

For the 32-player release limit, this cache is primarily a join-latency and correctness
optimization, not a concurrency requirement. Do not implement a service that regenerates
the full torus per join. Ship the shared snapshot when authoritative world edits and the
WebSocket delta stream exist, or earlier only if measured p95 main-thread initialization
exceeds the client startup budget.

## 8. Commands, Consistency, and Conflicts

Every structural command carries:

- a random 128-bit `operation_id`;
- its `input_sequence` and `client_tick`;
- `expected_revision` for every target chunk;
- `expected_entity_revision` for mutable non-terrain state;
- explicit standard- and microblock operations with no implicit overwrite semantics.

Core entity/script operations are typed protobuf `oneof` actions, not numeric `kind` plus
opaque bytes: run enable/disable, mount/dismount, component script enable, and installing a
previously validated script bundle each have stable fields. Extending this set requires a
new tagged action and compatibility test; clients never guess a payload layout.

Server processing:

1. Normalize wrapped X/Z and validate Y plus micro offsets.
2. Check membership, distance, per-second edit budget, and command size.
3. Verify the command belongs to the current zone and the worker fence is valid.
4. Check all expected chunk/entity/profile revisions. Any mismatch returns `CONFLICT`
   with no partial success and the relevant current revision in `CommandResult`.
5. Validate standard/microblock exclusion and entity occupancy on an in-memory copy.
6. Apply atomically at a tick boundary, emit one ordered event, and increment revisions.
7. Broadcast and return `CommandResult` after the event is durable.

The unique `(world_id, actor_user_id, client_op_id)` index makes retries idempotent. A
duplicate returns the first execution's `event_id`.

Conflicts on one cell in one tick follow the zone actor's deterministic command order;
newer client wall-clock time never wins automatically.

## 9. Persistence Model

| Table | Hot path | Purpose |
|---|---:|---|
| `users` (existing) | Join/bootstrap | EntropyDrop identity and immutable Minecraft skin URL/model |
| `worlds` | No | Dimensions, int32 seed, protocol and generator versions |
| `world_members` | Join | Role, permission, and ban state |
| `world_session_slots` | Join/heartbeat | Fixed 32-slot admission leases and reconnect grace |
| `world_join_queue` | Queue heartbeat | FIFO waiting leases; no player/world hot state |
| `zone_leases` | Every second | Single-writer lease and fencing |
| `world_event_streams` | Event transaction | Per-world commit-order guard for structural events |
| `chunk_snapshots` | Background | Compressed overlay and revision per chunk |
| `world_events` | Tick batch | Ordered durable structural events and idempotency |
| `world_event_chunks` | Tick batch | Event-to-chunk index for AOI catch-up |
| `world_event_entities` | Tick batch | Event-to-entity index for entity recovery |
| `script_bundles` | Script save | API V2 source bundle and content hash |
| `build_assets` | Entity placement/checkpoint | Deduplicated immutable definitions for entities already in the world |
| `entity_snapshots` | Sleep/checkpoint/unload | Definition, spatial manifest, run intent, health, and necessary recovery state |
| `entity_chunk_coverage` | Entity checkpoint | AOI lookup for sleeping entities, including multi-chunk bounds |
| `world_player_profiles` | First/ordinary join | Stable player id only; no position or birth point |
| `player_snapshots` | Immediate/periodic/offline | Latest runtime position and yaw; no backpack data |
| `world_checkpoints` | Background | Safe event-pruning watermark |

There are deliberately no `player_inventories` or `player_inventory_slots` tables.
`player_snapshots.state` also excludes backpack data. `build_assets` is world recovery
data, not a cloud copy of browser backpack entries.

### 9.1 Why Event IDs Do Not Update `worlds.revision`

Updating one world row for every write would serialize all players on a row lock. V2
allocates `event_id` from a cached global identity sequence. Gaps are allowed; monotonicity
is sufficient. Because sequence allocation itself does not guarantee commit order,
event transactions lock the dedicated `world_event_streams` row and reject a lower
late event for retry. This correctness lock is used only for non-empty structural
transactions, never for simulation ticks or snapshots. Per-chunk revisions localize
edit conflict detection; load tests must determine when the event stream needs a more
granular ordering domain.

For the V2 limit of 32 players, the single per-world event-stream lock is an intentional
correctness tradeoff: it cannot affect movement-only ticks and bounds simultaneous human
writers. Keep it while structural commit p99 is below 50 ms and stream-lock wait p99 below
10 ms. If either threshold fails under the 32-player build workload, migrate to per-zone
sequences plus an explicit cross-zone coordinator; hash partitioning by world alone does
not remove a hot world's stream-row contention.

### 9.2 Snapshot and Event Replay

- Load chunk: generate base terrain, load its latest snapshot, query events touching
  the chunk after `last_event_id`, and replay them.
- Load entity: read `entity_snapshots`, then replay later entity-indexed events.
- A background checkpoint records both a safe `event_id` and a manifest. Advance
  `worlds.latest_checkpoint_event_id` only after every manifest entry is durable.
- Archive an event only after every online recovery window and checkpoint watermark has
  passed it. Never delete solely by age.

### 9.3 Database Write Pattern

- A zone commits at most one event transaction per tick and writes nothing when there
  are no events.
- Use prepared statements or binary COPY for `world_events` and `world_event_chunks`.
- Snapshot upserts compare revision so an old background task cannot replace new data.
- High-rate queries index `(world_id, zone/chunk, event_id)`; `updated_at` is not a
  synchronization cursor.
- Hash-partition `world_events` into 32 partitions by `world_id` from the first release.
  Add time subpartitions only after a partition reaches measured capacity.

### 9.4 Server World Data, the Local Backpack, and the Resource Market

The browser owns the backpack. The current client already persists its three groups of
nine entries under `localStorage['space.backpack.v2']`; JSON export/import is the manual
backup and transfer mechanism. The server does not know which slot is selected, which
name a player gave an unpublished item, or whether two unpublished local entries are
identical. Publishing is an explicit copy operation; it never synchronizes or mutates a
player's local slots.

This does not make placement client-authoritative. There are two different lifetimes:

```text
browser backpack entry --untrusted placement command--> authoritative world mutation
                       `--explicit publish-----------> immutable market resource
                                                    `--> durable world entity definition
```

- `ENTITY`, `BLOCK_SET`, and `COLOR_SET` content stays local until the player explicitly
  publishes it. Entity/block-set placement remains independent from publication.
- Copying a world selection to the backpack downloads a validated structural snapshot;
  it does not create a server inventory record.
- Clearing browser site data loses local slots. A player can download a published resource
  again, but the market is not cross-device synchronization, backup, or slot recovery.
- Market rows retain publisher attribution, aggregate download/like counts, the immutable
  object key, and one like per authenticated user. Canonical payloads are public S3 objects
  served through the configured CDN; the download API records the download and returns the
  CDN URL instead of proxying the payload through the application server.
- Administrators retain a soft-deleted database tombstone for attribution, quota, and digest
  deduplication, but deletion removes the S3 object and requests a CloudFront invalidation.
  When a CDN domain is configured, `AWS_CLOUDFRONT_DISTRIBUTION_ID` is required so an
  immutable cached copy cannot remain downloadable after a successful admin response. The
  backend AWS identity therefore needs `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`,
  and `cloudfront:CreateInvalidation` permissions for this flow.

#### 9.4.1 Canonical publish contract

All published resources use schema version 2 and a closed field set (`extra=forbid`):

- `space-blockset`: a non-empty name and bounded voxel array;
- `space-entity`: the same voxel representation plus one explicit `root`, a tree of at
  most 63 child components, scripts, enabled flags, constraints, collision flags,
  physics mode/material, bearing/piston parameters, cockpit, gravity and vehicle state;
- `space-colorset`: exactly nine normalized six-digit `#rrggbb` values.

Every voxel has the sole portable block id `1`. Voxel base coordinates (`dx/dy/dz`) are safe integers. A micro voxel provides all three
integer offsets (`mx/my/mz`) in `0..4`; omission of all three means a standard voxel. The
API rejects duplicate occupancy, standard/micro overlap in one cell, bounds above 64 cells on an axis, unknown component
references, hierarchy cycles, duplicate ids, non-finite physics values, oversized scripts,
and resources above the block/component/constraint/byte budgets.

Before object storage, the API recomputes derived counts, normalizes colors and numbers, and sorts
order-insensitive arrays. SHA-256 is computed over this canonical content without the
display name or derived counts, so renaming or reordering cannot evade the global duplicate
constraint. Deleted rows continue to reserve their digest. Every publication has the fixed
SPDX license `AGPL-3.0-only`; each user may make at most ten successful publications per
UTC day, including resources later deleted by an administrator.

New publications upload canonical JSON to
`space-market/resources/{resource_id}/{digest}.json` before the database row is committed.
If the database write fails, the API removes the unreferenced object. Rows created before
CDN storage retain their legacy JSON only until the first download, which uploads the
canonical object and clears the database copy.

### 9.5 `build_assets`: Durable World Entity Definitions

`build_assets` is not cloud-backpack storage. It contains only canonical definitions
needed to recover entity instances that exist in the shared world (`kind=1`). Multiple
identical placed entities may share one immutable definition while retaining separate
runtime snapshots.

The ENTITY payload contains the component tree, blocks/microblocks, pivots, initial local
transforms, script references/enabled flags, body types/materials, constraints, cockpit
configuration, and validated counts/bounds. It excludes entity id, world transform,
velocity, mounted player, current tick, and running `self.state`.

The server parses and canonicalizes content, recomputes counts and SHA-256, validates all
script references and decompression limits, and never trusts the hash supplied in
`LocalBuildPlacement`. Equal canonical definitions reuse an asset. Small definitions may
use PostgreSQL `BYTEA`; a measured object-storage threshold may be introduced for large
world entities without adding inventory sync.

### 9.6 World Entity Instances and Spatial Manifest

Recover an entity from `entity_snapshots`, `entity_chunk_coverage`, its definition asset,
and later indexed events. The snapshot includes:

```text
identity: entity_id, owner_user_id, definition_asset_id
authority: home_zone, home_chunk, ownership_epoch
lifecycle: desired_run_state, runtime_health, lifecycle_epoch, last_sleep_reason
ordering: revision, last_event_id, definition_version, runtime_version
necessary runtime_state: transform/velocities, component transforms, self.state,
                         body flags, dynamic bodies, constraints and mounted player
```

`entity_chunk_coverage` lists every chunk intersecting validated entity bounds, including
wrapped chunks. It is updated atomically with a structural checkpoint and lets AOI wake
sleeping entities without scanning all snapshots in a 32x32 zone. The lifecycle state
itself is worker-local; only durable intent, health, generation, and the last complete
checkpoint belong in PostgreSQL.

### 9.7 Local Placement Trust Boundary

`LocalBuildPlacement` carries `operation_id`, kind, codec/compression metadata, declared
uncompressed size, hash, payload, and target transform—not a server slot id. The gateway
enforces a small compressed-message limit; the worker or background validator:

1. bounds compressed and uncompressed sizes before allocation and rejects hash mismatch;
2. decodes with a CPU/decompression-ratio budget and canonicalizes every coordinate/color;
3. revalidates permissions, reach, wrapped target zone, block count, script permissions,
   collision and expected chunk revisions;
4. for an entity, creates/reuses a durable definition then spawns a fresh entity id;
5. for a block set, expands to explicit standard/micro mutations and stores only the
   resulting world event/chunk snapshot, not the local item;
6. applies atomically at a tick boundary and acknowledges only after durable commit.

Large local imports use the existing bounded offline-job upload path. That upload is a
temporary command input, not a cloud backpack: staging expires whether the job succeeds
or fails, and only accepted world mutations remain.

### 9.8 Copy from the World

At a tick boundary the worker freezes only the selected structural view. A background
task canonicalizes it and returns a bounded download to the requesting browser; physics
continues. Runtime transform, velocity, mounted player, `self.state`, credentials, and
server-only script metadata are stripped. The browser validates again and writes the
entry to local storage. No database inventory revision or slot transaction exists.

### 9.9 Cache, References, and Garbage Collection

- Zone workers use a byte-bounded decompressed-definition LRU keyed by
  `(asset_id, content_hash)`. AOI wake may prefetch it asynchronously.
- `build_assets` has no synchronously maintained refcount.
- Background GC traces only authoritative references from `entity_snapshots`, unpruned
  events, and checkpoint manifests. An asset must be unreachable for two consecutive
  GC epochs and older than retention before deletion.
- A database reference must never point to an already-deleted object-store blob.
- Deduplication and hashes must not reveal another user's private definition.
- Appearance GC follows the same two-epoch retention rule and deletes only assets not
  referenced by any `world_player_profiles` row; the FK uses `RESTRICT` as the final guard.

## 10. Entities, Physics, and Scripts

### 10.1 Entity Authority

An entity definition contains its component tree, blocks, microblocks, pivot, script
references, and driving config. Runtime state contains position, quaternion, linear and
angular velocity, component-local transforms, and `self.state`.

- Active state exists only on the home-zone worker.
- Network updates contain quantized changed fields, never full serialized entities.
- When a pivot crosses a border, handoff proceeds: target preload, one-tick source freeze,
  state and pending-input transfer, ownership-epoch increment, target takeover, then ghost
  deletion after acknowledgment.
- Clients submit inputs or authorized edit commands, never arbitrary transforms or velocity.

### 10.2 Durable Intent and Wake References

`desired_run_state` is durable player/admin intent (`DISABLED` or `ENABLED`); it is not
the runtime lifecycle. `runtime_health` is `HEALTHY`, `RETRYABLE_FAULT`,
`SCRIPTS_DISABLED`, or `QUARANTINED`. A disabled or quarantined entity remains visible
as validated static geometry but never creates dynamic bodies or a script VM.

The worker maintains a deduplicated wake-reference set per entity. References come from:

- at least one admitted player's AOI intersecting `entity_chunk_coverage`;
- an admitted player mounted in or actively controlling the entity;
- an authorized pending interaction/edit that has already passed distance checks;
- an active constraint/dependency or required cross-zone handoff.

Chunk overlap from one or many players creates references, not separate runtimes. Queue
connections create none. A normal AOI reference may disappear when the last visible chunk
leaves; control, constraint, and handoff references must be explicitly released. Reference
updates are messages applied in deterministic order at a tick boundary.

### 10.3 Runtime Wake/Sleep State Machine

The initial cooldown is five seconds with a one-second minimum active residence. Both use
the worker's monotonic clock. Configuration is server-owned and bounded; clients cannot
request a longer lifetime.

| State | Hot resources | Meaning |
|---|---|---|
| `SLEEPING` | Spatial manifest + lightweight static proxy | Latest snapshot is durable; no VM or dynamic body |
| `LOADING` | Async load job | Read definition/snapshot/events outside the tick |
| `WAKING` | Decoded candidate state | Waiting for atomic tick-boundary installation |
| `ACTIVE` | Physics + eligible VM | Has wake references and runs normally |
| `COOLING` | Same as active | Last ordinary reference disappeared; hysteresis still runs it |
| `QUIESCING` | Hot but command-closed | Finish current tick, reject new control, drain accepted buffers |
| `CHECKPOINTING` | Frozen immutable state | Async durable snapshot before hot resources are released |
| `RETRY_BACKOFF` | Manifest plus bounded retained/frozen state | Recoverable load or checkpoint failure; timed retry |
| `QUARANTINED` | Static proxy only | Integrity/version failure; explicit repair/admin action required |

Allowed transitions are exhaustive:

| From | Condition | To / action |
|---|---|---|
| `SLEEPING` | enabled, not quarantined, wake-reference count becomes positive | `LOADING`; capture `(ownership_epoch,lifecycle_epoch)` |
| `LOADING` | definition, checkpoint, and event replay succeed and epochs still match | `WAKING` |
| `LOADING` | references fall to zero or intent becomes disabled | cancel result, `SLEEPING` |
| `LOADING` | recoverable I/O failure | `RETRY_BACKOFF`; exponential retry with jitter and a hard cap |
| `LOADING` | hash, bounds, codec, or version integrity failure | persist health, `QUARANTINED` |
| `WAKING` | next tick boundary, reference still present and enabled | instantiate once, increment lifecycle epoch, `ACTIVE` |
| `WAKING` | demand/intent vanished | discard candidate, `SLEEPING` |
| `ACTIVE` | ordinary wake-reference count reaches zero | set deadline, `COOLING` |
| `COOLING` | any wake reference returns before deadline | clear deadline, `ACTIVE` |
| `COOLING` | deadline expires and no mandatory reference remains | `QUIESCING` |
| `ACTIVE` or `COOLING` | disable, shutdown, migration, or unrecoverable runtime fault | bypass cooldown, `QUIESCING` |
| `QUIESCING` | current tick and accepted command buffers finish | freeze canonical runtime state, `CHECKPOINTING` |
| `CHECKPOINTING` | conditional snapshot/event commit succeeds, no new demand | destroy VM/bodies, retain static proxy, `SLEEPING` |
| `CHECKPOINTING` | commit succeeds and demand returned | install the still-valid frozen state, `WAKING` without rereading DB |
| `CHECKPOINTING` | recoverable persistence failure | retain bounded frozen state, `RETRY_BACKOFF`; never claim sleep |
| `RETRY_BACKOFF` | retry timer fires with matching epochs | resume failed load/checkpoint stage |
| `RETRY_BACKOFF` | retry/retained-memory limit exceeded or integrity failure | persist health, `QUARANTINED` |
| `QUARANTINED` | validated repair or compatible asset replacement | `SLEEPING`; waking still requires enabled intent plus a reference |

Every async completion carries world, entity, ownership epoch, lifecycle epoch, and job id;
the tick discards stale completions. A new reference during `CHECKPOINTING` sets
`wake_after_checkpoint` rather than canceling a write already in flight. No transition
creates two VMs, acknowledges an uncommitted sleep, or applies callbacks directly from an
I/O thread.

### 10.4 Checkpoint, Disable, Crash, and Handoff Semantics

- A sleep checkpoint stores only necessary recovery data: transform and velocities,
  component-local transforms, dynamic body flags/state, constraints, bounded `self.state`,
  mounted player, desired intent, health, revisions, and the last applied event id.
- The conditional write requires the captured ownership/lifecycle epochs and cannot
  overwrite a newer active or migrated entity. Coverage and definition changes commit in
  the same transaction or behind the same event watermark.
- Disabling is a durable ordered event. It stops new script/control commands immediately
  at the boundary, then follows `QUIESCING -> CHECKPOINTING`; disabling does not delete
  the entity or its static collision/visual proxy.
- Worker crash recovery starts durable enabled entities in `SLEEPING`. Only reconstructed
  wake references load them. A player reconnect therefore cannot duplicate a runtime.
- Handoff holds a mandatory wake reference. The source quiesces for one tick, transfers
  frozen state and pending ordered input under a new ownership epoch, and removes its hot
  state only after target acknowledgment. It does not create an intermediate DB sleep.
- A script budget failure normally sets `SCRIPTS_DISABLED` while physics remains active;
  corrupt structural/runtime data sets `QUARANTINED` and cannot auto-retry forever.

### 10.5 Script Sandbox

The server runs API V2 scripts under these requirements:

- One isolated VM or secure WASM runtime per entity, with no network, filesystem, DOM,
  system time, or dynamic module loading.
- `ctx` is a tick snapshot. Script methods append to a command buffer validated and applied
  at the tick boundary.
- Provide deterministic seeded RNG and no native `Math.random()`.
- Bound CPU, memory, logs, block edits, and tree traversal. Repeated violations disable scripts.
- Compile in background threads and switch atomically at a tick boundary only after success.
- Store source hash and API version on the server. A client copy is not proof of execution.

Initial budget guidance is 0.2 ms average and 1 ms hard maximum per invocation, 1 MiB
state, and 64 structural edits per tick. A zone also has a 4 ms soft / 6 ms hard aggregate
script budget and at most 32 invocations in one 60 Hz tick. Ordinary awake scripts may be
time-sliced at 20 Hz; explicitly budgeted vehicle-control scripts run at 60 Hz. Repeated
overruns set `SCRIPTS_DISABLED` without disabling entity physics. Load tests tune these
numbers, but no entity count may silently erase the aggregate cap.

## 11. API Boundaries

### 11.1 REST Control Plane

The bootstrap and terrain-overlay endpoints are implemented in the current FastAPI
service. The remaining endpoints belong to the gateway/worker delivery phases.

```text
POST   /space/api/v2/bootstrap                  Bearer/skin gate + latest state or ephemeral random start
PUT    /space/api/v2/worlds/{id}/players/me/position  Save latest per-user reconnect position
GET    /space/api/v2/worlds/{id}/terrain-edits  Paginated durable authored chunk overlays
POST   /space/api/v2/worlds/{id}/terrain-edits/batches  Idempotent batch of 1-256 mutations
GET    /space/api/v2/market/resources           List by kind; rank by downloads, likes, or latest
POST   /space/api/v2/market/resources           Validate and publish a canonical AGPL-3.0-only resource
GET    /space/api/v2/market/resources/{id}/download  Download canonical content and increment count
POST   /space/api/v2/market/resources/{id}/like Toggle the authenticated user's like
DELETE /space/api/v2/market/resources/{id}      Administrator-only soft delete
POST   /space/api/v2/worlds/{id}/join-ticket    Issue a short-lived real-time ticket
GET    /space/api/v2/worlds/{id}                Read metadata and membership permissions
GET    /space/api/v2/worlds/{id}/members        List members with permission
PATCH  /space/api/v2/worlds/{id}/members/{user} Change role or permission
GET    /space/api/v2/worlds/{id}/entity-definitions/{asset}  Fetch a definition by hash/ETag
POST   /space/api/v2/script-bundles             Save and compile a script bundle
POST   /space/api/v2/worlds/{id}/jobs/import    Queue large STL or batch-terrain work
GET    /space/api/v2/jobs/{id}                  Read job status
```

Authentication remains on the existing EntropyDrop `/skin/api/auth/*` routes;
Space must not add a login/session API. Until the authoritative WebSocket worker ships,
`/space/ws/v2` uses the `space-relay-v1` MessagePack subprotocol to relay validated player
poses from in-memory state; this is explicitly not the authoritative protobuf simulation
protocol. Terrain snapshots, bounded mutation batches, and the explicit resource market use
the authenticated REST bridge. There is deliberately no inventory-slot/backpack-sync
endpoint. Asset responses require current world access, use immutable cache headers plus
ETag, and never expose raw object-storage keys.

The transitional relay exposes `POST` and `DELETE`
`/space/api/v2/worlds/{world_id}/admission`. Redis sorted sets atomically expire leases,
reserve up to the world's configured capacity, and promote FIFO waiters. Queue polling renews
the 30-second waiting lease. Once promoted, the offline choice prompt renews its reservation
until the player enters online Space or explicitly stays offline; closing the page releases it.
Live relay connections renew their own reservations and release them on disconnect. Development
may use the process-local fallback when Redis is absent, while production fails closed instead
of oversubscribing a world.

### 11.2 Real-Time Channel

The running transitional channel is:

```text
GET /space/ws/v2   Upgrade: websocket; subprotocol: space-relay-v1
```

It accepts changed poses at up to 20 Hz, emits nearby-player snapshots at 10 Hz, fans
state between API replicas through Redis Pub/Sub, and checkpoints dirty reconnect state
every five seconds plus disconnect. A lightweight terrain revision message wakes clients;
chunk payloads still come from the durable REST cursor.

The target authoritative channel remains:

```text
GET /v2/realtime   Upgrade: websocket
```

`protocol.proto` defines pre-session queue status, handshake, input, AOI, commands,
remote-player/entity deltas, snapshots, and disconnect reasons. One connection upgrades
once and remains on the same WebSocket while moving from queue to admitted session.
Reject incompatible protocol versions immediately; never guess old field semantics.

## 12. Security and Abuse Controls

- Gateway and worker both validate permission; never trust client-visible ticket fields alone.
- Require TLS, an exact WebSocket `Origin` allowlist, short-lived one-use join-ticket nonce,
  and constant-time resume-token hash comparison. Reject missing/extra UUID byte lengths,
  unknown enum values where unsafe, non-monotonic sequences, and text frames before routing.
- Apply per-user, per-world, and per-zone token buckets independently for input packets,
  structural commands, edited cells, script logs, and chat.
- Rate-limit join-ticket creation, queued sockets, queue heartbeats, and reconnect attempts;
  one user may own only one queue row or occupied world slot.
- Validate command size, coordinates, colors, micro offsets, and entity ownership after decode.
- Treat browser-local backpack payloads as hostile uploads: bound compressed and expanded
  bytes, parse depth, nodes/blocks, coordinates, script capabilities, and validation CPU.
- Never accept client `event_id`, `server_tick`, or `fence_token` as authoritative.
- Bound decompressed snapshot size and compression ratio to prevent compression bombs.
- Read skin URLs only from the authenticated `users` row; never accept a URL from
  another player's packet. Authorize every entity-definition fetch against current
  world membership and AOI or ownership; opaque ids and hashes are not bearer capabilities.
- Audit every administrative action; game events retain actor and operation id.
- Script source is hostile input and never executes in API or gateway processes.

## 13. Failure and Recovery

### Worker Crash

1. The lease expires and the scheduler assigns the zone with a larger fencing token.
2. The new worker loads chunk, entity, and player snapshots.
3. It replays events after each snapshot's `last_event_id`.
4. It rebuilds script VMs and ghosts, starts ticking, and tells the gateway to reroute.
5. Clients use resume tokens to resend unacknowledged inputs and idempotent commands.

### Temporary PostgreSQL Failure

- Movement and observation may continue briefly, but new durable structural commands
  receive backpressure and are never falsely acknowledged.
- At the hard queue limit, the zone enters read-only build mode.
- On recovery, retry batches by operation id. Never cache without a bound and risk OOM.

### Gateway Crash

- Authoritative workers continue unaffected.
- Session slots and queue ordering survive in PostgreSQL leases. Clients reconnect through
  another gateway and resume AOI/input acknowledgment; stale gateway renew/free operations
  fail their `(session_id, connection_id, lease_epoch)` compare.

## 14. Performance Implementation Details

- Pin zone actors to CPU cores. Actors exchange immutable messages through bounded lock-free queues.
- Physics, entities, and chunks use SoA and object pools; avoid tick-time temporary objects and JSON.
- Background threads handle compression, hashing, script compilation, and database batch encoding.
- Entity definition/snapshot loads, wake decoding, and sleep checkpoint writes run in bounded
  background pools; lifecycle completion is applied only at tick boundaries.
- Entity state uses changed masks. Quantize position to centimeters from the zone origin and
  pack rotation with a smallest-three quaternion codec.
- Schedule each connection by a per-tick byte budget: local correction, nearby entities,
  terrain events, new chunk snapshots, then distant decoration.
- Slow clients cannot stall a zone. Drop superseded state frames while preserving the newest
  frame and reliable events; disconnect clients whose queue continues to grow.
- Gateways keep only live socket buffers. Durable admission slots/queue, scheduling, and
  zone leases are low-frequency control-plane work.

### 14.1 Release Capacity Envelope

The contract is **32 occupied slots per world**, including reserved and reconnect-grace
slots—not 32 per zone. The 33rd user queues even when players are spatially separated.
One deployment may host many worlds by adding gateways/workers, but no sharding path may
silently raise a single world's configured V2 cap above 32.

The worst visibility hotspot has 32 players and 992 directed remote-player relationships.
At 20 Hz and an illustrative 64-byte changed delta, player motion alone is about 1.27 MB/s
aggregate and 40 KB/s per client before entities, chunks, WebSocket/TLS and retransmission.
An eight-chunk square AOI has at most 289 observer-chunk memberships per player; workers
deduplicate the underlying loaded chunks and entity wake references.

Initial queue protection is 256 waiting sockets per world and 1,024 per gateway; beyond
that, return `DISCONNECT_SERVER_OVERLOADED` with retry guidance rather than allocating
unbounded memory. These are protective queue limits, not extra online players.

This repository still contains only target DDL/protocol/design, not a running backend, so
measured supported concurrency is currently zero. “Supports 32” becomes true only after the
release acceptance load/fault tests pass on declared hardware and network conditions.

## 15. Observability

Record and alert on:

- tick p50/p95/p99, count above 16.67 ms, and per-stage timing;
- players, entities, active chunks, ghosts, and scripts per zone;
- world admission slots by state, queue length/age, promotion latency, expired tickets,
  reconnect-grace recovery, and duplicate-session rejection;
- entity lifecycle counts and transition latency, wake references, load/checkpoint failures,
  stale async completions, retained retry bytes, and quarantined entities;
- per-connection input latency, retransmission/duplicates, correction distance, and send queue;
- AOI chunk entry/exit rate, snapshot hit rate, compression ratio, and encode time;
- persistence queue depth, event commit latency, and snapshot event lag;
- lease-renewal failures, fence rejection, and worker takeover time;
- script CPU/memory violations and automatic disable count.

Performance regression tests use a fixed map, entity population, and network model, with
results stored by commit. Average FPS alone is insufficient.

## 16. Implementation Order

### Phase 0: Freeze Contracts

1. Create golden fixtures for the chunk codec, protocol messages, wrapped coordinates,
   and int32 seeds.
2. Require TypeScript and server implementations to hash each fixture identically.
3. Add protobuf compatibility checks and schema linting.
4. Verify fixed admission slots/queue, player profiles, entity lifecycle fields/coverage,
   and the deliberate absence of cloud-inventory tables against golden DDL fixtures.

### Phase 1: Authoritative Single-Zone Server

1. Implement gateway, fixed 32-slot admission, FIFO queue, one worker, and binary
   WebSocket handshake.
2. Add reliable player/entity presence, player prediction/rollback, and epoch-gated
   20 Hz state frames.
3. Add sequenced AOI, bounded chunk snapshots, unified typed commands, operation ids,
   immutable skin/definition asset fetch, and slow-client scheduling.
4. Test two clients editing the same cell concurrently.

### Phase 2: Durability and Recovery

1. Apply `schema.sql` and implement zone lease/fencing.
2. Add tick group commit and background chunk/entity snapshots.
3. Add the versioned distant-LOD manifest, object-storage snapshot and revision-ordered
   dirty-tile deltas; never rebuild or query the full torus for each join.
4. Implement canonical world-entity assets, validated local placement, lifecycle
   checkpoints, content deduplication, and reachability GC.
5. Test worker/gateway crashes, database backpressure, and reconnection.

### Phase 3: Entities and Scripts

1. Add server physics, component trees, and driving input.
2. Add the complete wake/sleep state machine, sandboxed API V2, aggregate budgets,
   compile switching, and state recovery.
3. Add cross-zone entity handoff and ghost collision.

### Phase 4: Horizontal Scale

1. Add multiple gateways and workers, neighboring-zone colocation, and live migration.
2. Add event partitioning, checkpoint archival, and object storage.
3. Run load, fault-injection, long soak, and capacity-model tests.

## 17. Release Acceptance

The system is not real-time multiplayer until it passes at least these scenarios:

- 32 clients move, drive, and observe entities in one zone with tick p99 inside budget.
- A 33rd user receives FIFO `QueueStatus`, creates no player/AOI state, and is promoted
  exactly once after a slot is freed; simultaneous joins never exceed 32 occupied slots.
- A dropped active connection retains its slot for 20 seconds and resumes it with the
  matching token; a second connection cannot control the same user.
- Multiple clients edit the same standard or microcell in one tick; all converge with no
  invalid coexistence.
- A client that drops arbitrary coalescible state frames still installs every player/entity
  exactly once from reliable presence and converges after a full-reset epoch.
- Rapid AOI changes cannot install a chunk/entity result from an older `interest_sequence`;
  radius above eight is rejected and creates no entity wake reference.
- Position, body/look orientation, pose, animation and immutable skin URL/model converge
  for all visible players; the PNG is downloaded once on entry/AOI cache miss and is
  never carried in 20 Hz frames.
- Ten thousand retries of one `operation_id` create exactly one event.
- One thousand identical world entity instances reuse one structural asset; editing one
  instance does not alter the others.
- Backpack edits survive a same-browser reload through `space.backpack.v2`, create no
  server inventory rows/messages, and are lost when that browser storage is cleared.
- Market publication accepts only canonical v2 resources, rejects renamed/reordered
  duplicates, enforces ten successful publications per UTC day, and fixes the license to
  `AGPL-3.0-only`; download/like rankings and administrator soft-delete converge.
- Entity placement creates a new `entity_id` without copying velocity or `self.state`;
  block-set placement edits terrain only.
- An enabled sleeping entity wakes exactly once when overlapping AOIs arrive concurrently,
  remains active through cooldown, and sleeps only after a durable conditional checkpoint.
- A wake during checkpoint resumes from the frozen state; stale async jobs, checkpoint
  failure, disable, quarantine, crash recovery, and zone handoff follow the transition table.
- AOI, collision, and interpolation remain continuous across wrapped X and Z seams.
- Worker crashes before commit, after commit, and during a snapshot all recover identically.
- A stale worker returns and its write is rejected by the fencing trigger.
- A client resumes incrementally after 30 seconds, or correctly falls back to a full
  snapshot after archival.
- Infinite loops, memory growth, log spam, and per-frame block edits trip script budgets.
- A slow client crosses the soft threshold, loses only superseded/decorative work, preserves
  reliable ordering, and is disconnected at the hard threshold without affecting the tick.
- Invalid origins/tickets, UUID lengths, enum values, replayed operations, oversized local
  builds, compression bombs, forged skin URLs and unauthorized definition fetches fail closed.

## 18. Explicitly Rejected Designs

- Clients do not PUT final world state.
- `updated_at` is not a synchronization cursor.
- PostgreSQL does not store per-frame player or entity transforms.
- Backpack/inventory data is not stored in PostgreSQL or synchronized over WebSocket.
- Browser-local placement payloads are not trusted as authoritative world state.
- World entities do not embed duplicate full definition payloads.
- A reconnect grace or reserved handshake does not bypass the 32-slot admission cap.
- Microblocks do not become individual hot database rows.
- One global world revision does not lock every edit.
- REST polling does not implement real-time synchronization.
- Trusted service processes never execute user JavaScript directly.

## 19. Requirement Coverage Matrix

“Covered” below means schema, wire contract, consistency rule, failure behavior, and an
acceptance test are specified. It does not claim that the absent backend service is already
implemented.

| Requirement | Durable/source-of-truth coverage | Runtime/network coverage | Release proof |
|---|---|---|---|
| Random start without a saved state | No birth coordinates are stored; absence of `player_snapshots` triggers a safe random candidate | First client immediately checkpoints the ephemeral start; later joins use latest state | Empty/corrupt snapshot falls back safely; profile schema contains no spawn columns |
| All standard and microblocks | Seed/generator plus packed chunk overlays/events reconstruct every cell | AOI snapshot then ordered deltas; standard/micro exclusion validated atomically | Conflicting same-tick edits converge; codec golden fixtures round-trip |
| Distant torus for new entrants | Versioned immutable base artifact plus revisioned authored LOD tiles; no per-join PostgreSQL scan | HTTPS manifest/snapshot followed by reliable newer WebSocket tile deltas; deterministic base is the cache-miss fallback | Cold/warm join budgets pass; stale snapshot cannot replace newer edits; one dirty zone rebuilds only its tiles |
| All world entity information | Immutable `build_assets` + `entity_snapshots` + indexed events + coverage manifest | Reliable entity presence; immutable HTTPS definition; 20 Hz runtime deltas | Checkpoint/replay and 1,000 shared definitions recover identically |
| Enabled entity auto-run in loaded chunks | Durable desired run state, health, lifecycle/ownership epochs | AOI wake references and exhaustive wake/sleep state machine | Concurrent observers wake once; durable sleep, retry, quarantine and handoff tested |
| Visible player state/orientation/skin/pose | Latest `player_snapshots`, stable player id, and existing `users.skin_url`/`skin_type` | Reliable presence carries URL/model; epoch-gated 20 Hz motion deltas never carry PNG bytes | Missing skin blocks entry; latest checkpoint restores, AOI enter/leave and reconnect reset converge |
| Browser-only backpack | No inventory tables; existing `space.backpack.v2` plus JSON import/export | Untrusted local placement is revalidated; only accepted world result persists | Reload stays local, clearing storage loses it, server has no backpack endpoint/message |
| Explicit resource market | Immutable canonical v2 content, global SHA-256 uniqueness, publisher/quota metadata, likes and soft deletion | Authenticated REST publish/list/download/like/admin-delete; no slot synchronization | Strict-schema fixtures, duplicate/order/name equivalence, 10/day, counters/rankings and authorization tests |
| Maximum 32 online with queue | Fixed session slots, FIFO queue leases and user advisory lock | Queue status/heartbeat, reservation, active and reconnect-grace states | 32 simultaneous admits, 33rd queues, promotion/reconnect never oversubscribes |
| Real-time WebSocket behavior | No frame history in PostgreSQL | WSS binary protobuf, reliable presence/events, coalesced state, bounded fragmentation/backpressure | Slow client and stale-interest tests cannot delay tick or install obsolete data |
| Reconnect/idempotency/conflicts | Resume hash, operation id unique index, revisions, event/checkpoint watermarks | Input replay, presence reset, `InterestReset`, compare-and-swap commands | Crash points and 10,000 retries produce one durable result |
| Security and abuse resistance | Membership/ownership metadata and audit actor ids | Origin/ticket validation, dual permission checks, size/CPU/rate/script budgets | Fuzz, compression bomb, unauthorized asset, replay and sandbox tests fail closed |
