"""Record the latest 3D printing download service acceptance on each user."""
from alembic import op
import sqlalchemy as sa

revision = "e6b93f1a0c25"
down_revision = "d9a7e3b10462"
branch_labels = None
depends_on = None


def upgrade():
    # Existing users must actively accept; never derive this from terms_agreed.
    op.add_column("users", sa.Column("figure_print_terms_version", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("figure_print_terms_accepted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("users", "figure_print_terms_accepted_at")
    op.drop_column("users", "figure_print_terms_version")
