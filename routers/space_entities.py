import datetime
import base64
import binascii
import hashlib
import json
import logging
import math
import secrets
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth
import models
import s3_utils
from database import get_db
from rate_limit import limiter
from routers.space import (
    MAX_PLAYER_Y_CM,
    MIN_PLAYER_Y_CM,
    SPACE_CHUNK_SIZE,
    _require_world_membership,
)
from routers.space_market import validate_market_payload
from space.inventory_codec import (
    InventoryCodecError,
    decode_inventory_resource,
    encode_inventory_resource,
    inventory_content_digest,
)


router = APIRouter(prefix="/space/api/v2/worlds/{world_id}/entities", tags=["space-entities"])
token_router = APIRouter(prefix="/space/api/v2/worlds/{world_id}/entity-create-tokens", tags=["space-entities"])
logger = logging.getLogger(__name__)
entity_security = HTTPBearer(auto_error=False)

SPACE_ENTITY_RATE_LIMIT = "120/minute; 2000/hour"
SPACE_ENTITY_CREATE_RATE_LIMIT = "30/minute; 300/hour"
SPACE_ENTITY_MAX_PER_OWNER = 256
SPACE_ENTITY_MAX_DEFINITION_BYTES = 8 * 1024 * 1024
SPACE_ENTITY_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
SPACE_ENTITY_MAX_DEFINITION_BASE64_CHARS = ((SPACE_ENTITY_MAX_DEFINITION_BYTES + 2) // 3) * 4
SPACE_ENTITY_MAX_AOI_RADIUS_CM = 64 * SPACE_CHUNK_SIZE * 100
SPACE_ENTITY_MAX_AOI_RESULTS = 256
SPACE_ENTITY_MAX_AOI_CANDIDATES = 4096
SPACE_ENTITY_MAX_CREATE_TOKENS = 20
SPACE_ENTITY_CREATE_TOKEN_PREFIX = "edsp_"
SPACE_ENTITY_EXECUTION_LEASE_SECONDS = 8
SPACE_ENTITY_SNAPSHOT_MAX_DEPTH = 20
SPACE_ENTITY_SNAPSHOT_MAX_VALUES = 100_000
SPACE_ENTITY_SNAPSHOT_MAX_CONTAINER_ITEMS = 8_192
SPACE_ENTITY_SNAPSHOT_MAX_STRING_CHARS = 65_536
SPACE_ENTITY_SNAPSHOT_FORBIDDEN_KEYS = {"__proto__"}


class StrictEntityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityPosition(StrictEntityModel):
    x_cm: StrictInt
    y_cm: StrictInt = Field(ge=MIN_PLAYER_Y_CM, le=MAX_PLAYER_Y_CM)
    z_cm: StrictInt


class CreateWorldEntityRequest(StrictEntityModel):
    operation_id: uuid.UUID
    resource_id: str = Field(min_length=1, max_length=16)
    position: EntityPosition
    yaw_quarter_turns: StrictInt = Field(default=0, ge=0, le=3)
    desired_run_state: Literal["running", "stopped"] = "running"


class CreateBrowserWorldEntityRequest(StrictEntityModel):
    operation_id: uuid.UUID
    definition_base64: StrictStr = Field(
        min_length=1,
        max_length=SPACE_ENTITY_MAX_DEFINITION_BASE64_CHARS,
    )
    snapshot: dict[str, Any]
    position: EntityPosition
    desired_run_state: Literal["running", "stopped"] = "stopped"


class CheckpointBrowserWorldEntityRequest(StrictEntityModel):
    operation_id: uuid.UUID
    expected_revision: StrictInt = Field(ge=1)
    definition_base64: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=SPACE_ENTITY_MAX_DEFINITION_BASE64_CHARS,
    )
    snapshot: dict[str, Any]
    position: EntityPosition
    desired_run_state: Literal["running", "stopped"]


class SetWorldEntityRunStateRequest(StrictEntityModel):
    operation_id: uuid.UUID
    desired_run_state: Literal["running", "stopped"]
    expected_revision: StrictInt | None = Field(default=None, ge=1)


class CreateEntityTokenRequest(StrictEntityModel):
    name: StrictStr = Field(min_length=1, max_length=80)


