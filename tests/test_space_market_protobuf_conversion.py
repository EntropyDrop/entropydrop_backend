import json

from sqlalchemy import create_engine, text

from space.inventory_codec import decode_inventory_resource
from scripts import convert_space_market_to_protobuf as converter


def test_one_time_converter_uploads_protobuf_before_repointing_the_market_row(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE space_market_resources (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                name TEXT NOT NULL,
                content_digest BLOB NOT NULL UNIQUE,
                object_key TEXT,
                content JSON,
                preview JSON NOT NULL,
                size_bytes INTEGER NOT NULL,
                block_count INTEGER NOT NULL,
                node_count INTEGER NOT NULL,
                script_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
        """))
        legacy = {
            "type": "space-colorset",
            "version": 2,
            "name": "Legacy palette",
            "colors": ["#123456"] * 9,
        }
        connection.execute(text("""
            INSERT INTO space_market_resources (
                id, kind, schema_version, name, content_digest, object_key,
                content, preview, size_bytes, block_count, node_count,
                script_count, created_at, deleted_at
            ) VALUES (
                'legacy-1', 'colorset', 2, 'Legacy palette', :digest, NULL,
                :content, '{}', 1, 0, 0, 0, '2026-01-01', NULL
            )
        """), {"digest": b"x" * 32, "content": json.dumps(legacy)})

    objects: dict[str, bytes] = {}
    monkeypatch.setattr(converter, "engine", engine)
    monkeypatch.setattr(
        converter.s3_utils,
        "upload_to_s3",
        lambda body, key, **_kwargs: objects.setdefault(key, bytes(body)),
    )
    monkeypatch.setattr(
        converter.s3_utils,
        "delete_from_s3_strict",
        lambda key, **_kwargs: objects.pop(key, None),
    )

    result = converter.convert()

    assert result == {"found": 1, "converted": 1, "old_objects_deleted": 0, "failed": 0}
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT schema_version, object_key, content, size_bytes
            FROM space_market_resources WHERE id = 'legacy-1'
        """)).mappings().one()
    assert row["schema_version"] == 3
    assert row["object_key"].endswith(".pb")
    assert row["content"] is None
    assert row["size_bytes"] == len(objects[row["object_key"]])
    kind, payload = decode_inventory_resource(objects[row["object_key"]])
    assert kind == "colorset"
    assert payload["name"] == "Legacy palette"
