"""Compact presentation of paired broker quotes; consumes saved valuations."""

import pandas as pd
import plotly.graph_objects as go


RANGE_LAYER = "ice-range"


def quote_age_label(observed_at, cutoff_at):
    observed = pd.to_datetime(observed_at, errors="coerce", utc=True)
    cutoff = pd.to_datetime(cutoff_at, errors="coerce", utc=True)
    if pd.isna(observed) or pd.isna(cutoff) or observed > cutoff:
        return "Age unavailable"
    minutes = int((cutoff - observed).total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m old" if minutes >= 60 else f"{minutes}m old"


def range_visible(selected):
    return {"ice-bid", "ice-offer"}.issubset(set(selected))


def empty_range_trace():
    return go.Scatter(
        x=[], y=[], text=[], mode="lines", name="ICE broker bid–offer range",
        meta={"legend_layer": RANGE_LAYER, "role": "ice-chat-range"},
        line={"color": "#64748B", "width": 2}, connectgaps=False,
        hovertemplate="%{text}<extra></extra>", showlegend=False,
    )
