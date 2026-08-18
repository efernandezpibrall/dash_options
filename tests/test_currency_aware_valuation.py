import base64
from io import BytesIO

import pandas as pd
from openpyxl import load_workbook

from pages import pnl_explain, scenarios, valuation


def _walk(component):
    yield component
    children = getattr(component, 'children', None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if child is not None:
            yield from _walk(child)


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
        valuation.ASPECT_SOURCE_UNAVAILABLE_MESSAGE
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
    assert valuation._valuation_warning == 'Aspect data unavailable'


def test_valuation_table_has_explicit_currency_and_whole_number_totals():
    frame = pd.DataFrame(
        [
            {
                'currency': 'USD',
                'substrategy': 'Same strategy',
                'year': 2027,
                'unit_quantity': 'MMBtu',
                'price': 1.2345,
                'intrinsic_value': 1.0,
                'time_value': 0.2345,
                'qty_value': 100.25,
                'qty_pnl': 90.2,
            }
        ]
    )
    records = valuation._clean_valuation_records(
        frame.reindex(columns=valuation.VALUATION_TABLE_COLUMNS)
    )
    fields = [
        definition['field']
        for definition in valuation.build_valuation_column_defs(frame)
    ]

    assert fields[0] == 'currency'
    assert {'price', 'intrinsic_value', 'time_value'}.isdisjoint(fields)
    assert records[0]['currency'] == 'USD'
    assert records[0]['qty_value'] == '100'
    assert records[0]['qty_pnl'] == '90'
    assert 'USD' not in records[0]['qty_value']
    assert records[0]['__qty_pnl_raw'] == 90.2


def test_valuation_filters_reuse_the_greeks_sticky_toolbar_contract():
    toolbar = next(
        component
        for component in _walk(valuation.layout)
        if 'valuation-monitor-controls'
        in getattr(component, 'className', '').split()
    )
    selector_row = next(
        component
        for component in _walk(toolbar)
        if 'valuation-monitor-selector-row'
        in getattr(component, 'className', '').split()
    )
    ids = {
        getattr(component, 'id', None)
        for component in _walk(selector_row)
        if getattr(component, 'id', None)
    }
    labels = [
        component.children
        for component in _walk(selector_row)
        if 'inline-filter-label'
        in getattr(component, 'className', '').split()
    ]

    assert 'greeks-sticky-filter-bar' in toolbar.className.split()
    assert labels == ['COB Date', 'Strategies']
    assert ids == {
        'pnl-date-dropdown',
        'pnl-strategy-dropdown',
        'valuation-source-status-mount',
        'valuation-aspect-source-status',
        'valuation-source-status-inline',
    }
    assert 'greeks-compact-multi-dropdown' in next(
        component
        for component in _walk(selector_row)
        if getattr(component, 'id', None) == 'pnl-strategy-dropdown'
    ).className.split()
    assert 'greeks-inline-source-status' in selector_row.children[2].className.split()


def test_valuation_page_omits_redundant_currency_note():
    visible_text = ' '.join(
        str(component.children)
        for component in _walk(valuation.layout)
        if isinstance(getattr(component, 'children', None), str)
    )

    assert 'All values use native contract currency' not in visible_text


def test_aspect_unavailable_moves_to_source_status_instead_of_table_error(monkeypatch):
    frame = _valuation_rows()
    aspect = pd.DataFrame()
    aspect.attrs['currency_warning'] = valuation.ASPECT_SOURCE_UNAVAILABLE_MESSAGE
    monkeypatch.setattr(
        valuation,
        '_read_pnl_sources_for_date',
        lambda selected_date, engine: (frame, aspect),
    )
    monkeypatch.setattr(
        valuation,
        'get_database_engine',
        lambda **kwargs: object(),
    )

    rows, columns, error, error_style, aspect_status = valuation.update_pnl_table(
        '2026-07-30',
        ['Same strategy'],
        1,
    )

    assert rows
    assert columns
    assert error == ''
    assert error_style == valuation.ERROR_STYLE_HIDDEN
    assert aspect_status == {
        'label': 'Aspect',
        'source': 'at_lng.pnl_aspect',
        'latest_cob': None,
        'business_day_age': None,
        'fallback_used': False,
        'error': 'Aspect data unavailable',
    }


def test_valuation_export_matches_the_simplified_table():
    frame = pd.DataFrame(
        [
            {
                'currency': 'USD',
                'substrategy': 'Same strategy',
                'year': 2027,
                'unit_quantity': 'MMBtu',
                'qty_value': 100.25,
                'qty_intrinsic_value': 80.0,
                'qty_time_value': 20.25,
                'qty_premium': -10.0,
                'qty_pnl': 90.25,
                'ytd_hedging': None,
            }
        ]
    )
    row_data = valuation._clean_valuation_records(frame)
    column_defs = valuation.build_valuation_column_defs(frame)

    download = valuation.export_pnl_table(
        1,
        '2026-07-27',
        row_data,
        column_defs,
        [],
    )
    workbook = load_workbook(
        BytesIO(base64.b64decode(download['content'])),
        data_only=False,
    )
    worksheet = workbook['P&L and Option Values']
    headers = [cell.value for cell in worksheet[1]]

    assert headers[:4] == ['Currency', 'Strategy', 'Year', 'Unit']
    assert {'Price', 'Intrinsic Value', 'Time Value'}.isdisjoint(headers)
    assert worksheet['E2'].value == 100.25
    assert worksheet['E2'].number_format == '#,##0'


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
