"""drop derived Space market preview metadata

Revision ID: c3f7a92d10be
Revises: b8e4c7a261d0
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f7a92d10be"
down_revision: Union[str, None] = "b8e4c7a261d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("space_market_resources", "preview")


def downgrade() -> None:
    op.add_column(
        "space_market_resources",
        sa.Column(
            "preview",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.alter_column(
        "space_market_resources",
        "preview",
        existing_type=sa.JSON(),
        server_default=None,
        existing_nullable=False,
    )
