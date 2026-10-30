"""Read-only, credential-safe checks for paired Linux/SFTP deployments.

No network calls unless --database or --curve-cob is explicitly selected.
No migrations, source ingestion, valuation or publication are performed.
"""
import argparse
import ast
from datetime import date
import hashlib
from importlib import import_module, metadata
import json
from pathlib import Path
import platform
import re
import sys
import tarfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version


PACKAGES = ('at-options-dashboard', 'at-options-analytics', 'dash', 'numpy', 'pandas',
            'scipy', 'SQLAlchemy', 'alembic', 'trino', 'psycopg2-binary', 'gunicorn')
MODULES = ('dash_options.core.config', 'options.market_data.runtime_config',
           'options.vol_calibration.io.forward_curves', 'options.vol_calibration.daily')
RELATIONS = ('trades_options', 'trades_options_valuation', 'option_contract_expiries',
             'mapping_volatility_surface_calendar', 'implied_volatility_surface_from_prices',
             'vol_market_snapshots', 'vol_surface_publications', 'aspect_position_snapshot',
             'aspect_position', 'aspect_valuation_run', 'aspect_option_valuation')


def package_report():
    result, errors = {}, []
    for name in PACKAGES:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            errors.append(f'Missing package: {name}')
            continue
        result[name] = {'version': dist.version, 'requires_python': dist.metadata.get('Requires-Python')}
        required_python = dist.metadata.get('Requires-Python')
        if required_python and Version(platform.python_version()) not in SpecifierSet(required_python):
            errors.append(f'{name}: incompatible Python version')
        for raw in dist.requires or ():
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate({'extra': ''}):
                continue
            try:
                installed = metadata.version(req.name)
            except metadata.PackageNotFoundError:
                errors.append(f'{name} requires missing package {req.name}')
                continue
            if req.specifier and Version(installed) not in req.specifier:
                errors.append(f'{name}: incompatible installed dependency {req.name}')
    return result, sorted(set(errors))


def migration_report(root, revisions):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option('script_location', str(root / 'migrations'))
    graph = ScriptDirectory.from_config(config)
    heads = sorted(graph.get_heads())
    active = {r.revision for r in graph.walk_revisions()}
    legacy = set()
    from importlib.resources import files
    archive = files('dash_options').joinpath('resources/migration_history/through_20261029_03.tar.gz')
    with archive.open('rb') as stream, tarfile.open(fileobj=stream, mode='r:gz') as tar:
        for item in tar:
            if item.isfile() and '/versions/' in item.name and item.name.endswith('.py'):
                for node in ast.parse(tar.extractfile(item).read()).body:
                    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'revision' for t in node.targets):
                        legacy.add(ast.literal_eval(node.value))
    if sorted(revisions) == heads:
        status = 'current'
    elif revisions and set(revisions) <= active:
        status = 'upgrade_required'
    elif revisions and set(revisions) <= active | legacy:
        status = 'legacy_upgrade_required'
    else:
        status = 'unknown_or_unversioned'
    return {'recorded': sorted(revisions), 'expected': heads, 'status': status}


def database_report(engine, root, schema, curve_cob=None):
    from sqlalchemy import text
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', schema):
        raise ValueError('Invalid configured schema')
    with engine.connect() as connection, connection.begin():
        connection.execute(text('SET TRANSACTION READ ONLY'))
        connection.execute(text("SET LOCAL statement_timeout='15s'"))
        names = (*RELATIONS, 'curve', 'alembic_version')
        relations = {name: bool(connection.execute(text('SELECT to_regclass(:name)'),
                     {'name': f'{schema}.{name}'}).scalar()) for name in names}
        revisions = list(connection.execute(text(f'SELECT version_num FROM {schema}.alembic_version')).scalars()) if relations['alembic_version'] else []
        result = {'relations': relations, 'migrations': migration_report(root, revisions)}
        if curve_cob:
            from options.vol_calibration import api
            # The fallback reads through this same read-only transaction.
            class Bound:
                pass
            bound = Bound()
            bound.connection = connection
            result['curves'] = {}
            for product in ('TTF', 'JKM', 'NBP'):
                try:
                    with connection.begin_nested():
                        frame = api.load_forward_curve(product, curve_cob, engine=bound)
                    source = frame.attrs['forward_source']
                    result['curves'][product] = {k: source[k] for k in ('source', 'table', 'cob_date', 'identity')}
                    result['curves'][product]['rows'] = len(frame)
                except Exception as exc:
                    result['curves'][product] = {'status': 'unavailable', 'error_type': type(exc).__name__}
        return result


