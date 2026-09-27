"""Add expiring browser consent grants for external Space agents."""
from alembic import op
import sqlalchemy as sa

revision = "d9a7e3b10462"
down_revision = "c82e7a4d901b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "space_agent_authorizations",
        sa.Column("device_hash", sa.LargeBinary(32), primary_key=True),
        sa.Column("user_code_hash", sa.LargeBinary(32), nullable=False, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("user_id", sa.String(16), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("api_key_id", sa.String(16)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("poll_interval", sa.Integer(), nullable=False),
    )
    op.create_index("ix_space_agent_authorizations_expires_at", "space_agent_authorizations", ["expires_at"])


def downgrade():
    op.drop_table("space_agent_authorizations")
