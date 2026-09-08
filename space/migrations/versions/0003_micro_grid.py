"""Start fresh Space geometry with the 8x8x8 grid (no legacy conversion).

Deploy with all Space API and hosting writers stopped and a verified backup.
Account/profile, quota and billing/outbox records are deliberately retained.
"""
from alembic import op
import sqlalchemy as sa

revision = 'space_0003'
down_revision = 'space_0002'
branch_labels = None
depends_on = None

GEOMETRY_TABLES = (
    'space_market_resource_likes', 'space_market_resources',
    'space_entity_operations', 'space_world_entities', 'space_hosting_workers',
    'space_terrain_mutation_batches', 'space_surface_zone_snapshots',
    'chunk_snapshots', 'player_snapshots',
)


def upgrade():
    # Never discard prepaid simulation time or an active funding relationship.
    # Finish/reconcile it before this breaking release; do not rewrite a ledger.
    funded = op.get_bind().execute(sa.text(
        'SELECT count(*) FROM space_world_entities WHERE hosting_remaining_ms > 0 '
        'OR hosting_enabled OR hosting_authorization_id IS NOT NULL'
    )).scalar_one()
    if funded:
        raise RuntimeError('Settle funded Space entities before resetting the micro grid')

    for table in GEOMETRY_TABLES:
        op.execute(sa.table(table).delete())
    # Keep the world cursor monotonic so clients cannot mistake a reset for an
    # unchanged terrain revision. World seeds and public identities survive.
    op.execute('UPDATE world_event_streams SET last_event_id = last_event_id + 1')
    with op.batch_alter_table('space_market_resources') as batch:
        batch.drop_constraint('ck_space_market_resource_schema_version', type_='check')
        batch.alter_column('schema_version', existing_type=sa.SmallInteger(), server_default='6')
        batch.create_check_constraint('ck_space_market_resource_schema_version', 'schema_version = 6')
    with op.batch_alter_table('space_world_entities') as batch:
        batch.alter_column('schema_version', existing_type=sa.SmallInteger(), server_default='6')


def downgrade():
    raise RuntimeError('The old micro-grid data can only be restored from its pre-release backup')
