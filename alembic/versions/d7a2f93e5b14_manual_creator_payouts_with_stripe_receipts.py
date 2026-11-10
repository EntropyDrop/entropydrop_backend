"""creator withdrawals become manual: Stripe transfer receipt, fee deducted from the amount

Revision ID: d7a2f93e5b14
Revises: c5e8a17d3b92
Create Date: 2026-11-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d7a2f93e5b14"
down_revision: Union[str, None] = "c5e8a17d3b92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("creator_payouts") as batch:
        batch.alter_column("paypal_email", new_column_name="payout_email", existing_type=sa.String(127), existing_nullable=False)
        batch.add_column(sa.Column("fee_cents", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("transfer_reference", sa.String(100), nullable=True))
        batch.add_column(sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_creator_payout_transfer_reference", ["transfer_reference"])
        batch.create_check_constraint("ck_creator_payout_fee", "fee_cents IS NULL OR (fee_cents >= 0 AND fee_cents < amount_cents)")
    # PayPal-era payouts that were already paid carried no fee for the creator.
    op.execute(
        """
        UPDATE creator_payouts
        SET fee_cents = 0, transfer_reference = paypal_transaction_id, paid_at = updated_at
        WHERE status = 'paid'
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("creator_payouts") as batch:
        batch.drop_constraint("ck_creator_payout_fee", type_="check")
        batch.drop_constraint("uq_creator_payout_transfer_reference", type_="unique")
        batch.drop_column("paid_at")
        batch.drop_column("transfer_reference")
        batch.drop_column("fee_cents")
        batch.alter_column("payout_email", new_column_name="paypal_email", existing_type=sa.String(127), existing_nullable=False)
