"""add Space world and stable player profiles

Revision ID: f5a8d901c2e4
Revises: e3d489b1c7a2
Create Date: 2026-08-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5a8d901c2e4"
down_revision: Union[str, None] = "e3d489b1c7a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worlds",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("owner_user_id", sa.String(length=16), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("terrain_generator_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("width_chunks", sa.Integer(), nullable=False, server_default="1024"),
        sa.Column("length_chunks", sa.Integer(), nullable=False, server_default="128"),
        sa.Column("zone_size_chunks", sa.Integer(), nullable=False, server_default="32"),
        sa.Column("max_online_players", sa.Integer(), nullable=False, server_default="32"),
        sa.Column("status", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_online_players BETWEEN 1 AND 32", name="ck_worlds_player_limit"),
        sa.CheckConstraint("width_chunks > 0 AND length_chunks > 0", name="ck_worlds_dimensions"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_worlds_owner_user_id", "worlds", ["owner_user_id"], unique=False)

    op.create_table(
        "world_player_profiles",
        sa.Column("world_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.String(length=16), nullable=False),
        sa.Column("player_entity_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("spawn_x_cm", sa.Integer(), nullable=False),
        sa.Column("spawn_y_cm", sa.Integer(), nullable=False),
        sa.Column("spawn_z_cm", sa.Integer(), nullable=False),
        sa.Column("spawn_yaw_q15", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("spawn_yaw_q15 BETWEEN -32767 AND 32767", name="ck_world_player_spawn_yaw"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("world_id", "user_id"),
        sa.UniqueConstraint("world_id", "player_entity_id", name="uq_world_player_entity"),
    )


def downgrade() -> None:
    op.drop_table("world_player_profiles")
    op.drop_index("ix_worlds_owner_user_id", table_name="worlds")
    op.drop_table("worlds")
