import math

import pytest

from space.contracts import inventory_pb2
from space.inventory_codec import (
    InventoryCodecError,
    decode_inventory_resource,
    encode_inventory_resource,
    inventory_content_digest,
)


CROSS_LANGUAGE_BLOCKSET_HEX = "080452140a0543726f7373120b0801100420462d34ab1200"
CROSS_LANGUAGE_CANONICAL_ENTITY_HEX = (
    "08045a5a0a054f7264657212250a05776f726c641a0022052d0100000042070a01421a020801"
    "420a0a04726f6f741a0208011a0f0a014122014261cdccccccccccec3f1a190a017a1a05776f"
    "726c642204726f6f7461cdccccccccccec3f"
)


def test_inventory_protobuf_matches_the_frontend_deterministic_wire_fixture():
    canonical = {
        "type": "space-blockset",
        "version": 4,
        "name": "Cross",
        "blocks": [{
            "dx": -1,
            "dy": 2,
            "dz": 0,
            "mx": 4,
            "my": 3,
            "mz": 2,
            "block": 1,
            "color": 0x12AB34,
        }],
    }
    encoded = encode_inventory_resource("blockset", canonical)
    assert encoded.hex() == CROSS_LANGUAGE_BLOCKSET_HEX
    kind, decoded = decode_inventory_resource(bytes.fromhex(CROSS_LANGUAGE_BLOCKSET_HEX))
    assert kind == "blockset"
    assert decoded == canonical


