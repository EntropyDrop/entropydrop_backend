"""Separate generation queue priority from creation-time Pro/rights snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "c82e7a4d901b"
down_revision = "a8e6c4d20918"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("generation_logs", sa.Column(
        "pro_priority", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.execute("UPDATE generation_logs SET pro_priority = true WHERE is_pro = true")


def downgrade():
    op.drop_column("generation_logs", "pro_priority")