def diagnose(*, database=False, curve_cob=None, expected_dashboard_root=None, expected_analytics_root=None):
    result = {'status': 'passed', 'python': platform.python_version(), 'platform': platform.system(),
              'executable': sys.executable, 'checks': {}, 'errors': [], 'warnings': []}
    errors = result['errors']
    if sys.version_info < (3, 11):
        errors.append('Dashboard requires Python 3.11 or newer')
    result['packages'], dependency_errors = package_report()
    errors.extend(dependency_errors)
    try:
        from options.market_data.runtime_config import load_config
        from options.market_data.runtime_config import load_config as get_config
        from dash_options.core.config import load_runtime_config
        from dash_options.core.paths import application_root
        config = load_config(allow_empty=True)
        web = load_runtime_config()
        analytics = get_config() if config.sections() else config
        keys = [('DATABASE', 'CONNECTION_STRING'), ('DATABASE', 'SCHEMA')]
        keys += [('TRINOS', key) for key in ('HOST', 'PORT', 'USERNAME', 'TOKEN', 'HTTP_SCHEME', 'VERIFY_SSL')]
        consistent = all(web.get(s, k, fallback=None) == analytics.get(s, k, fallback=None) for s, k in keys)
        result['checks']['configuration'] = {'consistent': consistent, 'file': config.source_path,
            'database_configured': bool(config.get('DATABASE', 'CONNECTION_STRING', fallback='')),
            'trino_configured': all(config.get('TRINOS', k, fallback='') for k in ('USERNAME', 'TOKEN'))}
        if not consistent:
            errors.append('Web and analytics configurations disagree')
        result['modules'] = {}
        for name in MODULES:
            path = Path(import_module(name).__file__).resolve()
            expected = expected_dashboard_root if name.startswith('dash_options.') else expected_analytics_root
            matched = expected is None or path.is_relative_to(Path(expected).resolve())
            result['modules'][name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'expected_root_matches': matched}
            if not matched:
                errors.append(f'Unexpected import location: {name}')
        from options.vol_calibration import api
        result['analytics_identity'] = api.implementation_fingerprint()
        root = application_root()
        result['application_root'] = str(root)
        if not (root / 'migrations').is_dir() or not (root / 'alembic.ini').is_file():
            errors.append('Missing release migration tree; set OPTIONS_APP_ROOT')
        if database or curve_cob:
            from sqlalchemy import create_engine
            schema = config.get('DATABASE', 'SCHEMA', fallback='at_lng') or 'at_lng'
            engine = create_engine(config.get('DATABASE', 'CONNECTION_STRING'), connect_args={'connect_timeout': 10})
            try:
                db = result['database'] = database_report(engine, root, schema, curve_cob)
            finally:
                engine.dispose()
            if db['migrations']['status'] != 'current':
                errors.append('Database migration check: ' + db['migrations']['status'])
            errors.extend('Missing required relation: ' + name for name in RELATIONS if not db['relations'][name])
            if not db['relations']['curve']:
                result['warnings'].append('PostgreSQL curve fallback unavailable; Trino curves must be accessible')
            if any(v.get('status') == 'unavailable' for v in db.get('curves', {}).values()):
                errors.append('Requested exact-COB curve probe failed')
    except Exception as exc:
        # Driver/parser errors can embed credentials. Report type, never raw exception text.
        errors.append('Diagnostic could not complete: ' + type(exc).__name__)
    result['status'] = 'failed' if errors else 'passed'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', action='store_true', help='Inspect configured PostgreSQL in a read-only transaction')
    parser.add_argument('--curve-cob', type=date.fromisoformat, help='Probe exact-date Trino-first gas curves (also inspects PostgreSQL)')
    parser.add_argument('--expected-dashboard-root', type=Path)
    parser.add_argument('--expected-analytics-root', type=Path)
    args = parser.parse_args(argv)
    report = diagnose(**vars(args))
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
