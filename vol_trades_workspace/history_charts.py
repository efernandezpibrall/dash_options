"""History charts for the Vol Trades workspace."""

from __future__ import annotations

from vol_trades_workspace import trade_tape as tape_views

from vol_trades_workspace import chart_data

import json
import hashlib
from html import escape
from typing import Any
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import (
    dcc,
    html,
)
from plotly.subplots import make_subplots
import vol_trades_data as market_data
import ice_quote_data as quote_data

def build_expiry_figure(
    chain: pd.DataFrame,
    prepared: pd.DataFrame,
    published_nodes: pd.DataFrame,
    expiry: pd.Timestamp,
    x_axis: str = chart_data.X_AXIS_STRIKE,
    trade_tape: pd.DataFrame | None = None,
    prior_settlement_chain: pd.DataFrame | None = None,
    product: str | None = None,
    calibrated_nodes: pd.DataFrame | None = None,
) -> go.Figure:
    x_axis = chart_data._normalize_x_axis(x_axis)
    raw = chain.loc[chart_data._expiry_mask(chain, expiry, "underlying_contract_month")].copy()
    resolved_product = market_data._normalize_product(
        product
        or (
            raw["product"].iloc[0]
            if "product" in raw.columns and not raw.empty
            else market_data.PRODUCT
        )
    )
    spec = market_data._product_spec(resolved_product)
    underlying_hover_label = spec["underlying_label"]
    if "volume_delta" not in raw.columns:
        raw["volume_delta"] = np.nan
    if "snapshot_kind" not in raw.columns:
        raw["snapshot_kind"] = "SETTLEMENT"
    if "open_interest_date" not in raw.columns:
        raw["open_interest_date"] = pd.NaT
    if "open_interest_scope_status" not in raw.columns:
        raw["open_interest_scope_status"] = np.where(
            raw["snapshot_kind"].eq("INTRADAY"),
            "effective_date_unavailable",
            "settlement",
        )
    is_intraday = raw["snapshot_kind"].iloc[0] == "INTRADAY"
    executable_mask = (
        raw.get("executable_iv_status", pd.Series(index=raw.index, dtype=object))
        .astype(str)
        .eq("resolved")
        & pd.to_numeric(
            raw.get("executable_iv_mid", pd.Series(np.nan, index=raw.index)),
            errors="coerce",
        ).notna()
    )
    prior_raw = (
        prior_settlement_chain.loc[
            chart_data._expiry_mask(
                prior_settlement_chain,
                expiry,
                "underlying_contract_month",
            )
        ].copy()
        if is_intraday
        and prior_settlement_chain is not None
        and not prior_settlement_chain.empty
        else pd.DataFrame()
    )
    prior_reference = (
        chart_data._settlement_reference_smile(prior_raw, x_axis)
        if not prior_raw.empty
        else pd.DataFrame()
    )
    if not prior_reference.empty:
        current_strikes = set(
            pd.to_numeric(raw["strike"], errors="coerce").dropna().astype(float)
        )
        prior_reference = prior_reference.loc[
            pd.to_numeric(prior_reference["strike"], errors="coerce").isin(
                current_strikes
            )
        ].copy()
        prior_dates = pd.to_datetime(
            prior_raw["business_date"], errors="coerce"
        ).dropna()
        prior_expirations = pd.to_datetime(
            prior_raw["option_expiration_date"], errors="coerce"
        ).dropna()
        if prior_dates.empty:
            prior_reference = pd.DataFrame()
        else:
            prior_reference["business_date"] = prior_dates.iloc[0]
            prior_reference["option_expiration_date"] = (
                prior_expirations.iloc[0]
                if not prior_expirations.empty
                else pd.NaT
            )
    published = (
        published_nodes.loc[chart_data._expiry_mask(published_nodes, expiry, "contract_date")].copy()
        if published_nodes is not None and not published_nodes.empty
        else pd.DataFrame()
    )
    calibrated = (
        calibrated_nodes.loc[
            chart_data._expiry_mask(calibrated_nodes, expiry, "contract_date")
        ].copy()
        if calibrated_nodes is not None and not calibrated_nodes.empty
        else pd.DataFrame()
    )
    figure = make_subplots(
        rows=1,
        cols=1,
        specs=[[{"secondary_y": True}]],
    )
    activity = (
        raw.groupby(["strike", "put_call"], as_index=False)
        .agg(
            volume=("volume", lambda values: values.sum(min_count=1)),
            open_interest=("open_interest", lambda values: values.sum(min_count=1)),
            volume_delta=("volume_delta", lambda values: values.sum(min_count=1)),
            underlying_price=("underlying_price", "median"),
            open_interest_date=("open_interest_date", "max"),
            open_interest_scope_status=("open_interest_scope_status", "first"),
        )
        .sort_values("strike")
    )
    activity_data_mask = activity[["volume", "open_interest"]].notna().any(axis=1)
    activity_strikes_with_data = set(
        pd.to_numeric(
            activity.loc[activity_data_mask, "strike"], errors="coerce"
        ).dropna()
    )
    if x_axis == chart_data.X_AXIS_DELTA:
        activity = activity.merge(
            chart_data._activity_delta_projection(
                raw,
                trade_tape=trade_tape,
                prior_settlement=prior_reference,
            ),
            how="left",
            on="strike",
            validate="many_to_one",
        )
        activity = activity.loc[activity["display_delta"].notna()].copy()
    else:
        activity["display_delta"] = pd.to_numeric(activity["strike"], errors="coerce")
        activity["delta_source"] = "Strike"
    projected_activity_strikes = set(
        pd.to_numeric(activity["strike"], errors="coerce").dropna()
    )
    missing_activity_strikes = len(
        activity_strikes_with_data - projected_activity_strikes
    )
    activity_pivot = activity.pivot(
        index="strike",
        columns="put_call",
        values=["volume", "open_interest", "volume_delta"],
    ).sort_index()
    strikes = activity_pivot.index.to_numpy(dtype=float)
    activity_by_strike = activity.drop_duplicates("strike").set_index("strike")
    axis_values = pd.to_numeric(
        activity_by_strike["display_delta"], errors="coerce"
    ).reindex(activity_pivot.index)
    delta_sources = activity_by_strike["delta_source"].reindex(activity_pivot.index)
    underlying_prices = pd.to_numeric(
        activity_by_strike["underlying_price"], errors="coerce"
    ).reindex(activity_pivot.index)
    axis_differences = np.diff(np.unique(axis_values.dropna().to_numpy(dtype=float)))
    positive_differences = axis_differences[axis_differences > 0]
    axis_spacing = (
        float(np.median(positive_differences))
        if positive_differences.size
        else (
            0.02
            if x_axis == chart_data.X_AXIS_DELTA or not strikes.size
            else max(float(strikes[0]) * 0.01, 0.5)
        )
    )
    if x_axis == chart_data.X_AXIS_DELTA:
        open_interest_width = float(np.clip(0.82 * axis_spacing, 0.006, 0.035))
        volume_width = float(np.clip(0.40 * axis_spacing, 0.003, 0.018))
    else:
        open_interest_width = 0.82 * axis_spacing
        volume_width = 0.40 * axis_spacing
    activity_colors = {"C": "#2563EB", "P": "#0F766E"}
    activity_patterns = {"C": "", "P": "/"}

    def activity_values(metric: str, put_call: str) -> pd.Series:
        key = (metric, put_call)
        if key not in activity_pivot.columns:
            return pd.Series(index=activity_pivot.index, dtype=float)
        return pd.to_numeric(activity_pivot[key], errors="coerce")

    call_open_interest = activity_values("open_interest", "C")
    put_open_interest = activity_values("open_interest", "P")
    call_volume = activity_values("volume", "C")
    put_volume = activity_values("volume", "P")
    call_volume_delta = activity_values("volume_delta", "C")
    put_volume_delta = activity_values("volume_delta", "P")

    def add_activity_bar(
        values: pd.Series,
        *,
        put_call: str,
        metric_label: str,
        width: float,
        opacity: float,
        base: pd.Series,
        legendrank: int,
        deltas: pd.Series | None = None,
    ) -> None:
        if not values.notna().any():
            return
        option_label = "calls" if put_call == "C" else "puts"
        aligned_volume_deltas = (
            pd.to_numeric(deltas, errors="coerce").reindex(activity_pivot.index)
            if deltas is not None
            else pd.Series(np.nan, index=activity_pivot.index)
        )
        side_activity = activity.loc[activity["put_call"].eq(put_call)].set_index(
            "strike"
        )
        side_volume = pd.to_numeric(
            side_activity["volume"], errors="coerce"
        ).reindex(activity_pivot.index)
        side_open_interest = pd.to_numeric(
            side_activity["open_interest"], errors="coerce"
        ).reindex(activity_pivot.index)
        open_interest_as_of = chart_data._hover_date_strings(
            side_activity["open_interest_date"].reindex(activity_pivot.index)
        )
        open_interest_status = chart_data._hover_oi_status_strings(
            side_activity["open_interest_scope_status"].reindex(
                activity_pivot.index
            )
        )
        positive_delta = aligned_volume_deltas.fillna(0.0).gt(0.0)
        line_colors = [
            "#F97316" if is_positive else activity_colors[put_call]
            for is_positive in positive_delta
        ]
        line_widths = [2.2 if is_positive else 0.7 for is_positive in positive_delta]
        axis_hover = (
            " · Δ %{x:.3f}<br>%{customdata[2]}"
            if x_axis == chart_data.X_AXIS_DELTA
            else ""
        )
        new_volume = (
            " · New <b>%{customdata[3]:,.0f}</b>"
            if metric_label == "Volume" and deltas is not None
            else ""
        )
        hovertemplate = (
            f"<b>{option_label.title()} activity</b>"
            "<br>Strike %{customdata[0]:.2f}"
            + axis_hover
            + f"<br>{underlying_hover_label} %{{customdata[4]:.3f}}"
            + "<br>Volume / OI <b>%{customdata[7]} / %{customdata[8]}</b>"
            + new_volume
            + "<br>OI %{customdata[5]} · %{customdata[6]}"
            + "<extra></extra>"
        )
        figure.add_trace(
            go.Bar(
                x=axis_values,
                y=values,
                base=base,
                width=width,
                name=f"{metric_label} · {option_label}",
                legendgroup=f"{metric_label.lower().replace(' ', '-')}-{put_call}",
                legendrank=legendrank,
                meta={
                    "legend_layer": (
                        "open-interest" if metric_label == "Open interest" else "volume"
                    )
                    + ("-calls" if put_call == "C" else "-puts")
                },
                opacity=opacity,
                marker={
                    "color": activity_colors[put_call],
                    "line": {"color": line_colors, "width": line_widths},
                    "pattern": {"shape": activity_patterns[put_call]},
                },
                customdata=np.column_stack(
                    [
                        strikes,
                        axis_values,
                        delta_sources,
                        aligned_volume_deltas,
                        underlying_prices,
                        open_interest_as_of,
                        open_interest_status,
                        chart_data._hover_count_strings(side_volume),
                        chart_data._hover_count_strings(side_open_interest),
                    ]
                ),
                hovertemplate=hovertemplate,
            ),
            secondary_y=True,
        )

    zero_base = pd.Series(0.0, index=activity_pivot.index)
    add_activity_bar(
        call_open_interest,
        put_call="C",
        metric_label="Open interest",
        width=open_interest_width,
        opacity=0.12,
        base=zero_base,
        legendrank=60,
    )
    add_activity_bar(
        put_open_interest,
        put_call="P",
        metric_label="Open interest",
        width=open_interest_width,
        opacity=0.12,
        base=call_open_interest.fillna(0.0),
        legendrank=70,
    )
    add_activity_bar(
        call_volume,
        put_call="C",
        metric_label="Volume",
        width=volume_width,
        opacity=0.30,
        base=zero_base,
        legendrank=40,
        deltas=(
            call_volume_delta
            if raw["snapshot_kind"].iloc[0] == "INTRADAY"
            else None
        ),
    )
    add_activity_bar(
        put_volume,
        put_call="P",
        metric_label="Volume",
        width=volume_width,
        opacity=0.30,
        base=call_volume.fillna(0.0),
        legendrank=50,
        deltas=(
            put_volume_delta
            if raw["snapshot_kind"].iloc[0] == "INTRADAY"
            else None
        ),
    )
    if x_axis == chart_data.X_AXIS_DELTA and missing_activity_strikes:
        figure.add_annotation(
            x=0.5,
            y=0.01,
            xref="paper",
            yref="paper",
            text=(
                f"{missing_activity_strikes} activity strike"
                f"{'s' if missing_activity_strikes != 1 else ''} unavailable on Delta: "
                "no quality-approved IV reference"
            ),
            showarrow=False,
            bgcolor="rgba(255,247,237,0.94)",
            bordercolor="#FDBA74",
            borderpad=4,
            font={"color": "#9A3412", "size": 10},
        )

    executable = pd.DataFrame()
    settlement_reference = pd.DataFrame()
    matched_trades = pd.DataFrame()
    axis_hover = " · Δ %{x:.3f}" if x_axis == chart_data.X_AXIS_DELTA else ""
    if is_intraday:
        executable = raw.loc[executable_mask].copy()
        if not prior_reference.empty:
            prior_date = pd.to_datetime(
                prior_reference["business_date"], errors="coerce"
            ).dropna().iloc[0]
            figure.add_trace(
                go.Scatter(
                    x=prior_reference["display_x"],
                    y=100.0 * prior_reference["reference_iv"],
                    mode="lines",
                    name=f"Prior settlement IV · {prior_date.strftime('%d %b %Y')}",
                    legendrank=25,
                    legendgroup="prior-settlement",
                    meta={"legend_layer": "prior-settlement"},
                    line={"color": "#94A3B8", "width": 1.2, "dash": "dash"},
                    customdata=np.column_stack(
                        [
                            prior_reference["strike"],
                            prior_reference["option_security"],
                            prior_reference["settlement_price"],
                            prior_reference["forward"],
                        ]
                    ),
                    hovertemplate=(
                        f"<b>Prior official settlement · {prior_date.strftime('%d %b %Y')}</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[1]} · Premium <b>%{customdata[2]:.4f} "
                        + spec["price_unit"]
                        + "</b>"
                        f" · {underlying_hover_label} settle %{{customdata[3]:.3f}}"
                        "<extra></extra>"
                    ),
                ),
                secondary_y=False,
            )
        side_colors = {"C": "#2563EB", "P": "#0F766E"}
        for put_call, side_label in (("C", "Calls"), ("P", "Puts")):
            side = executable.loc[executable["put_call"].eq(put_call)].sort_values("strike")
            side["_axis_x"] = (
                side.apply(
                    chart_data._row_delta_x,
                    axis=1,
                    volatility_column="executable_iv_mid",
                    forward_column="underlying_mid",
                )
                if x_axis == chart_data.X_AXIS_DELTA
                else pd.to_numeric(side["strike"], errors="coerce")
            )
            side = side.loc[side["_axis_x"].notna()].sort_values("_axis_x")
            if side.empty:
                continue
            figure.add_trace(
                go.Scatter(
                    x=side["_axis_x"],
                    y=100.0 * side["executable_iv_bid"],
                    mode="lines",
                    line={"color": side_colors[put_call], "width": 0},
                    showlegend=False,
                    hoverinfo="skip",
                    legendgroup=f"exec-{put_call}",
                    meta={"legend_layer": "executable-band"},
                ),
                secondary_y=False,
            )
            figure.add_trace(
                go.Scatter(
                    x=side["_axis_x"],
                    y=100.0 * side["executable_iv_ask"],
                    mode="lines",
                    name=f"Executable IV band · {side_label}",
                    legendrank=12,
                    legendgroup=f"exec-{put_call}",
                    meta={"legend_layer": "executable-band"},
                    fill="tonexty",
                    fillcolor=(
                        "rgba(37,99,235,0.14)"
                        if put_call == "C"
                        else "rgba(15,118,110,0.14)"
                    ),
                    line={"color": side_colors[put_call], "width": 0.7},
                    hoverinfo="skip",
                ),
                secondary_y=False,
            )
            customdata = np.column_stack(
                [
                    side["strike"],
                    side["option_bid"],
                    side["option_mid"],
                    side["option_ask"],
                    side["underlying_bid"],
                    side["underlying_mid"],
                    side["underlying_ask"],
                    side["quote_capture_skew_ms"],
                    side.get(
                        "option_security",
                        pd.Series(
                            f"{side_label[:-1]} option", index=side.index
                        ),
                    ),
                    chart_data._hover_count_strings(side["volume"]),
                    chart_data._hover_count_strings(side["open_interest"]),
                    chart_data._hover_date_strings(side["open_interest_date"]),
                    chart_data._hover_oi_status_strings(side["open_interest_scope_status"]),
                ]
            )
            figure.add_trace(
                go.Scatter(
                    x=side["_axis_x"],
                    y=100.0 * side["executable_iv_mid"],
                    mode="lines",
                    name=f"Executable mid IV · {side_label}",
                    legendrank=10,
                    legendgroup=f"exec-{put_call}",
                    meta={
                        "legend_layer": (
                            "call-mid" if put_call == "C" else "put-mid"
                        )
                    },
                    line={"color": side_colors[put_call], "width": 1.6},
                    customdata=customdata,
                    hovertemplate=(
                        f"<b>Executable {side_label[:-1].lower()}</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[8]} · Option B/M/A "
                        "%{customdata[1]:.4f} / %{customdata[2]:.4f} / %{customdata[3]:.4f}"
                        f"<br>{underlying_hover_label} B/M/A "
                        "%{customdata[4]:.3f} / %{customdata[5]:.3f} / %{customdata[6]:.3f}"
                        "<br>Volume / OI <b>%{customdata[9]} / %{customdata[10]}</b>"
                        " · OI %{customdata[11]} (%{customdata[12]})"
                        "<br>Quote skew %{customdata[7]:.0f} ms<extra></extra>"
                    ),
                ),
                secondary_y=False,
            )
        if trade_tape is not None:
            payloads = tape_views.trade_trace_payloads(trade_tape, expiry, x_axis)
            side_colors = {"C": "#2563EB", "P": "#0F766E"}
            for put_call, side_label in (("C", "Calls"), ("P", "Puts")):
                payload = payloads[put_call]
                figure.add_trace(
                    go.Scatter(
                        x=payload["x"],
                        y=payload["y"],
                        mode="markers",
                        name=f"Trade-time IV · {side_label}",
                        legendrank=20 if put_call == "C" else 21,
                        legendgroup=f"trade-tape-{put_call}",
                        meta={
                            "role": "trade-tape",
                            "put_call": put_call,
                            "legend_layer": "trades",
                        },
                        marker={
                            "color": side_colors[put_call],
                            "size": payload["size"],
                            "symbol": payload["symbol"],
                            "line": {
                                "color": payload["line_color"],
                                "width": payload["line_width"],
                            },
                        },
                        customdata=payload["customdata"],
                        hovertemplate=(
                            f"<b>Trade-time IV · {side_label[:-1]}</b>"
                            "<br>Strike %{customdata[0]:.2f}"
                            + axis_hover
                            + " · IV <b>%{y:.2f}%</b>"
                            "<br>Trade <b>%{customdata[1]:.4f} × %{customdata[2]:,.0f}</b>"
                            " · %{customdata[3]}"
                            f"<br>{underlying_hover_label} %{{customdata[4]:.3f}}"
                            " · %{customdata[5]} · %{customdata[6]:.0f} ms"
                            "<br>Quote ages %{customdata[7]}"
                            "<br>Condition %{customdata[8]}<extra></extra>"
                        ),
                    ),
                    secondary_y=False,
                )
            matched_trades = trade_tape.loc[
                chart_data._expiry_mask(
                    trade_tape, expiry, "underlying_contract_month"
                )
                & trade_tape["trade_iv_status"].astype(str).eq("resolved")
            ].copy()
        else:
            matched_trades = raw.loc[
                raw.get("last_trade_iv_status", pd.Series(index=raw.index, dtype=object))
                .astype(str)
                .eq("resolved")
                & pd.to_numeric(
                    raw.get("last_trade_iv", pd.Series(np.nan, index=raw.index)),
                    errors="coerce",
                ).notna()
            ].copy()
            if not matched_trades.empty:
                matched_trades["_axis_x"] = (
                    matched_trades.apply(
                        chart_data._row_delta_x,
                        axis=1,
                        volatility_column="last_trade_iv",
                        forward_column="last_trade_underlying_price",
                    )
                    if x_axis == chart_data.X_AXIS_DELTA
                    else pd.to_numeric(matched_trades["strike"], errors="coerce")
                )
                matched_trades = matched_trades.loc[
                    matched_trades["_axis_x"].notna()
                ].copy()
                matched_trades["trade_time_gst"] = pd.to_datetime(
                    matched_trades["last_trade_at"], errors="coerce", utc=True
                ).dt.tz_convert("Asia/Dubai").dt.strftime("%H:%M:%S GST")
                figure.add_trace(
                    go.Scatter(
                        x=matched_trades["_axis_x"],
                        y=100.0 * matched_trades["last_trade_iv"],
                        mode="markers",
                        name="New matched trade IV",
                        legendrank=20,
                        meta={"legend_layer": "trades"},
                        marker={"color": "#F97316", "size": 9, "symbol": "diamond"},
                        customdata=np.column_stack(
                            [
                                matched_trades["strike"],
                                matched_trades["last_trade_price"],
                                matched_trades["trade_time_gst"],
                                matched_trades["last_trade_underlying_price"],
                                matched_trades["last_trade_underlying_source"].map(
                                    chart_data._trade_match_source_label
                                ),
                                matched_trades["last_trade_match_lag_ms"],
                                matched_trades["last_trade_condition_codes"].fillna("regular"),
                                matched_trades.get(
                                    "option_security",
                                    pd.Series("Option", index=matched_trades.index),
                                ),
                                chart_data._hover_count_strings(matched_trades["volume"]),
                                chart_data._hover_count_strings(matched_trades["open_interest"]),
                                chart_data._hover_date_strings(
                                    matched_trades["open_interest_date"]
                                ),
                                chart_data._hover_oi_status_strings(
                                    matched_trades["open_interest_scope_status"]
                                ),
                            ]
                        ),
                        hovertemplate=(
                            "<b>New matched trade</b>"
                            "<br>Strike %{customdata[0]:.2f}" + axis_hover
                            + " · IV <b>%{y:.2f}%</b>"
                            "<br>%{customdata[7]} · Trade %{customdata[1]:.4f}"
                            " · %{customdata[2]}"
                            f"<br>{underlying_hover_label} %{{customdata[3]:.3f}}"
                            " · %{customdata[4]} · %{customdata[5]:.0f} ms"
                            "<br>Volume / OI <b>%{customdata[8]} / %{customdata[9]}</b>"
                            " · OI %{customdata[10]} (%{customdata[11]})"
                            "<br>Condition %{customdata[6]}<extra></extra>"
                        ),
                    ),
                    secondary_y=False,
                )
    else:
        settlement_reference = chart_data._settlement_reference_smile(raw, x_axis)
        if not settlement_reference.empty:
            calibration_hover = (
                "<br>Calibration: <b>%{customdata[6]}</b>"
                if resolved_product == "BRENT"
                else ""
            )
            figure.add_trace(
                go.Scatter(
                    x=settlement_reference["display_x"],
                    y=100.0 * settlement_reference["reference_iv"],
                    mode="markers+lines",
                    name="Bloomberg settlement IV",
                    legendrank=5,
                    meta={"legend_layer": "bloomberg-settlement"},
                    opacity=0.65,
                    line={"color": "#64748B", "width": 0.9},
                    marker={
                        "color": "#64748B",
                        "line": {"width": 0},
                        "size": 2,
                        "symbol": "circle",
                    },
                    customdata=np.column_stack(
                        [
                            settlement_reference["strike"],
                            settlement_reference["option_security"],
                            settlement_reference["settlement_price"],
                            settlement_reference["volume"].map(
                                lambda value: (
                                    f"{value:,.0f}" if pd.notna(value) else "—"
                                )
                            ),
                            settlement_reference["open_interest"].map(
                                lambda value: (
                                    f"{value:,.0f}" if pd.notna(value) else "—"
                                )
                            ),
                            settlement_reference["forward"],
                            settlement_reference["calibration_status"],
                        ]
                    ),
                    hovertemplate=(
                        "<b>Settlement from Bloomberg</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[1]} · Premium <b>%{customdata[2]:.4f} "
                        + spec["price_unit"]
                        + "</b>"
                        f" · {underlying_hover_label} %{{customdata[5]:.3f}}"
                        "<br>Volume / OI <b>%{customdata[3]} / %{customdata[4]}</b>"
                        + calibration_hover
                        + "<extra></extra>"
                    ),
                ),
                secondary_y=False,
            )
    icap_settlement = pd.DataFrame()
    if not published.empty:
        if x_axis == chart_data.X_AXIS_DELTA:
            published["_axis_x"] = published.apply(
                lambda row: chart_data._display_delta(row.get("delta"), row.get("put_call")),
                axis=1,
            )
        else:
            published["_axis_x"] = pd.to_numeric(
                published["strike"], errors="coerce"
            )
        published = published.loc[published["_axis_x"].notna()].sort_values("_axis_x")
        if resolved_product == "TFO" and "source_name" in published:
            icap_mask = published["source_name"].map(chart_data._is_icap_settlement_source)
            icap_settlement = published.loc[icap_mask].copy()
            published = published.loc[~icap_mask].copy()
    surface_traces = (
        (
            icap_settlement,
            "Settlement vol surface (ICAP)",
            "icap-settlement",
            28,
            "markers",
            {"color": "#0F766E", "width": 2, "dash": "dash"},
            {
                "color": "#0F766E",
                "size": 7,
                "symbol": "diamond",
                "line": {"color": "#FFFFFF", "width": 1},
            },
        ),
        (
            published,
            f"Published {spec['published_label']} exact COB",
            "published",
            30,
            "lines",
            {"color": "#EA580C", "width": 2.2},
            None,
        ),
    )
    for nodes, name, layer, rank, mode, line, marker in surface_traces:
        if nodes.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=nodes["_axis_x"],
                y=100.0 * nodes["volatility"],
                mode=mode,
                name=name,
                legendrank=rank,
                meta={"legend_layer": layer},
                line=line,
                marker=marker,
                customdata=np.column_stack(
                    [
                        nodes["strike"],
                        nodes["put_call"],
                        nodes["delta"],
                        nodes["source_name"],
                        nodes["forward"],
                    ]
                ),
                hovertemplate=(
                    f"<b>{name}</b>"
                    + "<br>Strike %{customdata[0]:.2f}"
                    + axis_hover
                    + " · IV <b>%{y:.2f}%</b>"
                    f"<br>{underlying_hover_label} %{{customdata[4]:.3f}}"
                    " · %{customdata[1]} Δ %{customdata[2]:.3f}"
                    "<br>%{customdata[3]}<extra></extra>"
                ),
            ),
            secondary_y=False,
        )
    if not calibrated.empty:
        if x_axis == chart_data.X_AXIS_DELTA:
            calibrated["_axis_x"] = calibrated.apply(
                lambda row: chart_data._display_delta(row.get("delta"), row.get("put_call")),
                axis=1,
            )
        else:
            calibrated["_axis_x"] = pd.to_numeric(
                calibrated["strike"], errors="coerce"
            )
        calibrated = calibrated.loc[calibrated["_axis_x"].notna()].sort_values(
            "_axis_x"
        )
    if not calibrated.empty:
        metadata = market_data.calibrated_publication_metadata(calibrated)
        publication_cob = pd.to_datetime(metadata.get("cob_date"), errors="coerce")
        published_at = pd.to_datetime(
            metadata.get("published_at"), errors="coerce", utc=True
        )
        cob_label = (
            publication_cob.strftime("%d %b %Y")
            if not pd.isna(publication_cob)
            else "unknown"
        )
        published_label = (
            published_at.tz_convert("Asia/Dubai").strftime("%d %b %Y %H:%M GST")
            if not pd.isna(published_at)
            else "unavailable"
        )
        publication_id = str(metadata.get("publication_id") or "unavailable")
        surface_policies = (
            calibrated["calibration_policy_version"].dropna().astype(str).unique().tolist()
            if "calibration_policy_version" in calibrated
            else []
        )
        surface_label = chart_data._calibrated_surface_label(resolved_product, calibrated)
        figure.add_trace(
            go.Scatter(
                x=calibrated["_axis_x"],
                y=100.0 * calibrated["volatility"],
                mode="lines",
                name=f"{surface_label} · COB {cob_label}",
                legendrank=35,
                meta={"legend_layer": "calibrated"},
                opacity=0.50,
                line=(
                    {"color": "#E69525", "width": 2.2}
                    if resolved_product == "BRENT"
                    else {"color": "#7C3AED", "width": 2.2, "dash": "dash"}
                ),
                customdata=np.column_stack(
                    [
                        calibrated["strike"],
                        calibrated["delta"],
                        calibrated["forward_value"],
                        calibrated["source_name"],
                        calibrated["calibration_basis"],
                    ]
                ),
                hovertemplate=(
                    f"<b>{surface_label} publication</b>"
                    "<br>Strike %{customdata[0]:.3f}"
                    + axis_hover
                    + " · IV <b>%{y:.2f}%</b>"
                    f"<br>{underlying_hover_label} %{{customdata[2]:.3f}}"
                    " · call Δ %{customdata[1]:.3f}"
                    "<br>%{customdata[4]} · %{customdata[3]}"
                    f"<br>COB {cob_label} · Published {published_label}"
                    f"<br>Revision {publication_id}"
                    f"<br>Policy {escape(', '.join(surface_policies) or 'legacy')}<extra></extra>"
                ),
            ),
            secondary_y=False,
        )

    forward_values = pd.to_numeric(raw["underlying_price"], errors="coerce").dropna()
    if not forward_values.empty:
        figure.add_vline(
            x=(
                0.5
                if x_axis == chart_data.X_AXIS_DELTA
                else float(forward_values.iloc[0])
            ),
            line_width=1,
            line_dash="dot",
            line_color="#111827",
            name="pricing-reference",
        )

    focus_strikes = []
    if not executable.empty:
        focus_strikes.extend(
            pd.to_numeric(executable["strike"], errors="coerce").dropna()
        )
    if not prior_reference.empty:
        focus_strikes.extend(
            pd.to_numeric(prior_reference["strike"], errors="coerce").dropna()
        )
    if not matched_trades.empty:
        focus_strikes.extend(
            pd.to_numeric(matched_trades["strike"], errors="coerce").dropna()
        )
    if not published.empty:
        focus_strikes.extend(
            pd.to_numeric(published["strike"], errors="coerce").dropna()
        )
    if not settlement_reference.empty:
        # Settlement panels are an exchange-record view, so their initial range
        # includes every price-valid Bloomberg strike.
        focus_strikes.extend(
            pd.to_numeric(settlement_reference["strike"], errors="coerce").dropna()
        )
    if not forward_values.empty:
        focus_strikes.append(float(forward_values.iloc[0]))
    if focus_strikes and x_axis == chart_data.X_AXIS_STRIKE:
        focus_low = float(min(focus_strikes))
        focus_high = float(max(focus_strikes))
        focus_padding = max(0.04 * (focus_high - focus_low), axis_spacing)
        figure.update_xaxes(
            range=[max(0.0, focus_low - focus_padding), focus_high + focus_padding]
        )

    figure.update_yaxes(title_text="IV (%)", secondary_y=False)
    if is_intraday and trade_tape is not None:
        # Keep the executable smile visually fixed while the trade window moves.
        # Plotly otherwise autoranges the primary axis after every trade-trace
        # patch, which makes unchanged executable IVs appear to move.
        iv_values = []
        for trace in figure.data:
            if trace.type == "bar" or getattr(trace, "yaxis", "y") == "y2":
                continue
            iv_values.extend(
                value
                for value in pd.to_numeric(
                    pd.Series(trace.y, dtype=object), errors="coerce"
                ).dropna()
                if np.isfinite(value)
            )
        if iv_values:
            iv_low = float(min(iv_values))
            iv_high = float(max(iv_values))
            iv_padding = max(0.5, 0.05 * max(iv_high - iv_low, 1.0))
            figure.update_yaxes(
                range=[max(0.0, iv_low - iv_padding), iv_high + iv_padding],
                secondary_y=False,
            )
    figure.update_yaxes(
        title_text="Activity (contracts)",
        rangemode="tozero",
        showgrid=False,
        secondary_y=True,
    )
    if x_axis == chart_data.X_AXIS_DELTA:
        figure.update_xaxes(
            title_text="",
            range=[0.0, 1.0],
            tickmode="array",
            tickvals=[0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0],
            ticktext=[
                "0Δ put",
                "10Δ put",
                "25Δ put",
                "ATM",
                "25Δ call",
                "10Δ call",
                "0Δ call",
            ],
        )
    else:
        figure.update_xaxes(title_text="")
    expiry_trade_tape = (
        trade_tape.loc[
            chart_data._expiry_mask(trade_tape, expiry, "underlying_contract_month")
        ].copy()
        if trade_tape is not None and not trade_tape.empty
        else pd.DataFrame()
    )
    quality = chart_data._intraday_expiry_quality(raw, expiry_trade_tape, prior_reference)
    option_expirations = (
        pd.to_datetime(raw["option_expiration_date"], errors="coerce").dropna()
        if "option_expiration_date" in raw
        else pd.Series(dtype="datetime64[ns]")
    )
    if resolved_product in quote_data.QUOTE_FEED_PRODUCTS:
        for layer, label, color, symbol in (
            ("ice-bid", "ICE bid quote", "#B42318", "triangle-down"),
            ("ice-offer", "ICE offer quote", "#067647", "triangle-up"),
            ("ice-single", "ICE single quote", "#6941C6", "diamond"),
        ):
            figure.add_trace(
                go.Scatter(
                    x=[], y=[], mode="markers", name=label,
                    meta={"legend_layer": layer, "role": "ice-chat-quote"},
                    marker={"color": color, "symbol": symbol, "size": 11,
                            "line": {"color": "#ffffff", "width": 1}},
                    hovertemplate="%{text}<extra></extra>",
                    showlegend=False,
                ),
                secondary_y=False,
            )
    figure.update_layout(
        template="plotly_white",
        height=308,
        margin={"l": 58, "r": 44, "t": 18, "b": 48},
        barmode="overlay",
        hovermode="closest",
        hoverdistance=36,
        hoverlabel={
            "bgcolor": "#0F172A",
            "bordercolor": "#334155",
            "font": {
                "family": "Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif",
                "size": 11,
                "color": "#F8FAFC",
            },
            "align": "left",
            "namelength": 0,
        },
        showlegend=False,
        font={"family": "Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif", "size": 11},
        uirevision=(
            f"vol-trades-{resolved_product.lower()}-"
            f"{expiry.date().isoformat()}-{x_axis}"
        ),
        meta={
            "expiry": expiry.date().isoformat(),
            "option_expiration_date": (
                option_expirations.iloc[0].date().isoformat()
                if not option_expirations.empty
                else None
            ),
            "quality": quality,
        },
    )
    return figure


