"""Describe CUTE-10cm stickers as translucent, waterproof and durable."""
from copy import deepcopy

from alembic import op
import sqlalchemy as sa

revision = "b6e28d915c40"
down_revision = "a5d9e4072b63"
branch_labels = None
depends_on = None

OLD_NAME = "Pre-cut translucent waterproof sticker sheet"
NEW_NAME = "Translucent, waterproof and durable sticker sheet"


def rename_sticker(old_name, new_name):
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
        changed = False
        for material in specifications.get("materials", []):
            if material.get("name") == old_name:
                material["name"] = new_name
                changed = True
        if changed:
            connection.execute(products.update().where(products.c.id == kit["id"]).values(
                kit_specifications=specifications
            ))
    # Keep quantities, spare-sheet description, other materials and orders intact.


def upgrade():
    rename_sticker(OLD_NAME, NEW_NAME)


def downgrade():
    rename_sticker(NEW_NAME, OLD_NAME)
