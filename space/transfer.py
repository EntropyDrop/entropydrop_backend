"""Explicit Space-only COPY export/import. Credentials come only from environment.

export SOURCE_DATABASE_URL -> directory; import directory -> DATABASE_URL.
Import refuses nonempty target tables and commits every table in one transaction.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from config import settings
from space.database import Base, engine
from space import models

SKIP = {'space_hosting_workers', 'space_hosting_grants', 'space_hosting_authorizations'}


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def export(directory):
    source = create_engine(os.environ['SOURCE_DATABASE_URL'], isolation_level='REPEATABLE READ', hide_parameters=True)
    manifest = {'tables':[], 'objects':[]}
    with source.connect() as conn, conn.begin():
        conn.execute(text('SET TRANSACTION READ ONLY'))
        schema = inspect(conn)
        cursor = conn.connection.cursor()
        refs=[]
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP or table.name == 'space_accounts':
                continue
            for column in table.columns:
                if any(fk.target_fullname == 'space_accounts.id' for fk in column.foreign_keys):
                    refs.append(f'SELECT "{column.name}" FROM "{table.name}" WHERE "{column.name}" IS NOT NULL')
        ids = ' UNION '.join(refs)
        account_query = f'''SELECT id, username, skin_url, coalesce(skin_type, 'strong') AS skin_type,
            CURRENT_TIMESTAMP AS updated_at FROM users WHERE id IN ({ids}) ORDER BY id'''
        queries = [('space_accounts', ['id','username','skin_url','skin_type','updated_at'], account_query)]
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP or table.name == 'space_accounts':
                continue
            names={c['name'] for c in schema.get_columns(table.name)}
            columns=[c.name for c in table.columns if c.name in names]
            quoted=', '.join(f'"{c}"' for c in columns)
            order=', '.join(f'"{c.name}"' for c in table.primary_key)
            queries.append((table.name,columns,f'SELECT {quoted} FROM "{table.name}" ORDER BY {order}'))
        for name,columns,query in queries:
            path=directory/f'{name}.csv'
            with path.open('w') as output:
                cursor.copy_expert(f'COPY ({query}) TO STDOUT WITH (FORMAT CSV)', output)
            count=conn.execute(text(f'SELECT count(*) FROM ({query}) AS counted')).scalar_one()
            manifest['tables'].append({'name':name,'columns':columns,'rows':count,'sha256':sha(path)})
        manifest['objects'] = [dict(row) for row in conn.execute(text(
            'SELECT id, object_key, size_bytes FROM space_market_resources WHERE object_key IS NOT NULL')).mappings()]
    (directory/'manifest.json').write_text(json.dumps(manifest,indent=2,default=str))
    print(json.dumps({t['name']:t['rows'] for t in manifest['tables']}))


def restore(directory):
    manifest=json.loads((directory/'manifest.json').read_text())
    expected = set(Base.metadata.tables) - SKIP
    if {t['name'] for t in manifest['tables']} != expected:
        raise RuntimeError('Snapshot table inventory does not match Space schema')
    for table in manifest['tables']:
        if sha(directory/f"{table['name']}.csv") != table['sha256']:
            raise RuntimeError('Snapshot checksum mismatch: '+table['name'])
    with engine.begin() as conn:
        cursor=conn.connection.cursor()
        for table in Base.metadata.sorted_tables:
            if conn.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar_one():
                raise RuntimeError('Refusing to overwrite nonempty target: '+table.name)
        for table in manifest['tables']:
            name=table['name']
            valid={c.name for c in Base.metadata.tables[name].columns}
            if not set(table['columns']) <= valid:
                raise RuntimeError('Unknown snapshot columns')
            quoted=', '.join(f'"{c}"' for c in table['columns'])
            with (directory/f'{name}.csv').open() as source:
                cursor.copy_expert(f'COPY "{name}" ({quoted}) FROM STDIN WITH (FORMAT CSV)',source)
            count=conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
            if count != table['rows']:
                raise RuntimeError('Imported row count mismatch: '+name)
    print('Space import committed; all table checksums and row counts verified')


if __name__ == '__main__':
    if not settings.SPACE_STANDALONE:
        raise RuntimeError('Transfer requires SPACE_STANDALONE=true')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['export','import'])
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    if args.operation == 'export':
        args.directory.mkdir(parents=True,exist_ok=False)
        export(args.directory)
    else:
        restore(args.directory)
