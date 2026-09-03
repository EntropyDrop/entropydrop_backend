"""add browser-executed Space world entities

Revision ID: f4a7c91d2e60
Revises: e19b4c7d2a60
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a7c91d2e60"
down_revision: Union[str, None] = "e19b4c7d2a60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
    op.create_table(
        "space_world_entities",
        sa.Column("world_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=16), nullable=False),
        sa.Column("source_kind", sa.String(length=16), server_default="market", nullable=False),
        sa.Column("source_resource_id", sa.String(length=16), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), server_default="3", nullable=False),
        sa.Column("content_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("definition", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.LargeBinary(), nullable=True),
        sa.Column("snapshot_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("snapshot_size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("position_x_cm", sa.Integer(), nullable=False),
        sa.Column("position_y_cm", sa.Integer(), nullable=False),
        sa.Column("position_z_cm", sa.Integer(), nullable=False),
        sa.Column("yaw_quarter_turns", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("desired_run_state", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("create_operation_id", sa.Uuid(), nullable=False),
        sa.Column("create_request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("last_control_operation_id", sa.Uuid(), nullable=True),
        sa.Column("last_checkpoint_operation_id", sa.Uuid(), nullable=True),
        sa.Column("last_checkpoint_request_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("execution_instance_id", sa.Uuid(), nullable=True),
        sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "desired_run_state IN ('running', 'stopped')",
            name="ck_space_world_entity_run_state",
        ),
        sa.CheckConstraint(
            "source_kind IN ('market', 'browser')",
            name="ck_space_world_entity_source_kind",
        ),
        sa.CheckConstraint(
            "yaw_quarter_turns >= 0 AND yaw_quarter_turns <= 3",
            name="ck_space_world_entity_yaw",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_space_world_entity_revision"),
        sa.CheckConstraint("execution_epoch >= 0", name="ck_space_world_entity_execution_epoch"),
        sa.CheckConstraint("size_bytes > 0", name="ck_space_world_entity_size"),
        sa.CheckConstraint(
            "snapshot_size_bytes >= 0",
            name="ck_space_world_entity_snapshot_size",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("world_id", "id"),
        sa.UniqueConstraint(
            "world_id", "owner_user_id", "create_operation_id",
            name="uq_space_world_entity_create_operation",
        ),
    )
    op.create_index(
        "ix_space_world_entities_world_position",
        "space_world_entities",
        ["world_id", "position_x_cm", "position_z_cm"],
    )
    op.create_index(
        "ix_space_world_entities_owner",
        "space_world_entities",
        ["world_id", "owner_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_space_world_entities_owner", table_name="space_world_entities")
    op.drop_index("ix_space_world_entities_world_position", table_name="space_world_entities")
    op.drop_table("space_world_entities")
    op.drop_index("ix_space_entity_create_tokens_owner", table_name="space_entity_create_tokens")
    op.drop_table("space_entity_create_tokens")
