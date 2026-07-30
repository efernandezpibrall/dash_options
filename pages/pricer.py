"""Trader-facing multi-leg option structure pricer."""

from __future__ import annotations

import copy
import datetime as dt
import math
from datetime import date, timedelta

import dash_ag_grid as dag
import numpy as np
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from options.options_library import asian_76, black_76
from pricer_structure import (
    DEFAULT_ASSET,
    DEFAULT_STRUCTURE_TYPE,
    MAX_LEGS,
    MODEL_LABELS,
    SCHEMA_VERSION,
    SUPPORTED_ASSETS,
    SUPPORTED_STRUCTURE_TYPES,
    StructureValidationError,
    calculate_structure,
    correlation_sensitivity_series,
    count_business_days,
    default_context,
    default_draft,
    default_leg,
    expiration_extension_series,
    parallel_volatility_series,
    payoff_series,
    rate_sensitivity_series,
    time_decay_series,
    volatility_adjustment,
)


option_types = [
    {"label": "Commodity Options (Black-76)", "value": "black76"},
    {"label": "Average Price Options (Asian-76)", "value": "asian76"},
    {"label": "Spread Options (Kirk)", "value": "kirk"},
]
asset_options = [{"label": asset, "value": asset} for asset in SUPPORTED_ASSETS]
structure_type_options = [
    {"label": structure_type, "value": structure_type}
    for structure_type in SUPPORTED_STRUCTURE_TYPES
]

MAX_PRICER_DECIMALS = 20
PRICER_CHART_FONT = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
PRICER_CHART_TEXT = "#0f172a"
PRICER_CHART_MUTED = "#64748b"
PRICER_CHART_GRID = "rgba(148, 163, 184, 0.18)"
PRICER_CHART_AXIS = "#94a3b8"
PRICER_GRAPH_CONFIG = {
    "displayModeBar": "hover",
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}


