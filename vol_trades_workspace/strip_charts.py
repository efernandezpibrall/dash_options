"""Quarter and season charts in the existing expiry-panel visual grammar."""

from html import escape
import hashlib
import json
import textwrap

import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html

from options.ttf_strip_charts import project_strip_quotes
from options.ttf_strip_settlements import project_strip_settlements
from vol_trades_ice_quotes import prepare_strip_events
from vol_trades_market_window import history_identity, utc_timestamp
from vol_trades_strip_data import load_strip_inputs, load_strip_settlement_inputs
from vol_trades_workspace import chart_data, quote_charts
from workspace_cache import WorkspaceLoadCache


_PROJECTION_CACHE = WorkspaceLoadCache(max_entries=32)
_MARKERS = {
    "bid": ("ice-bid", "#B42318", "triangle-down"),
    "offer": ("ice-offer", "#067647", "triangle-up"),
    "single": ("ice-single", "#6941C6", "diamond"),
}


def _projection(inputs, months, rows):
    # Hash actual values: same-COB forward corrections must invalidate projections.
    payload = {
        key: hashlib.sha256(pd.util.hash_pandas_object(inputs[key], index=True).to_numpy().tobytes()).hexdigest()
        for key in ("surface", "forwards")
    }
    payload["quotes"] = rows
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return _PROJECTION_CACHE.get_or_load(
        ("ttf-strip-projection", inputs["publication_id"], digest),
        lambda: project_strip_quotes(
            inputs["surface"], inputs["forwards"], months, inputs["publication_id"], inputs["cob_date"], rows
        ),
        force_refresh=False,
        degraded=lambda result: bool(result["errors"]),
        healthy_ttl_seconds=3600,
        degraded_ttl_seconds=10,
    )


def _settlement_projection(inputs, months):
    fields = [
        "snapshot_id",
        "business_date",
        "underlying_contract_month",
        "option_expiration_date",
        "put_call",
        "strike",
        "underlying_price",
        "settlement_price",
        "implied_volatility",
        "product",
        "currency",
        "price_unit",
        "snapshot_kind",
        "option_security",
        "premium_style",
        "pricing_model",
    ]
    values = inputs["chain"].reindex(columns=fields)
    digest = hashlib.sha256(pd.util.hash_pandas_object(values, index=True).to_numpy().tobytes()).hexdigest()
    return _PROJECTION_CACHE.get_or_load(
        ("ttf-strip-settlement-projection", inputs["snapshot_id"], inputs["cob_date"], tuple(months), digest),
        lambda: project_strip_settlements(inputs["chain"], months, inputs["snapshot_id"], inputs["cob_date"]),
        force_refresh=False,
        degraded=lambda result: bool(result["errors"]),
        healthy_ttl_seconds=3600,
        degraded_ttl_seconds=10,
    )


