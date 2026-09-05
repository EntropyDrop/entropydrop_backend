import datetime
import logging
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
import s3_utils
from config import settings
from database import get_db
from rate_limit import limiter
from space.inventory_codec import (
    SCHEMA_VERSION as INVENTORY_SCHEMA_VERSION,
    InventoryCodecError,
    decode_inventory_resource,
    encode_inventory_resource,
    inventory_content_digest,
)
from space_quota import (
    QuotaWindow,
    UTC_DAY_SECONDS,
    ensure_usage_floor,
    reserve as reserve_quota,
    usage as quota_usage,
)


router = APIRouter(prefix="/space/api/v2/market", tags=["space-market"])
logger = logging.getLogger(__name__)

SPACE_MARKET_LICENSE = "AGPL-3.0-only"
SPACE_MARKET_DAILY_PUBLISH_LIMIT = 10
SPACE_MARKET_MAX_RESOURCE_BYTES = 8 * 1024 * 1024
SPACE_MARKET_MAX_BLOCKS = 65_536
SPACE_MARKET_MAX_COMPONENTS = 64
SPACE_MARKET_MAX_CONSTRAINTS = 256
SPACE_MARKET_MAX_SCRIPT_BYTES = 64 * 1024
SPACE_MARKET_MAX_TOTAL_SCRIPT_BYTES = 512 * 1024
SPACE_MARKET_MAX_SEATS = 256
SPACE_MARKET_MAX_COMPONENT_DEPTH = 16
SPACE_MARKET_MAX_BOUNDS = 64
SPACE_MARKET_MAX_COORDINATE = SPACE_MARKET_MAX_BOUNDS * 2
SPACE_MARKET_GRID_DIVISIONS = 5
SPACE_MARKET_GRID_EPSILON = 1e-6
SPACE_MARKET_RATE_LIMIT = "120/minute; 2000/hour"
SPACE_MARKET_OBJECT_PREFIX = "space-market/resources"
SPACE_MARKET_MAX_RESOURCES_PER_OWNER = settings.SPACE_MARKET_MAX_RESOURCES_PER_OWNER
SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER = settings.SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER
SPACE_MARKET_DAILY_UPLOAD_BYTES = settings.SPACE_MARKET_DAILY_UPLOAD_BYTES
SPACE_MARKET_USAGE_SCOPE = "market"
COMPONENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")

Number = StrictInt | StrictFloat
Vector3 = tuple[Number, Number, Number]
Quaternion = tuple[Number, Number, Number, Number]
RotationMatrix = tuple[
    tuple[int, int, int],
    tuple[int, int, int],
    tuple[int, int, int],
]


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
    return 0.0 if number == 0.0 else number


def _validate_vector(value: Vector3 | None, label: str, max_abs: float = 256) -> tuple[float, float, float] | None:
    if value is None:
        return None
    normalized = tuple(_finite_number(component, label) for component in value)
    for component in normalized:
        if abs(component) > max_abs:
            raise ValueError(f"{label} components must be within ±{max_abs}")
    return normalized


