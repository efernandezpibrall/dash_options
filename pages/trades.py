"""Trade-level ledger for immutable published option valuation snapshots."""

from __future__ import annotations

import logging

import dash
import dash_ag_grid as dag
from dash import Input, Output, State, dcc, html, no_update

from runtime_config import get_database_engine
from trade_ledger import (
    OUTPUT_COLUMNS,
    TradeLedgerDataError,
    build_trade_workbook,
    get_available_cob_dates,
    get_substrategies,
    load_trade_snapshot,
)


logger = logging.getLogger(__name__)

ERROR_STYLE_HIDDEN = {"display": "none"}
ERROR_STYLE_VISIBLE = {"display": "block"}

DATE_FIELDS = {
    "cob_date",
    "trade_date",
    "expiration_date",
    "maturity_date_a",
    "maturity_date_b",
    "maturity_date_c",
    "discount_curve_cob_date",
}
TIMESTAMP_FIELDS = {"valuation_created_at", "valuation_published_at"}
PERCENT_FIELDS = {
    "vol_a",
    "vol_b",
    "vol_c",
    "adjusted_vol_a",
    "adjusted_vol_b",
    "adjusted_vol_c",
    "volatility_used",
}
QUANTITY_FIELDS = {"quantity"}
MONETARY_DECIMAL_PLACES = 2
QUANTITY_GREEK_DECIMAL_PLACES = 6
MODEL_GREEK_DECIMAL_PLACES = 8
FOUR_DECIMAL_FIELDS = {
    "premium",
    "price",
    "intrinsic_value",
    "time_value",
    "pnl",
    "strike",
    "asset_a_multiplier",
    "asset_a_premium",
    "asset_b_multiplier",
    "asset_b_premium",
    "asset_c_multiplier",
    "asset_c_premium",
    "price_a",
    "price_b",
    "price_c",
    "adjusted_price_a",
    "adjusted_price_b",
    "adjusted_price_c",
    "adjusted_strike",
    "time_to_expiry",
    "correlation",
    "discount_factor_to_expiry",
    "forward_price_used",
    "delta_s1",
    "delta_s2",
    "gamma_s1",
    "gamma_s2",
    "gamma_s1s2",
    "vega_sigma1",
    "vega_sigma2",
    "corr_sensitivity",
    "theta",
    "rho",
}
MODEL_GREEK_FIELDS = {
    "delta_s1",
    "delta_s2",
    "gamma_s1",
    "gamma_s2",
    "gamma_s1s2",
    "vega_sigma1",
    "vega_sigma2",
    "corr_sensitivity",
    "theta",
    "rho",
}
POSITION_RISK_FIELDS = {
    "qty_delta_asset_a",
    "qty_delta_asset_b",
    "qty_gamma_asset_a",
    "qty_gamma_asset_b",
    "qty_delta_s1",
    "qty_delta_s2",
    "qty_gamma_s1",
    "qty_gamma_s2",
    "qty_gamma_s1s2",
    "qty_vega_sigma1",
    "qty_vega_sigma2",
    "qty_corr_sensitivity",
    "qty_theta",
    "qty_rho",
}
TOTAL_VALUE_FIELDS = {
    "qty_value",
    "qty_intrinsic_value",
    "qty_time_value",
    "qty_premium",
    "qty_pnl",
}
SIGNED_FIELDS = {
    "premium",
    "price",
    "intrinsic_value",
    "time_value",
    "pnl",
    *MODEL_GREEK_FIELDS,
    *POSITION_RISK_FIELDS,
    *TOTAL_VALUE_FIELDS,
}