class ClaimEntityExecutionLeasesRequest(StrictEntityModel):
    instance_id: uuid.UUID
    entity_ids: list[uuid.UUID] = Field(min_length=1, max_length=SPACE_ENTITY_MAX_AOI_RESULTS)


def _request_digest(payload: CreateWorldEntityRequest) -> bytes:
    canonical = {
        "desired_run_state": payload.desired_run_state,
        "position": payload.position.model_dump(),
        "resource_id": payload.resource_id,
        "yaw_quarter_turns": payload.yaw_quarter_turns,
    }
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()).digest()


def _decode_browser_definition(encoded: str) -> tuple[bytes, bytes, dict]:
    try:
        definition = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_DEFINITION_BASE64_INVALID"}) from error
    if not definition or len(definition) > SPACE_ENTITY_MAX_DEFINITION_BYTES:
        raise HTTPException(status_code=413, detail={
            "code": "ENTITY_DEFINITION_TOO_LARGE",
            "limit_bytes": SPACE_ENTITY_MAX_DEFINITION_BYTES,
        })
    try:
        kind, decoded = decode_inventory_resource(definition)
        if kind != "entity":
            raise ValueError("uploaded resource is not an entity")
        canonical = validate_market_payload(kind, decoded)
        definition = encode_inventory_resource("entity", canonical)
    except (InventoryCodecError, ValueError) as error:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_DEFINITION_INVALID"}) from error
    if len(definition) > SPACE_ENTITY_MAX_DEFINITION_BYTES:
        raise HTTPException(status_code=413, detail={"code": "ENTITY_DEFINITION_TOO_LARGE"})
    return definition, hashlib.sha256(definition).digest(), canonical


def _validate_snapshot_json(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [SPACE_ENTITY_SNAPSHOT_MAX_VALUES]
    budget[0] -= 1
    if budget[0] < 0 or depth > SPACE_ENTITY_SNAPSHOT_MAX_DEPTH:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TOO_COMPLEX"})
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str) and len(value) > SPACE_ENTITY_SNAPSHOT_MAX_STRING_CHARS:
            raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_STRING_TOO_LONG"})
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if (
            (isinstance(value, float) and not math.isfinite(value))
            or abs(value) > 1_000_000_000_000
        ):
            raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_NUMBER_INVALID"})
        return
    if isinstance(value, list):
        if len(value) > SPACE_ENTITY_SNAPSHOT_MAX_CONTAINER_ITEMS:
            raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TOO_COMPLEX"})
        for item in value:
            _validate_snapshot_json(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        if len(value) > SPACE_ENTITY_SNAPSHOT_MAX_CONTAINER_ITEMS:
            raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TOO_COMPLEX"})
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 256
                or key in SPACE_ENTITY_SNAPSHOT_FORBIDDEN_KEYS
            ):
                raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_KEY_INVALID"})
            _validate_snapshot_json(item, depth=depth + 1, budget=budget)
        return
    raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_VALUE_INVALID"})


def _snapshot_vector(snapshot: dict[str, Any], key: str, length: int) -> list[float]:
    value = snapshot.get(key)
    if (
        not isinstance(value, list)
        or len(value) != length
        or any(isinstance(part, bool) or not isinstance(part, (int, float)) for part in value)
        or any(not math.isfinite(float(part)) for part in value)
    ):
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TRANSFORM_INVALID"})
    return [float(part) for part in value]


