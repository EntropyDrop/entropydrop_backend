-- =============================================================================
-- Space Multiplayer V2 · durable control plane, snapshots and ordered events
-- PostgreSQL 15+
--
-- Hot state (players, physics, active chunks and script VMs) lives in an
-- authoritative zone worker. PostgreSQL is never queried from the 60 Hz tick.
-- It stores only control-plane data, compressed checkpoints and durable world
-- mutations. This schema extends the existing EntropyDrop `users` table; it
-- never creates or mirrors user identities. See docs/space-backend.md and
-- space/contracts/protocol.proto.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- 1. Existing EntropyDrop users, worlds and access control
-- -----------------------------------------------------------------------------

-- `users(id VARCHAR(16))`, `users.skin_url` and `users.skin_type` are owned by
-- the main EntropyDrop application.
-- They MUST exist before this schema is applied.

CREATE TABLE worlds (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id               VARCHAR(16) REFERENCES users(id) ON DELETE RESTRICT,
    name                        TEXT        NOT NULL DEFAULT 'New World'
                                             CHECK (char_length(name) BETWEEN 1 AND 128),
    seed                        INTEGER     NOT NULL,
    terrain_generator_version  SMALLINT    NOT NULL DEFAULT 1
                                             CHECK (terrain_generator_version >= 1),
    protocol_version            SMALLINT    NOT NULL DEFAULT 2 CHECK (protocol_version >= 2),
    snapshot_codec_version      SMALLINT    NOT NULL DEFAULT 1
                                             CHECK (snapshot_codec_version >= 1),
    width_chunks                SMALLINT    NOT NULL DEFAULT 1024
                                             CHECK (width_chunks BETWEEN 8 AND 2047),
    length_chunks               SMALLINT    NOT NULL DEFAULT 128
                                             CHECK (length_chunks BETWEEN 8 AND 2047),
    zone_size_chunks            SMALLINT    NOT NULL DEFAULT 32
                                             CHECK (zone_size_chunks BETWEEN 8 AND 64),
    max_online_players          SMALLINT    NOT NULL DEFAULT 32
                                             CHECK (max_online_players BETWEEN 1 AND 32),
    status                      SMALLINT    NOT NULL DEFAULT 1 CHECK (status IN (0, 1, 2)),
    settings                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    latest_checkpoint_event_id  BIGINT      NOT NULL DEFAULT 0
                                             CHECK (latest_checkpoint_event_id >= 0),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (width_chunks % zone_size_chunks = 0),
    CHECK (length_chunks % zone_size_chunks = 0)
);

COMMENT ON COLUMN worlds.seed IS
  'Signed int32 by contract; safe in JavaScript Number and stable across runtimes';
COMMENT ON COLUMN worlds.status IS '0=offline, 1=active, 2=maintenance';
COMMENT ON COLUMN worlds.zone_size_chunks IS
  'Toroidal spatial authority cell; 32 means one zone is 512x512 standard voxels';
COMMENT ON COLUMN worlds.max_online_players IS
  'Admission cap for active plus reconnect-grace sessions; V2 never exceeds 32';

CREATE TABLE world_members (
    world_id        UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    user_id         VARCHAR(16) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            SMALLINT    NOT NULL DEFAULT 1 CHECK (role IN (0, 1, 2, 3)),
    permission_mask BIGINT      NOT NULL DEFAULT 1 CHECK (permission_mask >= 0),
    banned_until    TIMESTAMPTZ,
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, user_id)
);

COMMENT ON COLUMN world_members.role IS '0=viewer, 1=builder, 2=moderator, 3=owner';
COMMENT ON COLUMN world_members.permission_mask IS
  'Bit mask validated by the gateway and revalidated by the authoritative worker';

