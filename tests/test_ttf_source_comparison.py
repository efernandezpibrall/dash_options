import pandas as pd
import pytest
from openpyxl import Workbook

from pages import greeks, valuation, vol_surface


def _curves():
    return pd.DataFrame(
        [
            {
                "valuation_run_id": "run-2",
                "valuation_revision": 2,
                "surface_source": "ICAP",
                "native_node_type": "call_delta",
                "native_node_value": 0.6,
                "strike": 59.0,
                "call_delta": 0.6,
                "volatility": 0.84,
                "forward_value": 58.211,
                "settlement_price": None,
                "total_volume": None,
                "open_interest": None,
                "quality_status": "ok",
                "valid_for_comparison": True,
                "source_name": "official",
                "method": "icap_call_delta_linear_fixed_point_v1",
                "day_count": "ACT/365.25",
            },
            {
                "valuation_run_id": "run-2",
                "valuation_revision": 2,
                "surface_source": "ICAP",
                "native_node_type": "call_delta",
                "native_node_value": 0.5,
                "strike": 61.0,
                "call_delta": 0.5,
                "volatility": 0.865,
                "forward_value": 58.211,
                "settlement_price": None,
                "total_volume": None,
                "open_interest": None,
                "quality_status": "ok",
                "valid_for_comparison": True,
                "source_name": "official",
                "method": "icap_call_delta_linear_fixed_point_v1",
                "day_count": "ACT/365.25",
            },
            {
                "valuation_run_id": "run-2",
                "valuation_revision": 2,
                "surface_source": "ICE",
                "native_node_type": "strike",
                "native_node_value": 60.0,
                "strike": 60.0,
                "call_delta": 0.54,
                "volatility": 0.855249,
                "forward_value": 58.211,
                "settlement_price": 7.269,
                "total_volume": 2180,
                "open_interest": 22391,
                "quality_status": "ok",
                "valid_for_comparison": True,
                "source_name": "ICE settlement",
                "method": "ice_settlement_black76_inversion_v1",
                "day_count": "ACT/365.25",
            },
        ]
    )


def _trades():
    return pd.DataFrame(
        [
            {
                "substrategy": "TTF call spread",
                "strike": 60.0,
                "volatility_used": 0.851156,
                "comparison_volatility_used": 0.855249,
            }
        ]
    )


def test_greeks_reads_only_the_current_published_valuation_view():
    assert greeks.VALUATION_TABLE.endswith(
        ".trades_options_valuation_current"
    )


def test_ttf_curve_figure_overlays_sources_trade_marks_and_difference():
    figure = vol_surface._create_ttf_source_comparison_figure(
        _curves(),
        _trades(),
    )
    names = {trace.name for trace in figure.data}
    assert {
        "ICAP",
        "ICE",
        "ICAP Trades",
        "ICE Trades",
        "ICE − ICAP",
    } <= names


def test_ttf_comparison_grid_keeps_source_and_provenance():
    records = vol_surface._ttf_comparison_records(_curves())
    assert records[0]["surface_source"] == "ICAP"
    assert records[0]["method"] == "icap_call_delta_linear_fixed_point_v1"
    assert records[-1]["settlement_price"] == 7.269


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


def test_valuation_monetary_totals_display_two_decimal_places():
    assert (
        valuation._format_valuation_display_value('qty_pnl', 1234.5)
        == '1,234.50'
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
