"""Quote components for the Vol Trades workspace."""

from __future__ import annotations

from vol_trades_workspace import quote_charts

import dash_ag_grid as dag
import pandas as pd
from dash import dcc, html

def build_service_strip(
    service: dict, *, error: str | None, loaded_at: str,
    product: str = "BRENT", last_quote_at: str | None = None,
):
    if error:
        state = "Data unavailable"
        tone = "danger"
    elif not service.get("connection_state"):
        state = "Service not started"
        tone = "warning"
    else:
        state = str(service.get("connection_state") or "Unknown")
        tone = "success" if state == "connected" else "warning"
    mode = "Outbound" if service.get("outbound_enabled") else "Receive only"
    session = str(service.get("session_id") or "")
    session_display = f"…{session[-8:]}" if session else "—"
    age = service.get("surface_business_day_age")
    surface_key = {"TFO": "ttf_surface_cob_date", "JKM": "jkm_surface_cob_date"}.get(product, "surface_cob_date")
    surface_date = service.get(surface_key)
    surface_display = "No surface"
    if surface_date:
        surface_display = pd.Timestamp(surface_date).strftime("%d %b")
        if product == "TFO":
            surface_display += " · TTF calibrated"
        elif product == "JKM":
            surface_display += " · JKM calibrated"
        elif age is not None:
            surface_display += " · fresh" if int(age) <= 1 else f" · {age} BD old"
    heartbeat = service.get("last_heartbeat_at") or "—"
    if heartbeat != "—":
        heartbeat_time = pd.Timestamp(heartbeat)
        if heartbeat_time.tzinfo is None:
            heartbeat_time = heartbeat_time.tz_localize("UTC")
        heartbeat = heartbeat_time.tz_convert("UTC").strftime("%H:%M:%S UTC")
    last_event = last_quote_at or service.get("last_event_at") or "—"
    if last_event != "—":
        event_time = pd.Timestamp(last_event)
        if event_time.tzinfo is None:
            event_time = event_time.tz_localize("UTC")
        last_event_display = event_time.tz_convert("Asia/Dubai").strftime("%d %b %H:%M GST")
    else:
        last_event_display = "No quotes"
    items = (
        ("Connection", state),
        ("Mode", f"{str(service.get('environment') or 'APITest').upper()} · {mode}"),
        ("Surface", surface_display),
        ("Last quote", last_event_display),
    )
    diagnostics = (
        f"Session {session_display} · Queue {service.get('queue_depth', 0)} · "
        f"Heartbeat {heartbeat} · "
        f"Loaded {pd.Timestamp(loaded_at).strftime('%H:%M:%S UTC')}"
    )
    return html.Div(
        [
            html.Div(
                [html.Span(label, className="ice-chat-status-label"), html.Strong(value)],
                className="ice-chat-status-item",
            )
            for label, value in items
        ],
        className=f"ice-chat-service-strip ice-chat-service-strip-{tone}",
        role="status",
        title=diagnostics,
        **{"aria-live": "polite"},
    )


NUMBER_FORMATTER_2 = {"function": "params.value == null ? '—' : Number(params.value).toFixed(2)"}




PRICE_FORMATTER = {
    "function": "params.value == null ? '—' : Number(params.value).toFixed(params.data && params.data.price_decimals != null ? Number(params.data.price_decimals) : 2)"
}


SIZE_FORMATTER = {
    "function": "params.value == null ? '—' : Number(params.value).toLocaleString(undefined, {maximumFractionDigits: 2})"
}


ACTION_FORMATTER = {"function": "params.value == null ? 'NO EDGE' : params.value"}


