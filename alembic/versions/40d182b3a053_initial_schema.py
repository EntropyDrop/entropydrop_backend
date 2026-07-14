"""initial_schema

Revision ID: 40d182b3a053
Revises: 
Create Date: 2026-05-27 17:15:13.264775

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '40d182b3a053'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure postgres extension for pg_trgm is enabled
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # Tables are auto-created by ORM on application startup.
    # This migration acts as the base revision stamp.
    pass


def downgrade() -> None:
    pass
