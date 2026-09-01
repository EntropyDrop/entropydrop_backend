#!/usr/bin/env python3
"""One-time conversion of active Space market JSON objects to Protobuf v3.

Deployment order:
  1. alembic upgrade a6b3d9f142ce
  2. python scripts/convert_space_market_to_protobuf.py --dry-run
  3. python scripts/convert_space_market_to_protobuf.py
  4. alembic upgrade b8e4c7a261d0
  5. alembic upgrade c3f7a92d10be

The command is idempotent. A v3 database row is committed only after its new
immutable CDN object has been uploaded. Old JSON objects are invalidated and
deleted only after the database commit succeeds.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import s3_utils  # noqa: E402
from database import engine  # noqa: E402
from routers.space_market import (  # noqa: E402
    _market_object_key,
    validate_market_payload,
)
from space.inventory_codec import (  # noqa: E402
    SCHEMA_VERSION,
    encode_inventory_resource,
    inventory_content_digest,
)


logger = logging.getLogger("space-market-protobuf-converter")

SELECT_LEGACY_WITH_CONTENT = text("""
    SELECT id, kind, schema_version, object_key, content, deleted_at
    FROM space_market_resources
    WHERE deleted_at IS NULL
      AND (schema_version <> :schema_version OR object_key IS NULL OR object_key NOT LIKE '%.pb')
    ORDER BY created_at, id
""")

SELECT_LEGACY_WITHOUT_CONTENT = text("""
    SELECT id, kind, schema_version, object_key, NULL AS content, deleted_at
    FROM space_market_resources
    WHERE deleted_at IS NULL
      AND (schema_version <> :schema_version OR object_key IS NULL OR object_key NOT LIKE '%.pb')
    ORDER BY created_at, id
""")

UPDATE_RESOURCE = text("""
    UPDATE space_market_resources
    SET schema_version = :schema_version,
        name = :name,
        content_digest = :content_digest,
        object_key = :object_key,
        content = NULL,
        size_bytes = :size_bytes,
        block_count = :block_count,
        node_count = :node_count,
        script_count = :script_count
    WHERE id = :resource_id
""")


def _legacy_payload(row: dict[str, Any]) -> dict[str, Any]:
    content = row.get("content")
    if content is not None:
        if isinstance(content, str):
            content = json.loads(content)
        if not isinstance(content, dict):
            raise ValueError("database content is not a JSON object")
        return dict(content)
    object_key = row.get("object_key")
    if not object_key:
        raise ValueError("resource has neither database content nor a CDN object key")
    decoded = json.loads(s3_utils.download_from_s3(object_key, is_public=True))
    if not isinstance(decoded, dict):
        raise ValueError("CDN content is not a JSON object")
    return decoded


def _cleanup_old_object(object_key: str | None) -> bool:
    if not object_key or object_key.endswith(".pb"):
        return False
    try:
        s3_utils.invalidate_cdn_object(object_key)
        s3_utils.delete_from_s3_strict(object_key, is_public=True)
        return True
    except Exception:
        logger.exception("Converted successfully but could not remove old object %s", object_key)
        return False


def convert(*, dry_run: bool = False, keep_old: bool = False) -> dict[str, int]:
    columns = {column["name"] for column in inspect(engine).get_columns("space_market_resources")}
    select_legacy = (
        SELECT_LEGACY_WITH_CONTENT
        if "content" in columns
        else SELECT_LEGACY_WITHOUT_CONTENT
    )
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(
            select_legacy,
            {"schema_version": SCHEMA_VERSION},
        ).mappings()]

    result = {"found": len(rows), "converted": 0, "old_objects_deleted": 0, "failed": 0}
    for row in rows:
        resource_id = str(row["id"])
        new_object_key: str | None = None
        uploaded = False
        try:
            payload = _legacy_payload(row)
            payload["version"] = SCHEMA_VERSION
            kind = str(row["kind"])
            canonical = validate_market_payload(kind, payload)
            encoded = encode_inventory_resource(kind, canonical)
            digest = inventory_content_digest(kind, canonical)
            new_object_key = _market_object_key(resource_id, digest)
            logger.info(
                "%s: %s -> %s (%d bytes)",
                resource_id,
                row.get("object_key") or "database JSON",
                new_object_key,
                len(encoded),
            )
            if dry_run:
                continue

            s3_utils.upload_to_s3(
                encoded,
                new_object_key,
                is_public=True,
                content_type="application/x-protobuf",
            )
            uploaded = True
            with engine.begin() as connection:
                connection.execute(UPDATE_RESOURCE, {
                    "schema_version": SCHEMA_VERSION,
                    "name": canonical["name"],
                    "content_digest": digest,
                    "object_key": new_object_key,
                    "size_bytes": len(encoded),
                    "block_count": len(canonical.get("blocks", [])),
                    "node_count": int(canonical.get("nodeCount", 0)),
                    "script_count": len(canonical.get("scripts", [])),
                    "resource_id": resource_id,
                })
            result["converted"] += 1
            if not keep_old and row.get("object_key") != new_object_key:
                result["old_objects_deleted"] += int(_cleanup_old_object(row.get("object_key")))
        except Exception:
            result["failed"] += 1
            logger.exception("Failed to convert market resource %s", resource_id)
            if uploaded and new_object_key:
                try:
                    s3_utils.delete_from_s3_strict(new_object_key, is_public=True)
                except Exception:
                    logger.exception("Could not compensate uploaded object %s", new_object_key)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    parser.add_argument("--keep-old", action="store_true", help="keep converted JSON CDN objects")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = convert(dry_run=args.dry_run, keep_old=args.keep_old)
    logger.info("summary: %s", result)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
