import datetime
import hashlib
import json
import math
import secrets
import threading
import time
import uuid
from typing import Literal

import zstandard as zstd
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth
import models
import space_surface
from config import settings
from database import get_db
from rate_limit import limiter
from space_quota import QuotaWindow, UTC_DAY_SECONDS, reserve as reserve_quota, usage as quota_usage


router = APIRouter(prefix="/space/api/v2", tags=["space"])

# 20x ordinary API rate limits (ordinary is 60/min, 1000/hr, 4000/day)
SPACE_HIGH_FREQ_RATE_LIMIT = "1200/minute; 20000/hour; 80000/day"
SPACE_PUBLIC_STATUS_RATE_LIMIT = "120/minute; 2000/hour"
# Reconnect checkpoints may run for an entire long-lived play session. Keep
# burst/hour protection, but do not turn normal continuous play into a daily 429.
SPACE_POSITION_RATE_LIMIT = "1200/minute; 20000/hour"

SPACE_CHUNK_SIZE = 16
SPACE_WORLD_HEIGHT = 256
SPACE_MICRO_DIVISIONS = 5
MAX_TERRAIN_MUTATIONS_PER_BATCH = 256
MAX_CHUNK_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_PAGE_SIZE = 256
MAX_TERRAIN_AOI_RADIUS_CHUNKS = 64
SPACE_TERRAIN_BURST_WINDOW_SECONDS = 10
SPACE_TERRAIN_BURST_LIMIT = settings.SPACE_TERRAIN_BURST_LIMIT
SPACE_TERRAIN_HOURLY_LIMIT = settings.SPACE_TERRAIN_HOURLY_LIMIT
SPACE_TERRAIN_DAILY_LIMIT = settings.SPACE_TERRAIN_DAILY_LIMIT
SPACE_TERRAIN_WORLD_SECOND_LIMIT = settings.SPACE_TERRAIN_WORLD_SECOND_LIMIT
SPACE_TERRAIN_MAX_CHUNKS_PER_BATCH = settings.SPACE_TERRAIN_MAX_CHUNKS_PER_BATCH
SPACE_TERRAIN_MAX_ZONES_PER_BATCH = settings.SPACE_TERRAIN_MAX_ZONES_PER_BATCH
SPACE_TERRAIN_EDIT_RADIUS_CHUNKS = settings.SPACE_TERRAIN_EDIT_RADIUS_CHUNKS
SPACE_TERRAIN_POSITION_GRACE_SECONDS = settings.SPACE_TERRAIN_POSITION_GRACE_SECONDS
SPACE_TERRAIN_MAX_EVENT_BYTES = settings.SPACE_TERRAIN_MAX_EVENT_BYTES
SPACE_TERRAIN_MAX_RESPONSE_BYTES = settings.SPACE_TERRAIN_MAX_RESPONSE_BYTES
SPACE_TERRAIN_USAGE_SCOPE = "terrain"
SPACE_ONLINE_PRESENCE_SECONDS = 30
MIN_PLAYER_Y_CM = -100_000
MAX_PLAYER_Y_CM = 1_000_000
SPACE_CHUNK_CODEC_RAW = 0
SPACE_CHUNK_CODEC_ZSTD = 1
TERRAIN_RECEIPT_CLEANUP_INTERVAL_SECONDS = 3600
TERRAIN_RECEIPT_CLEANUP_BATCH_SIZE = 400
TERRAIN_BATCH_MAX_FUTURE_SKEW_SECONDS = 300
_receipt_cleanup_lock = threading.Lock()
_last_receipt_cleanup_at = 0.0


class SpaceWorldResponse(BaseModel):
    id: str
    name: str
    seed: int
    terrain_generator_version: int
    terrain_revision: int
    surface_snapshot_url: str


class SpacePlayerResponse(BaseModel):
    user_id: str
    username: str | None
    is_admin: bool
    player_entity_id: str
    skin_url: str | None
    skin_type: str
    start_x_cm: int
    start_y_cm: int
    start_z_cm: int
    start_yaw_q15: int
    resumed: bool


class SpaceBootstrapResponse(BaseModel):
    protocol_version: int
    max_online_players: int
    queue_enabled: bool
    websocket_url: str
    world: SpaceWorldResponse
    player: SpacePlayerResponse


class SpacePublicStatusResponse(BaseModel):
    world_id: str
    online_players: int
    max_online_players: int
    presence_window_seconds: int
    updated_at: datetime.datetime


class TerrainMutation(BaseModel):
    kind: Literal["set_standard", "set_micro", "remove_micro", "clear_micro_cell"]
    x: int | None = None
    y: int | None = None
    z: int | None = None
    mx: int | None = None
    my: int | None = None
    mz: int | None = None
    block: int | None = None
    color: int | None = None
    part: str | None = Field(default=None, max_length=64)


class TerrainMutationBatchRequest(BaseModel):
    batch_id: uuid.UUID
    dedupe_epoch: Literal[0, 1] = 0
    created_at_ms: int | None = Field(default=None, ge=0)
    mutations: list[TerrainMutation] = Field(
        min_length=1,
        max_length=MAX_TERRAIN_MUTATIONS_PER_BATCH,
    )


class PlayerPositionUpdateRequest(BaseModel):
    x_cm: int
    y_cm: int = Field(ge=MIN_PLAYER_Y_CM, le=MAX_PLAYER_Y_CM)
    z_cm: int
    yaw_q15: int = Field(ge=-32767, le=32767)
    pitch_q15: int = Field(default=0, ge=-32767, le=32767)


class SpaceHeartbeatRequest(BaseModel):
    x_cm: int | None = None
    y_cm: int | None = Field(default=None, ge=MIN_PLAYER_Y_CM, le=MAX_PLAYER_Y_CM)
    z_cm: int | None = None
    yaw_q15: int | None = Field(default=None, ge=-32767, le=32767)
    pitch_q15: int | None = Field(default=0, ge=-32767, le=32767)
    since_terrain_revision: int = Field(default=0, ge=0)
    include_players: bool = True
    center_chunk_x: int | None = None
    center_chunk_z: int | None = None
    terrain_radius_chunks: int | None = Field(
        default=None,
        ge=1,
        le=MAX_TERRAIN_AOI_RADIUS_CHUNKS,
    )


def _empty_chunk_overlay() -> dict:
    return {"standard": [], "micro": []}


def _decode_chunk_overlay(snapshot: models.SpaceChunkSnapshot | None) -> dict:
    if snapshot is None or not snapshot.payload:
        return _empty_chunk_overlay()
    try:
        if snapshot.codec == SPACE_CHUNK_CODEC_RAW:
            encoded = bytes(snapshot.payload)
        elif snapshot.codec == SPACE_CHUNK_CODEC_ZSTD:
            if not (0 <= snapshot.uncompressed_size <= MAX_CHUNK_SNAPSHOT_BYTES):
                raise ValueError("invalid expanded size")
            # Compression contexts are intentionally request-local: synchronous
            # FastAPI handlers may execute concurrently on worker threads.
            encoded = zstd.ZstdDecompressor().decompress(
                bytes(snapshot.payload),
                max_output_size=snapshot.uncompressed_size,
            )
        else:
            raise ValueError("unsupported chunk codec")
    except (ValueError, zstd.ZstdError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "CORRUPT_CHUNK_SNAPSHOT", "message": "World chunk snapshot checksum verification failed."},
        ) from exc
    if (
        snapshot.uncompressed_size != len(encoded)
        or snapshot.content_hash != hashlib.sha256(encoded).digest()
    ):
        raise HTTPException(
            status_code=500,
            detail={"code": "CORRUPT_CHUNK_SNAPSHOT", "message": "World chunk snapshot checksum verification failed."},
        )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "CORRUPT_CHUNK_SNAPSHOT", "message": "World chunk snapshot corrupted."},
        ) from exc
    return {
        "standard": payload.get("standard", []) if isinstance(payload.get("standard"), list) else [],
        "micro": payload.get("micro", []) if isinstance(payload.get("micro"), list) else [],
    }


