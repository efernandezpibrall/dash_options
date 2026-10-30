import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from dash_options.core import release_diagnostics as diagnostics
from dash_options.core.config import clear_runtime_config_cache


def test_offline_report_is_safe_and_checks_import_roots(monkeypatch, tmp_path):
    path = tmp_path / 'config.ini'
    path.write_text('[DATABASE]\nCONNECTION_STRING=postgresql://name:private-password@host/db\n')
    monkeypatch.setenv('OPTIONS_CONFIG_PATH', str(path))
    clear_runtime_config_cache()
    monkeypatch.setattr(diagnostics, 'package_report', lambda: ({}, []))
    result = diagnostics.diagnose(expected_analytics_root=tmp_path / 'wrong-installation')
    assert result['status'] == 'failed'
    assert any('Unexpected import location' in s for s in result['errors'])
    assert 'private-password' not in json.dumps(result)
    assert 'database' not in result
    clear_runtime_config_cache()


def test_conflict_report_never_echoes_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv('OPTIONS_DATABASE_URL', 'postgresql://private-one')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://private-two')
    result = diagnostics.diagnose()
    assert result['status'] == 'failed'
    assert 'private-one' not in json.dumps(result) and 'private-two' not in json.dumps(result)


def test_database_diagnostics_are_read_only_and_curve_is_optional(database, monkeypatch):
    from dash_options.core.paths import application_root
    # The fixture database has the real migration graph; its curve table is optional.
    result = diagnostics.database_report(database, application_root(), 'at_lng')
    assert result['migrations']['status'] == 'current'
    assert 'curve' in result['relations']
    assert result['relations']['aspect_position']
    # Guard the diagnostic connection against writes even if a future probe attempts one.
    def attempt_write(*a, **k):
        connection = k['engine'].connection
        connection.execute(text('CREATE TABLE at_lng.diagnostic_must_not_write (x int)'))
    monkeypatch.setattr('options.vol_calibration.api.load_forward_curve', attempt_write)
    from datetime import date
    result = diagnostics.database_report(database, application_root(), 'at_lng', date(2026, 10, 29))
    assert result['curves']['TTF']['status'] == 'unavailable'
    with database.connect() as connection:
        assert connection.execute(text("SELECT to_regclass('at_lng.diagnostic_must_not_write')")).scalar() is None


def test_trino_publication_guard_does_not_require_postgres_curve(blank_database, monkeypatch):
    from options.vol_calibration import daily
    from contextlib import nullcontext
    with blank_database.begin() as connection:
        connection.execute(text('CREATE SCHEMA at_lng'))
        for table in ('implied_volatility_surface_from_prices', 'option_contract_expiries', 'mapping_volatility_surface_calendar'):
            connection.execute(text('CREATE TABLE at_lng.' + table + ' (id int)'))
    with blank_database.begin() as connection:
        engine = SimpleNamespace(connection=connection, connect=lambda: nullcontext(connection))
        inputs = SimpleNamespace(product='TTF', cob='2026-10-29', identity='pinned',
                                 manifest={'forward_source': {'source': 'trino'}})
        monkeypatch.setattr(daily, 'load_daily_inputs', lambda *a, **k: SimpleNamespace(identity='pinned'))
        assert daily.verify_daily_source(engine, inputs)
