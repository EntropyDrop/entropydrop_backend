"""hard delete Space market resources

Revision ID: e19b4c7d2a60
Revises: 1d623104f578
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e19b4c7d2a60"
down_revision: Union[str, None] = "1d623104f578"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Historical soft-deletion audit rows are intentionally removed before the
    # audit columns are dropped. Resource likes follow through ON DELETE CASCADE.
    op.execute("DELETE FROM space_market_resources WHERE deleted_at IS NOT NULL")

    op.drop_index("ix_space_market_resources_downloads", table_name="space_market_resources")
    op.drop_index("ix_space_market_resources_likes", table_name="space_market_resources")
    op.drop_index("ix_space_market_resources_latest", table_name="space_market_resources")
    op.drop_constraint("ck_space_market_resource_storage", "space_market_resources", type_="check")
    op.drop_constraint("ck_space_market_resource_schema_version", "space_market_resources", type_="check")
    op.drop_constraint(
        "space_market_resources_deleted_by_user_id_fkey",
        "space_market_resources",
        type_="foreignkey",
    )
    op.drop_column("space_market_resources", "deleted_by_user_id")
    op.drop_column("space_market_resources", "deleted_at")

    op.alter_column("space_market_resources", "object_key", existing_type=sa.String(length=512), nullable=False)
    op.create_check_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        "schema_version = 3",
    )
    op.create_check_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        "object_key IS NOT NULL",
    )
    op.create_index(
        "ix_space_market_resources_downloads",
        "space_market_resources",
        ["kind", "downloads_count", "created_at"],
    )
    op.create_index(
        "ix_space_market_resources_likes",
        "space_market_resources",
        ["kind", "likes_count", "created_at"],
    )
    op.create_index(
        "ix_space_market_resources_latest",
        "space_market_resources",
        ["kind", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_space_market_resources_latest", table_name="space_market_resources")
    op.drop_index("ix_space_market_resources_likes", table_name="space_market_resources")
    op.drop_index("ix_space_market_resources_downloads", table_name="space_market_resources")
    op.drop_constraint("ck_space_market_resource_storage", "space_market_resources", type_="check")
    op.drop_constraint("ck_space_market_resource_schema_version", "space_market_resources", type_="check")

    op.add_column(
        "space_market_resources",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "space_market_resources",
        sa.Column("deleted_by_user_id", sa.String(length=16), nullable=True),
    )
    op.create_foreign_key(
        "space_market_resources_deleted_by_user_id_fkey",
        "space_market_resources",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("space_market_resources", "object_key", existing_type=sa.String(length=512), nullable=True)
    op.create_check_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        "deleted_at IS NOT NULL OR schema_version = 3",
    )
    op.create_check_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        "deleted_at IS NOT NULL OR object_key IS NOT NULL",
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
