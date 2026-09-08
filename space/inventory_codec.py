"""Canonical Protobuf codec for portable Space and market resources."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from google.protobuf.message import DecodeError

from space.contracts import inventory_pb2


from space.voxel_grid import MICRO_DIVISIONS, MICRO_CELLS_PER_BLOCK

SCHEMA_VERSION = 6
InventoryKind = Literal["blockset", "entity", "colorset"]


class InventoryCodecError(ValueError):
    pass


def _canonical_double(value: Any) -> float:
    """Return the unique protobuf representation for either signed zero."""
    number = float(value)
    return 0.0 if number == 0.0 else number


_BODY_TYPE_TO_PROTO = {
    "dynamic": inventory_pb2.BODY_TYPE_DYNAMIC,
    "kinematic": inventory_pb2.BODY_TYPE_KINEMATIC,
}
_BODY_TYPE_FROM_PROTO = {value: key for key, value in _BODY_TYPE_TO_PROTO.items()}
_CONSTRAINT_TO_PROTO = {
    "point": inventory_pb2.CONSTRAINT_TYPE_POINT,
    "hinge": inventory_pb2.CONSTRAINT_TYPE_HINGE,
    "weld": inventory_pb2.CONSTRAINT_TYPE_WELD,
}
_CONSTRAINT_FROM_PROTO = {value: key for key, value in _CONSTRAINT_TO_PROTO.items()}


def _set_vector(target, value: list[float] | tuple[float, float, float] | None) -> None:
    if value is None:
        return
    target.x = _canonical_double(value[0])
    target.y = _canonical_double(value[1])
    target.z = _canonical_double(value[2])


def _vector(value) -> list[float]:
    return [
        _canonical_double(value.x),
        _canonical_double(value.y),
        _canonical_double(value.z),
    ]


def _set_quaternion(
    target,
    value: list[float] | tuple[float, float, float, float] | None,
) -> None:
    if value is None:
        return
    target.x = _canonical_double(value[0])
    target.y = _canonical_double(value[1])
    target.z = _canonical_double(value[2])
    target.w = _canonical_double(value[3])


def _quaternion(value) -> list[float]:
    return [
        _canonical_double(value.x),
        _canonical_double(value.y),
        _canonical_double(value.z),
        _canonical_double(value.w),
    ]


def _encode_voxel(target, block: dict[str, Any]) -> None:
    target.dx = int(block["dx"])
    target.dy = int(block["dy"])
    target.dz = int(block["dz"])
    if block.get("mx") is not None:
        target.micro_index = (
            1
            + int(block["mx"])
            + MICRO_DIVISIONS * int(block["my"])
            + MICRO_DIVISIONS ** 2 * int(block["mz"])
        )
    target.color = int(block["color"])


def _decode_voxel(block) -> dict[str, Any]:
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
            raise InventoryCodecError("micro voxel index is outside 0..511")
        result.update({
            "mx": packed % MICRO_DIVISIONS,
            "my": (packed // MICRO_DIVISIONS) % MICRO_DIVISIONS,
            "mz": packed // MICRO_DIVISIONS ** 2,
        })
    return result


def _voxel_sort_key(block: dict[str, Any]) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(block["dx"]),
        int(block["dy"]),
        int(block["dz"]),
        -1 if block.get("mx") is None else int(block["mx"]),
        -1 if block.get("my") is None else int(block["my"]),
        -1 if block.get("mz") is None else int(block["mz"]),
        int(block["color"]),
    )


def _encode_block_set(message, canonical: dict[str, Any], include_name: bool) -> None:
    if include_name:
        message.name = canonical["name"]
    for block in sorted(canonical["blocks"], key=_voxel_sort_key):
        _encode_voxel(message.blocks.add(), block)


def _decode_block_set(message) -> dict[str, Any]:
    blocks = [_decode_voxel(block) for block in message.blocks]
    return {
        "type": "space-blockset",
        "version": SCHEMA_VERSION,
        "name": message.name,
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


def _encode_body(message, body: dict[str, Any]) -> None:
    message.SetInParent()
    message.type = _BODY_TYPE_TO_PROTO[body.get("type", "dynamic")]
    for field in ("mass", "restitution", "friction"):
        if body.get(field) is not None:
            setattr(message, field, _canonical_double(body[field]))
    for source, target in (
        ("useGravity", "use_gravity"),
        ("collisionEnabled", "collision_enabled"),
    ):
        if body.get(source) is not None:
            setattr(message, target, bool(body[source]))


def _decode_body(message) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": _enum_value(_BODY_TYPE_FROM_PROTO, message.type, "body type"),
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


def _encode_component(message, component: dict[str, Any], include_name: bool) -> None:
    message.id = str(component["id"])
    if include_name:
        message.name = component.get("name", "")
    if component.get("pivot") is not None:
        _set_vector(message.pivot, component["pivot"])
    _encode_body(message.body, component.get("body", {}))
    for block in sorted(component.get("blocks", []), key=_voxel_sort_key):
        _encode_voxel(message.blocks.add(), block)
    if component.get("script") is not None:
        message.script = str(component["script"])
    message.script_disabled = bool(component.get("scriptDisabled", False))
    for seat in component.get("seats", []):
        _set_vector(message.seats.add().position, seat["position"])
    for child in sorted(component.get("children", []), key=lambda value: str(value["id"])):
        _encode_component(message.children.add(), child, include_name)
    if component.get("localPosition") is not None:
        _set_vector(message.local_position, component["localPosition"])
    if component.get("localRotation") is not None:
        _set_quaternion(message.local_rotation, component["localRotation"])
    if component.get("anchorRotation") is not None:
        _set_quaternion(message.anchor_rotation, component["anchorRotation"])


def _decode_component(message) -> dict[str, Any]:
    if not message.HasField("body"):
        raise InventoryCodecError(f'component "{message.id}" is missing its body configuration')
    for seat in message.seats:
        if not seat.HasField("position"):
            raise InventoryCodecError(f'component "{message.id}" contains a seat without a position')
    result: dict[str, Any] = {
        "id": message.id,
        "name": message.name,
        "body": _decode_body(message.body),
        "blocks": [_decode_voxel(block) for block in message.blocks],
        "seats": [{"position": _vector(seat.position)} for seat in message.seats],
        "children": [
            _decode_component(child)
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


def _encode_entity(message, canonical: dict[str, Any], include_name: bool) -> None:
    if "name" in canonical:
        raise InventoryCodecError("entity names belong to root.name")
    _encode_component(message.root, canonical["root"], include_name)
    for constraint in sorted(canonical.get("constraints", []), key=lambda value: str(value["id"])):
        encoded = message.constraints.add()
        encoded.id = constraint["id"]
        encoded.type = _CONSTRAINT_TO_PROTO[constraint.get("type", "point")]
        if constraint.get("bodyA") is not None:
            encoded.body_a_component_id = constraint["bodyA"]
        encoded.body_b_component_id = constraint["bodyB"]
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
            encoded.limits.min = _canonical_double(constraint["limits"]["min"])
            encoded.limits.max = _canonical_double(constraint["limits"]["max"])
        encoded.stiffness = _canonical_double(constraint.get("stiffness", 0.9))
        encoded.collide_connected = bool(constraint.get("collideConnected", False))


def _enum_value(mapping: dict[int, str], value: int, label: str) -> str:
    try:
        return mapping[value]
    except KeyError as error:
        raise InventoryCodecError(f"unknown {label} enum value {value}") from error


def _decode_entity(message) -> dict[str, Any]:
    if not message.HasField("root"):
        raise InventoryCodecError("entity is missing its root component")
    result: dict[str, Any] = {
        "type": "space-entity",
        "version": SCHEMA_VERSION,
        "root": _decode_component(message.root),
        "constraints": [],
    }
    for constraint in sorted(message.constraints, key=lambda value: str(value.id)):
        decoded: dict[str, Any] = {
            "id": constraint.id,
            "type": _enum_value(_CONSTRAINT_FROM_PROTO, constraint.type, "constraint type"),
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
                decoded[target] = _vector(getattr(constraint, source))
        if constraint.HasField("limits"):
            decoded["limits"] = {
                "min": _canonical_double(constraint.limits.min),
                "max": _canonical_double(constraint.limits.max),
            }
        result["constraints"].append(decoded)
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


def inventory_resource_name(kind: InventoryKind, canonical: dict[str, Any]) -> str:
    """Derive list metadata without introducing a second entity name source."""
    if kind == "entity":
        root = canonical["root"]
        return root.get("name") or root["id"]
    return canonical["name"]
