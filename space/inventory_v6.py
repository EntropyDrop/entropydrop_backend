"""Read legacy inventory v6 definitions and convert them to canonical v7.

Only the ``space_0004`` data migration and its tests use this module. New code must
use :mod:`space.inventory_codec` (v7); the v6 schema exists solely in
``space/contracts/inventory_v6.proto`` so old entity bytes can be read once.
"""
from __future__ import annotations

from typing import Any

from google.protobuf.message import DecodeError

from space.contracts import inventory_v6_pb2 as pb_v6
from space.inventory_codec import (
    SCHEMA_VERSION,
    InventoryCodecError,
    encode_inventory_resource,
    inventory_content_digest,
)
from space.voxel_grid import MICRO_DIVISIONS, MICRO_CELLS_PER_BLOCK


def _canonical_double(value: Any) -> float:
    number = float(value)
    return 0.0 if number == 0.0 else number


def _vector(value) -> list[float]:
    return [
        _canonical_double(value.x),
        _canonical_double(value.y),
        _canonical_double(value.z),
    ]


def _quaternion(value) -> list[float]:
    return [
        _canonical_double(value.x),
        _canonical_double(value.y),
        _canonical_double(value.z),
        _canonical_double(value.w),
    ]


def _decode_voxel_v6(block) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dx": int(block.dx),
        "dy": int(block.dy),
        "dz": int(block.dz),
        "block": 1,
        "color": int(block.color),
    }
    if block.HasField("micro_index"):
        packed = int(block.micro_index) - 1
        if not 0 <= packed < MICRO_CELLS_PER_BLOCK:
            raise InventoryCodecError("legacy micro voxel index is outside 0..511")
        result.update({
            "mx": packed % MICRO_DIVISIONS,
            "my": (packed // MICRO_DIVISIONS) % MICRO_DIVISIONS,
            "mz": packed // MICRO_DIVISIONS ** 2,
        })
    return result


def _decode_body_v6(message) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "kinematic" if message.type == pb_v6.BODY_TYPE_KINEMATIC else "dynamic",
    }
    for field in ("mass", "restitution", "friction"):
        if message.HasField(field):
            result[field] = _canonical_double(getattr(message, field))
    for field, target in (
        ("use_gravity", "useGravity"),
        ("collision_enabled", "collisionEnabled"),
    ):
        if message.HasField(field):
            result[target] = bool(getattr(message, field))
    return result


def _decode_component_v6(message) -> dict[str, Any]:
    if not message.HasField("body"):
        raise InventoryCodecError(f'legacy component "{message.id}" is missing its body')
    for seat in message.seats:
        if not seat.HasField("position"):
            raise InventoryCodecError(f'legacy component "{message.id}" has a seat without a position')
    result: dict[str, Any] = {
        "id": message.id,
        "name": message.name,
        "body": _decode_body_v6(message.body),
        "blocks": [_decode_voxel_v6(block) for block in message.blocks],
        "seats": [{"position": _vector(seat.position)} for seat in message.seats],
        "children": [
            _decode_component_v6(child)
            for child in sorted(message.children, key=lambda value: str(value.id))
        ],
    }
    if message.HasField("pivot"):
        result["pivot"] = _vector(message.pivot)
    if message.HasField("script"):
        result["script"] = message.script
    if message.script_disabled:
        result["scriptDisabled"] = True
    if message.HasField("local_position"):
        result["localPosition"] = _vector(message.local_position)
    if message.HasField("local_rotation"):
        result["localRotation"] = _quaternion(message.local_rotation)
    if message.HasField("anchor_rotation"):
        result["anchorRotation"] = _quaternion(message.anchor_rotation)
    return result


def _decode_constraint_v6(constraint) -> dict[str, Any]:
    type_name = ("point", "hinge", "weld")[int(constraint.type)]
    result: dict[str, Any] = {
        "id": constraint.id,
        "type": type_name,
        "bodyA": (
            constraint.body_a_component_id
            if constraint.HasField("body_a_component_id")
            else None
        ),
        "bodyB": constraint.body_b_component_id,
        "stiffness": _canonical_double(constraint.stiffness),
        "collideConnected": bool(constraint.collide_connected),
    }
    for source, target in (
        ("anchor_a", "anchorA"),
        ("anchor_b", "anchorB"),
        ("axis_a", "axisA"),
        ("axis_b", "axisB"),
        ("reference_a", "referenceA"),
        ("reference_b", "referenceB"),
    ):
        if constraint.HasField(source):
            result[target] = _vector(getattr(constraint, source))
    if constraint.HasField("limits"):
        result["limits"] = {
            "min": _canonical_double(constraint.limits.min),
            "max": _canonical_double(constraint.limits.max),
        }
    return result


def decode_v6_inventory_resource(encoded: bytes) -> tuple[str, dict[str, Any]]:
    """Decode v6 bytes into the portable dict shape used by the v7 codec."""
    resource = pb_v6.InventoryResource()
    try:
        resource.ParseFromString(encoded)
    except DecodeError as error:
        raise InventoryCodecError("legacy resource is not valid Protobuf") from error
    if resource.schema_version != 6:
        raise InventoryCodecError(
            f"expected legacy schema version 6, got {resource.schema_version}"
        )
    content = resource.WhichOneof("content")
    if content == "block_set":
        return "blockset", {
            "type": "space-blockset",
            "version": SCHEMA_VERSION,
            "name": resource.block_set.name,
            "blocks": [_decode_voxel_v6(block) for block in resource.block_set.blocks],
        }
    if content == "color_set":
        return "colorset", {
            "type": "space-colorset",
            "version": SCHEMA_VERSION,
            "name": resource.color_set.name,
            "colors": [f"#{int(color):06x}" for color in resource.color_set.colors],
        }
    if content == "entity":
        if not resource.entity.HasField("root"):
            raise InventoryCodecError("legacy entity is missing its root component")
        return "entity", {
            "type": "space-entity",
            "version": SCHEMA_VERSION,
            "root": _decode_component_v6(resource.entity.root),
            "constraints": [
                _decode_constraint_v6(constraint)
                for constraint in sorted(resource.entity.constraints, key=lambda value: str(value.id))
            ],
        }
    raise InventoryCodecError("legacy resource does not contain an inventory item")


def convert_v6_inventory_resource(encoded: bytes) -> tuple[str, dict[str, Any], bytes, bytes]:
    """Return ``(kind, portable, canonical_v7_bytes, name-free_digest)``."""
    kind, portable = decode_v6_inventory_resource(encoded)
    canonical = encode_inventory_resource(kind, portable)
    digest = inventory_content_digest(kind, portable)
    return kind, portable, canonical, digest
