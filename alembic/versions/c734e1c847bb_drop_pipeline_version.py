"""drop_pipeline_version

Revision ID: c734e1c847bb
Revises: 3144a13ec4af
Create Date: 2026-08-06 19:03:35.770641

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c734e1c847bb'
down_revision: Union[str, None] = '3144a13ec4af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("generation_logs", "pipeline_version")


def downgrade() -> None:
    op.add_column("generation_logs", sa.Column("pipeline_version", sa.String(length=100), nullable=True))
