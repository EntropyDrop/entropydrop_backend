"""Keep every PayPal contract and durable creation/cancellation intents."""
from alembic import op
import sqlalchemy as sa

revision = "e1b75a09c632"
down_revision = "d6a28f7c913e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("subscription_cancel_requested_at", sa.DateTime(timezone=True)))
    op.create_table(
        "paypal_subscriptions",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("user_id", sa.String(16), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("paypal_id", sa.String(100), unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("plan_id", sa.String(100)),
        sa.Column("request_id", sa.String(38), unique=True),
        sa.Column("return_url", sa.Text()),
        sa.Column("approval_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("create_requested_at", sa.DateTime(timezone=True)),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(100)),
    )
    op.create_index("ix_paypal_subscriptions_user_id", "paypal_subscriptions", ["user_id"])
    op.create_index("ix_paypal_subscriptions_next_check_at", "paypal_subscriptions", ["next_check_at"])
    # Existing display state is not proof that PayPal acknowledged cancellation.
    # Leave cancelled_at NULL until verified by PayPal or a signed terminal event.
    op.execute(sa.text("""
        INSERT INTO paypal_subscriptions (id, user_id, paypal_id, status, created_at)
        SELECT paypal_subscription_id, id, paypal_subscription_id,
               COALESCE(paypal_subscription_status, 'UNKNOWN'), COALESCE(created_at, CURRENT_TIMESTAMP)
        FROM users WHERE paypal_subscription_id IS NOT NULL
    """))


def downgrade():
    op.drop_table("paypal_subscriptions")
    op.drop_column("users", "subscription_cancel_requested_at")
