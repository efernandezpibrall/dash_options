"""Read-only trader view of persisted ICE Chat option quote valuations."""

from __future__ import annotations

from vol_trades_workspace import quote_components, quote_filters

from datetime import datetime, timezone

import pandas as pd
from dash import Input, Output, State, callback, html, no_update
from dash.exceptions import PreventUpdate

import ice_quote_data as quote_data
import ice_surface_workbench  # noqa: F401 - registers lazy surface editor callbacks
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
            "review_ref": result.review_ref,
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
    frame = quote_filters.filter_quote_rows(quote_table_rows(snapshot), product=product)
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
    Output("ice-chat-quote-grid", "rowData"),
    Output("ice-chat-quote-grid", "style"),
    Output("ice-chat-table-status", "children"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("ice-chat-service-snapshot", "data"),
    Input("ice-chat-contract", "value"),
    Input("ice-chat-option-type", "value"),
    Input("ice-chat-strike", "value"),
    Input("ice-chat-sender", "value"),
    Input("ice-chat-source-channel", "value"),
    Input("ice-chat-status-filter", "value"),
    Input("ice-chat-positive-only", "value"),
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
    product="BRENT",
):
    snapshot = snapshot or {}
    selected_product = quote_data._selected_product(product)
    label = quote_data.PRODUCT_LABELS[selected_product]
    rows = quote_table_rows(snapshot)
    frame = quote_filters.filter_quote_rows(
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
    return (
        status_strip,
        frame.to_dict("records"),
        {"height": f"{grid_height}px"},
        table_status,
    )


@callback(
    Output("ice-chat-reply-audit", "children"),
    Output("ice-chat-reply-audit-details", "open"),
    Input("ice-chat-quote-grid", "selectedRows"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("brent-vol-history-product", "value"),
    State("ice-chat-reply-audit", "children"),
)
def render_reply_audit(selected_rows, snapshot, product="BRENT", current_children=None):
    selected = (selected_rows or [None])[0]
    # Keep the current anchor when adding quotes, so target edits survive.
    if isinstance(current_children, list) and current_children and isinstance(current_children[0], dict):
        anchor = current_children[0].get("props", {}).get("data-anchor-event")
        selected = next((row for row in selected_rows or [] if str(row.get("event_id")) == anchor), selected)
    prompt = "Select a quote to inspect its surface assessment, leg values, edge checks and reply."
    if not selected or quote_data._row_product(selected.get("product_code")) != quote_data._selected_product(product):
        return prompt, False
    visible = {str(row.get("event_id")) for row in (snapshot or {}).get("rows", [])}
    if str(selected.get("event_id")) not in visible:
        return prompt, False
    render_key=str(selected['event_id'])
    if isinstance(current_children,list) and current_children and isinstance(current_children[0],dict):
        if current_children[0].get('props',{}).get('data-render-key')==render_key:
            return no_update,no_update
    from ice_surface_components import surface_assessment_panel
    from dash import dcc
    panel=surface_assessment_panel({'event_id':render_key},context=selected)
    setattr(panel,'data-render-key',render_key);setattr(panel,'data-anchor-event',render_key)
    return [panel,html.Details([html.Summary('Original quote, valuation inputs and ICE reply'),
        dcc.Store(id='ice-chat-detail-event',data=selected),html.Div(id='ice-chat-detail-body')],
        id='ice-chat-detail-disclosure',className='ice-surface-disclosure ice-surface-audit')],True


@callback(Output('ice-chat-detail-body','children'),Input('ice-chat-detail-disclosure','n_clicks'),
    State('ice-chat-detail-event','data'),State('brent-vol-history-product','value'),State('ice-chat-detail-body','children'),prevent_initial_call=True)
def load_reply_details(clicks,selected,product,prior):
    if not clicks or not selected or isinstance(prior,list):raise PreventUpdate
    record,error=quote_data.load_quote_reply(str(selected['event_id']),product)
    if error:return error
    if not record:return 'No saved reply for this quote.'
    audit=quote_components.quote_details(selected,record)
    audit.extend(quote_components._edge_audit(record.get('edge_assessment'),record.get('quote_context'),record))
    labels={'acknowledged':'Acknowledged by ICE','pending':'Awaiting ICE acknowledgment','suppressed':'Suppressed','blocked':'Reply on hold','explicit_failure':'ICE rejected reply','ambiguous_timeout':'Delivery uncertain — acknowledgment timed out'}
    from ice_quote_tape import gst
    metadata=[html.Strong(labels.get(record.get('outbound_status'),'No reply recorded')),
        html.Span('Sent '+gst(record.get('outbound_attempted_at'),True)),
        html.Span('Acknowledged '+gst(record.get('outbound_acknowledged_at'),True))]
    if record.get('outbound_batch_reference'):metadata.append(html.Span('Ref '+str(record['outbound_batch_reference'])))
    audit.append(html.Div(metadata,className='ice-chat-reply-metadata'))
    if record.get('outbound_error_message'):audit.append(html.P(record['outbound_error_message']))
    check=(record.get('edge_assessment') or {}).get('surface_check') or {}
    legs=check.get('legs') or []
    if legs:
        audit.insert(0,html.Table([html.Thead(html.Tr([html.Th(x) for x in ['Delivery','Leg','Weight','Forward','IV %','Vega / pt']])),
            html.Tbody([html.Tr([html.Td(str(l.get('contract_month'))[:10]),html.Td(f"{l['strike']:g}{l['option_type']}"),
                html.Td(f"{l['coefficient']:+g}"),html.Td(str(l.get('forward'))),html.Td(f"{100*l['strike_volatility']:.2f}"),
                html.Td(str(l.get('signed_vega')))]) for l in legs])]))
    if record.get('original_market_text'):audit.append(html.Pre(record['original_market_text']))
    audit.append(html.Pre(record.get('outbound_message_text') or 'No retained outbound message.'))
    return audit



@callback(
    Output("ice-chat-tape-context", "children"),
    Input("brent-vol-history-product", "value"),
)
def render_tape_context(product):
    selected = quote_data._selected_product(product)
    return {"TFO": "TTF · EUR/MWh", "JKM": "JKM · USD/MMBtu", "BRENT": "Brent · USD/bbl"}.get(selected, "No quote feed")