def _encode_snapshot(
    snapshot: dict[str, Any],
    world: models.SpaceWorld,
    position: EntityPosition,
) -> tuple[bytes, bytes]:
    if "slot" in snapshot:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_DEFINITION_DUPLICATED"})
    _validate_snapshot_json(snapshot)
    snapshot_position = _snapshot_vector(snapshot, "position", 3)
    _snapshot_vector(snapshot, "constructorOrigin", 3)
    quaternion = _snapshot_vector(snapshot, "quaternion", 4)
    quaternion_norm = math.sqrt(sum(component * component for component in quaternion))
    if quaternion_norm < 0.5 or quaternion_norm > 1.5:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TRANSFORM_INVALID"})
    for key in ("nodes", "bodies"):
        if not isinstance(snapshot.get(key, []), list) or len(snapshot.get(key, [])) > 64:
            raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TOO_COMPLEX"})
    if not isinstance(snapshot.get("states", {}), dict) or len(snapshot.get("states", {})) > 64:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TOO_COMPLEX"})
    if not isinstance(snapshot.get("scriptLogs", []), list) or len(snapshot.get("scriptLogs", [])) > 100:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TOO_COMPLEX"})
    if not isinstance(snapshot.get("nodeScriptErrors", []), list) or len(snapshot.get("nodeScriptErrors", [])) > 64:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_TOO_COMPLEX"})

    width_cm, length_cm = _world_dimensions_cm(world)
    snapshot_x_cm = round(snapshot_position[0] * 100) % width_cm
    snapshot_y_cm = round(snapshot_position[1] * 100)
    snapshot_z_cm = round(snapshot_position[2] * 100) % length_cm
    if (
        abs(snapshot_x_cm - position.x_cm) > 1
        or abs(snapshot_y_cm - position.y_cm) > 1
        or abs(snapshot_z_cm - position.z_cm) > 1
    ):
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_POSITION_MISMATCH"})
    try:
        encoded = json.dumps(
            snapshot,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_SNAPSHOT_INVALID"}) from error
    if len(encoded) > SPACE_ENTITY_MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail={
            "code": "ENTITY_SNAPSHOT_TOO_LARGE",
            "limit_bytes": SPACE_ENTITY_MAX_SNAPSHOT_BYTES,
        })
    return encoded, hashlib.sha256(encoded).digest()


def _browser_request_digest(
    *,
    definition_digest: bytes | None,
    snapshot_digest: bytes,
    position: EntityPosition,
    desired_run_state: str,
) -> bytes:
    canonical = {
        "definition_digest": definition_digest.hex() if definition_digest else None,
        "snapshot_digest": snapshot_digest.hex(),
        "position": position.model_dump(),
        "desired_run_state": desired_run_state,
    }
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()).digest()


def _entity_token_response(token: models.SpaceEntityCreateToken) -> dict:
    return {
        "id": token.id,
        "world_id": str(token.world_id),
        "name": token.name,
        "scope": "entity:create",
        "created_at": token.created_at.isoformat(),
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
    }


def _entity_creator(
    world_id: str,
    credentials: HTTPAuthorizationCredentials | None = Security(entity_security),
    db: Session = Depends(get_db),
) -> models.User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail={"code": "ENTITY_CREATE_AUTH_REQUIRED"})
    credential = credentials.credentials
    if not credential.startswith(SPACE_ENTITY_CREATE_TOKEN_PREFIX):
        return auth.get_current_user(credentials=credentials, db=db)
    token_hash = hashlib.sha256(credential.encode()).digest()
    token = db.query(models.SpaceEntityCreateToken).filter(
        models.SpaceEntityCreateToken.token_hash == token_hash,
        models.SpaceEntityCreateToken.world_id == world_id,
    ).first()
    if token is None:
        raise HTTPException(status_code=401, detail={"code": "ENTITY_CREATE_TOKEN_INVALID"})
    user = db.query(models.User).filter(models.User.id == token.user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "ENTITY_CREATE_TOKEN_INVALID"})
    token.last_used_at = datetime.datetime.now(datetime.timezone.utc)
    return user


def _world_dimensions_cm(world: models.SpaceWorld) -> tuple[int, int]:
    return (
        int(world.width_chunks) * SPACE_CHUNK_SIZE * 100,
        int(world.length_chunks) * SPACE_CHUNK_SIZE * 100,
    )


def _utc(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=datetime.timezone.utc)


def _validate_position(world: models.SpaceWorld, position: EntityPosition) -> None:
    width_cm, length_cm = _world_dimensions_cm(world)
    if not (0 <= position.x_cm < width_cm and 0 <= position.z_cm < length_cm):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ENTITY_POSITION_OUT_OF_BOUNDS",
                "message": "Entity X/Z coordinates must be inside the wrapped world coordinate range.",
            },
        )


