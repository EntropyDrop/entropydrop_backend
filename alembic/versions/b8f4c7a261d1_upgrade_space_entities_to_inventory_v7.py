"""Upgrade space world entities to inventory v7 wire format.

Revision ID: b8f4c7a261d1
Revises: a8e6c4d20918
"""
from alembic import op
import sqlalchemy as sa
from space.inventory_v6 import convert_v6_inventory_resource

revision = "b8f4c7a261d1"
down_revision = "a8e6c4d20918"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    entities = sa.table(
        "space_world_entities",
        sa.column("world_id", sa.Uuid(as_uuid=False)),
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("schema_version", sa.SmallInteger()),
        sa.column("definition", sa.LargeBinary()),
        sa.column("content_digest", sa.LargeBinary()),
        sa.column("size_bytes", sa.Integer()),
        sa.column("revision", sa.BigInteger()),
    )
    rows = bind.execute(
        sa.select(
            entities.c.world_id,
            entities.c.id,
            entities.c.schema_version,
            entities.c.definition,
            entities.c.revision,
        )
    ).fetchall()
    for world_id, entity_id, schema_version, definition, entity_revision in rows:
        version = int(schema_version)
        if version == 7:
            continue
        if version != 6:
            raise RuntimeError(
                f"Entity {entity_id} has unsupported inventory schema version {version}"
            )
        kind, _portable, canonical, digest = convert_v6_inventory_resource(bytes(definition))
        if kind != "entity":
            raise RuntimeError(f"Entity {entity_id} is not an entity inventory resource")
        bind.execute(
            entities.update()
            .where(entities.c.world_id == world_id, entities.c.id == entity_id)
            .values(
                definition=canonical,
                content_digest=digest,
                size_bytes=len(canonical),
                revision=int(entity_revision) + 1,
                schema_version=7,
            )
        )
    with op.batch_alter_table("space_world_entities") as batch:
        batch.alter_column("schema_version", existing_type=sa.SmallInteger(), server_default="7")


def downgrade():
    raise RuntimeError(
        "Migrated v7 entity definitions cannot be converted back; restore from backup"
    )
