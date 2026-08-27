from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "space/contracts/schema.sql").read_text()
PROTOCOL = (ROOT / "space/contracts/protocol.proto").read_text()
DESIGN = (ROOT / "docs/space-backend.md").read_text()


def test_space_contract_extends_existing_users_without_second_identity_or_skin_store():
    assert "CREATE TABLE users" not in SCHEMA
    assert "CREATE TABLE accounts" not in SCHEMA
    assert "REFERENCES users(id)" in SCHEMA
    assert "user_id" in SCHEMA
    assert "player_appearance_assets" not in SCHEMA
    assert "AppearanceCommand" not in PROTOCOL
    assert "string user_id = 2;" in PROTOCOL
    assert "string minecraft_skin_url" in PROTOCOL
    assert "string minecraft_skin_model" in PROTOCOL


def test_space_contract_covers_persistence_queue_and_browser_only_backpack():
    for table in (
        "chunk_snapshots",
        "world_events",
        "entity_snapshots",
        "entity_chunk_coverage",
        "world_player_profiles",
        "world_session_slots",
        "world_join_queue",
    ):
        assert f"CREATE TABLE {table}" in SCHEMA
    assert "slot_number BETWEEN 0 AND 31" in SCHEMA
    assert "player_inventories" not in SCHEMA
    assert "space.backpack.v2" in DESIGN
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
