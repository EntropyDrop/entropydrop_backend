"""Store optional destination URLs on campaign notifications."""
from alembic import op
import sqlalchemy as sa

revision = "d6a28f7c913e"
down_revision = "c8f43a601d92"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("forum_notifications", sa.Column("action_url", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("forum_notifications", "action_url")
