"""Store catalog kit specifications and purchase-time order-item snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "c7a31d902ef4"
down_revision = "b8d62a4f901c"
branch_labels = None
depends_on = None

CUTE_KIT_SPECIFICATIONS = {
    "product_name": "CUTE-7cm DIY kit",
    "dimensions": "Approx. 7 × 4.5 × 2.8 cm",
    "materials": [
        {"name": "Pre-cut sticker sheet", "quantity": 1, "description": None},
        {"name": "White 3D printed body parts", "quantity": 6,
         "description": "One each: head, torso, left arm, right arm, left leg and right leg."},
        {"name": "Long joint", "quantity": 1, "description": None},
        {"name": "Short joint", "quantity": 2, "description": None},
        {"name": "PTFE tube", "quantity": 2, "description": "Outer diameter 4 mm × length 12 mm. Non-printed."},
    ],
    "assembly_note": "Assembly and sticker application are not included. Assemble the parts and apply the stickers yourself.",
}


def upgrade():
    op.add_column("model_sales_limits", sa.Column("kit_specifications", sa.JSON(), nullable=True))
    op.add_column("order_items", sa.Column("kit_specifications_snapshot", sa.JSON(), nullable=True))
    products = sa.table("model_sales_limits", sa.column("model_type", sa.String(100)),
                        sa.column("order_type", sa.String(20)), sa.column("kit_specifications", sa.JSON()))
    op.get_bind().execute(products.update().where(
        products.c.model_type == "Cute DIY Kit", products.c.order_type == "print"
    ).values(kit_specifications=CUTE_KIT_SPECIFICATIONS))
    # Historical orders stay without a snapshot; their current catalog fallback
    # is explicitly labelled by the API and UI, never backdated as purchase terms.


def downgrade():
    op.drop_column("order_items", "kit_specifications_snapshot")
    op.drop_column("model_sales_limits", "kit_specifications")
