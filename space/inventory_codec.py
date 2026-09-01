"""Canonical Protobuf codec for Space backpack and market resources."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from google.protobuf.message import DecodeError

from space.contracts import inventory_pb2


SCHEMA_VERSION = 3
InventoryKind = Literal["blockset", "entity", "colorset"]


class InventoryCodecError(ValueError):
    pass


_BODY_TYPE_TO_PROTO = {
    "dynamic": inventory_pb2.BODY_TYPE_DYNAMIC,
    "kinematic": inventory_pb2.BODY_TYPE_KINEMATIC,
}
_BODY_TYPE_FROM_PROTO = {value: key for key, value in _BODY_TYPE_TO_PROTO.items()}
_MODE_TO_PROTO = {
    "free_physics": inventory_pb2.ENTITY_MODE_FREE_PHYSICS,
    "bearing": inventory_pb2.ENTITY_MODE_BEARING,
    "piston": inventory_pb2.ENTITY_MODE_PISTON,
    "drivable": inventory_pb2.ENTITY_MODE_DRIVABLE,
    "projectile": inventory_pb2.ENTITY_MODE_PROJECTILE,
    "programmable": inventory_pb2.ENTITY_MODE_PROGRAMMABLE,
}
_MODE_FROM_PROTO = {value: key for key, value in _MODE_TO_PROTO.items()}
_CONSTRAINT_TO_PROTO = {
    "point": inventory_pb2.CONSTRAINT_TYPE_POINT,
    "hinge": inventory_pb2.CONSTRAINT_TYPE_HINGE,
    "weld": inventory_pb2.CONSTRAINT_TYPE_WELD,
}
_CONSTRAINT_FROM_PROTO = {value: key for key, value in _CONSTRAINT_TO_PROTO.items()}


def _set_vector(target, value: list[float] | tuple[float, float, float] | None) -> None:
    if value is None:
        return
    target.x = float(value[0])
    target.y = float(value[1])
    target.z = float(value[2])


def _vector(value) -> list[float]:
    return [float(value.x), float(value.y), float(value.z)]


def _encode_voxel(target, block: dict[str, Any], component_index: int = 0) -> None:
    target.dx = int(block["dx"])
    target.dy = int(block["dy"])
    target.dz = int(block["dz"])
    if block.get("mx") is not None:
        target.micro_index = (
            1
            + int(block["mx"])
            + 5 * int(block["my"])
            + 25 * int(block["mz"])
        )
    target.color = int(block["color"])
    if block.get("part") is not None:
        target.part = str(block["part"])
    target.component_index = component_index


def _decode_voxel(block, component_id: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dx": int(block.dx),
        "dy": int(block.dy),
        "dz": int(block.dz),
        "block": 1,
        "color": int(block.color),
    }
    if block.HasField("micro_index"):
        packed = int(block.micro_index) - 1
        if not 0 <= packed < 125:
            raise InventoryCodecError("micro voxel index is outside 0..124")
        result.update({
            "mx": packed % 5,
            "my": (packed // 5) % 5,
            "mz": packed // 25,
        })
    if block.HasField("part"):
        result["part"] = block.part
    if component_id is not None:
        result["entityId"] = component_id
    return result


def _encode_block_set(message, canonical: dict[str, Any], include_name: bool) -> None:
    if include_name:
        message.name = canonical["name"]
    for block in canonical["blocks"]:
        _encode_voxel(message.blocks.add(), block)


def _decode_block_set(message) -> dict[str, Any]:
    blocks = [_decode_voxel(block) for block in message.blocks]
    return {
        "type": "space-blockset",
        "version": SCHEMA_VERSION,
        "name": message.name,
        "blockCount": len(blocks),
        "blocks": blocks,
    }


def _encode_color_set(message, canonical: dict[str, Any], include_name: bool) -> None:
    if include_name:
        message.name = canonical["name"]
    message.colors.extend(int(color.removeprefix("#"), 16) for color in canonical["colors"])


def _decode_color_set(message) -> dict[str, Any]:
    return {
        "type": "space-colorset",
        "version": SCHEMA_VERSION,
        "name": message.name,
        "colors": [f"#{int(color):06x}" for color in message.colors],
    }


def _component_index_map(canonical: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    components = list(canonical.get("childEntities", []))
    component_indices = {"root": 0}
    for index, component in enumerate(components, start=1):
        component_id = str(component["id"])
        if component_id in component_indices:
            raise InventoryCodecError(f"duplicate component id {component_id}")
        component_indices[component_id] = index
    return components, component_indices


def _require_component_index(component_indices: dict[str, int], component_id: Any) -> int:
    try:
        return component_indices[str(component_id)]
    except KeyError as error:
        raise InventoryCodecError(f"unknown component id {component_id}") from error


def _encode_entity(message, canonical: dict[str, Any], include_name: bool) -> None:
    if include_name:
        message.name = canonical["name"]
    components, component_indices = _component_index_map(canonical)

    for component in components:
        encoded = message.components.add()
        encoded.id = str(component["id"])
        encoded.parent_index = _require_component_index(component_indices, component["parentId"])
        if "collisionEnabled" in component:
            encoded.collision_enabled = bool(component["collisionEnabled"])
        if component.get("pivot") is not None:
            _set_vector(encoded.pivot, component["pivot"])
        if component.get("bodyType") is not None:
            encoded.body_type = _BODY_TYPE_TO_PROTO[component["bodyType"]]
        for source, target in (
            ("mass", "mass"),
            ("restitution", "restitution"),
            ("friction", "friction"),
        ):
            if component.get(source) is not None:
                setattr(encoded, target, float(component[source]))

    for block in canonical["blocks"]:
        _encode_voxel(
            message.blocks.add(),
            block,
            _require_component_index(component_indices, block["entityId"]),
        )

    for script in canonical.get("scripts", []):
        encoded = message.scripts.add()
        encoded.component_index = _require_component_index(component_indices, script["id"])
        encoded.code = script["code"]
    for enabled in canonical.get("enabled", []):
        encoded = message.enabled.add()
        encoded.component_index = _require_component_index(component_indices, enabled["id"])
        encoded.enabled = bool(enabled["enabled"])
    for constraint in canonical.get("constraints", []):
        encoded = message.constraints.add()
        encoded.id = constraint["id"]
        encoded.type = _CONSTRAINT_TO_PROTO[constraint.get("type", "point")]
        encoded.body_a_is_world = constraint["bodyA"] == "world"
        if not encoded.body_a_is_world:
            encoded.body_a_component_index = _require_component_index(
                component_indices,
                constraint["bodyA"],
            )
        encoded.body_b_component_index = _require_component_index(
            component_indices,
            constraint["bodyB"],
        )
        for source, target in (
            ("anchorA", "anchor_a"),
            ("anchorB", "anchor_b"),
            ("axisA", "axis_a"),
            ("axisB", "axis_b"),
            ("referenceA", "reference_a"),
            ("referenceB", "reference_b"),
        ):
            if constraint.get(source) is not None:
                _set_vector(getattr(encoded, target), constraint[source])
        if constraint.get("limits") is not None:
            encoded.limits.min = float(constraint["limits"]["min"])
            encoded.limits.max = float(constraint["limits"]["max"])
        encoded.stiffness = float(constraint.get("stiffness", 0.9))
        encoded.collide_connected = bool(constraint.get("collideConnected", False))

    message.mode = _MODE_TO_PROTO[canonical.get("mode", "free_physics")]
    message.body_type = _BODY_TYPE_TO_PROTO[canonical.get("bodyType", "dynamic")]
    for source, target in (
        ("mass", "mass"),
        ("restitution", "restitution"),
        ("friction", "friction"),
        ("bearingRpm", "bearing_rpm"),
        ("pistonDistance", "piston_distance"),
        ("pistonSpeed", "piston_speed"),
    ):
        if canonical.get(source) is not None:
            setattr(message, target, float(canonical[source]))
    for source, target in (
        ("useGravity", "use_gravity"),
        ("isVehicle", "is_vehicle"),
    ):
        if canonical.get(source) is not None:
            setattr(message, target, bool(canonical[source]))
    for source, target in (
        ("bearingAxis", "bearing_axis"),
        ("pistonAxis", "piston_axis"),
        ("cockpitPosition", "cockpit_position"),
    ):
        if canonical.get(source) is not None:
            _set_vector(getattr(message, target), canonical[source])


def _enum_value(mapping: dict[int, str], value: int, label: str) -> str:
    try:
        return mapping[value]
    except KeyError as error:
        raise InventoryCodecError(f"unknown {label} enum value {value}") from error


def _component_id(component_ids: list[str], index: int) -> str:
    if not 0 <= index < len(component_ids):
        raise InventoryCodecError(f"component index {index} is out of range")
    return component_ids[index]


def _decode_entity(message) -> dict[str, Any]:
    component_ids = ["root", *(component.id for component in message.components)]
    children = []
    for component in message.components:
        child: dict[str, Any] = {
            "id": component.id,
            "parentId": _component_id(component_ids, int(component.parent_index)),
            "kind": "child",
        }
        if component.HasField("collision_enabled"):
            child["collisionEnabled"] = component.collision_enabled
        if component.HasField("pivot"):
            child["pivot"] = _vector(component.pivot)
        if component.HasField("body_type"):
            child["bodyType"] = _enum_value(
                _BODY_TYPE_FROM_PROTO,
                component.body_type,
                "body type",
            )
        for field, target in (
            ("mass", "mass"),
            ("restitution", "restitution"),
            ("friction", "friction"),
        ):
            if component.HasField(field):
                child[target] = float(getattr(component, field))
        children.append(child)

    blocks = [
        _decode_voxel(block, _component_id(component_ids, int(block.component_index)))
        for block in message.blocks
    ]
    result: dict[str, Any] = {
        "type": "space-entity",
        "version": SCHEMA_VERSION,
        "name": message.name,
        "rootId": "root",
        "nodeCount": len(component_ids),
        "blockCount": len(blocks),
        "blocks": blocks,
        "childEntities": children,
        "scripts": [
            {
                "id": _component_id(component_ids, int(script.component_index)),
                "code": script.code,
            }
            for script in message.scripts
        ],
        "enabled": [
            {
                "id": _component_id(component_ids, int(enabled.component_index)),
                "enabled": bool(enabled.enabled),
            }
            for enabled in message.enabled
        ],
        "constraints": [],
        "mode": _enum_value(_MODE_FROM_PROTO, message.mode, "entity mode"),
        "bodyType": _enum_value(_BODY_TYPE_FROM_PROTO, message.body_type, "body type"),
    }
    for constraint in message.constraints:
        decoded: dict[str, Any] = {
            "id": constraint.id,
            "type": _enum_value(_CONSTRAINT_FROM_PROTO, constraint.type, "constraint type"),
            "bodyA": "world" if constraint.body_a_is_world else _component_id(
                component_ids,
                int(constraint.body_a_component_index),
            ),
            "bodyB": _component_id(component_ids, int(constraint.body_b_component_index)),
            "stiffness": float(constraint.stiffness),
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
                decoded[target] = _vector(getattr(constraint, source))
        if constraint.HasField("limits"):
            decoded["limits"] = {
                "min": float(constraint.limits.min),
                "max": float(constraint.limits.max),
            }
        result["constraints"].append(decoded)

    for field, target in (
        ("mass", "mass"),
        ("restitution", "restitution"),
        ("friction", "friction"),
        ("bearing_rpm", "bearingRpm"),
        ("piston_distance", "pistonDistance"),
        ("piston_speed", "pistonSpeed"),
    ):
        if message.HasField(field):
            result[target] = float(getattr(message, field))
    for field, target in (("use_gravity", "useGravity"), ("is_vehicle", "isVehicle")):
        if message.HasField(field):
            result[target] = bool(getattr(message, field))
    for field, target in (
        ("bearing_axis", "bearingAxis"),
        ("piston_axis", "pistonAxis"),
        ("cockpit_position", "cockpitPosition"),
    ):
        if message.HasField(field):
            result[target] = _vector(getattr(message, field))
    return result


def encode_inventory_resource(
    kind: InventoryKind,
    canonical: dict[str, Any],
    *,
    include_name: bool = True,
) -> bytes:
    resource = inventory_pb2.InventoryResource(schema_version=SCHEMA_VERSION)
    if kind == "blockset":
        _encode_block_set(resource.block_set, canonical, include_name)
    elif kind == "entity":
        _encode_entity(resource.entity, canonical, include_name)
    elif kind == "colorset":
        _encode_color_set(resource.color_set, canonical, include_name)
    else:
        raise InventoryCodecError(f"unsupported inventory kind {kind}")
    return resource.SerializeToString(deterministic=True)


def decode_inventory_resource(encoded: bytes) -> tuple[InventoryKind, dict[str, Any]]:
    resource = inventory_pb2.InventoryResource()
    try:
        resource.ParseFromString(encoded)
    except DecodeError as error:
        raise InventoryCodecError("resource is not valid Protobuf") from error
    if resource.schema_version != SCHEMA_VERSION:
        raise InventoryCodecError(f"expected schema version {SCHEMA_VERSION}")
    content = resource.WhichOneof("content")
    if content == "block_set":
        return "blockset", _decode_block_set(resource.block_set)
    if content == "entity":
        return "entity", _decode_entity(resource.entity)
    if content == "color_set":
        return "colorset", _decode_color_set(resource.color_set)
    raise InventoryCodecError("resource does not contain an inventory item")


def inventory_content_digest(kind: InventoryKind, canonical: dict[str, Any]) -> bytes:
    encoded = encode_inventory_resource(kind, canonical, include_name=False)
    return hashlib.sha256(encoded).digest()
