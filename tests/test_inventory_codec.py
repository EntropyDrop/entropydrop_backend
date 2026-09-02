import math

import pytest

from space.contracts import inventory_pb2
from space.inventory_codec import (
    InventoryCodecError,
    decode_inventory_resource,
    encode_inventory_resource,
    inventory_content_digest,
)


CROSS_LANGUAGE_BLOCKSET_HEX = "080352140a0543726f7373120b0801100420462d34ab1200"


def test_inventory_protobuf_matches_the_frontend_deterministic_wire_fixture():
    canonical = {
        "type": "space-blockset",
        "version": 3,
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


def test_recursive_entity_round_trip_keeps_component_local_body_script_and_seats():
    canonical = {
        "type": "space-entity",
        "version": 3,
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
        "version": 3,
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
        "version": 3,
        "name": "First",
        "colors": ["#123456"] * 9,
    }
    renamed = {**first, "name": "Renamed"}
    changed = {**first, "colors": ["#654321"] * 9}
    assert inventory_content_digest("colorset", first) == inventory_content_digest("colorset", renamed)
    assert inventory_content_digest("colorset", first) != inventory_content_digest("colorset", changed)


def test_inventory_decoder_rejects_non_v3_and_missing_content_messages():
    with pytest.raises(InventoryCodecError, match="schema version 3"):
        decode_inventory_resource(b"\x08\x02")
    with pytest.raises(InventoryCodecError, match="does not contain"):
        decode_inventory_resource(b"\x08\x03")


def test_inventory_decoder_rejects_missing_recursive_entity_messages():
    resource = inventory_pb2.InventoryResource(schema_version=3)
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
