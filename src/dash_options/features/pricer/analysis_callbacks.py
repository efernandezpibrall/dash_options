"""Pricer payoff and sensitivity analysis callbacks."""

from __future__ import annotations

import math
import plotly.graph_objects as go

from dash import (
    Input,
    Output,
    State,
    callback,
    no_update,
)
from datetime import date
from dash_options.features.pricer.structures import (
    SCHEMA_VERSION,
    StructureValidationError,
    correlation_sensitivity_series,
    expiration_extension_series,
    parallel_volatility_series,
    payoff_series,
    rate_sensitivity_series,
    time_decay_series,
)
from dash_options.features.pricer.charts import _empty_pricer_figure, _line_figure, _pricer_axis, _style_pricer_figure
from dash_options.features.pricer.state import parse_date


@callback(
    [
        Output("valuation-date", "min_date_allowed"),
        Output("valuation-date", "max_date_allowed"),
        Output("valuation-date", "date"),
    ],
    Input("pricer-calculation-store", "data"),
    State("valuation-date", "date"),
)
def sync_payoff_valuation_limit(snapshot, valuation_date):
    if not snapshot or snapshot.get("schema_version") != SCHEMA_VERSION:
        return date.today(), None, no_update
    minimum = snapshot["calculation_date"]
    maximum = (
        snapshot["context"].get("first_expiration_date")
        or snapshot["context"]["expiration_date"]
    )
    if snapshot["model"] == "asian76":
        maximum = snapshot["context"]["averaging_start_date"]
    if valuation_date:
        selected = parse_date(valuation_date)
        if selected < parse_date(minimum) or selected > parse_date(maximum):
            return minimum, maximum, None
    return minimum, maximum, no_update


@callback(
    Output("payoff-chart", "figure"),
    [
        Input("pricer-calculation-store", "data"),
        Input("valuation-date", "date"),
        Input("price-range-slider", "value"),
        Input("pricer-futures-lots", "value"),
    ],
)
def update_payoff_chart(calculation_store, valuation_date, price_range, futures_lots=0, option_type=None):
    del option_type
    if (
        not calculation_store
        or calculation_store.get("schema_version") != SCHEMA_VERSION
    ):
        return _empty_pricer_figure(
            "Calculate the structure first.",
            "Underlying price",
            "P&L after premium",
        )
    try:
        series = payoff_series(
            calculation_store,
            valuation_date=valuation_date,
            price_range=price_range or 50,
        )
    except StructureValidationError as exc:
        return _empty_pricer_figure(
            str(exc),
            "Underlying price",
            "P&L after premium",
        )
    premium_cost = float(calculation_store["totals"]["trade_value"])
    currency = calculation_store["context"]["trade_currency"]
    fig = go.Figure()
    if series["at_expiration"]:
        fig.add_trace(
            go.Scatter(
                x=series["x"],
                y=[value - premium_cost for value in series["payoff"]],
                mode="lines",
                name="P&L at expiration",
                line={"color": "#2563eb", "width": 2.5},
            )
        )
    else:
        fig.add_trace(
            go.Scatter(
                x=series["x"],
                y=[value - premium_cost for value in series["theoretical"]],
                mode="lines",
                name="P&L at selected valuation",
                line={"color": "#2563eb", "width": 2.5},
            )
        )
        fig.add_trace(
            go.Scatter(
                x=series["x"],
                y=[value - premium_cost for value in series["payoff"]],
                mode="lines",
                name=(
                    "Parallel strip intrinsic benchmark after premium"
                    if calculation_store["context"].get("delivery_components")
                    else "P&L at expiration"
                ),
                line={"color": "#dc2626", "width": 1.8, "dash": "dash"},
            )
        )
        fig.add_trace(
            go.Scatter(
                x=[series["current_underlying"]],
                y=[series["current_value"] - premium_cost],
                mode="markers",
                name="Selected valuation P&L",
                marker={"color": "#15803d", "size": 10, "symbol": "star"},
            )
        )
    fig.update_layout(
        xaxis=_pricer_axis(series["xaxis_title"]),
        yaxis=_pricer_axis(f"P&L ({currency})"),
    )
    premium_direction = "debit" if premium_cost >= 0 else "credit"
    fig.add_annotation(
        text=f"Net model premium: {abs(premium_cost):,.2f} {currency} {premium_direction}",
        x=0, y=1.08, xref="paper", yref="paper", xanchor="left",
        showarrow=False,
    )
    try:
        lots = 0.0 if futures_lots is None else float(futures_lots)
        if not math.isfinite(lots):
            raise ValueError
        if lots:
            # Futures lots are an independent position, not option-structure multiples.
            sizing = calculation_store["sizing"]
            futures_scale = (
                sizing["contract_multiplier"] * sizing["price_currency_conversion_factor"]
            )
            entry_price = series["current_underlying"]
            futures_values = [lots * futures_scale * (price - entry_price) for price in series["x"]]
            if not all(math.isfinite(value) for value in futures_values):
                raise ValueError
            fig.add_trace(
                go.Scatter(
                    x=series["x"], y=futures_values, mode="lines",
                    name=f"Futures P&L ({lots:g} lots @ {entry_price:g})",
                    line={"color": "#7c3aed", "width": 2, "dash": "dash"},
                )
            )
    except (TypeError, ValueError, OverflowError):
        fig.add_annotation(
            text="Enter a finite futures lot count<br>within the supported payoff range.",
            x=1, y=0.98, xref="paper", yref="paper", xanchor="right", yanchor="top",
            showarrow=False, font={"color": "#b91c1c", "size": 10},
        )
    fig = _style_pricer_figure(fig)
    fig.update_layout(margin={"l": 60, "r": 20, "t": 44, "b": 76})
    return fig