def _plot_card_graphs(cards) -> list[Any]:
    graphs = []
    for card in cards or []:
        children = getattr(card, "children", None)
        if children is None:
            continue
        if not isinstance(children, (list, tuple)):
            children = [children]
        for child in children:
            component_id = getattr(child, "id", None)
            if (
                isinstance(component_id, dict)
                and component_id.get("type")
                == "brent-vol-history-expiry-graph"
            ):
                graphs.append(child)
                break
    return graphs


def _trace_has_points(trace: Any) -> bool:
    values = getattr(trace, "x", None)
    if values is None:
        return False
    try:
        return len(values) > 0
    except TypeError:
        return True


def _trace_has_new_volume_edge(trace: Any) -> bool:
    meta = getattr(trace, "meta", None)
    if not isinstance(meta, dict) or meta.get("legend_layer") not in {
        "volume-calls",
        "volume-puts",
    }:
        return False
    marker = getattr(trace, "marker", None)
    marker_line = getattr(marker, "line", None) if marker is not None else None
    colors = getattr(marker_line, "color", None) if marker_line is not None else None
    if colors is None:
        return False
    if isinstance(colors, str):
        return colors.upper() == "#F97316"
    try:
        return any(str(color).upper() == "#F97316" for color in colors)
    except TypeError:
        return False


