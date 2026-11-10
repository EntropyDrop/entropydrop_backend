"""drop the per-user records of the former standalone 3D printing terms

Revision ID: c5e8a17d3b92
Revises: a3d58e1f7c94
Create Date: 2026-11-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c5e8a17d3b92"
down_revision: Union[str, None] = "a3d58e1f7c94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The standalone terms are now Section 18 of the Terms of Service and nothing
    # writes or reads these records any more. They are deleted, not archived.
    op.drop_column("users", "figure_print_terms_accepted_at")
    op.drop_column("users", "figure_print_terms_version")


def downgrade() -> None:
    # Restores the empty columns only; the dropped records cannot be recovered.
    op.add_column("users", sa.Column("figure_print_terms_version", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("figure_print_terms_accepted_at", sa.DateTime(timezone=True), nullable=True))