def parse_date(date_str, default_date=None):
    if not date_str:
        return default_date or date.today() + timedelta(days=365)
    if isinstance(date_str, dt.datetime):
        return date_str.date()
    if isinstance(date_str, dt.date):
        return date_str
    try:
        return dt.datetime.strptime(str(date_str).split("T", 1)[0], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default_date or date.today() + timedelta(days=365)


def _get_pricer_triggered_id():
    try:
        return ctx.triggered_id
    except Exception:
        return None


def _normalize_pricer_number_text(value):
    text = str(value).strip().replace(" ", "")
    if not text or "+" in text or "-" in text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    if text.count(".") > 1:
        return None
    integer_part, separator, decimal_part = text.partition(".")
    if integer_part and not integer_part.isdigit():
        return None
    if separator and decimal_part and not decimal_part.isdigit():
        return None
    if separator and len(decimal_part) > MAX_PRICER_DECIMALS:
        return None
    if not integer_part and not decimal_part:
        return None
    return text


def _coerce_pricer_float(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = _normalize_pricer_number_text(value)
        if value is None:
            return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


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


def _build_pricer_message(message, tone="neutral"):
    return html.Div(
        message,
        className=f"pricer-empty-state pricer-empty-state-{tone}",
        role="status" if tone != "danger" else "alert",
    )


def _build_pricer_result_card(
    label,
    value,
    detail=None,
    tone="neutral",
    *,
    detail_on_hover=False,
):
    detail_id = (
        f"pricer-result-card-{tone}-detail"
        if detail and detail_on_hover
        else None
    )
    classes = ["pricer-result-card", f"pricer-result-card-{tone}"]
    if detail_id:
        classes.append("pricer-result-card-has-hover-detail")
    return html.Div(
        [
            html.Div(label, className="pricer-result-card-label"),
            html.Div(value, className="pricer-result-card-value"),
            (
                html.Div(
                    detail,
                    id=detail_id,
                    className=(
                        "pricer-result-card-detail "
                        "pricer-result-card-detail-hover"
                    ),
                    role="tooltip",
                )
                if detail_id
                else (
                    html.Div(detail, className="pricer-result-card-detail")
                    if detail
                    else None
                )
            ),
        ],
        className=" ".join(classes),
        tabIndex=0 if detail_id else None,
        **({"aria-describedby": detail_id} if detail_id else {}),
    )


def _build_pricer_section_header(title, actions=None, *, heading_level=2):
    heading_component = html.H1 if heading_level == 1 else html.H2
    return html.Div(
        [
            heading_component(
                title,
                className="section-title-inline pricer-section-title",
            ),
            html.Div(actions or [], className="pricer-section-actions"),
        ],
        className="pricer-section-header",
    )


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


def _build_pricer_field(label, control, class_name=None, hint=None):
    classes = ["pricer-field"]
    if class_name:
        classes.append(class_name)
    return html.Div(
        [
            html.Label(label, className="pricer-field-label"),
            control,
            html.Span(hint, className="pricer-field-hint") if hint else None,
        ],
        className=" ".join(classes),
    )


def _build_pricer_number_input(
    input_id,
    value,
    *,
    minimum=None,
    maximum=None,
    step=None,
    persistence_key=None,
):
    resolved_step = "any" if step is None else step
    resolved_persistence = persistence_key or True
    if persistence_key and resolved_step == "any":
        resolved_persistence = f"{persistence_key}-step-any-v2"
    return dcc.Input(
        id=input_id,
        type="number",
        value=value,
        min=minimum,
        max=maximum,
        step=resolved_step,
        debounce=False,
        persistence=resolved_persistence,
        persistence_type="session",
        className="pricer-number-input",
    )


def _build_pricer_date_picker(
    picker_id,
    value,
    *,
    minimum=None,
    allow_past=False,
    persistence_key=None,
):
    resolved_minimum = minimum
    if resolved_minimum is None and not allow_past:
        resolved_minimum = date.today()
    return dcc.DatePickerSingle(
        id=picker_id,
        min_date_allowed=resolved_minimum,
        initial_visible_month=parse_date(value, date.today()),
        date=value,
        display_format="YYYY-MM-DD",
        persistence=persistence_key or True,
        persistence_type="session",
        className="pricer-date-picker",
    )


def _context_id(model, param, is_date=False):
    return {
        "type": "pricer-context-date" if is_date else "pricer-context-param",
        "model": model,
        "param": param,
    }


def _build_context_form(model):
    defaults = default_context(model, date.today())
    fields = []
    if model == "black76":
        fields.extend(
            [
                _build_pricer_field(
                    "Forward price",
                    _build_pricer_number_input(
                        _context_id(model, "forward"),
                        defaults["forward"],
                        minimum=0.01,
                        persistence_key="pricer-black76-forward",
                    ),
                ),
                _build_pricer_field(
                    "Option expiration",
                    _build_pricer_date_picker(
                        _context_id(model, "expiration_date", True),
                        defaults["expiration_date"],
                        allow_past=True,
                        persistence_key="pricer-black76-expiration",
                    ),
                ),
                _build_pricer_field(
                    "Contract expiration",
                    _build_pricer_date_picker(
                        _context_id(model, "contract_expiration_date", True),
                        defaults["contract_expiration_date"],
                        allow_past=True,
                        persistence_key="pricer-black76-contract-expiration",
                    ),
                ),
                _build_pricer_field(
                    "Risk-free rate",
                    _build_pricer_number_input(
                        _context_id(model, "rate"),
                        defaults["rate"],
                        minimum=-1,
                        maximum=2,
                        step=0.000001,
                        persistence_key="pricer-black76-rate",
                    ),
                ),
            ]
        )
    elif model == "asian76":
        fields.extend(
            [
                _build_pricer_field(
                    "Forward price",
                    _build_pricer_number_input(
                        _context_id(model, "forward"),
                        defaults["forward"],
                        minimum=0.01,
                        persistence_key="pricer-asian76-forward",
                    ),
                ),
                _build_pricer_field(
                    "Averaging start",
                    _build_pricer_date_picker(
                        _context_id(model, "averaging_start_date", True),
                        defaults["averaging_start_date"],
                        allow_past=True,
                        persistence_key="pricer-asian76-averaging-start",
                    ),
                ),
                _build_pricer_field(
                    "Expiration / averaging end",
                    _build_pricer_date_picker(
                        _context_id(model, "expiration_date", True),
                        defaults["expiration_date"],
                        allow_past=True,
                        persistence_key="pricer-asian76-expiration",
                    ),
                ),
                _build_pricer_field(
                    "Contract expiration",
                    _build_pricer_date_picker(
                        _context_id(model, "contract_expiration_date", True),
                        defaults["contract_expiration_date"],
                        allow_past=True,
                        persistence_key="pricer-asian76-contract-expiration",
                    ),
                ),
                _build_pricer_field(
                    "Risk-free rate",
                    _build_pricer_number_input(
                        _context_id(model, "rate"),
                        defaults["rate"],
                        minimum=-1,
                        maximum=2,
                        step=0.000001,
                        persistence_key="pricer-asian76-rate",
                    ),
                ),
            ]
        )
    elif model == "kirk":
        fields.extend(
            [
                _build_pricer_field(
                    "Asset 1 price",
                    _build_pricer_number_input(
                        _context_id(model, "asset_1"),
                        defaults["asset_1"],
                        minimum=0.01,
                        persistence_key="pricer-kirk-asset-1",
                    ),
                ),
                _build_pricer_field(
                    "Asset 2 price",
                    _build_pricer_number_input(
                        _context_id(model, "asset_2"),
                        defaults["asset_2"],
                        minimum=0.01,
                        persistence_key="pricer-kirk-asset-2",
                    ),
                ),
                _build_pricer_field(
                    "Option expiration",
                    _build_pricer_date_picker(
                        _context_id(model, "expiration_date", True),
                        defaults["expiration_date"],
                        allow_past=True,
                        persistence_key="pricer-kirk-expiration",
                    ),
                ),
                _build_pricer_field(
                    "Contract expiration",
                    _build_pricer_date_picker(
                        _context_id(model, "contract_expiration_date", True),
                        defaults["contract_expiration_date"],
                        allow_past=True,
                        persistence_key="pricer-kirk-contract-expiration",
                    ),
                ),
                _build_pricer_field(
                    "Correlation",
                    _build_pricer_number_input(
                        _context_id(model, "correlation"),
                        defaults["correlation"],
                        minimum=-1,
                        maximum=1,
                        step=0.00001,
                        persistence_key="pricer-kirk-correlation",
                    ),
                ),
                html.Div(
                    "Kirk is undiscounted in the current library; rate and Rho are not applicable.",
                    className="pricer-inline-method-note",
                    role="note",
                ),
            ]
        )
    return html.Div(fields, className="pricer-context-grid")


def _leg_column_defs(model):
    text_column = {
        "editable": True,
        "cellClass": "pricer-editable-cell pricer-table-text-cell",
    }
    numeric_column = {
        "editable": True,
        "type": "numericColumn",
        "cellClass": "pricer-editable-cell pricer-table-number-cell",
        "valueParser": {"function": "Number(params.newValue)"},
    }
    positive_rules = {
        "pricer-invalid-cell": (
            "params.value == null || !isFinite(Number(params.value)) || "
            "Number(params.value) <= 0"
        )
    }
    volatility_rules = {
        "pricer-invalid-cell": (
            "params.value == null || !isFinite(Number(params.value)) || "
            "Number(params.value) < 0.005 || Number(params.value) > 2"
        )
    }
    columns = [
        {
            "headerName": "Leg",
            "field": "name",
            "pinned": "left",
            "minWidth": 120,
            **text_column,
        },
        {
            "headerName": "Side",
            "field": "side",
            "width": 92,
            "editable": True,
            "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": ["BUY", "SELL"]},
            "cellClass": "pricer-editable-cell pricer-table-text-cell",
        },
        {
            "headerName": "Lots",
            "field": "ratio",
            "width": 92,
            **numeric_column,
            "cellClassRules": positive_rules,
        },
        {
            "headerName": "Call / Put",
            "field": "call_put",
            "width": 102,
            "editable": True,
            "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": ["C", "P"]},
            "cellClass": "pricer-editable-cell pricer-table-text-cell",
        },
        {
            "headerName": "Strike",
            "field": "strike",
            "minWidth": 105,
            **numeric_column,
            "cellClassRules": positive_rules if model != "kirk" else {},
        },
    ]
    if model in {"black76", "asian76"}:
        columns.append(
            {
                "headerName": "Input vol",
                "field": "volatility",
                "minWidth": 118,
                **numeric_column,
                "cellClassRules": volatility_rules,
                "headerTooltip": "Leg-specific input contract volatility",
            }
        )
    else:
        columns.extend(
            [
                {
                    "headerName": "Asset 1 input vol",
                    "field": "volatility_asset_1",
                    "minWidth": 145,
                    **numeric_column,
                    "cellClassRules": volatility_rules,
                },
                {
                    "headerName": "Asset 2 input vol",
                    "field": "volatility_asset_2",
                    "minWidth": 145,
                    **numeric_column,
                    "cellClassRules": volatility_rules,
                },
            ]
        )
    return columns


def _build_legs_grid():
    return dag.AgGrid(
        id="pricer-legs-grid",
        rowData=[default_leg("black76", 1)],
        columnDefs=_leg_column_defs("black76"),
        defaultColDef={
            "sortable": False,
            "filter": False,
            "resizable": True,
            "suppressHeaderMenuButton": True,
            "suppressHeaderFilterButton": True,
            "singleClickEdit": True,
        },
        dashGridOptions={
            "domLayout": "autoHeight",
            "rowHeight": 34,
            "headerHeight": 38,
            "stopEditingWhenCellsLoseFocus": True,
            "enableCellTextSelection": True,
            "ensureDomOrder": True,
            "animateRows": False,
            "rowSelection": {
                "mode": "singleRow",
                "checkboxes": True,
                "headerCheckbox": False,
                "enableClickSelection": True,
            },
        },
        getRowId="params.data.leg_id",
        persistence="pricer-structure-legs",
        persisted_props=["rowData"],
        persistence_type="session",
        selectedRows=[],
        className="ag-theme-alpine mckinsey-ag-grid pricer-data-grid pricer-legs-grid",
        style={"width": "100%"},
        dangerously_allow_code=True,
    )


def _format_number(value, decimals=4):
    if value is None:
        return "—"
    number = float(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:,.{decimals}f}"


