"""Move Space inventory names into components (Protobuf v5).

Revision ID: b6d1e4f80237
Revises: aa7e52c91604
"""
from alembic import op
import sqlalchemy as sa

revision = "b6d1e4f80237"
down_revision = "aa7e52c91604"
branch_labels = None
depends_on = None


def _replace_contract(version: int) -> None:
    # Pre-launch breaking migration: old definitions are deliberately discarded.
    # This affects only resource/entity records, never terrain or user accounts.
    op.execute("DELETE FROM space_market_resources")
    op.execute("DELETE FROM space_world_entities")
    op.drop_constraint("ck_space_market_resource_schema_version", "space_market_resources", type_="check")
    for table in ("space_market_resources", "space_world_entities"):
        op.alter_column(table, "schema_version", existing_type=sa.SmallInteger(),
                        server_default=str(version), existing_nullable=False)
    op.create_check_constraint("ck_space_market_resource_schema_version", "space_market_resources",
                               f"schema_version = {version}")


def upgrade() -> None:
    _replace_contract(5)


def downgrade() -> None:
    _replace_contract(4)
