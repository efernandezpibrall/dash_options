"""Trader-facing presentation of persisted diagnostics; no pricing or calibration."""
from datetime import date

from dash import html
import plotly.graph_objects as go

from ice_quote_tape import amount, gst


SCENARIO_HELP = {
    "level": ("Parallel level", "Moves every strike by the same number of vol points.",
              "Check an ATM outright for the same expiry before moving the whole smile."),
    "downside_skew": ("Downside skew", "Moves the downside wing; ATM and the upside wing stay fixed. One point is measured at 80% of forward.",
                      "Compare nearby puts and put spreads; check the ATM anchor separately."),
    "upside_skew": ("Upside skew", "Moves the upside wing; ATM and the downside wing stay fixed. One point is measured at 125% of forward.",
                    "Compare nearby calls and call spreads; check the ATM anchor separately."),
    "curvature": ("Both wings", "Changes both wings while holding ATM fixed. The move grows away from forward.",
                  "Check both wings and neighbouring expiries before attributing the gap to curvature."),
}


def _month(value):
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%b %y")
    except (ValueError, TypeError):
        return str(value or "—")


def _signed(value, decimals=2):
    return "—" if value is None else f"{value:+.{decimals}f}"


def _ranges(entry):
    ranges = entry.get("ranges_pp") or []
    if not ranges:
        return "Not sensitive" if entry.get("status") == "insensitive" else "No local fit"
    return " or ".join(f"{a:+.2f} to {b:+.2f}" for a, b in ranges)


def _table(headers, rows):
    return html.Div(html.Table([html.Thead(html.Tr([html.Th(h, scope="col") for h in headers])),
                               html.Tbody(rows)]), className="ice-surface-table-wrap", tabIndex=0)


def surface_assessment_panel(diagnostic, context=None):
    context = context or {}
    if not diagnostic:
        return html.Section([html.H3("Surface assessment"), html.P("No saved surface assessment for this quote."),
            html.P("Inspect the original terms and leg values below; there is no calibration signal to act on yet.")],
            className="ice-surface-panel", **{"aria-label": "Quote surface assessment"})
    diagnostic = dict(diagnostic)
    diagnostic["display_unit"] = context.get("price_unit_label") or "premium units"
    unit = diagnostic["display_unit"]
    legs = diagnostic.get("legs") or []
    from ice_surface_workbench import surface_workbench_launcher
    children = [surface_workbench_launcher(diagnostic.get("event_id"))]
    if legs:
        rows = [html.Tr([html.Td(_month(l.get("contract_month"))), html.Td(f"{l['strike']:g}{l['option_type']}"),
            html.Td(_signed(l.get("coefficient"), 4)), html.Td(amount(l.get("forward"))),
            html.Td(amount(100*l["strike_volatility"], 2)), html.Td(_signed(l.get("signed_vega"), 5))]) for l in legs]
        children.append(html.Details([html.Summary(f"Inspect {len(legs)} saved legs · IV, forward and signed vega"),
            html.P(f"Signed vega in {unit} per vol point, including direction, ratio and delivery weight. Offset between legs can hide material wing exposure.", className="ice-surface-note"),
            _table(("Delivery", "Leg", "Signed weight", "Forward", "IV %", "Signed vega / pt"), rows)], className="ice-surface-disclosure"))
    return html.Section(children, className="ice-surface-panel", **{"aria-label": "Quote surface assessment"})


def surface_scenario_detail(diagnostic, factor):
    entry = next((f for f in (diagnostic or {}).get("factors", []) if f["factor"] == factor), None)
    if not entry:
        return html.P("Scenario unavailable for this saved quote.")
    _, explanation, next_mark = SCENARIO_HELP[factor]
    change = entry.get("adjustment_pp")
    result = "No stable local solution; do not extrapolate beyond the saved search." if change is None else "Zero change: the model already satisfies this quote." if change == 0 else f"Nearest fitted change: {change:+.2f} vol points."
    return html.Div([html.P(result, className="ice-surface-scenario-result"), html.P(explanation),
        html.P([html.Strong("Package sensitivity: "), f"{_signed(entry.get('sensitivity'), 5)} {(diagnostic or {}).get('display_unit', 'premium units')} per +1 vol point, locally."]),
        html.P([html.Strong("Discriminating mark: "), next_mark]),
        html.Small("A boundary at ±10 points may be the search limit. The fit has not passed arbitrage acceptance.")], className="ice-surface-scenario-explainer")


