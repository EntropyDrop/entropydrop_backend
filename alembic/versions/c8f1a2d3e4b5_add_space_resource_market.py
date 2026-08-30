"""add Space resource market

Revision ID: c8f1a2d3e4b5
Revises: a91c7e5d2b40
Create Date: 2026-08-30 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8f1a2d3e4b5"
down_revision: Union[str, None] = "a91c7e5d2b40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "space_market_resources",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("publisher_user_id", sa.String(length=16), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), server_default="2", nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("license", sa.String(length=32), server_default="AGPL-3.0-only", nullable=False),
        sa.Column("content_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("block_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("node_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("script_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("downloads_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("likes_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_user_id", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            "downloads_count >= 0 AND likes_count >= 0",
            name="ck_space_market_resource_counts",
        ),
        sa.CheckConstraint(
            "kind IN ('blockset', 'entity', 'colorset')",
            name="ck_space_market_resource_kind",
        ),
        sa.CheckConstraint(
            "license = 'AGPL-3.0-only'",
            name="ck_space_market_resource_license",
        ),
        sa.CheckConstraint(
            "schema_version = 2",
            name="ck_space_market_resource_schema_version",
        ),
        sa.ForeignKeyConstraint(["deleted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["publisher_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_digest", name="uq_space_market_resource_digest"),
    )
    op.create_index(
        "ix_space_market_resources_downloads",
        "space_market_resources",
        ["deleted_at", "kind", "downloads_count", "created_at"],
    )
    op.create_index(
        "ix_space_market_resources_likes",
        "space_market_resources",
        ["deleted_at", "kind", "likes_count", "created_at"],
    )
    op.create_index(
        "ix_space_market_resources_latest",
        "space_market_resources",
        ["deleted_at", "kind", "created_at"],
    )
    op.create_index(
        "ix_space_market_resources_publisher_day",
        "space_market_resources",
        ["publisher_user_id", "created_at"],
    )
    op.create_table(
        "space_market_resource_likes",
        sa.Column("resource_id", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["space_market_resources.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("resource_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("space_market_resource_likes")
    op.drop_index(
        "ix_space_market_resources_publisher_day",
        table_name="space_market_resources",
    )
    op.drop_index(
        "ix_space_market_resources_latest",
        table_name="space_market_resources",
    )
    op.drop_index(
        "ix_space_market_resources_likes",
        table_name="space_market_resources",
    )
    op.drop_index(
        "ix_space_market_resources_downloads",
        table_name="space_market_resources",
    )
    op.drop_table("space_market_resources")