COLUMN_LABELS = {
    "cob_date": "COB",
    "trade_date": "Trade date",
    "entity": "Entity",
    "type_trade": "Trade type",
    "book": "Book",
    "strategy": "Strategy",
    "substrategy": "Substrategy",
    "type_option": "Option type",
    "model": "Model",
    "put_call": "C/P",
    "buy_sell": "Buy/Sell",
    "currency": "Currency",
    "premium": "Execution basis / unit",
    "expiration_date": "Expiry",
    "quantity": "Quantity",
    "unit_quantity": "Unit",
    "strike": "Strike",
    "asset_a": "Asset A",
    "asset_a_multiplier": "A multiplier",
    "asset_a_premium": "A adjustment",
    "maturity_date_type_a": "A maturity type",
    "maturity_date_a": "A maturity",
    "asset_sign_a": "A sign",
    "asset_b": "Asset B",
    "asset_b_multiplier": "B multiplier",
    "asset_b_premium": "B adjustment",
    "maturity_date_type_b": "B maturity type",
    "maturity_date_b": "B maturity",
    "asset_sign_b": "B sign",
    "asset_c": "Asset C",
    "asset_c_multiplier": "C multiplier",
    "asset_c_premium": "C adjustment",
    "maturity_date_type_c": "C maturity type",
    "maturity_date_c": "C maturity",
    "asset_sign_c": "C sign",
    "price_a": "Market price A",
    "price_b": "Market price B",
    "price_c": "Market price C",
    "vol_a": "Market vol A",
    "vol_b": "Market vol B",
    "vol_c": "Market vol C",
    "correlation": "Correlation",
    "adjusted_price_a": "Adjusted price A",
    "adjusted_price_b": "Adjusted price B",
    "adjusted_price_c": "Adjusted price C",
    "adjusted_strike": "Adjusted strike",
    "time_to_expiry": "Time to expiry",
    "adjusted_vol_a": "Adjusted vol A",
    "adjusted_vol_b": "Adjusted vol B",
    "adjusted_vol_c": "Adjusted vol C",
    "price": "Value / unit",
    "delta_s1": "Delta S1 / unit",
    "delta_s2": "Delta S2 / unit",
    "gamma_s1": "Gamma S1 / unit",
    "gamma_s2": "Gamma S2 / unit",
    "gamma_s1s2": "Cross-gamma / unit",
    "vega_sigma1": "Vega A / unit",
    "vega_sigma2": "Vega B / unit",
    "corr_sensitivity": "Correlation / unit",
    "theta": "Theta / unit",
    "rho": "Rho / unit",
    "intrinsic_value": "Intrinsic / unit",
    "time_value": "Time value / unit",
    "pnl": "P&L / unit",
    "qty_delta_asset_a": "Qty Delta asset A",
    "qty_delta_asset_b": "Qty Delta asset B",
    "qty_gamma_asset_a": "Qty Gamma asset A",
    "qty_gamma_asset_b": "Qty Gamma asset B",
    "qty_delta_s1": "Qty Delta S1",
    "qty_delta_s2": "Qty Delta S2",
    "qty_gamma_s1": "Qty Gamma S1",
    "qty_gamma_s2": "Qty Gamma S2",
    "qty_gamma_s1s2": "Qty cross-gamma",
    "qty_vega_sigma1": "Qty Vega S1",
    "qty_vega_sigma2": "Qty Vega S2",
    "qty_corr_sensitivity": "Qty correlation",
    "qty_theta": "Qty Theta",
    "qty_rho": "Qty Rho",
    "qty_value": "Total value",
    "qty_intrinsic_value": "Total intrinsic",
    "qty_time_value": "Total time value",
    "qty_premium": "Total execution basis",
    "qty_pnl": "Total P&L",
    "contract_convention_code": "Contract convention",
    "discount_curve_code": "Discount curve",
    "margin_style": "Margin style",
    "discount_curve_cob_date": "Curve COB",
    "discount_factor_to_expiry": "Discount factor",
    "pricing_model_version": "Pricing model version",
    "convention_source_url": "Convention source",
    "forward_price_used": "Forward used",
    "volatility_used": "Volatility used",
    "valuation_run_id": "Valuation run ID",
    "valuation_revision": "Revision",
    "valuation_methodology_version": "Methodology",
    "valuation_input_fingerprint": "Input fingerprint",
    "valuation_created_at": "Run created at",
    "valuation_created_by": "Run created by",
    "valuation_published_at": "Published at",
    "valuation_published_by": "Published by",
}

EXPORT_COLUMNS = [column for column in OUTPUT_COLUMNS if column != "cob_date"]


def _format_function(decimal_places: int) -> dict[str, str]:
    return {
        "function": (
            "params.value == null ? '—' : "
            f"d3.format(',.{decimal_places}f')(params.value)"
        )
    }


def _percent_format_function() -> dict[str, str]:
    return {
        "function": (
            "params.value == null ? '—' : "
            "d3.format(',.2f')(params.value * 100) + '%'"
        )
    }


