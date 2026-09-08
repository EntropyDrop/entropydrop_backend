"""Space schema. Standalone mode contains no login credentials or credit balance."""
import datetime
import uuid
from sqlalchemy import Column, Integer, BigInteger, SmallInteger, String, DateTime, Text, JSON, Boolean, Float, Index, UniqueConstraint, CheckConstraint, Date, ForeignKey, Uuid, LargeBinary
from config import settings
from space.database import Base
from space.ids import generate_base58_id

ACCOUNT_TABLE = "space_accounts" if settings.SPACE_STANDALONE else "users"

if settings.SPACE_STANDALONE:
    class User(Base):
        __tablename__ = "space_accounts"
        id = Column(String(16), primary_key=True)
        username = Column(String(100), nullable=True)
        skin_url = Column(String(500), nullable=True)
        skin_type = Column(String(20), nullable=False, default="strong")
        updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
        # Request-local values supplied by the account service, never authoritative DB columns.
        credits = 0
        api_key_count = 0
        is_admin = False
else:
    def __getattr__(name):
        if name in {"User", "CreditLog", "SpaceApiKey"}:
            import models as accounts
            return getattr(accounts, name)
        raise AttributeError(name)

class SpaceWorld(Base):
    """Space world control-plane metadata; user identity remains in users."""
    __tablename__ = "worlds"

    id = Column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = Column(String(16), ForeignKey(f"{ACCOUNT_TABLE}.id", ondelete="RESTRICT"), nullable=True, index=True)
    name = Column(String(128), nullable=False, default="EntropyDrop Space")
    seed = Column(Integer, nullable=False)
    terrain_generator_version = Column(Integer, nullable=False, default=1)
    protocol_version = Column(Integer, nullable=False, default=2)
    width_chunks = Column(Integer, nullable=False, default=1024)
    length_chunks = Column(Integer, nullable=False, default=128)
    zone_size_chunks = Column(Integer, nullable=False, default=32)
    max_online_players = Column(Integer, nullable=False, default=32)
    status = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)


class SpaceWorldPlayerProfile(Base):
    """Stable Space entity identity for an existing user; runtime state lives elsewhere."""
    __tablename__ = "world_player_profiles"
    __table_args__ = (
        UniqueConstraint("world_id", "player_entity_id", name="uq_world_player_entity"),
    )

    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(String(16), ForeignKey(f"{ACCOUNT_TABLE}.id", ondelete="CASCADE"), primary_key=True)
    player_entity_id = Column(Uuid(as_uuid=False), default=lambda: str(uuid.uuid4()), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)


