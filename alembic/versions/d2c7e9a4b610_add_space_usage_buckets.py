"""add durable Space usage buckets

Revision ID: d2c7e9a4b610
Revises: c8e4f9a2b103
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2c7e9a4b610"
down_revision: Union[str, None] = "c8e4f9a2b103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "space_usage_buckets",
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=48), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.CheckConstraint("used >= 0", name="ck_space_usage_bucket_used"),
        sa.CheckConstraint("window_seconds > 0", name="ck_space_usage_bucket_window"),
        sa.PrimaryKeyConstraint(
            "principal_id", "scope_id", "metric", "window_seconds", "bucket_start"
        ),
    )
    op.create_index(
        "ix_space_usage_buckets_retention",
        "space_usage_buckets",
        ["metric", "window_seconds", "bucket_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_space_usage_buckets_retention", table_name="space_usage_buckets")
    op.drop_table("space_usage_buckets")