def _column(
    field: str,
    *,
    width: int = 108,
    pinned: bool = False,
    sort: str | None = None,
    sort_index: int | None = None,
    tooltip: str | None = None,
) -> dict:
    definition = {
        "headerName": COLUMN_LABELS.get(field, field),
        "field": field,
        "headerTooltip": tooltip or COLUMN_LABELS.get(field, field),
        "tooltipField": field,
        "width": width,
        "minWidth": min(width, 72),
        "maxWidth": max(width + 80, 150),
        "sortable": True,
        "resizable": True,
        "filter": "agTextColumnFilter",
        "suppressMovable": pinned,
        "cellClass": "trades-text-cell",
        "headerClass": "trades-text-header",
    }
    if pinned:
        definition.update({"pinned": "left", "lockPinned": True})
    if sort:
        definition["sort"] = sort
    if sort_index is not None:
        definition["sortIndex"] = sort_index

    if field in DATE_FIELDS or field in TIMESTAMP_FIELDS:
        definition.update(
            {
                "cellDataType": "dateString",
                "filter": "agDateColumnFilter",
                "cellClass": "trades-date-cell",
            }
        )
    elif (
        field in QUANTITY_FIELDS
        or field in FOUR_DECIMAL_FIELDS
        or field in POSITION_RISK_FIELDS
        or field in TOTAL_VALUE_FIELDS
        or field == "valuation_revision"
        or field in PERCENT_FIELDS
    ):
        definition.update(
            {
                "cellDataType": "number",
                "filter": "agNumberColumnFilter",
                "cellClass": "trades-number-cell",
                "headerClass": "trades-number-header",
            }
        )
        if field in PERCENT_FIELDS:
            definition["valueFormatter"] = _percent_format_function()
        elif field == "valuation_revision":
            definition["valueFormatter"] = _format_function(0)
        elif field in MODEL_GREEK_FIELDS:
            definition["valueFormatter"] = _format_function(
                MODEL_GREEK_DECIMAL_PLACES
            )
        elif field in POSITION_RISK_FIELDS:
            definition["valueFormatter"] = _format_function(
                QUANTITY_GREEK_DECIMAL_PLACES
            )
        elif field in QUANTITY_FIELDS:
            definition["valueFormatter"] = _format_function(2)
        elif field in FOUR_DECIMAL_FIELDS:
            definition["valueFormatter"] = _format_function(4)
        else:
            definition["valueFormatter"] = _format_function(
                MONETARY_DECIMAL_PLACES
            )

    if field in SIGNED_FIELDS:
        definition["cellClassRules"] = {
            "trades-positive-cell": "Number(params.value) > 0",
            "trades-negative-cell": "Number(params.value) < 0",
            "trades-missing-cell": (
                "params.value === null || params.value === undefined || "
                "Number.isNaN(Number(params.value))"
            ),
        }
    return definition


def _group(header: str, children: list[dict], *, open_by_default: bool = True) -> dict:
    return {
        "headerName": header,
        "headerClass": "trades-column-group-header",
        "marryChildren": True,
        "openByDefault": open_by_default,
        "children": children,
    }


