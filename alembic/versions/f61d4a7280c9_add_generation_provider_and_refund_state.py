"""add generation provider and refund state

Revision ID: f61d4a7280c9
Revises: a320925161ce
Create Date: 2026-07-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f61d4a7280c9"
down_revision: Union[str, None] = "a320925161ce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "generation_logs",
        sa.Column("pipeline_version", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "generation_logs",
        sa.Column("provider_task_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "generation_logs",
        sa.Column(
            "credits_charged",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "generation_logs",
        sa.Column(
            "credits_refunded",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_generation_logs_provider_task_id"),
        "generation_logs",
        ["provider_task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_generation_logs_provider_task_id"),
        table_name="generation_logs",
    )
    op.drop_column("generation_logs", "credits_refunded")
    op.drop_column("generation_logs", "credits_charged")
    op.drop_column("generation_logs", "provider_task_id")
    op.drop_column("generation_logs", "pipeline_version")
