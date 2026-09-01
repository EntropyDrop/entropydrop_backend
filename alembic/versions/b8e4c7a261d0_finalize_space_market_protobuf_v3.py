"""finalize Space market Protobuf v3 schema

Revision ID: b8e4c7a261d0
Revises: a6b3d9f142ce

Existing installations must run ``tools/convert_space_market_to_protobuf.py``
between the prepare and finalize revisions. Fresh databases can upgrade through
both revisions normally because they contain no legacy market rows.
"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "b8e4c7a261d0"
down_revision: Union[str, None] = "a6b3d9f142ce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        remaining = connection.execute(sa.text("""
            SELECT COUNT(*)
            FROM space_market_resources
            WHERE deleted_at IS NULL
              AND (schema_version <> 3 OR object_key IS NULL OR object_key NOT LIKE '%.pb')
        """)).scalar_one()
        if remaining:
            raise RuntimeError(
                f"{remaining} active Space market resources still need Protobuf conversion; "
                "run scripts/convert_space_market_to_protobuf.py first"
            )

    op.drop_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        type_="check",
    )
    op.drop_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        type_="check",
    )
    op.drop_column("space_market_resources", "content")
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


def downgrade() -> None:
    op.drop_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        type_="check",
    )
    op.drop_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        type_="check",
    )
    op.add_column(
        "space_market_resources",
        sa.Column("content", sa.JSON(), nullable=True),
    )
    op.create_check_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        "schema_version IN (2, 3)",
    )
    op.create_check_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        "(schema_version = 2 AND (object_key IS NOT NULL OR content IS NOT NULL)) "
        "OR (schema_version = 3 AND object_key IS NOT NULL)",
    )
