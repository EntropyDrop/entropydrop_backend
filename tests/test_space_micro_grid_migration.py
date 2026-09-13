"""Exercise the breaking migrations against the real historical schema.

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

from space.contracts import inventory_v6_pb2
from space.inventory_v6 import convert_v6_inventory_resource

ROOT = Path(__file__).resolve().parents[1]


def migration(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'space/migrations/versions' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_v6_entity_definition() -> bytes:
    """A v6 entity with one legacy micro voxel (packed micro_index + fixed32 color)."""
    resource = inventory_v6_pb2.InventoryResource(schema_version=6)
    root = resource.entity.root
    root.id = 'root'
    root.name = 'Legacy Walker'
    root.body.SetInParent()
    root.body.type = inventory_v6_pb2.BODY_TYPE_DYNAMIC
    block = root.blocks.add()
    block.dx, block.dy, block.dz = 0, 0, 0
    block.micro_index = 1 + 2 + 8 * 1 + 64 * 0
    block.color = 0x12AB34
    return resource.SerializeToString(deterministic=True)


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
        self.wire_reset = migration('0004_inventory_v7')
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

    def test_v7_migration_converts_entities_and_preserves_unrelated_content(self):
        self.reset.upgrade()
        # Data written by the previous v6 release after the 0003 reset.
        definition = legacy_v6_entity_definition()
        self.metadata.clear()
        self.metadata.reflect(self.connection)
        self.insert('space_world_entities', world_id=self.world, id=uuid.uuid4(), owner_user_id=self.user,
                    schema_version=6, definition=definition, content_digest=b'z' * 32,
                    size_bytes=len(definition), revision=1)
        self.insert('chunk_snapshots', world_id=self.world)
        self.insert('player_snapshots', world_id=self.world, user_id=self.user)
        self.insert('space_hosting_workers', world_id=self.world)
        self.insert('space_market_resources', id='legacy_resource', publisher_user_id=self.user,
                    kind='blockset', license='AGPL-3.0-only', schema_version=6, content_digest=b'y' * 32)

        self.wire_reset.upgrade()

        _kind, _portable, canonical, digest = convert_v6_inventory_resource(definition)
        row = self.connection.execute(sa.text(
            'SELECT schema_version, definition, content_digest, size_bytes, revision '
            'FROM space_world_entities'
        )).one()
        self.assertEqual(row[0], 7)
        self.assertEqual(bytes(row[1]), canonical)
        self.assertEqual(bytes(row[2]), digest)
        self.assertEqual(row[3], len(canonical))
        self.assertEqual(row[4], 2, 'converted entities bump revision so clients refetch')

        # Unrelated Space content is preserved by the v7 migration.
        self.assertEqual(self.count('chunk_snapshots'), 1)
        self.assertEqual(self.count('player_snapshots'), 1)
        self.assertEqual(self.count('space_hosting_workers'), 1)
        self.assertEqual(self.connection.execute(
            sa.text('SELECT last_event_id FROM world_event_streams')).scalar_one(), 42)

        # Market rows survive, stay on schema 6, and the constraint accepts 6 and 7.
        self.assertEqual(self.count('space_market_resources'), 1)
        self.assertEqual(self.connection.execute(
            sa.text('SELECT schema_version FROM space_market_resources')).scalar_one(), 6)
        self.insert('space_market_resources', id='legacy_second', publisher_user_id=self.user,
                    kind='blockset', license='AGPL-3.0-only', schema_version=6, content_digest=b'w' * 32)
        self.insert('space_market_resources', id='current', publisher_user_id=self.user,
                    kind='blockset', license='AGPL-3.0-only', schema_version=7, content_digest=b'u' * 32)
        with self.assertRaises(sa.exc.IntegrityError):
            with self.connection.begin_nested():
                self.insert('space_market_resources', id='invalid_old', schema_version=5,
                            kind='blockset', license='AGPL-3.0-only', content_digest=b'v' * 32)

    def test_v7_migration_rejects_an_unknown_entity_schema(self):
        self.reset.upgrade()
        self.metadata.clear()
        self.metadata.reflect(self.connection)
        self.insert('space_world_entities', world_id=self.world, id=uuid.uuid4(), owner_user_id=self.user,
                    schema_version=5, definition=b'not-v7', content_digest=b'z' * 32,
                    size_bytes=7, revision=1)
        with self.assertRaisesRegex(RuntimeError, 'unsupported inventory schema'):
            self.wire_reset.upgrade()

    def test_prepaid_time_blocks_reset_without_deleting_any_content(self):
        self.connection.execute(sa.text('UPDATE space_world_entities SET hosting_remaining_ms = 100'))
        with self.assertRaisesRegex(RuntimeError, 'Settle funded'):
            self.reset.upgrade()
        for name in self.reset.GEOMETRY_TABLES:
            self.assertEqual(self.count(name), 1, name)
        self.assertEqual(self.connection.execute(sa.text('SELECT schema_version FROM space_market_resources')).scalar_one(), 5)


if __name__ == '__main__':
    unittest.main()