def _result_numeric_column(
    field,
    header,
    *,
    pinned=None,
    min_width=72,
    sign_coloring=True,
    decimal_places=None,
    cell_tooltip_field=None,
):
    number_format = (
        f",.{decimal_places}f" if decimal_places is not None else ",.6~f"
    )
    column = {
        "headerName": header,
        "field": field,
        "minWidth": min_width,
        "type": "numericColumn",
        "pinned": pinned,
        "valueFormatter": {
            "function": (
                "params.value == null || !isFinite(Number(params.value)) "
                f"? '—' : d3.format('{number_format}')(Number(params.value))"
            )
        },
        "cellClass": "pricer-table-number-cell",
    }
    if sign_coloring:
        column["cellClassRules"] = {
            "pricer-positive-cell": "Number(params.value) > 0",
            "pricer-negative-cell": "Number(params.value) < 0",
            "pricer-missing-cell": "params.value == null",
        }
    if cell_tooltip_field:
        column["tooltipField"] = cell_tooltip_field
        column["cellClass"] = (
            f"{column['cellClass']} pricer-metric-tooltip-cell"
        )
    return column


def _result_greek_column(field, label, *, prefix, decimal_places):
    display_label = label
    cell_tooltip_field = None
    if field == "vega":
        display_label = "Vega"
        cell_tooltip_field = "_vega_tooltip"
    elif field == "rho":
        display_label = "Rho"
        cell_tooltip_field = "_rho_tooltip"
    return _result_numeric_column(
        f"{prefix}_{field}",
        display_label,
        decimal_places=decimal_places,
        cell_tooltip_field=cell_tooltip_field,
    )


def _combined_result_columns(snapshot):
    model = snapshot["model"]
    columns = [
        {
            "headerName": "Leg",
            "field": "name",
            "pinned": "left",
            "minWidth": 84,
            "cellClass": "pricer-table-text-cell",
            "headerClass": "pricer-table-text-header",
        },
        {
            "headerName": "Side",
            "field": "side",
            "minWidth": 58,
            "cellClass": "pricer-table-text-cell",
            "headerClass": "pricer-table-text-header",
        },
        {
            "headerName": "Lots",
            "field": "ratio",
            "minWidth": 60,
            "type": "numericColumn",
            "cellClass": "pricer-table-number-cell",
        },
        {
            "headerName": "C/P",
            "field": "call_put",
            "minWidth": 50,
            "cellClass": "pricer-table-text-cell",
            "headerClass": "pricer-table-text-header",
        },
        _result_numeric_column(
            "strike",
            "Strike",
            min_width=70,
            sign_coloring=False,
        ),
    ]
    if model in {"black76", "asian76"}:
        columns.append(
            _result_numeric_column(
                "raw_volatility",
                "Input vol",
                min_width=74,
                sign_coloring=False,
            )
        )
    else:
        columns.extend(
            [
                _result_numeric_column(
                    "raw_volatility_asset_1",
                    "Asset 1 vol",
                    min_width=86,
                    sign_coloring=False,
                ),
                _result_numeric_column(
                    "raw_volatility_asset_2",
                    "Asset 2 vol",
                    min_width=86,
                    sign_coloring=False,
                ),
            ]
        )
    columns.extend(
        [
            {
                "headerName": "Position contribution",
                "headerClass": (
                    "pricer-result-column-group "
                    "pricer-result-column-group-position"
                ),
                "children": [
                    _result_numeric_column(
                        "trade_value",
                        "Value",
                        decimal_places=2,
                    ),
                    *[
                        _result_greek_column(
                            field,
                            snapshot["greek_labels"][field],
                            prefix="trade",
                            decimal_places=2,
                        )
                        for field in snapshot["greek_fields"]
                    ],
                ],
            },
            {
                "headerName": "Unit analytics",
                "headerClass": "pricer-result-column-group",
                "children": [
                    _result_numeric_column(
                        "unit_value",
                        "Value",
                        decimal_places=4,
                    ),
                    *[
                        _result_greek_column(
                            field,
                            snapshot["greek_labels"][field],
                            prefix="unit",
                            decimal_places=4,
                        )
                        for field in snapshot["greek_fields"]
                    ],
                ],
            },
        ]
    )
    return columns


def _combined_result_rows(snapshot):
    rows = []
    for leg in snapshot["legs"]:
        row = {
            "leg_id": leg["leg_id"],
            "name": leg["name"],
            "side": leg["side"],
            "ratio": leg["ratio"],
            "call_put": leg["call_put"],
            "strike": leg["strike"],
            "unit_value": leg["unit"]["value"],
            "trade_value": leg["trade_contribution"]["value"],
            **{
                f"unit_{field}": leg["unit"]["greeks"].get(field)
                for field in snapshot["greek_fields"]
            },
            **{
                f"trade_{field}": leg["trade_contribution"]["greeks"].get(field)
                for field in snapshot["greek_fields"]
            },
            "_vega_tooltip": "Input vol, 1 point",
            "_rho_tooltip": "1 rate point",
        }
        if snapshot["model"] in {"black76", "asian76"}:
            row["raw_volatility"] = leg["raw_volatility"]
        else:
            row["raw_volatility_asset_1"] = leg["raw_volatility_asset_1"]
            row["raw_volatility_asset_2"] = leg["raw_volatility_asset_2"]
        rows.append(row)
    total = {
        "leg_id": "__total__",
        "name": "Total",
        "side": "",
        "ratio": None,
        "call_put": "",
        "strike": None,
        "trade_value": snapshot["totals"]["trade_value"],
        "unit_value": snapshot["totals"]["unit_structure_value"],
        **{
            f"trade_{field}": snapshot["totals"]["trade_greeks"].get(field)
            for field in snapshot["greek_fields"]
        },
        **{
            f"unit_{field}": snapshot["totals"]["unit_structure_greeks"].get(field)
            for field in snapshot["greek_fields"]
        },
        "_vega_tooltip": "Input vol, 1 point",
        "_rho_tooltip": "1 rate point",
    }
    return rows, total


def _build_combined_result_grid(snapshot):
    rows, total = _combined_result_rows(snapshot)
    options = {
        "domLayout": "autoHeight",
        "rowHeight": 31,
        "headerHeight": 44,
        "groupHeaderHeight": 30,
        "enableCellTextSelection": True,
        "ensureDomOrder": True,
        "animateRows": False,
        "suppressColumnVirtualisation": True,
        "pinnedBottomRowData": [total],
        "enableBrowserTooltips": False,
        "tooltipShowDelay": 0,
        "tooltipHideDelay": 3000,
    }
    return dag.AgGrid(
        id="pricer-combined-results-grid",
        rowData=rows,
        columnDefs=_combined_result_columns(snapshot),
        defaultColDef={
            "sortable": False,
            "filter": False,
            "resizable": True,
            "suppressHeaderMenuButton": True,
            "suppressHeaderFilterButton": True,
            "wrapHeaderText": True,
            "autoHeaderHeight": True,
        },
        dashGridOptions=options,
        columnSize="autoSize",
        columnSizeOptions={"skipHeader": False},
        getRowId="params.data.leg_id",
        className=(
            "ag-theme-alpine mckinsey-ag-grid pricer-data-grid "
            "pricer-results-grid pricer-combined-results-grid"
        ),
        style={"width": "100%"},
        dangerously_allow_code=True,
    )


def _build_pricing_model_field():
    return _build_pricer_field(
        "Pricing model",
        dcc.Dropdown(
            id="option-type",
            options=option_types,
            value="black76",
            clearable=False,
            persistence="pricer-model",
            persistence_type="session",
            className="pricer-filter-dropdown pricer-option-type-dropdown",
        ),
        class_name="pricer-model-field",
    )


def _build_asset_field():
    return _build_pricer_field(
        "Asset",
        dcc.Dropdown(
            id="pricer-asset",
            options=asset_options,
            value=DEFAULT_ASSET,
            clearable=False,
            persistence="pricer-asset",
            persistence_type="session",
            className="pricer-filter-dropdown pricer-asset-dropdown",
        ),
        class_name="pricer-asset-field",
    )


