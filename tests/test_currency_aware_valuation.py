import pandas as pd

from pages import pnl_explain, scenarios, valuation


def _valuation_rows():
    return pd.DataFrame(
        [
            {
                'currency': 'USD',
                'unit_quantity': 'MMBtu',
                'substrategy': 'Same strategy',
                'maturity_date_a': pd.Timestamp('2027-01-01'),
                'price': 1.2345,
                'intrinsic_value': 1.0,
                'time_value': 0.2345,
                'qty_value': 100.25,
                'qty_intrinsic_value': 80.00,
                'qty_time_value': 20.25,
                'qty_premium': -10.00,
                'qty_pnl': 90.25,
                'quantity': 100,
            },
            {
                'currency': 'EUR',
                'unit_quantity': 'MWh',
                'substrategy': 'Same strategy',
                'maturity_date_a': pd.Timestamp('2027-01-01'),
                'price': 2.3456,
                'intrinsic_value': 2.0,
                'time_value': 0.3456,
                'qty_value': 200.25,
                'qty_intrinsic_value': 170.00,
                'qty_time_value': 30.25,
                'qty_premium': -20.00,
                'qty_pnl': 180.25,
                'quantity': 100,
            },
        ]
    )


def test_valuation_builds_one_final_total_per_currency(monkeypatch):
    aspect = pd.DataFrame()
    aspect.attrs['currency_warning'] = (
        'Aspect YTD suppressed: currency is unavailable from the source.'
    )
    monkeypatch.setattr(
        valuation,
        '_read_pnl_sources_for_date',
        lambda selected_date, engine: (_valuation_rows(), aspect),
    )
    monkeypatch.setattr(
        valuation,
        'get_database_engine',
        lambda **kwargs: object(),
    )

    result = valuation.fetch_pnl_data(
        object(),
        '2026-07-27',
        ['Same strategy'],
    )
    final_totals = result[
        result['substrategy'].eq('All')
        & result['unit_quantity'].eq('All')
    ]

    assert len(final_totals) == 2
    assert dict(
        zip(final_totals['currency'], final_totals['qty_pnl'])
    ) == {'EUR': 180.25, 'USD': 90.25}
    assert final_totals[
        ['price', 'intrinsic_value', 'time_value']
    ].isna().all().all()
    assert valuation._valuation_warning.startswith('Aspect YTD suppressed')


def test_valuation_records_show_two_decimals_with_explicit_currency():
    records = valuation._clean_valuation_records(
        pd.DataFrame(
            [
                {
                    'currency': 'USD',
                    'qty_value': 100.25,
                    'qty_pnl': 90.2,
                }
            ]
        )
    )

    assert records[0]['qty_value'] == '100.25 USD'
    assert records[0]['qty_pnl'] == '90.20 USD'
    assert records[0]['__qty_pnl_raw'] == 90.2


def test_scenario_and_pnl_grids_preserve_currency_and_cents():
    scenario_rows = pd.DataFrame(
        [
            {
                'currency': 'USD',
                'substrategy': 'One',
                **{
                    column: 1.25
                    for column in scenarios.PNL_COLUMNS
                },
            }
        ]
    )
    scenario_grid, scenario_columns = scenarios._grid_payload(
        scenario_rows,
        'substrategy',
    )
    assert scenario_grid[0]['currency'] == 'USD'
    assert scenario_grid[0]['exact_pnl'] == 1.25
    assert "d3.format(',.2f')" in next(
        column for column in scenario_columns
        if column['field'] == 'exact_pnl'
    )['valueFormatter']['function']

    explain_rows = pd.DataFrame(
        [
            {
                'currency': 'EUR',
                'substrategy': 'One',
                **{
                    column: 2.25
                    for column in pnl_explain.EXPLAIN_COLUMNS
                },
            }
        ]
    )
    explain_grid, explain_columns = pnl_explain._grid_payload(
        explain_rows,
        'substrategy',
    )
    assert explain_grid[0]['currency'] == 'EUR'
    assert explain_grid[0]['actual_pnl'] == 2.25
    assert "d3.format(',.2f')" in next(
        column for column in explain_columns
        if column['field'] == 'actual_pnl'
    )['valueFormatter']['function']