def build_strip_figure(label, months, projection, *, x_axis, selected_layers, identity, settlement=None):
    figure = go.Figure()
    delta_mode = chart_data._normalize_x_axis(x_axis) == chart_data.X_AXIS_DELTA
    selected = None if selected_layers is None else set(selected_layers)

    def visible(layer):
        return selected is None or layer in selected

    for curve in projection.get("curves", []):
        points = curve["points"]
        figure.add_trace(
            go.Scatter(
                x=[point["delta"] if delta_mode else point["strike"] for point in points],
                y=[100 * point["volatility"] for point in points],
                mode="lines",
                name="Calibrated TTF strip",
                meta={"legend_layer": "calibrated"},
                visible=visible("calibrated"),
                line={"color": "#7C3AED", "width": 2.2, "dash": "dash"},
                opacity=0.7,
                customdata=[[point["strike"], curve["forward"], curve["valuation_date"]] for point in points],
                hovertemplate=(
                    "<b>Calibrated TTF strip</b><br>Strike %{customdata[0]:.2f}<br>Equivalent strip IV %{y:.2f}%"
                    "<br>ICE strip forward %{customdata[1]:.3f} EUR/MWh<br>Valuation date %{customdata[2]}<extra></extra>"
                ),
            )
        )
    for side, (layer, color, symbol) in _MARKERS.items():
        x, y, text, symbols = [], [], [], []
        for row in projection.get("quotes", []):
            for point in row["points"]:
                if point["side"] != side:
                    continue
                x.append(point["delta"] if delta_mode else float(row["strike"]))
                y.append(100 * point["volatility"])
                symbols.append(symbol + ("-open" if row["option_type"] == "P" else ""))
                stamp = utc_timestamp(row["observed_at"]).tz_convert("Asia/Dubai").strftime("%d %b %H:%M:%S GST")
                quoted_size = pd.to_numeric(row.get({"bid": "bid_size", "offer": "offer_size", "single": "single_size"}[side]), errors="coerce")
                size_label = f"{quoted_size:,.0f}" if pd.notna(quoted_size) and quoted_size > 0 else "Size unavailable"
                text.append(
                    f"<b>ICE {side} · {escape(label)} · {'Put' if row['option_type'] == 'P' else 'Call'}</b>"
                    f"<br>{stamp} · Strike {float(row['strike']):.2f}"
                    f"<br>Premium {point['price']:.4f} EUR/MWh · Equivalent IV {100 * point['volatility']:.2f}%"
                    f"<br>Quoted size {size_label}"
                    f"<br>Our equivalent IV {100 * row['our_volatility']:.2f}%"
                    f"<br>Forward {float(row['forward']):.3f} EUR/MWh"
                    f"<br>Sender {escape(str(row.get('sender_handle') or '—'))} · {escape(str(row.get('source_channel') or '—'))}"
                    f"<br>Selected surface {escape(str(row['comparison_cob_date']))} · {escape(str(row['comparison_publication_id']))}"
                    f"<br>Quote saved surface {escape(str(row.get('surface_cob_date') or '—'))}"
                    f"<br>Event {escape(str(row.get('event_id') or '—'))}"
                )
        figure.add_trace(
            go.Scatter(
                x=x,
                y=y,
                text=text,
                mode="markers",
                name=f"ICE {side}",
                meta={"legend_layer": layer},
                visible=visible(layer),
                hovertemplate="%{text}<extra></extra>",
                marker={
                    "color": color,
                    "symbol": symbols,
                    "size": 6,
                    "opacity": 0.85,
                    "line": {
                        "color": [color if value.endswith("-open") else "#FFFFFF" for value in symbols],
                        "width": [1 if value.endswith("-open") else 0.5 for value in symbols],
                    },
                },
            )
        )
    settlement = settlement or {}
    points = settlement.get("points") or []
    if points:
        settlement_layer = settlement.get("legend_layer", "bloomberg-settlement")
        text = []
        for point in points:
            if point["volatility"] is None:
                text.append("")
                continue
            component_lines = "".join(
                f"<br>{escape(item['month'][:7])}: premium {item['premium']:.4f} · weight {item['weight']:.2%}"
                f"<br>forward {item['forward']:.3f} · expiry {escape(item['expiry'])}"
                + (" · interpolated" if item["interpolated"] else "")
                for item in point["components"]
            )
            text.append(
                f"<b>Bloomberg-derived strip settlement · {escape(label)}</b>"
                f"<br>Settlement date {escape(settlement['cob_date'])} · Strike {point['strike']:.2f}"
                f"<br>Equivalent IV {100 * point['volatility']:.2f}%"
                f"<br>Premium {point['premium']:.4f} EUR/MWh"
                f"<br>Settlement strip forward {settlement['forward']:.3f} EUR/MWh"
                f"<br>{'Includes interpolated monthly IVs' if point['interpolated'] else 'Exact monthly settlement strikes'}"
                "<br>Delivery-hour weighted monthly options"
                + component_lines
                + f"<br>Bloomberg snapshot<br>{escape(settlement['snapshot_id'])}"
            )
        figure.add_trace(
            go.Scatter(
                x=[point["delta"] if delta_mode else point["strike"] for point in points],
                y=[None if point["volatility"] is None else 100 * point["volatility"] for point in points],
                text=text,
                mode="markers+lines",
                name="Bloomberg-derived strip settlement",
                meta={
                    "legend_layer": settlement_layer,
                    "snapshot_id": settlement["snapshot_id"],
                    "cob_date": settlement["cob_date"],
                    "derived": True,
                },
                visible=visible(settlement_layer),
                connectgaps=False,
                opacity=0.65,
                line={"color": "#64748B", "width": 0.9},
                marker={"color": "#64748B", "size": 2, "symbol": "circle"},
                hovertemplate="%{text}<extra></extra>",
            )
        )
    errors = list(projection.get("errors") or []) + list(settlement.get("errors") or [])
    if errors:
        figure.add_annotation(
            text="<br>".join(escape(line) for line in textwrap.wrap(" · ".join(errors), 34)),
            x=0.5,
            y=0.98,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"size": 10, "color": "#92400E"},
            bgcolor="rgba(255,255,255,.9)",
        )
    if not projection.get("curves") and not points and not errors:
        figure.add_annotation(
            text="No comparable strip inputs", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False
        )
    figure.update_layout(
        template="plotly_white",
        title=None,
        height=308,
        showlegend=False,
        margin={"l": 47, "r": 14, "t": 12, "b": 32},
        hovermode="closest",
        font={"family": "Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif", "size": 11},
        hoverlabel={"bgcolor": "#0F172A", "font": {"color": "#F8FAFC", "size": 11}, "namelength": 0},
        uirevision=f"ttf-strip-{identity}-{x_axis}",
        meta={
            "period_label": label,
            "months": [value.isoformat() for value in months],
            "product": "TFO",
            "x_axis": x_axis,
            "period_kind": "strip",
        },
    )
    figure.update_yaxes(title="Equivalent strip IV (%)")
    if delta_mode:
        figure.update_xaxes(
            range=[0, 1],
            tickmode="array",
            tickvals=[0, 0.1, 0.25, 0.5, 0.75, 0.9, 1],
            ticktext=["0Δ put", "10Δ put", "25Δ put", "ATM", "25Δ call", "10Δ call", "0Δ call"],
        )
    else:
        figure.update_xaxes(title=None)
    return figure


