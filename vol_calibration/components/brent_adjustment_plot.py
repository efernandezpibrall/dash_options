"""Plots for Brent SVI-baseline intraday adjustments."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from vol_calibration.brent_intraday import (
    ADJUSTMENT_PARAMS,
    adjustment_values,
    select_surface_slice,
    surface_nodes,
)
from vol_calibration.components.smile_grid import create_smile_grid_figure


def _market_x(data: pd.DataFrame, x_axis: str) -> np.ndarray:
    if x_axis == "log_moneyness":
        return np.log(
            pd.to_numeric(data["strike"], errors="coerce")
            / pd.to_numeric(data["forward"], errors="coerce")
        ).to_numpy(dtype=float)
    if x_axis == "moneyness":
        return (
            pd.to_numeric(data["strike"], errors="coerce")
            / pd.to_numeric(data["forward"], errors="coerce")
        ).to_numpy(dtype=float)
    delta = pd.to_numeric(data["delta"], errors="coerce").to_numpy(dtype=float)
    return np.where(delta < 0, -delta, 1.0 - delta)


def _node_x(nodes: pd.DataFrame, x_axis: str) -> np.ndarray:
    if x_axis == "log_moneyness":
        return nodes["log_moneyness"].to_numpy(dtype=float)
    if x_axis == "moneyness":
        return np.exp(nodes["log_moneyness"].to_numpy(dtype=float))
    return 1.0 - nodes["call_delta"].to_numpy(dtype=float)


def _expiry_list(
    market_data: pd.DataFrame,
    operational_surface: pd.DataFrame,
    x_axis: str,
) -> list[pd.Timestamp]:
    market_expiries = pd.to_datetime(
        market_data.get("expiry", pd.Series(dtype="datetime64[ns]")),
        errors="coerce",
    ).dropna().dt.normalize()
    values = set(market_expiries.tolist())
    if x_axis == "delta" and operational_surface is not None and not operational_surface.empty:
        surface_expiries = pd.to_datetime(
            operational_surface["contract_date"], errors="coerce"
        ).dropna().dt.normalize()
        values.update(surface_expiries.tolist())
    return sorted(values)


def _params_for_expiry(params: pd.DataFrame, expiry) -> dict[str, float] | None:
    if params is None or params.empty or "expiry" not in params.columns:
        return None
    target = pd.Timestamp(expiry).to_period("M")
    def to_period(value):
        if isinstance(value, str):
            try:
                return pd.Period(datetime.strptime(value.strip(), "%b-%y"), freq="M")
            except ValueError:
                pass
        parsed = pd.to_datetime(value, errors="coerce")
        return pd.Period(parsed, freq="M") if pd.notna(parsed) else pd.NaT

    periods = params["expiry"].apply(to_period)
    matching = params.loc[periods == target]
    if matching.empty:
        return None
    row = matching.iloc[0]
    result = {}
    for name in ADJUSTMENT_PARAMS:
        value = pd.to_numeric(pd.Series([row.get(name)]), errors="coerce").iloc[0]
        result[name] = float(value) if np.isfinite(value) else 0.0
    return result


def create_brent_adjustment_grid(
    market_data: pd.DataFrame,
    params: pd.DataFrame,
    x_axis: str,
    selected_row: int | None,
    operational_surface: pd.DataFrame,
    operational_metadata: dict | None,
) -> go.Figure:
    market_data = market_data.copy() if market_data is not None else pd.DataFrame()
    eligible_mask = market_data.get(
        "calibration_eligible", pd.Series(True, index=market_data.index)
    ).fillna(False)
    eligible = market_data.loc[eligible_mask].copy()
    excluded = market_data.loc[~eligible_mask].copy()
    base_surface = operational_surface if x_axis == "delta" else pd.DataFrame()
    figure = create_smile_grid_figure(
        eligible,
        pd.DataFrame(),
        x_axis,
        selected_row,
        operational_surface=base_surface,
        operational_metadata=operational_metadata,
    )
    for trace in figure.data:
        if trace.name == "Market":
            trace.name = "Eligible observed"

    expiries = _expiry_list(market_data, operational_surface, x_axis)
    shown = {trace.name for trace in figure.data if trace.showlegend is not False}
    num_cols = 3
    for index, expiry in enumerate(expiries):
        row = index // num_cols + 1
        column = index % num_cols + 1
        exp_market = market_data[
            pd.to_datetime(market_data["expiry"], errors="coerce").dt.to_period("M")
            == expiry.to_period("M")
        ].copy()
        exp_eligible_mask = exp_market.get(
            "calibration_eligible", pd.Series(True, index=exp_market.index)
        ).fillna(False)
        exp_eligible = exp_market[exp_eligible_mask]
        exp_excluded = exp_market.drop(exp_eligible.index)

        if not exp_excluded.empty:
            finite = np.isfinite(pd.to_numeric(exp_excluded["iv"], errors="coerce"))
            exp_excluded = exp_excluded.loc[finite].copy()
        if not exp_excluded.empty:
            customdata = np.column_stack(
                [
                    exp_excluded.get("exclusion_reason", "").astype(str),
                    exp_excluded.get("iv_source", "").astype(str),
                ]
            )
            name = "Excluded reference"
            figure.add_trace(
                go.Scatter(
                    x=_market_x(exp_excluded, x_axis),
                    y=pd.to_numeric(exp_excluded["iv"], errors="coerce") * 100,
                    mode="markers",
                    marker={
                        "size": 6,
                        "color": "rgba(108,117,125,0.35)",
                        "symbol": "x",
                    },
                    name=name,
                    showlegend=name not in shown,
                    customdata=customdata,
                    hovertemplate=(
                        "Excluded observed quote<br>"
                        "X: %{x:.3f}<br>IV reference: %{y:.2f}%<br>"
                        "Reason: %{customdata[0]}<br>IV source: %{customdata[1]}"
                        "<extra></extra>"
                    ),
                ),
                row=row,
                col=column,
            )
            shown.add(name)

        if exp_eligible.empty:
            continue
        surface_slice = select_surface_slice(operational_surface, expiry)
        if surface_slice.empty:
            continue
        forward = float(exp_eligible["forward"].iloc[0])
        dte = float(exp_eligible["dte"].iloc[0])
        try:
            nodes = surface_nodes(surface_slice, forward, dte)
        except Exception:
            continue
        node_x = _node_x(nodes, x_axis)
        order = np.argsort(node_x)

        if x_axis != "delta":
            name = "Official SVI baseline"
            figure.add_trace(
                go.Scatter(
                    x=node_x[order],
                    y=nodes["baseline_iv"].to_numpy(dtype=float)[order] * 100,
                    mode="lines+markers",
                    line={"color": "#0f766e", "width": 2, "dash": "dash"},
                    marker={"size": 6, "symbol": "diamond"},
                    name=name,
                    showlegend=name not in shown,
                ),
                row=row,
                col=column,
            )
            shown.add(name)

        adjustment = _params_for_expiry(params, expiry)
        if adjustment is None or not any(abs(value) > 1e-12 for value in adjustment.values()):
            continue
        adjusted_iv = nodes["baseline_iv"].to_numpy(dtype=float) + adjustment_values(
            adjustment, nodes["log_moneyness"]
        )
        name = "Intraday adjusted"
        figure.add_trace(
            go.Scatter(
                x=node_x[order],
                y=adjusted_iv[order] * 100,
                mode="lines+markers",
                line={"color": "#fd7e14", "width": 3},
                marker={"size": 5},
                name=name,
                showlegend=name not in shown,
            ),
            row=row,
            col=column,
        )
        shown.add(name)
    return figure


def create_brent_adjustment_comparison(
    market_expiry: pd.DataFrame,
    surface_slice: pd.DataFrame,
    current_params: Mapping[str, float],
    candidate_params: Mapping[str, float],
    final_params: Mapping[str, float],
    expiry_label: str,
    x_axis: str,
) -> go.Figure:
    figure = go.Figure()
    if market_expiry is None or market_expiry.empty or surface_slice is None or surface_slice.empty:
        return figure
    eligible_mask = market_expiry.get(
        "calibration_eligible", pd.Series(False, index=market_expiry.index)
    ).fillna(False)
    eligible = market_expiry.loc[eligible_mask].copy()
    excluded = market_expiry.loc[~eligible_mask].copy()
    if eligible.empty:
        return figure
    forward = float(eligible["forward"].iloc[0])
    dte = float(eligible["dte"].iloc[0])
    nodes = surface_nodes(surface_slice, forward, dte)

    for data, name, color, symbol in (
        (excluded, "Excluded reference", "rgba(108,117,125,0.35)", "x"),
        (eligible, "Eligible observed", "#007bff", "circle"),
    ):
        if data.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=_market_x(data, x_axis),
                y=pd.to_numeric(data["iv"], errors="coerce") * 100,
                mode="markers",
                marker={"size": 7, "color": color, "symbol": symbol},
                name=name,
            )
        )
    node_x = _node_x(nodes, x_axis)
    order = np.argsort(node_x)
    for params, name, color, dash, width in (
        (current_params, "Official SVI baseline", "#6c757d", "dash", 2),
        (candidate_params, "Candidate", "#0d6efd", "dash", 2),
        (final_params, "Final", "#198754", "solid", 3),
    ):
        adjusted = nodes["baseline_iv"].to_numpy(dtype=float) + adjustment_values(
            params, nodes["log_moneyness"]
        )
        figure.add_trace(
            go.Scatter(
                x=node_x[order],
                y=adjusted[order] * 100,
                mode="lines",
                line={"color": color, "dash": dash, "width": width},
                name=name,
            )
        )
    axis_labels = {
        "delta": "Delta",
        "moneyness": "Moneyness (K/F)",
        "log_moneyness": "Log-Moneyness (x)",
    }
    figure.update_layout(
        title=f"SVI-Anchored Adjustment - {expiry_label}",
        xaxis_title=axis_labels.get(x_axis, "Delta"),
        yaxis_title="IV (%)",
        height=350,
        margin={"t": 40, "b": 40, "l": 50, "r": 20},
        legend={"orientation": "h", "y": 1.1, "x": 0.5, "xanchor": "center"},
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    figure.update_xaxes(showgrid=True, gridcolor="lightgray")
    figure.update_yaxes(showgrid=True, gridcolor="lightgray")
    return figure