def _entity_response(entity: models.SpaceWorldEntity, current_user: models.User) -> dict:
    can_control = entity.owner_user_id == current_user.id or bool(current_user.is_admin)
    return {
        "id": str(entity.id),
        "world_id": str(entity.world_id),
        "owner_user_id": entity.owner_user_id,
        "source_kind": entity.source_kind,
        "source_resource_id": entity.source_resource_id,
        "name": entity.name,
        "schema_version": entity.schema_version,
        "definition_digest": bytes(entity.content_digest).hex(),
        "definition_size_bytes": entity.size_bytes,
        "definition_url": (
            f"/space/api/v2/worlds/{entity.world_id}/entities/{entity.id}/definition"
            f"?digest={bytes(entity.content_digest).hex()}"
        ),
        "snapshot_digest": bytes(entity.snapshot_digest).hex() if entity.snapshot_digest else None,
        "snapshot_size_bytes": entity.snapshot_size_bytes,
        "snapshot_url": (
            f"/space/api/v2/worlds/{entity.world_id}/entities/{entity.id}/snapshot"
            f"?digest={bytes(entity.snapshot_digest).hex()}"
            if entity.snapshot is not None and entity.snapshot_digest is not None else None
        ),
        "position": {
            "x_cm": entity.position_x_cm,
            "y_cm": entity.position_y_cm,
            "z_cm": entity.position_z_cm,
        },
        "yaw_quarter_turns": entity.yaw_quarter_turns,
        "desired_run_state": entity.desired_run_state,
        "revision": entity.revision,
        "can_control": can_control,
        "can_edit": entity.source_kind == "browser" and can_control,
        "created_at": entity.created_at.isoformat(),
        "updated_at": entity.updated_at.isoformat(),
    }


def _wrapped_intervals(center: int, radius: int, extent: int) -> list[tuple[int, int]]:
    if radius * 2 >= extent:
        return [(0, extent - 1)]
    low = center - radius
    high = center + radius
    if low < 0:
        return [(0, high), (extent + low, extent - 1)]
    if high >= extent:
        return [(low, extent - 1), (0, high - extent)]
    return [(low, high)]


def _interval_filter(column, intervals: list[tuple[int, int]]):
    predicates = [and_(column >= low, column <= high) for low, high in intervals]
    return predicates[0] if len(predicates) == 1 else or_(*predicates)


def _wrapped_delta(value: int, center: int, extent: int) -> int:
    return (value - center + extent // 2) % extent - extent // 2


def _load_canonical_entity(resource: models.SpaceMarketResource) -> tuple[bytes, bytes]:
    try:
        stored = s3_utils.download_from_s3(resource.object_key, is_public=True)
    except Exception as error:
        logger.exception("Could not load market entity %s for world placement", resource.id)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ENTITY_DEFINITION_UNAVAILABLE",
                "message": "The market entity definition is temporarily unavailable.",
            },
        ) from error
    if not stored or len(stored) > SPACE_ENTITY_MAX_DEFINITION_BYTES:
        raise HTTPException(status_code=500, detail={"code": "MARKET_ENTITY_CORRUPT"})
    try:
        kind, decoded = decode_inventory_resource(stored)
        if kind != "entity":
            raise ValueError("market resource kind does not match its object")
        canonical = validate_market_payload(kind, decoded)
    except (InventoryCodecError, ValueError) as error:
        logger.exception("Market entity %s is not a valid canonical entity", resource.id)
        raise HTTPException(status_code=500, detail={"code": "MARKET_ENTITY_CORRUPT"}) from error
    if inventory_content_digest("entity", canonical) != bytes(resource.content_digest):
        raise HTTPException(status_code=500, detail={"code": "MARKET_ENTITY_CORRUPT"})
    encoded = encode_inventory_resource("entity", canonical)
    return encoded, hashlib.sha256(encoded).digest()