def _encode_chunk_overlay(payload: dict) -> tuple[bytes, bytes, int, int]:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_CHUNK_SNAPSHOT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "CHUNK_OVERLAY_TOO_LARGE", "message": "Chunk block modifications exceed size limit."},
        )
    compressed = zstd.ZstdCompressor(level=6).compress(encoded)
    if len(compressed) < len(encoded):
        stored = compressed
        codec = SPACE_CHUNK_CODEC_ZSTD
    else:
        stored = encoded
        codec = SPACE_CHUNK_CODEC_RAW
    return stored, hashlib.sha256(encoded).digest(), codec, len(encoded)


def _require_world_membership(
    db: Session,
    world_id: str,
    user: models.User,
) -> models.SpaceWorld:
    world = db.query(models.SpaceWorld).filter(models.SpaceWorld.id == world_id).first()
    if world is None:
        raise HTTPException(status_code=404, detail={"code": "WORLD_NOT_FOUND"})
    membership = db.query(models.SpaceWorldPlayerProfile).filter(
        models.SpaceWorldPlayerProfile.world_id == world.id,
        models.SpaceWorldPlayerProfile.user_id == user.id,
    ).first()
    if membership is None:
        raise HTTPException(status_code=403, detail={"code": "WORLD_MEMBERSHIP_REQUIRED"})
    return world


def _validate_player_position(
    world: models.SpaceWorld,
    x_cm: int,
    y_cm: int,
    z_cm: int,
    yaw_q15: int,
    pitch_q15: int = 0,
) -> dict[str, int]:
    width_cm = world.width_chunks * SPACE_CHUNK_SIZE * 100
    length_cm = world.length_chunks * SPACE_CHUNK_SIZE * 100
    if not (
        0 <= x_cm < width_cm
        and MIN_PLAYER_Y_CM <= y_cm <= MAX_PLAYER_Y_CM
        and 0 <= z_cm < length_cm
        and -32767 <= yaw_q15 <= 32767
        and -32767 <= pitch_q15 <= 32767
    ):
        raise HTTPException(status_code=422, detail={"code": "PLAYER_POSITION_OUT_OF_BOUNDS"})
    return {
        "x_cm": x_cm,
        "y_cm": y_cm,
        "z_cm": z_cm,
        "yaw_q15": yaw_q15,
        "pitch_q15": pitch_q15,
    }


