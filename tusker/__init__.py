from contextlib import contextmanager
import argparse
import datetime
import os
import re
import string
import sys
import time

import migra
import psycopg2
import sqlbag
import schemainspect
import sqlalchemy

from .config import Config

TUSKER_COMMENT = 'CREATED BY TUSKER - If this table is left behind tusker probably crashed and was not able to clean up after itself. Either try running `tusker clean` or remove this database manually.'


class DatabaseAdmin:

    def __init__(self, cfg: Config):
        self.config = cfg.database
        self.conn = self._connect('template1')
        self.conn.autocommit = True

    def _connect(self, name):
        if self.config.url:
            return psycopg2.connect(self.config.url, dbname='template1')
        else:
            return psycopg2.connect(**self.config.args(dbname='template1'))

    @contextmanager
    def createdb(self, suffix):
        cursor = self.conn.cursor()
        now = int(time.time())
        dbname = f'{self.config.dbname}_{now}_{suffix}'
        cursor.execute(f'CREATE DATABASE "{dbname}"')
        cursor.execute(f'COMMENT ON DATABASE "{dbname}" IS \'{TUSKER_COMMENT}\'')
        try:
            if self.config.url:
                engine = sqlalchemy.create_engine(
                    self.config.url,
                    connect_args={'dbname': dbname},
                )
            else:
                engine = sqlalchemy.create_engine(
                    'postgresql://', 
                    connect_args=self.config.args(dbname=dbname)
                )
            try:
                yield engine
            finally:
                engine.dispose()
        finally:
            # For some reason the connection is not properly returned/closed
            # by migra and/or SQLAlchemy. In order to fix this issue simply
            # terminate all backends still using this database.
            cursor.execute('''
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s
            ''', [dbname])
            cursor.execute(f'DROP DATABASE {dbname}')


def cmd_diff(args, cfg: Config):
    dba = DatabaseAdmin(cfg)
    if args.verbose:
        print('Creating databases...', out=sys.stderr)
    with dba.createdb('schema') as schema_engine, dba.createdb('migrations') as migrations_engine:
        schema_cursor = schema_engine.connect()
        if args.verbose:
            print('Creating target schema...', out=sys.stderr)
        #with schema_engine as schema_cursor:
        with open(cfg.schema.filename) as fh:
            sql = fh.read()
            if sql.strip():
                schema_cursor.execute(sql)
        if args.verbose:
            print('Creating migrated schema...', out=sys.stderr)
        migrations_cursor = migrations_engine.connect()
        for filename in sorted(os.listdir(cfg.migrations.directory)):
            if not filename.endswith('.sql'):
                continue
            if args.verbose:
                print(f"- {filename}", out=sys.stderr)
            filename = os.path.join(cfg.migrations.directory, filename)
            with open(filename) as fh:
                sql = fh.read()
                if sql.strip():
                    migrations_cursor.execute(sql)
        if args.verbose:
            print('Diffing...', out=sys.stderr)
        from schemainspect.pg import PostgreSQL
        migration = migra.Migration(migrations_engine, schema_engine)
        migration.set_safety(False)
        migration.add_all_changes()
        print(migration.sql, end='')


def cmd_clean(args, cfg: Config):
    dba = DatabaseAdmin(cfg)
    cursor = dba.conn.cursor()
    try:
        cursor.execute('''
            SELECT db.datname
            FROM pg_database db
            JOIN pg_shdescription dsc ON dsc.objoid = db.oid
            WHERE dsc.description = %s;
        ''', (TUSKER_COMMENT,))
        rows = cursor.fetchall()
        for row in rows:
            dbname = row[0]
            if '"' in dbname:
                raise RuntimeError('Database with an " in its name found. Please fix that manually.')
            if args.verbose:
                print(f'Dropping {dbname} ...', out=sys.stderr)
            cursor.execute(f'DROP DATABASE "{dbname}"')
    finally:
        cursor.close()

            

def main():
    parser = argparse.ArgumentParser(
        description='Generate a database migration.')
    parser.add_argument(
        "--verbose",
        help='Enable verbose output', 
        action='store_true',
        default=False)
    parser.add_argument(
        "--config",
        help='The configuration file. Default: tusker.toml', 
        default='tusker.toml')
    subparsers = parser.add_subparsers(
        dest='command',
        required=True)
    parser_diff = subparsers.add_parser(
        'diff',
        help='Show differences of target schema and migrations')
    parser_diff.set_defaults(func=cmd_diff)
    parser_clean = subparsers.add_parser(
        'clean',
        help='Clean up left ofter *_migrations or *_schema tables')
    parser_clean.set_defaults(func=cmd_clean)
    args = parser.parse_args()
    cfg = Config(args.config)
    args.func(args, cfg)
