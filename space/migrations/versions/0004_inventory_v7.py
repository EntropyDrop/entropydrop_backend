"""Migrate stored inventory v6 entity definitions to the v7 wire format.

Inventory v7 adopts the authoritative realtime Voxel encoding: `is_micro` plus
`micro_x`/`micro_y`/`micro_z` and a varint `color_rgb`, replacing the packed
`micro_index` and `fixed32 color` of v6. This migration rewrites every
`space_world_entities.definition` from v6 to canonical v7, recomputes the name-free
content digest and byte size, and increments `revision` so clients refetch.

Terrain chunks, far-surface snapshots, player positions, entity operation receipts
and hosting-worker leases use their own formats and are left untouched. Market
resources stay in object storage: their rows are preserved with `schema_version`
still 6 and the check constraint accepts both 6 and 7, but the API rejects legacy
downloads until the resource is re-published. Account/profile, quota and
billing/outbox records are unchanged.

Deploy with all Space API and hosting writers stopped and a verified backup.
"""
from alembic import op
import sqlalchemy as sa

from space.inventory_v6 import convert_v6_inventory_resource

revision = 'space_0004'
down_revision = 'space_0003'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    entities = sa.table(
        'space_world_entities',
        sa.column('world_id', sa.Uuid(as_uuid=False)),
        sa.column('id', sa.Uuid(as_uuid=False)),
        sa.column('schema_version', sa.SmallInteger()),
        sa.column('definition', sa.LargeBinary()),
        sa.column('content_digest', sa.LargeBinary()),
        sa.column('size_bytes', sa.Integer()),
        sa.column('revision', sa.BigInteger()),
    )
    rows = bind.execute(sa.select(
        entities.c.world_id,
        entities.c.id,
        entities.c.schema_version,
        entities.c.definition,
        entities.c.revision,
    )).fetchall()
    for world_id, entity_id, schema_version, definition, revision in rows:
        version = int(schema_version)
        if version == 7:
            continue
        if version != 6:
            raise RuntimeError(
                f'Entity {entity_id} has unsupported inventory schema version {version}'
            )
        kind, _portable, canonical, digest = convert_v6_inventory_resource(bytes(definition))
        if kind != 'entity':
            raise RuntimeError(f'Entity {entity_id} is not an entity inventory resource')
        bind.execute(
            entities.update()
            .where(entities.c.world_id == world_id, entities.c.id == entity_id)
            .values(
                definition=canonical,
                content_digest=digest,
                size_bytes=len(canonical),
                revision=int(revision) + 1,
                schema_version=7,
            )
        )

    with op.batch_alter_table('space_market_resources') as batch:
        batch.drop_constraint('ck_space_market_resource_schema_version', type_='check')
        batch.alter_column('schema_version', existing_type=sa.SmallInteger(), server_default='7')
        batch.create_check_constraint(
            'ck_space_market_resource_schema_version', 'schema_version IN (6, 7)'
        )
    with op.batch_alter_table('space_world_entities') as batch:
        batch.alter_column('schema_version', existing_type=sa.SmallInteger(), server_default='7')


def downgrade():
    raise RuntimeError(
        'Migrated v7 entity definitions cannot be converted back; restore from the pre-release backup'
    )
