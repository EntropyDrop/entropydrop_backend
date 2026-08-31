"""move Space market content to CDN-backed object storage

Revision ID: d4e7f1a9c2b3
Revises: b6a4c2d9e1f0
Create Date: 2026-08-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e7f1a9c2b3"
down_revision: Union[str, None] = "b6a4c2d9e1f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "space_market_resources",
        sa.Column("object_key", sa.String(length=512), nullable=True),
    )
    op.alter_column(
        "space_market_resources",
        "content",
        existing_type=sa.JSON(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        "object_key IS NOT NULL OR content IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        type_="check",
    )
    # A downgrade cannot retrieve CDN objects. Preserve row validity while
    # restoring the old non-null schema; downgrade is structural, not a content
    # restoration mechanism.
    op.execute("UPDATE space_market_resources SET content = '{}'::json WHERE content IS NULL")
    op.alter_column(
        "space_market_resources",
        "content",
        existing_type=sa.JSON(),
        nullable=False,
    )
    op.drop_column("space_market_resources", "object_key")
