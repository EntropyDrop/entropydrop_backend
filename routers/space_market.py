import datetime
import hashlib
import json
import math
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)
from sqlalchemy import desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only

import auth
import models
from database import get_db
from rate_limit import limiter


router = APIRouter(prefix="/space/api/v2/market", tags=["space-market"])

SPACE_MARKET_LICENSE = "AGPL-3.0-only"
SPACE_MARKET_DAILY_PUBLISH_LIMIT = 10
SPACE_MARKET_MAX_RESOURCE_BYTES = 8 * 1024 * 1024
SPACE_MARKET_MAX_BLOCKS = 65_536
SPACE_MARKET_MAX_COMPONENTS = 64
SPACE_MARKET_MAX_CONSTRAINTS = 256
SPACE_MARKET_MAX_SCRIPT_BYTES = 64 * 1024
SPACE_MARKET_MAX_TOTAL_SCRIPT_BYTES = 512 * 1024
SPACE_MARKET_MAX_BOUNDS = 64
SPACE_MARKET_MAX_COORDINATE = SPACE_MARKET_MAX_BOUNDS * 2
SPACE_MARKET_PREVIEW_BLOCKS = 64
SPACE_MARKET_RATE_LIMIT = "120/minute; 2000/hour"
COMPONENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")

Number = StrictInt | StrictFloat
Vector3 = tuple[Number, Number, Number]


class StrictResourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _finite_number(value: Any, label: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be at most {maximum}")
    return number


def _validate_vector(value: Vector3 | None, label: str, max_abs: float = 256) -> None:
    if value is None:
        return
    for component in value:
        number = _finite_number(component, label)
        if abs(number) > max_abs:
            raise ValueError(f"{label} components must be within ±{max_abs}")


def _valid_component_id(value: str, allow_root: bool = True) -> bool:
    return bool(COMPONENT_ID_PATTERN.fullmatch(value)) and (allow_root or value != "root")


class MarketVoxel(StrictResourceModel):
    dx: StrictInt
    dy: StrictInt
    dz: StrictInt
    mx: StrictInt | None = None
    my: StrictInt | None = None
    mz: StrictInt | None = None
    block: StrictInt = Field(default=1, ge=1, le=1)
    color: StrictInt = Field(ge=0, le=0xFFFFFF)
    part: StrictStr | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_grid(self):
        if any(abs(value) > SPACE_MARKET_MAX_COORDINATE for value in (self.dx, self.dy, self.dz)):
            raise ValueError("voxel coordinates are outside the portable bounds")
        micro = (self.mx, self.my, self.mz)
        if any(value is not None for value in micro):
            if any(value is None for value in micro):
                raise ValueError("micro coordinates mx/my/mz must be provided together")
            if any(not 0 <= int(value) < 5 for value in micro):
                raise ValueError("micro coordinates must be between 0 and 4")
        return self

class BlockSetPayload(StrictResourceModel):
    type: Literal["space-blockset"]
    version: Literal[2]
    name: StrictStr = Field(min_length=1, max_length=80)
    blockCount: StrictInt | None = Field(default=None, ge=1, le=SPACE_MARKET_MAX_BLOCKS)
    blocks: list[MarketVoxel] = Field(min_length=1, max_length=SPACE_MARKET_MAX_BLOCKS)

    @model_validator(mode="after")
    def validate_shape(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("resource name may not be blank")
        _validate_voxel_collection(self.blocks, lambda _block: "blockset")
        self.blockCount = len(self.blocks)
        self.blocks.sort(key=_voxel_sort_key)
        return self


class EntityVoxel(MarketVoxel):
    entityId: StrictStr = Field(min_length=1, max_length=64)


class EntityChild(StrictResourceModel):
    id: StrictStr = Field(min_length=1, max_length=64)
    parentId: StrictStr = Field(min_length=1, max_length=64)
    kind: Literal["child"] | None = None
    collisionEnabled: StrictBool | None = None
    pivot: Vector3 | None = None
    bodyType: Literal["dynamic", "kinematic"] | None = None
    mass: Number | None = None
    restitution: Number | None = None
    friction: Number | None = None

    @model_validator(mode="after")
    def validate_physics(self):
        if not _valid_component_id(self.id, allow_root=False):
            raise ValueError("child component id is not portable")
        if not _valid_component_id(self.parentId):
            raise ValueError("child parent id is not portable")
        _validate_vector(self.pivot, "component pivot", SPACE_MARKET_MAX_COORDINATE)
        if self.mass is not None:
            self.mass = _finite_number(self.mass, "component mass", 0.1, 1e12)
        if self.restitution is not None:
            self.restitution = _finite_number(self.restitution, "component restitution", 0, 1)
        if self.friction is not None:
            self.friction = _finite_number(self.friction, "component friction", 0, 1)
        return self


class EntityScript(StrictResourceModel):
    id: StrictStr = Field(min_length=1, max_length=64)
    code: StrictStr


class EntityEnabled(StrictResourceModel):
    id: StrictStr = Field(min_length=1, max_length=64)
    enabled: StrictBool


class ConstraintLimits(StrictResourceModel):
    min: Number
    max: Number

    @model_validator(mode="after")
    def validate_limits(self):
        minimum = _finite_number(self.min, "constraint minimum", -10_000, 10_000)
        maximum = _finite_number(self.max, "constraint maximum", -10_000, 10_000)
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        self.min = minimum
        self.max = maximum
        return self


class EntityConstraint(StrictResourceModel):
    id: StrictStr = Field(min_length=1, max_length=64)
    type: Literal["point", "hinge", "weld"] = "point"
    bodyA: StrictStr = Field(min_length=1, max_length=64)
    bodyB: StrictStr = Field(min_length=1, max_length=64)
    anchorA: Vector3 | None = None
    anchorB: Vector3 | None = None
    axisA: Vector3 | None = None
    axisB: Vector3 | None = None
    referenceA: Vector3 | None = None
    referenceB: Vector3 | None = None
    limits: ConstraintLimits | None = None
    stiffness: Number = 0.9
    collideConnected: StrictBool = False

    @model_validator(mode="after")
    def validate_constraint_values(self):
        if not _valid_component_id(self.id, allow_root=False):
            raise ValueError("constraint id is not portable")
        for field_name in ("anchorA", "anchorB", "axisA", "axisB", "referenceA", "referenceB"):
            _validate_vector(getattr(self, field_name), f"constraint {field_name}")
        self.stiffness = _finite_number(self.stiffness, "constraint stiffness", 0, 1)
        return self


class EntityPayload(StrictResourceModel):
    type: Literal["space-entity"]
    version: Literal[2]
    name: StrictStr = Field(min_length=1, max_length=80)
    rootId: Literal["root"] = "root"
    nodeCount: StrictInt | None = Field(default=None, ge=1, le=SPACE_MARKET_MAX_COMPONENTS)
    blockCount: StrictInt | None = Field(default=None, ge=1, le=SPACE_MARKET_MAX_BLOCKS)
    blocks: list[EntityVoxel] = Field(min_length=1, max_length=SPACE_MARKET_MAX_BLOCKS)
    childEntities: list[EntityChild] = Field(default_factory=list, max_length=SPACE_MARKET_MAX_COMPONENTS - 1)
    scripts: list[EntityScript] = Field(default_factory=list, max_length=SPACE_MARKET_MAX_COMPONENTS)
    enabled: list[EntityEnabled] = Field(default_factory=list, max_length=SPACE_MARKET_MAX_COMPONENTS)
    constraints: list[EntityConstraint] = Field(default_factory=list, max_length=SPACE_MARKET_MAX_CONSTRAINTS)
    mode: Literal["free_physics", "bearing", "piston", "drivable", "projectile", "programmable"] = "free_physics"
    bodyType: Literal["dynamic", "kinematic"] = "dynamic"
    mass: Number | None = None
    restitution: Number | None = None
    friction: Number | None = None
    useGravity: StrictBool | None = None
    bearingAxis: Vector3 | None = None
    bearingRpm: Number | None = None
    pistonAxis: Vector3 | None = None
    pistonDistance: Number | None = None
    pistonSpeed: Number | None = None
    cockpitPosition: Vector3 | None = None
    isVehicle: StrictBool | None = None

    @model_validator(mode="after")
    def validate_entity(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("resource name may not be blank")

        child_ids = [child.id for child in self.childEntities]
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("child component ids must be unique")
        known_ids = {"root", *child_ids}
        child_by_id = {child.id: child for child in self.childEntities}
        for child in self.childEntities:
            if child.parentId not in known_ids:
                raise ValueError(f"unknown parent {child.parentId} for component {child.id}")
            visited = {child.id}
            parent_id = child.parentId
            while parent_id in child_by_id:
                if parent_id in visited:
                    raise ValueError("component hierarchy contains a cycle")
                visited.add(parent_id)
                parent_id = child_by_id[parent_id].parentId

        for block in self.blocks:
            if block.entityId not in known_ids:
                raise ValueError(f"voxel references unknown component {block.entityId}")
        _validate_voxel_collection(self.blocks, lambda block: block.entityId)

        script_ids = [script.id for script in self.scripts]
        enabled_ids = [entry.id for entry in self.enabled]
        if len(set(script_ids)) != len(script_ids) or any(script_id not in known_ids for script_id in script_ids):
            raise ValueError("scripts must reference unique known components")
        if len(set(enabled_ids)) != len(enabled_ids) or any(enabled_id not in known_ids for enabled_id in enabled_ids):
            raise ValueError("enabled flags must reference unique known components")
        total_script_bytes = 0
        for script in self.scripts:
            script_bytes = len(script.code.encode("utf-8"))
            if script_bytes > SPACE_MARKET_MAX_SCRIPT_BYTES:
                raise ValueError("one component script exceeds 64 KiB")
            total_script_bytes += script_bytes
        if total_script_bytes > SPACE_MARKET_MAX_TOTAL_SCRIPT_BYTES:
            raise ValueError("entity scripts exceed 512 KiB in total")

        constraint_ids = [constraint.id for constraint in self.constraints]
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValueError("constraint ids must be unique")
        for constraint in self.constraints:
            if (
                (constraint.bodyA != "world" and constraint.bodyA not in known_ids)
                or constraint.bodyB not in known_ids
                or constraint.bodyA == constraint.bodyB
            ):
                raise ValueError(f"constraint {constraint.id} references an invalid component")

        if self.mass is not None:
            self.mass = _finite_number(self.mass, "entity mass", 0.1, 1e12)
        if self.restitution is not None:
            self.restitution = _finite_number(self.restitution, "entity restitution", 0, 1)
        if self.friction is not None:
            self.friction = _finite_number(self.friction, "entity friction", 0, 1)
        _validate_vector(self.bearingAxis, "bearing axis")
        _validate_vector(self.pistonAxis, "piston axis")
        _validate_vector(self.cockpitPosition, "cockpit position", SPACE_MARKET_MAX_COORDINATE)
        if self.bearingRpm is not None:
            self.bearingRpm = _finite_number(self.bearingRpm, "bearing rpm", -10_000, 10_000)
        if self.pistonDistance is not None:
            self.pistonDistance = _finite_number(self.pistonDistance, "piston distance", 0, SPACE_MARKET_MAX_COORDINATE)
        if self.pistonSpeed is not None:
            self.pistonSpeed = _finite_number(self.pistonSpeed, "piston speed", 0, 10_000)

        self.blockCount = len(self.blocks)
        self.nodeCount = len(known_ids)
        self.blocks.sort(key=lambda block: (block.entityId, *_voxel_sort_key(block)))
        self.childEntities.sort(key=lambda child: child.id)
        self.scripts.sort(key=lambda script: script.id)
        self.enabled.sort(key=lambda entry: entry.id)
        self.constraints.sort(key=lambda constraint: constraint.id)
        return self


class ColorSetPayload(StrictResourceModel):
    type: Literal["space-colorset"]
    version: Literal[2]
    name: StrictStr = Field(min_length=1, max_length=80)
    colors: list[StrictStr] = Field(min_length=9, max_length=9)

    @model_validator(mode="after")
    def validate_colors(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("resource name may not be blank")
        normalized = [color.lower() for color in self.colors]
        if any(not HEX_COLOR_PATTERN.fullmatch(color) for color in normalized):
            raise ValueError("colors must be six-digit #rrggbb values")
        self.colors = normalized
        return self


class SpaceMarketPublishRequest(StrictResourceModel):
    kind: Literal["blockset", "entity", "colorset"]
    payload: dict[str, Any]


def _voxel_sort_key(block: MarketVoxel) -> tuple[int, int, int, int, int, int, int, str]:
    return (
        block.dx,
        block.dy,
        block.dz,
        -1 if block.mx is None else block.mx,
        -1 if block.my is None else block.my,
        -1 if block.mz is None else block.mz,
        block.color,
        block.part or "",
    )


def _validate_voxel_collection(blocks: list[MarketVoxel], owner) -> None:
    mins = [min(getattr(block, axis) for block in blocks) for axis in ("dx", "dy", "dz")]
    maxs = [max(getattr(block, axis) for block in blocks) for axis in ("dx", "dy", "dz")]
    if any(maximum - minimum + 1 > SPACE_MARKET_MAX_BOUNDS for minimum, maximum in zip(mins, maxs)):
        raise ValueError("resource bounds exceed 64 standard cells on one axis")

    standard_cells: set[tuple[Any, ...]] = set()
    micro_cells: set[tuple[Any, ...]] = set()
    micro_parents: set[tuple[Any, ...]] = set()
    for block in blocks:
        prefix = owner(block)
        parent_key = (prefix, block.dx, block.dy, block.dz)
        if block.mx is None:
            if parent_key in standard_cells:
                raise ValueError("resource contains duplicate voxels")
            if parent_key in micro_parents:
                raise ValueError("standard and micro voxels may not share one cell")
            standard_cells.add(parent_key)
            continue
        key = (parent_key, block.mx, block.my, block.mz)
        if parent_key in standard_cells:
            raise ValueError("standard and micro voxels may not share one cell")
        if key in micro_cells:
            raise ValueError("resource contains duplicate voxels")
        micro_cells.add(key)
        micro_parents.add(parent_key)


def validate_market_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    model_type = {
        "blockset": BlockSetPayload,
        "entity": EntityPayload,
        "colorset": ColorSetPayload,
    }.get(kind)
    if model_type is None:
        raise ValueError("unsupported resource kind")
    model = model_type.model_validate(payload)
    canonical = model.model_dump(exclude_none=True)
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > SPACE_MARKET_MAX_RESOURCE_BYTES:
        raise ValueError("canonical resource exceeds 8 MiB")
    return canonical


def market_content_digest(canonical: dict[str, Any]) -> bytes:
    digest_payload = dict(canonical)
    digest_payload.pop("name", None)
    digest_payload.pop("blockCount", None)
    digest_payload.pop("nodeCount", None)
    encoded = json.dumps(digest_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _market_preview(kind: str, canonical: dict[str, Any]) -> dict[str, Any]:
    if kind == "colorset":
        return {"colors": canonical["colors"]}
    blocks = []
    for block in canonical.get("blocks", [])[:SPACE_MARKET_PREVIEW_BLOCKS]:
        micro = block.get("mx") is not None
        blocks.append({
            "x": block["dx"] + (block.get("mx", 0) / 5 if micro else 0),
            "y": block["dy"] + (block.get("my", 0) / 5 if micro else 0),
            "z": block["dz"] + (block.get("mz", 0) / 5 if micro else 0),
            "size": 0.2 if micro else 1,
            "color": block["color"],
        })
    return {"blocks": blocks}


def _utc_day_bounds(now: datetime.datetime | None = None) -> tuple[datetime.datetime, datetime.datetime]:
    current = now or datetime.datetime.now(datetime.timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + datetime.timedelta(days=1)


def _published_today(db: Session, user_id: str) -> int:
    start, end = _utc_day_bounds()
    return int(db.query(func.count(models.SpaceMarketResource.id)).filter(
        models.SpaceMarketResource.publisher_user_id == user_id,
        models.SpaceMarketResource.created_at >= start,
        models.SpaceMarketResource.created_at < end,
    ).scalar() or 0)


def _quota_response(db: Session, user_id: str) -> dict[str, int]:
    published = _published_today(db, user_id)
    return {
        "daily_limit": SPACE_MARKET_DAILY_PUBLISH_LIMIT,
        "published_today": published,
        "remaining_today": max(0, SPACE_MARKET_DAILY_PUBLISH_LIMIT - published),
    }


def _resource_response(resource: models.SpaceMarketResource, publisher: models.User | None, is_liked: bool) -> dict[str, Any]:
    return {
        "id": resource.id,
        "kind": resource.kind,
        "schema_version": resource.schema_version,
        "name": resource.name,
        "license": resource.license,
        "digest": bytes(resource.content_digest).hex(),
        "publisher": {"id": resource.publisher_user_id, "username": publisher.username if publisher else None},
        "size_bytes": resource.size_bytes,
        "block_count": resource.block_count,
        "node_count": resource.node_count,
        "script_count": resource.script_count,
        "downloads_count": resource.downloads_count,
        "likes_count": resource.likes_count,
        "is_liked": is_liked,
        "preview": resource.preview,
        "created_at": resource.created_at.isoformat(),
    }


def _validation_error_response(error: ValidationError | ValueError) -> HTTPException:
    if isinstance(error, ValidationError):
        errors = [
            {
                "path": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"],
                "type": item["type"],
            }
            for item in error.errors(include_url=False)
        ]
        message = errors[0]["message"] if errors else "resource structure is invalid"
    else:
        errors = []
        message = str(error)
    return HTTPException(status_code=422, detail={"code": "INVALID_MARKET_RESOURCE", "message": message, "errors": errors})


@router.get("/resources")
@limiter.limit(SPACE_MARKET_RATE_LIMIT)
def list_market_resources(
    request: Request,
    kind: Literal["blockset", "entity", "colorset"] | None = Query(default=None),
    sort: Literal["downloads", "likes", "latest"] = Query(default="latest"),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    filters = [models.SpaceMarketResource.deleted_at.is_(None)]
    if kind:
        filters.append(models.SpaceMarketResource.kind == kind)
    order = {
        "downloads": (desc(models.SpaceMarketResource.downloads_count), desc(models.SpaceMarketResource.created_at)),
        "likes": (desc(models.SpaceMarketResource.likes_count), desc(models.SpaceMarketResource.created_at)),
        "latest": (desc(models.SpaceMarketResource.created_at), desc(models.SpaceMarketResource.id)),
    }[sort]
    total = int(db.query(func.count(models.SpaceMarketResource.id)).filter(*filters).scalar() or 0)
    rows = db.query(models.SpaceMarketResource, models.User).options(load_only(
        models.SpaceMarketResource.id,
        models.SpaceMarketResource.publisher_user_id,
        models.SpaceMarketResource.kind,
        models.SpaceMarketResource.schema_version,
        models.SpaceMarketResource.name,
        models.SpaceMarketResource.license,
        models.SpaceMarketResource.content_digest,
        models.SpaceMarketResource.preview,
        models.SpaceMarketResource.size_bytes,
        models.SpaceMarketResource.block_count,
        models.SpaceMarketResource.node_count,
        models.SpaceMarketResource.script_count,
        models.SpaceMarketResource.downloads_count,
        models.SpaceMarketResource.likes_count,
        models.SpaceMarketResource.created_at,
    )).outerjoin(
        models.User,
        models.User.id == models.SpaceMarketResource.publisher_user_id,
    ).filter(*filters).order_by(*order).offset(offset).limit(limit).all()
    resource_ids = [resource.id for resource, _publisher in rows]
    liked_ids = set()
    if resource_ids:
        liked_ids = {
            row[0]
            for row in db.query(models.SpaceMarketResourceLike.resource_id).filter(
                models.SpaceMarketResourceLike.user_id == current_user.id,
                models.SpaceMarketResourceLike.resource_id.in_(resource_ids),
            ).all()
        }
    return {
        "items": [_resource_response(resource, publisher, resource.id in liked_ids) for resource, publisher in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "quota": _quota_response(db, current_user.id),
    }


@router.post("/resources", status_code=201)
@limiter.limit(SPACE_MARKET_RATE_LIMIT)
def publish_market_resource(
    request: Request,
    publish_request: SpaceMarketPublishRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    try:
        canonical = validate_market_payload(publish_request.kind, publish_request.payload)
    except (ValidationError, ValueError) as error:
        raise _validation_error_response(error) from error

    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = market_content_digest(canonical)
    db.query(models.User).filter(models.User.id == current_user.id).with_for_update().first()
    existing = db.query(models.SpaceMarketResource).filter(models.SpaceMarketResource.content_digest == digest).first()
    if existing:
        raise HTTPException(status_code=409, detail={
            "code": "RESOURCE_ALREADY_PUBLISHED",
            "message": "An identical canonical resource is already in the market.",
            "resource_id": existing.id,
        })
    if _published_today(db, current_user.id) >= SPACE_MARKET_DAILY_PUBLISH_LIMIT:
        raise HTTPException(status_code=429, detail={
            "code": "DAILY_PUBLISH_LIMIT_REACHED",
            "message": "The daily market publication limit is 10.",
            "daily_limit": SPACE_MARKET_DAILY_PUBLISH_LIMIT,
        })

    resource = models.SpaceMarketResource(
        publisher_user_id=current_user.id,
        kind=publish_request.kind,
        schema_version=2,
        name=canonical["name"],
        license=SPACE_MARKET_LICENSE,
        content_digest=digest,
        content=canonical,
        preview=_market_preview(publish_request.kind, canonical),
        size_bytes=len(encoded),
        block_count=len(canonical.get("blocks", [])),
        node_count=int(canonical.get("nodeCount", 0)),
        script_count=len(canonical.get("scripts", [])),
        downloads_count=0,
        likes_count=0,
    )
    db.add(resource)
    try:
        db.commit()
        db.refresh(resource)
    except IntegrityError as error:
        db.rollback()
        duplicate = db.query(models.SpaceMarketResource).filter(models.SpaceMarketResource.content_digest == digest).first()
        if duplicate:
            raise HTTPException(status_code=409, detail={
                "code": "RESOURCE_ALREADY_PUBLISHED",
                "message": "An identical canonical resource is already in the market.",
                "resource_id": duplicate.id,
            }) from error
        raise
    return {"resource": _resource_response(resource, current_user, False), "quota": _quota_response(db, current_user.id)}


@router.get("/resources/{resource_id}/download")
@limiter.limit(SPACE_MARKET_RATE_LIMIT)
def download_market_resource(
    request: Request,
    resource_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    resource = db.query(models.SpaceMarketResource).filter(
        models.SpaceMarketResource.id == resource_id,
        models.SpaceMarketResource.deleted_at.is_(None),
    ).first()
    if resource is None:
        raise HTTPException(status_code=404, detail={"code": "MARKET_RESOURCE_NOT_FOUND"})
    db.query(models.SpaceMarketResource).filter(models.SpaceMarketResource.id == resource.id).update({
        models.SpaceMarketResource.downloads_count: models.SpaceMarketResource.downloads_count + 1,
    }, synchronize_session=False)
    db.commit()
    db.refresh(resource)
    return {
        "id": resource.id,
        "kind": resource.kind,
        "name": resource.name,
        "license": resource.license,
        "digest": bytes(resource.content_digest).hex(),
        "downloads_count": resource.downloads_count,
        "payload": resource.content,
    }


@router.post("/resources/{resource_id}/like")
@limiter.limit(SPACE_MARKET_RATE_LIMIT)
def toggle_market_resource_like(
    request: Request,
    resource_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    resource = db.query(models.SpaceMarketResource).filter(
        models.SpaceMarketResource.id == resource_id,
        models.SpaceMarketResource.deleted_at.is_(None),
    ).with_for_update().first()
    if resource is None:
        raise HTTPException(status_code=404, detail={"code": "MARKET_RESOURCE_NOT_FOUND"})
    like = db.query(models.SpaceMarketResourceLike).filter(
        models.SpaceMarketResourceLike.resource_id == resource.id,
        models.SpaceMarketResourceLike.user_id == current_user.id,
    ).first()
    if like:
        db.delete(like)
        resource.likes_count = max(0, int(resource.likes_count or 0) - 1)
        liked = False
    else:
        db.add(models.SpaceMarketResourceLike(resource_id=resource.id, user_id=current_user.id))
        resource.likes_count = int(resource.likes_count or 0) + 1
        liked = True
    db.commit()
    return {"is_liked": liked, "likes_count": resource.likes_count}


@router.delete("/resources/{resource_id}")
@limiter.limit(SPACE_MARKET_RATE_LIMIT)
def admin_delete_market_resource(
    request: Request,
    resource_id: str,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(auth.get_current_admin),
):
    resource = db.query(models.SpaceMarketResource).filter(
        models.SpaceMarketResource.id == resource_id,
        models.SpaceMarketResource.deleted_at.is_(None),
    ).with_for_update().first()
    if resource is None:
        raise HTTPException(status_code=404, detail={"code": "MARKET_RESOURCE_NOT_FOUND"})
    resource.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    resource.deleted_by_user_id = current_admin.id
    db.commit()
    return {"deleted": True, "resource_id": resource.id}