class SpaceWorldEventStream(Base):
    """Per-world monotonic cursor for reliable incremental terrain delivery."""
    __tablename__ = "world_event_streams"

    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    last_event_id = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpacePlayerSnapshot(Base):
    """Latest durable reconnect state for one player in one Space world."""
    __tablename__ = "player_snapshots"

    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(String(16), ForeignKey(f"{ACCOUNT_TABLE}.id", ondelete="CASCADE"), primary_key=True)
    revision = Column(BigInteger, nullable=False, default=0)
    last_event_id = Column(BigInteger, nullable=False, default=0)
    state_version = Column(SmallInteger, nullable=False, default=1)
    state = Column(LargeBinary, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpaceWorldEntity(Base):
    """Durable entity state for browser leases and metered server execution."""
    __tablename__ = "space_world_entities"
    __table_args__ = (
        UniqueConstraint(
            "world_id", "owner_user_id", "create_operation_id",
            name="uq_space_world_entity_create_operation",
        ),
        CheckConstraint(
            "desired_run_state IN ('running', 'stopped')",
            name="ck_space_world_entity_run_state",
        ),
        CheckConstraint(
            "yaw_quarter_turns >= 0 AND yaw_quarter_turns <= 3",
            name="ck_space_world_entity_yaw",
        ),
        CheckConstraint("revision >= 1", name="ck_space_world_entity_revision"),
        CheckConstraint("execution_epoch >= 0", name="ck_space_world_entity_execution_epoch"),
        CheckConstraint("execution_mode IN ('browser', 'hosted')", name="ck_space_hosting_mode"),
        CheckConstraint("hosting_remaining_ms BETWEEN 0 AND 3600000", name="ck_space_hosting_time"),
        CheckConstraint("hosting_budget_remaining BETWEEN 0 AND 168", name="ck_space_hosting_budget"),
        CheckConstraint("hosting_billed_hours >= 0", name="ck_space_hosting_billed"),
        CheckConstraint("NOT hosting_enabled OR (execution_mode = 'hosted' AND desired_run_state = 'running')", name="ck_space_hosting_enabled"),
        CheckConstraint("size_bytes > 0", name="ck_space_world_entity_size"),
        CheckConstraint("snapshot_size_bytes >= 0", name="ck_space_world_entity_snapshot_size"),
        Index("ix_space_world_entities_world_position", "world_id", "position_x_cm", "position_z_cm"),
        Index("ix_space_world_entities_owner", "world_id", "owner_user_id", "created_at"),
    )

    world_id = Column(
        Uuid(as_uuid=False),
        ForeignKey("worlds.id", ondelete="CASCADE"),
        primary_key=True,
    )
    id = Column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = Column(
        String(16),
        ForeignKey(f"{ACCOUNT_TABLE}.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name = Column(String(80), nullable=False)
    schema_version = Column(SmallInteger, nullable=False, default=6, server_default="6")
    content_digest = Column(LargeBinary(32), nullable=False)
    definition = Column(LargeBinary, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    snapshot = Column(LargeBinary, nullable=True)
    snapshot_digest = Column(LargeBinary(32), nullable=True)
    snapshot_size_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    position_x_cm = Column(Integer, nullable=False)
    position_y_cm = Column(Integer, nullable=False)
    position_z_cm = Column(Integer, nullable=False)
    yaw_quarter_turns = Column(SmallInteger, nullable=False, default=0, server_default="0")
    desired_run_state = Column(String(16), nullable=False, default="running", server_default="running")
    revision = Column(BigInteger, nullable=False, default=1, server_default="1")
    create_operation_id = Column(Uuid(as_uuid=False), nullable=False)
    create_request_digest = Column(LargeBinary(32), nullable=False)
    last_control_operation_id = Column(Uuid(as_uuid=False), nullable=True)
    last_checkpoint_operation_id = Column(Uuid(as_uuid=False), nullable=True)
    last_checkpoint_request_digest = Column(LargeBinary(32), nullable=True)
    execution_instance_id = Column(Uuid(as_uuid=False), nullable=True)
    execution_lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    execution_epoch = Column(BigInteger, nullable=False, default=0, server_default="0")
    execution_mode = Column(String(16), nullable=False, default="browser", server_default="browser")
    hosting_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    hosting_remaining_ms = Column(BigInteger, nullable=False, default=0, server_default="0")
    hosting_budget_remaining = Column(Integer, nullable=False, default=0, server_default="0")
    hosting_billed_hours = Column(Integer, nullable=False, default=0, server_default="0")
    hosting_authorization_id = Column(String(36), nullable=True)
    hosting_anchor = Column(JSON, nullable=True)
    hosting_reason = Column(String(80), nullable=True)
    hosting_error = Column(String(500), nullable=True)
    hosting_last_tick_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpaceEntityOperation(Base):
    """Durable acknowledgements for code/default edits and start/stop commands."""
    __tablename__ = "space_entity_operations"
    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    operation_id = Column(Uuid(as_uuid=False), primary_key=True)
    request_digest = Column(LargeBinary(32), nullable=False)
    result = Column(JSON, nullable=False)


class SpaceHostingOperation(Base):
    """Durable dedupe receipts: a delayed retry can never reopen a stopped job."""
    __tablename__ = "space_hosting_operations"
    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    operation_id = Column(Uuid(as_uuid=False), primary_key=True)
    request_digest = Column(LargeBinary(32), nullable=False)
    result = Column(JSON, nullable=False)


class SpaceHostingWorker(Base):
    __tablename__ = "space_hosting_workers"
    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    instance_id = Column(String(36), nullable=False)
    epoch = Column(BigInteger, nullable=False, default=1)
    lease_expires_at = Column(DateTime(timezone=True), nullable=False)


class SpaceHostingGrant(Base):
    """Local durable reservation/outbox. Retained when its entity is deleted."""
    __tablename__ = "space_hosting_grants"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    authorization_id = Column(String(36), nullable=False)
    world_id = Column(Uuid(as_uuid=False), nullable=False, index=True)
    entity_id = Column(Uuid(as_uuid=False), nullable=False, index=True)
    state = Column(String(16), nullable=False, default="pending")
    settlement = Column(String(16), nullable=True)
    settled = Column(Boolean, nullable=False, default=False)
    error = Column(String(80), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    __table_args__ = (CheckConstraint("state IN ('pending','ready','consumed','cancelled','failed')", name="ck_space_grant_state"),)


class SpaceChunkSnapshot(Base):
    """Packed player-authored voxel overlay for one Space terrain chunk."""
    __tablename__ = "chunk_snapshots"
    __table_args__ = (
        Index("ix_chunk_snapshots_world_revision", "world_id", "revision"),
        Index("chunk_snapshots_resume_idx", "world_id", "last_event_id"),
    )

    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    chunk_x = Column(Integer, primary_key=True)
    chunk_z = Column(Integer, primary_key=True)
    revision = Column(BigInteger, nullable=False, default=0)
    last_event_id = Column(BigInteger, nullable=False, default=0)
    codec = Column(SmallInteger, nullable=False, default=0)
    codec_version = Column(SmallInteger, nullable=False, default=1)
    uncompressed_size = Column(Integer, nullable=False, default=0)
    content_hash = Column(LargeBinary(32), nullable=False)
    payload = Column(LargeBinary, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpaceSurfaceZoneSnapshot(Base):
    """Compressed 8x8-per-chunk far-surface summary for one world zone."""
    __tablename__ = "space_surface_zone_snapshots"
    __table_args__ = (
        Index("ix_space_surface_zones_world_revision", "world_id", "revision"),
    )

    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    zone_x = Column(SmallInteger, primary_key=True)
    zone_z = Column(SmallInteger, primary_key=True)
    revision = Column(BigInteger, nullable=False, default=1)
    source_terrain_revision = Column(BigInteger, nullable=False, default=0)
    terrain_generator_version = Column(Integer, nullable=False)
    schema_version = Column(SmallInteger, nullable=False, default=3)
    samples_per_chunk_axis = Column(SmallInteger, nullable=False, default=8)
    codec = Column(SmallInteger, nullable=False, default=1)
    uncompressed_size = Column(Integer, nullable=False)
    content_hash = Column(LargeBinary(32), nullable=False)
    payload = Column(LargeBinary, nullable=False)
    dirty = Column(Boolean, nullable=False, default=False, server_default="false")
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpaceTerrainMutationBatch(Base):
    """Idempotency receipt for one accepted client terrain mutation batch."""
    __tablename__ = "space_terrain_mutation_batches"
    __table_args__ = (
        Index("ix_space_terrain_batches_retention", "dedupe_epoch", "client_created_at"),
        CheckConstraint("dedupe_epoch IN (0, 1)", name="ck_space_terrain_batches_epoch"),
        CheckConstraint(
            "dedupe_epoch = 0 OR client_created_at IS NOT NULL",
            name="ck_space_terrain_batches_epoch_timestamp",
        ),
    )

    world_id = Column(Uuid(as_uuid=False), ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True)
    batch_id = Column(Uuid(as_uuid=False), primary_key=True)
    actor_user_id = Column(String(16), ForeignKey(f"{ACCOUNT_TABLE}.id", ondelete="SET NULL"), nullable=True)
    dedupe_epoch = Column(SmallInteger, nullable=False, default=0, server_default="0")
    client_created_at = Column(DateTime(timezone=True), nullable=True)
    result = Column(JSON, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpaceUsageBucket(Base):
    """Authoritative per-principal counters for bounded Space operations."""
    __tablename__ = "space_usage_buckets"
    __table_args__ = (
        CheckConstraint("used >= 0", name="ck_space_usage_bucket_used"),
        CheckConstraint("window_seconds > 0", name="ck_space_usage_bucket_window"),
        Index(
            "ix_space_usage_buckets_retention",
            "metric", "window_seconds", "bucket_start",
        ),
    )

    principal_id = Column(String(64), primary_key=True)
    scope_id = Column(String(64), primary_key=True)
    metric = Column(String(48), primary_key=True)
    window_seconds = Column(Integer, primary_key=True)
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    used = Column(BigInteger, nullable=False, default=0, server_default="0")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpaceMarketResource(Base):
    """Canonical, immutable Space backpack resource published to the market."""
    __tablename__ = "space_market_resources"
    __table_args__ = (
        UniqueConstraint("content_digest", name="uq_space_market_resource_digest"),
        CheckConstraint(
            "kind IN ('blockset', 'entity', 'colorset')",
            name="ck_space_market_resource_kind",
        ),
        CheckConstraint(
            "schema_version = 6",
            name="ck_space_market_resource_schema_version",
        ),
        CheckConstraint("license = 'AGPL-3.0-only'", name="ck_space_market_resource_license"),
        CheckConstraint(
            "downloads_count >= 0 AND likes_count >= 0",
            name="ck_space_market_resource_counts",
        ),
        CheckConstraint(
            "object_key IS NOT NULL",
            name="ck_space_market_resource_storage",
        ),
        Index(
            "ix_space_market_resources_downloads",
            "kind", "downloads_count", "created_at",
        ),
        Index(
            "ix_space_market_resources_likes",
            "kind", "likes_count", "created_at",
        ),
        Index(
            "ix_space_market_resources_latest",
            "kind", "created_at",
        ),
        Index(
            "ix_space_market_resources_publisher_day",
            "publisher_user_id", "created_at",
        ),
    )

    id = Column(String(16), primary_key=True, default=generate_base58_id)
    publisher_user_id = Column(
        String(16),
        ForeignKey(f"{ACCOUNT_TABLE}.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind = Column(String(16), nullable=False)
    schema_version = Column(SmallInteger, nullable=False, default=6, server_default="6")
    name = Column(String(80), nullable=False)
    license = Column(String(32), nullable=False, default="AGPL-3.0-only", server_default="AGPL-3.0-only")
    content_digest = Column(LargeBinary(32), nullable=False)
    object_key = Column(String(512), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    block_count = Column(Integer, nullable=False, default=0, server_default="0")
    node_count = Column(Integer, nullable=False, default=0, server_default="0")
    script_count = Column(Integer, nullable=False, default=0, server_default="0")
    downloads_count = Column(Integer, nullable=False, default=0, server_default="0")
    likes_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpaceMarketResourceLike(Base):
    """One authenticated player's like on one active Space market resource."""
    __tablename__ = "space_market_resource_likes"

    resource_id = Column(
        String(16),
        ForeignKey("space_market_resources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = Column(
        String(16),
        ForeignKey(f"{ACCOUNT_TABLE}.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )


class SpaceHostingAuthorization(Base):
    """Durable revocation outbox, including entities deleted before reconciliation."""
    __tablename__ = "space_hosting_authorizations"
    id = Column(String(36), primary_key=True)
    world_id = Column(Uuid(as_uuid=False), nullable=False, index=True)
    entity_id = Column(Uuid(as_uuid=False), nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)
    settled = Column(Boolean, nullable=False, default=False)
