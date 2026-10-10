"""Replace the active Cute kit with cute10; preserve purchase-time snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "a9c64e280fb1"
down_revision = "e4b7c9a21035"
branch_labels = None
depends_on = None

CUTE_KIT_SPECIFICATIONS = {
    "product_name": "CUTE-10cm DIY kit",
    "dimensions": "Approx. 10 × 6.7 × 4.1 cm",
    "materials": [
        {"name": "Pre-cut sticker sheet", "quantity": 1, "description": None},
        {"name": "White 3D printed body parts", "quantity": 6,
         "description": "One each: head, torso, left arm, right arm, left leg and right leg."},
        {"name": "Long joint", "quantity": 1, "description": None},
        {"name": "Short joint", "quantity": 4,
         "description": "Two shoulder joints and two hip joints."},
    ],
    "assembly_note": "Assembly and sticker application are not included. Assemble the parts and apply the stickers yourself.",
}


def update_catalog(specifications):
    products = sa.table("model_sales_limits", sa.column("model_type", sa.String(100)),
                        sa.column("order_type", sa.String(20)), sa.column("kit_specifications", sa.JSON()))
    op.get_bind().execute(products.update().where(
        products.c.model_type == "Cute DIY Kit", products.c.order_type == "print"
    ).values(kit_specifications=specifications))
    # Price, stock, independent skin copies and all saved order-item data remain intact.


def upgrade():
    update_catalog(CUTE_KIT_SPECIFICATIONS)


def downgrade():
    previous = {
        **CUTE_KIT_SPECIFICATIONS,
        "product_name": "CUTE-7cm DIY kit",
        "dimensions": "Approx. 7 × 4.5 × 2.8 cm",
        "materials": [*CUTE_KIT_SPECIFICATIONS["materials"][:3],
            {"name": "Short joint", "quantity": 2, "description": None},
            {"name": "PTFE tube", "quantity": 2, "description": "Outer diameter 4 mm × length 12 mm. Non-printed."}],
    }
    update_catalog(previous)
