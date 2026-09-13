"""Offline container smoke test; uses no accounts, database connection or network."""
import asyncio
import base64
from space.hosting_worker import NodeRuntime
from space.inventory_codec import decode_inventory_resource, encode_inventory_resource


async def main():
    runtime = NodeRuntime()
    try:
        assert await runtime.step({"probe": True}) == {"ready": True}
        definition = encode_inventory_resource("entity", {
            "type": "space-entity", "version": 7, "root": {"name": "Hosting smoke", "id": "root", "body": {"type": "dynamic", "useGravity": False},
                "blocks": [{"dx": 0, "dy": 0, "dz": 0, "block": 1, "color": 123}],
                "script": "self.state.ticks = (self.state.ticks || 0) + 1;",
                "children": [], "seats": []}, "constraints": []})
        payload = {"world_id": "smoke", "seed": 1337, "steps": 20,
            "entities": [{"id": "smoke-entity", "running": True, "position": [80, 220, 80],
                "anchor": [80, 80], "yaw_quarter_turns": 0, "snapshot": None,
                "definition_base64": base64.b64encode(definition).decode()}],
            "chunks": [{"chunk_x": 5, "chunk_z": 5, "revision": 0, "standard": [], "micro": []}]}
        result = await runtime.step(payload)
        assert not result.get("faults") and not result.get("error"), result
        snapshot = result["entities"][0]["snapshot"]
        assert snapshot["states"]["root"]["ticks"] == 20
        restored = decode_inventory_resource(base64.b64decode(result["entities"][0]["definition_base64"]))[1]
        assert "name" not in restored and restored["root"]["name"] == "Hosting smoke"
        payload["entities"][0]["snapshot"] = snapshot
        payload["entities"][0]["definition_base64"] = result["entities"][0]["definition_base64"]
        await runtime.close()
        result = await runtime.step(payload)
        assert result["entities"][0]["snapshot"]["states"]["root"]["ticks"] == 40
        restored = decode_inventory_resource(base64.b64decode(result["entities"][0]["definition_base64"]))[1]
        assert restored["root"]["name"] == "Hosting smoke"
        print("Hosted runtime: offline execution and process-restart recovery passed")
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