QUOTE_COLUMN_DEFS = [
    {
        "headerName": "Instrument",
        "headerClass": "ice-chat-group-header ice-chat-group-instrument",
        "children": [
            {
                "headerName": "Product",
                "field": "product_label",
                "pinned": "left",
                "lockPinned": True,
                "width": 72,
                "headerClass": "ice-chat-text-header",
                "cellClass": "ice-chat-text-cell ice-chat-product-cell ice-chat-instrument-cell",
            },
            {
                "headerName": "Contract",
                "field": "contract_label",
                "pinned": "left",
                "lockPinned": True,
                "width": 94,
                "tooltipField": "edge_explanation",
                "headerClass": "ice-chat-text-header",
                "cellClass": "ice-chat-text-cell ice-chat-instrument-cell",
            },
            {
                "headerName": "Type",
                "field": "option_label",
                "pinned": "left",
                "lockPinned": True,
                "width": 80,
                "tooltipField": "option_label",
                "headerClass": "ice-chat-text-header",
                "cellClass": "ice-chat-text-cell ice-chat-instrument-cell",
            },
            {
                "headerName": "Strike",
                "field": "strike_label",
                "tooltipField": "structure_label",
                "pinned": "left",
                "lockPinned": True,
                "width": 116,
                "headerClass": "ice-chat-number-header",
                "cellClass": "ice-chat-number-cell ice-chat-instrument-cell",
            },
        ],
    },
    {
        "headerName": "Context",
        "headerClass": "ice-chat-group-header ice-chat-group-context",
        "children": [
            {
                "headerName": "Time (GST)",
                "field": "observed_display",
                "width": 112,
                "headerClass": "ice-chat-text-header",
                "cellClass": "ice-chat-text-cell ice-chat-time-cell",
            },
            {
                "headerName": "Quote unit",
                "field": "price_unit_label",
                "width": 72,
                "headerClass": "ice-chat-text-header",
                "cellClass": "ice-chat-text-cell ice-chat-unit-cell",
            },
        ],
    },
    {
        "headerName": "Market",
        "headerClass": "ice-chat-group-header ice-chat-group-market",
        "children": [
            {"headerName": "Bid qty", "field": "bid_size", "valueFormatter": SIZE_FORMATTER, "width": 56, "type": "rightAligned", "headerClass": "ice-chat-number-header ice-chat-group-start", "cellClass": "ice-chat-number-cell ice-chat-size-cell ice-chat-group-start"},
            {"headerName": "Bid", "field": "bid", "valueFormatter": PRICE_FORMATTER, "width": 56, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-market-price-cell"},
            {"headerName": "Offer", "field": "offer", "valueFormatter": PRICE_FORMATTER, "width": 58, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-market-price-cell"},
            {"headerName": "Offer qty", "field": "offer_size", "valueFormatter": SIZE_FORMATTER, "width": 64, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-size-cell"},
            {"headerName": "Trade", "field": "single_price", "valueFormatter": PRICE_FORMATTER, "width": 58, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-market-price-cell"},
            {"headerName": "Qty", "field": "single_size", "valueFormatter": SIZE_FORMATTER, "width": 48, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-size-cell"},
            {"headerName": "Indication*", "field": "broker_indication_display", "width": 106, "headerClass": "ice-chat-text-header", "cellClass": "ice-chat-text-cell ice-chat-market-price-cell"},
        ],
    },
    {
        "headerName": "Market IV (%)",
        "headerClass": "ice-chat-group-header ice-chat-group-iv",
        "children": [
            {"headerName": "Bid", "field": "bid_iv_pct", "valueFormatter": NUMBER_FORMATTER_2, "width": 56, "type": "rightAligned", "headerClass": "ice-chat-number-header ice-chat-group-start", "cellClass": "ice-chat-number-cell ice-chat-group-start"},
            {"headerName": "Offer", "field": "offer_iv_pct", "valueFormatter": NUMBER_FORMATTER_2, "width": 58, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell"},
            {"headerName": "Trade", "field": "single_iv_pct", "valueFormatter": NUMBER_FORMATTER_2, "width": 58, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell"},
        ],
    },
    {
        "headerName": "Our valuation",
        "headerClass": "ice-chat-group-header ice-chat-group-valuation",
        "children": [
            {"headerName": "Theo", "field": "theoretical_display", "width": 94, "headerClass": "ice-chat-number-header ice-chat-group-start", "cellClass": "ice-chat-number-cell ice-chat-theo-cell ice-chat-group-start"},
            {"headerName": "Reason", "field": "display_error", "tooltipField": "display_error", "width": 182, "headerClass": "ice-chat-text-header", "cellClass": "ice-chat-text-cell"},
            {"headerName": "Ind. gap*", "field": "broker_indication_gap", "valueFormatter": PRICE_FORMATTER, "width": 74, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell"},
            {"headerName": "IV %", "field": "our_iv_pct", "valueFormatter": NUMBER_FORMATTER_2, "width": 52, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-our-iv-cell"},
        ],
    },
    {
        "headerName": "Edge",
        "headerClass": "ice-chat-group-header ice-chat-group-edge",
        "children": [
            {
                "headerName": "Action",
                "field": "signal_label",
                "valueFormatter": ACTION_FORMATTER,
                "width": 66,
                "headerClass": "ice-chat-text-header ice-chat-group-start",
                "cellClass": "ice-chat-action-cell ice-chat-group-start",
                "cellClassRules": {
                    "ice-chat-cell-buy": "params.value === 'BUY' && params.data.edge_confidence === 'qualified'",
                    "ice-chat-cell-sell": "params.value === 'SELL' && params.data.edge_confidence === 'qualified'",
                    "ice-chat-edge-provisional": "params.data.edge_confidence === 'provisional' || params.data.edge_confidence === 'side_unassigned' || params.data.edge_confidence === 'within_buffer'",
                    "ice-chat-cell-neutral": "params.value === 'NO EDGE' || params.value === 'TRADE' || params.value === 'UNVERIFIED'",
                },
            },
            {"headerName": "Gross", "field": "signal_gross_edge", "valueFormatter": PRICE_FORMATTER, "width": 62, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell"},
            {"headerName": "Net", "field": "signal_price_edge", "valueFormatter": PRICE_FORMATTER, "width": 62, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-edge-metric", "cellClassRules": {"ice-chat-edge-positive": "Number(params.value) > 0 && params.data.edge_confidence === 'qualified'", "ice-chat-edge-provisional": "Number(params.value) > 0 && params.data.edge_confidence !== 'qualified'", "ice-chat-edge-negative": "Number(params.value) < 0", "ice-chat-edge-flat": "params.value != null && Number(params.value) === 0"}},
            {"headerName": "Vol pts", "field": "signal_iv_edge_pp", "valueFormatter": NUMBER_FORMATTER_2, "width": 64, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-edge-metric", "cellClassRules": {"ice-chat-edge-positive": "Number(params.value) > 0", "ice-chat-edge-negative": "Number(params.value) < 0", "ice-chat-edge-flat": "Number(params.value) === 0"}},
        ],
    },
    {
        "headerName": "Workflow",
        "headerClass": "ice-chat-group-header ice-chat-group-workflow",
        "children": [
            {"headerName": "Sender", "field": "sender_handle", "width": 110, "headerClass": "ice-chat-text-header ice-chat-group-start", "cellClass": "ice-chat-text-cell ice-chat-group-start"},
            {"headerName": "Delivery", "field": "outbound_status", "width": 108, "headerClass": "ice-chat-text-header", "cellClass": "ice-chat-text-cell", "cellClassRules": {"ice-chat-cell-delivered": "params.value === 'acknowledged'", "ice-chat-cell-delivery-error": "params.value === 'explicit_failure' || params.value === 'ambiguous_timeout'"}},
        ],
    },
    {
        "headerName": "Reference",
        "headerClass": "ice-chat-group-header ice-chat-group-reference",
        "children": [
            {"headerName": "Forward", "field": "forward", "valueFormatter": PRICE_FORMATTER, "width": 68, "type": "rightAligned", "headerClass": "ice-chat-number-header ice-chat-group-start", "cellClass": "ice-chat-number-cell ice-chat-group-start"},
            {"headerName": "Forward source", "field": "forward_source", "width": 132, "headerClass": "ice-chat-text-header", "cellClass": "ice-chat-text-cell"},
            {"headerName": "Surface COB", "field": "surface_cob_date", "width": 90, "headerClass": "ice-chat-text-header", "cellClass": "ice-chat-text-cell"},
        ],
    },
]


def build_layout():
    return html.Section(
        [
            html.Header(
                [
                    html.Div(
                        [
                            html.H2("ICE quotes", id="ice-chat-section-title"),
                        ]
                    ),
                    html.Div(id="ice-chat-service-status"),
                ],
                className="ice-chat-page-header",
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Label("Window", htmlFor="ice-chat-window"),
                            dcc.RadioItems(
                                id="ice-chat-window",
                                options=[
                                    {"label": "2h", "value": "2h"},
                                    {"label": "8h", "value": "8h"},
                                    {"label": "Today", "value": "today"},
                                    {"label": "7d", "value": "7d"},
                                ],
                                value="today",
                                inline=True,
                                className="ice-chat-window-control",
                            ),
                        ],
                        className="ice-chat-filter-group ice-chat-filter-window",
                    ),
                    *[
                        html.Div(
                            [html.Label(label, htmlFor=component_id), dcc.Dropdown(id=component_id, options=[], value=None, clearable=True)],
                            className="ice-chat-filter-group",
                        )
                        for label, component_id in (
                            ("Contract", "ice-chat-contract"),
                            ("Type", "ice-chat-option-type"),
                            ("Strike", "ice-chat-strike"),
                        )
                    ],
                    html.Div(
                        dcc.Checklist(
                            id="ice-chat-positive-only",
                            options=[{"label": "Fresh qualified edge", "value": "positive"}],
                            value=[],
                        ),
                        className="ice-chat-filter-group ice-chat-positive-filter",
                    ),
                    html.Div(
                        [
                            dcc.Dropdown(id=component_id, options=[], value=None)
                            for component_id in (
                                "ice-chat-sender",
                                "ice-chat-source-channel",
                                "ice-chat-status-filter",
                            )
                        ],
                        hidden=True,
                    ),
                ],
                className="ice-chat-filter-bar",
                **{"aria-label": "ICE Chat quote filters"},
            ),
            dcc.Store(id="ice-chat-quote-snapshot"),
            dcc.Store(id="ice-chat-service-snapshot"),
            dcc.Interval(id="ice-chat-refresh-interval", interval=10_000, n_intervals=0),
            html.Div(id="ice-chat-signal-summary", hidden=True),
            html.Section(
                [
                    html.Div(
                        [
                            html.H3(
                                "Broker quotes vs our mark",
                                id="ice-chat-chart-title",
                            ),
                            html.Span(
                                "Vol differences compare marks · premium edge and confidence are shown in the tape",
                                id="ice-chat-chart-hint",
                                className="ice-chat-section-hint",
                            ),
                        ],
                        className="ice-chat-section-header",
                    ),
                    dcc.Loading(
                        html.Div(
                            dcc.Graph(
                                id="ice-chat-quote-chart",
                                figure=quote_charts._empty_figure("Waiting for ICE Chat quote data"),
                                config={
                                    "displaylogo": False,
                                    "responsive": True,
                                    "displayModeBar": "hover",
                                    "modeBarButtonsToRemove": ["select2d", "lasso2d"],
                                },
                            ),
                            className="ice-chat-chart-shell",
                            role="img",
                            **{"aria-label": "Broker quote volatility edge against our saved mark"},
                        ),
                        type="circle",
                    ),
                ],
                className="ice-chat-card ice-chat-edge-card",
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.H3("Quote tape"),
                            html.Div(id="ice-chat-table-status", role="status", **{"aria-live": "polite"}),
                        ],
                        className="ice-chat-section-header",
                    ),
                    html.Small(
                        "* Fence prices are positive amounts. To-call or to-put is inferred from the calibrated "
                        "leg values; it is not a broker-confirmed trade side. Indication gap is not executable edge.",
                        className="ice-chat-indication-note",
                    ),
                    html.Div(
                        dag.AgGrid(
                            id="ice-chat-quote-grid",
                            rowData=[],
                            columnDefs=QUOTE_COLUMN_DEFS,
                            defaultColDef={
                                "sortable": True,
                                "filter": False,
                                "resizable": True,
                                "suppressHeaderMenuButton": True,
                                "suppressHeaderFilterButton": True,
                            },
                            dashGridOptions={
                                "rowHeight": 30,
                                "headerHeight": 34,
                                "groupHeaderHeight": 28,
                                "pagination": False,
                                "suppressPaginationPanel": True,
                                "rowSelection": {
                                    "mode": "singleRow",
                                    "enableClickSelection": True,
                                    "checkboxes": False,
                                },
                                "enableCellTextSelection": True,
                                "ensureDomOrder": True,
                                "suppressMovableColumns": True,
                                "animateRows": False,
                                "getRowId": {"function": "params.data.event_id"},
                                "rowClassRules": {
                                    "ice-chat-row-error": "params.data.normalization_status === 'rejected' || params.data.valuation_status === 'failed' || params.data.outbound_status === 'explicit_failure'",
                                    "ice-chat-row-blocked": "params.data.valuation_status === 'blocked'",
                                },
                                "ariaLabel": "ICE Chat option quote tape",
                            },
                            className=(
                                "ag-theme-alpine mckinsey-ag-grid "
                                "ice-chat-quote-grid"
                            ),
                            style={"height": "180px"},
                            dangerously_allow_code=True,
                        ),
                        className="ice-chat-grid-shell",
                    ),
                    html.Details(
                        [html.Summary("Edge assessment & reply delivery"), html.Div(
                            "Select a quote to inspect the three edge checks, sent message and acknowledgment.",
                            id="ice-chat-reply-audit", role="status",
                        )],
                        id="ice-chat-reply-audit-details", className="ice-chat-reply-audit", open=False,
                    ),
                ],
                className="ice-chat-card ice-chat-table-card",
            ),
        ],
        id="ice-quotes",
        className="ice-chat-quotes-page",
    )


def _edge_audit(assessment, context, valuation=None):
    if not assessment:
        return [html.P("Edge assessment not recorded for this earlier valuation.")]
    convention, costs, confidence = (assessment[key] for key in
        ("check_1_convention", "check_2_net_edge", "check_3_confidence"))
    unit = f"{assessment['currency']}/{assessment['unit']}"
    def amount(value):
        return f"{value:+.6f}" if value is not None else "unassigned"
    status = confidence["status"].replace("_", " ")
    reported_trade = (valuation is not None and valuation.get("single_price") is not None
                      and valuation.get("bid") is None and valuation.get("offer") is None)
    if reported_trade:
        status = "TRADE; no actionable bid/offer"
    tone = "qualified" if confidence["qualified"] else "provisional" if confidence["reasons"] else "neutral"
    if reported_trade:
        tone = "neutral"
    rows = [html.Tr([html.Td(side), html.Td(amount(costs['gross'][side])),
                    html.Td(amount(costs['total_cost'])), html.Td(amount(costs['net'][side]))])
            for side in ("BUY", "SELL")]
    children = [
        html.P([html.Strong("1 · Convention: "),
                f"{'verified' if convention['verified'] else 'unconfirmed'}; {convention['quotation']}; "
                f"{'submitted' if convention['orientation'] == 1 else 'reversed'} option legs. {convention['source']}" ]),
        html.P([html.Strong("2 · Gross / net: "),
                f"{unit}; buffer {costs['buffer']:.3f} ({costs['buffer_ticks']:g} tick). Spread included in bid/offer edges."]),
        html.Div(html.Table([html.Thead(html.Tr([html.Th(label) for label in ("Side", "Gross", "Est. costs", "Net")])),
                            html.Tbody(rows)]), className="ice-chat-edge-audit-table"),
        html.Small(f"Option commission {costs['option_commission_per_leg']} per leg × {costs['option_units']:g}; "
                   f"hedge execution cost {costs['hedge_execution_cost_per_unit']} per unit × "
                   f"{costs['hedge_units'] if costs['hedge_units'] is not None else 'ratio unassigned'}. {costs['costs_source']}"),
        html.P([html.Strong("3 · Confidence: "), html.Span(status, className=f"ice-chat-edge-{tone}"),
                "; " + ("; ".join(confidence['reasons']) or "all checks passed at assessment")]),
        html.Small(f"Assessed {pd.Timestamp(assessment['assessed_at']).tz_convert('Asia/Dubai').strftime('%d %b %H:%M:%S GST')}; "
                   f"surface age at quote receipt {confidence['surface_age_business_days']} business days (limit {confidence['max_surface_age_business_days']:g}); "
                   f"quote age {confidence['quote_age_seconds']:.0f}s (limit {confidence['max_quote_age_seconds']:g}s); policy {assessment['policy_version']}.")
    ]
    if valuation and valuation.get("surface_cob_date"):
        children.insert(0, html.P(f"Quote valuation surface COB {valuation['surface_cob_date']}; "
            f"{valuation['pricing_model']}; publication {str(valuation['surface_publication_id'])[:8]}. "
            "Historical reviews retain the inputs available at original receipt."))
    if context:
        children.insert(0, html.P(f"Confirmed correction: {context['confirmation']}. "
            f"Original {context['original_product']} {context['original_bid']}/{context['original_offer']}; "
            f"effective TTF {context['bid']:.3f}/{context['offer']:.3f} EUR/MWh. Source payload preserved."))
    return [html.Div(children, className="ice-chat-edge-audit")]
