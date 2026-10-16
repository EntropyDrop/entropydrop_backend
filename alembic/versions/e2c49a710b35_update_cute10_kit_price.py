"""Set the CUTE-10cm DIY kit price for new purchases to US$30."""
from alembic import op
import sqlalchemy as sa

revision = "e2c49a710b35"
down_revision = "c6a841f2309d"
branch_labels = None
depends_on = None


def upgrade():
    products = sa.table(
        "model_sales_limits",
        sa.column("model_type", sa.String(100)),
        sa.column("order_type", sa.String(20)),
        sa.column("price", sa.Float),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.get_bind().execute(
        products.update()
        .where(products.c.model_type == "Cute DIY Kit", products.c.order_type == "print")
        .values(price=30.0, updated_at=sa.func.now())
    )


def downgrade():
    # Prices are operational data. A schema rollback must not change active offers
    # or any order's agreed price; change the product price explicitly if needed.
    pass
