"""prepare Space market Protobuf v3 conversion

Revision ID: a6b3d9f142ce
Revises: f8c2a91d4e70

Run ``python scripts/convert_space_market_to_protobuf.py`` immediately after this
migration. The temporary v2/v3 constraint lets the converter update each CDN
object and its database row atomically without taking the whole market offline.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6b3d9f142ce"
down_revision: Union[str, None] = "f8c2a91d4e70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        "schema_version IN (2, 3)",
    )
    op.alter_column(
        "space_market_resources",
        "schema_version",
        existing_type=sa.SmallInteger(),
        server_default="3",
        existing_nullable=False,
    )
    op.drop_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        "(schema_version = 2 AND (object_key IS NOT NULL OR content IS NOT NULL)) "
        "OR (schema_version = 3 AND object_key IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_space_market_resource_storage",
        "space_market_resources",
        "object_key IS NOT NULL OR content IS NOT NULL",
    )
    op.alter_column(
        "space_market_resources",
        "schema_version",
        existing_type=sa.SmallInteger(),
        server_default="2",
        existing_nullable=False,
    )
    op.drop_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        "schema_version = 2",
    )
