"""Add a 50% global figure promotion without repricing existing orders."""
from alembic import op
import sqlalchemy as sa

revision = "b8e17d930a42"
down_revision = "a6d3e9f127c8"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table("figure_discounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("discount_bps", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_figure_discount_singleton"),
        sa.CheckConstraint("discount_bps >= 0 AND discount_bps <= 9900", name="ck_figure_discount_rate"),
    )
    op.bulk_insert(table, [{"id": 1, "discount_bps": 5000, "version": 1}])
    op.add_column("orders", sa.Column("global_discount_bps", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("global_discount_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("orders", "global_discount_version")
    op.drop_column("orders", "global_discount_bps")
    op.drop_table("figure_discounts")
