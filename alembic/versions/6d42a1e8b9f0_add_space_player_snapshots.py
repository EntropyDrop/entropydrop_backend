"""add durable Space player reconnect snapshots

Revision ID: 6d42a1e8b9f0
Revises: 19f3c8c5a7d1
Create Date: 2026-08-27 00:00:02.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6d42a1e8b9f0"
down_revision: Union[str, None] = "19f3c8c5a7d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_snapshots",
        sa.Column("world_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_event_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state_version", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("state", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_player_snapshots_revision"),
        sa.CheckConstraint("last_event_id >= 0", name="ck_player_snapshots_last_event"),
        sa.CheckConstraint("state_version >= 1", name="ck_player_snapshots_state_version"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("world_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("player_snapshots")
