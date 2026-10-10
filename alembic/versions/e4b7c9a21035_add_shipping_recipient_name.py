"""Store explicitly provided shipping recipient names without guessing old names."""
from alembic import op
import sqlalchemy as sa

revision = "e4b7c9a21035"
down_revision = "d2f81a604bc9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("shipping_addresses", sa.Column("recipient_name", sa.String(300), nullable=False, server_default=""))
    # Historical order snapshots are immutable; do not copy account nicknames.


def downgrade():
    op.drop_column("shipping_addresses", "recipient_name")