def _expiry_legend_contract(cards) -> dict[str, Any]:
    available = set()
    graphs = {}
    new_volume_layers = set()
    for graph in _plot_card_graphs(cards):
        figure = graph.figure
        graph_id = dict(graph.id)
        expiry = str(graph_id.get("expiry") or "")
        trace_entries = []
        for index, trace in enumerate(figure.data):
            meta = getattr(trace, "meta", None)
            layer = meta.get("legend_layer") if isinstance(meta, dict) else None
            if not layer:
                continue
            trace_entries.append({"index": index, "layer": layer})
            if _trace_has_points(trace) or layer.startswith("ice-"):
                available.add(layer)
            if _trace_has_new_volume_edge(trace):
                new_volume_layers.add(layer)
        shape_entries = []
        for index, shape in enumerate(figure.layout.shapes or ()):
            layer = getattr(shape, "name", None)
            if layer not in EXPIRY_LEGEND_LAYER_SPECS:
                continue
            shape_entries.append({"index": index, "layer": layer})
            available.add(layer)
        iv_range = getattr(figure.layout.yaxis, "range", None)
        graphs[expiry] = {
            "traces": trace_entries,
            "shapes": shape_entries,
            "iv_range": list(iv_range) if iv_range is not None else None,
            "strike_range": list(figure.layout.xaxis.range) if figure.layout.xaxis.range is not None else None,
            "generation": graph_id.get("generation"),
            "option_expiration_date": dict(figure.layout.meta or {}).get(
                "option_expiration_date"
            ),
        }
    available_layers = [
        layer for layer in EXPIRY_LEGEND_LAYER_ORDER if layer in available
    ]
    return {
        "available_layers": available_layers,
        "new_volume_layers": [
            layer for layer in EXPIRY_LEGEND_LAYER_ORDER if layer in new_volume_layers
        ],
        "graphs": graphs,
    }