@token_router.post("", status_code=201)
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def create_entity_create_token(
    request: Request,
    world_id: str,
    payload: CreateEntityTokenRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_CREATE_TOKEN_NAME_REQUIRED"})
    count = db.query(models.SpaceEntityCreateToken).filter(
        models.SpaceEntityCreateToken.world_id == world.id,
        models.SpaceEntityCreateToken.user_id == current_user.id,
    ).count()
    if count >= SPACE_ENTITY_MAX_CREATE_TOKENS:
        raise HTTPException(status_code=429, detail={
            "code": "ENTITY_CREATE_TOKEN_LIMIT_REACHED",
            "limit": SPACE_ENTITY_MAX_CREATE_TOKENS,
        })
    plaintext = f"{SPACE_ENTITY_CREATE_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    token = models.SpaceEntityCreateToken(
        world_id=world.id,
        user_id=current_user.id,
        name=name,
        token_hash=hashlib.sha256(plaintext.encode()).digest(),
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return {**_entity_token_response(token), "token": plaintext}


@token_router.get("")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def list_entity_create_tokens(
    request: Request,
    world_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    tokens = db.query(models.SpaceEntityCreateToken).filter(
        models.SpaceEntityCreateToken.world_id == world.id,
        models.SpaceEntityCreateToken.user_id == current_user.id,
    ).order_by(models.SpaceEntityCreateToken.created_at.desc()).all()
    return {"items": [_entity_token_response(token) for token in tokens]}


@token_router.delete("/{token_id}")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def revoke_entity_create_token(
    request: Request,
    world_id: str,
    token_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    token = db.query(models.SpaceEntityCreateToken).filter(
        models.SpaceEntityCreateToken.id == token_id,
        models.SpaceEntityCreateToken.world_id == world.id,
        models.SpaceEntityCreateToken.user_id == current_user.id,
    ).first()
    if token is None:
        raise HTTPException(status_code=404, detail={"code": "ENTITY_CREATE_TOKEN_NOT_FOUND"})
    db.delete(token)
    db.commit()
    return {"revoked": True, "token_id": token_id}


@router.post("", status_code=201)
@limiter.limit(SPACE_ENTITY_CREATE_RATE_LIMIT)
def create_world_entity(
    request: Request,
    world_id: str,
    payload: CreateWorldEntityRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_entity_creator),
):
    world = _require_world_membership(db, world_id, current_user)
    _validate_position(world, payload.position)
    operation_id = str(payload.operation_id)
    request_digest = _request_digest(payload)
    existing = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.owner_user_id == current_user.id,
        models.SpaceWorldEntity.create_operation_id == operation_id,
    ).first()
    if existing is not None:
        if bytes(existing.create_request_digest) != request_digest:
            raise HTTPException(status_code=409, detail={"code": "ENTITY_OPERATION_ID_REUSED"})
        return _entity_response(existing, current_user)

    resource = db.query(models.SpaceMarketResource).filter(
        models.SpaceMarketResource.id == payload.resource_id,
    ).first()
    if resource is None:
        raise HTTPException(status_code=404, detail={"code": "MARKET_RESOURCE_NOT_FOUND"})
    if resource.kind != "entity":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ENTITY_RESOURCE_REQUIRED",
                "message": "Only a published entity resource can be placed in the world.",
            },
        )

    # Serialize quota checks for one creator. The definition is copied before
    # insertion so a later hard-delete of the market row/object cannot break
    # an entity already present in the world.
    db.query(models.User).filter(models.User.id == current_user.id).with_for_update().first()
    owned_count = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.owner_user_id == current_user.id,
    ).count()
    if owned_count >= SPACE_ENTITY_MAX_PER_OWNER:
        raise HTTPException(status_code=429, detail={
            "code": "WORLD_ENTITY_QUOTA_REACHED",
            "limit": SPACE_ENTITY_MAX_PER_OWNER,
        })

    definition, definition_digest = _load_canonical_entity(resource)
    entity = models.SpaceWorldEntity(
        world_id=world.id,
        owner_user_id=current_user.id,
        source_kind="market",
        source_resource_id=resource.id,
        name=resource.name,
        schema_version=resource.schema_version,
        content_digest=definition_digest,
        definition=definition,
        size_bytes=len(definition),
        position_x_cm=payload.position.x_cm,
        position_y_cm=payload.position.y_cm,
        position_z_cm=payload.position.z_cm,
        yaw_quarter_turns=payload.yaw_quarter_turns,
        desired_run_state=payload.desired_run_state,
        revision=1,
        create_operation_id=operation_id,
        create_request_digest=request_digest,
    )
    db.add(entity)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        existing = db.query(models.SpaceWorldEntity).filter(
            models.SpaceWorldEntity.world_id == world.id,
            models.SpaceWorldEntity.owner_user_id == current_user.id,
            models.SpaceWorldEntity.create_operation_id == operation_id,
        ).first()
        if existing is not None and bytes(existing.create_request_digest) == request_digest:
            return _entity_response(existing, current_user)
        raise HTTPException(status_code=409, detail={"code": "ENTITY_CREATE_CONFLICT"}) from error
    db.refresh(entity)
    return _entity_response(entity, current_user)