TRADES_COLUMN_DEFS = [
    _group(
        "Trade",
        [
            _column("trade_date", width=104, pinned=True, sort="desc", sort_index=0),
            _column("substrategy", width=230, pinned=True, sort="asc", sort_index=1),
            _column("strategy", width=142),
            _column("book", width=100),
            _column("entity", width=90),
            _column("type_trade", width=92),
            _column("model", width=116),
            _column("type_option", width=98),
            _column("buy_sell", width=82),
            _column("put_call", width=66),
            _column("expiration_date", width=104, sort="asc", sort_index=2),
        ],
    ),
    _group(
        "Position and premium",
        [
            _column("quantity", width=104),
            _column("unit_quantity", width=76),
            _column("currency", width=88),
            _column("strike", width=92),
            _column(
                "premium",
                width=142,
                tooltip="Signed booked execution-price basis per unit in the native contract currency.",
            ),
            _column(
                "qty_premium",
                width=162,
                tooltip="Quantity-weighted execution-price basis in the native contract currency; futures-style amounts are not universal upfront cashflows.",
            ),
        ],
    ),
    _group(
        "Valuation",
        [
            _column("price", width=110),
            _column("qty_value", width=122),
            _column("intrinsic_value", width=116),
            _column("time_value", width=124),
            _column("qty_intrinsic_value", width=130),
            _column("qty_time_value", width=140),
            _column("pnl", width=108),
            _column("qty_pnl", width=124),
        ],
    ),
    _group(
        "Position Greeks (original fields)",
        [
            _column("qty_delta_asset_a", width=128),
            _column("qty_delta_asset_b", width=128),
            _column("qty_gamma_asset_a", width=132),
            _column("qty_gamma_asset_b", width=132),
            _column("qty_delta_s1", width=112),
            _column("qty_delta_s2", width=112),
            _column("qty_gamma_s1", width=116),
            _column("qty_gamma_s2", width=116),
            _column("qty_gamma_s1s2", width=132),
            _column("qty_vega_sigma1", width=112),
            _column("qty_vega_sigma2", width=112),
            _column("qty_theta", width=108),
            _column("qty_corr_sensitivity", width=130),
            _column("qty_rho", width=104),
        ],
    ),
    _group(
        "Model Greeks per unit (original fields)",
        [
            _column("delta_s1", width=116),
            _column("delta_s2", width=116),
            _column("gamma_s1", width=120),
            _column("gamma_s2", width=120),
            _column("gamma_s1s2", width=132),
            _column("vega_sigma1", width=112),
            _column("vega_sigma2", width=112),
            _column("theta", width=108),
            _column("corr_sensitivity", width=136),
            _column("rho", width=104),
        ],
    ),
    _group(
        "Contract legs",
        [
            _column("asset_a", width=138),
            _column("asset_sign_a", width=70),
            _column("asset_a_multiplier", width=112),
            _column("asset_a_premium", width=108),
            _column("maturity_date_type_a", width=126),
            _column("maturity_date_a", width=104),
            _column("asset_b", width=138),
            _column("asset_sign_b", width=70),
            _column("asset_b_multiplier", width=112),
            _column("asset_b_premium", width=108),
            _column("maturity_date_type_b", width=126),
            _column("maturity_date_b", width=104),
            _column("asset_c", width=138),
            _column("asset_sign_c", width=70),
            _column("asset_c_multiplier", width=112),
            _column("asset_c_premium", width=108),
            _column("maturity_date_type_c", width=126),
            _column("maturity_date_c", width=104),
        ],
    ),
    _group(
        "Market and model inputs",
        [
            _column("price_a", width=112),
            _column("price_b", width=112),
            _column("price_c", width=112),
            _column("adjusted_price_a", width=120),
            _column("adjusted_price_b", width=120),
            _column("adjusted_price_c", width=120),
            _column("adjusted_strike", width=116),
            _column("vol_a", width=110),
            _column("vol_b", width=110),
            _column("vol_c", width=110),
            _column("adjusted_vol_a", width=116),
            _column("adjusted_vol_b", width=116),
            _column("adjusted_vol_c", width=116),
            _column("correlation", width=106),
            _column("time_to_expiry", width=118),
            _column("forward_price_used", width=118),
            _column("volatility_used", width=118),
        ],
    ),
    _group(
        "Valuation lineage",
        [
            _column("contract_convention_code", width=180),
            _column("margin_style", width=112),
            _column("discount_curve_code", width=126),
            _column("discount_curve_cob_date", width=104),
            _column("discount_factor_to_expiry", width=126),
            _column("pricing_model_version", width=210),
            _column("convention_source_url", width=250),
            _column("valuation_run_id", width=250),
            _column("valuation_revision", width=86),
            _column("valuation_methodology_version", width=220),
            _column("valuation_input_fingerprint", width=250),
            _column("valuation_created_at", width=176),
            _column("valuation_created_by", width=130),
            _column("valuation_published_at", width=176),
            _column("valuation_published_by", width=130),
        ],
    ),
]


def _build_filter_bar():
    return html.Div(
        [
            html.Div(
                [
                    html.Label("COB", htmlFor="trades-date-dropdown", className="filter-group-header"),
                    dcc.Dropdown(
                        id="trades-date-dropdown",
                        options=[],
                        value=None,
                        clearable=False,
                        className="trades-filter-dropdown trades-date-dropdown",
                    ),
                ],
                className="filter-group trades-sticky-filter-group trades-date-filter-group",
            ),
            html.Div(
                [
                    html.Label(
                        "Substrategies",
                        htmlFor="trades-strategy-dropdown",
                        className="filter-group-header",
                    ),
                    dcc.Dropdown(
                        id="trades-strategy-dropdown",
                        options=[],
                        value=[],
                        multi=True,
                        placeholder="All active substrategies",
                        className="trades-filter-dropdown trades-strategy-dropdown",
                    ),
                ],
                className="filter-group trades-sticky-filter-group trades-strategy-filter-group",
            ),
            html.Button(
                "Export",
                id="export-trades-table-btn",
                className="custom-export-btn trades-export-button",
                title="Export the grid's current filtered and sorted rows",
            ),
        ],
        className="professional-section-header trades-sticky-filter-bar",
    )