def _stamp_plot_generation(cards, *, snapshot_id, product, x_axis, publication_id, icap_revision=None):
    identity = json.dumps([snapshot_id, product, x_axis, publication_id, icap_revision])
    generation = hashlib.sha256(identity.encode()).hexdigest()[:20]
    for graph in _plot_card_graphs(cards):
        graph.id = {**dict(graph.id), "generation": generation}
        graph.figure.update_layout(meta={
            **dict(graph.figure.layout.meta or {}), "generation": generation,
            "product": product, "x_axis": x_axis,
        })
    return generation


DEFAULT_HIDDEN_EXPIRY_LAYERS = frozenset({"call-mid", "put-mid"})


def _default_expiry_layers(available_layers: list[str]) -> list[str]:
    return [
        layer
        for layer in available_layers
        if layer not in DEFAULT_HIDDEN_EXPIRY_LAYERS
    ]


def _selected_expiry_layers(
    available_layers: list[str],
    current_options: list[dict[str, Any]] | None,
    current_value: list[str] | None,
) -> list[str]:
    previous_available = {
        str(option.get("value"))
        for option in (current_options or [])
        if option.get("value")
    }
    if not previous_available or current_value is None:
        return _default_expiry_layers(available_layers)
    selected = {str(value) for value in current_value}
    selected.update(
        (set(available_layers) - previous_available)
        - DEFAULT_HIDDEN_EXPIRY_LAYERS
    )
    return [layer for layer in available_layers if layer in selected]


