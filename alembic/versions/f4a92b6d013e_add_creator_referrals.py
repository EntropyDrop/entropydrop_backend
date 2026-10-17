"""Creator referral receipts, discount codes and immutable order attribution."""
from alembic import op
import sqlalchemy as sa

revision = "f4a92b6d013e"
down_revision = "e2c49a710b35"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("creator_codes",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("creator_name", sa.String(100), nullable=False),
        sa.Column("discount_bps", sa.Integer(), nullable=False),
        sa.Column("commission_bps", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("discount_bps >= 0 AND discount_bps <= 9900", name="ck_creator_discount"),
        sa.CheckConstraint("commission_bps >= 0 AND commission_bps <= 10000", name="ck_creator_commission"))
    op.create_table("user_creator_referrals",
        sa.Column("user_id", sa.String(16), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("original_code", sa.String(32), nullable=False),
        sa.Column("original_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_code", sa.String(32), nullable=False),
        sa.Column("current_clicked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_user_creator_referrals_original_code", "user_creator_referrals", ["original_code"])
    op.add_column("orders", sa.Column("creator_code", sa.String(32)))
    op.add_column("orders", sa.Column("creator_snapshot", sa.JSON()))
    op.add_column("orders", sa.Column("discount_amount", sa.Float(), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("creator_commission", sa.Float(), nullable=False, server_default="0"))
    op.create_index("ix_orders_creator_code", "orders", ["creator_code"])


def downgrade():
    op.drop_index("ix_orders_creator_code", table_name="orders")
    for column in ["creator_commission", "discount_amount", "creator_snapshot", "creator_code"]:
        op.drop_column("orders", column)
    op.drop_table("user_creator_referrals")
    op.drop_table("creator_codes")
