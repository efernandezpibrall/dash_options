"""Shared quote tape presentation for modular and deployed page layouts."""
from dash import html

PRICE_FORMATTER = {"function": "params.value == null ? '—' : Number(params.value).toFixed(params.data.price_decimals || 3)"}

def tape_column(header, field, width, *, numeric=False, formatter=None, tooltip=None, pinned=False):
    column = {"headerName": header, "field": field, "width": width, "minWidth": width,
              "headerClass": "ice-chat-number-header" if numeric else "ice-chat-text-header",
              "cellClass": "ice-chat-number-cell" if numeric else "ice-chat-text-cell"}
    if formatter:
        column["valueFormatter"] = {"function": formatter}
    if tooltip:
        column["tooltipField"] = tooltip
    if pinned:
        column.update(pinned="left", lockPinned=True)
    return column

QUOTE_COLUMN_DEFS = [
    {"headerName": "Quote", "headerClass": "ice-chat-group-header", "children": [
        tape_column("Time GST", "observed_at", 132, formatter="params.data.time_display", tooltip="observed_display", pinned=True),
        tape_column("Contract", "contract_label", 108, tooltip="contract_label", pinned=True),
        tape_column("Strategy", "strategy_display", 184, tooltip="strategy_tooltip", pinned=True),
    ]},
    {"headerName": "Broker", "headerClass": "ice-chat-group-header", "children": [
        {**tape_column("Price", "broker_display", 138, numeric=True, tooltip="broker_tooltip"), "sortable": False},
        {**tape_column("IV %", "broker_iv_display", 112, numeric=True, tooltip="broker_iv_tooltip"), "sortable": False},
    ]},
    {"headerName": "Our valuation", "headerClass": "ice-chat-group-header", "children": [
        {**tape_column("Model", "model_sort_value", 148, numeric=True, formatter="params.data.model_display", tooltip="model_tooltip"), "cellClass": "ice-chat-number-cell ice-chat-theo-cell"},
        tape_column("IV %", "our_iv_pct", 112, numeric=True, formatter="params.data.our_iv_display", tooltip="our_iv_tooltip"),
        {**tape_column("Net edge", "signal_price_edge", 132, tooltip="edge_tooltip"), "cellRenderer": "IceTapeEdge"},
    ]},
    {"headerName": "Inputs", "headerClass": "ice-chat-group-header", "children": [
        tape_column("Forward", "forward", 112, numeric=True, formatter="params.data.forward_display", tooltip="forward_tooltip"),
        tape_column("Forward source", "forward_source_display", 124, tooltip="forward_source_tooltip"),
        tape_column("Surface", "surface_published_at", 152, formatter="params.data.surface_display", tooltip="surface_tooltip"),
    ]},
    {"headerName": "Greeks", "headerClass": "ice-chat-group-header", "children": [
        tape_column("D", "tape_delta", 132, numeric=True, formatter="params.data.delta_display", tooltip="greek_tooltip"),
        tape_column("G", "tape_gamma", 156, numeric=True, formatter="params.data.gamma_display", tooltip="greek_tooltip"),
        tape_column("V", "tape_vega", 132, numeric=True, formatter="params.data.vega_display", tooltip="greek_tooltip"),
    ]},
    tape_column("Sender", "sender_handle", 110, tooltip="sender_handle"),
    {"headerName": "Delivery", "headerClass": "ice-chat-group-header", "children": [
        tape_column("Reply", "reply_display", 100, tooltip="reply_tooltip"),
    ]},
]


# Semantic bands distinguish inputs from our marks without changing saved values.
_TAPE_BANDS = {"Quote": "identity", "Broker": "broker", "Our valuation": "valuation",
               "Inputs": "inputs", "Greeks": "greeks", "Delivery": "delivery"}
for _group in QUOTE_COLUMN_DEFS:
    _band = _TAPE_BANDS.get(_group["headerName"], "sender")
    _group["headerClass"] = _group.get("headerClass", "") + f" ice-tape-band-{_band}"
    for _index, _column in enumerate(_group.get("children", [_group])):
        _classes = f" ice-tape-band-{_band}" + (" ice-tape-band-start" if _index == 0 else "")
        _column["headerClass"] = _column.get("headerClass", "") + _classes
        _column["cellClass"] = _column.get("cellClass", "") + _classes
        if _column["field"] == "signal_price_edge":
            _column["cellClassRules"] = {
                f"ice-tape-edge-cell-{side}": f"params.data.edge_marker === '{side}'"
                for side in ("buy", "sell", "none", "unpriced")
            }
        if _column["field"] == "reply_display":
            _column["cellRenderer"] = "IceTapeDelivery"


def quote_details(selected, record):
    from ice_quote_tape import amount, gst, display_orientation
    children = []
    if record.get("original_market_text"):
        children.append(html.P([html.Strong("Original market: "), record["original_market_text"]], className="ice-chat-quote-detail-source"))
    if selected.get("pricing_reason"):
        children.append(html.P([html.Strong("Pricing: "), selected["pricing_reason"]]))
    children.append(html.P([html.Strong("Inputs: "),
        f"Surface published {gst(record.get('surface_published_at'), True)}; market date {record.get('surface_cob_date') or '—'}; "
        f"publication {record.get('surface_publication_id') or 'unrecorded'}. "
        f"Forward source {selected.get('forward_source') or '—'}; forward date {selected.get('forward_cob_date') or '—'}."]))
    components = record.get("components") or []
    if components:
        if all(part.get("premium_basis") == "unassigned" for part in components):
            children.append(html.P([html.Strong("Legs priced · basis required. "),
                "Prices and IVs are separate leg values in contract order. Greeks include each leg's direction and ratio. "
                "No combined price, IV, Greeks or broker edge is assigned until equal MWh versus equal MW/lots is confirmed."]))
        headings = ("Delivery", "Leg", "Ratio", "Hours", "Weight", "Forward", "Model IV %", "Unit premium", "D", "G", "V")
        rows = []
        for part in components:
            greeks = part.get("unit_greeks") or {}
            direction = "−" if part.get("direction", 1) * display_orientation(selected) < 0 else "+"
            strike = part.get("strike", selected.get("strike"))
            leg = direction + amount(strike, 2) + str(part.get("option_type") or selected.get("option_type") or "")
            rows.append(html.Tr([html.Td(value) for value in (
                str(part.get("contract_month") or "—"), leg,
                amount(part.get("ratio", 1), 2), amount(part.get("delivery_hours"), 0), amount(part.get("weight"), 4),
                amount(part.get("forward"), 4), amount(float(part["strike_volatility"]) * 100 if part.get("strike_volatility") is not None else None, 2),
                amount(part.get("theoretical_price"), 4), amount(greeks.get("delta"), 4),
                amount(greeks.get("gamma"), 6), amount(greeks.get("vega"), 4),
            )]))
        children.extend([html.P("Saved option legs · unit Greeks below; tape Greeks include direction, ratio and assigned delivery weights."),
            html.Div(html.Table([html.Thead(html.Tr([html.Th(h) for h in headings])), html.Tbody(rows)]), className="ice-chat-quote-detail-table")])
    sizes = [f"{label} {selected[key]}" for label, key in (("Bid qty", "bid_size"), ("Offer qty", "offer_size"), ("Trade qty", "single_size")) if selected.get(key) is not None]
    if sizes:
        children.append(html.P(" · ".join(sizes)))
    if selected.get("broker_indication_gap") is not None:
        children.append(html.P(f"Indication gap {amount(selected['broker_indication_gap'])}; comparison only, not executable edge."))
    return children
