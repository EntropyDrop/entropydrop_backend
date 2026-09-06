"""Explicit, budgeted entity hosting; ordinary run-state requests never buy time."""
import datetime as dt
import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import StrictBool, StrictInt
from sqlalchemy.orm import Session

from space import models
from config import settings
from space.database import get_db
from rate_limit import limiter
from routers.space import _require_world_membership
from routers.space_entities import (
    EntityCreator, StrictEntityModel, _entity_creator, _enforce_running_entity_quota,
    EntityPosition,
)
from space.inventory_codec import decode_inventory_resource

def require_hosting_enabled():
    if not settings.SPACE_HOSTING_ENABLED:
        raise HTTPException(503, detail={"code": "HOSTING_DISABLED", "message": "Entity hosting is not available."})


router = APIRouter(prefix="/space/api/v2/worlds/{world_id}/entities", tags=["space-hosting"],
                   dependencies=[Depends(require_hosting_enabled)])
HOUR_MS = 3_600_000
MAX_HOSTED_PER_WORLD = 4
MAX_HOSTED_BLOCKS = 512
MAX_HOSTED_COMPONENTS = 8
HOSTING_RADIUS_CHUNKS = 2


def utc(value):
    return value.replace(tzinfo=dt.timezone.utc) if value and value.tzinfo is None else value