-- Each world owns exactly 32 low-frequency admission slots. A gateway reserves
-- one in a short transaction before creating any player/zone hot state. The
-- slot remains occupied during reconnect grace, so reconnect cannot jump the
-- queue and two connections cannot control the same user concurrently.
CREATE TABLE world_session_slots (
    world_id          UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    slot_number       SMALLINT    NOT NULL CHECK (slot_number BETWEEN 0 AND 31),
    state             SMALLINT    NOT NULL DEFAULT 0 CHECK (state IN (0, 1, 2, 3)),
    user_id           VARCHAR(16) REFERENCES users(id) ON DELETE RESTRICT,
    session_id        UUID,
    resume_token_hash BYTEA       CHECK (resume_token_hash IS NULL OR octet_length(resume_token_hash) = 32),
    gateway_instance  UUID,
    connection_id     UUID,
    lease_epoch       BIGINT      NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    lease_until       TIMESTAMPTZ,
    reserved_at       TIMESTAMPTZ,
    activated_at      TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, slot_number),
    CHECK (
      (state = 0
       AND user_id IS NULL AND session_id IS NULL AND resume_token_hash IS NULL
       AND gateway_instance IS NULL AND connection_id IS NULL AND lease_until IS NULL)
      OR
      (state IN (1, 2, 3)
       AND user_id IS NOT NULL AND session_id IS NOT NULL AND resume_token_hash IS NOT NULL
       AND gateway_instance IS NOT NULL AND connection_id IS NOT NULL AND lease_until IS NOT NULL)
    )
);

COMMENT ON COLUMN world_session_slots.state IS
  '0=free, 1=reserved handshake, 2=active, 3=reconnect grace; states 1..3 count toward the cap';
COMMENT ON COLUMN world_session_slots.lease_epoch IS
  'Incremented on every reservation/rebind; stale gateways cannot renew or free a reassigned slot';

CREATE UNIQUE INDEX world_session_slots_session_idx
    ON world_session_slots (session_id) WHERE session_id IS NOT NULL;
CREATE UNIQUE INDEX world_session_slots_user_idx
    ON world_session_slots (world_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX world_session_slots_expiry_idx
    ON world_session_slots (world_id, lease_until) WHERE state <> 0;

-- Queue rows are control-plane leases, not player sessions. They create no zone
-- actor, player entity, AOI, or snapshot traffic. FIFO order is queue_sequence;
-- expired/disconnected rows are deleted before the next promotion transaction.
CREATE TABLE world_join_queue (
    queue_ticket_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    world_id         UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    user_id          VARCHAR(16) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    queue_sequence   BIGINT      GENERATED ALWAYS AS IDENTITY,
    gateway_instance UUID        NOT NULL,
    connection_id    UUID        NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (world_id, user_id)
);

CREATE INDEX world_join_queue_fifo_idx
    ON world_join_queue (world_id, queue_sequence);
CREATE INDEX world_join_queue_expiry_idx ON world_join_queue (expires_at);

-- Every admission/queue transaction takes this user-scoped lock before it
-- checks either table. It closes the cross-table race where two gateways could
-- otherwise reserve a slot and enqueue the same user concurrently. Hash
-- collisions only serialize unrelated joins; they cannot violate correctness.
CREATE OR REPLACE FUNCTION lock_world_admission_user(
    target_world UUID,
    target_user VARCHAR(16)
)
RETURNS void LANGUAGE sql AS $$
  SELECT pg_advisory_xact_lock(
    hashtextextended(target_world::text || ':' || target_user::text, 0)
  );
$$;

CREATE OR REPLACE FUNCTION seed_world_session_slots()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO world_session_slots (world_id, slot_number)
  SELECT NEW.id, slot_number
  FROM generate_series(0, 31) AS generated(slot_number);
  RETURN NEW;
END;
$$;

CREATE TRIGGER worlds_seed_session_slots
AFTER INSERT ON worlds FOR EACH ROW EXECUTE FUNCTION seed_world_session_slots();

-- -----------------------------------------------------------------------------
-- 2. Zone ownership: one live writer per spatial zone
-- -----------------------------------------------------------------------------

CREATE TABLE zone_leases (
    world_id       UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    zone_x         SMALLINT    NOT NULL CHECK (zone_x >= 0),
    zone_z         SMALLINT    NOT NULL CHECK (zone_z >= 0),
    owner_instance UUID        NOT NULL,
    fence_token    BIGINT      NOT NULL CHECK (fence_token > 0),
    lease_until    TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, zone_x, zone_z)
);

CREATE INDEX zone_leases_expiry_idx ON zone_leases (lease_until);

COMMENT ON COLUMN zone_leases.fence_token IS
  'Monotonically increases on reassignment; stale workers cannot commit events';