def surface_figure(diagnostic, month, factor):
    figure = go.Figure()
    diagnostic = diagnostic or {}
    curve = next((c for c in diagnostic.get("curves", []) if c["contract_month"] == month), None)
    if curve:
        figure.add_scatter(x=curve["strikes"], y=[v*100 for v in curve["volatilities"]], name="Saved surface", line={"color":"#2563a6", "width":2.5}, hovertemplate="Strike %{x:.2f}<br>IV %{y:.2f}%<extra>Saved surface</extra>")
        scenario = (curve.get("scenarios") or {}).get(factor)
        if scenario:
            figure.add_scatter(x=curve["strikes"], y=[v*100 for v in scenario["volatilities"]], name=f"Scenario {scenario['adjustment_pp']:+.2f} pt", line={"color":"#b87508", "width":2.5, "dash":"dash"}, hovertemplate="Strike %{x:.2f}<br>IV %{y:.2f}%<extra>Conditional scenario</extra>")
        else:
            figure.add_annotation(text="No drawable scenario for this month / factor", xref="paper", yref="paper", x=.5, y=1.02, showarrow=False)
        legs = [l for l in diagnostic.get("legs", []) if str(l["contract_month"]) == month]
        for positive, name, symbol in ((True, "Positive weight", "circle"), (False, "Negative weight", "diamond")):
            selected = [l for l in legs if (l["coefficient"] > 0) == positive]
            if selected:
                figure.add_scatter(x=[l["strike"] for l in selected], y=[100*l["strike_volatility"] for l in selected], mode="markers", name=name,
                    marker={"color":"#172b46", "size":10, "symbol":symbol},
                    text=[f"{l['strike']:g}{l['option_type']} · weight {l['coefficient']:+g}" for l in selected], hovertemplate="%{text}<br>IV %{y:.2f}%<extra></extra>")
        figure.add_vline(x=curve["forward"], line_dash="dot", line_color="#94a3b8", annotation_text=f"F {curve['forward']:.2f}", annotation_position="bottom right")
        if legs:
            low = min([curve["forward"]*.75, *[l["strike"]*.9 for l in legs]])
            high = max([curve["forward"]*1.3, *[l["strike"]*1.1 for l in legs]])
            visible = [v*100 for k,v in zip(curve["strikes"], curve["volatilities"]) if low<=k<=high]
            if scenario:
                visible.extend(v*100 for k,v in zip(curve["strikes"], scenario["volatilities"]) if low<=k<=high)
            visible.extend(100*l["strike_volatility"] for l in legs)
            figure.update_xaxes(range=[low, high])
            if visible:
                padding = max(1., (max(visible)-min(visible))*.15)
                figure.update_yaxes(range=[min(visible)-padding, max(visible)+padding])
    else:
        figure.add_annotation(text="Saved smile unavailable; inspect the original leg values", showarrow=False)
    add_broker_vol_marks(figure, (diagnostic.get("broker_vols") or {}).get("marks", []), month)
    figure.update_layout(template="plotly_white", height=365, margin={"l":50, "r":20, "t":35, "b":80},
        xaxis_title="Strike", yaxis_title="IV (%)", legend={"orientation":"h", "y":-.22, "x":0, "font":{"size":11}},
        font={"family":"Arial, sans-serif", "size":12, "color":"#334155"}, paper_bgcolor="white", plot_bgcolor="white",
        uirevision=f"{diagnostic.get('event_id')}:{month}:{factor}")
    return figure


def add_broker_vol_marks(figure, marks, month):
    """Render IVs computed by shared analytics, grouped by side."""
    styles = {"Bid": ("#15803d", "triangle-up"), "Offer": ("#b91c1c", "triangle-down"),
              "Mid": ("#7c3aed", "diamond"), "Trade": ("#0f766e", "star")}
    for side, (color, symbol) in styles.items():
        selected = [m for m in marks if m["side"] == side and m["contract_month"] == month]
        if not selected:
            continue
        figure.add_scatter(x=[m["strike"] for m in selected], y=[m["iv"]*100 for m in selected],
            mode="markers", name="Broker " + side.lower() + " IV", marker={"color":color,"symbol":symbol,"size":11},
            text=[f"{m.get('sender','')} · {gst(m.get('observed_at'),True)} · F {format(m['forward'],'.4f') if m.get('forward') is not None else 'monthly inputs'} · K {m.get('original_strike',m['strike']):g} · premium {m['premium']:.4f}" for m in selected],
            hovertemplate="%{text}<br>Strike %{x:.2f} · IV %{y:.2f}%<extra></extra>")
    # Include broker observations even when they sit outside the smile's old zoom.
    visible = [m["iv"]*100 for m in marks if m["contract_month"] == month]
    existing = figure.layout.yaxis.range
    if visible and existing:
        figure.update_yaxes(range=[min(existing[0],min(visible)-1),max(existing[1],max(visible)+1)])
    return figure