def _encode_player_snapshot(position: dict[str, int]) -> bytes:
    return json.dumps(
        {"position": position},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _decode_player_snapshot(
    snapshot: models.SpacePlayerSnapshot | None,
    world: models.SpaceWorld,
) -> dict[str, int] | None:
    if snapshot is None or snapshot.state_version != 1:
        return None
    try:
        payload = json.loads(snapshot.state.decode("utf-8"))
        position = payload["position"]
        return _validate_player_position(
            world,
            int(position["x_cm"]),
            int(position["y_cm"]),
            int(position["z_cm"]),
            int(position["yaw_q15"]),
            int(position.get("pitch_q15", 0)),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, HTTPException):
        # A bad or future snapshot must not prevent login. Bootstrap will issue
        # a fresh ephemeral world-wide random start instead.
        return None


def _parse_snapshot_cursor(cursor: str | None) -> tuple[int, int] | None:
    if not cursor:
        return None
    try:
        cx, cz = (int(part) for part in cursor.split(",", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail={"code": "INVALID_CURSOR"})
    if cx < 0 or cz < 0:
        raise HTTPException(status_code=422, detail={"code": "INVALID_CURSOR"})
    return cx, cz


def _wrapped_chunk_axis_filter(column, center: int, radius: int, size: int):
    center %= size
    if radius * 2 + 1 >= size:
        return None
    lower = center - radius
    upper = center + radius
    if lower < 0:
        return or_(column >= lower + size, column <= upper)
    if upper >= size:
        return or_(column >= lower, column <= upper - size)
    return and_(column >= lower, column <= upper)


def _chunk_aoi_filters(
    world: models.SpaceWorld,
    center_chunk_x: int | None,
    center_chunk_z: int | None,
    radius_chunks: int | None,
) -> list:
    values = (center_chunk_x, center_chunk_z, radius_chunks)
    if all(value is None for value in values):
        return []
    if any(value is None for value in values):
        raise HTTPException(status_code=422, detail={"code": "INCOMPLETE_TERRAIN_AOI"})
    assert center_chunk_x is not None and center_chunk_z is not None and radius_chunks is not None
    if not (1 <= radius_chunks <= MAX_TERRAIN_AOI_RADIUS_CHUNKS):
        raise HTTPException(status_code=422, detail={"code": "INVALID_TERRAIN_AOI_RADIUS"})
    filters = [
        _wrapped_chunk_axis_filter(
            models.SpaceChunkSnapshot.chunk_x,
            center_chunk_x,
            radius_chunks,
            world.width_chunks,
        ),
        _wrapped_chunk_axis_filter(
            models.SpaceChunkSnapshot.chunk_z,
            center_chunk_z,
            radius_chunks,
            world.length_chunks,
        ),
    ]
    return [condition for condition in filters if condition is not None]


def _terrain_batch_client_created_at(
    batch_request: TerrainMutationBatchRequest,
    now: datetime.datetime,
) -> datetime.datetime | None:
    if batch_request.dedupe_epoch == 0:
        return None
    if batch_request.created_at_ms is None:
        raise HTTPException(status_code=422, detail={"code": "TERRAIN_BATCH_TIMESTAMP_REQUIRED"})
    try:
        created_at = datetime.datetime.fromtimestamp(
            batch_request.created_at_ms / 1000,
            tz=datetime.timezone.utc,
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_TERRAIN_BATCH_TIMESTAMP"}) from exc
    retention_days = max(1, int(settings.SPACE_TERRAIN_BATCH_RECEIPT_RETENTION_DAYS))
    if created_at < now - datetime.timedelta(days=retention_days):
        raise HTTPException(
            status_code=409,
            detail={"code": "TERRAIN_BATCH_EXPIRED", "message": "Terrain batch edit has expired safe retry window."},
        )
    if created_at > now + datetime.timedelta(seconds=TERRAIN_BATCH_MAX_FUTURE_SKEW_SECONDS):
        raise HTTPException(status_code=422, detail={"code": "INVALID_TERRAIN_BATCH_TIMESTAMP"})
    return created_at


def _maybe_cleanup_terrain_receipts(db: Session, now: datetime.datetime) -> None:
    global _last_receipt_cleanup_at
    monotonic_now = time.monotonic()
    if monotonic_now - _last_receipt_cleanup_at < TERRAIN_RECEIPT_CLEANUP_INTERVAL_SECONDS:
        return
    with _receipt_cleanup_lock:
        if monotonic_now - _last_receipt_cleanup_at < TERRAIN_RECEIPT_CLEANUP_INTERVAL_SECONDS:
            return
        retention_days = max(1, int(settings.SPACE_TERRAIN_BATCH_RECEIPT_RETENTION_DAYS))
        cutoff = now - datetime.timedelta(days=retention_days)
        expired_keys = db.query(
            models.SpaceTerrainMutationBatch.world_id,
            models.SpaceTerrainMutationBatch.batch_id,
        ).filter(
            models.SpaceTerrainMutationBatch.dedupe_epoch == 1,
            models.SpaceTerrainMutationBatch.client_created_at < cutoff,
        ).order_by(
            models.SpaceTerrainMutationBatch.client_created_at.asc(),
        ).limit(TERRAIN_RECEIPT_CLEANUP_BATCH_SIZE).all()
        if expired_keys:
            db.query(models.SpaceTerrainMutationBatch).filter(or_(*[
                and_(
                    models.SpaceTerrainMutationBatch.world_id == row.world_id,
                    models.SpaceTerrainMutationBatch.batch_id == row.batch_id,
                )
                for row in expired_keys
            ])).delete(synchronize_session=False)
        _last_receipt_cleanup_at = monotonic_now


def _terrain_receipt_response(world_id: str, batch_id: str, stored_result: dict) -> dict:
    # New receipts omit identifiers already present in indexed columns. Rebuild
    # the stable public response while remaining compatible with legacy rows.
    result = dict(stored_result or {})
    result["world_id"] = str(world_id)
    result["batch_id"] = str(batch_id)
    return result


def _standard_cell(mutation: TerrainMutation, world: models.SpaceWorld) -> tuple[int, int, int]:
    if mutation.x is None or mutation.y is None or mutation.z is None:
        raise HTTPException(status_code=422, detail={"code": "INVALID_TERRAIN_MUTATION"})
    max_x = world.width_chunks * SPACE_CHUNK_SIZE
    max_z = world.length_chunks * SPACE_CHUNK_SIZE
    if not (0 <= mutation.x < max_x and 0 <= mutation.y < SPACE_WORLD_HEIGHT and 0 <= mutation.z < max_z):
        raise HTTPException(status_code=422, detail={"code": "TERRAIN_POSITION_OUT_OF_BOUNDS"})
    return mutation.x, mutation.y, mutation.z


def _micro_cell(mutation: TerrainMutation, world: models.SpaceWorld) -> tuple[int, int, int]:
    if mutation.mx is None or mutation.my is None or mutation.mz is None:
        raise HTTPException(status_code=422, detail={"code": "INVALID_TERRAIN_MUTATION"})
    max_mx = world.width_chunks * SPACE_CHUNK_SIZE * SPACE_MICRO_DIVISIONS
    max_mz = world.length_chunks * SPACE_CHUNK_SIZE * SPACE_MICRO_DIVISIONS
    max_my = SPACE_WORLD_HEIGHT * SPACE_MICRO_DIVISIONS
    if not (0 <= mutation.mx < max_mx and 0 <= mutation.my < max_my and 0 <= mutation.mz < max_mz):
        raise HTTPException(status_code=422, detail={"code": "TERRAIN_POSITION_OUT_OF_BOUNDS"})
    return mutation.mx, mutation.my, mutation.mz


def _chunk_for_standard(x: int, z: int) -> tuple[int, int]:
    return x // SPACE_CHUNK_SIZE, z // SPACE_CHUNK_SIZE


def _chunk_for_micro(mx: int, mz: int) -> tuple[int, int]:
    divisor = SPACE_CHUNK_SIZE * SPACE_MICRO_DIVISIONS
    return mx // divisor, mz // divisor


def _overlay_maps(payload: dict) -> tuple[dict[str, list], dict[str, list]]:
    standard = {
        f"{int(edit[0])},{int(edit[1])},{int(edit[2])}": list(edit[:5])
        for edit in payload["standard"]
        if isinstance(edit, list) and len(edit) >= 5
    }
    micro = {
        f"{int(edit[0])},{int(edit[1])},{int(edit[2])}": list(edit[:5])
        for edit in payload["micro"]
        if isinstance(edit, list) and len(edit) >= 4
    }
    return standard, micro


def _clear_micro_parent(micro: dict[str, list], x: int, y: int, z: int) -> int:
    base_x = x * SPACE_MICRO_DIVISIONS
    base_y = y * SPACE_MICRO_DIVISIONS
    base_z = z * SPACE_MICRO_DIVISIONS
    removed = 0
    for dx in range(SPACE_MICRO_DIVISIONS):
        for dy in range(SPACE_MICRO_DIVISIONS):
            for dz in range(SPACE_MICRO_DIVISIONS):
                if micro.pop(f"{base_x + dx},{base_y + dy},{base_z + dz}", None) is not None:
                    removed += 1
    return removed


def _wrapped_chunk_distance(value: int, center: int, size: int) -> int:
    delta = abs((value % size) - (center % size))
    return min(delta, size - delta)


def _validate_terrain_edit_scope(
    db: Session,
    world: models.SpaceWorld,
    user: models.User,
    touched_chunks: set[tuple[int, int]],
    now: datetime.datetime,
) -> None:
    if user.is_admin or not touched_chunks:
        return
    snapshot = db.query(models.SpacePlayerSnapshot).filter(
        models.SpacePlayerSnapshot.world_id == world.id,
        models.SpacePlayerSnapshot.user_id == user.id,
    ).first()
    position = _decode_player_snapshot(snapshot, world)
    if position is None:
        profile = db.query(models.SpaceWorldPlayerProfile).filter(
            models.SpaceWorldPlayerProfile.world_id == world.id,
            models.SpaceWorldPlayerProfile.user_id == user.id,
        ).first()
        profile_created_at = profile.created_at if profile is not None else now
        if profile_created_at.tzinfo is None:
            profile_created_at = profile_created_at.replace(tzinfo=datetime.timezone.utc)
        if now - profile_created_at <= datetime.timedelta(seconds=SPACE_TERRAIN_POSITION_GRACE_SECONDS):
            return
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TERRAIN_PLAYER_POSITION_REQUIRED",
                "message": "A recent player position is required before editing terrain.",
                "retryable": True,
            },
            headers={"Retry-After": "2"},
        )
    if snapshot is not None and snapshot.updated_at is not None:
        updated_at = snapshot.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
        if now - updated_at > datetime.timedelta(seconds=SPACE_REALTIME_POSITION_MAX_AGE_SECONDS):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TERRAIN_PLAYER_POSITION_STALE",
                    "message": "The saved player position is stale; reconnect before editing terrain.",
                    "retryable": True,
                },
                headers={"Retry-After": "2"},
            )
    center_chunk_x = position["x_cm"] // (SPACE_CHUNK_SIZE * 100)
    center_chunk_z = position["z_cm"] // (SPACE_CHUNK_SIZE * 100)
    for chunk_x, chunk_z in touched_chunks:
        if (
            _wrapped_chunk_distance(chunk_x, center_chunk_x, int(world.width_chunks))
            > SPACE_TERRAIN_EDIT_RADIUS_CHUNKS
            or _wrapped_chunk_distance(chunk_z, center_chunk_z, int(world.length_chunks))
            > SPACE_TERRAIN_EDIT_RADIUS_CHUNKS
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "TERRAIN_EDIT_OUT_OF_RANGE",
                    "message": "Terrain may only be edited near the player's current position.",
                    "radius_chunks": SPACE_TERRAIN_EDIT_RADIUS_CHUNKS,
                },
            )


