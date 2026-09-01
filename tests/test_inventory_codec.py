import pytest

from space.inventory_codec import (
    InventoryCodecError,
    decode_inventory_resource,
    encode_inventory_resource,
    inventory_content_digest,
)


CROSS_LANGUAGE_BLOCKSET_HEX = "080352190a0543726f737312100801100420462d34ab12003203746970"


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
            "part": "tip",
        }],
    }
    encoded = encode_inventory_resource("blockset", canonical)
    assert encoded.hex() == CROSS_LANGUAGE_BLOCKSET_HEX
    kind, decoded = decode_inventory_resource(bytes.fromhex(CROSS_LANGUAGE_BLOCKSET_HEX))
    assert kind == "blockset"
    assert decoded == {**canonical, "blockCount": 1}


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
