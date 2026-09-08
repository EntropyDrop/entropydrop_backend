"""Exercise the breaking migration against the real historical schema.

Runs in SQLite by default. SPACE_MIGRATION_TEST_DATABASE_URL also tests
PostgreSQL inside a temporary schema, never altering existing application data.
"""
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import unittest
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]


def migration(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'space/migrations/versions' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MicroGridMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine(os.getenv('SPACE_MIGRATION_TEST_DATABASE_URL', 'sqlite:///:memory:'))
        self.addCleanup(self.engine.dispose)
        self.connection = self.engine.connect()
        self.addCleanup(self.connection.close)
        self.transaction = self.connection.begin()
        self.addCleanup(self.transaction.rollback)
        if self.engine.dialect.name == 'postgresql':
            name = 'micro_grid_test_' + uuid.uuid4().hex
            self.connection.execute(sa.text('CREATE SCHEMA ' + name))
            self.connection.execute(sa.text('SET LOCAL search_path TO ' + name))
        else:
            self.connection.execute(sa.text('PRAGMA foreign_keys=ON'))
        self.operations = Operations.context(MigrationContext.configure(self.connection))
        self.operations.__enter__()
        self.addCleanup(self.operations.__exit__, None, None, None)
        migration('0001_local_space').upgrade()
        migration('0002_entity_operations').upgrade()
        self.reset = migration('0003_micro_grid')
        self.metadata = sa.MetaData()
        self.metadata.reflect(self.connection)
        self.world = uuid.uuid4()
        self.entity = uuid.uuid4()
        self.user = 'migration_user'
        self.insert('space_accounts', id=self.user)
        self.insert('worlds', id=self.world, owner_user_id=self.user, status=1)
        self.insert('world_player_profiles', world_id=self.world, user_id=self.user)
        self.insert('world_event_streams', world_id=self.world, last_event_id=41)
        self.insert('space_hosting_grants', world_id=self.world, entity_id=self.entity,
                    state='consumed', settlement='capture', settled=True)
        self.insert('space_hosting_authorizations', world_id=self.world, entity_id=self.entity,
                    revoked=True, settled=True)
        self.insert('space_hosting_operations', world_id=self.world)
        self.insert('space_usage_buckets')
        self.insert('space_world_entities', world_id=self.world, id=self.entity, owner_user_id=self.user)
        self.insert('space_market_resources', id='old_resource', publisher_user_id=self.user,
                    kind='blockset', license='AGPL-3.0-only')
        self.insert('space_market_resource_likes', resource_id='old_resource', user_id=self.user)
        for name in self.reset.GEOMETRY_TABLES:
            if name in {'space_world_entities', 'space_market_resources', 'space_market_resource_likes'}:
                continue
            values = {'world_id': self.world}
            if name == 'player_snapshots':
                values['user_id'] = self.user
            self.insert(name, **values)

    def insert(self, name, **values):
        table = self.metadata.tables[name]
        for column in table.columns:
            if column.name in values or column.nullable or column.server_default is not None:
                continue
            kind = column.type
            if isinstance(kind, sa.Boolean): value = False
            elif isinstance(kind, sa.Integer): value = 1
            elif isinstance(kind, sa.DateTime): value = datetime.now(timezone.utc)
            elif isinstance(kind, sa.Uuid): value = uuid.uuid4()
            elif isinstance(kind, sa.LargeBinary): value = b'x' * 32
            elif isinstance(kind, sa.JSON): value = {}
            else: value = 'fixture'
            values[column.name] = value
        values = {key: (value.hex if isinstance(value, uuid.UUID) and not isinstance(table.c[key].type, sa.Uuid) else value)
                  for key, value in values.items()}
        self.connection.execute(table.insert().values(**values))

    def count(self, table):
        return self.connection.execute(sa.select(sa.func.count()).select_from(self.metadata.tables[table])).scalar_one()

    def test_reset_preserves_identity_and_billing_then_accepts_only_v6_market(self):
        self.reset.upgrade()
        for name in self.reset.GEOMETRY_TABLES:
            self.assertEqual(self.count(name), 0, name)
        for name in ('space_accounts', 'worlds', 'world_player_profiles', 'space_hosting_grants',
                     'space_hosting_authorizations', 'space_hosting_operations', 'space_usage_buckets'):
            self.assertEqual(self.count(name), 1, name)
        self.assertEqual(self.connection.execute(sa.text('SELECT last_event_id FROM world_event_streams')).scalar_one(), 42)
        self.metadata.clear()
        self.metadata.reflect(self.connection)
        self.insert('space_market_resources', id='new_resource', kind='blockset', license='AGPL-3.0-only')
        self.assertEqual(self.connection.execute(sa.text('SELECT schema_version FROM space_market_resources')).scalar_one(), 6)
        self.insert('space_world_entities', world_id=self.world, id=uuid.uuid4(), owner_user_id=self.user)
        self.assertEqual(self.connection.execute(sa.text('SELECT schema_version FROM space_world_entities')).scalar_one(), 6)
        with self.assertRaises(sa.exc.IntegrityError):
            with self.connection.begin_nested():
                self.insert('space_market_resources', id='invalid_old', schema_version=5,
                            kind='blockset', license='AGPL-3.0-only', content_digest=b'y' * 32)

    def test_prepaid_time_blocks_reset_without_deleting_any_content(self):
        self.connection.execute(sa.text('UPDATE space_world_entities SET hosting_remaining_ms = 100'))
        with self.assertRaisesRegex(RuntimeError, 'Settle funded'):
            self.reset.upgrade()
        for name in self.reset.GEOMETRY_TABLES:
            self.assertEqual(self.count(name), 1, name)
        self.assertEqual(self.connection.execute(sa.text('SELECT schema_version FROM space_market_resources')).scalar_one(), 5)


if __name__ == '__main__':
    unittest.main()
