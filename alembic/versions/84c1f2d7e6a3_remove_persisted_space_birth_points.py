"""remove persisted Space birth points

Revision ID: 84c1f2d7e6a3
Revises: 6d42a1e8b9f0
Create Date: 2026-08-27 00:00:03.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "84c1f2d7e6a3"
down_revision: Union[str, None] = "6d42a1e8b9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_world_player_spawn_yaw",
        "world_player_profiles",
        type_="check",
    )
    op.drop_column("world_player_profiles", "spawn_yaw_q15")
    op.drop_column("world_player_profiles", "spawn_z_cm")
    op.drop_column("world_player_profiles", "spawn_y_cm")
    op.drop_column("world_player_profiles", "spawn_x_cm")


def downgrade() -> None:
    op.add_column(
        "world_player_profiles",
        sa.Column("spawn_x_cm", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "world_player_profiles",
        sa.Column("spawn_y_cm", sa.Integer(), nullable=False, server_default="3200"),
    )
    op.add_column(
        "world_player_profiles",
        sa.Column("spawn_z_cm", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "world_player_profiles",
        sa.Column("spawn_yaw_q15", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_world_player_spawn_yaw",
        "world_player_profiles",
        "spawn_yaw_q15 BETWEEN -32767 AND 32767",
    )
