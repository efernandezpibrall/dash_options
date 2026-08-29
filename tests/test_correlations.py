import numpy as np
import pandas as pd

from pages import correlations
from pages.correlations import (
    MIN_OBSERVATIONS,
    _matrix_figure,
    assign_delivery_periods,
    calculate_correlation_analysis,
    group_forward_prices,
)


def _curve_rows(trade_dates, product, maturities, prices):
    return pd.DataFrame(
        [
            {
                'trade_date': trade_date,
                'product': product,
                'maturity_date': maturity,
                'price': price,
            }
            for trade_date, price in zip(trade_dates, prices)
            for maturity in maturities
        ]
    )


def test_seasonal_period_boundaries_and_complete_strip_requirement():
    maturities = pd.date_range('2026-10-01', '2027-09-01', freq='MS')
    frame = pd.DataFrame(
        {
            'trade_date': pd.Timestamp('2026-07-08'),
            'product': 'JKM',
            'maturity_date': maturities,
            'price': np.arange(len(maturities), dtype=float) + 10,
        }
    )
    assigned = assign_delivery_periods(frame, 'seasonal')
    assert assigned.loc[assigned['maturity_date'].dt.month.isin([10, 11, 12, 1, 2, 3, 4]), 'period'].nunique() == 1
    grouped = group_forward_prices(frame, 'seasonal')
    assert dict(zip(grouped['period'], grouped['delivery_months'])) == {
        'Winter 2026/27': 7,
        'Summer 2027': 5,
    }

    incomplete = frame[frame['maturity_date'] != pd.Timestamp('2027-04-01')]
    incomplete_grouped = group_forward_prices(incomplete, 'seasonal')
    assert 'Winter 2026/27' not in set(incomplete_grouped['period'])


def test_correlation_requires_overlap_and_non_constant_returns():
    trade_dates = pd.bdate_range('2026-01-01', periods=MIN_OBSERVATIONS + 5)
    maturities = [pd.Timestamp('2027-01-01')]
    jkm = _curve_rows(trade_dates, 'JKM', maturities, np.linspace(10, 14, len(trade_dates)))
    ttf = _curve_rows(trade_dates, 'TTF', maturities, np.linspace(20, 26, len(trade_dates)))
    hh = _curve_rows(trade_dates, 'HH', maturities, np.repeat(3.0, len(trade_dates)))
    grouped = group_forward_prices(pd.concat([jkm, ttf, hh], ignore_index=True), 'monthly')
    analysis = calculate_correlation_analysis(grouped, '2027-01', ['JKM', 'TTF', 'HH'], 'JKM', 'TTF', 10)
    assert len(analysis['pair']) == len(trade_dates) - 1
    assert analysis['correlations'].loc['JKM', 'TTF'] > 0.99
    assert pd.isna(analysis['correlations'].loc['JKM', 'HH'])
    assert analysis['overlap'].loc['JKM', 'TTF'] == len(trade_dates) - 1
    assert _matrix_figure(analysis).data[0].type == 'heatmap'


def test_overlap_counts_preserve_pairwise_missing_observations():
    dates = pd.bdate_range('2026-01-01', periods=MIN_OBSERVATIONS + 4)
    grouped = pd.DataFrame(
        {
            'trade_date': np.repeat(dates, 3),
            'product': np.tile(['JKM', 'TTF', 'HH'], len(dates)),
            'period': '2027-01',
            'price': np.tile([10.0, 20.0, 3.0], len(dates)),
        }
    )
    grouped.loc[
        grouped['product'].eq('TTF')
        & grouped['trade_date'].isin(dates[[3, 7]]),
        'price',
    ] = np.nan

    analysis = calculate_correlation_analysis(
        grouped,
        '2027-01',
        ['JKM', 'TTF', 'HH'],
        'JKM',
        'TTF',
        10,
    )

    present = analysis['returns'].notna().astype(int)
    expected = present.T.dot(present).astype('Int64')
    pd.testing.assert_frame_equal(analysis['overlap'], expected)


def test_control_loading_exposes_source_failure_without_callback_error(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(correlations, '_load_grouped_data', unavailable)

    result = correlations.update_correlation_controls(
        'monthly', ['JKM', 'TTF'], '1Y', 0, None, None, None
    )

    assert result[:2] == ([], None)
    assert result[3] == 'JKM'
    assert result[5] == 'TTF'
    assert result[6].children == 'Correlation source is unavailable.'


def test_control_loading_clears_cache_only_for_refresh_trigger(monkeypatch):
    clear_calls = []
    monkeypatch.setattr(correlations, 'clear_forward_curve_cache', lambda: clear_calls.append(True))
    monkeypatch.setattr(
        correlations,
        '_load_grouped_data',
        lambda *_args, **_kwargs: (pd.DataFrame(), pd.DataFrame()),
    )
    monkeypatch.setattr(correlations, 'triggered_id', lambda: 'correlations-history')
    correlations.update_correlation_controls('monthly', ['JKM', 'TTF'], '1Y', 3, None, None, None)
    assert clear_calls == []

    monkeypatch.setattr(correlations, 'triggered_id', lambda: 'refresh-options-data')
    correlations.update_correlation_controls('monthly', ['JKM', 'TTF'], '1Y', 3, None, None, None)
    assert clear_calls == [True]

    monkeypatch.setattr(correlations, 'triggered_id', lambda: None)
    correlations.update_correlation_controls('monthly', ['JKM', 'TTF'], '1Y', 3, None, None, None)
    assert clear_calls == [True, True]
