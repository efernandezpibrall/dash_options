import json

import pandas as pd

from pages import greeks, prices, vol_surface


def test_prices_query_transfers_only_the_five_cobs_the_chart_can_render(monkeypatch):
    captured = {}

    def fake_read(trino_query, postgres_query, **kwargs):
        captured['trino'] = trino_query
        captured['postgres'] = str(postgres_query)
        captured['params'] = kwargs['postgres_params']
        return pd.DataFrame(columns=['code', 'COB', 'currency', 'units', 'expiry', 'contract', 'value'])

    monkeypatch.setattr(prices, 'read_with_fallback', fake_read)
    result = prices.get_enverus_underlying_prices('20260601', '20260713')
    assert result.empty
    assert 'LIMIT 5' in captured['trino']
    assert 'LIMIT 5' in captured['postgres']
    assert captured['params']['from_cob'] == pd.Timestamp('2026-06-01').date()


def test_greeks_browser_reference_preserves_payload_and_is_small():
    greeks._clear_greeks_server_cache()
    payload = {
        'meta': {'message': 'OK', 'raw_rows': 1, 'normalized_rows': 2},
        'rows': [
            {'greek': 'delta' if index % 2 == 0 else 'gamma', 'value': float(index)}
            for index in range(100)
        ],
    }
    reference = greeks._cache_greeks_payload(payload, 'test', ['snapshot'])
    assert greeks._resolve_greeks_payload(reference) == payload
    assert len(json.dumps(reference)) < len(json.dumps(payload))


def test_vol_surface_queries_only_columns_used_by_normalization():
    expected_columns = {
        'cob_date',
        'product',
        'maturity_date',
        'option_expiration_date',
        'put_call',
        'delta',
        'value',
    }

    assert set(vol_surface.SURFACE_SOURCE_COLUMNS) == expected_columns
    for _, query in vol_surface.SURFACE_POSTGRES_SOURCES:
        normalized_query = ' '.join(query.lower().split())
        assert 'select *' not in normalized_query
        assert all(column in normalized_query for column in expected_columns)
