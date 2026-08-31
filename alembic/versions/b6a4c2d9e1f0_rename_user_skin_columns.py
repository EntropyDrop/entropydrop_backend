"""rename user skin columns

Revision ID: b6a4c2d9e1f0
Revises: f2c9d10a4b7e
Create Date: 2026-08-31
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b6a4c2d9e1f0"
down_revision: Union[str, None] = "f2c9d10a4b7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("users", "minecraft_skin_url", new_column_name="skin_url")
    op.alter_column("users", "minecraft_skin_model", new_column_name="skin_type")


def downgrade() -> None:
    op.alter_column("users", "skin_type", new_column_name="minecraft_skin_model")
    op.alter_column("users", "skin_url", new_column_name="minecraft_skin_url")
