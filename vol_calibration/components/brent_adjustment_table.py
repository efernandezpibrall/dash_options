"""Brent-specific table for SVI-relative intraday adjustments."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from dash import dash_table, html

from vol_calibration.brent_intraday import ADJUSTMENT_PARAMS


def create_brent_adjustment_table() -> html.Div:
    columns = [
        {"id": "expiry", "name": "Expiry", "type": "text", "editable": False},
        {"id": "atm_shift", "name": "ATM shift", "type": "numeric", "editable": False},
        {"id": "skew_shift", "name": "Skew shift", "type": "numeric", "editable": False},
        {
            "id": "put_curvature_shift",
            "name": "Put curvature",
            "type": "numeric",
            "editable": False,
        },
        {
            "id": "call_curvature_shift",
            "name": "Call curvature",
            "type": "numeric",
            "editable": False,
        },
        {
            "id": "eligible_points",
            "name": "Eligible",
            "type": "numeric",
            "editable": False,
        },
        {
            "id": "excluded_points",
            "name": "Excluded",
            "type": "numeric",
            "editable": False,
        },
        {
            "id": "validation",
            "name": "Checks",
            "type": "text",
            "editable": False,
        },
        {"id": "rmse", "name": "RMSE", "type": "text", "editable": False},
    ]
    table = dash_table.DataTable(
        id="brent-param-table",
        columns=[
            {
                **column,
                **(
                    {"format": {"specifier": ".5f"}}
                    if column["type"] == "numeric"
                    and column["id"] in ADJUSTMENT_PARAMS
                    else {}
                ),
            }
            for column in columns
        ],
        data=[],
        editable=False,
        row_selectable="single",
        selected_rows=[],
        page_action="none",
        fixed_rows={"headers": True},
        tooltip_header={
            "atm_shift": "Parallel volatility shift relative to the official SVI baseline.",
            "skew_shift": "Linear residual slope in log-moneyness.",
            "put_curvature_shift": "Quadratic residual adjustment below the forward.",
            "call_curvature_shift": "Quadratic residual adjustment above the forward.",
            "eligible_points": "American futures-style, OI-qualified, body strikes used by calibration.",
            "excluded_points": "Observed reference quotes shown on charts but assigned zero calibration weight.",
            "validation": "Discrete butterfly and adjacent-expiry total-variance checks.",
            "rmse": "Unweighted error versus eligible observed strikes.",
        },
        tooltip_duration=None,
        style_table={"height": "400px", "overflowY": "auto", "overflowX": "auto"},
        style_header={
            "backgroundColor": "#343a40",
            "color": "white",
            "fontWeight": "bold",
            "textAlign": "center",
            "padding": "10px 5px",
        },
        style_cell={
            "textAlign": "center",
            "padding": "8px 5px",
            "minWidth": "80px",
            "whiteSpace": "nowrap",
        },
        style_cell_conditional=[
            {"if": {"column_id": "expiry"}, "textAlign": "left", "fontWeight": "bold"},
            {"if": {"column_id": "validation"}, "fontWeight": "bold"},
            {"if": {"column_id": "rmse"}, "fontWeight": "bold"},
        ],
        style_data_conditional=[
            {
                "if": {"filter_query": '{validation} = "Pass"', "column_id": "validation"},
                "backgroundColor": "#d4edda",
                "color": "#155724",
            },
            {
                "if": {"filter_query": '{validation} = "Blocked"', "column_id": "validation"},
                "backgroundColor": "#fff3cd",
                "color": "#856404",
            },
            {
                "if": {"filter_query": '{validation} = "Fail"', "column_id": "validation"},
                "backgroundColor": "#f8d7da",
                "color": "#721c24",
            },
            {"if": {"state": "selected"}, "backgroundColor": "rgba(13, 110, 253, 0.12)"},
            {"if": {"row_index": "odd"}, "backgroundColor": "#f8f9fa"},
        ],
    )
    return html.Div(table, className="parameter-table-container")


def format_brent_adjustment_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    output = frame.copy()
    output["expiry"] = pd.to_datetime(output["expiry"], errors="coerce").dt.strftime("%b-%y")
    output["rmse"] = output["rmse"].apply(
        lambda value: f"{float(value) * 100:.2f}%" if np.isfinite(value) else ""
    )
    columns = [
        "expiry",
        *ADJUSTMENT_PARAMS,
        "eligible_points",
        "excluded_points",
        "validation",
        "rmse",
        "message",
    ]
    for column in columns:
        if column not in output.columns:
            output[column] = ""
    return output[columns].to_dict("records")


def parse_brent_adjustment_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    for column in ADJUSTMENT_PARAMS:
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    if "rmse" in frame.columns:
        frame["rmse"] = frame["rmse"].apply(
            lambda value: (
                float(value.replace("%", "")) / 100.0
                if isinstance(value, str) and "%" in value
                else value
            )
        )
    return frame