def status(entity, *, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    recent = entity.hosting_last_tick_at and (now - utc(entity.hosting_last_tick_at)).total_seconds() < 15
    return {
        "entity_id": str(entity.id), "world_id": str(entity.world_id),
        "execution_mode": entity.execution_mode,
        "enabled": entity.hosting_enabled,
        "state": ("running" if recent else "starting") if entity.hosting_enabled else "paused",
        "reason": entity.hosting_reason,
        "error": entity.hosting_error,
        "credits_per_hour": 1,
        "remaining_ms": entity.hosting_remaining_ms,
        "budget_remaining_credits": entity.hosting_budget_remaining,
        "billed_hours": entity.hosting_billed_hours,
        "last_tick_at": utc(entity.hosting_last_tick_at).isoformat() if entity.hosting_last_tick_at else None,
        "activity_radius_chunks": HOSTING_RADIUS_CHUNKS,
    }


def validate_hosted_definition(definition):
    _, portable = decode_inventory_resource(bytes(definition))
    nodes, blocks = 0, 0
    def visit(node):
        nonlocal nodes, blocks
        nodes += 1
        blocks += len(node.get("blocks", []))
        for field in ("pivot", "localPosition"):
            if any(abs(v) > 16 for v in node.get(field, [])):
                raise HTTPException(422, detail={"code": "HOSTING_ENTITY_BOUNDS_LIMIT"})
        if any(abs(block.get(axis, 0)) > 16 for block in node.get("blocks", []) for axis in ("dx", "dy", "dz")):
            raise HTTPException(422, detail={"code": "HOSTING_ENTITY_BOUNDS_LIMIT"})
        for child in node.get("children", []):
            visit(child)
    visit(portable["root"])
    if nodes > MAX_HOSTED_COMPONENTS or blocks > MAX_HOSTED_BLOCKS:
        raise HTTPException(422, detail={"code": "HOSTING_ENTITY_TOO_COMPLEX",
                                        "max_components": MAX_HOSTED_COMPONENTS,
                                        "max_blocks": MAX_HOSTED_BLOCKS})


class HostingRequest(StrictEntityModel):
    operation_id: uuid.UUID
    enabled: StrictBool
    release_to_browser: StrictBool = False
    # Maximum NEW credits this operation authorizes. Existing prepaid time survives pause.
    max_credits: StrictInt = 1


def authorized_entity(db, world_id, entity_id, creator):
    if creator.api_key_scopes is not None and "space:entity:run" not in creator.api_key_scopes:
        raise HTTPException(403, detail={"code": "SPACE_API_KEY_SCOPE_REQUIRED",
                                        "required_scope": "space:entity:run"})
    world = _require_world_membership(db, world_id, creator.user)
    entity = db.query(models.SpaceWorldEntity).filter_by(world_id=world_id, id=entity_id).with_for_update().first()
    if entity is None:
        raise HTTPException(404, detail={"code": "WORLD_ENTITY_NOT_FOUND"})
    # Even administrators cannot charge another user's account through hosting.
    if entity.owner_user_id != creator.user.id:
        raise HTTPException(403, detail={"code": "ENTITY_HOSTING_FORBIDDEN"})
    return world, entity


@router.put("/{entity_id}/hosting")
@limiter.limit("60/minute")
def set_hosting(request: Request, world_id: uuid.UUID, entity_id: uuid.UUID, payload: HostingRequest,
                db: Session = Depends(get_db), creator: EntityCreator = Depends(_entity_creator)):
    world_id, entity_id = str(world_id), str(entity_id)
    world, entity = authorized_entity(db, world_id, entity_id, creator)
    if not 0 <= payload.max_credits <= 168:
        raise HTTPException(422, detail={"code": "HOSTING_BUDGET_INVALID", "max_credits": 168})
    if payload.enabled and payload.release_to_browser:
        raise HTTPException(422, detail={"code": "HOSTING_RELEASE_REQUIRES_PAUSE"})
    digest = hashlib.sha256(json.dumps({**payload.model_dump(mode="json"), "entity_id": entity_id,
                                       "owner": creator.user.id}, sort_keys=True).encode()).digest()
    previous = db.get(models.SpaceHostingOperation, (world_id, str(payload.operation_id)))
    if previous:
        if bytes(previous.request_digest) != digest:
            raise HTTPException(409, detail={"code": "ENTITY_OPERATION_ID_REUSED"})
        return previous.result
    now = dt.datetime.now(dt.timezone.utc)
    if payload.enabled:
        if world.status != 1:
            raise HTTPException(409, detail={"code": "HOSTING_WORLD_INACTIVE"})
        worker = db.get(models.SpaceHostingWorker, world_id)
        if not worker or utc(worker.lease_expires_at) <= now:
            raise HTTPException(503, detail={"code": "HOSTING_WORKER_UNAVAILABLE"})
        validate_hosted_definition(entity.definition)
        count = db.query(models.SpaceWorldEntity).filter_by(world_id=world_id, hosting_enabled=True).count()
        if not entity.hosting_enabled and count >= MAX_HOSTED_PER_WORLD:
            raise HTTPException(429, detail={"code": "HOSTING_WORLD_FULL", "limit": MAX_HOSTED_PER_WORLD})
        if not entity.hosting_remaining_ms and (payload.max_credits == 0 or creator.user.credits < 1):
            raise HTTPException(402, detail={"code": "HOSTING_CREDITS_REQUIRED"})
        _enforce_running_entity_quota(db, world, creator.user,
            EntityPosition(x_cm=entity.position_x_cm, y_cm=entity.position_y_cm, z_cm=entity.position_z_cm),
            exclude_entity_id=entity_id)
        entity.execution_mode = "hosted"
        entity.hosting_anchor = entity.hosting_anchor or [entity.position_x_cm, entity.position_z_cm]
        entity.hosting_reason = None
        entity.hosting_error = None
        entity.hosting_last_tick_at = None
        entity.hosting_budget_remaining = payload.max_credits
    else:
        entity.hosting_reason = "user_paused"
        entity.hosting_budget_remaining = 0
        if payload.release_to_browser:
            entity.execution_mode = "browser"
            entity.hosting_anchor = None
    if settings.SPACE_STANDALONE:
        from space.billing import authorize
        authorize(db, creator, entity, payload)
    entity.hosting_enabled = payload.enabled
    entity.desired_run_state = "running" if payload.enabled else "stopped"
    entity.execution_instance_id = None
    # Let a previously granted browser lease expire before the worker takes over.
    if not payload.enabled:
        entity.execution_lease_expires_at = None
    entity.execution_epoch += 1
    entity.revision += 1
    entity.updated_at = now
    result = status(entity, now=now)
    db.add(models.SpaceHostingOperation(world_id=world_id, operation_id=str(payload.operation_id),
                                        request_digest=digest, result=result))
    db.commit()
    return result


@router.get("/{entity_id}/hosting")
@limiter.limit("120/minute")
def get_hosting(request: Request, world_id: uuid.UUID, entity_id: uuid.UUID,
                db: Session = Depends(get_db), creator: EntityCreator = Depends(_entity_creator)):
    _, entity = authorized_entity(db, str(world_id), str(entity_id), creator)
    result = status(entity)
    snapshot = json.loads(entity.snapshot) if entity.snapshot else {}
    result["position"] = {"x_cm": entity.position_x_cm, "y_cm": entity.position_y_cm, "z_cm": entity.position_z_cm}
    result["logs"] = snapshot.get("scriptLogs", [])[-100:]
    result["script_error"] = snapshot.get("scriptError")
    result["tick_count"] = snapshot.get("tickCount", 0)
    db.commit()  # persist API key last-used metadata
    return result
