"""Persist the selected shipping source; existing orders keep website addresses."""
from alembic import op
import sqlalchemy as sa

revision = "b7d8e9a10264"
down_revision = "a9c64e280fb1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("shipping_source", sa.String(20), nullable=False, server_default="address_book"))


def downgrade():
    op.drop_column("orders", "shipping_source")