def _build_structure_type_field():
    return _build_pricer_field(
        "Type",
        dcc.Dropdown(
            id="pricer-structure-type",
            options=structure_type_options,
            value=DEFAULT_STRUCTURE_TYPE,
            clearable=False,
            persistence="pricer-structure-type",
            persistence_type="session",
            className="pricer-filter-dropdown pricer-type-dropdown",
        ),
        class_name="pricer-type-field",
    )


layout = html.Main(
    [
        dcc.Store(
            id="pricer-draft-store",
            data=default_draft("black76"),
            storage_type="session",
        ),
        dcc.Store(id="pricer-calculation-store", storage_type="session"),
        html.Section(
            [
                _build_pricer_section_header(
                    "Structure configuration",
                    heading_level=1,
                ),
                html.Div(
                    [
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.Div(
                                            [
                                                _build_structure_type_field(),
                                                _build_asset_field(),
                                                _build_pricing_model_field(),
                                                _build_pricer_field(
                                                    "Valuation date",
                                                    _build_pricer_date_picker(
                                                        "pricer-valuation-date",
                                                        date.today().isoformat(),
                                                        allow_past=True,
                                                        persistence_key=(
                                                            "pricer-valuation-date-v1"
                                                        ),
                                                    ),
                                                ),
                                                html.Div(
                                                    id="pricer-shared-context",
                                                ),
                                            ],
                                            className=(
                                                "pricer-context-with-valuation"
                                            ),
                                        ),
                                    ],
                                    className="pricer-setup-panel pricer-market-panel",
                                ),
                                html.Div(
                                    [
                                        html.Div(
                                            [
                                                _build_pricer_field(
                                                    "Contract multiplier",
                                                    _build_pricer_number_input(
                                                        "pricer-contract-multiplier",
                                                        1,
                                                        minimum=0,
                                                        step=0.01,
                                                        persistence_key=(
                                                            "pricer-contract-"
                                                            "multiplier-aligned-v2"
                                                        ),
                                                    ),
                                                ),
                                            ],
                                            className="pricer-sizing-grid",
                                        ),
                                    ],
                                    className="pricer-setup-panel pricer-sizing-panel",
                                ),
                            ],
                            className="pricer-market-sizing-layout",
                        ),
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.H3(
                                            "Option legs",
                                            className="pricer-subsection-title",
                                        ),
                                        html.Div(
                                            [
                                                html.Button(
                                                    "Add leg",
                                                    id="pricer-add-leg",
                                                    className=(
                                                        "custom-export-btn "
                                                        "pricer-secondary-button"
                                                    ),
                                                ),
                                                html.Button(
                                                    "Duplicate selected",
                                                    id="pricer-duplicate-leg",
                                                    className=(
                                                        "custom-export-btn "
                                                        "pricer-secondary-button"
                                                    ),
                                                ),
                                                html.Button(
                                                    "Remove selected",
                                                    id="pricer-remove-leg",
                                                    className="pricer-remove-button",
                                                ),
                                            ],
                                            className="pricer-leg-edit-actions",
                                        ),
                                        html.Div(
                                            id="pricer-leg-action-status",
                                            className="pricer-action-status",
                                            role="status",
                                        ),
                                    ],
                                    className="pricer-leg-heading",
                                ),
                                html.Div(
                                    [
                                        html.Div(
                                            id="pricer-calculation-status",
                                            className="pricer-calculation-status",
                                        ),
                                        html.Button(
                                            "Calculate structure",
                                            id="calculate-button",
                                            className=(
                                                "custom-export-btn "
                                                "pricer-calculate-button"
                                            ),
                                        ),
                                    ],
                                    className="pricer-leg-actions",
                                ),
                            ],
                            className="pricer-leg-toolbar",
                        ),
                        _build_legs_grid(),
                    ],
                    className="pricer-section-body pricer-config-body",
                ),
            ],
            className="pricer-section pricer-config-section",
        ),
        html.Section(
            [
                _build_pricer_section_header("Pricing output"),
                html.Div(
                    [
                        html.Div(
                            id="pricer-warning-container",
                            className="pricer-warning-container",
                        ),
                        html.Div(
                            [
                                html.Div(
                                    id="model-inputs-used-container",
                                    className="pricer-model-inputs-used-container",
                                ),
                                html.Div(
                                    id="results-container",
                                    className="pricer-summary-grid",
                                ),
                                html.Div(
                                    id="time-info",
                                    className="pricer-time-info",
                                ),
                            ],
                            className="pricer-output-overview",
                        ),
                        html.Section(
                            [
                                html.Div(
                                    id="pricer-unit-results-container",
                                ),
                            ],
                            className="pricer-output-panel",
                        ),
                        html.Div(
                            id="greeks-container",
                            className="pricer-compatibility-output",
                        ),
                    ],
                    className="pricer-section-body pricer-output-body-structure",
                ),
            ],
            className="pricer-section pricer-output-section",
        ),
        html.Section(
            [
                _build_pricer_section_header("Payoff analysis"),
                html.Div(
                    [
                        html.Div(
                            [
                                _build_pricer_field(
                                    "Valuation date",
                                    dcc.DatePickerSingle(
                                        id="valuation-date",
                                        min_date_allowed=date.today(),
                                        initial_visible_month=date.today(),
                                        date=None,
                                        display_format="YYYY-MM-DD",
                                        placeholder="At expiration",
                                        className="pricer-date-picker",
                                    ),
                                    class_name="pricer-payoff-date-field",
                                ),
                                _build_pricer_field(
                                    "Price range (%)",
                                    dcc.Slider(
                                        id="price-range-slider",
                                        min=10,
                                        max=100,
                                        step=5,
                                        value=50,
                                        marks={
                                            10: "10%",
                                            25: "25%",
                                            50: "50%",
                                            75: "75%",
                                            100: "100%",
                                        },
                                        className="pricer-slider",
                                    ),
                                    class_name="pricer-payoff-slider-field",
                                ),
                            ],
                            className="pricer-payoff-controls",
                        ),
                        _build_pricer_chart_card(
                            "payoff-chart",
                            "Total structure payoff and value",
                            "Calculate the structure to see its payoff.",
                            class_name="pricer-wide-chart",
                        ),
                    ],
                    className="pricer-section-body pricer-payoff-body",
                ),
            ],
            className="pricer-section pricer-payoff-section",
        ),
        html.Section(
            [
                _build_pricer_section_header("Structure sensitivities"),
                html.Div(
                    [
                        _build_pricer_chart_card(
                            "volatility-chart",
                            "Parallel volatility shift",
                            "Calculate the structure to see volatility sensitivity.",
                        ),
                        _build_pricer_chart_card(
                            "rate-chart",
                            "Risk-free rate sensitivity",
                            "Calculate the structure to see rate sensitivity.",
                        ),
                        _build_pricer_chart_card(
                            "correlation-chart",
                            "Correlation sensitivity",
                            "Available for Kirk structures.",
                        ),
                        _build_pricer_chart_card(
                            "extension-chart",
                            "Expiration extension",
                            "Calculate the structure to see expiration sensitivity.",
                        ),
                        _build_pricer_chart_card(
                            "time-chart",
                            "Time decay",
                            "Calculate the structure to see time decay.",
                            class_name="pricer-wide-chart",
                        ),
                    ],
                    className="pricer-section-body pricer-chart-grid",
                ),
            ],
            className="pricer-section pricer-sensitivity-section",
        ),
    ],
    className="options-dashboard-container pricer-page",
)