def _grid_rotation_matrix(value: Quaternion | None, label: str) -> RotationMatrix:
    if value is None:
        return ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    components = [_finite_number(component, label, -1, 1) for component in value]
    length_squared = sum(component * component for component in components)
    if not math.isclose(length_squared, 1, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError(f"{label} must be a normalized quaternion")
    length = math.sqrt(length_squared)
    x, y, z, w = (component / length for component in components)
    matrix = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    snapped: list[tuple[int, int, int]] = []
    for row in matrix:
        snapped_row = tuple(round(component) for component in row)
        if any(
            component not in (-1, 0, 1)
            or not math.isclose(original, component, rel_tol=0, abs_tol=SPACE_MARKET_GRID_EPSILON)
            for original, component in zip(row, snapped_row)
        ):
            raise ValueError(f"{label} must be one of the 24 axis-aligned 90-degree rotations")
        snapped.append(snapped_row)
    return tuple(snapped)  # type: ignore[return-value]


def _validate_quaternion(value: Quaternion | None, label: str) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    _grid_rotation_matrix(value, label)
    return tuple(_finite_number(component, label, -1, 1) for component in value)


def _valid_component_id(value: str) -> bool:
    return bool(COMPONENT_ID_PATTERN.fullmatch(value))


def _valid_constraint_id(value: str) -> bool:
    # Constraint ids have their own namespace and never identify endpoints.
    return bool(COMPONENT_ID_PATTERN.fullmatch(value))


class MarketVoxel(StrictResourceModel):
    dx: StrictInt
    dy: StrictInt
    dz: StrictInt
    mx: StrictInt | None = None
    my: StrictInt | None = None
    mz: StrictInt | None = None
    block: StrictInt = Field(default=1, ge=1, le=1)
    color: StrictInt = Field(ge=0, le=0xFFFFFF)

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
    version: Literal[4]
    name: StrictStr = Field(min_length=1, max_length=80)
    blocks: list[MarketVoxel] = Field(min_length=1, max_length=SPACE_MARKET_MAX_BLOCKS)

    @model_validator(mode="after")
    def validate_shape(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("resource name may not be blank")
        _validate_voxel_collection(self.blocks, lambda _block: "blockset")
        self.blocks.sort(key=_voxel_sort_key)
        return self


class ComponentBody(StrictResourceModel):
    type: Literal["dynamic", "kinematic"]
    mass: Number | None = None
    restitution: Number | None = None
    friction: Number | None = None
    useGravity: StrictBool | None = None
    collisionEnabled: StrictBool | None = None

    @model_validator(mode="after")
    def validate_physics(self):
        if self.mass is not None:
            self.mass = _finite_number(self.mass, "component mass", 0.1, 1e12)
        if self.restitution is not None:
            self.restitution = _finite_number(self.restitution, "component restitution", 0, 1)
        if self.friction is not None:
            self.friction = _finite_number(self.friction, "component friction", 0, 1)
        return self


class ComponentSeat(StrictResourceModel):
    position: Vector3

    @model_validator(mode="after")
    def validate_position(self):
        self.position = _validate_vector(
            self.position,
            "seat position",
            SPACE_MARKET_MAX_COORDINATE,
        )
        return self


class EntityComponent(StrictResourceModel):
    id: StrictStr = Field(min_length=1, max_length=64)
    pivot: Vector3 | None = None
    localPosition: Vector3 | None = None
    localRotation: Quaternion | None = None
    anchorRotation: Quaternion | None = None
    body: ComponentBody
    blocks: list[MarketVoxel] = Field(default_factory=list, max_length=SPACE_MARKET_MAX_BLOCKS)
    script: StrictStr | None = None
    scriptDisabled: StrictBool = False
    seats: list[ComponentSeat] = Field(default_factory=list, max_length=SPACE_MARKET_MAX_SEATS)
    children: list["EntityComponent"] = Field(default_factory=list, max_length=SPACE_MARKET_MAX_COMPONENTS - 1)

    @model_validator(mode="after")
    def validate_component(self):
        if not _valid_component_id(self.id):
            raise ValueError("component id is not portable")
        self.pivot = _validate_vector(self.pivot, "component pivot", SPACE_MARKET_MAX_COORDINATE)
        self.localPosition = _validate_vector(
            self.localPosition,
            "component local position",
            SPACE_MARKET_MAX_COORDINATE,
        )
        self.localRotation = _validate_quaternion(self.localRotation, "component local rotation")
        self.anchorRotation = _validate_quaternion(self.anchorRotation, "component anchor rotation")
        if self.script is not None and len(self.script.encode("utf-8")) > SPACE_MARKET_MAX_SCRIPT_BYTES:
            raise ValueError("one component script exceeds 64 KiB")
        if self.blocks:
            _validate_voxel_collection(self.blocks, lambda _block: self.id)
            self.blocks.sort(key=_voxel_sort_key)
        self.children.sort(key=lambda child: child.id)
        return self


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
    bodyA: StrictStr | None = Field(default=None, min_length=1, max_length=64)
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
        if not _valid_constraint_id(self.id):
            raise ValueError("constraint id is not portable")
        if self.bodyA is not None and not _valid_component_id(self.bodyA):
            raise ValueError("constraint bodyA component id is not portable")
        if not _valid_component_id(self.bodyB):
            raise ValueError("constraint bodyB component id is not portable")
        for field_name in ("anchorA", "anchorB", "axisA", "axisB", "referenceA", "referenceB"):
            setattr(
                self,
                field_name,
                _validate_vector(getattr(self, field_name), f"constraint {field_name}"),
            )
        self.stiffness = _finite_number(self.stiffness, "constraint stiffness", 0, 1)
        return self


class EntityPayload(StrictResourceModel):
    type: Literal["space-entity"]
    version: Literal[4]
    name: StrictStr = Field(min_length=1, max_length=80)
    root: EntityComponent
    constraints: list[EntityConstraint] = Field(default_factory=list, max_length=SPACE_MARKET_MAX_CONSTRAINTS)

    @model_validator(mode="after")
    def validate_entity(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("resource name may not be blank")

        if self.root.localPosition is not None or self.root.localRotation is not None:
            raise ValueError("entity root may not have a parent-relative transform")
        known_ids: set[str] = set()
        total_blocks = 0
        total_script_bytes = 0
        total_seats = 0

        def visit(component: EntityComponent, depth: int) -> None:
            nonlocal total_blocks, total_script_bytes, total_seats
            if depth > SPACE_MARKET_MAX_COMPONENT_DEPTH:
                raise ValueError("component hierarchy exceeds maximum depth 16")
            if component.id in known_ids:
                raise ValueError("component ids must be unique across the entity")
            known_ids.add(component.id)
            total_blocks += len(component.blocks)
            total_seats += len(component.seats)
            if component.script is not None:
                total_script_bytes += len(component.script.encode("utf-8"))
            for child in component.children:
                visit(child, depth + 1)

        visit(self.root, 0)
        if len(known_ids) > SPACE_MARKET_MAX_COMPONENTS:
            raise ValueError("entity exceeds 64 components")
        if total_blocks < 1 or total_blocks > SPACE_MARKET_MAX_BLOCKS:
            raise ValueError("entity must contain between 1 and 65536 voxels")
        if total_script_bytes > SPACE_MARKET_MAX_TOTAL_SCRIPT_BYTES:
            raise ValueError("entity scripts exceed 512 KiB in total")
        if total_seats > SPACE_MARKET_MAX_SEATS:
            raise ValueError("entity exceeds 256 seats")
        _validate_stopped_entity_grid(self.root)

        constraint_ids = [constraint.id for constraint in self.constraints]
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValueError("constraint ids must be unique")
        for constraint in self.constraints:
            if (
                (constraint.bodyA is not None and constraint.bodyA not in known_ids)
                or constraint.bodyB not in known_ids
                or constraint.bodyA == constraint.bodyB
            ):
                raise ValueError(f"constraint {constraint.id} references an invalid component")

        self.constraints.sort(key=lambda constraint: constraint.id)
        return self


class ColorSetPayload(StrictResourceModel):
    type: Literal["space-colorset"]
    version: Literal[4]
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


def _voxel_sort_key(block: MarketVoxel) -> tuple[int, int, int, int, int, int, int]:
    return (
        block.dx,
        block.dy,
        block.dz,
        -1 if block.mx is None else block.mx,
        -1 if block.my is None else block.my,
        -1 if block.mz is None else block.mz,
        block.color,
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


def _matrix_multiply(left: RotationMatrix, right: RotationMatrix) -> RotationMatrix:
    return tuple(
        tuple(sum(left[row][inner] * right[inner][column] for inner in range(3)) for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def _rotate_vector(matrix: RotationMatrix, vector: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(
        sum(matrix[row][column] * vector[column] for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def _add_vectors(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(left[index] + right[index] for index in range(3))  # type: ignore[return-value]


def _voxel_bounds(block: MarketVoxel) -> tuple[tuple[float, float, float], float]:
    micro = block.mx is not None
    offsets = (block.mx, block.my, block.mz) if micro else (0, 0, 0)
    minimum = tuple(
        float(value) + float(offset or 0) / SPACE_MARKET_GRID_DIVISIONS
        for value, offset in zip((block.dx, block.dy, block.dz), offsets)
    )
    return minimum, 1 / SPACE_MARKET_GRID_DIVISIONS if micro else 1.0  # type: ignore[return-value]


def _validate_stopped_entity_grid(
    root: EntityComponent,
) -> list[tuple[int, int, int, int, int, int]]:
    """Require the authored Stop pose to be a non-overlapping micro-grid assembly."""
    components: list[EntityComponent] = []

    def collect(component: EntityComponent) -> None:
        components.append(component)
        for child in component.children:
            collect(child)

    collect(root)
    raw_bounds = [_voxel_bounds(block) for component in components for block in component.blocks]
    minimum = tuple(min(bounds[0][axis] for bounds in raw_bounds) for axis in range(3))
    maximum = tuple(max(bounds[0][axis] + bounds[1] for bounds in raw_bounds) for axis in range(3))
    default_root_pivot = tuple((minimum[axis] + maximum[axis]) / 2 for axis in range(3))
    root_pivot = tuple(float(value) for value in (root.pivot or default_root_pivot))
    identity: RotationMatrix = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    grid_boxes: list[tuple[int, int, int, int, int, int]] = []

    def append_blocks(
        component: EntityComponent,
        pivot: tuple[float, float, float],
        rotation: RotationMatrix,
        translation: tuple[float, float, float],
    ) -> None:
        for block in component.blocks:
            block_minimum, size = _voxel_bounds(block)
            center = tuple(block_minimum[axis] + size / 2 for axis in range(3))
            relative_center = tuple(center[axis] - pivot[axis] for axis in range(3))
            stopped_center = _add_vectors(translation, _rotate_vector(rotation, relative_center))
            fine_bounds = [
                (stopped_center[axis] - size / 2) * SPACE_MARKET_GRID_DIVISIONS
                for axis in range(3)
            ] + [
                (stopped_center[axis] + size / 2) * SPACE_MARKET_GRID_DIVISIONS
                for axis in range(3)
            ]
            snapped = [round(value) for value in fine_bounds]
            if any(
                not math.isclose(value, grid_value, rel_tol=0, abs_tol=SPACE_MARKET_GRID_EPSILON)
                for value, grid_value in zip(fine_bounds, snapped)
            ):
                raise ValueError("stopped entity voxels must align to the 0.2-unit construction grid")
            grid_boxes.append((snapped[0], snapped[1], snapped[2], snapped[3], snapped[4], snapped[5]))

    def visit(
        component: EntityComponent,
        parent_pivot: tuple[float, float, float],
        parent_rotation: RotationMatrix,
        parent_translation: tuple[float, float, float],
    ) -> None:
        pivot = tuple(float(value) for value in (component.pivot or default_root_pivot))
        default_position = tuple(pivot[axis] - parent_pivot[axis] for axis in range(3))
        local_position = tuple(float(value) for value in (component.localPosition or default_position))
        local_rotation = _grid_rotation_matrix(component.localRotation, "component local rotation")
        rotation = _matrix_multiply(parent_rotation, local_rotation)
        translation = _add_vectors(parent_translation, _rotate_vector(parent_rotation, local_position))
        append_blocks(component, pivot, rotation, translation)
        for child in component.children:
            visit(child, pivot, rotation, translation)

    append_blocks(root, root_pivot, identity, root_pivot)
    for child in root.children:
        visit(child, root_pivot, identity, root_pivot)

    buckets: dict[tuple[int, int, int], list[tuple[int, int, int, int, int, int]]] = {}
    for box in grid_boxes:
        min_x, min_y, min_z, max_x, max_y, max_z = box
        keys = [
            (x, y, z)
            for x in range(min_x // SPACE_MARKET_GRID_DIVISIONS, (max_x - 1) // SPACE_MARKET_GRID_DIVISIONS + 1)
            for y in range(min_y // SPACE_MARKET_GRID_DIVISIONS, (max_y - 1) // SPACE_MARKET_GRID_DIVISIONS + 1)
            for z in range(min_z // SPACE_MARKET_GRID_DIVISIONS, (max_z - 1) // SPACE_MARKET_GRID_DIVISIONS + 1)
        ]
        for key in keys:
            for other in buckets.get(key, []):
                if (
                    min_x < other[3] and max_x > other[0]
                    and min_y < other[4] and max_y > other[1]
                    and min_z < other[5] and max_z > other[2]
                ):
                    raise ValueError("stopped entity components contain overlapping voxels")
        for key in keys:
            buckets.setdefault(key, []).append(box)
    return grid_boxes


def entity_stopped_y_bounds(canonical: dict[str, Any]) -> tuple[float, float]:
    """Return the validated entity's stopped-pose Y bounds relative to its construction origin."""
    entity = EntityPayload.model_validate(canonical)
    grid_boxes = _validate_stopped_entity_grid(entity.root)
    return (
        min(box[1] for box in grid_boxes) / SPACE_MARKET_GRID_DIVISIONS,
        max(box[4] for box in grid_boxes) / SPACE_MARKET_GRID_DIVISIONS,
    )


def validate_inventory_resource_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    model_type = {
        "blockset": BlockSetPayload,
        "entity": EntityPayload,
        "colorset": ColorSetPayload,
    }.get(kind)
    if model_type is None:
        raise ValueError("unsupported resource kind")
    model = model_type.model_validate(payload)
    canonical = model.model_dump(exclude_none=True)
    if isinstance(model, EntityPayload):
        for canonical_constraint, constraint in zip(canonical["constraints"], model.constraints):
            canonical_constraint["bodyA"] = constraint.bodyA
    encoded = encode_inventory_resource(kind, canonical)
    if len(encoded) > SPACE_MARKET_MAX_RESOURCE_BYTES:
        raise ValueError("canonical resource exceeds 8 MiB")
    return canonical


def market_content_digest(kind: str, canonical: dict[str, Any]) -> bytes:
    return inventory_content_digest(kind, canonical)


def entity_resource_metrics(canonical: dict[str, Any]) -> tuple[int, int, int]:
    """Return block, component and scripted-component counts for one entity."""
    block_count = 0
    node_count = 0
    script_count = 0

    def visit(component: dict[str, Any]) -> None:
        nonlocal block_count, node_count, script_count
        node_count += 1
        block_count += len(component.get("blocks", []))
        script_count += int(component.get("script") is not None)
        for child in component.get("children", []):
            visit(child)

    visit(canonical["root"])
    return block_count, node_count, script_count


def _market_object_key(resource_id: str, digest: bytes) -> str:
    return f"{SPACE_MARKET_OBJECT_PREFIX}/{resource_id}/{digest.hex()}.pb"


def _upload_market_object(encoded: bytes, object_key: str) -> None:
    s3_utils.upload_to_s3(
        encoded,
        object_key,
        is_public=True,
        content_type="application/x-protobuf",
    )


def _market_storage_error(action: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "MARKET_STORAGE_UNAVAILABLE",
            "message": f"Market object storage could not {action} the resource.",
        },
    )


def _is_missing_market_object(error: Exception) -> bool:
    response = getattr(error, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return str(code) in {"404", "NoSuchKey", "NotFound"}


def _cleanup_unreferenced_market_object(db: Session, object_key: str) -> None:
    """Best-effort compensation after a database write fails."""
    try:
        referenced = db.query(models.SpaceMarketResource.id).filter(
            models.SpaceMarketResource.object_key == object_key,
        ).first()
    except Exception:
        logger.exception("Could not verify whether market object %s is referenced", object_key)
        return
    if referenced:
        return
    try:
        s3_utils.delete_from_s3_strict(object_key, is_public=True)
    except Exception:
        logger.exception("Could not remove unreferenced market object %s", object_key)


def _utc_day_bounds(now: datetime.datetime | None = None) -> tuple[datetime.datetime, datetime.datetime]:
    current = now or datetime.datetime.now(datetime.timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + datetime.timedelta(days=1)


def _live_publications_today(db: Session, user_id: str) -> int:
    start, end = _utc_day_bounds()
    return int(db.query(func.count(models.SpaceMarketResource.id)).filter(
        models.SpaceMarketResource.publisher_user_id == user_id,
        models.SpaceMarketResource.created_at >= start,
        models.SpaceMarketResource.created_at < end,
    ).scalar() or 0)


def _live_publication_bytes_today(db: Session, user_id: str) -> int:
    start, end = _utc_day_bounds()
    return int(db.query(func.coalesce(func.sum(models.SpaceMarketResource.size_bytes), 0)).filter(
        models.SpaceMarketResource.publisher_user_id == user_id,
        models.SpaceMarketResource.created_at >= start,
        models.SpaceMarketResource.created_at < end,
    ).scalar() or 0)


def _published_today(db: Session, user_id: str) -> int:
    metered = quota_usage(
        db,
        principal_id=user_id,
        scope_id=SPACE_MARKET_USAGE_SCOPE,
        metric="market_publications",
        window_seconds=UTC_DAY_SECONDS,
    )
    return max(metered, _live_publications_today(db, user_id))


def _quota_response(db: Session, user_id: str) -> dict[str, int]:
    published = _published_today(db, user_id)
    return {
        "daily_limit": SPACE_MARKET_DAILY_PUBLISH_LIMIT,
        "published_today": published,
        "remaining_today": max(0, SPACE_MARKET_DAILY_PUBLISH_LIMIT - published),
    }


def _can_delete_market_resource(resource: models.SpaceMarketResource, user: models.User) -> bool:
    return resource.publisher_user_id == user.id or bool(user.is_admin)


def _resource_response(
    resource: models.SpaceMarketResource,
    publisher: models.User | None,
    is_liked: bool,
    current_user: models.User,
) -> dict[str, Any]:
    return {
        "id": resource.id,
        "kind": resource.kind,
        "schema_version": resource.schema_version,
        "name": resource.name,
        "license": resource.license,
        "digest": bytes(resource.content_digest).hex(),
        "content_url": s3_utils.get_cdn_url(resource.object_key),
        "publisher": {"id": resource.publisher_user_id, "username": publisher.username if publisher else None},
        "size_bytes": resource.size_bytes,
        "block_count": resource.block_count,
        "node_count": resource.node_count,
        "script_count": resource.script_count,
        "downloads_count": resource.downloads_count,
        "likes_count": resource.likes_count,
        "is_liked": is_liked,
        "can_delete": _can_delete_market_resource(resource, current_user),
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
    mine: bool = Query(default=False),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    filters = []
    if kind:
        filters.append(models.SpaceMarketResource.kind == kind)
    if mine:
        filters.append(models.SpaceMarketResource.publisher_user_id == current_user.id)
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
        models.SpaceMarketResource.object_key,
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
        "items": [
            _resource_response(resource, publisher, resource.id in liked_ids, current_user)
            for resource, publisher in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "quota": _quota_response(db, current_user.id),
    }


@router.post("/resources", status_code=201)
@limiter.limit(SPACE_MARKET_RATE_LIMIT)
async def publish_market_resource(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/x-protobuf":
        raise HTTPException(status_code=415, detail={
            "code": "MARKET_PROTOBUF_REQUIRED",
            "message": "Market resources must be uploaded as application/x-protobuf.",
        })
    encoded_request = await request.body()
    if not encoded_request or len(encoded_request) > SPACE_MARKET_MAX_RESOURCE_BYTES:
        raise HTTPException(status_code=413, detail={
            "code": "MARKET_RESOURCE_TOO_LARGE",
            "message": "Market resource must be a non-empty Protobuf message no larger than 8 MiB.",
        })
    try:
        kind, decoded = decode_inventory_resource(encoded_request)
        canonical = validate_inventory_resource_payload(kind, decoded)
    except (InventoryCodecError, ValidationError, ValueError) as error:
        raise _validation_error_response(error) from error

    encoded = encode_inventory_resource(kind, canonical)
    digest = market_content_digest(kind, canonical)
    if kind == "entity":
        block_count, node_count, script_count = entity_resource_metrics(canonical)
    else:
        block_count = len(canonical.get("blocks", []))
        node_count = 0
        script_count = 0
    db.query(models.User).filter(models.User.id == current_user.id).with_for_update().first()
    existing = db.query(models.SpaceMarketResource).filter(models.SpaceMarketResource.content_digest == digest).first()
    if existing:
        raise HTTPException(status_code=409, detail={
            "code": "RESOURCE_ALREADY_PUBLISHED",
            "message": "An identical canonical resource is already in the market.",
            "resource_id": existing.id,
        })
    owned_count, owned_bytes = db.query(
        func.count(models.SpaceMarketResource.id),
        func.coalesce(func.sum(models.SpaceMarketResource.size_bytes), 0),
    ).filter(
        models.SpaceMarketResource.publisher_user_id == current_user.id,
    ).one()
    if int(owned_count or 0) >= SPACE_MARKET_MAX_RESOURCES_PER_OWNER:
        raise HTTPException(status_code=429, detail={
            "code": "MARKET_RESOURCE_COUNT_QUOTA_REACHED",
            "message": "The account's market resource count allowance has been reached.",
            "limit": SPACE_MARKET_MAX_RESOURCES_PER_OWNER,
        })
    if int(owned_bytes or 0) + len(encoded) > SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER:
        raise HTTPException(status_code=429, detail={
            "code": "MARKET_STORAGE_QUOTA_REACHED",
            "message": "The account's market storage allowance has been reached.",
            "limit_bytes": SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER,
            "used_bytes": int(owned_bytes or 0),
            "requested_bytes": len(encoded),
        })
    try:
        ensure_usage_floor(
            db,
            principal_id=current_user.id,
            scope_id=SPACE_MARKET_USAGE_SCOPE,
            metric="market_publications",
            window_seconds=UTC_DAY_SECONDS,
            floor=_live_publications_today(db, current_user.id),
        )
        ensure_usage_floor(
            db,
            principal_id=current_user.id,
            scope_id=SPACE_MARKET_USAGE_SCOPE,
            metric="market_upload_bytes",
            window_seconds=UTC_DAY_SECONDS,
            floor=_live_publication_bytes_today(db, current_user.id),
        )
        reserve_quota(
            db,
            principal_id=current_user.id,
            scope_id=SPACE_MARKET_USAGE_SCOPE,
            metric="market_publications",
            amount=1,
            windows=(QuotaWindow(
                UTC_DAY_SECONDS,
                SPACE_MARKET_DAILY_PUBLISH_LIMIT,
                "utc_day",
            ),),
            code="DAILY_PUBLISH_LIMIT_REACHED",
            message="The daily market publication limit is 10.",
        )
        reserve_quota(
            db,
            principal_id=current_user.id,
            scope_id=SPACE_MARKET_USAGE_SCOPE,
            metric="market_upload_bytes",
            amount=len(encoded),
            windows=(QuotaWindow(
                UTC_DAY_SECONDS,
                SPACE_MARKET_DAILY_UPLOAD_BYTES,
                "utc_day",
            ),),
            code="MARKET_DAILY_UPLOAD_QUOTA_REACHED",
            message="The daily market upload-byte allowance has been reached.",
        )
    except HTTPException:
        db.rollback()
        raise

    resource_id = models.generate_base58_id()
    object_key = _market_object_key(resource_id, digest)
    try:
        _upload_market_object(encoded, object_key)
    except Exception as error:
        db.rollback()
        logger.exception("Could not upload market resource %s", resource_id)
        raise _market_storage_error("store") from error

    resource = models.SpaceMarketResource(
        id=resource_id,
        publisher_user_id=current_user.id,
        kind=kind,
        schema_version=INVENTORY_SCHEMA_VERSION,
        name=canonical["name"],
        license=SPACE_MARKET_LICENSE,
        content_digest=digest,
        object_key=object_key,
        size_bytes=len(encoded),
        block_count=block_count,
        node_count=node_count,
        script_count=script_count,
        downloads_count=0,
        likes_count=0,
    )
    db.add(resource)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        _cleanup_unreferenced_market_object(db, object_key)
        duplicate = db.query(models.SpaceMarketResource).filter(models.SpaceMarketResource.content_digest == digest).first()
        if duplicate:
            raise HTTPException(status_code=409, detail={
                "code": "RESOURCE_ALREADY_PUBLISHED",
                "message": "An identical canonical resource is already in the market.",
                "resource_id": duplicate.id,
            }) from error
        raise
    except Exception:
        db.rollback()
        _cleanup_unreferenced_market_object(db, object_key)
        raise
    db.refresh(resource)
    return {
        "resource": _resource_response(resource, current_user, False, current_user),
        "quota": _quota_response(db, current_user.id),
    }


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
    ).first()
    if resource is None:
        raise HTTPException(status_code=404, detail={"code": "MARKET_RESOURCE_NOT_FOUND"})

    if not resource.object_key:
        raise _market_storage_error("load")

    db.query(models.SpaceMarketResource).filter(models.SpaceMarketResource.id == resource.id).update({
        models.SpaceMarketResource.downloads_count: models.SpaceMarketResource.downloads_count + 1,
    }, synchronize_session=False)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(resource)
    return {
        "id": resource.id,
        "kind": resource.kind,
        "name": resource.name,
        "license": resource.license,
        "digest": bytes(resource.content_digest).hex(),
        "downloads_count": resource.downloads_count,
        "download_url": s3_utils.get_cdn_url(resource.object_key),
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
def delete_market_resource(
    request: Request,
    resource_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    resource = db.query(models.SpaceMarketResource).filter(
        models.SpaceMarketResource.id == resource_id,
    ).with_for_update().first()
    if resource is None:
        raise HTTPException(status_code=404, detail={"code": "MARKET_RESOURCE_NOT_FOUND"})
    if not _can_delete_market_resource(resource, current_user):
        raise HTTPException(status_code=403, detail={
            "code": "MARKET_DELETE_FORBIDDEN",
            "message": "Only the publisher or an administrator may delete this market resource.",
        })

    backup_content = None
    object_deleted = False
    invalidation_requested = False
    object_key = resource.object_key
    if object_key:
        try:
            backup_content = s3_utils.download_from_s3(object_key, is_public=True)
        except Exception as error:
            if not _is_missing_market_object(error):
                logger.exception("Could not read market resource %s before deletion", resource.id)
                raise _market_storage_error("read before deleting") from error
        try:
            invalidation_requested = s3_utils.invalidate_cdn_object(object_key)
            s3_utils.delete_from_s3_strict(object_key, is_public=True)
            object_deleted = True
        except Exception as error:
            logger.exception("Could not delete market resource object %s", resource.id)
            raise _market_storage_error("delete") from error

    db.query(models.SpaceMarketResourceLike).filter(
        models.SpaceMarketResourceLike.resource_id == resource.id,
    ).delete(synchronize_session=False)
    db.delete(resource)
    try:
        db.commit()
    except Exception as error:
        db.rollback()
        if object_deleted and backup_content is not None:
            try:
                _upload_market_object(backup_content, object_key)
            except Exception:
                logger.exception("Could not restore market resource %s after database failure", resource_id)
        raise HTTPException(
            status_code=500,
            detail={"code": "MARKET_DELETE_FAILED", "message": "The market resource was not deleted."},
        ) from error
    return {
        "deleted": True,
        "resource_id": resource_id,
        "cdn_object_deleted": object_deleted,
        "cdn_invalidation_requested": invalidation_requested,
    }
