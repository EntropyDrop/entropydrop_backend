"""Durable receipts for entity configuration and run-state requests."""
from alembic import op
import sqlalchemy as sa

revision = 'e3a97d50c812'
down_revision = 'c7a4d8e90512'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("space_entity_operations",
        sa.Column("world_id", sa.Uuid(), sa.ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("request_digest", sa.LargeBinary(32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False))


def downgrade():
    op.drop_table("space_entity_operations")
