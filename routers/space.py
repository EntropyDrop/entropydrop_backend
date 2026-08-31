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
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth
import models
from config import settings
from database import get_db
from rate_limit import limiter


router = APIRouter(prefix="/space/api/v2", tags=["space"])

# 20x ordinary API rate limits (ordinary is 60/min, 1000/hr, 4000/day)
SPACE_HIGH_FREQ_RATE_LIMIT = "1200/minute; 20000/hour; 80000/day"
# Reconnect checkpoints may run for an entire long-lived play session. Keep
# burst/hour protection, but do not turn normal continuous play into a daily 429.
SPACE_POSITION_RATE_LIMIT = "1200/minute; 20000/hour"

SPACE_CHUNK_SIZE = 16
SPACE_WORLD_HEIGHT = 128
SPACE_MICRO_DIVISIONS = 5
MAX_TERRAIN_MUTATIONS_PER_BATCH = 256
MAX_CHUNK_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_PAGE_SIZE = 256
MAX_TERRAIN_AOI_RADIUS_CHUNKS = 64
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


class SpacePlayerResponse(BaseModel):
    user_id: str
    username: str | None
    is_admin: bool
    player_entity_id: str
    skin_url: str
    skin_type: str
    start_x_cm: int | None = None
    start_y_cm: int | None = None
    start_z_cm: int | None = None
    start_yaw_q15: int | None = None
    resumed: bool


class SpaceBootstrapResponse(BaseModel):
    protocol_version: int
    max_online_players: int
    queue_enabled: bool
    websocket_url: str
    world: SpaceWorldResponse
    player: SpacePlayerResponse


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
            detail={"code": "CORRUPT_CHUNK_SNAPSHOT", "message": "世界区块快照校验失败。"},
        ) from exc
    if (
        snapshot.uncompressed_size != len(encoded)
        or snapshot.content_hash != hashlib.sha256(encoded).digest()
    ):
        raise HTTPException(
            status_code=500,
            detail={"code": "CORRUPT_CHUNK_SNAPSHOT", "message": "世界区块快照校验失败。"},
        )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "CORRUPT_CHUNK_SNAPSHOT", "message": "世界区块快照损坏。"},
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
            detail={"code": "CHUNK_OVERLAY_TOO_LARGE", "message": "单个区块的方块修改数据过大。"},
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
        # A bad or future snapshot must not prevent login. The immutable birth
        # point remains the safe fallback.
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
            detail={"code": "TERRAIN_BATCH_EXPIRED", "message": "地形编辑批次已超过安全重试期限。"},
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