def update_parameters(option_type):
    """Compatibility helper retained for direct tests and older callers."""
    return _build_context_form(option_type if option_type in MODEL_LABELS else "black76")


@callback(
    [
        Output("pricer-shared-context", "children"),
        Output("pricer-legs-grid", "columnDefs"),
        Output("pricer-legs-grid", "rowData"),
        Output("pricer-legs-grid", "selectedRows"),
        Output("pricer-draft-store", "data"),
        Output("pricer-leg-action-status", "children"),
    ],
    [
        Input("option-type", "value"),
        Input("pricer-add-leg", "n_clicks"),
        Input("pricer-duplicate-leg", "n_clicks"),
        Input("pricer-remove-leg", "n_clicks"),
        Input("pricer-legs-grid", "cellValueChanged"),
    ],
    [
        State("pricer-legs-grid", "rowData"),
        State("pricer-legs-grid", "selectedRows"),
        State("pricer-draft-store", "data"),
    ],
)
def manage_structure_legs(
    model,
    _add_clicks,
    _duplicate_clicks,
    _remove_clicks,
    _cell_event,
    rows,
    selected_rows,
    draft,
):
    triggered = _get_pricer_triggered_id()
    rows = [dict(row) for row in (rows or [])]
    draft = dict(draft or {})
    if triggered in (None, "option-type"):
        if draft.get("model") == model and draft.get("legs"):
            rows = [dict(row) for row in draft["legs"]]
            next_sequence = int(draft.get("next_leg_sequence") or len(rows) + 1)
        else:
            rows = [default_leg(model, 1)]
            next_sequence = 2
        new_draft = {
            "schema_version": 1,
            "model": model,
            "legs": rows,
            "next_leg_sequence": next_sequence,
        }
        return (
            _build_context_form(model).children,
            _leg_column_defs(model),
            rows,
            [],
            new_draft,
            "",
        )

    next_sequence = int(draft.get("next_leg_sequence") or len(rows) + 1)
    status = ""
    if triggered == "pricer-add-leg":
        if len(rows) >= MAX_LEGS:
            status = f"A structure can contain at most {MAX_LEGS} legs."
        else:
            rows.append(default_leg(model, next_sequence))
            next_sequence += 1
            status = f"Added Leg {next_sequence - 1}."
    elif triggered == "pricer-duplicate-leg":
        selected = (selected_rows or [None])[0]
        if selected is None:
            status = "Select one leg to duplicate."
        elif len(rows) >= MAX_LEGS:
            status = f"A structure can contain at most {MAX_LEGS} legs."
        else:
            duplicate = copy.deepcopy(selected)
            duplicate["leg_id"] = f"leg-{next_sequence}"
            duplicate["name"] = f"Leg {next_sequence}"
            rows.append(duplicate)
            next_sequence += 1
            status = f"Duplicated as Leg {next_sequence - 1}."
    elif triggered == "pricer-remove-leg":
        selected = (selected_rows or [None])[0]
        if selected is None:
            status = "Select one leg to remove."
        elif len(rows) <= 1:
            status = "A structure must retain at least one leg."
        else:
            selected_id = selected.get("leg_id")
            rows = [row for row in rows if row.get("leg_id") != selected_id]
            status = f"Removed {selected.get('name') or selected_id}."
    elif isinstance(triggered, dict) or triggered == "pricer-legs-grid":
        status = ""

    new_draft = {
        "schema_version": 1,
        "model": model,
        "legs": rows,
        "next_leg_sequence": next_sequence,
    }
    row_output = no_update if triggered == "pricer-legs-grid" else rows
    return no_update, no_update, row_output, [], new_draft, status


def _sync_contract_date(expiration_value, contract_value):
    if not expiration_value:
        return no_update, no_update
    expiration = parse_date(expiration_value)
    contract = parse_date(contract_value, expiration)
    minimum = expiration.isoformat()
    if not contract_value or contract < expiration:
        return minimum, minimum
    return no_update, minimum


@callback(
    [
        Output(
            {
                "type": "pricer-context-date",
                "model": "black76",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "model": "black76",
                "param": "contract_expiration_date",
            },
            "min_date_allowed",
        ),
    ],
    [
        Input(
            {
                "type": "pricer-context-date",
                "model": "black76",
                "param": "expiration_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "model": "black76",
                "param": "contract_expiration_date",
            },
            "date",
        ),
    ],
    prevent_initial_call=True,
)
def sync_black76_contract_expiration_date(expiration_value, contract_value):
    return _sync_contract_date(expiration_value, contract_value)


@callback(
    [
        Output(
            {
                "type": "pricer-context-date",
                "model": "asian76",
                "param": "expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "model": "asian76",
                "param": "expiration_date",
            },
            "min_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "model": "asian76",
                "param": "averaging_start_date",
            },
            "max_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "model": "asian76",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "model": "asian76",
                "param": "contract_expiration_date",
            },
            "min_date_allowed",
        ),
    ],
    [
        Input(
            {
                "type": "pricer-context-date",
                "model": "asian76",
                "param": "averaging_start_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "model": "asian76",
                "param": "expiration_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "model": "asian76",
                "param": "contract_expiration_date",
            },
            "date",
        ),
    ],
    prevent_initial_call=True,
)
def sync_asian76_dates(averaging_start_value, expiration_value, contract_value):
    if not averaging_start_value or not expiration_value:
        return no_update, no_update, no_update, no_update, no_update
    averaging_start = parse_date(averaging_start_value)
    expiration = parse_date(expiration_value, averaging_start)
    corrected_expiration = max(averaging_start, expiration)
    expiration_update = (
        corrected_expiration.isoformat()
        if corrected_expiration != expiration
        else no_update
    )
    contract = parse_date(contract_value, corrected_expiration)
    contract_update = (
        corrected_expiration.isoformat()
        if not contract_value or contract < corrected_expiration
        else no_update
    )
    return (
        expiration_update,
        averaging_start.isoformat(),
        corrected_expiration.isoformat(),
        contract_update,
        corrected_expiration.isoformat(),
    )


@callback(
    [
        Output(
            {
                "type": "pricer-context-date",
                "model": "kirk",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "model": "kirk",
                "param": "contract_expiration_date",
            },
            "min_date_allowed",
        ),
    ],
    [
        Input(
            {
                "type": "pricer-context-date",
                "model": "kirk",
                "param": "expiration_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "model": "kirk",
                "param": "contract_expiration_date",
            },
            "date",
        ),
    ],
    prevent_initial_call=True,
)
def sync_kirk_contract_expiration_date(expiration_value, contract_value):
    return _sync_contract_date(expiration_value, contract_value)


def _context_from_states(model, param_values, param_ids, date_values, date_ids):
    context = {}
    for value, component_id in zip(param_values or [], param_ids or []):
        if (
            isinstance(component_id, dict)
            and component_id.get("model") == model
        ):
            context[component_id.get("param")] = value
    for value, component_id in zip(date_values or [], date_ids or []):
        if (
            isinstance(component_id, dict)
            and component_id.get("model") == model
        ):
            context[component_id.get("param")] = value
    return context