-- Structural events for one world deliberately serialize on this small row.
-- The 60 Hz simulation never touches it; only non-empty durable event batches do.
CREATE TABLE world_event_streams (
    world_id      UUID        PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    last_event_id BIGINT      NOT NULL DEFAULT 0 CHECK (last_event_id >= 0),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 3. Packed chunk snapshots
--
-- A payload contains the complete player-authored overlay for one 16x128x16
-- chunk: tri-state standard voxels (inherit/air/solid), palette-compressed
-- colors and sparse 5x5x5 micro groups. It is encoded and compressed by the
-- application; no individual voxel rows exist in PostgreSQL.
-- -----------------------------------------------------------------------------

CREATE TABLE chunk_snapshots (
    world_id          UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    chunk_x           SMALLINT    NOT NULL CHECK (chunk_x >= 0),
    chunk_z           SMALLINT    NOT NULL CHECK (chunk_z >= 0),
    revision          BIGINT      NOT NULL CHECK (revision >= 0),
    last_event_id     BIGINT      NOT NULL DEFAULT 0 CHECK (last_event_id >= 0),
    codec             SMALLINT    NOT NULL DEFAULT 1 CHECK (codec IN (0, 1)),
    codec_version     SMALLINT    NOT NULL DEFAULT 1 CHECK (codec_version >= 1),
    uncompressed_size INTEGER     NOT NULL CHECK (uncompressed_size >= 0),
    content_hash      BYTEA       NOT NULL CHECK (octet_length(content_hash) = 32),
    payload           BYTEA       NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, chunk_x, chunk_z)
);

COMMENT ON COLUMN chunk_snapshots.codec IS '0=raw, 1=zstd';
COMMENT ON COLUMN chunk_snapshots.revision IS
  'Per-chunk logical revision used by AOI cache validation and optimistic edits';

CREATE INDEX chunk_snapshots_resume_idx
    ON chunk_snapshots (world_id, last_event_id);

CREATE INDEX ix_chunk_snapshots_world_revision
    ON chunk_snapshots (world_id, revision);

-- Transitional REST bridge idempotency receipts. The authoritative WebSocket
-- worker later deduplicates with world_events.client_op_id instead, but keeping
-- a stable batch UUID is required while clients upload edits over FastAPI.
CREATE TABLE space_terrain_mutation_batches (
    world_id     UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    batch_id     UUID        NOT NULL,
    actor_user_id VARCHAR(16) REFERENCES users(id) ON DELETE SET NULL,
    dedupe_epoch SMALLINT     NOT NULL DEFAULT 0 CHECK (dedupe_epoch IN (0, 1)),
    client_created_at TIMESTAMPTZ,
    result       JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, batch_id),
    CHECK ((dedupe_epoch = 0) OR (client_created_at IS NOT NULL))
);

CREATE INDEX ix_space_terrain_batches_retention
    ON space_terrain_mutation_batches (dedupe_epoch, client_created_at);

-- -----------------------------------------------------------------------------
-- 4. Ordered durable mutation log
--
-- event_id comes from a cached global PostgreSQL sequence. It is strictly
-- increasing but may have gaps. Ordering within a world/zone is event_id order.
-- Player movement and per-frame physics snapshots are intentionally excluded.
-- -----------------------------------------------------------------------------

CREATE TABLE world_events (
    event_id        BIGINT      GENERATED ALWAYS AS IDENTITY,
    world_id        UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    zone_x          SMALLINT    NOT NULL CHECK (zone_x >= 0),
    zone_z          SMALLINT    NOT NULL CHECK (zone_z >= 0),
    fence_token     BIGINT      NOT NULL CHECK (fence_token > 0),
    server_tick     BIGINT      NOT NULL CHECK (server_tick >= 0),
    actor_user_id   VARCHAR(16) REFERENCES users(id) ON DELETE SET NULL,
    client_op_id    UUID,
    event_kind      SMALLINT    NOT NULL CHECK (event_kind BETWEEN 1 AND 32767),
    payload_version SMALLINT    NOT NULL DEFAULT 1 CHECK (payload_version >= 1),
    payload         BYTEA       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, event_id)
) PARTITION BY HASH (world_id);

ALTER SEQUENCE world_events_event_id_seq CACHE 1024;

DO $partition_world_events$
BEGIN
  FOR partition_index IN 0..31 LOOP
    EXECUTE format(
      'CREATE TABLE world_events_p%s PARTITION OF world_events FOR VALUES WITH (MODULUS 32, REMAINDER %s)',
      partition_index,
      partition_index
    );
  END LOOP;
