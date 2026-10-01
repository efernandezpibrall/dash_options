"""History layout for the Vol Trades workspace."""

from __future__ import annotations

from vol_trades_workspace import chart_data, grids, history_cards, history_charts

import dash_ag_grid as dag
from dash import (
    dcc,
    html,
)
from vol_calibration.feature_flags import inline_calibration_enabled
import vol_trades_data as market_data


def _build_market_snapshot():
    return html.Div(
        [
            html.Label("Market snapshot", htmlFor="brent-vol-history-date"),
            dcc.Dropdown(
                id="brent-vol-history-date",
                options=[],
                value=None,
                clearable=False,
                placeholder="No complete snapshots",
            ),
        ],
        className="brent-vol-history-toolbar-control brent-vol-history-date-control",
    )


def _build_bloomberg_controls():
    return html.Div(
        [
            html.Div(
                [
                    html.Span("Bloomberg", className="brent-vol-history-toolbar-label"),
                    html.Div(
                        id="brent-vol-history-refresh-status",
                        className="brent-vol-history-refresh-status",
                        role="status",
                        **{"aria-live": "polite"},
                    ),
                ],
                className="brent-vol-history-refresh-heading",
            ),
            html.Div(
                [
                    html.Button(
                        [html.Span("↻", className="brent-vol-history-refresh-icon", **{"aria-hidden": "true"}), "Refresh Intraday"],
                        id="brent-vol-history-refresh-button",
                        n_clicks=0,
                        disabled=True,
                        className="brent-vol-history-refresh-button brent-vol-history-refresh-button-primary",
                        title="Refresh today's Bloomberg intraday snapshot",
                        style={"display": "none"},
                    ),
                    html.Button(
                        [html.Span("↻", className="brent-vol-history-refresh-icon", **{"aria-hidden": "true"}), "Refresh settlements"],
                        id="brent-vol-history-settlement-refresh-button",
                        n_clicks=0,
                        disabled=True,
                        className="brent-vol-history-refresh-button brent-vol-history-refresh-button-secondary",
                        title="Load missing or incomplete Bloomberg settlements",
                        style={"display": "none"},
                    ),
                ],
                className="brent-vol-history-refresh-row",
            ),
        ],
        className="brent-vol-history-toolbar-control brent-vol-history-refresh-control",
    )


def _build_market_window(calibration_control):
    return html.Div(
        [
            html.Div(
                [
                    html.Label("Market window", htmlFor="brent-vol-history-trade-start"),
                    html.Small(id="brent-vol-history-market-window-status", role="status"),
                ],
                className="brent-vol-history-window-heading",
            ),
            html.Div(
                [
                    html.Div(
                        dcc.Slider(
                            id="brent-vol-history-trade-start",
                            min=0,
                            max=1,
                            value=0,
                            marks={0: "00:00", 1: "Latest"},
                            step=60,
                            disabled=True,
                            updatemode="mouseup",
                            allow_direct_input=False,
                            persistence=True,
                            persistence_type="session",
                        ),
                        id="brent-vol-history-trade-slider-track",
                        className="brent-vol-history-trade-slider-wrap",
                    ),
                    html.Div(
                        [
                            html.Button(
                                label,
                                id=f"brent-vol-history-trade-{preset}",
                                n_clicks=0,
                                disabled=True,
                                **{"aria-pressed": "false"},
                            )
                            for preset, label in (
                                ("all", "All day"), ("4h", "4h"), ("1h", "1h"), ("15m", "15m")
                            )
                        ],
                        className="brent-vol-history-trade-presets",
                        role="group",
                        **{"aria-label": "Market window presets"},
                    ),
                    html.Div(
                        [
                            calibration_control,
                            html.Div(
                                id="brent-vol-history-market-data-status",
                                className="brent-vol-history-market-data-status",
                            ),
                        ],
                        className="brent-vol-history-calibration-status",
                    ),
                ],
                className="brent-vol-history-window-controls",
            ),
        ],
        className="brent-vol-history-trade-slider-control",
    )


