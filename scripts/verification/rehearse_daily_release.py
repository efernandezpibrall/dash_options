"""Rehearse additive migrations, all-product publication and application rollback.

Only an owned temporary PostgreSQL is opened. The baseline is a calibration
fixture, not a production clone; three old external trade-table revisions are
explicitly excluded. Source bundles and the installed rollback release are
explicit paths, so the rehearsal does not depend on a workstation layout.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import numpy as np
from sqlalchemy import text
from options.vol_calibration import api

from scripts.verification.benchmark_daily_publication import candidates
from scripts.verification.verify_settlement_pipeline import (
    ROOT,
    temporary_postgres,
    prepare_external_calibration_fixture,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--cache-candidates', type=Path)
    parser.add_argument('--reference', type=Path, help='Independent numeric reference for all five products')
    parser.add_argument('--baseline-head', required=True)
    parser.add_argument('--rollback-release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rollback = args.rollback_release.absolute()
    reader_python = rollback / ('venv/Scripts/python.exe' if sys.platform=='win32' else 'venv/bin/python')
    health_paths = ('src/dash_options/core/health.py', 'health.py')
    if not reader_python.is_file() or not any((rollback / 'dashboard' / name).is_file() for name in health_paths):
        parser.error('An installed paired rollback release is required')
    config = Config(str(ROOT/'alembic.ini'))
    config.set_main_option('script_location',str(ROOT/'migrations'))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if not heads:
        raise ValueError('Application migration heads are unavailable')
    excluded = {'20260831_02','20260831_04','20260902_01'}
    baseline = {r.revision for r in script.iterate_revisions(args.baseline_head,'base')} - excluded
    upgrade = list(reversed(list(script.iterate_revisions(heads,args.baseline_head,implicit_base=True))))
    report = {'scope':__doc__, 'baseline_head':args.baseline_head, 'heads':sorted(heads),
              'baseline_calibration_fixture':sorted(baseline), 'external_trade_baseline_exclusions':sorted(excluded),
              'rollback_release':str(rollback), 'production_changed':False, 'migrations':[], 'products':[], 'complete':False}
    values = candidates(args.sources,args.cache_candidates,args.reference)
    with temporary_postgres() as engine:
        with engine.begin() as connection:
            for revision in reversed(list(script.walk_revisions('base','heads'))):
                if revision.revision in baseline:
                    with Operations.context(MigrationContext.configure(connection)):
                        revision.module.upgrade()
            # These existing history tables exercise the idempotent migration
            # path used by the deployed application's older Alembic marker.
            for code in ('20261016_01','20261016_02','20261016_03'):
                with Operations.context(MigrationContext.configure(connection)):
                    script.get_revision(code).module.upgrade()
            ctx = MigrationContext.configure(connection,opts={'version_table_schema':'at_lng'})
            ctx.stamp(script,args.baseline_head)
            prepare_external_calibration_fixture(connection)
            for revision in upgrade:
                with Operations.context(MigrationContext.configure(connection)):
                    revision.module.upgrade()
                report['migrations'].append(revision.revision)
            ctx.stamp(script,heads)
            assert set(connection.execute(text('SELECT version_num FROM at_lng.alembic_version')).scalars()) == set(heads)
        assert api.historical_content_snapshot(engine) == {}
        with tempfile.TemporaryDirectory(prefix='daily-rollback-') as directory:
            products = []
            for inputs,candidate in values:
                receipt = api.publish_hybrid_surface(engine,candidate['surface'],candidate['results'],
                    commodity=inputs.product,trading_date=inputs.cob,settlement_cob=inputs.cob,
                    actor_subject='rollback-rehearsal',created_by='rollback-rehearsal',
                    base_publication_id=None,expected_current_publication_id=None,idempotency_key=str(uuid4()),
                    expected_expiries=inputs.targets.contract_date,input_manifest=candidate['manifest'],
                    validation_certificate=candidate['certificate'],return_surface=False,stage_before_activation=True)
                verified = api.verify_daily_readback(engine,candidate,receipt['publication_id'])
                assert api.release_verified_stage(engine,receipt['publication_id'],actor='rollback-rehearsal')
                frame = candidate['surface'].sort_values(['contract_date','strike']).reset_index(drop=True)
                expected = Path(directory)/(inputs.product+'.npz')
                np.savez(expected,**{name:frame[name].to_numpy(float) for name in
                         ('strike','delta','volatility','total_variance','working_forward')})
                products.append({'product':inputs.product,'cob':str(inputs.cob),'expected':str(expected),
                                 'publication_id':receipt['publication_id'],'warnings':candidate['certificate'].get('warnings',[])})
                report['products'].append({'product':inputs.product,'points':verified['point_count']})
            before = api.historical_content_snapshot(engine)
            path = Path(directory)/'probe.json'
            path.write_text(json.dumps({'database_url':engine.url.render_as_string(hide_password=False),
                                        'migration_heads':sorted(heads),'products':products}))
            result = subprocess.run([str(reader_python),'-I',str(ROOT/'scripts/verification/probe_installed_daily_reader.py'),
                                     '--input',str(path),'--dashboard',str(rollback/'dashboard')],
                                    capture_output=True,text=True)
            if result.returncode:
                raise RuntimeError('Rollback reader/schema compatibility probe failed')
            report['rollback_reader_pricer_schema'] = json.loads(result.stdout)
            assert api.historical_content_snapshot(engine) == before
            report['historical_content_unchanged_by_reader_rollback'] = True
    report['complete'] = True
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
