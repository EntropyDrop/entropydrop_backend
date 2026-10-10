"""Add the Cute figure kit without resetting existing inventory or prices."""
from alembic import op
import sqlalchemy as sa

revision = "f3c72a6b910e"
down_revision = "e6b93f1a0c25"
branch_labels = None
depends_on = None


def upgrade():
    products = sa.table(
        "model_sales_limits",
        sa.column("id", sa.String(16)),
        sa.column("model_type", sa.String(100)),
        sa.column("order_type", sa.String(20)),
        sa.column("price", sa.Float),
        sa.column("stock", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    if connection.execute(sa.select(products.c.id).where(products.c.model_type == "Cute DIY Kit")).first():
        return
    connection.execute(products.insert().values(
        id="CuTeKit3n7pQ2sVx", model_type="Cute DIY Kit", order_type="print",
        price=30.0, stock=300, created_at=sa.func.now(), updated_at=sa.func.now(),
    ))


def downgrade():
    # This is operational inventory, possibly referenced by paid orders.
    # Keep it intact when rolling back application schema changes.
    pass