def _clear_micro_parent(micro: dict[str, list], x: int, y: int, z: int) -> None:
    base_x = x * SPACE_MICRO_DIVISIONS
    base_y = y * SPACE_MICRO_DIVISIONS
    base_z = z * SPACE_MICRO_DIVISIONS
    for dx in range(SPACE_MICRO_DIVISIONS):
        for dy in range(SPACE_MICRO_DIVISIONS):
            for dz in range(SPACE_MICRO_DIVISIONS):
                micro.pop(f"{base_x + dx},{base_y + dy},{base_z + dz}", None)


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
    # Keep the initial position one zone away from the coordinate boundary. The
    # authoritative world generator validates/refines safe ground when the
    # multiplayer worker is introduced; 32 m starts above current terrain.
    margin_cm = min(32 * 16 * 100, (world.width_chunks * 16 * 100) // 4)
    width_cm = world.width_chunks * 16 * 100
    length_cm = world.length_chunks * 16 * 100
    x = margin_cm + secrets.randbelow(max(1, width_cm - margin_cm * 2))
    z_margin = min(margin_cm, length_cm // 4)
    z = z_margin + secrets.randbelow(max(1, length_cm - z_margin * 2))
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


@router.post("/bootstrap", response_model=SpaceBootstrapResponse)
@limiter.limit(SPACE_HIGH_FREQ_RATE_LIMIT)
def bootstrap_space(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Gate Space entry and return the latest state or a one-time random start."""
    skin_url = (current_user.skin_url or "").strip()
    if not skin_url:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SKIN_REQUIRED",
                "message": "进入 Space 前需要先设置角色皮肤。",
                "action_url": "/skin/edit",
            },
        )

    world = _get_or_create_default_world(db)
    profile = _get_or_create_player_profile(db, world, current_user)
    snapshot = db.query(models.SpacePlayerSnapshot).filter(
        models.SpacePlayerSnapshot.world_id == world.id,
        models.SpacePlayerSnapshot.user_id == current_user.id,
    ).first()
    saved_position = _decode_player_snapshot(snapshot, world)
    skin_type = "slim" if (current_user.skin_type or "").lower() == "slim" else "strong"

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
        },
        "player": {
            "user_id": current_user.id,
            "username": current_user.username,
            "is_admin": current_user.is_admin,
            "player_entity_id": str(profile.player_entity_id),
            "skin_url": skin_url,
            "skin_type": skin_type,
            "start_x_cm": saved_position["x_cm"] if saved_position else None,
            "start_y_cm": saved_position["y_cm"] if saved_position else None,
            "start_z_cm": saved_position["z_cm"] if saved_position else None,
            "start_yaw_q15": saved_position["yaw_q15"] if saved_position else None,
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
    rows = query.order_by(
        models.SpaceChunkSnapshot.chunk_x,
        models.SpaceChunkSnapshot.chunk_z,
    ).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
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


@router.post("/worlds/{world_id}/terrain-edits/batches")
@limiter.limit(SPACE_HIGH_FREQ_RATE_LIMIT)
def apply_terrain_mutation_batch(
    request: Request,
    world_id: uuid.UUID,
    batch_request: TerrainMutationBatchRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Atomically apply at most 256 idempotent terrain mutations."""
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

    stream = db.query(models.SpaceWorldEventStream).filter(
        models.SpaceWorldEventStream.world_id == world.id,
    ).with_for_update().first()
    if stream is None:
        stream = models.SpaceWorldEventStream(world_id=world.id, last_event_id=0)
        db.add(stream)
        db.flush()
    stream.last_event_id = int(stream.last_event_id or 0) + 1
    terrain_revision = stream.last_event_id

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

    rows = db.query(models.SpaceChunkSnapshot).filter(
        models.SpaceChunkSnapshot.world_id == world.id,
        or_(*[
            and_(
                models.SpaceChunkSnapshot.chunk_x == chunk_x,
                models.SpaceChunkSnapshot.chunk_z == chunk_z,
            )
            for chunk_x, chunk_z in touched_chunks
        ]),
    ).with_for_update().all()
    row_by_chunk = {(row.chunk_x, row.chunk_z): row for row in rows}
    state_by_chunk: dict[tuple[int, int], tuple[models.SpaceChunkSnapshot, dict, dict]] = {}
    empty_encoded, empty_hash, empty_codec, empty_size = _encode_chunk_overlay(_empty_chunk_overlay())
    for chunk in touched_chunks:
        row = row_by_chunk.get(chunk)
        if row is None:
            row = models.SpaceChunkSnapshot(
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
            db.add(row)
        standard, micro = _overlay_maps(_decode_chunk_overlay(row))
        state_by_chunk[chunk] = row, standard, micro

    for mutation in normalized:
        kind, chunk, *values = mutation
        _, standard, micro = state_by_chunk[chunk]
        if kind == "set_standard":
            x, y, z, block, color = values
            standard[f"{x},{y},{z}"] = [x, y, z, block, color]
            if block != 0:
                _clear_micro_parent(micro, x, y, z)
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
            micro[f"{mx},{my},{mz}"] = packed
        elif kind == "remove_micro":
            mx, my, mz = values
            micro.pop(f"{mx},{my},{mz}", None)
        else:
            x, y, z = values
            _clear_micro_parent(micro, x, y, z)

    revisions = []
    for chunk in sorted(touched_chunks):
        row, standard, micro = state_by_chunk[chunk]
        overlay = {
            "standard": sorted(standard.values(), key=lambda edit: (edit[0], edit[1], edit[2])),
            "micro": sorted(micro.values(), key=lambda edit: (edit[0], edit[1], edit[2])),
        }
        encoded, content_hash, codec, uncompressed_size = _encode_chunk_overlay(overlay)
        row.revision = int(row.revision or 0) + 1
        row.last_event_id = terrain_revision
        row.codec = codec
        row.codec_version = 1
        row.uncompressed_size = uncompressed_size
        row.content_hash = content_hash
        row.payload = encoded
        revisions.append({"chunk_x": chunk[0], "chunk_z": chunk[1], "revision": row.revision})

    stored_result = {
        "applied": len(batch_request.mutations),
        "terrain_revision": terrain_revision,
        "chunks": revisions,
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
            detail={"code": "TERRAIN_BATCH_RETRY", "message": "世界正在更新，请重试同一批次。"},
        ) from exc
    # Wake connected clients immediately; the durable REST cursor remains the
    # source of truth and transports the potentially large chunk payload.
    from routers.space_realtime import realtime_hub
    realtime_hub.notify_terrain_from_thread(str(world.id), terrain_revision)
    return _terrain_receipt_response(world.id, batch_id, stored_result)