@callback(
    [
        Output("volatility-chart", "figure"),
        Output("rate-chart", "figure"),
        Output("time-chart", "figure"),
        Output("extension-chart", "figure"),
        Output("correlation-chart", "figure"),
    ],
    Input("pricer-calculation-store", "data"),
)
def render_structure_sensitivity_charts(snapshot):
    empty = _empty_pricer_figure("Calculate the structure first.")
    if not snapshot or snapshot.get("schema_version") != SCHEMA_VERSION:
        return empty, empty, empty, empty, empty
    try:
        vol = parallel_volatility_series(snapshot)
        zero_index = min(
            range(len(vol["shifts_percentage_points"])),
            key=lambda index: abs(vol["shifts_percentage_points"][index]),
        )
        vol_fig = _line_figure(
            vol["shifts_percentage_points"],
            vol["values"],
            "Parallel input-volatility shift (percentage points)",
            marker_x=vol["shifts_percentage_points"][zero_index],
            marker_y=vol["values"][zero_index],
        )
    except Exception as exc:
        vol_fig = _empty_pricer_figure(
            f"Volatility sensitivity unavailable ({type(exc).__name__})."
        )

    if snapshot["model"] == "kirk":
        rate_fig = _empty_pricer_figure(
            "Not applicable: the current Kirk implementation is undiscounted.",
            "Risk-free rate",
            "Trade value",
        )
    elif snapshot["context"].get("margin_style") == "futures_style":
        rate_fig = _empty_pricer_figure(
            "Not applicable: the futures-style premium convention is undiscounted.",
            "Risk-free rate",
            "Trade value",
        )
    else:
        try:
            rate = rate_sensitivity_series(snapshot)
            base_rate = snapshot["context"]["rate"]
            base_index = min(
                range(len(rate["rates"])),
                key=lambda index: abs(rate["rates"][index] - base_rate),
            )
            rate_fig = _line_figure(
                rate["rates"],
                rate["values"],
                "Risk-free rate",
                marker_x=rate["rates"][base_index],
                marker_y=rate["values"][base_index],
            )
            rate_fig.update_xaxes(tickformat=".1%")
        except Exception as exc:
            rate_fig = _empty_pricer_figure(
                f"Rate sensitivity unavailable ({type(exc).__name__})."
            )

    try:
        decay = time_decay_series(snapshot)
        time_fig = _line_figure(
            decay["dates"],
            decay["values"],
            "Valuation date",
            marker_x=decay["dates"][0],
            marker_y=decay["values"][0],
            annotation=(
                "Averaging starts; realized fixings are required afterward."
                if decay["truncated_at_averaging_start"]
                else None
            ),
        )
    except Exception as exc:
        time_fig = _empty_pricer_figure(
            f"Time decay unavailable ({type(exc).__name__})."
        )

    if snapshot["context"].get("delivery_components"):
        extension_fig = _empty_pricer_figure(
            "Not applicable: every strip month has a governed TFO expiry.",
            "Expiration date",
            "Trade value",
        )
    else:
        try:
            extension = expiration_extension_series(snapshot)
            base_index = extension["dates"].index(extension["base_expiration"])
            extension_fig = _line_figure(
                extension["dates"],
                extension["values"],
                "Expiration date",
                marker_x=extension["base_expiration"],
                marker_y=extension["values"][base_index],
            )
        except Exception as exc:
            extension_fig = _empty_pricer_figure(
                f"Expiration sensitivity unavailable ({type(exc).__name__})."
            )

    if snapshot["model"] != "kirk":
        correlation_fig = _empty_pricer_figure(
            "Correlation sensitivity is only available for Kirk structures.",
            "Correlation",
            "Trade value",
        )
    else:
        try:
            correlation = correlation_sensitivity_series(snapshot)
            base_rho = snapshot["context"]["correlation"]
            base_index = min(
                range(len(correlation["correlations"])),
                key=lambda index: abs(
                    correlation["correlations"][index] - base_rho
                ),
            )
            correlation_fig = _line_figure(
                correlation["correlations"],
                correlation["values"],
                "Correlation",
                marker_x=correlation["correlations"][base_index],
                marker_y=correlation["values"][base_index],
            )
        except Exception as exc:
            correlation_fig = _empty_pricer_figure(
                f"Correlation sensitivity unavailable ({type(exc).__name__})."
            )
    return vol_fig, rate_fig, time_fig, extension_fig, correlation_fig
