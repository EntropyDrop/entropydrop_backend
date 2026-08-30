"""add generation log license snapshots

Revision ID: e7f4a91c2b6d
Revises: c8f1a2d3e4b5
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7f4a91c2b6d"
down_revision: Union[str, None] = "c8f1a2d3e4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "generation_logs",
        sa.Column(
            "license",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.add_column(
        "generation_logs",
        sa.Column("public_license", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "generation_logs",
        sa.Column(
            "license_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("'1'"),
        ),
    )
    op.add_column(
        "generation_logs",
        sa.Column("license_granted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_generation_logs_license"),
        "generation_logs",
        ["license"],
        unique=False,
    )

    # The paid entitlement is historical and permanent for generated work, so
    # is_pro is authoritative there. Legacy manual uploads remain unknown.
    op.execute(
        """
        UPDATE generation_logs
        SET license = CASE
                WHEN mode = 'human_upload' THEN 'unknown'
                WHEN is_pro = true THEN 'entropydrop-commercial-1.0'
                ELSE 'cc-by-nc-4.0'
            END,
            public_license = CASE
                WHEN is_public = true AND mode <> 'human_upload'
                    THEN 'cc-by-nc-4.0'
                ELSE NULL
            END,
            license_version = 1,
            license_granted_at = COALESCE(created_at, CURRENT_TIMESTAMP)
        """
    )

    # Conservatively preserve unknown provenance through legacy non-Pro edit
    # and regeneration chains rooted in an uploaded skin.
    op.execute(
        """
        WITH RECURSIVE unknown_lineage(id) AS (
            SELECT id
            FROM generation_logs
            WHERE mode = 'human_upload'
            UNION
            SELECT child.id
            FROM generation_logs AS child
            JOIN unknown_lineage AS parent ON child.parent = parent.id
            WHERE child.is_pro = false
        )
        UPDATE generation_logs
        SET license = 'unknown', public_license = NULL
        WHERE id IN (SELECT id FROM unknown_lineage)
        """
    )

    op.alter_column("generation_logs", "license_granted_at", nullable=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_generation_logs_license"), table_name="generation_logs")
    op.drop_column("generation_logs", "license_granted_at")
    op.drop_column("generation_logs", "license_version")
    op.drop_column("generation_logs", "public_license")
    op.drop_column("generation_logs", "license")
