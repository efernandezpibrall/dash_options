"""Read-only trader view of persisted ICE Chat option quote valuations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any

import dash_ag_grid as dag
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, callback, dcc, html, no_update
from plotly.subplots import make_subplots
from sqlalchemy import text

from dash_utils import triggered_id
from runtime_config import get_database_engine


QUOTE_VIEW = "at_lng.ice_chat_quote_dashboard"
SERVICE_TABLE = "at_lng.ice_chat_service_status"
ROW_LIMIT = 10_000
QUOTE_COLUMNS = (
    "event_id",
    "observed_at",
    "received_at",
    "source_channel",
    "recovered",
    "sender_handle",
    "normalization_status",
    "normalization_error",
    "contract_month",
    "option_type",
    "strike",
    "bid",
    "bid_size",
    "offer",
    "offer_size",
    "single_price",
    "single_size",
    "valuation_status",
    "valuation_reason",
    "valued_at",
    "theoretical_price",
    "our_volatility",
    "bid_implied_volatility",
    "offer_implied_volatility",
    "single_implied_volatility",
    "bid_sell_edge",
    "offer_buy_edge",
    "single_deviation",
    "bid_volatility_edge",
    "offer_volatility_edge",
    "bid_edge_ticks",
    "offer_edge_ticks",
    "best_action",
    "best_edge",
    "best_edge_ticks",
    "forward",
    "forward_source",
    "forward_cob_date",
    "surface_cob_date",
    "surface_business_day_age",
    "surface_source",
    "option_expiration_date",
    "pricing_model",
    "pricing_model_version",
    "outbound_channel",
    "outbound_status",
    "request_seq_id",
    "outbound_acknowledged_at",
    "outbound_error_code",
    "outbound_error_message",
)
# The current ICE service contract is Brent-only and predates explicit quote
# convention fields. These expressions keep those rows readable while allowing
# a future view to expose product/currency/unit/tick metadata without a Dash
# release. Upstream must populate all five fields before admitting a new product.
QUOTE_METADATA_EXPRESSIONS = (
    "COALESCE(NULLIF(to_jsonb(quote_row)->>'product_code', ''), 'B') AS product_code",
    "COALESCE(NULLIF(to_jsonb(quote_row)->>'product_label', ''), "
    "NULLIF(to_jsonb(quote_row)->>'product_name', ''), 'Brent') AS product_label",
    "COALESCE(NULLIF(to_jsonb(quote_row)->>'currency_code', ''), 'USD') AS currency_code",
    "COALESCE(NULLIF(to_jsonb(quote_row)->>'price_unit', ''), 'bbl') AS price_unit",
    "CASE WHEN COALESCE(to_jsonb(quote_row)->>'tick_size', '') "
    "~ '^[0-9]+([.][0-9]+)?$' "
    "THEN (to_jsonb(quote_row)->>'tick_size')::numeric ELSE 0.01 END AS tick_size",
)
TIME_WINDOWS = {
    "2h": timedelta(hours=2),
    "8h": timedelta(hours=8),
    "7d": timedelta(days=7),
}


@dataclass(frozen=True)
class QuoteLoadResult:
    rows: list[dict]
    service: dict
    error: str | None
    loaded_at: str
    truncated: bool


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: ICE Chat quote data could not be loaded"


def _tick_decimal_places(value: Any) -> int:
    try:
        exponent = Decimal(str(value)).normalize().as_tuple().exponent
    except Exception:
        return 2
    return min(6, max(0, -int(exponent)))


def _compact_number(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.6f}".rstrip("0").rstrip(".")


def _cutoff(window: str, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if window == "today":
        dubai = pd.Timestamp(current).tz_convert("Asia/Dubai")
        return dubai.normalize().tz_convert("UTC").to_pydatetime()
    return current - TIME_WINDOWS.get(window, TIME_WINDOWS["8h"])


def _serialize_frame(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    normalized = frame.copy()
    if "event_id" in normalized:
        normalized["event_id"] = normalized["event_id"].astype(str)
    for column in (
        "observed_at",
        "received_at",
        "valued_at",
        "outbound_acknowledged_at",
    ):
        if column in normalized:
            normalized[column] = pd.to_datetime(
                normalized[column], errors="coerce", utc=True
            )
    for column in (
        "contract_month",
        "forward_cob_date",
        "surface_cob_date",
        "option_expiration_date",
    ):
        if column in normalized:
            normalized[column] = pd.to_datetime(
                normalized[column], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
    for column in (
        "strike",
        "bid",
        "bid_size",
        "offer",
        "offer_size",
        "single_price",
        "single_size",
        "theoretical_price",
        "our_volatility",
        "bid_implied_volatility",
        "offer_implied_volatility",
        "single_implied_volatility",
        "bid_sell_edge",
        "offer_buy_edge",
        "single_deviation",
        "bid_volatility_edge",
        "offer_volatility_edge",
        "bid_edge_ticks",
        "offer_edge_ticks",
        "best_edge",
        "best_edge_ticks",
        "forward",
        "tick_size",
    ):
        if column in normalized:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if "product_code" not in normalized:
        normalized["product_code"] = "B"
    normalized["product_code"] = normalized["product_code"].replace("", pd.NA).fillna("B")
    if "product_label" not in normalized:
        normalized["product_label"] = pd.NA
    product_fallback = normalized["product_code"].map({"B": "Brent"}).fillna(
        normalized["product_code"]
    )
    normalized["product_label"] = (
        normalized["product_label"].replace("", pd.NA).fillna(product_fallback)
    )
    for column, fallback in (("currency_code", "USD"), ("price_unit", "bbl")):
        if column not in normalized:
            normalized[column] = fallback
        normalized[column] = normalized[column].replace("", pd.NA).fillna(fallback)
    if "tick_size" not in normalized:
        normalized["tick_size"] = 0.01
    normalized["tick_size"] = pd.to_numeric(
        normalized["tick_size"], errors="coerce"
    ).where(lambda values: values > 0, 0.01).fillna(0.01)
    normalized["price_decimals"] = normalized["tick_size"].map(_tick_decimal_places)
    normalized["price_unit_label"] = (
        normalized["currency_code"].astype(str)
        + "/"
        + normalized["price_unit"].astype(str)
    )
    normalized["contract_label"] = pd.to_datetime(
        normalized.get("contract_month"), errors="coerce"
    ).dt.strftime("%b-%y")
    normalized["option_label"] = normalized.get("option_type", pd.Series(dtype=str)).map(
        {"C": "Call", "P": "Put"}
    )
    normalized["strike_label"] = normalized["strike"].map(_compact_number)
    normalized["instrument_label"] = (
        normalized["contract_label"].fillna("—")
        + " · "
        + normalized["strike_label"]
        + " "
        + normalized["option_label"].fillna(normalized["option_type"])
    )
    normalized["processing_status"] = normalized.get("valuation_status").fillna(
        normalized.get("normalization_status")
    )
    normalized["display_error"] = (
        normalized.get("valuation_reason")
        .fillna(normalized.get("normalization_error"))
        .fillna(normalized.get("outbound_error_message"))
    )
    for source, target in (
        ("our_volatility", "our_iv_pct"),
        ("bid_implied_volatility", "bid_iv_pct"),
        ("offer_implied_volatility", "offer_iv_pct"),
        ("single_implied_volatility", "single_iv_pct"),
        ("bid_volatility_edge", "bid_iv_edge_pp"),
        ("offer_volatility_edge", "offer_iv_edge_pp"),
    ):
        normalized[target] = normalized[source] * 100.0
    normalized["single_iv_deviation_pp"] = (
        normalized["single_iv_pct"] - normalized["our_iv_pct"]
    )
    normalized["single_edge_ticks"] = (
        normalized["single_deviation"] / normalized["tick_size"]
    )
    edge_candidates = pd.concat(
        {
            "SELL": pd.to_numeric(normalized["bid_edge_ticks"], errors="coerce"),
            "BUY": pd.to_numeric(normalized["offer_edge_ticks"], errors="coerce"),
        },
        axis=1,
    )
    normalized["signal_edge_ticks"] = edge_candidates.max(axis=1, skipna=True)
    has_executable_quote = ~edge_candidates.isna().all(axis=1)
    normalized["signal_side"] = None
    normalized.loc[has_executable_quote, "signal_side"] = edge_candidates.loc[
        has_executable_quote
    ].idxmax(axis=1)
    normalized["signal_label"] = normalized["best_action"].fillna("NO EDGE")
    single_only = edge_candidates.isna().all(axis=1) & normalized["single_price"].notna()
    normalized.loc[single_only, "signal_label"] = "SINGLE"
    normalized.loc[single_only, "signal_edge_ticks"] = normalized.loc[
        single_only, "single_edge_ticks"
    ]
    normalized["signal_price_edge"] = pd.NA
    normalized.loc[normalized["signal_side"] == "SELL", "signal_price_edge"] = (
        normalized.loc[normalized["signal_side"] == "SELL", "bid_sell_edge"]
    )
    normalized.loc[normalized["signal_side"] == "BUY", "signal_price_edge"] = (
        normalized.loc[normalized["signal_side"] == "BUY", "offer_buy_edge"]
    )
    normalized.loc[single_only, "signal_price_edge"] = normalized.loc[
        single_only, "single_deviation"
    ]
    normalized["signal_price_edge"] = pd.to_numeric(
        normalized["signal_price_edge"], errors="coerce"
    )
    normalized["signal_iv_edge_pp"] = pd.NA
    sell_side = normalized["signal_side"] == "SELL"
    buy_side = normalized["signal_side"] == "BUY"
    normalized.loc[sell_side, "signal_iv_edge_pp"] = normalized.loc[
        sell_side, "bid_iv_edge_pp"
    ]
    normalized.loc[buy_side, "signal_iv_edge_pp"] = normalized.loc[
        buy_side, "offer_iv_edge_pp"
    ]
    normalized.loc[single_only, "signal_iv_edge_pp"] = normalized.loc[
        single_only, "single_iv_deviation_pp"
    ]
    normalized["signal_iv_edge_pp"] = pd.to_numeric(
        normalized["signal_iv_edge_pp"], errors="coerce"
    )
    normalized["observed_display"] = normalized["observed_at"].dt.tz_convert(
        "Asia/Dubai"
    ).dt.strftime("%d %b %H:%M:%S")
    return json.loads(normalized.to_json(orient="records", date_format="iso"))


def load_quote_snapshot(
    window: str,
    *,
    engine=None,
    now: datetime | None = None,
) -> QuoteLoadResult:
    loaded_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        return QuoteLoadResult(
            rows=[],
            service={},
            error="Database configuration is unavailable",
            loaded_at=loaded_at.isoformat(),
            truncated=False,
        )
    quote_query = text(
        f"""
        SELECT {", ".join(QUOTE_COLUMNS)},
               {", ".join(QUOTE_METADATA_EXPRESSIONS)}
        FROM {QUOTE_VIEW} AS quote_row
        WHERE observed_at >= :cutoff
        ORDER BY observed_at DESC, event_id DESC
        LIMIT :row_limit
        """
    )
    service_query = text(
        f"""
        SELECT environment, connection_state, outbound_enabled, session_id,
               connected_at, disconnected_at, last_heartbeat_at, last_event_at,
               surface_cob_date, surface_business_day_age, queue_depth,
               last_request_seq_id, last_error_code, last_error_message,
               updated_at
        FROM {SERVICE_TABLE}
        ORDER BY updated_at DESC
        LIMIT 1
        """
    )
    surface_query = text(
        """
        SELECT max(cob_date) AS surface_cob_date
        FROM at_lng.implied_volatility_surface_from_prices
        WHERE upper(product)='BRENT'
        """
    )
    try:
        with db_engine.connect() as connection:
            frame = pd.read_sql(
                quote_query,
                connection,
                params={"cutoff": _cutoff(window, now), "row_limit": ROW_LIMIT + 1},
            )
            service_row = connection.execute(service_query).mappings().first()
            surface_cob = connection.execute(surface_query).scalar()
    except Exception as exc:
        return QuoteLoadResult(
            rows=[],
            service={},
            error=_safe_error(exc),
            loaded_at=loaded_at.isoformat(),
            truncated=False,
        )
    truncated = len(frame) > ROW_LIMIT
    frame = frame.iloc[:ROW_LIMIT].copy()
    service = dict(service_row) if service_row else {}
    for key, value in list(service.items()):
        if isinstance(value, (datetime, pd.Timestamp)):
            service[key] = pd.Timestamp(value).isoformat()
    service["surface_cob_date"] = str(
        service.get("surface_cob_date") or surface_cob
    ) if (service.get("surface_cob_date") or surface_cob) else None
    return QuoteLoadResult(
        rows=_serialize_frame(frame),
        service=service,
        error=None,
        loaded_at=loaded_at.isoformat(),
        truncated=truncated,
    )


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
        title_text="Vol edge vs our mark (vol pts)",
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
            "Single quote",
            "single_iv_deviation_pp",
            "single_price",
            "single_iv_pct",
            "#6941c6",
            "circle",
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
        observed_times = pd.to_datetime(iv_subset["observed_at"], utc=True, errors="coerce")
        marker_sizes = [10] * len(iv_subset)
        if observed_times.notna().any():
            latest_position = int(observed_times.reset_index(drop=True).idxmax())
            marker_sizes[latest_position] = 13
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
        figure.add_trace(
            go.Scatter(
                x=iv_subset["observed_at"],
                y=iv_subset[iv_column],
                mode="markers",
                name=name,
                marker={
                    "color": color,
                    "symbol": symbol,
                    "size": marker_sizes,
                    "opacity": 0.9,
                    "line": {"color": "#ffffff", "width": 1.5},
                },
                cliponaxis=False,
                customdata=custom,
                hovertemplate=(
                    "<b>%{customdata[0]} · %{customdata[1]} %{customdata[2]} · K %{customdata[3]}</b><br>"
                    "%{x|%d %b %Y · %H:%M:%S UTC}<br><br>"
                    + name + " edge: <b>%{y:+.2f} vol pts</b><br>"
                    "Broker quote: %{customdata[5]} %{customdata[4]} · %{customdata[7]}% IV<br>"
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
    required = {"contract_month", "option_type", "strike", "observed_at"}
    if frame.empty or not required.issubset(frame.columns):
        return _empty_figure("Selected instrument has no visible history")
    contract = selected.get("contract_month")
    option_type = selected.get("option_type")
    strike = selected.get("strike")
    instrument_mask = (
        (frame["contract_month"].astype(str) == str(contract))
        & (frame["option_type"] == option_type)
        & (pd.to_numeric(frame["strike"], errors="coerce") == float(strike))
    )
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
    series = (
        ("Bid", "bid", "#dc2626"),
        ("Offer", "offer", "#059669"),
        ("Single", "single_price", "#7c3aed"),
        ("Our theo", "theoretical_price", "#111827"),
    )
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
        ("Single IV", "single_iv_pct", "#7c3aed"),
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
        top_title=f"Premium ({price_unit_label or 'price units'})",
        bottom_title="Implied volatility (%)",
    )


def filter_quote_rows(
    rows: list[dict],
    *,
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
    if contract:
        frame = frame[frame["contract_month"].astype(str) == str(contract)]
    if option_type:
        frame = frame[frame["option_type"] == option_type]
    if strike is not None:
        frame = frame[pd.to_numeric(frame["strike"], errors="coerce") == float(strike)]
    if sender:
        frame = frame[frame["sender_handle"] == sender]
    if source_channel:
        frame = frame[frame["source_channel"] == source_channel]
    if status:
        frame = frame[frame["processing_status"] == status]
    if positive_only:
        frame = frame[pd.to_numeric(frame["best_edge_ticks"], errors="coerce") >= 1.0]
    return frame.sort_values("observed_at", ascending=False)


def build_service_strip(service: dict, *, error: str | None, loaded_at: str):
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
    surface_date = service.get("surface_cob_date")
    surface_display = "No surface"
    if surface_date:
        surface_display = pd.Timestamp(surface_date).strftime("%d %b")
        if age is not None:
            surface_display += " · fresh" if int(age) <= 1 else f" · {age} BD old"
    heartbeat = service.get("last_heartbeat_at") or "—"
    last_event = service.get("last_event_at") or "—"
    if last_event != "—":
        event_time = pd.Timestamp(last_event)
        if event_time.tzinfo is None:
            event_time = event_time.tz_localize("UTC")
        last_event_display = event_time.tz_convert("Asia/Dubai").strftime("%H:%M GST")
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
        f"Heartbeat {str(heartbeat)[11:19] + ' UTC' if heartbeat != '—' else '—'} · "
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
COMPACT_NUMBER_FORMATTER = {
    "function": "params.value == null ? '—' : Number(params.value).toLocaleString(undefined, {maximumFractionDigits: 6})"
}
PRICE_FORMATTER = {
    "function": "params.value == null ? '—' : Number(params.value).toFixed(params.data && params.data.price_decimals != null ? Number(params.data.price_decimals) : 2)"
}
SIZE_FORMATTER = {
    "function": "params.value == null ? '—' : Number(params.value).toLocaleString(undefined, {maximumFractionDigits: 2})"
}
ACTION_FORMATTER = {"function": "params.value == null ? 'NO EDGE' : params.value"}

# Fixed widths keep the tape stable while allowing for the rendered header text,
# 10px dense-grid padding, and representative formatted values in each column.
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
                "width": 66,
                "headerClass": "ice-chat-text-header",
                "cellClass": "ice-chat-text-cell ice-chat-instrument-cell",
            },
            {
                "headerName": "Type",
                "field": "option_label",
                "pinned": "left",
                "lockPinned": True,
                "width": 50,
                "headerClass": "ice-chat-text-header",
                "cellClass": "ice-chat-text-cell ice-chat-instrument-cell",
            },
            {
                "headerName": "Strike",
                "field": "strike",
                "pinned": "left",
                "lockPinned": True,
                "width": 52,
                "type": "rightAligned",
                "valueFormatter": COMPACT_NUMBER_FORMATTER,
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
            {"headerName": "Single", "field": "single_price", "valueFormatter": PRICE_FORMATTER, "width": 58, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-market-price-cell"},
            {"headerName": "Qty", "field": "single_size", "valueFormatter": SIZE_FORMATTER, "width": 48, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-size-cell"},
        ],
    },
    {
        "headerName": "Market IV (%)",
        "headerClass": "ice-chat-group-header ice-chat-group-iv",
        "children": [
            {"headerName": "Bid", "field": "bid_iv_pct", "valueFormatter": NUMBER_FORMATTER_2, "width": 56, "type": "rightAligned", "headerClass": "ice-chat-number-header ice-chat-group-start", "cellClass": "ice-chat-number-cell ice-chat-group-start"},
            {"headerName": "Offer", "field": "offer_iv_pct", "valueFormatter": NUMBER_FORMATTER_2, "width": 58, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell"},
            {"headerName": "Single", "field": "single_iv_pct", "valueFormatter": NUMBER_FORMATTER_2, "width": 58, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell"},
        ],
    },
    {
        "headerName": "Our valuation",
        "headerClass": "ice-chat-group-header ice-chat-group-valuation",
        "children": [
            {"headerName": "Theo", "field": "theoretical_price", "valueFormatter": PRICE_FORMATTER, "width": 58, "type": "rightAligned", "headerClass": "ice-chat-number-header ice-chat-group-start", "cellClass": "ice-chat-number-cell ice-chat-theo-cell ice-chat-group-start"},
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
                    "ice-chat-cell-buy": "params.value === 'BUY'",
                    "ice-chat-cell-sell": "params.value === 'SELL'",
                    "ice-chat-cell-neutral": "params.value === 'NO EDGE' || params.value === 'SINGLE'",
                },
            },
            {"headerName": "Margin", "field": "signal_price_edge", "valueFormatter": PRICE_FORMATTER, "width": 62, "type": "rightAligned", "headerClass": "ice-chat-number-header", "cellClass": "ice-chat-number-cell ice-chat-edge-metric", "cellClassRules": {"ice-chat-edge-positive": "Number(params.value) > 0", "ice-chat-edge-negative": "Number(params.value) < 0", "ice-chat-edge-flat": "Number(params.value) === 0"}},
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


layout = html.Main(
    [
        html.Header(
            [
                html.Div(
                    [
                        html.H1("ICE Chat quotes"),
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
                        options=[{"label": "Positive executable edge", "value": "positive"}],
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
                        html.H2(
                            "Broker quotes executable edge",
                            id="ice-chat-chart-title",
                        ),
                        html.Span(
                            "Above 0 favors us on bid/offer · single quotes are neutral",
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
                            figure=_empty_figure("Waiting for ICE Chat quote data"),
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
                        html.H2("Quote tape"),
                        html.Div(id="ice-chat-table-status", role="status", **{"aria-live": "polite"}),
                    ],
                    className="ice-chat-section-header",
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
            ],
            className="ice-chat-card ice-chat-table-card",
        ),
    ],
    className="options-dashboard-container ice-chat-quotes-page",
)


@callback(
    Output("ice-chat-quote-snapshot", "data"),
    Output("ice-chat-service-snapshot", "data"),
    Input("ice-chat-refresh-interval", "n_intervals"),
    Input("ice-chat-window", "value"),
)
def refresh_quote_snapshot(_interval, window):
    result = load_quote_snapshot(window or "today")
    return (
        {
            "rows": result.rows,
            "error": result.error,
            "loaded_at": result.loaded_at,
            "truncated": result.truncated,
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
)
def update_quote_filter_options(snapshot):
    frame = pd.DataFrame((snapshot or {}).get("rows") or [])
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
        options("contract_month", "contract_label"),
        options("option_type", "option_label"),
        options("strike"),
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
):
    snapshot = snapshot or {}
    rows = snapshot.get("rows") or []
    frame = filter_quote_rows(
        rows,
        contract=contract,
        option_type=option_type,
        strike=strike,
        sender=sender,
        source_channel=source_channel,
        status=status,
        positive_only="positive" in (positive_only or []),
    )
    selected = (selected_rows or [None])[0]
    if selected:
        figure = build_instrument_figure(frame, selected)
        chart_title = "Instrument quote history"
        instrument = selected.get("instrument_label") or "Selected instrument"
        chart_hint = f"{instrument} · broker premium and IV vs our marks"
    else:
        figure = build_all_quotes_figure(frame)
        chart_title = "Broker quotes executable edge"
        chart_hint = "Above 0 favors us on bid/offer · single quotes are neutral"
    status_strip = build_service_strip(
        service or {},
        error=snapshot.get("error"),
        loaded_at=snapshot.get("loaded_at") or datetime.now(timezone.utc).isoformat(),
    )
    if snapshot.get("error"):
        table_status = snapshot["error"]
    elif frame.empty:
        table_status = "No quotes match the selected filters"
    else:
        suffix = " · first 10,000 rows" if snapshot.get("truncated") else ""
        table_status = f"{len(frame):,} quote events{suffix}"
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