END;
$partition_world_events$;

CREATE OR REPLACE FUNCTION validate_and_serialize_world_event()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  previous_event_id BIGINT;
BEGIN
  -- Lock the matching lease until commit so reassignment cannot race a writer
  -- that passed a non-locking fence check.
  PERFORM 1
  FROM zone_leases lease
  WHERE lease.world_id = NEW.world_id
    AND lease.zone_x = NEW.zone_x
    AND lease.zone_z = NEW.zone_z
    AND lease.fence_token = NEW.fence_token
    AND lease.lease_until > clock_timestamp()
  FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'stale or expired zone writer for world %, zone (%, %)',
      NEW.world_id, NEW.zone_x, NEW.zone_z
      USING ERRCODE = '40001';
  END IF;

  -- Identity values can be allocated out of commit order. Holding one stream
  -- row lock makes committed event_id order monotonic per world. A transaction
  -- that lost the race aborts and retries with a fresh identity value.
  INSERT INTO world_event_streams (world_id)
  VALUES (NEW.world_id)
  ON CONFLICT (world_id) DO NOTHING;

  SELECT last_event_id INTO previous_event_id
  FROM world_event_streams
  WHERE world_id = NEW.world_id
  FOR UPDATE;

  IF previous_event_id >= NEW.event_id THEN
    RAISE EXCEPTION 'event order conflict for world %: previous %, attempted %',
      NEW.world_id, previous_event_id, NEW.event_id
      USING ERRCODE = '40001';
  END IF;

  UPDATE world_event_streams
  SET last_event_id = NEW.event_id
  WHERE world_id = NEW.world_id;
  RETURN NEW;
END;
$$;

CREATE TRIGGER world_events_validate_fence
BEFORE INSERT ON world_events
FOR EACH ROW EXECUTE FUNCTION validate_and_serialize_world_event();

CREATE UNIQUE INDEX world_events_idempotency_idx
    ON world_events (world_id, actor_user_id, client_op_id)
    WHERE client_op_id IS NOT NULL;

CREATE INDEX world_events_zone_resume_idx
    ON world_events (world_id, zone_x, zone_z, event_id);

CREATE INDEX world_events_created_brin
    ON world_events USING brin (created_at) WITH (pages_per_range = 64);

-- Maps an event to every touched chunk. Most terrain events touch one chunk;
-- entity assembly may touch several. This keeps AOI catch-up index-only.
CREATE TABLE world_event_chunks (
    world_id UUID     NOT NULL,
    event_id BIGINT   NOT NULL,
    chunk_x  SMALLINT NOT NULL CHECK (chunk_x >= 0),
    chunk_z  SMALLINT NOT NULL CHECK (chunk_z >= 0),
    PRIMARY KEY (world_id, event_id, chunk_x, chunk_z),
    FOREIGN KEY (world_id, event_id)
      REFERENCES world_events(world_id, event_id) ON DELETE CASCADE
);

CREATE INDEX world_event_chunks_chunk_resume_idx
    ON world_event_chunks (world_id, chunk_x, chunk_z, event_id);

-- Entity recovery cannot scan every event in a busy zone. This reverse index
-- points at structure/script/ownership events that affect one entity. There is
-- intentionally no entity FK: deletion events must remain replayable.
CREATE TABLE world_event_entities (
    world_id UUID   NOT NULL,
    event_id BIGINT NOT NULL,
    entity_id UUID  NOT NULL,
    PRIMARY KEY (world_id, event_id, entity_id),
    FOREIGN KEY (world_id, event_id)
      REFERENCES world_events(world_id, event_id) ON DELETE CASCADE
);

CREATE INDEX world_event_entities_entity_resume_idx
    ON world_event_entities (world_id, entity_id, event_id);

-- -----------------------------------------------------------------------------
-- 5. Durable entity, script and player checkpoints
--
-- These are recovery snapshots, not network-frame history. Active transforms,
-- velocities and component state remain in the owning zone worker and are
-- flushed periodically or during handoff/unload.
-- -----------------------------------------------------------------------------

CREATE TABLE script_bundles (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id  VARCHAR(16) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    api_version    SMALLINT    NOT NULL DEFAULT 2 CHECK (api_version >= 2),
    source_hash    BYTEA       NOT NULL CHECK (octet_length(source_hash) = 32),
    source_bundle  BYTEA       NOT NULL,
    manifest       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, source_hash)
);