def test_inventory_entity_matches_the_frontend_canonical_order_fixture():
    canonical = {
        "type": "space-entity",
        "version": 4,
        "name": "Order",
        "root": {
            "id": "world",
            "body": {"type": "dynamic"},
            "blocks": [{"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 1}],
            "seats": [],
            "children": [
                {
                    "id": "root",
                    "body": {"type": "kinematic"},
                    "blocks": [],
                    "seats": [],
                    "children": [],
                },
                {
                    "id": "B",
                    "body": {"type": "kinematic"},
                    "blocks": [],
                    "seats": [],
                    "children": [],
                },
            ],
        },
        "constraints": [
            {"id": "z", "type": "point", "bodyA": "world", "bodyB": "root", "stiffness": 0.9},
            {"id": "A", "type": "point", "bodyA": None, "bodyB": "B", "stiffness": 0.9},
        ],
    }
    encoded = encode_inventory_resource("entity", canonical)
    digest = inventory_content_digest("entity", canonical)
    assert encoded.hex() == CROSS_LANGUAGE_CANONICAL_ENTITY_HEX
    kind, decoded = decode_inventory_resource(encoded)
    assert kind == "entity"
    assert decoded["root"]["id"] == "world"
    assert decoded["root"]["children"][1]["id"] == "root"
    assert decoded["constraints"][0]["bodyA"] is None
    assert decoded["constraints"][1]["bodyA"] == "world"

    canonical["root"]["children"].reverse()
    canonical["constraints"].reverse()
    assert encode_inventory_resource("entity", canonical) == encoded
    assert inventory_content_digest("entity", canonical) == digest


def test_inventory_decoder_normalizes_component_and_constraint_order_by_id():
    resource = inventory_pb2.InventoryResource(schema_version=4)
    resource.entity.name = "Wire order"
    resource.entity.root.id = "root"
    resource.entity.root.body.SetInParent()
    for component_id in ("a", "B"):
        child = resource.entity.root.children.add()
        child.id = component_id
        child.body.SetInParent()
    for constraint_id in ("z", "A"):
        constraint = resource.entity.constraints.add()
        constraint.id = constraint_id

    kind, decoded = decode_inventory_resource(resource.SerializeToString(deterministic=True))
    assert kind == "entity"
    assert [child["id"] for child in decoded["root"]["children"]] == ["B", "a"]
    assert [constraint["id"] for constraint in decoded["constraints"]] == ["A", "z"]


def test_inventory_decoder_treats_root_and_world_as_ordinary_component_ids():
    resource = inventory_pb2.InventoryResource(schema_version=4)
    resource.entity.name = "Opaque component ids"
    resource.entity.root.id = "world"
    resource.entity.root.body.SetInParent()
    child = resource.entity.root.children.add()
    child.id = "root"
    child.body.SetInParent()

    kind, decoded = decode_inventory_resource(resource.SerializeToString(deterministic=True))
    assert kind == "entity"
    assert decoded["root"]["id"] == "world"
    assert decoded["root"]["children"][0]["id"] == "root"


def test_shared_inventory_schema_excludes_browser_backpack_state():
    assert not hasattr(inventory_pb2, "Backpack")
    assert not hasattr(inventory_pb2, "InventorySlot")
    assert "micro_index" in inventory_pb2.Voxel.DESCRIPTOR.fields_by_name


def test_inventory_v4_constraint_references_use_presence_instead_of_string_sentinels():
    assert inventory_pb2.DESCRIPTOR.package == "entropydrop.space.inventory.v4"
    fields = inventory_pb2.EntityConstraint.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in fields.items()} == {
        "id": 1,
        "type": 2,
        "body_a_component_id": 3,
        "body_b_component_id": 4,
        "anchor_a": 5,
        "anchor_b": 6,
        "axis_a": 7,
        "axis_b": 8,
        "reference_a": 9,
        "reference_b": 10,
        "limits": 11,
        "stiffness": 12,
        "collide_connected": 13,
    }
    assert fields["body_a_component_id"].has_presence is True
    assert "body_a_is_world" not in fields


def test_inventory_codec_sorts_voxels_at_the_encoding_boundary():
    unsorted = {
        "type": "space-blockset",
        "version": 4,
        "name": "Order",
        "blocks": [
            {"dx": 1, "dy": 0, "dz": 0, "block": 1, "color": 2},
            {"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 1},
        ],
    }
    sorted_copy = {**unsorted, "blocks": list(reversed(unsorted["blocks"]))}
    assert encode_inventory_resource("blockset", unsorted) == encode_inventory_resource(
        "blockset",
        sorted_copy,
    )


def test_inventory_decoder_uses_standard_oneof_merge_and_invalid_wire_semantics():
    kind, color_set = decode_inventory_resource(bytes.fromhex(
        "080452030a014262090a0143120456341200"
    ))
    assert kind == "colorset"
    assert color_set == {
        "type": "space-colorset",
        "version": 4,
        "name": "C",
        "colors": ["#123456"],
    }

    kind, block_set = decode_inventory_resource(bytes.fromhex(
        "080452030a0142520412020801"
    ))
    assert kind == "blockset"
    assert block_set["name"] == "B"
    assert block_set["blocks"][0]["dx"] == -1

    with pytest.raises(InventoryCodecError, match="not valid Protobuf"):
        decode_inventory_resource(bytes.fromhex(f"{CROSS_LANGUAGE_BLOCKSET_HEX}00deadbeef"))
    with pytest.raises(InventoryCodecError, match="not valid Protobuf"):
        decode_inventory_resource(bytes.fromhex("080352070a01ff12020801"))

    with pytest.raises(InventoryCodecError, match="schema version"):
        decode_inventory_resource(bytes.fromhex("090352050a01781200"))
    kind, empty_block_set = decode_inventory_resource(bytes.fromhex("080452005001"))
    assert kind == "blockset"
    assert empty_block_set["blocks"] == []

    kind, bom_entity = decode_inventory_resource(bytes.fromhex(
        "08045a120a0178120d0a07efbbbf726f6f741a002200"
    ))
    assert kind == "entity"
    assert bom_entity["root"]["id"] == "\ufeffroot"


def test_recursive_entity_round_trip_keeps_component_local_body_script_and_seats():
    canonical = {
        "type": "space-entity",
        "version": 4,
        "name": "Rover",
        "root": {
            "id": "root",
            "anchorRotation": [0, 0, math.sqrt(0.5), math.sqrt(0.5)],
            "body": {"type": "dynamic", "useGravity": False},
            "blocks": [{"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 1}],
            "seats": [{"position": [0, 1, 0]}],
            "children": [{
                "id": "wheel",
                "pivot": [1, 0, 0],
                "localPosition": [1, 2, 3],
                "localRotation": [0, 1, 0, 0],
                "anchorRotation": [math.sqrt(0.5), 0, 0, math.sqrt(0.5)],
                "body": {"type": "kinematic", "collisionEnabled": False},
                "blocks": [{"dx": 1, "dy": 0, "dz": 0, "block": 1, "color": 2}],
                "script": "self.setLocalSpin([1,0,0], 60);",
                "scriptDisabled": True,
                "seats": [],
                "children": [],
            }],
        },
        "constraints": [],
    }
    kind, decoded = decode_inventory_resource(encode_inventory_resource("entity", canonical))
    assert kind == "entity"
    assert decoded == canonical


def test_inventory_digest_includes_component_transforms():
    canonical = {
        "type": "space-entity",
        "version": 4,
        "name": "Arm",
        "root": {
            "id": "root",
            "body": {"type": "dynamic"},
            "blocks": [{"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 1}],
            "seats": [],
            "children": [{
                "id": "arm",
                "localPosition": [1, 0, 0],
                "localRotation": [0, 0, 0, 1],
                "anchorRotation": [0, 0, 0, 1],
                "body": {"type": "kinematic"},
                "blocks": [],
                "seats": [],
                "children": [],
            }],
        },
        "constraints": [],
    }
    moved = {
        **canonical,
        "root": {
            **canonical["root"],
            "children": [{**canonical["root"]["children"][0], "localPosition": [2, 0, 0]}],
        },
    }

    assert inventory_content_digest("entity", canonical) != inventory_content_digest("entity", moved)


def test_inventory_digest_ignores_display_name_but_not_content():
    first = {
        "type": "space-colorset",
        "version": 4,
        "name": "First",
        "colors": ["#123456"] * 9,
    }
    renamed = {**first, "name": "Renamed"}
    changed = {**first, "colors": ["#654321"] * 9}
    assert inventory_content_digest("colorset", first) == inventory_content_digest("colorset", renamed)
    assert inventory_content_digest("colorset", first) != inventory_content_digest("colorset", changed)


def test_inventory_decoder_rejects_non_v4_and_missing_content_messages():
    with pytest.raises(InventoryCodecError, match="schema version 4"):
        decode_inventory_resource(b"\x08\x03")
    with pytest.raises(InventoryCodecError, match="does not contain"):
        decode_inventory_resource(b"\x08\x04")


def test_inventory_decoder_rejects_missing_recursive_entity_messages():
    resource = inventory_pb2.InventoryResource(schema_version=4)
    resource.entity.name = "Missing root"
    with pytest.raises(InventoryCodecError, match="missing its root component"):
        decode_inventory_resource(resource.SerializeToString())

    resource.entity.root.id = "root"
    with pytest.raises(InventoryCodecError, match="missing its body configuration"):
        decode_inventory_resource(resource.SerializeToString())

    resource.entity.root.body.SetInParent()
    resource.entity.root.seats.add()
    with pytest.raises(InventoryCodecError, match="seat without a position"):
        decode_inventory_resource(resource.SerializeToString())


def test_inventory_codec_canonicalizes_signed_zero_for_every_double_field():
    negative_zero = {
        "type": "space-entity",
        "version": 4,
        "name": "Signed zero",
        "root": {
            "id": "root",
            "pivot": [-0.0, -0.0, -0.0],
            "anchorRotation": [-0.0, -0.0, -0.0, 1.0],
            "body": {
                "type": "dynamic",
                "mass": -0.0,
                "restitution": -0.0,
                "friction": -0.0,
            },
            "blocks": [{"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 1}],
            "seats": [{"position": [-0.0, -0.0, -0.0]}],
            "children": [{
                "id": "child",
                "pivot": [-0.0, -0.0, -0.0],
                "localPosition": [-0.0, -0.0, -0.0],
                "localRotation": [-0.0, -0.0, -0.0, 1.0],
                "anchorRotation": [-0.0, -0.0, -0.0, 1.0],
                "body": {"type": "kinematic"},
                "blocks": [],
                "seats": [],
                "children": [],
            }],
        },
        "constraints": [{
            "id": "joint",
            "type": "point",
            "bodyA": "root",
            "bodyB": "child",
            "anchorA": [-0.0, -0.0, -0.0],
            "anchorB": [-0.0, -0.0, -0.0],
            "axisA": [-0.0, -0.0, -0.0],
            "axisB": [-0.0, -0.0, -0.0],
            "referenceA": [-0.0, -0.0, -0.0],
            "referenceB": [-0.0, -0.0, -0.0],
            "limits": {"min": -0.0, "max": -0.0},
            "stiffness": -0.0,
        }],
    }

    def positive_zero(value):
        if isinstance(value, float):
            return 0.0 if value == 0.0 else value
        if isinstance(value, list):
            return [positive_zero(item) for item in value]
        if isinstance(value, dict):
            return {key: positive_zero(item) for key, item in value.items()}
        return value

    encoded = encode_inventory_resource("entity", negative_zero)
    assert encoded == encode_inventory_resource("entity", positive_zero(negative_zero))

    _kind, decoded = decode_inventory_resource(encoded)

    def assert_no_negative_zero(value):
        if isinstance(value, float) and value == 0.0:
            assert math.copysign(1.0, value) == 1.0
        elif isinstance(value, list):
            for item in value:
                assert_no_negative_zero(item)
        elif isinstance(value, dict):
            for item in value.values():
                assert_no_negative_zero(item)

    assert_no_negative_zero(decoded)
