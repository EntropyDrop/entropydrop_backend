"""Persist subscription grants, stock holds, addresses and skin withdrawals.

Revision ID: a8e6c4d20918
Revises: e3a97d50c812
"""
from alembic import op
import sqlalchemy as sa

revision = "a8e6c4d20918"
down_revision = "e3a97d50c812"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("credit_logs", sa.Column("idempotency_key", sa.String(180), nullable=True))
    op.create_index("uq_credit_logs_idempotency_key", "credit_logs", ["idempotency_key"], unique=True)
    op.add_column("orders", sa.Column("address_snapshot", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("orders", sa.Column("inventory_reserved", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("inventory_reserved_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("capture_started", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("generation_logs", sa.Column("withdrawal", sa.JSON(none_as_null=True), nullable=True))

    connection = op.get_bind()
    addresses = sa.table("shipping_addresses", *[sa.column(name) for name in (
        "id", "user_id", "country", "phone", "zip_code", "state", "city", "detail_address", "is_default"
    )])
    orders = sa.table("orders", sa.column("address_id"), sa.column("user_id"), sa.column("address_snapshot", sa.JSON()))
    for address in connection.execute(sa.select(addresses)).mappings():
        snapshot = dict(address)
        snapshot["is_default"] = bool(snapshot["is_default"])
        connection.execute(orders.update().where(
            orders.c.address_id == address["id"], orders.c.user_id == address["user_id"]
        ).values(address_snapshot=snapshot))


def downgrade():
    op.drop_column("generation_logs", "withdrawal")
    for name in ("capture_started", "inventory_reserved_until", "inventory_reserved", "address_snapshot"):
        op.drop_column("orders", name)
    op.drop_index("uq_credit_logs_idempotency_key", table_name="credit_logs")
    op.drop_column("credit_logs", "idempotency_key")
