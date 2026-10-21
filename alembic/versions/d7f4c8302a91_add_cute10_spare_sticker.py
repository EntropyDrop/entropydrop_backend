"""Include a spare sticker sheet in new CUTE-10cm kit purchases."""
from copy import deepcopy

from alembic import op
import sqlalchemy as sa

revision = "d7f4c8302a91"
down_revision = "c9f25a7e104d"
branch_labels = None
depends_on = None


def update_sticker(quantity, description):
    products = sa.table(
        "model_sales_limits",
        sa.column("id", sa.String(16)),
        sa.column("model_type", sa.String(100)),
        sa.column("order_type", sa.String(20)),
        sa.column("kit_specifications", sa.JSON()),
    )
    connection = op.get_bind()
    kits = connection.execute(sa.select(products.c.id, products.c.kit_specifications).where(
        products.c.model_type == "Cute DIY Kit", products.c.order_type == "print"
    )).mappings().all()
    for kit in kits:
        specifications = deepcopy(kit["kit_specifications"])
        if not specifications or specifications.get("product_name") != "CUTE-10cm DIY kit":
            continue
        for material in specifications.get("materials", []):
            if material.get("name") == "Pre-cut sticker sheet":
                material.update(quantity=quantity, description=description)
        connection.execute(products.update().where(products.c.id == kit["id"]).values(
            kit_specifications=specifications
        ))
    # Preserve other materials, prices, stock and all purchase-time snapshots.


def upgrade():
    update_sticker(2, "Includes 1 spare sheet.")


def downgrade():
    update_sticker(1, None)
