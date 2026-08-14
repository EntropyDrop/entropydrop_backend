"""add image-to-skin edited result

Revision ID: b4c7e2f91a6d
Revises: c734e1c847bb
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4c7e2f91a6d"
down_revision: Union[str, None] = "c734e1c847bb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "generation_logs",
        sa.Column(
            "image_to_skin_edited_result",
            sa.String(length=500),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE generation_logs
            SET image_to_skin_edited_result = edited_result,
                edited_result = NULL
            WHERE model_version LIKE 'SKING_DDJ%'
              AND edited_result LIKE 'real_to_render_intermediate/%'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE generation_logs
            SET edited_result = COALESCE(
                edited_result,
                image_to_skin_edited_result
            )
            WHERE image_to_skin_edited_result IS NOT NULL
            """
        )
    )
    op.drop_column("generation_logs", "image_to_skin_edited_result")
