"""add Space far-surface zone snapshots

Revision ID: b7d3e8f1a902
Revises: a6f2c8d914e3
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d3e8f1a902"
down_revision: Union[str, None] = "a6f2c8d914e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "space_surface_zone_snapshots",
        sa.Column("world_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("zone_x", sa.SmallInteger(), nullable=False),
        sa.Column("zone_z", sa.SmallInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("source_terrain_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("terrain_generator_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("samples_per_chunk_axis", sa.SmallInteger(), server_default="4", nullable=False),
        sa.Column("codec", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("uncompressed_size", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("dirty", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("zone_x >= 0 AND zone_z >= 0", name="ck_space_surface_zone_position"),
        sa.CheckConstraint("revision >= 1", name="ck_space_surface_zone_revision"),
        sa.CheckConstraint("source_terrain_revision >= 0", name="ck_space_surface_zone_source_revision"),
        sa.CheckConstraint("samples_per_chunk_axis = 4", name="ck_space_surface_zone_samples"),
        sa.CheckConstraint("codec = 1", name="ck_space_surface_zone_codec"),
        sa.CheckConstraint("uncompressed_size > 0", name="ck_space_surface_zone_size"),
        sa.CheckConstraint("octet_length(content_hash) = 32", name="ck_space_surface_zone_hash"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("world_id", "zone_x", "zone_z"),
    )
    op.create_index(
        "ix_space_surface_zones_world_revision",
        "space_surface_zone_snapshots",
        ["world_id", "revision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_space_surface_zones_world_revision",
        table_name="space_surface_zone_snapshots",
    )
    op.drop_table("space_surface_zone_snapshots")
