"""compress Space chunks and bound new terrain receipts

Revision ID: a91c7e5d2b40
Revises: d37a6b9e2f14
Create Date: 2026-08-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import zstandard as zstd


revision: str = "a91c7e5d2b40"
down_revision: Union[str, None] = "d37a6b9e2f14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rewrite_chunk_payloads(compress: bool) -> None:
    bind = op.get_bind()
    source_codec = 0 if compress else 1
    rows = bind.execute(sa.text(
        """
        SELECT world_id, chunk_x, chunk_z, payload, uncompressed_size
        FROM chunk_snapshots
        WHERE codec = :source_codec
        """
    ), {"source_codec": source_codec})
    compressor = zstd.ZstdCompressor(level=6)
    decompressor = zstd.ZstdDecompressor()
    while True:
        batch = rows.fetchmany(64)
        if not batch:
            break
        for row in batch:
            payload = bytes(row.payload)
            if compress:
                if len(payload) != int(row.uncompressed_size):
                    continue
                rewritten = compressor.compress(payload)
                if len(rewritten) >= len(payload):
                    continue
                target_codec = 1
            else:
                try:
                    rewritten = decompressor.decompress(
                        payload,
                        max_output_size=int(row.uncompressed_size),
                    )
                except zstd.ZstdError:
                    continue
                if len(rewritten) != int(row.uncompressed_size):
                    continue
                target_codec = 0
            bind.execute(sa.text(
                """
                UPDATE chunk_snapshots
                SET payload = :payload, codec = :target_codec
                WHERE world_id = :world_id AND chunk_x = :chunk_x AND chunk_z = :chunk_z
                """
            ), {
                "payload": rewritten,
                "target_codec": target_codec,
                "world_id": row.world_id,
                "chunk_x": row.chunk_x,
                "chunk_z": row.chunk_z,
            })


def upgrade() -> None:
    op.add_column(
        "space_terrain_mutation_batches",
        sa.Column("dedupe_epoch", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "space_terrain_mutation_batches",
        sa.Column("client_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_space_terrain_batches_epoch",
        "space_terrain_mutation_batches",
        "dedupe_epoch IN (0, 1)",
    )
    op.create_check_constraint(
        "ck_space_terrain_batches_epoch_timestamp",
        "space_terrain_mutation_batches",
        "dedupe_epoch = 0 OR client_created_at IS NOT NULL",
    )
    op.create_index(
        "ix_space_terrain_batches_retention",
        "space_terrain_mutation_batches",
        ["dedupe_epoch", "client_created_at"],
        unique=False,
    )
    _rewrite_chunk_payloads(compress=True)


def downgrade() -> None:
    _rewrite_chunk_payloads(compress=False)
    op.drop_index(
        "ix_space_terrain_batches_retention",
        table_name="space_terrain_mutation_batches",
    )
    op.drop_constraint(
        "ck_space_terrain_batches_epoch_timestamp",
        "space_terrain_mutation_batches",
        type_="check",
    )
    op.drop_constraint(
        "ck_space_terrain_batches_epoch",
        "space_terrain_mutation_batches",
        type_="check",
    )
    op.drop_column("space_terrain_mutation_batches", "client_created_at")
    op.drop_column("space_terrain_mutation_batches", "dedupe_epoch")