def _apply_expiry_layer_selection(
    cards,
    contract: dict[str, Any],
    selected_layers: list[str] | None,
) -> None:
    selected = set(selected_layers or [])
    graph_contracts = contract.get("graphs") or {}
    for graph in _plot_card_graphs(cards):
        expiry = str(dict(graph.id).get("expiry") or "")
        graph_contract = graph_contracts.get(expiry) or {}
        for entry in graph_contract.get("traces") or []:
            graph.figure.data[int(entry["index"])].visible = (
                entry["layer"] in selected
            )
        for entry in graph_contract.get("shapes") or []:
            graph.figure.layout.shapes[int(entry["index"])].visible = (
                entry["layer"] in selected
            )


EXPIRY_LEGEND_LAYER_ORDER = (
    "call-mid",
    "put-mid",
    "executable-band",
    "trades",
    "ice-bid",
    "ice-offer",
    "ice-single",
    "prior-settlement",
    "bloomberg-settlement",
    "icap-settlement",
    "published",
    "calibrated",
    "pricing-reference",
    "volume-calls",
    "volume-puts",
    "open-interest-calls",
    "open-interest-puts",
)


EXPIRY_LEGEND_LAYER_SPECS = {
    "call-mid": {
        "label": "Call mid",
        "group": "iv",
        "swatch": "brent-vol-history-legend-calls",
        "description": "Show or hide executable call mid volatility",
    },
    "put-mid": {
        "label": "Put mid",
        "group": "iv",
        "swatch": "brent-vol-history-legend-puts",
        "description": "Show or hide executable put mid volatility",
    },
    "executable-band": {
        "label": "Band",
        "group": "iv",
        "swatch": "brent-vol-history-legend-executable-band",
        "description": "Show or hide executable bid-ask volatility bands",
    },
    "trades": {
        "label": "Trades",
        "group": "iv",
        "swatch": "brent-vol-history-legend-trade",
        "description": (
            "Show or hide trade-time volatility; filled markers are exact "
            "midpoints and hollow markers are prevailing midpoints"
        ),
    },
    "ice-bid": {
        "label": "ICE bid", "group": "iv",
        "swatch": "brent-vol-history-legend-ice-bid",
        "description": "Show ICE Chat bid implied volatility quotes",
    },
    "ice-offer": {
        "label": "ICE offer", "group": "iv",
        "swatch": "brent-vol-history-legend-ice-offer",
        "description": "Show ICE Chat offer implied volatility quotes",
    },
    "ice-single": {
        "label": "ICE single", "group": "iv",
        "swatch": "brent-vol-history-legend-ice-single",
        "description": "Show ICE Chat single-price implied volatility quotes",
    },
    "prior-settlement": {
        "label": "Prior settle",
        "group": "reference",
        "swatch": "brent-vol-history-legend-prior-settlement",
        "description": "Show or hide the prior official settlement smile",
    },
    "bloomberg-settlement": {
        "label": "Settlement",
        "group": "reference",
        "swatch": "brent-vol-history-legend-bloomberg-settlement",
        "description": "Settlement from Bloomberg",
    },
    "icap-settlement": {
        "label": "ICAP settlement",
        "group": "reference",
        "swatch": "brent-vol-history-legend-icap-settlement",
        "description": (
            "Show or hide the latest ICAP settlement surface on or before "
            "the selected date"
        ),
    },
    "published": {
        "label": "Published",
        "group": "reference",
        "swatch": "brent-vol-history-legend-published",
        "description": "Show or hide the exact-COB published surface",
    },
    "calibrated": {
        "label": "Calibrated",
        "group": "reference",
        "swatch": "brent-vol-history-legend-calibrated",
        "description": "Show or hide the latest governed calibrated surface",
    },
    "pricing-reference": {
        "label": "ATM / future",
        "group": "reference",
        "swatch": "brent-vol-history-legend-pricing-reference",
        "description": "Show or hide the ATM or pricing-future reference line",
    },
    "volume-calls": {
        "label": "Volume calls",
        "group": "activity",
        "swatch": "brent-vol-history-legend-volume-calls",
        "new_swatch": "brent-vol-history-legend-volume-calls-new",
        "description": "Show or hide call volume",
    },
    "volume-puts": {
        "label": "Volume puts",
        "group": "activity",
        "swatch": "brent-vol-history-legend-volume-puts",
        "new_swatch": "brent-vol-history-legend-volume-puts-new",
        "description": "Show or hide put volume",
    },
    "open-interest-calls": {
        "label": "OI calls",
        "group": "activity",
        "swatch": "brent-vol-history-legend-open-interest-calls",
        "description": "Show or hide call open interest",
    },
    "open-interest-puts": {
        "label": "OI puts",
        "group": "activity",
        "swatch": "brent-vol-history-legend-open-interest-puts",
        "description": "Show or hide put open interest",
    },
}


