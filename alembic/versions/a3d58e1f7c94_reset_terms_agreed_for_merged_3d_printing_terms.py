"""reset terms_agreed after merging the 3D printing terms into the Terms of Service

Revision ID: a3d58e1f7c94
Revises: e1b75a09c632
Create Date: 2026-11-10
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a3d58e1f7c94"
down_revision: Union[str, None] = "e1b75a09c632"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The standalone 3D printing download terms are now Section 18 of the Terms of
    # Service and the separate download confirmation was removed, so every user must
    # accept the updated Terms of Service again. users.figure_print_terms_* are left
    # untouched here; revision c5e8a17d3b92 drops them.
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
