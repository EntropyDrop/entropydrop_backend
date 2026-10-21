"""Clarify that CUTE-10cm stickers are pre-cut and need no cutting."""
from copy import deepcopy

from alembic import op
import sqlalchemy as sa

revision = "c8f43a601d92"
down_revision = "b6e28d915c40"
branch_labels = None
depends_on = None

OLD_NAME = "Translucent, waterproof and durable sticker sheet"
NEW_NAME = "Pre-cut translucent, waterproof and durable sticker sheet"
OLD_DESCRIPTION = "Includes 1 spare sheet."
NEW_DESCRIPTION = "No cutting required. Includes 1 spare sheet."


def update_sticker(old_name, new_name, old_description, new_description):
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
                if material.get("description") == old_description:
                    material["description"] = new_description
                changed = True
        if changed:
            connection.execute(products.update().where(products.c.id == kit["id"]).values(
                kit_specifications=specifications
            ))
    # Keep quantities, custom descriptions, other materials and orders intact.


def upgrade():
    update_sticker(OLD_NAME, NEW_NAME, OLD_DESCRIPTION, NEW_DESCRIPTION)


def downgrade():
    update_sticker(NEW_NAME, OLD_NAME, NEW_DESCRIPTION, OLD_DESCRIPTION)
