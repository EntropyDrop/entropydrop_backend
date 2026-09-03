"""replace world entity tokens with account Space API keys

Revision ID: a6f2c8d914e3
Revises: f4a7c91d2e60
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6f2c8d914e3"
down_revision: Union[str, None] = "f4a7c91d2e60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "space_api_keys",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("key_prefix", sa.String(length=40), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_space_api_key_token_hash"),
    )
    op.create_index(
        "ix_space_api_keys_owner",
        "space_api_keys",
        ["user_id", "created_at"],
    )

    op.drop_index("ix_space_entity_create_tokens_owner", table_name="space_entity_create_tokens")
    op.drop_table("space_entity_create_tokens")
    op.drop_constraint(
        "ck_space_world_entity_source_kind",
        "space_world_entities",
        type_="check",
    )
    op.drop_column("space_world_entities", "source_resource_id")
    op.drop_column("space_world_entities", "source_kind")


def downgrade() -> None:
    op.add_column(
        "space_world_entities",
        sa.Column("source_kind", sa.String(length=16), server_default="browser", nullable=False),
    )
    op.add_column(
        "space_world_entities",
        sa.Column("source_resource_id", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_space_world_entity_source_kind",
        "space_world_entities",
        "source_kind IN ('market', 'browser')",
    )

    op.create_table(
        "space_entity_create_tokens",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("world_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_space_entity_create_token_hash"),
    )
    op.create_index(
        "ix_space_entity_create_tokens_owner",
        "space_entity_create_tokens",
        ["world_id", "user_id", "created_at"],
    )

    op.drop_index("ix_space_api_keys_owner", table_name="space_api_keys")
    op.drop_table("space_api_keys")
