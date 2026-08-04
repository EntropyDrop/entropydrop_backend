"""add provider submission state

Revision ID: 9b31c72f5d8a
Revises: f61d4a7280c9
Create Date: 2026-08-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9b31c72f5d8a"
down_revision: Union[str, None] = "f61d4a7280c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "generation_logs",
        sa.Column(
            "provider_submission_state",
            sa.String(length=20),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_generation_logs_provider_submission_state"),
        "generation_logs",
        ["provider_submission_state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_generation_logs_provider_submission_state"),
        table_name="generation_logs",
    )
    op.drop_column("generation_logs", "provider_submission_state")
