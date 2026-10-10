"""Human review, durable refunds and figure order mailbox notifications."""
from alembic import op
import sqlalchemy as sa

revision = "a4e19c7d8032"
down_revision = "f3c72a6b910e"
branch_labels = None
depends_on = None


def upgrade():
    for name, type_ in [
        ("figure_review_status", sa.String(20)), ("figure_review_reason", sa.Text()),
        ("figure_reviewed_by", sa.String(16)), ("figure_reviewed_at", sa.DateTime(timezone=True)),
        ("paypal_capture_id", sa.String(100)), ("paypal_refund_id", sa.String(100)),
        ("refund_status", sa.String(30)), ("refund_error", sa.Text()),
        ("refund_requested_at", sa.DateTime(timezone=True)), ("refund_checked_at", sa.DateTime(timezone=True)),
        ("tracking_number", sa.String(200)),
    ]:
        op.add_column("orders", sa.Column(name, type_, nullable=True))
    op.add_column("orders", sa.Column("inventory_consumed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_orders_figure_review_status", "orders", ["figure_review_status"])
    op.create_index("uq_orders_paypal_refund_id", "orders", ["paypal_refund_id"], unique=True)
    op.add_column("order_items", sa.Column("source_snapshot", sa.JSON(), nullable=True))
    op.add_column("forum_notifications", sa.Column("order_id", sa.String(16), nullable=True))
    op.add_column("forum_notifications", sa.Column("message", sa.Text(), nullable=True))
    op.add_column("forum_notifications", sa.Column("event_key", sa.String(100), nullable=True))
    op.create_index("ix_forum_notifications_order_id", "forum_notifications", ["order_id"])
    op.create_index("uq_forum_notifications_event_key", "forum_notifications", ["event_key"], unique=True)
    # Existing fulfilled/in-production orders retain their workflow. Unstarted
    # paid orders require review. Unknown historical stock consumption is not inferred.
    op.execute("UPDATE orders SET figure_review_status = 'approved' WHERE order_type = 'print' AND (status IN ('shipping', 'completed') OR (status = 'paid' AND goods_status = 'printing'))")
    op.execute("UPDATE orders SET figure_review_status = 'pending', goods_status = 'awaiting_review' WHERE order_type = 'print' AND status = 'paid' AND figure_review_status IS NULL")


def downgrade():
    op.drop_index("uq_forum_notifications_event_key", table_name="forum_notifications")
    op.drop_index("ix_forum_notifications_order_id", table_name="forum_notifications")
    for name in ["event_key", "message", "order_id"]:
        op.drop_column("forum_notifications", name)
    op.drop_column("order_items", "source_snapshot")
    op.drop_index("uq_orders_paypal_refund_id", table_name="orders")
    op.drop_index("ix_orders_figure_review_status", table_name="orders")
    for name in ["figure_review_status", "figure_review_reason", "figure_reviewed_by", "figure_reviewed_at", "paypal_capture_id", "paypal_refund_id", "refund_status", "refund_error", "refund_requested_at", "refund_checked_at", "inventory_consumed", "tracking_number"]:
        op.drop_column("orders", name)