@callback(
    [
        Output("pricer-calculation-store", "data"),
        Output("pricer-calculation-status", "children"),
    ],
    [
        Input("calculate-button", "n_clicks"),
        Input("pricer-structure-type", "value"),
        Input("pricer-asset", "value"),
        Input("option-type", "value"),
        Input("pricer-contract-multiplier", "value"),
        Input("pricer-legs-grid", "rowData"),
        Input(
            {"type": "pricer-context-param", "model": ALL, "param": ALL},
            "value",
        ),
        Input(
            {"type": "pricer-context-date", "model": ALL, "param": ALL},
            "date",
        ),
        Input("pricer-valuation-date", "date"),
    ],
    [
        State(
            {"type": "pricer-context-param", "model": ALL, "param": ALL},
            "id",
        ),
        State(
            {"type": "pricer-context-date", "model": ALL, "param": ALL},
            "id",
        ),
    ],
    prevent_initial_call=True,
    running=[(Output("calculate-button", "disabled"), True, False)],
)
def calculate_structure_callback(
    n_clicks,
    structure_type,
    asset,
    model,
    contract_multiplier,
    rows,
    param_values,
    date_values,
    valuation_date_value,
    param_ids,
    date_ids,
):
    triggered = _get_pricer_triggered_id()
    if triggered == "pricer-structure-type":
        if not n_clicks:
            return None, ""
        return None, _build_pricer_message(
            "Type changed. Calculate the structure again."
        )
    if triggered == "pricer-asset":
        if not n_clicks:
            return None, ""
        return None, _build_pricer_message(
            "Asset changed. Calculate the structure again."
        )
    if triggered == "option-type":
        if not n_clicks:
            return None, ""
        return None, _build_pricer_message(
            "Model changed. Configure the new structure and calculate again."
        )
    if triggered != "calculate-button":
        if not n_clicks:
            return None, ""
        return None, _build_pricer_message(
            "Inputs changed. Calculate the structure again."
        )
    if not n_clicks:
        return None, _build_pricer_message("No calculation performed.")
    context = _context_from_states(
        model,
        param_values,
        param_ids,
        date_values,
        date_ids,
    )
    context["structure_type"] = structure_type
    context["asset"] = asset
    sizing = {
        "structure_quantity": 1,
        "contract_multiplier": contract_multiplier,
    }
    valuation_date = parse_date(valuation_date_value, date.today())
    try:
        snapshot = calculate_structure(
            model,
            context,
            sizing,
            rows or [],
            as_of=valuation_date,
        )
    except StructureValidationError as exc:
        return None, _build_pricer_message(str(exc), tone="danger")
    except Exception as exc:
        return None, _build_pricer_message(
            f"Structure calculation failed ({type(exc).__name__}).",
            tone="danger",
        )
    leg_count = len(snapshot["legs"])
    leg_label = "leg" if leg_count == 1 else "legs"
    return snapshot, _build_pricer_message(
        f"Calculated {leg_count} {leg_label} as one "
        f"{snapshot['model_label']} structure.",
        tone="success",
    )


def _model_inputs_summary(snapshot):
    context = snapshot["context"]
    cards = [
        _build_pricer_result_card(
            "Type",
            context["structure_type"],
            tone="context",
        ),
        _build_pricer_result_card(
            "Asset",
            context["asset"],
            tone="market",
        ),
        _build_pricer_result_card(
            "Pricing model",
            snapshot["model_label"],
            tone="basis",
        ),
        _build_pricer_result_card(
            "Valuation date",
            snapshot["calculation_date"],
            tone="context",
        ),
    ]
    if snapshot["model"] in {"black76", "asian76"}:
        cards.extend(
            [
                _build_pricer_result_card(
                    "Forward price used",
                    _format_number(context["forward"]),
                    tone="market",
                ),
                _build_pricer_result_card(
                    (
                        "Expiration / averaging end"
                        if snapshot["model"] == "asian76"
                        else "Option expiration"
                    ),
                    context["expiration_date"],
                    tone="context",
                ),
                _build_pricer_result_card(
                    "Contract expiration",
                    context["contract_expiration_date"],
                    tone="context",
                ),
                _build_pricer_result_card(
                    "Risk-free rate",
                    f"{context['rate']:.4%}",
                    tone="context",
                ),
            ]
        )
    else:
        cards.extend(
            [
                _build_pricer_result_card(
                    "Asset price cross used",
                    (
                        f"{_format_number(context['asset_1'])} / "
                        f"{_format_number(context['asset_2'])}"
                    ),
                    "Asset 1 / Asset 2",
                    tone="market",
                ),
                _build_pricer_result_card(
                    "Option expiration",
                    context["expiration_date"],
                    tone="context",
                ),
                _build_pricer_result_card(
                    "Contract expiration",
                    context["contract_expiration_date"],
                    tone="context",
                ),
                _build_pricer_result_card(
                    "Correlation",
                    f"{context['correlation']:.4f}",
                    tone="context",
                ),
            ]
        )
    return cards


@callback(
    [
        Output("results-container", "children"),
        Output("pricer-unit-results-container", "children"),
        Output("greeks-container", "children"),
        Output("time-info", "children"),
        Output("model-inputs-used-container", "children"),
        Output("pricer-warning-container", "children"),
    ],
    Input("pricer-calculation-store", "data"),
)
def render_structure_results(snapshot):
    if not snapshot:
        return "", "", "", "", "", ""
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        return (
            "",
            _build_pricer_message(
                "Stored calculation is stale. Calculate the structure again.",
                tone="warning",
            ),
            "",
            "",
            "",
            _build_pricer_message("Stale calculation snapshot.", tone="warning"),
        )
    calculated_cards = [
        _build_pricer_result_card(
            "Unit structure value",
            _format_number(snapshot["totals"]["unit_structure_value"]),
            tone="unit",
        ),
        _build_pricer_result_card(
            "Total trade value",
            _format_number(snapshot["totals"]["trade_value"]),
            tone="trade",
        ),
    ]
    time_to_expiry_value = f"{snapshot['context']['time_to_expiry']:.6f}y"
    time_detail = None
    if snapshot["model"] == "asian76":
        time_detail = (
            f"Averaging starts {snapshot['context']['averaging_start_date']} "
            f"("
            f"{round(snapshot['context']['time_to_averaging_start'] * 365)} days"
            f")"
        )
    volatility_adjustment_detail = (
        f"√({snapshot['context']['option_business_days']} days / "
        f"{snapshot['context']['contract_business_days']} contract days)"
    )
    warning_children = [
        _build_pricer_message(warning, tone="warning")
        for warning in snapshot.get("warnings") or []
    ]
    output_cards = [
        *_model_inputs_summary(snapshot),
        *calculated_cards,
        _build_pricer_result_card(
            "Time to expiry",
            time_to_expiry_value,
            time_detail,
            tone="time",
        ),
        _build_pricer_result_card(
            "Volatility adjustment",
            f"{snapshot['context']['vol_adjustment_factor']:.6f}×",
            volatility_adjustment_detail,
            tone="adjustment",
            detail_on_hover=True,
        ),
    ]
    return (
        output_cards,
        _build_combined_result_grid(snapshot),
        "",
        "",
        "",
        warning_children,
    )


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
    maximum = snapshot["context"]["expiration_date"]
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
    ],
)
def update_payoff_chart(calculation_store, valuation_date, price_range, option_type=None):
    del option_type
    if (
        not calculation_store
        or calculation_store.get("schema_version") != SCHEMA_VERSION
    ):
        return _empty_pricer_figure(
            "Calculate the structure first.",
            "Underlying price",
            "Trade value",
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
            "Trade value",
        )
    fig = go.Figure()
    if series["at_expiration"]:
        fig.add_trace(
            go.Scatter(
                x=series["x"],
                y=series["payoff"],
                mode="lines",
                name="Total expiration payoff",
                line={"color": "#2563eb", "width": 2.5},
            )
        )
    else:
        fig.add_trace(
            go.Scatter(
                x=series["x"],
                y=series["theoretical"],
                mode="lines",
                name="Total structure value",
                line={"color": "#2563eb", "width": 2.5},
            )
        )
        fig.add_trace(
            go.Scatter(
                x=series["x"],
                y=series["payoff"],
                mode="lines",
                name="Total expiration payoff",
                line={"color": "#dc2626", "width": 1.8, "dash": "dash"},
            )
        )
        fig.add_trace(
            go.Scatter(
                x=[series["current_underlying"]],
                y=[series["current_value"]],
                mode="markers",
                name="Selected Valuation",
                marker={"color": "#15803d", "size": 10, "symbol": "star"},
            )
        )
    fig.update_layout(
        xaxis=_pricer_axis(series["xaxis_title"]),
        yaxis=_pricer_axis("Trade value"),
    )
    return _style_pricer_figure(fig)


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


