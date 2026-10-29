import pandas as pd
import pytest
from openpyxl import Workbook

from dash_options.features.portfolio import greeks, valuation






def test_greeks_reads_only_the_current_published_valuation_view():
    assert greeks.VALUATION_TABLE.endswith(
        ".trades_options_valuation_current"
    )






def test_valuation_comparison_records_include_official_shadow_differences():
    frame = pd.DataFrame(
        [
            {
                "volatility_used": 0.851156,
                "comparison_volatility_used": 0.855249,
                "price": 7.2306,
                "comparison_price": 7.269,
                "qty_pnl": -31245,
                "comparison_qty_pnl": -28384,
            }
        ]
    )
    record = valuation._build_ttf_comparison_records(frame)[0]
    assert record["vol_difference_pp"] == pytest.approx(0.4093)
    assert record["price_difference"] == pytest.approx(0.0384)
    assert record["qty_pnl_difference"] == 2861


def test_valuation_monetary_totals_display_as_whole_numbers():
    assert (
        valuation._format_valuation_display_value('qty_pnl', 1234.5)
        == '1,234'
    )
    pnl_columns = {
        definition['field']: definition
        for definition in valuation.TTF_COMPARISON_COLUMN_DEFS
        if definition.get('field') in {'qty_pnl', 'comparison_qty_pnl'}
    }
    for definition in pnl_columns.values():
        formatter = definition['valueFormatter']['function']
        assert "d3.format(',.2f')" in formatter


def test_valuation_workbook_monetary_columns_show_two_decimal_places():
    workbook = Workbook()
    worksheet = workbook.active
    frame = pd.DataFrame(
        [{'Total P&L': 1234.5, 'Strategy': 'Example'}]
    )
    worksheet.append(frame.columns.tolist())
    worksheet.append(frame.iloc[0].tolist())

    valuation._apply_excel_number_formats(
        worksheet,
        frame,
        {'Total P&L': '#,##0.00'},
    )

    assert worksheet['A2'].value == 1234.5
    assert worksheet['A2'].number_format == '#,##0.00'


def test_retired_smile_delta_is_not_exposed_in_ttf_comparison_grid():
    fields = {
        definition.get("field")
        for definition in valuation.TTF_COMPARISON_COLUMN_DEFS
    }

    assert "smile_call_delta_used" not in fields
    assert "comparison_call_delta_used" in fields
