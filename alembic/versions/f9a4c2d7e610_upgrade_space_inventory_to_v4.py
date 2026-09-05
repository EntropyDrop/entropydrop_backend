"""upgrade Space inventory resources to Protobuf v4

Revision ID: f9a4c2d7e610
Revises: d2c7e9a4b610
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9a4c2d7e610"
down_revision: Union[str, None] = "d2c7e9a4b610"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # V3 constraint references used string sentinels and cannot be interpreted
    # safely as v4. This intentionally starts the pre-launch market and worlds
    # without any entities encoded under the obsolete contract.
    op.execute("DELETE FROM space_market_resources")
    op.execute("DELETE FROM space_world_entities")
    op.drop_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        type_="check",
    )
    op.alter_column(
        "space_market_resources",
        "schema_version",
        existing_type=sa.SmallInteger(),
        server_default="4",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        "schema_version = 4",
    )
    op.alter_column(
        "space_world_entities",
        "schema_version",
        existing_type=sa.SmallInteger(),
        server_default="4",
        existing_nullable=False,
    )


def downgrade() -> None:
    # V4 records are equally unsafe to reinterpret as v3.
    op.execute("DELETE FROM space_market_resources")
    op.execute("DELETE FROM space_world_entities")
    op.drop_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        type_="check",
    )
    op.alter_column(
        "space_market_resources",
        "schema_version",
        existing_type=sa.SmallInteger(),
        server_default="3",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_space_market_resource_schema_version",
        "space_market_resources",
        "schema_version = 3",
    )
    op.alter_column(
        "space_world_entities",
        "schema_version",
        existing_type=sa.SmallInteger(),
        server_default="3",
        existing_nullable=False,
    )