def build_layout(ice_quotes_layout):
    return html.Main(
        [
            html.H1(
                "Vol trades",
                className="brent-vol-history-visually-hidden-heading",
            ),
            html.Header(
                [
                    html.Div(
                        [
                            html.Fieldset(
                                [
                                    html.Legend("Product"),
                                    dcc.RadioItems(
                                        id="brent-vol-history-product",
                                        options=[
                                            {"label": "Brent", "value": "BRENT"},
                                            {"label": "TFO", "value": "TFO"},
                                            {"label": "HH · ON", "value": "ON"},
                                            {"label": "HH · LNE", "value": "LNE"},
                                            {"label": "JKM", "value": "JKM"},
                                        ],
                                        value=market_data.PRODUCT,
                                        inline=True,
                                        persistence=True,
                                        persistence_type="session",
                                        className="brent-vol-history-product-options",
                                    ),
                                ],
                                className=(
                                    "brent-vol-history-toolbar-control "
                                    "brent-vol-history-product-control"
                                ),
                            ),
                            html.Fieldset(
                                [
                                    html.Legend("X axis"),
                                    dcc.RadioItems(
                                        id="brent-vol-history-x-axis",
                                        options=[
                                            {"label": "Strike", "value": chart_data.X_AXIS_STRIKE},
                                            {"label": "Delta", "value": chart_data.X_AXIS_DELTA},
                                        ],
                                        value=chart_data.X_AXIS_STRIKE,
                                        inline=True,
                                        persistence=True,
                                        persistence_type="session",
                                        className="brent-vol-history-axis-options",
                                    ),
                                ],
                                className=(
                                    "brent-vol-history-toolbar-control "
                                    "brent-vol-history-axis-control"
                                ),
                            ),
                            _build_market_snapshot(),
                            _build_bloomberg_controls(),
                            _build_market_window(
                                html.Div(
                                    html.Button(
                                        "Calibrate surface",
                                        id="brent-vol-history-calibration-toggle",
                                        n_clicks=0,
                                        disabled=not inline_calibration_enabled(),
                                        className="brent-vol-history-calibration-toggle",
                                        title=(
                                            "Open the calibration controls for this immutable market context"
                                            if inline_calibration_enabled()
                                            else "Inline calibration is disabled by configuration"
                                        ),
                                        **{"aria-expanded": "false"},
                                    ),
                                    className=(
                                        "brent-vol-history-toolbar-control "
                                        "brent-vol-history-calibration-control"
                                    ),
                                    style=(
                                        None
                                        if inline_calibration_enabled()
                                        else {"display": "none"}
                                    ),
                                )
                            ),
                        ],
                        className="brent-vol-history-toolbar",
                    ),
                ],
                className=(
                    "professional-section-header "
                    "brent-vol-history-sticky-filter-bar"
                ),
            ),
            dcc.Store(id="brent-vol-history-snapshot"),
            dcc.Store(id="brent-vol-history-source-status-mount", data=True),
            dcc.Store(id="brent-vol-history-trade-window-state", storage_type="session"),
            dcc.Store(id="brent-vol-history-ice-overlay-revisions"),
            dcc.Store(id="brent-vol-history-expiry-layer-manifest"),
            dcc.Store(id="brent-vol-history-refresh-job", storage_type="session"),
            dcc.Store(id="brent-vol-history-refresh-completion", storage_type="session"),
            dcc.Store(
                id="brent-vol-history-calibration-open",
                storage_type="memory",
                data={"open": False},
            ),
            dcc.Store(
                id="brent-vol-history-calibration-context",
                storage_type="memory",
            ),
            dcc.Store(id="vol-trades-publication-revision", storage_type="memory"),
            dcc.Store(id="vol-trades-source-revision"),
            dcc.Interval(id="vol-trades-source-poll", interval=30000, n_intervals=0),
            html.Div(
                id="brent-vol-history-calibration-panel",
                className="brent-vol-history-calibration-panel",
                **{"aria-live": "polite"},
            ),
            dcc.Interval(
                id="brent-vol-history-refresh-poll",
                interval=1000,
                n_intervals=0,
                disabled=True,
            ),
            dcc.Interval(
                id="brent-vol-history-worker-poll",
                interval=10000,
                n_intervals=0,
                disabled=False,
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.H2(
                                "Expiry panels",
                                className=(
                                    "section-title-inline greeks-monitor-title "
                                    "brent-vol-history-section-title"
                                ),
                            ),
                            history_charts.build_expiry_legend(),
                            html.Div(
                                id="ice-chat-overlay-status", role="status",
                                className="brent-vol-history-ice-overlay-status",
                            ),
                        ],
                        className=(
                            "inline-section-header supply-dest-section-header "
                            "greeks-monitor-section-header "
                            "brent-vol-history-expiry-section-header"
                        ),
                    ),
                    html.Div(id="vol-trades-provenance", role="status", **{"aria-live": "polite"}),
                    html.Div(
                        dcc.Checklist(id="vol-trades-icap-prior", value=[],
                                      options=[{"label": "Compare prior ICAP date (if selected date is unavailable)",
                                                "value": "allow"}]),
                        id="vol-trades-icap-prior-control", style={"display": "none"},
                    ),
                    dcc.Loading(
                        type="circle",
                        children=html.Div(
                            id="brent-vol-history-plots",
                            className="brent-vol-history-plot-grid",
                        ),
                    ),
                ],
                className=(
                    "main-section-container supply-dest-section greeks-monitor-section "
                    "brent-vol-history-section brent-vol-history-expiry-section"
                ),
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H2(
                                        "Exact trade tape",
                                        className="brent-vol-history-table-title",
                                    ),
                                    html.P(
                                        (
                                            "Time-ordered prints for the selected expiry "
                                            "and trade window."
                                        ),
                                        className="brent-vol-history-table-subtitle",
                                    ),
                                ],
                                className="brent-vol-history-table-heading",
                            ),
                            html.Span(
                                [
                                    html.Span(
                                        className=(
                                            "brent-vol-history-table-context-dot"
                                        ),
                                        **{"aria-hidden": "true"},
                                    ),
                                    "Latest print only",
                                ],
                                className="brent-vol-history-table-context",
                            ),
                        ],
                        className="brent-vol-history-table-header",
                    ),
                    html.Div(id="brent-vol-history-trade-source-note"),
                    dag.AgGrid(
                        id="brent-vol-history-trade-grid",
                        rowData=[],
                        columnDefs=grids.TRADE_TAPE_COLUMN_DEFS,
                        eventListeners={
                            "modelUpdated": [
                                (
                                    "params.api.setGridAriaProperty('label', "
                                    "'Bloomberg exact option trade tape')"
                                )
                            ],
                        },
                        defaultColDef={
                            "sortable": True,
                            "filter": True,
                            "resizable": True,
                            "suppressHeaderFilterButton": True,
                        },
                        dashGridOptions={
                            "rowHeight": 30,
                            "headerHeight": 34,
                            "groupHeaderHeight": 28,
                            "pagination": True,
                            "paginationPageSize": 50,
                            "enableCellTextSelection": True,
                            "animateRows": False,
                            "ensureDomOrder": True,
                            "tooltipShowDelay": 250,
                            "overlayNoRowsTemplate": (
                                "<span>No exact trades in this expiry and trade "
                                "window.</span>"
                            ),
                            "getRowId": {"function": "params.data.event_id"},
                            "ariaLabel": "Bloomberg exact option trade tape",
                        },
                        className=(
                            "ag-theme-alpine mckinsey-ag-grid brent-vol-history-grid "
                            "brent-vol-history-table-grid vol-trades-trade-grid"
                        ),
                        style={"width": "100%", "height": "560px"},
                        dangerously_allow_code=True,
                    ),
                ],
                className=(
                    "brent-vol-history-section brent-vol-history-table-section "
                    "brent-vol-history-trade-table-section"
                ),
            ),
            ice_quotes_layout,
            html.Section(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H2(
                                        "Option-chain detail",
                                        className="brent-vol-history-table-title",
                                    ),
                                    html.P(
                                        (
                                            "Premium, volatility and activity by strike. "
                                            "Expand grouped headers for diagnostics."
                                        ),
                                        className="brent-vol-history-table-subtitle",
                                    ),
                                ],
                                className="brent-vol-history-table-heading",
                            ),
                            html.Div(
                                [
                                    html.Label("Detail expiry", htmlFor="brent-vol-history-detail-expiry"),
                                    dcc.Dropdown(
                                        id="brent-vol-history-detail-expiry",
                                        options=[],
                                        value=None,
                                        clearable=False,
                                    ),
                                ],
                                className="brent-vol-history-detail-control",
                            ),
                        ],
                        className=(
                            "brent-vol-history-detail-header "
                            "brent-vol-history-table-header"
                        ),
                    ),
                    html.Div(id="brent-vol-history-chain-source-note"),
                    dag.AgGrid(
                        id="brent-vol-history-grid",
                        rowData=[],
                        columnDefs=grids.DETAIL_COLUMN_DEFS,
                        eventListeners={
                            "firstDataRendered": [
                                (
                                    "params.api.setGridAriaProperty('label', "
                                    "'Bloomberg option-chain detail')"
                                )
                            ]
                        },
                        defaultColDef={
                            "sortable": True,
                            "filter": True,
                            "resizable": True,
                            "suppressHeaderMenuButton": False,
                            "suppressHeaderFilterButton": True,
                        },
                        dashGridOptions={
                            "rowHeight": 30,
                            "headerHeight": 34,
                            "groupHeaderHeight": 28,
                            "pagination": True,
                            "paginationPageSize": 50,
                            "enableCellTextSelection": True,
                            "animateRows": False,
                            "ensureDomOrder": True,
                            "tooltipShowDelay": 250,
                            "overlayNoRowsTemplate": (
                                "<span>No options are available for this expiry.</span>"
                            ),
                            "getRowId": {"function": "params.data.option_security"},
                            "ariaLabel": "Bloomberg option-chain detail",
                        },
                        className=(
                            "ag-theme-alpine mckinsey-ag-grid brent-vol-history-grid "
                            "brent-vol-history-table-grid vol-trades-chain-grid"
                        ),
                        style={"width": "100%", "height": "560px"},
                        dangerously_allow_code=True,
                    ),
                ],
                className=(
                    "brent-vol-history-section brent-vol-history-table-section "
                    "brent-vol-history-chain-table-section"
                ),
            ),
            html.Aside(
                history_cards._oi_methodology_children(market_data.PRODUCT),
                id="brent-vol-history-oi-methodology",
                className="brent-vol-history-methodology-note",
                **{
                    "aria-labelledby": "brent-vol-history-oi-methodology-title",
                    "role": "note",
                },
            ),
        ],
        className="options-dashboard-container brent-vol-history-page",
    )
