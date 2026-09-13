import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "space/contracts/schema.sql").read_text()
PROTOCOL = (ROOT / "space/contracts/protocol.proto").read_text()
DESIGN = (ROOT / "docs/space-backend.md").read_text()
CONTRACTS_DIR = ROOT / "space/contracts"


def test_space_contract_extends_existing_users_without_second_identity_or_skin_store():
    assert "CREATE TABLE users" not in SCHEMA
    assert "CREATE TABLE accounts" not in SCHEMA
    assert "REFERENCES users(id)" in SCHEMA
    assert "user_id" in SCHEMA
    assert "player_appearance_assets" not in SCHEMA
    assert "AppearanceCommand" not in PROTOCOL
    assert "string user_id = 2;" in PROTOCOL
    assert "string skin_url" in PROTOCOL
    assert "string skin_type" in PROTOCOL
    assert "minecraft_skin_url" not in PROTOCOL
    assert "minecraft_skin_model" not in PROTOCOL


def test_space_contract_covers_persistence_queue_and_browser_only_backpack():
    for table in (
        "chunk_snapshots",
        "space_surface_zone_snapshots",
        "world_events",
        "entity_snapshots",
        "entity_chunk_coverage",
        "world_player_profiles",
        "player_snapshots",
        "world_session_slots",
        "world_join_queue",
    ):
        assert f"CREATE TABLE {table}" in SCHEMA
    assert "slot_number BETWEEN 0 AND 31" in SCHEMA
    assert "player_inventories" not in SCHEMA
    assert "spawn_x_cm" not in SCHEMA
    assert "space.backpack.v8.pb" in DESIGN
    assert "QueueStatus queue_status" in PROTOCOL


def test_space_contract_has_complete_wake_sleep_checkpoint_transitions():
    for state in (
        "`SLEEPING`",
        "`LOADING`",
        "`WAKING`",
        "`ACTIVE`",
        "`COOLING`",
        "`QUIESCING`",
        "`CHECKPOINTING`",
        "`RETRY_BACKOFF`",
        "`QUARANTINED`",
    ):
        assert state in DESIGN
    assert "wake_after_checkpoint" in DESIGN
    assert "conditional snapshot/event commit succeeds" in DESIGN


def _protoc() -> str | None:
    return shutil.which("protoc")


def _buf_cli() -> list[str] | None:
    """Locate the buf CLI: on PATH, or as the pinned @bufbuild/buf dev dependency
    of the sibling engine checkout (see entropydrop_space_engine/package.json)."""
    if shutil.which("buf"):
        return ["buf"]
    sibling = (
        ROOT.parent / "entropydrop_space_engine" / "node_modules" / "@bufbuild" / "buf" / "bin" / "buf"
    )
    node = shutil.which("node")
    if node and sibling.is_file():
        return [node, str(sibling)]
    return None


def test_contract_protos_compile_with_protoc():
    protoc = _protoc()
    if protoc is None:
        raise AssertionError("protoc is not on PATH; install protoc 33.2 to verify the contracts")
    # Fail (do not skip): the contracts are part of the backend check surface and
    # protoc is pinned by the checked-in Python bindings.
    result = subprocess.run(
        [
            protoc,
            f"-I={CONTRACTS_DIR}",
            "--descriptor_set_out=/dev/null",
            "protocol.proto",
            "inventory_v6.proto",
        ],
        cwd=CONTRACTS_DIR,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_contract_protos_lint_clean_with_buf():
    import os
    import tempfile

    buf_cli = _buf_cli()
    if buf_cli is None:
        import pytest

        pytest.skip("buf CLI not available (install it or run `npm ci` in the sibling engine repo)")
    with tempfile.TemporaryDirectory(prefix="space-contracts-buf-") as cache:
        # Keep the buf content cache out of the (possibly read-only) user HOME.
        env = dict(os.environ, BUF_CACHE_DIR=cache)
        result = subprocess.run(
            [*buf_cli, "lint"],
            cwd=CONTRACTS_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stdout + result.stderr