SPACE_REALTIME_POSITION_MAX_AGE_SECONDS = 30


def _terrain_quota_response(
    db: Session,
    user_id: str,
    now: datetime.datetime,
) -> dict[str, int | str]:
    used_today = quota_usage(
        db,
        principal_id=user_id,
        scope_id=SPACE_TERRAIN_USAGE_SCOPE,
        metric="terrain_effective_changes",
        window_seconds=UTC_DAY_SECONDS,
        now=now,
    )
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "daily_limit": SPACE_TERRAIN_DAILY_LIMIT,
        "used_today": used_today,
        "remaining_today": max(0, SPACE_TERRAIN_DAILY_LIMIT - used_today),
        "reset_at": (day_start + datetime.timedelta(days=1)).isoformat(),
    }


def _get_or_create_default_world(db: Session) -> models.SpaceWorld:
    world = db.query(models.SpaceWorld).filter(
        models.SpaceWorld.id == settings.SPACE_DEFAULT_WORLD_ID
    ).first()
    if world:
        return world

    world = models.SpaceWorld(
        id=settings.SPACE_DEFAULT_WORLD_ID,
        owner_user_id=None,
        name="EntropyDrop Space",
        seed=settings.SPACE_WORLD_SEED,
        max_online_players=32,
    )
    db.add(world)
    try:
        db.commit()
        db.refresh(world)
        return world
    except IntegrityError:
        # Another API replica may have created the singleton concurrently.
        db.rollback()
        existing = db.query(models.SpaceWorld).filter(
            models.SpaceWorld.id == settings.SPACE_DEFAULT_WORLD_ID
        ).first()
        if existing is None:
            raise
        return existing


def _world_terrain_revision(db: Session, world: models.SpaceWorld) -> int:
    stream = db.query(models.SpaceWorldEventStream).filter(
        models.SpaceWorldEventStream.world_id == world.id,
    ).first()
    return int(stream.last_event_id or 0) if stream is not None else 0


def _random_initial_position(world: models.SpaceWorld) -> dict[str, int]:
    # X/Z are uniform over the complete wrapped world, including positions near
    # either seam. The authoritative worker can later refine the exact landing
    # surface; 32 m starts above the current procedural terrain ceiling.
    width_cm = world.width_chunks * 16 * 100
    length_cm = world.length_chunks * 16 * 100
    x = secrets.randbelow(max(1, width_cm))
    z = secrets.randbelow(max(1, length_cm))
    yaw = secrets.randbelow(65535) - 32767
    return {"x_cm": x, "y_cm": 3200, "z_cm": z, "yaw_q15": yaw}


def _get_or_create_player_profile(
    db: Session,
    world: models.SpaceWorld,
    user: models.User,
) -> models.SpaceWorldPlayerProfile:
    profile = db.query(models.SpaceWorldPlayerProfile).filter(
        models.SpaceWorldPlayerProfile.world_id == world.id,
        models.SpaceWorldPlayerProfile.user_id == user.id,
    ).first()
    if profile:
        return profile

    profile = models.SpaceWorldPlayerProfile(
        world_id=world.id,
        user_id=user.id,
        player_entity_id=str(uuid.uuid4()),
    )
    db.add(profile)
    try:
        db.commit()
        db.refresh(profile)
        return profile
    except IntegrityError:
        # The composite PK makes concurrent first entries choose one durable row.
        db.rollback()
        existing = db.query(models.SpaceWorldPlayerProfile).filter(
            models.SpaceWorldPlayerProfile.world_id == world.id,
            models.SpaceWorldPlayerProfile.user_id == user.id,
        ).first()
        if existing is None:
            raise
        return existing


@router.get("/ping")
@limiter.exempt
def ping_space():
    return {"status": "ok"}