-- Canonical immutable definitions for entities that already exist in the
-- authoritative world. Browser-local backpack entries are never stored here.
-- A locally placed entity is validated/canonicalized before kind=1 is created;
-- block sets become terrain events and color sets remain client-only.
CREATE TABLE build_assets (
    asset_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    kind              SMALLINT    NOT NULL DEFAULT 1 CHECK (kind = 1),
    owner_user_id     VARCHAR(16) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    scope_world_id    UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    codec_version     SMALLINT    NOT NULL DEFAULT 1 CHECK (codec_version >= 1),
    content_hash      BYTEA       NOT NULL CHECK (octet_length(content_hash) = 32),
    block_count       INTEGER     NOT NULL DEFAULT 0 CHECK (block_count BETWEEN 0 AND 65536),
    uncompressed_size INTEGER     NOT NULL CHECK (uncompressed_size BETWEEN 0 AND 67108864),
    compression       SMALLINT    NOT NULL DEFAULT 1 CHECK (compression IN (0, 1)),
    payload           BYTEA,
    object_key        TEXT        CHECK (object_key IS NULL OR char_length(object_key) BETWEEN 1 AND 1024),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((payload IS NULL) <> (object_key IS NULL)),
    UNIQUE (scope_world_id, kind, codec_version, content_hash),
    UNIQUE (asset_id, kind, scope_world_id)
);

COMMENT ON COLUMN build_assets.kind IS '1=durable world ENTITY definition; not a cloud-backpack category';

CREATE TABLE entity_snapshots (
    world_id           UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    entity_id          UUID        NOT NULL DEFAULT gen_random_uuid(),
    owner_user_id      VARCHAR(16) REFERENCES users(id) ON DELETE SET NULL,
    script_bundle_id   UUID        REFERENCES script_bundles(id) ON DELETE SET NULL,
    definition_asset_id UUID       NOT NULL,
    definition_asset_kind SMALLINT NOT NULL DEFAULT 1 CHECK (definition_asset_kind = 1),
    home_zone_x        SMALLINT    NOT NULL CHECK (home_zone_x >= 0),
    home_zone_z        SMALLINT    NOT NULL CHECK (home_zone_z >= 0),
    home_chunk_x       SMALLINT    NOT NULL CHECK (home_chunk_x >= 0),
    home_chunk_z       SMALLINT    NOT NULL CHECK (home_chunk_z >= 0),
    desired_run_state SMALLINT     NOT NULL DEFAULT 1 CHECK (desired_run_state IN (0, 1)),
    runtime_health    SMALLINT     NOT NULL DEFAULT 0 CHECK (runtime_health IN (0, 1, 2, 3)),
    lifecycle_epoch   BIGINT       NOT NULL DEFAULT 0 CHECK (lifecycle_epoch >= 0),
    last_sleep_reason SMALLINT     NOT NULL DEFAULT 0 CHECK (last_sleep_reason BETWEEN 0 AND 32767),
    last_slept_at     TIMESTAMPTZ,
    ownership_epoch    BIGINT      NOT NULL DEFAULT 1 CHECK (ownership_epoch > 0),
    revision           BIGINT      NOT NULL DEFAULT 0 CHECK (revision >= 0),
    last_event_id      BIGINT      NOT NULL DEFAULT 0 CHECK (last_event_id >= 0),
    definition_version SMALLINT    NOT NULL DEFAULT 1 CHECK (definition_version >= 1),
    runtime_version    SMALLINT    NOT NULL DEFAULT 1 CHECK (runtime_version >= 1),
    runtime_state      BYTEA       NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, entity_id),
    FOREIGN KEY (definition_asset_id, definition_asset_kind, world_id)
      REFERENCES build_assets(asset_id, kind, scope_world_id)
      ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX entity_snapshots_zone_idx
    ON entity_snapshots (world_id, home_zone_x, home_zone_z);

CREATE INDEX entity_snapshots_chunk_idx
    ON entity_snapshots (world_id, home_chunk_x, home_chunk_z);

COMMENT ON COLUMN entity_snapshots.desired_run_state IS
  'Durable player/admin intent: 0=disabled, 1=enabled; runtime lifecycle remains worker-local';
