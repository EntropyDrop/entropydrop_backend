"""Bind creators to accounts and persist commission withdrawal accounting."""
from alembic import op
import sqlalchemy as sa

revision = "a6d3e9f127c8"
down_revision = "f4a92b6d013e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("is_creator", sa.Boolean(), nullable=False, server_default=sa.false()))
    for table in ("creator_codes", "orders"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("creator_user_id", sa.String(16), nullable=True))
            batch.create_foreign_key(f"fk_{table}_creator_user", "users", ["creator_user_id"], ["id"])
            batch.create_index(f"ix_{table}_creator_user_id", ["creator_user_id"])
    op.add_column("orders", sa.Column("creator_commission_cents", sa.Integer(), nullable=False, server_default="0"))
    op.execute("UPDATE orders SET creator_commission_cents = CAST(ROUND(CAST(creator_commission AS NUMERIC) * 100) AS INTEGER)")
    op.create_table("creator_payouts",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("creator_user_id", sa.String(16), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("paypal_email", sa.String(127), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reviewed_by", sa.String(16), sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_note", sa.String(500)),
        sa.Column("paypal_batch_id", sa.String(100), unique=True),
        sa.Column("paypal_item_id", sa.String(100), unique=True),
        sa.Column("paypal_transaction_id", sa.String(100)),
        sa.Column("paypal_status", sa.String(32)),
        sa.Column("first_sent_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("processing_until", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("creator_user_id", "request_key", name="uq_creator_payout_request"),
        sa.CheckConstraint("amount_cents > 0", name="ck_creator_payout_amount"),
        sa.CheckConstraint("currency = 'USD'", name="ck_creator_payout_currency"))
    for column in ("creator_user_id", "status", "next_check_at"):
        op.create_index(f"ix_creator_payouts_{column}", "creator_payouts", [column])
    op.create_table("creator_payout_events",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("payout_id", sa.String(16), sa.ForeignKey("creator_payouts.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("event", sa.String(100), nullable=False),
        sa.Column("actor_user_id", sa.String(16), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_creator_payout_events_payout_id", "creator_payout_events", ["payout_id"])


def downgrade():
    op.drop_table("creator_payout_events")
    op.drop_table("creator_payouts")
    op.drop_column("orders", "creator_commission_cents")
    for table in ("orders", "creator_codes"):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_creator_user_id")
            batch.drop_constraint(f"fk_{table}_creator_user", type_="foreignkey")
            batch.drop_column("creator_user_id")
    op.drop_column("users", "is_creator")