@router.post("/browser", status_code=201)
@limiter.limit(SPACE_ENTITY_CREATE_RATE_LIMIT)
def create_browser_world_entity(
    request: Request,
    world_id: str,
    payload: CreateBrowserWorldEntityRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Persist an entity authored in an authenticated Space browser."""
    world = _require_world_membership(db, world_id, current_user)
    _validate_position(world, payload.position)
    definition, definition_digest, canonical = _decode_browser_definition(payload.definition_base64)
    snapshot, snapshot_digest = _encode_snapshot(payload.snapshot, world, payload.position)
    operation_id = str(payload.operation_id)
    request_digest = _browser_request_digest(
        definition_digest=definition_digest,
        snapshot_digest=snapshot_digest,
        position=payload.position,
        desired_run_state=payload.desired_run_state,
    )
    existing = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.owner_user_id == current_user.id,
        models.SpaceWorldEntity.create_operation_id == operation_id,
    ).first()
    if existing is not None:
        if bytes(existing.create_request_digest) != request_digest:
            raise HTTPException(status_code=409, detail={"code": "ENTITY_OPERATION_ID_REUSED"})
        return _entity_response(existing, current_user)

    db.query(models.User).filter(models.User.id == current_user.id).with_for_update().first()
    owned_count = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.owner_user_id == current_user.id,
    ).count()
    if owned_count >= SPACE_ENTITY_MAX_PER_OWNER:
        raise HTTPException(status_code=429, detail={
            "code": "WORLD_ENTITY_QUOTA_REACHED",
            "limit": SPACE_ENTITY_MAX_PER_OWNER,
        })

    entity = models.SpaceWorldEntity(
        world_id=world.id,
        owner_user_id=current_user.id,
        source_kind="browser",
        source_resource_id=None,
        name=str(canonical.get("name") or "Entity")[:80],
        schema_version=3,
        content_digest=definition_digest,
        definition=definition,
        size_bytes=len(definition),
        snapshot=snapshot,
        snapshot_digest=snapshot_digest,
        snapshot_size_bytes=len(snapshot),
        position_x_cm=payload.position.x_cm,
        position_y_cm=payload.position.y_cm,
        position_z_cm=payload.position.z_cm,
        yaw_quarter_turns=0,
        desired_run_state=payload.desired_run_state,
        revision=1,
        create_operation_id=operation_id,
        create_request_digest=request_digest,
    )
    db.add(entity)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        existing = db.query(models.SpaceWorldEntity).filter(
            models.SpaceWorldEntity.world_id == world.id,
            models.SpaceWorldEntity.owner_user_id == current_user.id,
            models.SpaceWorldEntity.create_operation_id == operation_id,
        ).first()
        if existing is not None and bytes(existing.create_request_digest) == request_digest:
            return _entity_response(existing, current_user)
        raise HTTPException(status_code=409, detail={"code": "ENTITY_CREATE_CONFLICT"}) from error
    db.refresh(entity)
    return _entity_response(entity, current_user)


@router.get("")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def list_world_entities(
    request: Request,
    world_id: str,
    center_x_cm: int = Query(ge=0),
    center_z_cm: int = Query(ge=0),
    radius_cm: int = Query(default=32 * SPACE_CHUNK_SIZE * 100, ge=100, le=SPACE_ENTITY_MAX_AOI_RADIUS_CM),
    limit: int = Query(default=SPACE_ENTITY_MAX_AOI_RESULTS, ge=1, le=SPACE_ENTITY_MAX_AOI_RESULTS),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    width_cm, length_cm = _world_dimensions_cm(world)
    if center_x_cm >= width_cm or center_z_cm >= length_cm:
        raise HTTPException(status_code=422, detail={"code": "ENTITY_AOI_OUT_OF_BOUNDS"})
    candidates = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        _interval_filter(
            models.SpaceWorldEntity.position_x_cm,
            _wrapped_intervals(center_x_cm, radius_cm, width_cm),
        ),
        _interval_filter(
            models.SpaceWorldEntity.position_z_cm,
            _wrapped_intervals(center_z_cm, radius_cm, length_cm),
        ),
    ).order_by(models.SpaceWorldEntity.created_at, models.SpaceWorldEntity.id).limit(
        SPACE_ENTITY_MAX_AOI_CANDIDATES + 1
    ).all()
    in_radius = []
    radius_squared = radius_cm * radius_cm
    for entity in candidates[:SPACE_ENTITY_MAX_AOI_CANDIDATES]:
        dx = _wrapped_delta(entity.position_x_cm, center_x_cm, width_cm)
        dz = _wrapped_delta(entity.position_z_cm, center_z_cm, length_cm)
        if dx * dx + dz * dz <= radius_squared:
            in_radius.append((dx * dx + dz * dz, entity))
    in_radius.sort(key=lambda item: (item[0], item[1].created_at, str(item[1].id)))
    truncated = len(candidates) > SPACE_ENTITY_MAX_AOI_CANDIDATES or len(in_radius) > limit
    return {
        "items": [_entity_response(entity, current_user) for _distance, entity in in_radius[:limit]],
        "truncated": truncated,
        "limit": limit,
    }


@router.get("/{entity_id}/definition")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def get_world_entity_definition(
    request: Request,
    world_id: str,
    entity_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    entity = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.id == entity_id,
    ).first()
    if entity is None:
        raise HTTPException(status_code=404, detail={"code": "WORLD_ENTITY_NOT_FOUND"})
    definition = bytes(entity.definition)
    if len(definition) != entity.size_bytes or hashlib.sha256(definition).digest() != bytes(entity.content_digest):
        raise HTTPException(status_code=500, detail={"code": "WORLD_ENTITY_DEFINITION_CORRUPT"})
    return Response(
        content=definition,
        media_type="application/x-protobuf",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{bytes(entity.content_digest).hex()}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{entity_id}/snapshot")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def get_world_entity_snapshot(
    request: Request,
    world_id: str,
    entity_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    entity = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.id == entity_id,
    ).first()
    if entity is None:
        raise HTTPException(status_code=404, detail={"code": "WORLD_ENTITY_NOT_FOUND"})
    if entity.snapshot is None or entity.snapshot_digest is None:
        raise HTTPException(status_code=404, detail={"code": "WORLD_ENTITY_SNAPSHOT_NOT_FOUND"})
    snapshot = bytes(entity.snapshot)
    if (
        len(snapshot) != entity.snapshot_size_bytes
        or hashlib.sha256(snapshot).digest() != bytes(entity.snapshot_digest)
    ):
        raise HTTPException(status_code=500, detail={"code": "WORLD_ENTITY_SNAPSHOT_CORRUPT"})
    return Response(
        content=snapshot,
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "ETag": f'"{bytes(entity.snapshot_digest).hex()}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put("/{entity_id}/checkpoint")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def checkpoint_browser_world_entity(
    request: Request,
    world_id: str,
    entity_id: str,
    payload: CheckpointBrowserWorldEntityRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    _validate_position(world, payload.position)
    entity = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.id == entity_id,
    ).with_for_update().first()
    if entity is None:
        raise HTTPException(status_code=404, detail={"code": "WORLD_ENTITY_NOT_FOUND"})
    if entity.owner_user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail={"code": "ENTITY_EDIT_FORBIDDEN"})
    if entity.source_kind != "browser":
        raise HTTPException(status_code=409, detail={"code": "MARKET_ENTITY_IMMUTABLE"})

    definition = None
    definition_digest = None
    canonical = None
    if payload.definition_base64 is not None:
        definition, definition_digest, canonical = _decode_browser_definition(payload.definition_base64)
    snapshot, snapshot_digest = _encode_snapshot(payload.snapshot, world, payload.position)
    request_digest = _browser_request_digest(
        definition_digest=definition_digest,
        snapshot_digest=snapshot_digest,
        position=payload.position,
        desired_run_state=payload.desired_run_state,
    )
    operation_id = str(payload.operation_id)
    if str(entity.last_checkpoint_operation_id or "") == operation_id:
        if bytes(entity.last_checkpoint_request_digest or b"") != request_digest:
            raise HTTPException(status_code=409, detail={"code": "ENTITY_OPERATION_ID_REUSED"})
        return _entity_response(entity, current_user)
    if payload.expected_revision != entity.revision:
        raise HTTPException(status_code=409, detail={
            "code": "ENTITY_REVISION_CONFLICT",
            "current": _entity_response(entity, current_user),
        })

    if definition is not None and definition_digest is not None:
        entity.definition = definition
        entity.content_digest = definition_digest
        entity.size_bytes = len(definition)
        entity.name = str((canonical or {}).get("name") or entity.name)[:80]
    entity.snapshot = snapshot
    entity.snapshot_digest = snapshot_digest
    entity.snapshot_size_bytes = len(snapshot)
    entity.position_x_cm = payload.position.x_cm
    entity.position_y_cm = payload.position.y_cm
    entity.position_z_cm = payload.position.z_cm
    entity.desired_run_state = payload.desired_run_state
    if payload.desired_run_state == "stopped" and entity.execution_instance_id is not None:
        entity.execution_instance_id = None
        entity.execution_lease_expires_at = None
        entity.execution_epoch = int(entity.execution_epoch or 0) + 1
    entity.revision += 1
    entity.last_checkpoint_operation_id = operation_id
    entity.last_checkpoint_request_digest = request_digest
    entity.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    db.refresh(entity)
    return _entity_response(entity, current_user)


@router.delete("/{entity_id}")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def delete_world_entity(
    request: Request,
    world_id: str,
    entity_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    entity = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.id == entity_id,
    ).with_for_update().first()
    if entity is None:
        raise HTTPException(status_code=404, detail={"code": "WORLD_ENTITY_NOT_FOUND"})
    if entity.owner_user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail={"code": "ENTITY_DELETE_FORBIDDEN"})
    db.delete(entity)
    db.commit()
    return {"deleted": True, "entity_id": entity_id}


@router.put("/execution-leases")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def claim_world_entity_execution_leases(
    request: Request,
    world_id: str,
    payload: ClaimEntityExecutionLeasesRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    requested_ids = [str(entity_id) for entity_id in payload.entity_ids]
    instance_id = str(payload.instance_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(seconds=SPACE_ENTITY_EXECUTION_LEASE_SECONDS)
    rows = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.id.in_(requested_ids),
    ).with_for_update().all()
    by_id = {str(entity.id): entity for entity in rows}
    results = []
    for entity_id in requested_ids:
        entity = by_id.get(entity_id)
        granted = False
        epoch = 0
        if (
            entity is not None
            and entity.owner_user_id == current_user.id
            and entity.desired_run_state == "running"
        ):
            current_expiry = _utc(entity.execution_lease_expires_at)
            same_instance = str(entity.execution_instance_id or "") == instance_id
            available = current_expiry is None or current_expiry <= now
            if same_instance or available:
                if not same_instance:
                    entity.execution_epoch = int(entity.execution_epoch or 0) + 1
                entity.execution_instance_id = instance_id
                entity.execution_lease_expires_at = expires_at
                granted = True
                epoch = entity.execution_epoch
        results.append({
            "entity_id": entity_id,
            "granted": granted,
            "execution_epoch": epoch,
            "lease_expires_at": expires_at.isoformat() if granted else None,
        })
    db.commit()
    return {
        "instance_id": instance_id,
        "lease_seconds": SPACE_ENTITY_EXECUTION_LEASE_SECONDS,
        "items": results,
    }


@router.put("/{entity_id}/run-state")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def set_world_entity_run_state(
    request: Request,
    world_id: str,
    entity_id: str,
    payload: SetWorldEntityRunStateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = _require_world_membership(db, world_id, current_user)
    entity = db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world.id,
        models.SpaceWorldEntity.id == entity_id,
    ).with_for_update().first()
    if entity is None:
        raise HTTPException(status_code=404, detail={"code": "WORLD_ENTITY_NOT_FOUND"})
    if entity.owner_user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail={
            "code": "ENTITY_CONTROL_FORBIDDEN",
            "message": "Only the entity owner or an administrator may start or stop it.",
        })
    operation_id = str(payload.operation_id)
    if str(entity.last_control_operation_id or "") == operation_id:
        return _entity_response(entity, current_user)
    if payload.expected_revision is not None and payload.expected_revision != entity.revision:
        raise HTTPException(status_code=409, detail={
            "code": "ENTITY_REVISION_CONFLICT",
            "current": _entity_response(entity, current_user),
        })
    if entity.desired_run_state != payload.desired_run_state:
        entity.desired_run_state = payload.desired_run_state
        entity.revision += 1
        if payload.desired_run_state == "stopped" and entity.execution_instance_id is not None:
            entity.execution_instance_id = None
            entity.execution_lease_expires_at = None
            entity.execution_epoch = int(entity.execution_epoch or 0) + 1
    entity.last_control_operation_id = operation_id
    entity.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    db.refresh(entity)
    return _entity_response(entity, current_user)
