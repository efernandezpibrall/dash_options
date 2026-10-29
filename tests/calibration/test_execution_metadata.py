"""Portable CLI resource accounting, including child work and failure paths."""

import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from dash_options.calibration.execution import metadata as execution_metadata


def test_command_sql_trace_is_scoped_redacted_and_preserves_failures():
    engine = create_engine('sqlite://')
    rows = []
    with execution_metadata.command_sql_trace(engine, rows.append):
        with engine.connect() as connection:
            assert connection.execute(text('SELECT :secret'), {'secret':'private-marker'}).scalar_one() == 'private-marker'
            with pytest.raises(OperationalError):
                connection.execute(text('SELECT * FROM missing_owned_test_table'))
    with engine.connect() as connection:
        connection.execute(text('SELECT 3'))
    assert len(rows) == 2
    assert [row['outcome'] for row in rows] == ['succeeded','failed']
    assert all(row['stage']=='command_sql' and row['seconds']>=0 for row in rows)
    assert 'private-marker' not in str(rows) and 'SELECT' not in str(rows)
    def unavailable(record):
        raise RuntimeError('instrumentation sink failure')
    with execution_metadata.command_sql_trace(engine, unavailable), engine.connect() as connection:
        assert connection.execute(text('SELECT 7')).scalar_one() == 7
        with pytest.raises(OperationalError):
            connection.execute(text('SELECT * FROM still_missing'))
    engine.dispose()


def test_nested_analytics_sql_is_counted_once_and_retains_phase():
    from options.vol_calibration.timing import calibration_trace, stage, sql_trace
    engine = create_engine("sqlite://")
    rows = []
    with calibration_trace(rows.append), stage("HH", "readback"):
        with execution_metadata.command_sql_trace(engine, rows.append), engine.connect() as connection:
            with sql_trace(connection, "HH"):
                connection.execute(text("SELECT 1"))
        with engine.connect() as connection, sql_trace(connection, "HH"):
            connection.execute(text("SELECT 2"))
    queries = [row for row in rows if row["stage"] in {"command_sql", "sql_execute"}]
    assert [row["stage"] for row in queries] == ["command_sql", "sql_execute"]
    assert all(row["product"] == "HH" and row["parent_stage"] == "readback" for row in queries)
    assert len({row["parent_span_id"] for row in queries}) == 1
    engine.dispose()
from dash_options.calibration.execution.metadata import command_usage


def test_cli_usage_includes_reaped_worker_cpu_and_explicit_memory_scope():
    records = []
    with command_usage(records.append):
        subprocess.run([sys.executable, '-I', '-c', 'sum(i*i for i in range(500000))'], check=True)
    record, = records
    assert record['outcome'] == 'succeeded' and record['seconds'] > 0
    assert record['process_cpu_seconds'] >= 0
    if sys.platform != 'win32':
        assert record['reaped_children_cpu_seconds'] > 0
        assert record['process_peak_rss_bytes'] > 0
        assert record['reported_children_peak_rss_bytes'] > 0
        assert record['average_cpu_cores'] > 0
    assert 'not aggregate' in record['memory_scope']


def test_resource_telemetry_cannot_replace_the_execution_failure(caplog):
    def failed_sink(_):
        raise OSError('sink unavailable')
    with pytest.raises(ValueError, match='original failure'), command_usage(failed_sink):
        raise ValueError('original failure')
    assert 'Resource telemetry unavailable: OSError' in caplog.text


def test_unavailable_counters_are_explicit_and_do_not_block_execution(monkeypatch):
    def unavailable():
        raise OSError('counter unavailable')
    monkeypatch.setattr(execution_metadata, '_usage', unavailable)
    records = []
    with command_usage(records.append):
        pass
    record, = records
    assert record['outcome'] == 'succeeded'
    assert record['average_cpu_cores'] is None and record['process_peak_rss_bytes'] is None
