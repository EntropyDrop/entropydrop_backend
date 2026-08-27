"""add durable Space chunk snapshots and terrain batch receipts

Revision ID: 19f3c8c5a7d1
Revises: f5a8d901c2e4
Create Date: 2026-08-27 00:00:01.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "19f3c8c5a7d1"
down_revision: Union[str, None] = "f5a8d901c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chunk_snapshots",
        sa.Column("world_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("chunk_x", sa.Integer(), nullable=False),
        sa.Column("chunk_z", sa.Integer(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_event_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("codec", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("codec_version", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("uncompressed_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chunk_x >= 0", name="ck_chunk_snapshots_x"),
        sa.CheckConstraint("chunk_z >= 0", name="ck_chunk_snapshots_z"),
        sa.CheckConstraint("revision >= 0", name="ck_chunk_snapshots_revision"),
        sa.CheckConstraint("uncompressed_size >= 0", name="ck_chunk_snapshots_size"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("world_id", "chunk_x", "chunk_z"),
    )
    op.create_index(
        "ix_chunk_snapshots_world_revision",
        "chunk_snapshots",
        ["world_id", "revision"],
        unique=False,
    )
    op.create_index(
        "chunk_snapshots_resume_idx",
        "chunk_snapshots",
        ["world_id", "last_event_id"],
        unique=False,
    )

    op.create_table(
        "space_terrain_mutation_batches",
        sa.Column("world_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("batch_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("actor_user_id", sa.String(length=16), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("world_id", "batch_id"),
    )


def downgrade() -> None:
    op.drop_table("space_terrain_mutation_batches")
    op.drop_index("chunk_snapshots_resume_idx", table_name="chunk_snapshots")
    op.drop_index("ix_chunk_snapshots_world_revision", table_name="chunk_snapshots")
    op.drop_table("chunk_snapshots")
