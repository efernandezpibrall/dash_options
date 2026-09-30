"""Pricer chart presentation and shared figure builders."""

from __future__ import annotations

import plotly.graph_objects as go

from dash import (
    dcc,
    html,
)
from .constants import (
    PRICER_CHART_AXIS,
    PRICER_CHART_FONT,
    PRICER_CHART_GRID,
    PRICER_CHART_MUTED,
    PRICER_CHART_TEXT,
    PRICER_GRAPH_CONFIG,
)


def _pricer_axis(title="", **overrides):
    axis = {
        "title": {"text": title, "font": {"size": 11, "color": PRICER_CHART_MUTED}},
        "showgrid": True,
        "gridcolor": PRICER_CHART_GRID,
        "gridwidth": 1,
        "zeroline": False,
        "linecolor": PRICER_CHART_AXIS,
        "linewidth": 1,
        "tickfont": {"size": 10, "color": PRICER_CHART_MUTED},
        "ticks": "outside",
        "ticklen": 3,
        "automargin": True,
    }
    axis.update(overrides)
    return axis


def _style_pricer_figure(fig, height=400):
    fig.update_layout(
        title={"text": ""},
        font={"family": PRICER_CHART_FONT, "size": 11, "color": PRICER_CHART_TEXT},
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
        margin={"l": 60, "r": 20, "t": 18, "b": 76},
        hovermode="x unified",
        hoverlabel={
            "bgcolor": "rgba(255, 255, 255, 0.96)",
            "bordercolor": "rgba(148, 163, 184, 0.45)",
            "font": {
                "size": 11,
                "color": PRICER_CHART_TEXT,
                "family": PRICER_CHART_FONT,
            },
            "align": "left",
        },
        legend={
            "orientation": "h",
            "yanchor": "top",
            "y": -0.18,
            "xanchor": "center",
            "x": 0.5,
            "font": {"size": 9, "color": PRICER_CHART_MUTED},
        },
        height=height,
        transition={"duration": 160, "easing": "cubic-in-out"},
        uirevision="pricer-structure",
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor=PRICER_CHART_GRID,
        linecolor=PRICER_CHART_AXIS,
        tickfont={"size": 10, "color": PRICER_CHART_MUTED},
        title_font={"size": 11, "color": PRICER_CHART_MUTED},
        automargin=True,
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor=PRICER_CHART_GRID,
        linecolor=PRICER_CHART_AXIS,
        tickfont={"size": 10, "color": PRICER_CHART_MUTED},
        title_font={"size": 11, "color": PRICER_CHART_MUTED},
        automargin=True,
        zeroline=True,
        zerolinecolor="rgba(71, 85, 105, 0.42)",
        zerolinewidth=1,
    )
    return fig


def _empty_pricer_figure(message, xaxis_title="", yaxis_title="Trade value"):
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"size": 13, "color": PRICER_CHART_MUTED},
    )
    fig.update_layout(
        xaxis=_pricer_axis(xaxis_title),
        yaxis=_pricer_axis(yaxis_title),
    )
    return _style_pricer_figure(fig)


def _build_pricer_chart_card(graph_id, title, empty_message, class_name=None):
    classes = ["pricer-chart-card"]
    if class_name:
        classes.append(class_name)
    return html.Section(
        [
            html.H3(title, className="pricer-chart-card-title"),
            dcc.Loading(
                dcc.Graph(
                    id=graph_id,
                    figure=_empty_pricer_figure(empty_message),
                    config=PRICER_GRAPH_CONFIG,
                    className="pricer-chart-graph",
                ),
                type="circle",
            ),
        ],
        className=" ".join(classes),
        **{"aria-label": title},
    )


def _line_figure(x, y, x_title, *, marker_x=None, marker_y=None, annotation=None):
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x,
            y=y,
            mode="lines",
            name="Total structure value",
            line={"color": "#2563eb", "width": 2.5},
        )
    )
    if marker_x is not None and marker_y is not None:
        fig.add_trace(
            go.Scatter(
                x=[marker_x],
                y=[marker_y],
                mode="markers",
                name="Current structure",
                marker={"color": "#dc2626", "size": 9, "symbol": "star"},
            )
        )
    if annotation:
        fig.add_annotation(
            text=annotation,
            x=0.01,
            y=0.99,
            xref="paper",
            yref="paper",
            xanchor="left",
            yanchor="top",
            showarrow=False,
            bgcolor="rgba(255,255,255,0.9)",
            bordercolor="rgba(148,163,184,0.5)",
            font={"size": 10, "color": PRICER_CHART_MUTED},
        )
    fig.update_layout(
        xaxis=_pricer_axis(x_title),
        yaxis=_pricer_axis("Trade value"),
    )
    return _style_pricer_figure(fig)
