"""Add sticker application tools to new CUTE-10cm kit purchases."""
from copy import deepcopy

from alembic import op
import sqlalchemy as sa

revision = "e8a42f617c03"
down_revision = "d7f4c8302a91"
branch_labels = None
depends_on = None

TOOLS = [
    {"name": "Tweezers", "quantity": 1, "description": "For sticker application."},
    {"name": "Flat-head screwdriver", "quantity": 1, "description": "For sticker application."},
]


def update_tools(include):
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
        materials = specifications.setdefault("materials", [])
        if include:
            names = {material.get("name") for material in materials}
            materials.extend(deepcopy(tool) for tool in TOOLS if tool["name"] not in names)
        else:
            # Do not remove tools whose configuration has since been customized.
            specifications["materials"] = [material for material in materials if material not in TOOLS]
        connection.execute(products.update().where(products.c.id == kit["id"]).values(
            kit_specifications=specifications
        ))
    # Existing order snapshots, prices, stock and other materials remain intact.


def upgrade():
    update_tools(True)


def downgrade():
    update_tools(False)