def render_strip_charts(
    quote_snapshot,
    history,
    window,
    x_axis,
    selected_layers,
    *,
    input_loader=load_strip_inputs,
    settlement_loader=load_strip_settlement_inputs,
    **filters,
):
    history, quote_snapshot = history or {}, quote_snapshot or {}
    if history.get("product") != "TFO" or not window:
        return []
    quote_context = quote_snapshot.get("market_context") or {}
    if (
        window.get("history_key") != history_identity(history)
        or (quote_context and quote_context.get("history_key") != history_identity(history))
        or (
            quote_context and window.get("mode") == "live" and quote_context.get("cutoff_at") != window.get("cutoff_at")
        )
    ):
        return []
    groups = prepare_strip_events(quote_snapshot.get("rows") or [], "TFO", window)
    rows = [row for group in groups.values() for row in group]
    if not rows:
        return []
    for row in rows:
        row.setdefault("contract_label", row.get("strip_label") or row["_strip_label"])
        row.setdefault("option_filter_key", row["option_type"])
        row.setdefault("strike_filter_key", str(row["strike"]))
    filtered = quote_charts.filter_quote_rows(rows, product="TFO", **filters)
    if filtered.empty:
        return []
    groups = {key: frame.to_dict("records") for key, frame in filtered.groupby("_strip_key", sort=False)}
    months = sorted({month for rows in groups.values() for month in rows[0]["_strip_months"]})
    try:
        inputs = input_loader(history, months)
        load_error = None
    except Exception:
        inputs = None
        load_error = "Selected surface or same-COB forwards unavailable"
    try:
        settlements = settlement_loader(history, months)
        settlement_error = None
    except Exception:
        settlements = None
        settlement_error = "Bloomberg monthly settlements unavailable"
    plots = []
    ordered = sorted(groups.items(), key=lambda item: (item[0].startswith("season:"), item[1][0]["_strip_months"][0]))
    for key, rows in ordered:
        first = rows[0]
        valid_units = [
            row
            for row in rows
            if str(row.get("currency_code") or "EUR").upper() == "EUR"
            and str(row.get("price_unit") or "MWh").upper() == "MWH"
        ]
        projection = (
            _projection(inputs, first["_strip_months"], valid_units)
            if inputs and valid_units
            else {"curves": [], "quotes": [], "errors": [load_error or "No valid EUR/MWh strip quotes"]}
        )
        label = first["_strip_label"]
        identity = f"{key}-{(history.get('calibration') or {}).get('publication_id')}-{history.get('snapshot_id')}"
        settlement = (
            {
                **_settlement_projection(settlements, first["_strip_months"]),
                "legend_layer": "prior-settlement"
                if history.get("snapshot_kind") == "INTRADAY"
                else "bloomberg-settlement",
            }
            if settlements
            else {"points": [], "errors": [settlement_error] if settlement_error else []}
        )
        figure = build_strip_figure(
            label,
            first["_strip_months"],
            projection,
            x_axis=x_axis,
            selected_layers=selected_layers,
            identity=identity,
            settlement=settlement,
        )
        plots.append(
            html.Section(
                [
                    html.Header(
                        html.H3(label, className="brent-vol-history-card-title"),
                        className="brent-vol-history-card-header",
                    ),
                    dcc.Graph(
                        id={"type": "brent-vol-history-strip-graph", "period": key},
                        figure=figure,
                        config={
                            "displaylogo": False,
                            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                            "responsive": True,
                        },
                        className="brent-vol-history-graph",
                    ),
                ],
                className="brent-vol-history-expiry-card",
                role="region",
                **{"aria-label": f"{label} TFO equivalent strip volatility"},
            )
        )
    return plots