COMMENT ON COLUMN entity_snapshots.runtime_health IS
  '0=healthy, 1=retryable fault, 2=scripts disabled, 3=quarantined';
COMMENT ON COLUMN entity_snapshots.lifecycle_epoch IS
  'Monotonic conditional-checkpoint generation; stale asynchronous sleep writes are rejected';

-- Large entities may touch more than their pivot chunk. This manifest lets an
-- AOI change find sleeping entities without scanning an entire 32x32 zone.
CREATE TABLE entity_chunk_coverage (
    world_id UUID     NOT NULL,
    entity_id UUID    NOT NULL,
    chunk_x  SMALLINT NOT NULL CHECK (chunk_x >= 0),
    chunk_z  SMALLINT NOT NULL CHECK (chunk_z >= 0),
    PRIMARY KEY (world_id, entity_id, chunk_x, chunk_z),
    FOREIGN KEY (world_id, entity_id)
      REFERENCES entity_snapshots(world_id, entity_id) ON DELETE CASCADE
);

CREATE INDEX entity_chunk_coverage_aoi_idx
    ON entity_chunk_coverage (world_id, chunk_x, chunk_z, entity_id);

CREATE TABLE player_snapshots (
    world_id      UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    user_id       VARCHAR(16) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision      BIGINT      NOT NULL DEFAULT 0 CHECK (revision >= 0),
    last_event_id BIGINT      NOT NULL DEFAULT 0 CHECK (last_event_id >= 0),
    state_version SMALLINT    NOT NULL DEFAULT 1 CHECK (state_version >= 1),
    state         BYTEA       NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, user_id)
);

-- Stable identity is separated from runtime state. No birth point is stored:
-- when player_snapshots has no valid row, bootstrap samples an ephemeral X/Z
-- position uniformly across the complete wrapped world at Y=32 m, and the
-- admitted client immediately checkpoints it. Skin is never duplicated here:
-- every entry reads the URL/model from the existing users row.
CREATE TABLE world_player_profiles (
    world_id         UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    user_id          VARCHAR(16) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    player_entity_id UUID        NOT NULL DEFAULT gen_random_uuid(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, user_id),
    UNIQUE (world_id, player_entity_id)
);

CREATE OR REPLACE FUNCTION validate_world_player_profile_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.player_entity_id IS DISTINCT FROM OLD.player_entity_id THEN
    RAISE EXCEPTION 'stable player identity cannot be changed by an ordinary profile update'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER world_player_profiles_validate_update
BEFORE UPDATE ON world_player_profiles
FOR EACH ROW EXECUTE FUNCTION validate_world_player_profile_update();

-- A checkpoint establishes a safe event-retention watermark. Events at or
-- below event_id may be archived only after every manifest entry is durable.
CREATE TABLE world_checkpoints (
    checkpoint_id BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    world_id      UUID        NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    event_id      BIGINT      NOT NULL CHECK (event_id >= 0),
    server_tick   BIGINT      NOT NULL CHECK (server_tick >= 0),
    codec         SMALLINT    NOT NULL DEFAULT 1 CHECK (codec IN (0, 1)),
    manifest      BYTEA       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (world_id, event_id)
);

CREATE INDEX world_checkpoints_latest_idx
    ON world_checkpoints (world_id, event_id DESC);

-- -----------------------------------------------------------------------------
-- 6. Timestamp maintenance (control-plane only; not used for synchronization)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER worlds_set_updated_at
BEFORE UPDATE ON worlds FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER world_members_set_updated_at
BEFORE UPDATE ON world_members FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER world_session_slots_set_updated_at
BEFORE UPDATE ON world_session_slots FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER world_join_queue_set_updated_at
BEFORE UPDATE ON world_join_queue FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER zone_leases_set_updated_at
BEFORE UPDATE ON zone_leases FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER world_event_streams_set_updated_at
BEFORE UPDATE ON world_event_streams FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER chunk_snapshots_set_updated_at
BEFORE UPDATE ON chunk_snapshots FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER entity_snapshots_set_updated_at
BEFORE UPDATE ON entity_snapshots FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER player_snapshots_set_updated_at
BEFORE UPDATE ON player_snapshots FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER world_player_profiles_set_updated_at
BEFORE UPDATE ON world_player_profiles FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
