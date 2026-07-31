import json

import pandas as pd

from pages import greeks, prices


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
