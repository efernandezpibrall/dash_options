import json

import pandas as pd
import pytest

import market_data
import surface_data
from pages import greeks, prices


def test_prices_query_transfers_only_the_five_cobs_the_chart_can_render(monkeypatch):
    captured = {}

    def fake_read(trino_query, postgres_query, **kwargs):
        captured['trino'] = trino_query
        captured['postgres'] = str(postgres_query)
        captured['params'] = kwargs['postgres_params']
        return pd.DataFrame(columns=['code', 'COB', 'currency', 'units', 'expiry', 'contract', 'value'])

    monkeypatch.setattr(market_data, 'read_with_fallback', fake_read)
    result = market_data.load_recent_underlying_prices('20260601', '20260713')
    assert result.empty
    assert 'LIMIT 5' in captured['trino']
    assert 'LIMIT 5' in captured['postgres']
    assert captured['params']['from_cob'] == pd.Timestamp('2026-06-01').date()


@pytest.mark.parametrize(
    ('grouping', 'ttf_first', 'hh', 'ttf_next'),
    [
        (
            'monthly',
            {"Apr'26": 10, "May'26": 20, "Sep'26": 40, "Oct'26": 60, "Dec'26": 80, "Jan'27": 100},
            {"Apr'26": 110, "May'26": 120},
            {"Apr'26": 210, "May'26": 220},
        ),
        ('quarterly', {"Q2'26": 15, "Q3'26": 40, "Q4'26": 70, "Q1'27": 100},
         {"Q2'26": 115}, {"Q2'26": 215}),
        # Preserve this page's existing May-September summer convention.
        ('season', {"Win'26": 50, "Sum'26": 30, "Win'27": 100},
         {"Win'26": 110, "Sum'26": 120}, {"Win'26": 210, "Sum'26": 220}),
        ('calendar', {'2026': 42, '2027': 100}, {'2026': 115}, {'2026': 215}),
    ],
)
def test_price_period_grouping_preserves_labels_averages_and_source_groups(
    grouping, ttf_first, hh, ttf_next,
):
    frame = pd.DataFrame(
        [
            ('TTF', '2026-07-30', '2026-04-01', 10),
            ('TTF', '2026-07-30', '2026-05-01', 20),
            ('TTF', '2026-07-30', '2026-09-01', 40),
            ('TTF', '2026-07-30', '2026-10-01', 60),
            ('TTF', '2026-07-30', '2026-12-01', 80),
            ('TTF', '2026-07-30', '2027-01-01', 100),
            ('TTF', '2026-07-30', 'invalid maturity', 999),
            ('HH', '2026-07-30', '2026-04-01', 110),
            ('HH', '2026-07-30', '2026-05-01', 120),
            ('TTF', '2026-07-31', '2026-04-01', 210),
            ('TTF', '2026-07-31', '2026-05-01', 220),
        ],
        columns=['contract', 'trade_date', 'maturity_date', 'settlement_price'],
    )
    before = frame.copy(deep=True)

    result = prices.group_data_by_period(frame, grouping)

    expected = {}
    for contract, cob, values in (
        ('TTF', '2026-07-30', ttf_first),
        ('HH', '2026-07-30', hh),
        ('TTF', '2026-07-31', ttf_next),
    ):
        expected.update({(contract, cob, period): value for period, value in values.items()})
    actual = {
        (row.contract, row.trade_date, row.period): row.settlement_price
        for row in result.itertuples()
    }
    assert len(result) == len(expected)
    assert actual == pytest.approx(expected)
    pd.testing.assert_frame_equal(frame, before)


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

    assert set(surface_data.SURFACE_SOURCE_COLUMNS) == expected_columns
    for _, query in surface_data.SURFACE_POSTGRES_SOURCES:
        normalized_query = ' '.join(query.lower().split())
        assert 'select *' not in normalized_query
        assert all(column in normalized_query for column in expected_columns)
