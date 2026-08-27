"""add Space world terrain event streams

Revision ID: d37a6b9e2f14
Revises: 84c1f2d7e6a3
Create Date: 2026-08-27 21:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d37a6b9e2f14"
down_revision: Union[str, None] = "84c1f2d7e6a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "world_event_streams",
        sa.Column("world_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("last_event_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("last_event_id >= 0", name="ck_world_event_streams_last_event"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("world_id"),
    )
    op.execute(
        """
        INSERT INTO world_event_streams (world_id, last_event_id, updated_at)
        SELECT worlds.id, COALESCE(MAX(chunk_snapshots.last_event_id), 0), CURRENT_TIMESTAMP
        FROM worlds
        LEFT JOIN chunk_snapshots ON chunk_snapshots.world_id = worlds.id
        GROUP BY worlds.id
        """
    )


def downgrade() -> None:
    op.drop_table("world_event_streams")
