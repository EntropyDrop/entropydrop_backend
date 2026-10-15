"""Keep direct purchases separate from pending shopping carts."""
from alembic import op
import sqlalchemy as sa

revision = "c6a841f2309d"
down_revision = "b7d8e9a10264"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("checkout_mode", sa.String(20), nullable=False, server_default="cart"))


def downgrade():
    op.drop_column("orders", "checkout_mode")