@router.get("/status", response_model=SpacePublicStatusResponse)
@limiter.limit(SPACE_PUBLIC_STATUS_RATE_LIMIT)
def get_space_public_status(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Expose only aggregate recent presence for the public Space landing page."""
    now = datetime.datetime.now(datetime.timezone.utc)
    world = db.query(models.SpaceWorld).filter(
        models.SpaceWorld.id == settings.SPACE_DEFAULT_WORLD_ID,
    ).first()
    max_online_players = max(1, min(32, int(world.max_online_players))) if world else 32
    online_players = 0
    if world is not None:
        cutoff = now - datetime.timedelta(seconds=SPACE_ONLINE_PRESENCE_SECONDS)
        online_players = int(db.query(func.count(models.SpacePlayerSnapshot.user_id)).filter(
            models.SpacePlayerSnapshot.world_id == world.id,
            models.SpacePlayerSnapshot.updated_at >= cutoff,
        ).scalar() or 0)
        online_players = min(max_online_players, online_players)
    response.headers["Cache-Control"] = "public, max-age=5, stale-while-revalidate=10"
    return {
        "world_id": str(world.id) if world is not None else settings.SPACE_DEFAULT_WORLD_ID,
        "online_players": online_players,
        "max_online_players": max_online_players,
        "presence_window_seconds": SPACE_ONLINE_PRESENCE_SECONDS,
        "updated_at": now,
    }


@router.post("/bootstrap", response_model=SpaceBootstrapResponse)
@limiter.limit(SPACE_HIGH_FREQ_RATE_LIMIT)
def bootstrap_space(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Return the latest saved pose or an ephemeral world-wide random start."""
    skin_url = (current_user.skin_url or "").strip()
    world = _get_or_create_default_world(db)
    profile = _get_or_create_player_profile(db, world, current_user)
    snapshot = db.query(models.SpacePlayerSnapshot).filter(
        models.SpacePlayerSnapshot.world_id == world.id,
        models.SpacePlayerSnapshot.user_id == current_user.id,
    ).first()
    saved_position = _decode_player_snapshot(snapshot, world)
    start_position = saved_position or _random_initial_position(world)
    skin_type = (
        "slim"
        if skin_url and (current_user.skin_type or "").lower() == "slim"
        else "strong"
    )

    return {
        "protocol_version": 2,
        "max_online_players": min(32, world.max_online_players),
        "queue_enabled": True,
        "websocket_url": settings.SPACE_WS_URL,
        "world": {
            "id": str(world.id),
            "name": world.name,
            "seed": world.seed,
            "terrain_generator_version": world.terrain_generator_version,
            "terrain_revision": _world_terrain_revision(db, world),
            "surface_snapshot_url": f"/space/api/v2/worlds/{world.id}/surface-zones",
        },
        "player": {
            "user_id": current_user.id,
            "username": current_user.username,
            "is_admin": current_user.is_admin,
            "player_entity_id": str(profile.player_entity_id),
            "skin_url": skin_url or None,
            "skin_type": skin_type,
            "start_x_cm": start_position["x_cm"],
            "start_y_cm": start_position["y_cm"],
            "start_z_cm": start_position["z_cm"],
            "start_yaw_q15": start_position["yaw_q15"],
            "resumed": saved_position is not None,
        },
    }


@router.put("/worlds/{world_id}/players/me/position")
@router.post("/worlds/{world_id}/players/me/position")
@limiter.limit(SPACE_POSITION_RATE_LIMIT)
def update_player_position(
    request: Request,
    world_id: uuid.UUID,
    position_request: PlayerPositionUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Persist the authenticated player's latest small reconnect checkpoint."""
    world = _require_world_membership(db, str(world_id), current_user)
    position = _validate_player_position(
        world,
        position_request.x_cm,
        position_request.y_cm,
        position_request.z_cm,
        position_request.yaw_q15,
        position_request.pitch_q15,
    )
    encoded = _encode_player_snapshot(position)
    snapshot = db.query(models.SpacePlayerSnapshot).filter(
        models.SpacePlayerSnapshot.world_id == world.id,
        models.SpacePlayerSnapshot.user_id == current_user.id,
    ).with_for_update().first()
    if snapshot is None:
        snapshot = models.SpacePlayerSnapshot(
            world_id=world.id,
            user_id=current_user.id,
            revision=1,
            last_event_id=0,
            state_version=1,
            state=encoded,
        )
        db.add(snapshot)
    else:
        snapshot.revision = int(snapshot.revision or 0) + 1
        snapshot.state_version = 1
        snapshot.state = encoded
    try:
        db.commit()
    except IntegrityError:
        # Several lifecycle events (hidden/pagehide/beforeunload) may race on a
        # player's very first checkpoint. One insert wins; update that durable
        # row instead of turning a harmless duplicate insert into a 500.
        db.rollback()
        snapshot = db.query(models.SpacePlayerSnapshot).filter(
            models.SpacePlayerSnapshot.world_id == world.id,
            models.SpacePlayerSnapshot.user_id == current_user.id,
        ).with_for_update().first()
        if snapshot is None:
            raise
        snapshot.revision = int(snapshot.revision or 0) + 1
        snapshot.state_version = 1
        snapshot.state = encoded
        db.commit()
    return {
        "world_id": str(world.id),
        "revision": snapshot.revision,
        **position,
    }


@router.post("/worlds/{world_id}/heartbeat")
@limiter.exempt
def space_heartbeat(
    request: Request,
    world_id: uuid.UUID,
    heartbeat_req: SpaceHeartbeatRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Update caller position and return other active players + latest terrain chunk changes."""
    world = _require_world_membership(db, str(world_id), current_user)

    # 1. Update self position if provided
    if (
        heartbeat_req.x_cm is not None
        and heartbeat_req.y_cm is not None
        and heartbeat_req.z_cm is not None
        and heartbeat_req.yaw_q15 is not None
    ):
        position = _validate_player_position(
            world,
            heartbeat_req.x_cm,
            heartbeat_req.y_cm,
            heartbeat_req.z_cm,
            heartbeat_req.yaw_q15,
            heartbeat_req.pitch_q15 or 0,
        )
        encoded = _encode_player_snapshot(position)
        snapshot = db.query(models.SpacePlayerSnapshot).filter(
            models.SpacePlayerSnapshot.world_id == world.id,
            models.SpacePlayerSnapshot.user_id == current_user.id,
        ).with_for_update().first()
        if snapshot is None:
            snapshot = models.SpacePlayerSnapshot(
                world_id=world.id,
                user_id=current_user.id,
                revision=1,
                last_event_id=0,
                state_version=1,
                state=encoded,
            )
            db.add(snapshot)
        else:
            snapshot.revision = int(snapshot.revision or 0) + 1
            snapshot.state_version = 1
            snapshot.state = encoded
            snapshot.updated_at = datetime.datetime.now(datetime.timezone.utc)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            snapshot = db.query(models.SpacePlayerSnapshot).filter(
                models.SpacePlayerSnapshot.world_id == world.id,
                models.SpacePlayerSnapshot.user_id == current_user.id,
            ).with_for_update().first()
            if snapshot is not None:
                snapshot.revision = int(snapshot.revision or 0) + 1
                snapshot.state_version = 1
                snapshot.state = encoded
                snapshot.updated_at = datetime.datetime.now(datetime.timezone.utc)
                db.commit()

    players = []
    if heartbeat_req.include_players:
        # REST compatibility fallback only. The primary player-state path is the
        # 10 Hz WebSocket snapshot stream and does not scan PostgreSQL per frame.
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
        active_snapshots = db.query(
            models.SpacePlayerSnapshot,
            models.User,
            models.SpaceWorldPlayerProfile.player_entity_id,
        ).join(
            models.User, models.User.id == models.SpacePlayerSnapshot.user_id
        ).outerjoin(
            models.SpaceWorldPlayerProfile,
            and_(
                models.SpaceWorldPlayerProfile.world_id == models.SpacePlayerSnapshot.world_id,
                models.SpaceWorldPlayerProfile.user_id == models.SpacePlayerSnapshot.user_id,
            )
        ).filter(
            models.SpacePlayerSnapshot.world_id == world.id,
            models.SpacePlayerSnapshot.updated_at >= cutoff,
        ).all()

        for snap, user, entity_id in active_snapshots:
            pos = _decode_player_snapshot(snap, world)
            if not pos:
                continue
            skin_url = (user.skin_url or "").strip() or "/skin/default.png"
            yaw_rad = (pos["yaw_q15"] / 32767.0) * math.pi
            pitch_rad = (pos.get("pitch_q15", 0) / 32767.0) * math.pi
            skin_type = "slim" if (user.skin_type or "").lower() == "slim" else "strong"
            players.append({
                "user_id": user.id,
                "username": user.username or f"Player-{user.id[:6]}",
                "player_entity_id": str(entity_id or user.id),
                "skin_url": skin_url,
                "skin_type": skin_type,
                "x": pos["x_cm"] / 100.0,
                "y": pos["y_cm"] / 100.0,
                "z": pos["z_cm"] / 100.0,
                "yaw": yaw_rad,
                "pitch": pitch_rad,
                "is_self": user.id == current_user.id,
                "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
            })

    # 3. Query modified terrain chunks since requested revision
    modified_chunks = []
    max_revision = int(heartbeat_req.since_terrain_revision or 0)
    terrain_aoi_filters = _chunk_aoi_filters(
        world,
        heartbeat_req.center_chunk_x,
        heartbeat_req.center_chunk_z,
        heartbeat_req.terrain_radius_chunks,
    )
    # Chunk revision is local to one chunk and therefore cannot be a world
    # cursor. Page complete event ids instead, so a first edit in a different
    # chunk is never hidden just because both chunks happen to be revision 1.
    event_rows = db.query(models.SpaceChunkSnapshot.last_event_id).filter(
        models.SpaceChunkSnapshot.world_id == world.id,
        models.SpaceChunkSnapshot.last_event_id > heartbeat_req.since_terrain_revision,
        *terrain_aoi_filters,
    ).distinct().order_by(models.SpaceChunkSnapshot.last_event_id.asc()).limit(16).all()
    event_ids = [int(row[0]) for row in event_rows]
    if event_ids:
        chunk_rows = db.query(models.SpaceChunkSnapshot).filter(
            models.SpaceChunkSnapshot.world_id == world.id,
            models.SpaceChunkSnapshot.last_event_id.in_(event_ids),
            *terrain_aoi_filters,
        ).order_by(
            models.SpaceChunkSnapshot.last_event_id.asc(),
            models.SpaceChunkSnapshot.chunk_x.asc(),
            models.SpaceChunkSnapshot.chunk_z.asc(),
        ).all()
        max_revision = event_ids[-1]
        for row in chunk_rows:
            overlay = _decode_chunk_overlay(row)
            modified_chunks.append({
                "chunk_x": row.chunk_x,
                "chunk_z": row.chunk_z,
                "revision": row.revision,
                "terrain_revision": row.last_event_id,
                "standard": overlay["standard"],
                "micro": overlay["micro"],
            })
    return {
        "world_id": str(world.id),
        "players": players,
        "terrain_chunks": modified_chunks,
        "max_terrain_revision": max_revision,
    }


@router.get("/worlds/{world_id}/players")
@limiter.limit(SPACE_HIGH_FREQ_RATE_LIMIT)
def list_world_players(
    request: Request,
    world_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Return active online players in the specified world."""
    world = _require_world_membership(db, str(world_id), current_user)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
    active_snapshots = db.query(
        models.SpacePlayerSnapshot,
        models.User,
        models.SpaceWorldPlayerProfile.player_entity_id,
    ).join(
        models.User, models.User.id == models.SpacePlayerSnapshot.user_id
    ).outerjoin(
        models.SpaceWorldPlayerProfile,
        and_(
            models.SpaceWorldPlayerProfile.world_id == models.SpacePlayerSnapshot.world_id,
            models.SpaceWorldPlayerProfile.user_id == models.SpacePlayerSnapshot.user_id,
        )
    ).filter(
        models.SpacePlayerSnapshot.world_id == world.id,
        models.SpacePlayerSnapshot.updated_at >= cutoff,
    ).all()

    players = []
    for snap, user, entity_id in active_snapshots:
        pos = _decode_player_snapshot(snap, world)
        if not pos:
            continue
        skin_url = (user.skin_url or "").strip() or "/skin/default.png"
        yaw_rad = (pos["yaw_q15"] / 32767.0) * math.pi
        pitch_rad = (pos.get("pitch_q15", 0) / 32767.0) * math.pi
        skin_type = "slim" if (user.skin_type or "").lower() == "slim" else "strong"
        players.append({
            "user_id": user.id,
            "username": user.username or f"Player-{user.id[:6]}",
            "player_entity_id": str(entity_id or user.id),
            "skin_url": skin_url,
            "skin_type": skin_type,
            "x": pos["x_cm"] / 100.0,
            "y": pos["y_cm"] / 100.0,
            "z": pos["z_cm"] / 100.0,
            "yaw": yaw_rad,
            "pitch": pitch_rad,
            "is_self": user.id == current_user.id,
            "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
        })
    return {"world_id": str(world.id), "players": players}


@router.get("/worlds/{world_id}/terrain-edits")
@limiter.limit(SPACE_HIGH_FREQ_RATE_LIMIT)
def list_terrain_edits(
    request: Request,
    world_id: uuid.UUID,
    cursor: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=MAX_SNAPSHOT_PAGE_SIZE, ge=1, le=MAX_SNAPSHOT_PAGE_SIZE),
    center_chunk_x: int | None = Query(default=None),
    center_chunk_z: int | None = Query(default=None),
    radius_chunks: int | None = Query(default=None, ge=1, le=MAX_TERRAIN_AOI_RADIUS_CHUNKS),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Return durable authored chunk overlays in stable, paginated chunk order."""
    world = _require_world_membership(db, str(world_id), current_user)
    parsed_cursor = _parse_snapshot_cursor(cursor)
    aoi_filters = _chunk_aoi_filters(
        world,
        center_chunk_x,
        center_chunk_z,
        radius_chunks,
    )
    query = db.query(models.SpaceChunkSnapshot).filter(
        models.SpaceChunkSnapshot.world_id == world.id,
        *aoi_filters,
    )
    if parsed_cursor is not None:
        cursor_x, cursor_z = parsed_cursor
        query = query.filter(or_(
            models.SpaceChunkSnapshot.chunk_x > cursor_x,
            and_(
                models.SpaceChunkSnapshot.chunk_x == cursor_x,
                models.SpaceChunkSnapshot.chunk_z > cursor_z,
            ),
        ))
    candidate_rows = query.order_by(
        models.SpaceChunkSnapshot.chunk_x,
        models.SpaceChunkSnapshot.chunk_z,
    ).limit(limit + 1).all()
    rows = []
    response_bytes = 0
    for row in candidate_rows[:limit]:
        row_bytes = max(0, int(row.uncompressed_size or 0))
        if not rows and row_bytes > SPACE_TERRAIN_MAX_RESPONSE_BYTES:
            raise HTTPException(status_code=413, detail={
                "code": "TERRAIN_CHUNK_SNAPSHOT_TOO_LARGE",
                "message": "A stored terrain chunk exceeds the response-size limit.",
                "limit_bytes": SPACE_TERRAIN_MAX_RESPONSE_BYTES,
                "actual_bytes": row_bytes,
                "chunk_x": row.chunk_x,
                "chunk_z": row.chunk_z,
            })
        if rows and response_bytes + row_bytes > SPACE_TERRAIN_MAX_RESPONSE_BYTES:
            break
        rows.append(row)
        response_bytes += row_bytes
    has_more = len(rows) < len(candidate_rows)
    chunks = []
    for row in rows:
        overlay = _decode_chunk_overlay(row)
        chunks.append({
            "chunk_x": row.chunk_x,
            "chunk_z": row.chunk_z,
            "revision": row.revision,
            "standard": overlay["standard"],
            "micro": overlay["micro"],
        })
    next_cursor = None
    if has_more and rows:
        next_cursor = f"{rows[-1].chunk_x},{rows[-1].chunk_z}"
    return {"world_id": str(world.id), "chunks": chunks, "next_cursor": next_cursor}


@router.get("/worlds/{world_id}/surface-zones")
@limiter.limit(SPACE_HIGH_FREQ_RATE_LIMIT)
def list_surface_zones(
    request: Request,
    world_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Return immutable URLs for every ready, revisioned far-surface zone."""
    world = _require_world_membership(db, str(world_id), current_user)
    rows = db.query(models.SpaceSurfaceZoneSnapshot).filter(
        models.SpaceSurfaceZoneSnapshot.world_id == world.id,
        models.SpaceSurfaceZoneSnapshot.dirty.is_(False),
        models.SpaceSurfaceZoneSnapshot.terrain_generator_version == world.terrain_generator_version,
        models.SpaceSurfaceZoneSnapshot.schema_version == space_surface.SURFACE_SCHEMA_VERSION,
        models.SpaceSurfaceZoneSnapshot.samples_per_chunk_axis
        == space_surface.SURFACE_SAMPLES_PER_CHUNK_AXIS,
    ).order_by(
        models.SpaceSurfaceZoneSnapshot.zone_x,
        models.SpaceSurfaceZoneSnapshot.zone_z,
    ).all()
    expected = (
        int(world.width_chunks) // int(world.zone_size_chunks)
        * (int(world.length_chunks) // int(world.zone_size_chunks))
    )
    if len(rows) < expected and db.get_bind().dialect.name == "postgresql":
        # Production normally has the Redis-singleton background process. This
        # makes API-only local deployments and temporarily missing workers
        # self-heal without delaying the manifest response.
        space_surface.ensure_surface_generation_started()
    zones = []
    for row in rows:
        digest = bytes(row.content_hash).hex()
        zones.append({
            "zone_x": int(row.zone_x),
            "zone_z": int(row.zone_z),
            "revision": int(row.revision),
            "source_terrain_revision": int(row.source_terrain_revision),
            "digest": digest,
            "byte_length": int(row.uncompressed_size),
            "url": (
                f"/space/api/v2/worlds/{world.id}/surface-zones/"
                f"{row.zone_x}/{row.zone_z}?digest={digest}"
            ),
        })
    return {
        "schema_version": space_surface.SURFACE_SCHEMA_VERSION,
        "samples_per_chunk_axis": space_surface.SURFACE_SAMPLES_PER_CHUNK_AXIS,
        "zone_size_chunks": int(world.zone_size_chunks),
        "width_chunks": int(world.width_chunks),
        "length_chunks": int(world.length_chunks),
        "complete": len(zones) == expected,
        "zones": zones,
    }


@router.get("/worlds/{world_id}/surface-zones/{zone_x}/{zone_z}")
@limiter.limit(SPACE_HIGH_FREQ_RATE_LIMIT)
def get_surface_zone(
    request: Request,
    world_id: uuid.UUID,
    zone_x: int,
    zone_z: int,
    digest: str | None = Query(default=None, min_length=64, max_length=64),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Return one validated raw EDSZ payload; HTTP compression handles transfer size."""
    world = _require_world_membership(db, str(world_id), current_user)
    max_zone_x = int(world.width_chunks) // int(world.zone_size_chunks)
    max_zone_z = int(world.length_chunks) // int(world.zone_size_chunks)
    if not (0 <= zone_x < max_zone_x and 0 <= zone_z < max_zone_z):
        raise HTTPException(status_code=404, detail={"code": "SURFACE_ZONE_NOT_FOUND"})
    row = db.query(models.SpaceSurfaceZoneSnapshot).filter(
        models.SpaceSurfaceZoneSnapshot.world_id == world.id,
        models.SpaceSurfaceZoneSnapshot.zone_x == zone_x,
        models.SpaceSurfaceZoneSnapshot.zone_z == zone_z,
        models.SpaceSurfaceZoneSnapshot.dirty.is_(False),
        models.SpaceSurfaceZoneSnapshot.terrain_generator_version == world.terrain_generator_version,
        models.SpaceSurfaceZoneSnapshot.schema_version == space_surface.SURFACE_SCHEMA_VERSION,
        models.SpaceSurfaceZoneSnapshot.samples_per_chunk_axis
        == space_surface.SURFACE_SAMPLES_PER_CHUNK_AXIS,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "SURFACE_ZONE_NOT_READY"})
    actual_digest = bytes(row.content_hash).hex()
    if digest is not None and not secrets.compare_digest(digest.lower(), actual_digest):
        raise HTTPException(status_code=409, detail={"code": "SURFACE_ZONE_REVISION_CHANGED"})
    try:
        payload = space_surface.decode_surface_zone_row(row)
    except (ValueError, zstd.ZstdError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "CORRUPT_SURFACE_ZONE_SNAPSHOT"},
        ) from exc
    return Response(
        content=payload,
        media_type="application/vnd.entropydrop.surface-zone",
        headers={
            "ETag": f'"{actual_digest}"',
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/worlds/{world_id}/terrain-edits/batches")
@limiter.limit(SPACE_HIGH_FREQ_RATE_LIMIT)
def apply_terrain_mutation_batch(
    request: Request,
    world_id: uuid.UUID,
    batch_request: TerrainMutationBatchRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Apply one idempotent, spatially bounded and transactionally metered batch."""
    world = _require_world_membership(db, str(world_id), current_user)
    batch_id = str(batch_request.batch_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    receipt = db.query(models.SpaceTerrainMutationBatch).filter(
        models.SpaceTerrainMutationBatch.world_id == world.id,
        models.SpaceTerrainMutationBatch.batch_id == batch_id,
    ).first()
    if receipt is not None:
        return _terrain_receipt_response(world.id, batch_id, receipt.result)

    client_created_at = _terrain_batch_client_created_at(batch_request, now)
    _maybe_cleanup_terrain_receipts(db, now)

    normalized: list[tuple] = []
    touched_chunks: set[tuple[int, int]] = set()
    for mutation in batch_request.mutations:
        if mutation.kind == "set_standard":
            x, y, z = _standard_cell(mutation, world)
            block = mutation.block
            color = mutation.color
            if block not in (0, 1) or color is None or not (0 <= color <= 0xFFFFFF):
                raise HTTPException(status_code=422, detail={"code": "INVALID_TERRAIN_MUTATION"})
            chunk = _chunk_for_standard(x, z)
            normalized.append((mutation.kind, chunk, x, y, z, block, color))
        elif mutation.kind == "set_micro":
            mx, my, mz = _micro_cell(mutation, world)
            color = mutation.color
            if color is None or not (0 <= color <= 0xFFFFFF):
                raise HTTPException(status_code=422, detail={"code": "INVALID_TERRAIN_MUTATION"})
            chunk = _chunk_for_micro(mx, mz)
            normalized.append((mutation.kind, chunk, mx, my, mz, color, mutation.part))
        elif mutation.kind == "remove_micro":
            mx, my, mz = _micro_cell(mutation, world)
            chunk = _chunk_for_micro(mx, mz)
            normalized.append((mutation.kind, chunk, mx, my, mz))
        else:
            x, y, z = _standard_cell(mutation, world)
            chunk = _chunk_for_standard(x, z)
            normalized.append((mutation.kind, chunk, x, y, z))
        touched_chunks.add(chunk)

    if len(touched_chunks) > SPACE_TERRAIN_MAX_CHUNKS_PER_BATCH:
        raise HTTPException(status_code=413, detail={
            "code": "TERRAIN_BATCH_TOO_MANY_CHUNKS",
            "message": "A terrain batch touches too many chunks.",
            "limit": SPACE_TERRAIN_MAX_CHUNKS_PER_BATCH,
            "actual": len(touched_chunks),
        })
    touched_zones = {
        (chunk_x // int(world.zone_size_chunks), chunk_z // int(world.zone_size_chunks))
        for chunk_x, chunk_z in touched_chunks
    }
    if len(touched_zones) > SPACE_TERRAIN_MAX_ZONES_PER_BATCH:
        raise HTTPException(status_code=413, detail={
            "code": "TERRAIN_BATCH_TOO_MANY_ZONES",
            "message": "A terrain batch touches too many surface zones.",
            "limit": SPACE_TERRAIN_MAX_ZONES_PER_BATCH,
            "actual": len(touched_zones),
        })
    _validate_terrain_edit_scope(db, world, current_user, touched_chunks, now)

    rows = db.query(models.SpaceChunkSnapshot).filter(
        models.SpaceChunkSnapshot.world_id == world.id,
        or_(*[
            and_(
                models.SpaceChunkSnapshot.chunk_x == chunk_x,
                models.SpaceChunkSnapshot.chunk_z == chunk_z,
            )
            for chunk_x, chunk_z in touched_chunks
        ]),
    ).order_by(
        models.SpaceChunkSnapshot.chunk_x,
        models.SpaceChunkSnapshot.chunk_z,
    ).with_for_update().all()
    row_by_chunk = {(row.chunk_x, row.chunk_z): row for row in rows}
    state_by_chunk: dict[tuple[int, int], tuple[models.SpaceChunkSnapshot, dict, dict]] = {}
    empty_encoded, empty_hash, empty_codec, empty_size = _encode_chunk_overlay(_empty_chunk_overlay())
    for chunk in touched_chunks:
        row = row_by_chunk.get(chunk) or models.SpaceChunkSnapshot(
            world_id=world.id,
            chunk_x=chunk[0],
            chunk_z=chunk[1],
            revision=0,
            last_event_id=0,
            codec=empty_codec,
            codec_version=1,
            uncompressed_size=empty_size,
            content_hash=empty_hash,
            payload=empty_encoded,
        )
        standard, micro = _overlay_maps(_decode_chunk_overlay(row))
        state_by_chunk[chunk] = row, standard, micro

    effective_changes = 0
    for mutation in normalized:
        kind, chunk, *values = mutation
        _, standard, micro = state_by_chunk[chunk]
        if kind == "set_standard":
            x, y, z, block, color = values
            key = f"{x},{y},{z}"
            packed = [x, y, z, block, color]
            if standard.get(key) != packed:
                standard[key] = packed
                effective_changes += 1
            if block != 0:
                effective_changes += _clear_micro_parent(micro, x, y, z)
        elif kind == "set_micro":
            mx, my, mz, color, part = values
            parent_key = (
                f"{mx // SPACE_MICRO_DIVISIONS},"
                f"{my // SPACE_MICRO_DIVISIONS},"
                f"{mz // SPACE_MICRO_DIVISIONS}"
            )
            if standard.get(parent_key, [None, None, None, 0])[3] != 0:
                raise HTTPException(status_code=409, detail={"code": "STANDARD_CELL_OCCUPIED"})
            packed = [mx, my, mz, color]
            if part:
                packed.append(part)
            key = f"{mx},{my},{mz}"
            if micro.get(key) != packed:
                micro[key] = packed
                effective_changes += 1
        elif kind == "remove_micro":
            mx, my, mz = values
            if micro.pop(f"{mx},{my},{mz}", None) is not None:
                effective_changes += 1
        else:
            x, y, z = values
            effective_changes += _clear_micro_parent(micro, x, y, z)

    encoded_by_chunk: dict[tuple[int, int], tuple[bytes, bytes, int, int]] = {}
    changed_chunks: set[tuple[int, int]] = set()
    event_bytes = 0
    for chunk in sorted(touched_chunks):
        row, standard, micro = state_by_chunk[chunk]
        overlay = {
            "standard": sorted(standard.values(), key=lambda edit: (edit[0], edit[1], edit[2])),
            "micro": sorted(micro.values(), key=lambda edit: (edit[0], edit[1], edit[2])),
        }
        encoded = _encode_chunk_overlay(overlay)
        encoded_by_chunk[chunk] = encoded
        if encoded[1] != bytes(row.content_hash):
            changed_chunks.add(chunk)
            event_bytes += encoded[3]
    if event_bytes > SPACE_TERRAIN_MAX_EVENT_BYTES:
        raise HTTPException(status_code=413, detail={
            "code": "TERRAIN_EVENT_TOO_LARGE",
            "message": "The resulting terrain update is too large for one resumable event.",
            "limit_bytes": SPACE_TERRAIN_MAX_EVENT_BYTES,
            "actual_bytes": event_bytes,
        })

    # Existing rows are serialized above; the user lock serializes missing
    # usage buckets, while the stream lock serializes the global world budget.
    db.query(models.User).filter(models.User.id == current_user.id).with_for_update().first()
    stream = db.query(models.SpaceWorldEventStream).filter(
        models.SpaceWorldEventStream.world_id == world.id,
    ).with_for_update().first()
    if stream is None:
        stream = models.SpaceWorldEventStream(world_id=world.id, last_event_id=0)
        db.add(stream)
        db.flush()
    if not current_user.is_admin:
        try:
            reserve_quota(
                db,
                principal_id=current_user.id,
                scope_id=SPACE_TERRAIN_USAGE_SCOPE,
                metric="terrain_submitted_mutations",
                amount=len(normalized),
                windows=(QuotaWindow(
                    SPACE_TERRAIN_BURST_WINDOW_SECONDS,
                    SPACE_TERRAIN_BURST_LIMIT,
                    "10_seconds",
                ),),
                code="TERRAIN_BURST_QUOTA_REACHED",
                message="Too many terrain edits were submitted at once.",
                now=now,
            )
            reserve_quota(
                db,
                principal_id=current_user.id,
                scope_id=SPACE_TERRAIN_USAGE_SCOPE,
                metric="terrain_effective_changes",
                amount=effective_changes,
                windows=(
                    QuotaWindow(3_600, SPACE_TERRAIN_HOURLY_LIMIT, "hour"),
                    QuotaWindow(UTC_DAY_SECONDS, SPACE_TERRAIN_DAILY_LIMIT, "utc_day"),
                ),
                code="TERRAIN_EDIT_QUOTA_REACHED",
                message="The terrain edit allowance for this period has been reached.",
                now=now,
            )
            reserve_quota(
                db,
                principal_id="world",
                scope_id=str(world.id),
                metric="terrain_submitted_mutations",
                amount=len(normalized),
                windows=(QuotaWindow(1, SPACE_TERRAIN_WORLD_SECOND_LIMIT, "second"),),
                code="TERRAIN_WORLD_BUSY",
                message="The world is receiving too many terrain edits; retry shortly.",
                now=now,
            )
        except HTTPException:
            db.rollback()
            raise

    terrain_revision = int(stream.last_event_id or 0)
    revisions = []
    if changed_chunks:
        stream.last_event_id = terrain_revision + 1
        terrain_revision = stream.last_event_id
        for chunk in sorted(changed_chunks):
            row, _standard, _micro = state_by_chunk[chunk]
            encoded, content_hash, codec, uncompressed_size = encoded_by_chunk[chunk]
            if chunk not in row_by_chunk:
                db.add(row)
            row.revision = int(row.revision or 0) + 1
            row.last_event_id = terrain_revision
            row.codec = codec
            row.codec_version = 1
            row.uncompressed_size = uncompressed_size
            row.content_hash = content_hash
            row.payload = encoded
            revisions.append({"chunk_x": chunk[0], "chunk_z": chunk[1], "revision": row.revision})

        changed_zones = {
            (chunk_x // int(world.zone_size_chunks), chunk_z // int(world.zone_size_chunks))
            for chunk_x, chunk_z in changed_chunks
        }
        surface_rows = db.query(models.SpaceSurfaceZoneSnapshot).filter(
            models.SpaceSurfaceZoneSnapshot.world_id == world.id,
            or_(*[
                and_(
                    models.SpaceSurfaceZoneSnapshot.zone_x == zone_x,
                    models.SpaceSurfaceZoneSnapshot.zone_z == zone_z,
                )
                for zone_x, zone_z in changed_zones
            ]),
        ).with_for_update().all()
        for surface_row in surface_rows:
            surface_row.dirty = True

    stored_result = {
        "applied": len(batch_request.mutations),
        "effective_changes": effective_changes,
        "terrain_revision": terrain_revision,
        "chunks": revisions,
        "quota": _terrain_quota_response(db, current_user.id, now),
    }
    db.add(models.SpaceTerrainMutationBatch(
        world_id=world.id,
        batch_id=batch_id,
        actor_user_id=current_user.id,
        dedupe_epoch=batch_request.dedupe_epoch,
        client_created_at=client_created_at,
        result=stored_result,
    ))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        duplicate = db.query(models.SpaceTerrainMutationBatch).filter(
            models.SpaceTerrainMutationBatch.world_id == world.id,
            models.SpaceTerrainMutationBatch.batch_id == batch_id,
        ).first()
        if duplicate is not None:
            return _terrain_receipt_response(world.id, batch_id, duplicate.result)
        raise HTTPException(
            status_code=409,
            detail={"code": "TERRAIN_BATCH_RETRY", "message": "World state is updating, please retry the batch."},
        ) from exc
    if changed_chunks:
        # Wake connected clients only for an actual durable world revision.
        from routers.space_realtime import realtime_hub
        realtime_hub.notify_terrain_from_thread(str(world.id), terrain_revision)
    return _terrain_receipt_response(world.id, batch_id, stored_result)
