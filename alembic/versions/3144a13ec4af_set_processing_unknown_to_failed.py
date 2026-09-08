"""set_processing_unknown_to_failed

Revision ID: 3144a13ec4af
Revises: 9b31c72f5d8a
Create Date: 2026-08-06 18:49:57.214265

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3144a13ec4af'
down_revision: Union[str, None] = '9b31c72f5d8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    logs = sa.table("generation_logs", sa.column("status"), sa.column("provider_submission_state"))
    op.get_bind().execute(logs.update().where(
        logs.c.status == "processing", logs.c.provider_submission_state == "unknown"
    ).values(status="failed"))


def downgrade() -> None:
    pass
