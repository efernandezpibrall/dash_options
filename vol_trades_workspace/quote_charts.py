"""Quote charts for the Vol Trades workspace."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import ice_quote_data as quote_data

def _empty_figure(message: str) -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"color": "#6b7280", "size": 13},
    )
    return _finish_edge_figure(figure)


def _finish_edge_figure(figure: go.Figure) -> go.Figure:
    figure.update_layout(
        template="plotly_white",
        margin={"l": 66, "r": 26, "t": 42, "b": 28},
        height=320,
        hovermode="closest",
        legend={
            "orientation": "h",
            "y": 1.12,
            "x": 0,
            "xanchor": "left",
            "yanchor": "top",
            "bgcolor": "rgba(255,255,255,0.92)",
            "bordercolor": "#d0d5dd",
            "borderwidth": 1,
            "font": {"color": "#344054", "size": 11},
            "itemsizing": "constant",
        },
        hoverlabel={
            "bgcolor": "#ffffff",
            "bordercolor": "#98a2b3",
            "font": {"color": "#101828", "size": 12},
            "align": "left",
            "namelength": -1,
        },
        uirevision="ice-chat-vol-edge-v3",
        font={"color": "#334155", "size": 11},
        plot_bgcolor="#fbfdff",
        paper_bgcolor="#ffffff",
    )
    figure.update_yaxes(
        title_text="Broker IV minus our mark (vol pts)",
        zeroline=False,
        gridcolor="#e4e7ec",
        gridwidth=1,
        linecolor="#d0d5dd",
        tickcolor="#98a2b3",
        ticks="outside",
        ticklen=4,
        automargin=True,
        title_standoff=10,
    )
    figure.update_xaxes(
        title_text=None,
        showgrid=False,
        linecolor="#d0d5dd",
        tickcolor="#98a2b3",
        ticks="outside",
        ticklen=4,
        tickformat="%H:%M",
        hoverformat="%d %b %Y · %H:%M:%S UTC",
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikedash="dot",
        spikecolor="#98a2b3",
        spikethickness=1,
        automargin=True,
    )
    return figure


def _finish_figure(figure: go.Figure, *, top_title: str, bottom_title: str) -> go.Figure:
    figure.update_layout(
        template="plotly_white",
        margin={"l": 54, "r": 18, "t": 26, "b": 34},
        height=390,
        hovermode="closest",
        legend={"orientation": "h", "y": 1.06, "x": 0},
        uirevision="ice-chat-quotes-v1",
        font={"color": "#334155", "size": 11},
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
    )
    figure.update_yaxes(
        title_text=top_title,
        row=1,
        col=1,
        zeroline=False,
        gridcolor="#e2e8f0",
        title_standoff=8,
    )
    figure.update_yaxes(
        title_text=bottom_title,
        row=2,
        col=1,
        zeroline=False,
        gridcolor="#e2e8f0",
        title_standoff=8,
    )
    figure.update_xaxes(
        title_text="Observed time (UTC)",
        row=2,
        col=1,
        gridcolor="#f1f5f9",
    )
    return figure


def build_all_quotes_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return _empty_figure("No ICE Chat quotes in the selected window")
    figure = go.Figure()
    traces = (
        (
            "Sell to bid",
            "bid_iv_edge_pp",
            "bid",
            "bid_iv_pct",
            "#b42318",
            "triangle-down",
        ),
        (
            "Buy at offer",
            "offer_iv_edge_pp",
            "offer",
            "offer_iv_pct",
            "#067647",
            "triangle-up",
        ),
        (
            "ICE single price",
            "single_iv_deviation_pp",
            "single_price",
            "single_iv_pct",
            "#6941c6",
            "diamond",
        ),
    )
    working = frame.copy()
    visible_edges: list[float] = []
    for name, iv_column, quote_column, market_iv_column, color, symbol in traces:
        iv_subset = working.dropna(subset=[iv_column]).copy()
        if iv_subset.empty:
            continue
        edge_values = pd.to_numeric(iv_subset[iv_column], errors="coerce")
        visible_edges.extend(edge_values.dropna().astype(float).tolist())
        symbols = [
            symbol + ("-open" if side == "P" else "")
            for side in iv_subset["option_type"]
        ]
        size_field = {"bid": "bid_size", "offer": "offer_size", "single_price": "single_size"}[quote_column]
        sizes = iv_subset.get(size_field, pd.Series(index=iv_subset.index, dtype=float))
        sizes = pd.to_numeric(sizes, errors="coerce")
        size_labels = sizes.map(lambda value: "Size unavailable" if pd.isna(value) or value <= 0 else f"{value:,.0f}")
        custom = iv_subset[
            [
                "product_label",
                "contract_label",
                "option_label",
                "strike",
                "price_unit_label",
                quote_column,
                "theoretical_price",
                market_iv_column,
                "our_iv_pct",
                "forward",
                "surface_cob_date",
                "sender_handle",
                "outbound_status",
            ]
        ].fillna("—")
        custom["quoted_size"] = size_labels
        figure.add_trace(
            go.Scatter(
                x=iv_subset["observed_at"],
                y=iv_subset[iv_column],
                mode="markers",
                name=name,
                marker={
                    "color": color,
                    "symbol": symbols,
                    "size": [6] * len(iv_subset),
                    "opacity": 0.85,
                    "line": {
                        "color": [color if value.endswith("-open") else "#ffffff" for value in symbols],
                        "width": [1 if value.endswith("-open") else 0.5 for value in symbols],
                    },
                },
                cliponaxis=False,
                customdata=custom,
                hovertemplate=(
                    "<b>%{customdata[0]} · %{customdata[1]} %{customdata[2]} · K %{customdata[3]}</b><br>"
                    "%{x|%d %b %Y · %H:%M:%S UTC}<br><br>"
                    + name + " edge: <b>%{y:+.2f} vol pts</b><br>"
                    "Broker quote: %{customdata[5]} %{customdata[4]} · %{customdata[7]}% IV<br>"
                    "Quoted size: %{customdata[13]}<br>"
                    "Our mark: %{customdata[6]} %{customdata[4]} · %{customdata[8]}% IV<br>"
                    "Forward: %{customdata[9]} · Sender: %{customdata[11]}<br>"
                    "Surface: %{customdata[10]} · Delivery: %{customdata[12]}"
                    "<extra></extra>"
                ),
            )
        )
    if visible_edges:
        lower_data = min(min(visible_edges), 0.0)
        upper_data = max(max(visible_edges), 0.0)
        span = max(upper_data - lower_data, 1.0)
        padding = max(0.35, span * 0.1)
        lower_bound = lower_data - padding
        upper_bound = upper_data + padding
        figure.update_yaxes(range=[lower_bound, upper_bound])
        figure.add_hrect(
            y0=0.0,
            y1=upper_bound,
            fillcolor="rgba(18, 183, 106, 0.075)",
            line_width=0,
            layer="below",
        )
        figure.add_hrect(
            y0=lower_bound,
            y1=0.0,
            fillcolor="rgba(148, 163, 184, 0.065)",
            line_width=0,
            layer="below",
        )
    figure.add_hline(y=0.0, line_color="#475467", line_width=1.5, layer="above")
    figure.add_annotation(
        x=1.0,
        y=0.0,
        xref="paper",
        yref="y",
        text="OUR VOL",
        showarrow=False,
        xanchor="right",
        yanchor="bottom",
        yshift=5,
        font={"color": "#667085", "size": 9},
        bgcolor="rgba(255,255,255,0.86)",
        borderpad=2,
    )
    return _finish_edge_figure(figure)


def build_instrument_figure(frame: pd.DataFrame, selected: dict) -> go.Figure:
    required = {"contract_label", "option_type", "strike", "observed_at"}
    if frame.empty or not required.issubset(frame.columns):
        return _empty_figure("Selected instrument has no visible history")
    contract = selected.get("contract_label")
    option_type = selected.get("option_type")
    strike = selected.get("strike")
    instrument_mask = (
        (frame["contract_label"].astype(str) == str(contract))
        & (frame["option_type"] == option_type)
        & (pd.to_numeric(frame["strike"], errors="coerce") == float(strike))
    )
    if "structure_code" in frame:
        instrument_mask &= frame["structure_code"].fillna("").eq(selected.get("structure_code") or "")
    if selected.get("structure_code") in {"CLLR", "CALLSPR", "PUTSPR", "CFLY", "STNGL", "STRDL", "PUT_SPREAD_VS_CALL", "DIAGONAL_CALL_SPREAD", "CALL_SPREAD_VS_PUT_SPREAD"}:
        instrument_mask &= frame["structure_label"].eq(selected.get("structure_label"))
    if selected.get("product_code") is not None and "product_code" in frame:
        instrument_mask &= frame["product_code"] == selected["product_code"]
    subset = frame[instrument_mask].sort_values("observed_at")
    if subset.empty:
        return _empty_figure("Selected instrument has no visible history")
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.13,
        row_heights=[0.58, 0.42],
    )
    fence = selected.get("structure_code") == "CLLR"
    series = ((
        ("Broker indication*", "broker_indication_magnitude", "#7c3aed"),
        ("Our theo", "theoretical_cash_premium", "#111827"),
    ) if fence else (
        ("Bid", "bid", "#dc2626"),
        ("Offer", "offer", "#059669"),
        ("Trade", "single_price", "#7c3aed"),
        ("Our theo", "theoretical_price", "#111827"),
    ))
    for name, column, color in series:
        values = subset.dropna(subset=[column])
        if not values.empty:
            figure.add_trace(
                go.Scatter(
                    x=values["observed_at"],
                    y=values[column],
                    mode="lines+markers",
                    name=name,
                    line={"color": color, "width": 2 if name == "Our theo" else 1.5},
                ),
                row=1,
                col=1,
            )
    iv_series = (
        ("Bid IV", "bid_iv_pct", "#dc2626"),
        ("Offer IV", "offer_iv_pct", "#059669"),
        ("Trade IV", "single_iv_pct", "#7c3aed"),
        ("Our IV", "our_iv_pct", "#111827"),
    )
    for name, column, color in iv_series:
        values = subset.dropna(subset=[column])
        if not values.empty:
            figure.add_trace(
                go.Scatter(
                    x=values["observed_at"],
                    y=values[column],
                    mode="lines+markers",
                    name=name,
                    line={"color": color, "width": 2 if name == "Our IV" else 1.5},
                ),
                row=2,
                col=1,
            )
    price_unit_label = selected.get("price_unit_label") or "/".join(
        value
        for value in (
            selected.get("currency_code"),
            selected.get("price_unit"),
        )
        if value
    )
    return _finish_figure(
        figure,
        top_title=(f"Positive fence premium ({price_unit_label or 'price units'})"
                   if fence else f"Premium ({price_unit_label or 'price units'})"),
        bottom_title="Implied volatility (%)",
    )


def filter_quote_rows(
    rows: list[dict],
    *,
    product=None,
    contract=None,
    option_type=None,
    strike=None,
    sender=None,
    source_channel=None,
    status=None,
    positive_only=False,
) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if product is not None:
        selected_product = quote_data._selected_product(product)
        if selected_product not in quote_data.QUOTE_FEED_PRODUCTS:
            return frame.iloc[0:0].copy()
        codes = frame.get("product_code", pd.Series("B", index=frame.index))
        frame = frame.loc[codes.map(quote_data._row_product).eq(selected_product)].copy()
    if contract:
        frame = frame[frame["contract_label"].astype(str) == str(contract)]
    if option_type:
        frame = frame[frame["option_filter_key"] == option_type]
    if strike is not None:
        frame = frame[frame["strike_filter_key"] == str(strike)]
    if sender:
        frame = frame[frame["sender_handle"] == sender]
    if source_channel:
        frame = frame[frame["source_channel"] == source_channel]
    if status:
        frame = frame[frame["processing_status"] == status]
    if positive_only:
        frame = frame[frame["edge_actionable_now"].eq(True)]
    return frame.sort_values("observed_at", ascending=False)
