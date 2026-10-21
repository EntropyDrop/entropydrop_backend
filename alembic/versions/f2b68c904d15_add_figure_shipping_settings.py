"""Configure flat kit shipping, defaulting to $12.90 for new orders."""
from decimal import Decimal

from alembic import op
import sqlalchemy as sa

revision = "f2b68c904d15"
down_revision = "e8a42f617c03"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table("figure_shipping",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("shipping_fee", sa.Numeric(8, 2), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_figure_shipping_singleton"),
        sa.CheckConstraint("shipping_fee >= 0 AND shipping_fee <= 999999.99", name="ck_figure_shipping_fee"),
    )
    op.bulk_insert(table, [{"id": 1, "shipping_fee": Decimal("12.90"), "version": 1}])
    # orders.shipping_fee already stores each purchase's agreed fee.


def downgrade():
    op.drop_table("figure_shipping")
