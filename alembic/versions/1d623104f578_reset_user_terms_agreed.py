"""reset user terms agreed status

Revision ID: 1d623104f578
Revises: c3f7a92d10be
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1d623104f578"
down_revision: Union[str, None] = "c3f7a92d10be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Reset terms_agreed to FALSE for all users after significant Terms of Service updates
    op.execute(
        """
        UPDATE users
        SET terms_agreed = FALSE
        WHERE terms_agreed IS TRUE
        """
    )


def downgrade() -> None:
    # Reset of user consent is irreversible
    pass
