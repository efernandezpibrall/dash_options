"""Read-only trader view of persisted ICE Chat option quote valuations."""

from __future__ import annotations

from vol_trades_workspace import quote_charts, quote_components

from datetime import datetime, timezone

import pandas as pd
from dash import Input, Output, callback, html, no_update

import ice_quote_data as quote_data
from dash_utils import triggered_id
from vol_trades_market_window import market_context, quote_table_rows


# Fixed widths keep the tape stable while allowing for the rendered header text,
# 10px dense-grid padding, and representative formatted values in each column.


layout = quote_components.build_layout()


@callback(
    Output("ice-chat-section-title", "children"),
    Output("ice-chat-contract", "value"),
    Output("ice-chat-option-type", "value"),
    Output("ice-chat-strike", "value"),
    Output("ice-chat-sender", "value"),
    Output("ice-chat-source-channel", "value"),
    Output("ice-chat-status-filter", "value"),
    Output("ice-chat-positive-only", "value"),
    Output("ice-chat-positive-only", "options"),
    Input("brent-vol-history-product", "value"),
)
def select_quote_product(product):
    label = quote_data.PRODUCT_LABELS[quote_data._selected_product(product)]
    edge_label = "Fresh qualified edge" if quote_data._selected_product(product) == "TFO" else "Positive price edge"
    return f"ICE quotes · {label}", None, None, None, None, None, None, [], [{"label": edge_label, "value": "positive"}]


@callback(
    Output("ice-chat-quote-snapshot", "data"),
    Output("ice-chat-service-snapshot", "data"),
    Input("ice-chat-refresh-interval", "n_intervals"),
    Input("ice-chat-window", "value"),
    Input("brent-vol-history-snapshot", "data"),
)
def refresh_quote_snapshot(_interval, window, history_snapshot=None):
    result = quote_data.load_quote_snapshot(window or "today", history_snapshot=history_snapshot)
    return (
        {
            "rows": result.rows,
            "error": result.error,
            "loaded_at": result.loaded_at,
            "truncated": result.truncated,
            "table_cutoff_at": quote_data._cutoff(window or "today", pd.Timestamp(result.loaded_at).to_pydatetime()).isoformat(),
            "market_context": market_context(history_snapshot, now=result.loaded_at),
        },
        result.service,
    )


