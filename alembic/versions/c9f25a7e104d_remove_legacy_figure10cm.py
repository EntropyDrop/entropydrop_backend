"""Remove the retired Figure10cm catalog entry; preserve historical orders."""
from alembic import op
import sqlalchemy as sa

revision = "c9f25a7e104d"
down_revision = "b8e17d930a42"
branch_labels = None
depends_on = None


def upgrade():
    products = sa.table(
        "model_sales_limits",
        sa.column("model_type", sa.String(100)),
        sa.column("order_type", sa.String(20)),
    )
    op.get_bind().execute(
        products.delete().where(
            products.c.model_type == "Figure10cm",
            products.c.order_type == "print",
        )
    )
    # Order items store their model type and price independently of the catalog.


def downgrade():
    # A rollback must not relist a retired product with invented stock/pricing.
    # Reintroducing it requires an explicit catalog entry with current terms.
    pass
