"""add minecraft_skin_model to users

Revision ID: e3d489b1c7a2
Revises: b4c7e2f91a6d
Create Date: 2026-08-26 15:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3d489b1c7a2'
down_revision: Union[str, None] = 'b4c7e2f91a6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('minecraft_skin_model', sa.String(length=20), server_default='strong', nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'minecraft_skin_model')