# ---------------------------------------------------------------------------
# Compatibility helpers
# ---------------------------------------------------------------------------
# These wrappers preserve the direct-call contracts exercised by the existing
# Asian-76 tests while the active Dash page uses the structure snapshot above.


def _values_by_param(values, ids, model):
    result = {}
    for value, component_id in zip(values or [], ids or []):
        if isinstance(component_id, dict) and component_id.get("model") == model:
            result[component_id.get("param")] = value
    return result


def _count_pricer_business_days(start_date, end_date):
    return count_business_days(parse_date(start_date), parse_date(end_date))


def _adjust_pricer_volatility(raw_volatility, expiration_date, contract_expiration_date):
    factor, option_days, contract_days = volatility_adjustment(
        date.today(),
        parse_date(expiration_date),
        parse_date(contract_expiration_date),
    )
    return raw_volatility * factor, factor, option_days, contract_days


def _parse_asian76_model_inputs(
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    params = _values_by_param(all_params, all_param_ids, "asian76")
    dates = _values_by_param(all_dates, all_date_ids, "asian76")
    if not all_param_ids:
        ordered = list(all_params or [])
        params = {
            "forward-price": ordered[0] if len(ordered) > 0 else 100,
            "strike-price": ordered[1] if len(ordered) > 1 else 100,
            "risk-free-rate": ordered[2] if len(ordered) > 2 else 0.05,
            "volatility": ordered[3] if len(ordered) > 3 else 0.2,
        }
    if not all_date_ids:
        ordered_dates = list(all_dates or [])
        dates = {
            "averaging-start-date": (
                ordered_dates[0]
                if len(ordered_dates) > 0
                else (date.today() + timedelta(days=7)).isoformat()
            ),
            "expiration-date": (
                ordered_dates[1]
                if len(ordered_dates) > 1
                else (date.today() + timedelta(days=30)).isoformat()
            ),
            "contract-expiration-date": (
                ordered_dates[2]
                if len(ordered_dates) > 2
                else (date.today() + timedelta(days=30)).isoformat()
            ),
        }
    forward = _coerce_pricer_float(params.get("forward-price"), 100)
    strike = _coerce_pricer_float(params.get("strike-price"), 100)
    rate = _coerce_pricer_float(params.get("risk-free-rate"), 0.05)
    raw_volatility = _coerce_pricer_float(params.get("volatility"), 0.2)
    averaging_start = parse_date(
        dates.get("averaging-start-date"),
        date.today() + timedelta(days=7),
    )
    expiration = parse_date(
        dates.get("expiration-date"),
        date.today() + timedelta(days=30),
    )
    contract_expiration = parse_date(
        dates.get("contract-expiration-date"),
        expiration,
    )
    context = {
        "forward": forward,
        "rate": rate,
        "averaging_start_date": averaging_start.isoformat(),
        "expiration_date": expiration.isoformat(),
        "contract_expiration_date": contract_expiration.isoformat(),
    }
    leg = {
        "leg_id": "leg-1",
        "name": "Leg 1",
        "side": "BUY",
        "ratio": 1,
        "call_put": "C",
        "strike": strike,
        "volatility": raw_volatility,
    }
    snapshot = calculate_structure(
        "asian76",
        context,
        {"structure_quantity": 1, "contract_multiplier": 1},
        [leg],
        as_of=date.today(),
    )
    normalized_context = snapshot["context"]
    normalized_leg = snapshot["legs"][0]
    return {
        "F": forward,
        "K": strike,
        "r": rate,
        "raw_v": raw_volatility,
        "v": normalized_leg["volatility_used"],
        "averaging_start_date": averaging_start,
        "expiration_date": expiration,
        "contract_expiration_date": contract_expiration,
        "vol_adjustment_factor": normalized_context["vol_adjustment_factor"],
        "option_business_days": normalized_context["option_business_days"],
        "contract_business_days": normalized_context["contract_business_days"],
        "days_to_averaging_start": (averaging_start - date.today()).days,
        "days_to_expiry": (expiration - date.today()).days,
        "T_A": normalized_context["time_to_averaging_start"],
        "T": normalized_context["time_to_expiry"],
    }


def _parse_asian76_params(
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    inputs = _parse_asian76_model_inputs(
        all_params,
        all_dates,
        all_param_ids,
        all_date_ids,
    )
    return (
        inputs["F"],
        inputs["K"],
        inputs["r"],
        inputs["v"],
        inputs["averaging_start_date"],
        inputs["expiration_date"],
        inputs["days_to_expiry"],
        inputs["T_A"],
        inputs["T"],
    )


def _price_single_asset_option(
    model,
    call_put,
    forward,
    strike,
    time_to_expiry,
    rate,
    volatility,
    time_to_averaging_start=None,
):
    if model == "black76":
        return black_76(
            call_put,
            forward,
            strike,
            time_to_expiry,
            rate,
            volatility,
        )
    if model == "asian76":
        if (
            time_to_averaging_start is None
            or time_to_averaging_start < 0
            or time_to_averaging_start > time_to_expiry
        ):
            raise ValueError(
                "Asian-76 requires 0 <= time to averaging start <= time to expiration."
            )
        return asian_76(
            call_put,
            forward,
            strike,
            time_to_expiry,
            time_to_averaging_start,
            rate,
            volatility,
        )
    raise ValueError(f"Unsupported single-asset model: {model}")


def _build_pricer_greeks_grid(grid_id, rows, columns):
    row_data = []
    for row in rows:
        item = dict(row)
        for column in columns:
            field = column["id"]
            if field == "greek":
                continue
            item[f"__{field}_raw"] = item.get(field)
        row_data.append(item)
    return dag.AgGrid(
        id=grid_id,
        rowData=row_data,
        columnDefs=[
            {
                "headerName": column["name"],
                "field": column["id"],
                "minWidth": 110,
            }
            for column in columns
        ],
        dashGridOptions={"domLayout": "autoHeight"},
        className="ag-theme-alpine mckinsey-ag-grid pricer-data-grid",
    )


def calculate_option(
    n_clicks,
    option_type,
    call_put,
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    if _get_pricer_triggered_id() == "option-type":
        return (
            _build_pricer_message("Click Calculate to see results."),
            _build_pricer_message("Greeks will appear here."),
            _build_pricer_message("Time information will appear here."),
            _build_pricer_message("Calculate to confirm model inputs."),
            None,
        )
    if not n_clicks:
        return (
            _build_pricer_message("No calculation performed."),
            _build_pricer_message("Greeks will appear here."),
            _build_pricer_message("Time information will appear here."),
            _build_pricer_message("Calculate to confirm model inputs."),
            None,
        )
    if option_type != "asian76":
        raise ValueError(
            "The compatibility callback is retained for Asian-76 direct tests only; "
            "the active page uses calculate_structure_callback."
        )
    inputs = _parse_asian76_model_inputs(
        all_params,
        all_dates,
        all_param_ids,
        all_date_ids,
    )
    context = {
        "forward": inputs["F"],
        "rate": inputs["r"],
        "averaging_start_date": inputs["averaging_start_date"].isoformat(),
        "expiration_date": inputs["expiration_date"].isoformat(),
        "contract_expiration_date": inputs["contract_expiration_date"].isoformat(),
    }
    leg = {
        "leg_id": "leg-1",
        "name": "Leg 1",
        "side": "BUY",
        "ratio": 1,
        "call_put": call_put,
        "strike": inputs["K"],
        "volatility": inputs["raw_v"],
    }
    snapshot = calculate_structure(
        "asian76",
        context,
        {"structure_quantity": 1, "contract_multiplier": 1},
        [leg],
        as_of=date.today(),
    )
    result_leg = snapshot["legs"][0]
    greeks = result_leg["unit"]["greeks"]
    snapshot["value"] = result_leg["unit"]["value"]
    snapshot["params"] = {
        "F": inputs["F"],
        "K": inputs["K"],
        "T": inputs["T"],
        "T_A": inputs["T_A"],
        "r": inputs["r"],
        "raw_v": inputs["raw_v"],
        "v": inputs["v"],
        "vol_adjustment_factor": inputs["vol_adjustment_factor"],
        "option_business_days": inputs["option_business_days"],
        "contract_business_days": inputs["contract_business_days"],
        "call_put": call_put,
        "averaging_start_date": inputs["averaging_start_date"].isoformat(),
        "expiration_date": inputs["expiration_date"].isoformat(),
        "contract_expiration_date": inputs["contract_expiration_date"].isoformat(),
    }
    greeks_grid = _build_pricer_greeks_grid(
        "pricer-asian76-greeks-grid",
        [
            {"greek": "Delta", "value": greeks["delta"]},
            {"greek": "Gamma", "value": greeks["gamma"]},
            {"greek": "Theta (Pre-Averaging)", "value": greeks["theta"]},
            {"greek": "Vega (Input Vol)", "value": greeks["vega"]},
            {"greek": "Rho", "value": greeks["rho"]},
        ],
        [{"name": "Greek", "id": "greek"}, {"name": "Value", "id": "value"}],
    )
    return (
        _build_pricer_result_card(
            "Option Value",
            _format_number(result_leg["unit"]["value"]),
            "Asian-76 continuous arithmetic-average approximation",
            tone="primary",
        ),
        greeks_grid,
        _build_pricer_result_card(
            "Time to Expiration",
            f"{inputs['T']:.4f} years",
            f"Averaging starts in {inputs['days_to_averaging_start']} days",
        ),
        _model_inputs_summary(snapshot),
        snapshot,
    )


def _legacy_asian_snapshot(
    call_put,
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    return calculate_option(
        1,
        "asian76",
        call_put,
        all_params,
        all_dates,
        all_param_ids,
        all_date_ids,
    )[-1]


def update_volatility_chart(
    n_clicks,
    option_type,
    call_put,
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    if _get_pricer_triggered_id() == "option-type" or not n_clicks:
        return _empty_pricer_figure("Calculate option price first.")
    if option_type != "asian76":
        return _empty_pricer_figure("Compatibility chart is available for Asian-76.")
    snapshot = _legacy_asian_snapshot(
        call_put,
        all_params,
        all_dates,
        all_param_ids,
        all_date_ids,
    )
    inputs = _parse_asian76_model_inputs(
        all_params,
        all_dates,
        all_param_ids,
        all_date_ids,
    )
    raw_vols = np.linspace(0.05, 1.0, 40)
    values = [
        asian_76(
            call_put,
            inputs["F"],
            inputs["K"],
            inputs["T"],
            inputs["T_A"],
            inputs["r"],
            raw_vol * inputs["vol_adjustment_factor"],
        )[0]
        for raw_vol in raw_vols
    ]
    fig = _line_figure(
        raw_vols,
        values,
        "Input Contract Volatility (σ)",
        marker_x=inputs["raw_v"],
        marker_y=snapshot["value"],
    )
    return fig


def update_rate_chart(
    n_clicks,
    option_type,
    call_put,
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    if _get_pricer_triggered_id() == "option-type" or not n_clicks:
        return _empty_pricer_figure("Calculate option price first.")
    if option_type != "asian76":
        return _empty_pricer_figure("Compatibility chart is available for Asian-76.")
    inputs = _parse_asian76_model_inputs(
        all_params,
        all_dates,
        all_param_ids,
        all_date_ids,
    )
    rates = np.linspace(-0.02, 0.15, 40)
    values = [
        asian_76(
            call_put,
            inputs["F"],
            inputs["K"],
            inputs["T"],
            inputs["T_A"],
            candidate,
            inputs["v"],
        )[0]
        for candidate in rates
    ]
    current = asian_76(
        call_put,
        inputs["F"],
        inputs["K"],
        inputs["T"],
        inputs["T_A"],
        inputs["r"],
        inputs["v"],
    )[0]
    return _line_figure(
        rates,
        values,
        "Risk-Free Rate (r)",
        marker_x=inputs["r"],
        marker_y=current,
    )


def update_time_chart(
    n_clicks,
    option_type,
    call_put,
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    if _get_pricer_triggered_id() == "option-type" or not n_clicks:
        return _empty_pricer_figure("Calculate option price first.")
    if option_type != "asian76":
        return _empty_pricer_figure("Compatibility chart is available for Asian-76.")
    snapshot = _legacy_asian_snapshot(
        call_put,
        all_params,
        all_dates,
        all_param_ids,
        all_date_ids,
    )
    series = time_decay_series(snapshot, max_points=60)
    return _line_figure(
        series["dates"],
        series["values"],
        "Valuation Date",
        annotation=(
            "Averaging starts; realized fixings required afterward."
            if series["truncated_at_averaging_start"]
            else None
        ),
    )


def update_extension_chart(
    n_clicks,
    option_type,
    call_put,
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    if _get_pricer_triggered_id() == "option-type" or not n_clicks:
        return _empty_pricer_figure("Calculate option price first.")
    if option_type != "asian76":
        return _empty_pricer_figure("Compatibility chart is available for Asian-76.")
    snapshot = _legacy_asian_snapshot(
        call_put,
        all_params,
        all_dates,
        all_param_ids,
        all_date_ids,
    )
    series = expiration_extension_series(snapshot)
    return _line_figure(
        series["dates"],
        series["values"],
        "Expiration / Averaging End",
    )


def update_correlation_chart(
    n_clicks,
    option_type,
    call_put,
    all_params,
    all_dates,
    all_param_ids=None,
    all_date_ids=None,
):
    del call_put, all_params, all_dates, all_param_ids, all_date_ids
    if _get_pricer_triggered_id() == "option-type" or not n_clicks:
        return _empty_pricer_figure("Calculate option price first.")
    if option_type != "kirk":
        return _empty_pricer_figure(
            "Correlation sensitivity is only available for Kirk spread options."
        )
    return _empty_pricer_figure("Use the active structure correlation chart.")
