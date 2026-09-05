"""Add fenced entity hosting at one credit per runtime hour."""
from alembic import op
import sqlalchemy as sa

revision = "aa7e52c91604"
down_revision = "f9a4c2d7e610"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("execution_mode", sa.String(16), nullable=False, server_default="browser"),
        sa.Column("hosting_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("hosting_remaining_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("hosting_budget_remaining", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hosting_billed_hours", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hosting_anchor", sa.JSON(), nullable=True),
        sa.Column("hosting_reason", sa.String(80), nullable=True),
        sa.Column("hosting_error", sa.String(500), nullable=True),
        sa.Column("hosting_last_tick_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("space_world_entities", column)
    for name, expression in (
        ("mode", "execution_mode IN ('browser', 'hosted')"),
        ("time", "hosting_remaining_ms BETWEEN 0 AND 3600000"),
        ("budget", "hosting_budget_remaining BETWEEN 0 AND 168"),
        ("billed", "hosting_billed_hours >= 0"),
        ("enabled", "NOT hosting_enabled OR (execution_mode = 'hosted' AND desired_run_state = 'running')"),
    ):
        op.create_check_constraint("ck_space_hosting_" + name, "space_world_entities", expression)
    op.create_table("space_hosting_operations",
        sa.Column("world_id", sa.Uuid(), sa.ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("request_digest", sa.LargeBinary(32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False))
    op.create_table("space_hosting_workers",
        sa.Column("world_id", sa.Uuid(), sa.ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("space_hosting_workers")
    op.drop_table("space_hosting_operations")
    for name in ("mode", "time", "budget", "billed", "enabled"):
        op.drop_constraint("ck_space_hosting_" + name, "space_world_entities", type_="check")
    for name in ("execution_mode", "hosting_enabled", "hosting_remaining_ms", "hosting_budget_remaining",
                 "hosting_billed_hours", "hosting_anchor", "hosting_reason", "hosting_error", "hosting_last_tick_at"):
        op.drop_column("space_world_entities", name)