layout = html.Main(
    [
        dcc.Download(id="download-trades-table"),
        dcc.Store(id="trades-date-state", storage_type="memory"),
        dcc.Store(id="trades-strategy-state", storage_type="memory"),
        dcc.Store(id="trades-snapshot-meta", storage_type="memory"),
        html.Header(
            [
                html.H1("Trade ledger", className="trades-page-title"),
                html.P(
                    "Every active option trade leg with its booked economics and "
                    "published valuation and Greeks for the selected COB.",
                    className="trades-page-subtitle",
                ),
            ],
            className="trades-page-header",
        ),
        _build_filter_bar(),
        html.Div(
            id="trades-source-status",
            className="trades-source-status",
            role="status",
            **{"aria-live": "polite"},
        ),
        html.P(
            "Amounts are shown in each row's native contract currency. Premium is "
            "the signed booked execution-price basis; for futures-style contracts "
            "it is not described as a universal upfront cashflow. No FX conversion "
            "or cross-currency total is applied.",
            className="trades-sign-note",
        ),
        html.Section(
            [
                html.Div(
                    [
                        html.H2(
                            "Active trade legs",
                            id="trades-active-ledger-heading",
                            className="trades-section-title",
                        ),
                        html.Div(
                            id="trades-export-status",
                            className="trades-export-status",
                            role="status",
                            **{"aria-live": "polite"},
                        ),
                    ],
                    className="trades-section-header",
                ),
                dcc.Loading(
                    id="trades-loading",
                    type="circle",
                    children=[
                        html.Div(
                            id="trades-error-message",
                            className="trades-error-message",
                            style=ERROR_STYLE_HIDDEN,
                            role="alert",
                        ),
                        html.Div(
                            dag.AgGrid(
                                id="trades-table",
                                rowData=[],
                                columnDefs=TRADES_COLUMN_DEFS,
                                defaultColDef={
                                    "sortable": True,
                                    "filter": True,
                                    "resizable": True,
                                    "suppressHeaderMenuButton": False,
                                    "suppressHeaderFilterButton": False,
                                    "wrapHeaderText": False,
                                    "autoHeaderHeight": False,
                                },
                                dashGridOptions={
                                    "domLayout": "normal",
                                    "rowHeight": 28,
                                    "headerHeight": 34,
                                    "groupHeaderHeight": 30,
                                    "pagination": False,
                                    "suppressPaginationPanel": True,
                                    "enableCellTextSelection": True,
                                    "ensureDomOrder": True,
                                    "animateRows": False,
                                    "getRowId": {"function": "params.data._trade_key"},
                                    "ariaLabel": "Active option trade ledger",
                                },
                                className=(
                                    "ag-theme-alpine mckinsey-ag-grid "
                                    "supply-dest-summary-grid trades-ag-grid"
                                ),
                                style={
                                    "width": "100%",
                                    "height": "calc(100vh - 330px)",
                                    "minHeight": "420px",
                                },
                                dangerously_allow_code=True,
                            ),
                            className="trades-table-container",
                        ),
                    ],
                ),
            ],
            className="trades-section",
            **{"aria-labelledby": "trades-active-ledger-heading"},
        ),
    ],
    className="options-dashboard-container trades-page",
)


def _safe_data_error(exc: Exception, *, context: str) -> str:
    if isinstance(exc, TradeLedgerDataError):
        return str(exc)
    logger.exception("%s failed", context)
    return f"{context} failed. Check the configured published valuation source."


@dash.callback(
    Output("trades-date-dropdown", "options"),
    Output("trades-date-dropdown", "value"),
    Output("trades-date-state", "data"),
    Input("refresh-options-data", "n_clicks"),
    State("trades-date-dropdown", "value"),
)
def update_trades_date_options(n_clicks, current_date):
    del n_clicks
    try:
        dates = get_available_cob_dates(get_database_engine(required=False))
    except Exception as exc:
        message = _safe_data_error(exc, context="Trade COB discovery")
        return [], None, {"error": message}

    options = [{"label": value, "value": value} for value in dates]
    selected = current_date if current_date in dates else (dates[0] if dates else None)
    if not dates:
        return (
            [],
            None,
            {
                "error": (
                    "No published COB has complete original valuation Greeks."
                )
            },
        )
    return options, selected, {"error": ""}