def _expiry_legend_options(
    available_layers: list[str] | tuple[str, ...],
    *,
    new_volume_layers: list[str] | tuple[str, ...] = (),
    product: str = market_data.PRODUCT,
    surface_label: str | None = None,
) -> list[dict[str, Any]]:
    available = set(available_layers)
    new_volume = set(new_volume_layers)
    options = []
    previous_group = None
    for layer in EXPIRY_LEGEND_LAYER_ORDER:
        if layer not in available:
            continue
        spec = EXPIRY_LEGEND_LAYER_SPECS[layer]
        group_start = previous_group is not None and spec["group"] != previous_group
        label = spec["label"]
        description = spec["description"]
        swatch = spec["swatch"]
        if layer == "calibrated" and product == "BRENT":
            swatch += " brent-vol-history-legend-calibrated-brent"
        if layer == "calibrated" and product in {"LNE", "ON"}:
            label = surface_label or "HH surface · LNE calibrated"
            description = "Show or hide the shared HH publication for this settlement date; calibrated only from LNE"
        if layer in new_volume:
            label = f"{label} · new edge"
            description = (
                f"{description}; an orange edge marks "
                "same-day volume added since the previous snapshot"
            )
            swatch = spec["new_swatch"]
        options.append(
            {
                "label": html.Span(
                    [
                        html.Span(
                            className=(
                                "brent-vol-history-legend-swatch " + swatch
                            ),
                            **{"aria-hidden": "true"},
                        ),
                        html.Span(label),
                    ],
                    className=(
                        "brent-vol-history-layer-label"
                        + (
                            " brent-vol-history-layer-group-start"
                            if group_start
                            else ""
                        )
                    ),
                    title=description,
                ),
                "value": layer,
            }
        )
        previous_group = spec["group"]
    return options


def _expiry_legend_controls(options, value):
    return [
        dcc.Checklist(
            id="brent-vol-history-expiry-layers",
            options=options,
            value=value,
            className="brent-vol-history-layer-options",
            inputClassName="brent-vol-history-layer-input",
            inline=True,
        ),
        html.Button(
            "Reset",
            id="brent-vol-history-expiry-layers-reset",
            n_clicks=0,
            className="brent-vol-history-layer-reset",
            title="Restore default chart layers",
        ),
    ]


def build_expiry_legend():
    """Return the shared, selectable layer key for every expiry chart."""
    return html.Div(
        _expiry_legend_controls([], []),
        id="brent-vol-history-expiry-legend",
        className="brent-vol-history-common-legend",
        role="group",
        **{"aria-label": "Common chart layers for all expiry panels"},
    )
