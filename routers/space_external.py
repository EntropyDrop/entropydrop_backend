"""Scoped external building and live account allowances for Space settings."""
import base64
import binascii
import datetime as dt
import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, StrictInt, StrictStr
from sqlalchemy.orm import Session

from space import models
from config import settings
from space.database import get_db
from rate_limit import limiter
from routers import space, space_entities as entities
from routers.space_market import validate_inventory_resource_payload
from space.inventory_codec import InventoryCodecError, decode_inventory_resource, encode_inventory_resource
from space_quota import bucket_start, usage

router = APIRouter(prefix="/space/api/v2/worlds/{world_id}", tags=["space-external"])
MAX_BUILD_BLOCKS = 1024
MAX_BUILD_BYTES = 1024 * 1024
BUILD_RATE_LIMIT = "30/minute; 300/hour"
BUILD_NAMESPACE = uuid.UUID("a80d5b20-f59d-47b1-8ae1-88e36c2fd349")


class BuildBlocksetRequest(entities.StrictEntityModel):
    operation_id: uuid.UUID
    created_at_ms: StrictInt = Field(ge=0)
    definition_base64: StrictStr = Field(min_length=1, max_length=((MAX_BUILD_BYTES + 2) // 3) * 4)
    position: entities.EntityPosition
    yaw_quarter_turns: StrictInt = Field(default=0, ge=0, le=3)


class BuildTerrainBatch(space.TerrainMutationBatchRequest):
    # Each micro parent also needs a standard-air edit and a micro clear.
    mutations: list[space.TerrainMutation] = Field(min_length=1, max_length=MAX_BUILD_BLOCKS * 3)


def _build_mutations(payload, canonical):
    origin = [getattr(payload.position, f"{axis}_cm") * space.SPACE_MICRO_DIVISIONS // 100 for axis in "xyz"]
    mutations, micro_parents = [], set()
    for block in canonical["blocks"]:
        micro = "mx" in block
        size = 1 if micro else space.SPACE_MICRO_DIVISIONS
        x, y, z = [block[f"d{axis}"] * space.SPACE_MICRO_DIVISIONS + block.get(f"m{axis}", 0) for axis in "xyz"]
        # Rotate voxel volumes around the construction origin, preserving grid alignment.
        for _ in range(payload.yaw_quarter_turns):
            x, z = z, -x - size
        x, y, z = x + origin[0], y + origin[1], z + origin[2]
        if micro:
            parent = (x // space.SPACE_MICRO_DIVISIONS, y // space.SPACE_MICRO_DIVISIONS, z // space.SPACE_MICRO_DIVISIONS)
            if parent not in micro_parents:
                micro_parents.add(parent)
                px, py, pz = parent
                mutations.extend([
                    space.TerrainMutation(kind="set_standard", x=px, y=py, z=pz, block=0, color=0),
                    space.TerrainMutation(kind="clear_micro_cell", x=px, y=py, z=pz),
                ])
            mutations.append(space.TerrainMutation(kind="set_micro", mx=x, my=y, mz=z, color=block["color"]))
        else:
            mutations.append(space.TerrainMutation(kind="set_standard", x=x // space.SPACE_MICRO_DIVISIONS, y=y // space.SPACE_MICRO_DIVISIONS, z=z // space.SPACE_MICRO_DIVISIONS,
                                                   block=1, color=block["color"]))
    return mutations


@router.post("/blocksets/build", status_code=201)
@limiter.limit(BUILD_RATE_LIMIT)
def build_blockset(request: Request, world_id: uuid.UUID, payload: BuildBlocksetRequest,
                   db: Session = Depends(get_db), creator: entities.EntityCreator = Depends(entities._entity_creator)):
    world = space._require_world_membership(db, str(world_id), creator.user)
    entities._validate_position(world, payload.position, require_buildable_height=True)
    if any(getattr(payload.position, f"{axis}_cm") % 100 for axis in "xyz"):
        raise HTTPException(422, detail={"code": "BLOCKSET_ORIGIN_MUST_ALIGN_TO_METRE"})
    try:
        raw = base64.b64decode(payload.definition_base64, validate=True)
        if len(raw) > MAX_BUILD_BYTES:
            raise HTTPException(413, detail={"code": "BLOCKSET_DEFINITION_TOO_LARGE"})
        kind, decoded = decode_inventory_resource(raw)
        if kind != "blockset":
            raise ValueError("expected a blockset")
        canonical = validate_inventory_resource_payload(kind, decoded)
    except (binascii.Error, InventoryCodecError, ValueError) as error:
        raise HTTPException(422, detail={"code": "BLOCKSET_DEFINITION_INVALID"}) from error
    if len(canonical["blocks"]) > MAX_BUILD_BLOCKS:
        raise HTTPException(413, detail={"code": "BLOCKSET_TOO_MANY_BLOCKS", "limit": MAX_BUILD_BLOCKS})
    definition_digest = hashlib.sha256(encode_inventory_resource("blockset", canonical)).hexdigest()
    digest = hashlib.sha256(json.dumps({
        "definition": definition_digest, "position": payload.position.model_dump(),
        "yaw": payload.yaw_quarter_turns, "created_at_ms": payload.created_at_ms,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    batch_id = uuid.uuid5(BUILD_NAMESPACE, f"{world.id}:{creator.user.id}:{payload.operation_id}")
    batch = BuildTerrainBatch(batch_id=batch_id, dedupe_epoch=1, created_at_ms=payload.created_at_ms,
                              mutations=_build_mutations(payload, canonical))
    # Validate the retry window even when a receipt exists, preventing replay after pruning.
    space._terrain_batch_client_created_at(batch, dt.datetime.now(dt.timezone.utc))
    receipt = db.query(models.SpaceTerrainMutationBatch).filter_by(world_id=world.id, batch_id=str(batch_id)).first()
    if receipt:
        metadata = receipt.result.get("blockset_build", {})
        if receipt.actor_user_id != creator.user.id or metadata.get("request_digest") != digest:
            raise HTTPException(409, detail={"code": "BLOCKSET_OPERATION_ID_REUSED"})
        db.commit()
        return metadata["response"]
    result = space._apply_terrain_mutation_batch(request, world.id, batch, db, creator.user,
                                                external_build=True, commit=False)
    response = {**result, "operation_id": str(payload.operation_id), "name": canonical["name"],
                "built_blocks": len(canonical["blocks"]), "credits_charged": 0}
    receipt = db.query(models.SpaceTerrainMutationBatch).filter_by(world_id=world.id, batch_id=str(batch_id)).one()
    receipt.result = {**receipt.result, "blockset_build": {"request_digest": digest, "response": response}}
    db.commit()
    if result["chunks"]:
        from routers.space_realtime import realtime_hub
        realtime_hub.notify_terrain_from_thread(str(world.id), result["terrain_revision"])
    return response


def allowance(used, limit):
    return {"used": used, "limit": limit, "remaining": max(0, limit - used)}


@router.get("/api-usage")
@limiter.limit(entities.SPACE_ENTITY_RATE_LIMIT)
def api_usage(request: Request, world_id: uuid.UUID, db: Session = Depends(get_db),
              creator: entities.EntityCreator = Depends(entities._entity_creator)):
    user = creator.user
    world = space._require_world_membership(db, str(world_id), user)
    now = dt.datetime.now(dt.timezone.utc)
    owned = db.query(models.SpaceWorldEntity).filter_by(world_id=world.id, owner_user_id=user.id)
    terrain = {}
    for label, seconds, limit in (("hour", 3600, space.SPACE_TERRAIN_HOURLY_LIMIT),
                                  ("day", 86400, space.SPACE_TERRAIN_DAILY_LIMIT)):
        count = usage(db, principal_id=user.id, scope_id=space.SPACE_TERRAIN_USAGE_SCOPE,
                      metric="terrain_effective_changes", window_seconds=seconds, now=now)
        terrain[label] = {**allowance(count, limit),
                          "reset_at": (bucket_start(now, seconds) + dt.timedelta(seconds=seconds)).isoformat()}
    result = {
        "world_id": str(world.id), "updated_at": now.isoformat(), "credits": user.credits,
        "features": {"entity_hosting": settings.SPACE_HOSTING_ENABLED},
        "pricing": {"entity_create_credits": 0, "blockset_build_credits": 0},
        "quotas": {
            "api_keys": allowance(user.api_key_count if settings.SPACE_STANDALONE else db.query(models.SpaceApiKey).filter_by(user_id=user.id).count(), entities.SPACE_API_KEY_MAX_PER_USER),
            "entities": allowance(owned.count(), entities.SPACE_ENTITY_MAX_PER_OWNER),
            "entity_storage_bytes": allowance(entities._owned_entity_storage_bytes(db, str(world.id), user.id), entities.SPACE_ENTITY_MAX_TOTAL_BYTES_PER_OWNER),
            "running_entities": allowance(owned.filter_by(desired_run_state="running").count(), entities.SPACE_ENTITY_MAX_RUNNING_PER_OWNER),
            "terrain": terrain,
        },
        "limits": {"blockset_blocks_per_build": MAX_BUILD_BLOCKS, "blockset_definition_bytes": MAX_BUILD_BYTES,
                   "build_requests_per_minute": 30, "build_requests_per_hour": 300,
                   "entity_create_requests_per_minute": 30, "entity_create_requests_per_hour": 300,
                   "terrain_submitted_per_10_seconds": space.SPACE_TERRAIN_BURST_LIMIT,
                   "terrain_chunks_per_build": space.SPACE_TERRAIN_MAX_CHUNKS_PER_BATCH,
                   "terrain_zones_per_build": space.SPACE_TERRAIN_MAX_ZONES_PER_BATCH,
                   "build_retry_days": settings.SPACE_TERRAIN_BATCH_RECEIPT_RETENTION_DAYS},
        "admin_quota_exemptions": bool(user.is_admin),
    }
    if settings.SPACE_HOSTING_ENABLED:
        from routers import space_hosting as hosting
        result["pricing"].update(hosting_credits_per_hour=1, hosting_billing="prepaid_simulation_hour", hosting_max_budget_credits=168)
        result["quotas"]["hosted_entities_world"] = allowance(
            db.query(models.SpaceWorldEntity).filter_by(world_id=world.id, hosting_enabled=True).count(), hosting.MAX_HOSTED_PER_WORLD)
        result["limits"].update(hosted_blocks_per_entity=hosting.MAX_HOSTED_BLOCKS, hosted_components_per_entity=hosting.MAX_HOSTED_COMPONENTS)
    db.commit()
    return result