@dash.callback(
    Output("trades-strategy-dropdown", "options"),
    Output("trades-strategy-dropdown", "value"),
    Output("trades-strategy-state", "data"),
    Input("trades-date-dropdown", "value"),
    Input("refresh-options-data", "n_clicks"),
    State("trades-strategy-dropdown", "value"),
)
def update_strategy_options(selected_date, n_clicks, current_strategies):
    del n_clicks
    if not selected_date:
        return [], [], {"error": ""}
    try:
        strategies = get_substrategies(
            get_database_engine(required=False),
            selected_date,
        )
    except Exception as exc:
        message = _safe_data_error(exc, context="Trade substrategy discovery")
        return [], [], {"error": message}
    options = [{"label": value, "value": value} for value in strategies]
    preserved = [
        value for value in (current_strategies or []) if value in strategies
    ]
    return options, preserved or strategies, {"error": ""}


@dash.callback(
    Output("trades-table", "rowData"),
    Output("trades-snapshot-meta", "data"),
    Output("trades-source-status", "children"),
    Output("trades-source-status", "className"),
    Output("trades-error-message", "children"),
    Output("trades-error-message", "style"),
    Input("trades-date-dropdown", "value"),
    Input("trades-strategy-dropdown", "value"),
    Input("refresh-options-data", "n_clicks"),
    Input("trades-date-state", "data"),
    Input("trades-strategy-state", "data"),
)
def update_trades_table(
    selected_date,
    selected_strategies,
    n_clicks,
    date_state,
    strategy_state,
):
    del n_clicks
    selector_error = (
        (date_state or {}).get("error")
        or (strategy_state or {}).get("error")
    )
    if selector_error:
        return (
            [],
            None,
            "Published valuation snapshot unavailable",
            "trades-source-status trades-source-status-error",
            selector_error,
            ERROR_STYLE_VISIBLE,
        )
    if not selected_date:
        return (
            [],
            None,
            "Select a published COB",
            "trades-source-status trades-source-status-warning",
            "",
            ERROR_STYLE_HIDDEN,
        )

    try:
        snapshot = load_trade_snapshot(
            get_database_engine(required=False),
            selected_date,
            selected_strategies,
        )
    except Exception as exc:
        message = _safe_data_error(exc, context="Trade snapshot load")
        return (
            [],
            None,
            "Published trade snapshot unavailable",
            "trades-source-status trades-source-status-error",
            message,
            ERROR_STYLE_VISIBLE,
        )

    metadata = snapshot.metadata()
    status = (
        f"{snapshot.row_count:,} active legs · COB {snapshot.cob_date} · "
        f"currencies {', '.join(metadata['currencies'])} · "
        f"revision {snapshot.valuation_revision} · "
        f"{snapshot.valuation_methodology_version} · "
        f"published {snapshot.valuation_published_at}"
    )
    return (
        snapshot.records(),
        metadata,
        status,
        "trades-source-status trades-source-status-success",
        "",
        ERROR_STYLE_HIDDEN,
    )


@dash.callback(
    Output("download-trades-table", "data"),
    Output("trades-export-status", "children"),
    Input("export-trades-table-btn", "n_clicks"),
    State("trades-date-dropdown", "value"),
    State("trades-strategy-dropdown", "value"),
    State("trades-table", "virtualRowData"),
    State("trades-table", "rowData"),
    State("trades-table", "filterModel"),
    State("trades-snapshot-meta", "data"),
    prevent_initial_call=True,
)
def export_trades_table(
    n_clicks,
    selected_date,
    selected_strategies,
    virtual_rows,
    row_data,
    filter_model,
    metadata,
):
    if not n_clicks:
        raise dash.exceptions.PreventUpdate
    records = virtual_rows if virtual_rows is not None else row_data
    if not selected_date or not metadata:
        return no_update, "No published snapshot is available to export."
    if not records:
        return no_update, "The current grid filter contains no rows to export."

    try:
        workbook = build_trade_workbook(
            records,
            metadata,
            columns=EXPORT_COLUMNS,
            labels=COLUMN_LABELS,
            filter_model=filter_model,
            selected_substrategies=selected_strategies,
        )
    except Exception:
        logger.exception("Trade workbook export failed")
        return no_update, "Export failed. The displayed snapshot was not changed."

    filename = f"trade_ledger_{selected_date}_{len(records)}_rows.xlsx"
    return dcc.send_bytes(workbook, filename), f"Exported {len(records):,} rows."
