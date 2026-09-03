"""upgrade Space surface snapshots to multilevel sampling

Revision ID: c8e4f9a2b103
Revises: b7d3e8f1a902
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e4f9a2b103"
down_revision: Union[str, None] = "b7d3e8f1a902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_space_surface_zone_samples",
        "space_surface_zone_snapshots",
        type_="check",
    )
    op.alter_column(
        "space_surface_zone_snapshots",
        "samples_per_chunk_axis",
        existing_type=sa.SmallInteger(),
        server_default="8",
        existing_nullable=False,
    )
    op.alter_column(
        "space_surface_zone_snapshots",
        "schema_version",
        existing_type=sa.SmallInteger(),
        server_default="2",
        existing_nullable=False,
    )
    op.execute(
        "UPDATE space_surface_zone_snapshots "
        "SET samples_per_chunk_axis = 8, schema_version = 2, dirty = TRUE"
    )
    op.create_check_constraint(
        "ck_space_surface_zone_samples",
        "space_surface_zone_snapshots",
        "samples_per_chunk_axis = 8",
    )


def downgrade() -> None:
    # Surface rows are a derived cache. Discard v2 payloads so the v1 worker can
    # rebuild them without ever serving bytes under the wrong schema.
    op.execute("DELETE FROM space_surface_zone_snapshots")
    op.drop_constraint(
        "ck_space_surface_zone_samples",
        "space_surface_zone_snapshots",
        type_="check",
    )
    op.alter_column(
        "space_surface_zone_snapshots",
        "samples_per_chunk_axis",
        existing_type=sa.SmallInteger(),
        server_default="4",
        existing_nullable=False,
    )
    op.alter_column(
        "space_surface_zone_snapshots",
        "schema_version",
        existing_type=sa.SmallInteger(),
        server_default="1",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_space_surface_zone_samples",
        "space_surface_zone_snapshots",
        "samples_per_chunk_axis = 4",
    )
