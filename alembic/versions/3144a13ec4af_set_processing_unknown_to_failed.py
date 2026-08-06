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
    from sqlalchemy.orm import Session
    import models

    bind = op.get_bind()
    db = Session(bind=bind)

    try:
        updated = db.query(models.GenerationLog).filter(
            models.GenerationLog.status == "processing",
            models.GenerationLog.provider_submission_state == "unknown"
        ).update({"status": "failed"}, synchronize_session=False)
        db.commit()
        print(f"[*] Set {updated} processing generation log(s) with unknown provider_submission_state to failed.")
    except Exception as e:
        db.rollback()
        raise e


def downgrade() -> None:
    pass