@callback(
    Output("ice-chat-contract", "options"),
    Output("ice-chat-option-type", "options"),
    Output("ice-chat-strike", "options"),
    Output("ice-chat-sender", "options"),
    Output("ice-chat-source-channel", "options"),
    Output("ice-chat-status-filter", "options"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("brent-vol-history-product", "value"),
)
def update_quote_filter_options(snapshot, product="BRENT"):
    frame = quote_charts.filter_quote_rows(quote_table_rows(snapshot), product=product)
    if frame.empty:
        return [], [], [], [], [], []

    def options(column, label_column=None):
        values = frame[[column] + ([label_column] if label_column else [])].dropna()
        values = values.drop_duplicates(column).sort_values(column)
        labels = values[label_column] if label_column else values[column]
        return [
            {"value": value, "label": label}
            for value, label in zip(values[column], labels)
        ]

    return (
        options("contract_label"),
        options("option_filter_key", "option_label"),
        options("strike_filter_key", "strike_label"),
        options("sender_handle"),
        options("source_channel"),
        options("processing_status"),
    )


@callback(
    Output("ice-chat-service-status", "children"),
    Output("ice-chat-quote-chart", "figure"),
    Output("ice-chat-quote-grid", "rowData"),
    Output("ice-chat-quote-grid", "style"),
    Output("ice-chat-table-status", "children"),
    Output("ice-chat-chart-title", "children"),
    Output("ice-chat-chart-hint", "children"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("ice-chat-service-snapshot", "data"),
    Input("ice-chat-contract", "value"),
    Input("ice-chat-option-type", "value"),
    Input("ice-chat-strike", "value"),
    Input("ice-chat-sender", "value"),
    Input("ice-chat-source-channel", "value"),
    Input("ice-chat-status-filter", "value"),
    Input("ice-chat-positive-only", "value"),
    Input("ice-chat-quote-grid", "selectedRows"),
    Input("brent-vol-history-product", "value"),
)
def render_quote_dashboard(
    snapshot,
    service,
    contract,
    option_type,
    strike,
    sender,
    source_channel,
    status,
    positive_only,
    selected_rows,
    product="BRENT",
):
    snapshot = snapshot or {}
    selected_product = quote_data._selected_product(product)
    label = quote_data.PRODUCT_LABELS[selected_product]
    rows = quote_table_rows(snapshot)
    frame = quote_charts.filter_quote_rows(
        rows,
        product=selected_product,
        contract=contract,
        option_type=option_type,
        strike=strike,
        sender=sender,
        source_channel=source_channel,
        status=status,
        positive_only="positive" in (positive_only or []),
    )
    selected = (selected_rows or [None])[0]
    if selected and (
        frame.empty or str(selected.get("event_id")) not in set(frame["event_id"].astype(str))
    ):
        selected = None
    if selected:
        figure = quote_charts.build_instrument_figure(frame, selected)
        chart_title = "Instrument quote history"
        instrument = selected.get("instrument_label") or "Selected instrument"
        orientation = {"to_put": "to put", "to_call": "to call"}.get(selected.get("fence_premium_orientation"), "side unresolved")
        chart_hint = (f"{instrument} · inferred {orientation}; indication gap is non-executable; no single structure IV"
                      if selected.get("structure_code") == "CLLR" else
                      f"{instrument} · correlation not assigned; CSO valuation on hold"
                      if selected.get("structure_code") in {"CSO3", "CSO4"} else
                      f"{instrument} · signed leg valuation; no single structure IV; broker edge unverified"
                      if selected.get("structure_code") in {"CALLSPR", "PUTSPR", "CFLY", "STNGL", "STRDL", "PUT_SPREAD_VS_CALL", "DIAGONAL_CALL_SPREAD", "CALL_SPREAD_VS_PUT_SPREAD"} else
                      f"{instrument} · broker premium and IV vs our marks")
        if selected.get("edge_confidence"):
            chart_hint = f"{instrument} · {selected['signal_label']} · {selected.get('edge_explanation') or 'net edge assessed'}"
    elif selected_product not in quote_data.QUOTE_FEED_PRODUCTS:
        figure = quote_charts._empty_figure(f"ICE quote feed is unavailable for {label}")
        chart_title = "Broker quotes vs our mark"
        chart_hint = f"No ICE quote feed is configured for {label}"
    else:
        figure = quote_charts.build_all_quotes_figure(frame)
        chart_title = "Broker quotes vs our mark"
        chart_hint = "Vol differences compare marks · premium edge and confidence are shown in the tape"
    status_strip = (
        html.Div(
            f"No ICE quote feed is configured for {label}.",
            className="ice-chat-feed-unavailable",
            role="status",
        )
        if selected_product not in quote_data.QUOTE_FEED_PRODUCTS
        else quote_components.build_service_strip(
            service or {},
            error=snapshot.get("error"),
            loaded_at=snapshot.get("loaded_at") or datetime.now(timezone.utc).isoformat(),
            product=selected_product,
            last_quote_at=next(
                (
                    row.get("observed_at") for row in rows
                    if quote_data._row_product(row.get("product_code")) == selected_product
                    and row.get("observed_at")
                ),
                None,
            ),
        )
    )
    if selected_product not in quote_data.QUOTE_FEED_PRODUCTS:
        table_status = f"ICE quote feed is unavailable for {label}"
    elif snapshot.get("error"):
        table_status = snapshot["error"]
    elif frame.empty:
        table_status = "No quotes match the selected filters"
    else:
        suffix = " · first 10,000 rows" if snapshot.get("truncated") else ""
        table_status = f"{len(frame):,} quote events{suffix}"
        held_csos = frame["structure_code"].isin(["CSO3", "CSO4"]).sum()
        if held_csos:
            table_status += f" · {held_csos} CSOs: correlation not assigned"
    visible_rows = min(len(frame), 11)
    grid_height = min(420, max(180, 88 + 30 * visible_rows))
    selection_only = triggered_id() == "ice-chat-quote-grid"
    return (
        no_update if selection_only else status_strip,
        figure,
        no_update if selection_only else frame.to_dict("records"),
        no_update if selection_only else {"height": f"{grid_height}px"},
        no_update if selection_only else table_status,
        chart_title,
        chart_hint,
    )


@callback(
    Output("ice-chat-signal-summary", "children"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("ice-chat-contract", "value"),
    Input("ice-chat-option-type", "value"),
    Input("ice-chat-strike", "value"),
    Input("ice-chat-sender", "value"),
    Input("ice-chat-source-channel", "value"),
    Input("ice-chat-status-filter", "value"),
    Input("ice-chat-positive-only", "value"),
    Input("ice-chat-quote-grid", "selectedRows"),
)
def render_signal_summary(
    snapshot,
    contract,
    option_type,
    strike,
    sender,
    source_channel,
    status,
    positive_only,
    selected_rows,
):
    # Preserve the callback signature for dashboard tabs opened before this section was removed.
    return None


@callback(
    Output("ice-chat-reply-audit", "children"),
    Output("ice-chat-reply-audit-details", "open"),
    Input("ice-chat-quote-grid", "selectedRows"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("brent-vol-history-product", "value"),
)
def render_reply_audit(selected_rows, snapshot, product="BRENT"):
    selected = (selected_rows or [None])[0]
    prompt = "Select a quote to inspect the three edge checks, sent message and acknowledgment."
    if not selected or quote_data._row_product(selected.get("product_code")) != quote_data._selected_product(product):
        return prompt, False
    visible = {str(row.get("event_id")) for row in (snapshot or {}).get("rows", [])}
    if str(selected.get("event_id")) not in visible:
        return prompt, False
    record, error = quote_data.load_quote_reply(str(selected["event_id"]), product)
    if error:
        return error, True
    if not record:
        return "No reply delivery is recorded for this quote.", True
    state = record["outbound_status"]
    labels = {"acknowledged": "🟢 Acknowledged by ICE", "pending": "🟡 Awaiting ICE acknowledgment",
              "suppressed": "Suppressed", "blocked": "🟡 Reply on hold",
              "explicit_failure": "🔴 ICE rejected reply", "ambiguous_timeout": "🟡 Delivery uncertain — acknowledgment timed out"}
    def gst(value):
        return pd.Timestamp(value).tz_convert("Asia/Dubai").strftime("%d %b %H:%M:%S GST") if value else "—"
    metadata = [html.Strong(labels.get(state, "No reply recorded")),
                html.Span(f"Sent {gst(record['outbound_attempted_at'])}"),
                html.Span(f"Acknowledged {gst(record['outbound_acknowledged_at'])}")]
    if record["outbound_batch_reference"]:
        metadata.append(html.Span(f"Ref {record['outbound_batch_reference']}"))
    children = quote_components._edge_audit(record["edge_assessment"], record["quote_context"], record)
    children.append(html.Div(metadata, className="ice-chat-reply-metadata"))
    if record["outbound_error_message"]:
        children.append(html.P(record["outbound_error_message"]))
    if record["outbound_message_text"]:
        children.append(html.Pre(record["outbound_message_text"], className="ice-chat-reply-text"))
    else:
        children.append(html.P("Exact text was not retained for this earlier reply." if state
                               else "No outbound message has been sent for this quote."))
    return children, True
