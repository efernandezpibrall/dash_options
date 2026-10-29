"""Verified backup/restore and atomic retirement of five obsolete history tables."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from sqlalchemy import text
from dash_options.core.config import get_database_engine

TABLES = ('brent_delta_history_points','brent_delta_history_settlements',
          'brent_delta_history_runs','brent_realized_history_settlements','brent_realized_history_runs')


def fingerprints(db):
    db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    result = {}
    for table in TABLES:
        values = sorted(db.execute(text('SELECT to_jsonb(t)::text FROM at_lng.'+table+' t')).scalars())
        result[table] = {'rows':len(values),'sha256':hashlib.sha256('\n'.join(values).encode()).hexdigest()}
    return result


def restore(engine, archive):
    proof = json.loads((archive/'restore_rehearsal.json').read_text())
    dump = archive/'brent_history_restore.dump'
    if hashlib.sha256(dump.read_bytes()).hexdigest()!=proof['dump_sha256']:
        raise ValueError('Restore dump checksum mismatch')
    with engine.begin() as db:
        existing = [db.execute(text('SELECT to_regclass(:name)'),{'name':'at_lng.'+t}).scalar() for t in TABLES]
        if all(existing):
            if fingerprints(db)!=proof['tables']:
                raise ValueError('Existing history differs from the archive; nothing overwritten')
            return
        if any(existing):
            raise ValueError('Partial existing history requires recovery review; nothing overwritten')
        db.execute(text('CREATE SCHEMA IF NOT EXISTS at_lng'))
    u = engine.url
    subprocess.run(['pg_restore','--exit-on-error','--single-transaction','--no-owner','--no-acl',
        '-h',u.host,'-p',str(u.port or 5432),'-U',u.username,'-d',u.database,str(dump)],
        env={**os.environ,'PGPASSWORD':u.password or ''},check=True,capture_output=True)
    with engine.begin() as db:
        if fingerprints(db)!=proof['tables']:
            raise ValueError('Restored history failed exact row reconciliation')


def retire(engine, archive):
    proof = json.loads((archive/'restore_rehearsal.json').read_text())
    dump = archive/'brent_history_restore.dump'
    if proof['status']!='passed' or hashlib.sha256(dump.read_bytes()).hexdigest()!=proof['dump_sha256']:
        raise ValueError('A verified restorable backup is required')
    with engine.begin() as db:
        db.execute(text("SET LOCAL lock_timeout='5s'"))
        db.execute(text("SET LOCAL statement_timeout='60s'"))
        for table in TABLES:
            db.execute(text('LOCK TABLE at_lng.'+table+' IN ACCESS EXCLUSIVE MODE'))
        if fingerprints(db)!=proof['tables']:
            raise ValueError('History changed since backup; retirement cancelled')
        # RESTRICT prevents silently removing an unexpected dependent object.
        for table in TABLES:
            db.execute(text('DROP TABLE at_lng.'+table+' RESTRICT'))
    with engine.connect() as db:
        if any(db.execute(text('SELECT to_regclass(:name)'),{'name':'at_lng.'+t}).scalar() for t in TABLES):
            raise ValueError('Retirement readback failed')
    (archive/'retirement.json').write_text(json.dumps({'status':'retired','tables':list(TABLES),
        'dump_sha256':proof['dump_sha256'],'row_fingerprints':proof['tables']},indent=2)+'\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--restore',action='store_true')
    p.add_argument('--retire',action='store_true')
    args = p.parse_args()
    if args.restore==args.retire:
        p.error('Choose exactly one action')
    (restore if args.restore else retire)(get_database_engine(),args.archive)
    print('Restore verified' if args.restore else 'Five history tables retired and absence verified')


if __name__=='__main__':
    main()
