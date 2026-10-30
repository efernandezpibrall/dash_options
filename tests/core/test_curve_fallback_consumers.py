from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from dash_options.core import database_reads, source_status
from dash_options.features.vol_trades import data, strip_data


def test_curve_helper_uses_trino_without_opening_postgres(monkeypatch):
    frame = pd.DataFrame({'value': [4.]})
    monkeypatch.setattr(database_reads, 'read_trino_query', lambda *a, **k: frame)
    monkeypatch.setattr(database_reads, 'get_database_engine', lambda: pytest.fail('Postgres must not open'))
    result, source = database_reads.read_curve_with_fallback('trino', 'postgres')
    assert result is frame and source == 'transformed.enverus.curve'


@pytest.mark.parametrize('empty', [pd.DataFrame(), pd.DataFrame({'latest_cob': [None]})])
def test_empty_curve_results_fall_back(monkeypatch, empty):
    monkeypatch.setattr(database_reads, 'read_trino_query', lambda *a, **k: empty)
    monkeypatch.setattr(database_reads.pd, 'read_sql', lambda q, engine, **k: pd.DataFrame({'latest_cob': ['2026-10-29']}))
    frame, source = database_reads.read_curve_with_fallback('trino', 'postgres', engine=object())
    assert source.endswith('.curve') and frame.latest_cob.iloc[0] == '2026-10-29'


def test_jkm_date_picker_works_without_postgres_curve(monkeypatch):
    dates = [date(2026, 10, 29), date(2026, 10, 28)]
    surfaces = pd.DataFrame({'business_date': dates, 'observed_at': dates, 'option_quote_count': [11, 11]})
    def read(query, *a, **k):
        if 'FROM at_lng.curve' in str(query): raise RuntimeError('optional table missing')
        return surfaces.copy()
    monkeypatch.setattr(data.pd, 'read_sql', read)
    monkeypatch.setattr(database_reads, 'read_trino_query', lambda *a, **k: pd.DataFrame({'business_date': dates[:1], 'forward_count': [96]}))
    result = data.load_available_jkm_cobs(engine=object())
    assert result.business_date.tolist() == dates[:1]
    assert result.forward_count.tolist() == [96]


def test_ttf_strip_uses_trino_and_preserves_units_and_date(monkeypatch):
    cob = date(2026, 10, 29)
    result = SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: {'commodity': 'TTF', 'cob_date': cob}))
    connection = SimpleNamespace(execute=lambda *a, **k: result)
    engine = SimpleNamespace(connect=lambda: nullcontext(connection))
    monkeypatch.setattr(strip_data, '_read_calibrated_surface_points', lambda *a: pd.DataFrame({'contract_date': [date(2026, 12, 1)]}))
    monkeypatch.setattr(database_reads, 'read_trino_query', lambda *a, **k: pd.DataFrame({'contract': ['2026M12'], 'value': [30.], 'currency': ['EUR'], 'units': ['MWH'], 'cob': [cob]}))
    monkeypatch.setattr(database_reads.pd, 'read_sql', lambda *a, **k: pytest.fail('PostgreSQL curve must not be queried'))
    result = strip_data.load_strip_inputs({'publication_id': 'publication', 'calibration': {'cob_date': str(cob)}}, ['2026-12-01'], engine=engine)
    row = result['forwards'].iloc[0]
    assert (row['value'], row.currency, row.units, row.cob, row.contract_month) == (30., 'EUR', 'MWH', cob, date(2026, 12, 1))


def test_missing_postgres_curve_does_not_mark_other_sources_unavailable(monkeypatch):
    engine = SimpleNamespace(connect=lambda: nullcontext(object()))
    monkeypatch.setattr(source_status, 'get_database_engine', lambda: engine)
    def read(query, *a, **k):
        if 'FROM at_lng.curve' in str(query): raise RuntimeError('missing curve')
        return pd.DataFrame({'label': ['Portfolio', 'Vol Surface'], 'latest_cob': ['2026-10-29'] * 2})
    monkeypatch.setattr(source_status.pd, 'read_sql', read)
    monkeypatch.setattr(database_reads, 'read_trino_query', lambda *a, **k: pd.DataFrame({'latest_cob': ['2026-10-29']}))
    statuses = source_status._load_dashboard_source_statuses_uncached(as_of='2026-10-30')
    assert all(s['error'] is None for s in statuses)
    assert statuses[-1]['source'] == 'transformed.enverus.curve'
    assert not statuses[-1]['fallback_used']
