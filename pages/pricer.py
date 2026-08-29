"""Trader-facing multi-leg option structure pricer."""

from __future__ import annotations

import copy
import datetime as dt
import math
from datetime import date, timedelta

import dash_ag_grid as dag
import numpy as np
import plotly.graph_objects as go
from dash import (
    ALL,
    MATCH,
    Input,
    Output,
    Patch,
    State,
    callback,
    clientside_callback,
    ctx,
    dcc,
    html,
    no_update,
)

from options.options_library import asian_76, black_76
from pricer_exchange_registry import (
    DEFAULT_EXCHANGE_MAPPING_ID,
    canonical_exchange_mapping_id,
    exchange_mapping_capture_message,
    exchange_mapping_for_asset_model,
    exchange_mapping_options,
    exchange_mapping_pricing_supported,
    exchange_option_mapping,
)
from pricer_surface_reference import (
    REFERENCE_SCHEMA_VERSION,
    build_published_surface_reference,
    build_surface_comparison_views,
)
from pricer_structure import (
    DEFAULT_ASSET,
    DELIVERY_SHAPE_LABELS,
    GREEK_FIELDS,
    GREEK_LABELS,
    MAX_LEGS,
    MAX_OPTION_HORIZON_DAYS,
    MODEL_LABELS,
    PREMIUM_CONVENTION_LABELS,
    SCHEMA_VERSION,
    SINGLE_ASSET_MODELS,
    SUPPORTED_ASSETS,
    SUPPORTED_DELIVERY_SHAPES,
    SUPPORTED_PREMIUM_CONVENTIONS,
    StructureValidationError,
    asset_price_spec,
    available_delivery_months,
    build_delivery_month_component,
    calculate_structure,
    correlation_sensitivity_series,
    count_business_days,
    default_contract_size,
    default_context,
    default_leg,
    default_model_for_asset,
    default_premium_convention,
    expiration_extension_series,
    parallel_volatility_series,
    payoff_series,
    rate_sensitivity_series,
    time_decay_series,
    volatility_adjustment,
)


option_types = [
    {"label": "Black-76", "value": "black76"},
    {"label": "Asian-76", "value": "asian76"},
    {"label": "Kirk", "value": "kirk"},
]
asset_options = [{"label": asset, "value": asset} for asset in SUPPORTED_ASSETS]
premium_convention_options = [
    {"label": PREMIUM_CONVENTION_LABELS[value], "value": value}
    for value in SUPPORTED_PREMIUM_CONVENTIONS
]
COMPACT_DELIVERY_SHAPE_LABELS = {
    "MONTH": "Month",
    "Q1": "Q1",
    "Q2": "Q2",
    "Q3": "Q3",
    "Q4": "Q4",
    "SUM": "Summer",
    "WIN": "Winter",
}

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

PRICER_WORKSPACE_SCHEMA_VERSION = 9
PRICER_CONTRACT_SIZE_MIGRATION_SCHEMA_VERSION = 7
DEFAULT_STRUCTURE_ID = "structure-1"
VOLATILITY_ADJUSTMENT_FIELDS = (
    "atm_vol_adjustment",
    "skew_vol_adjustment",
    "smile_vol_adjustment",
)
VOLATILITY_ADJUSTMENT_SCALE = 0.01
MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT = 50.0
FUTURES_STYLE_RATE_NOTE = (
    "The futures-style premium convention is undiscounted; the risk-free rate "
    "and Rho are not applicable."
)
UPFRONT_RATE_NOTE = (
    "Risk-free rate used to discount the upfront option premium and calculate Rho."
)


def _instance_id(component_type, structure_id):
    return {"type": component_type, "structure_id": structure_id}


def _month_only_field_id(structure_id, field):
    return {
        "type": "pricer-month-only-field",
        "structure_id": structure_id,
        "field": field,
    }


def _instance_persistence(structure_id, key):
    return f"pricer-{structure_id}-{key}"


def _nonnegative_click_count(value):
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _structure_display_label(structure_id, fallback_sequence=1):
    prefix, separator, suffix = str(structure_id or "").rpartition("-")
    if separator and prefix == "structure" and suffix.isdigit():
        return f"S{int(suffix)}"
    return f"S{fallback_sequence}"


def _default_workspace():
    return {
        "schema_version": PRICER_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": 2,
        "drafts": {},
        "structures": [
            {
                "structure_id": DEFAULT_STRUCTURE_ID,
                "label": "S1",
                "template": None,
            }
        ],
    }


def _migrate_template_premium_convention(
    template,
    *,
    migrate_legacy_contract_size=False,
    migrate_legacy_kirk_context=True,
    migrate_governed_mapping=False,
):
    if not isinstance(template, dict):
        return template
    mapping_id = canonical_exchange_mapping_id(template.get("mapping_id"))
    if mapping_id is not None:
        template["mapping_id"] = mapping_id
    template.pop("structure_type", None)
    context = template.get("context")
    if not isinstance(context, dict):
        return template
    context_mapping_id = canonical_exchange_mapping_id(
        context.get("exchange_mapping_id")
    )
    if context_mapping_id is not None:
        context["exchange_mapping_id"] = context_mapping_id
    context.pop("structure_type", None)
    mapping = exchange_option_mapping(mapping_id or context_mapping_id)
    if mapping is not None:
        template["mapping_id"] = mapping.mapping_id
        template["model"] = mapping.model
        template["asset"] = mapping.asset
        context["exchange_mapping_id"] = mapping.mapping_id
        context["premium_convention"] = mapping.premium_convention
    model = template.get("model")
    if model not in MODEL_LABELS:
        model = "black76"
    legacy_asset = template.get("asset")
    asset = legacy_asset if legacy_asset in SUPPORTED_ASSETS else DEFAULT_ASSET
    if mapping is not None:
        asset = mapping.asset
    if model == "kirk" and migrate_legacy_kirk_context:
        if context.get("asset_1_forward") is None and context.get("asset_1") is not None:
            context["asset_1_forward"] = context["asset_1"]
        if context.get("asset_2_forward") is None and context.get("asset_2") is not None:
            context["asset_2_forward"] = context["asset_2"]
        if not context.get("asset_1_code") and legacy_asset in SUPPORTED_ASSETS:
            context["asset_1_code"] = legacy_asset
        common_reference_expiry = context.get("contract_expiration_date")
        if not context.get("asset_1_reference_expiry") and common_reference_expiry:
            context["asset_1_reference_expiry"] = common_reference_expiry
        if not context.get("asset_2_reference_expiry") and common_reference_expiry:
            context["asset_2_reference_expiry"] = common_reference_expiry
        if not context.get("contractual_expiry") and context.get("expiration_date"):
            context["contractual_expiry"] = context["expiration_date"]
        context.pop("asset_1", None)
        context.pop("asset_2", None)
    premium_convention = context.get("premium_convention")
    if premium_convention in (None, "", "product_default") or (
        model == "kirk" and premium_convention == "upfront"
    ):
        context["premium_convention"] = default_premium_convention(asset, model)
    if (
        model in SINGLE_ASSET_MODELS
        and context.get("premium_convention") == "futures_style"
    ):
        context["rate"] = 0.0
    context.setdefault("delivery_shape", "MONTH")
    if (migrate_legacy_contract_size or migrate_governed_mapping) and model != "kirk":
        try:
            legacy_size = float(template.get("contract_multiplier", 1))
        except (TypeError, ValueError, OverflowError):
            legacy_size = None
        if legacy_size == 1.0 or (migrate_governed_mapping and mapping is not None):
            valuation_date = parse_date(
                template.get("valuation_date"),
                date.today(),
            )
            resolved_context = default_context(model, valuation_date)
            resolved_context.update(context)
            resolved_context["asset"] = asset
            try:
                template["contract_multiplier"] = default_contract_size(
                    asset,
                    resolved_context,
                    as_of=valuation_date,
                )
            except StructureValidationError:
                pass
    return template


def _is_valid_calculation_snapshot(snapshot):
    if not (
        isinstance(snapshot, dict)
        and snapshot.get("schema_version") == SCHEMA_VERSION
        and snapshot.get("model") in MODEL_LABELS
        and isinstance(snapshot.get("context"), dict)
        and isinstance(snapshot.get("legs"), list)
        and snapshot.get("legs")
        and isinstance(snapshot.get("totals"), dict)
        and isinstance(snapshot.get("greek_fields"), list)
        and isinstance(snapshot.get("greek_labels"), dict)
        and isinstance(snapshot.get("model_label"), str)
        and isinstance(snapshot.get("calculation_date"), str)
    ):
        return False
    context = snapshot["context"]
    common_context = {
        "asset",
        "premium_convention",
        "resolved_premium_convention",
        "delivery_shape",
        "margin_style",
        "expiration_date",
        "contract_expiration_date",
        "time_to_expiry",
        "day_count_basis",
    }
    if snapshot["model"] in SINGLE_ASSET_MODELS:
        model_context = {
            "forward",
            "rate",
            "vol_adjustment_factor",
            "variance_calendar_code",
        }
    else:
        model_context = {
            "asset_1_code",
            "asset_2_code",
            "asset_1_forward",
            "asset_2_forward",
            "asset_1_price_unit",
            "asset_2_price_unit",
            "asset_1_calendar_code",
            "asset_2_calendar_code",
            "asset_1_contractual_business_days",
            "asset_2_contractual_business_days",
            "asset_1_reference_business_days",
            "asset_2_reference_business_days",
            "asset_1_vol_adjustment_factor",
            "asset_2_vol_adjustment_factor",
            "asset_1_reference_expiry",
            "asset_2_reference_expiry",
            "contractual_expiry",
            "correlation",
            "discount_factor",
        }
    if snapshot["model"] == "asian76":
        model_context |= {"averaging_start_date", "time_to_averaging_start"}
    if not common_context.issubset(context) or not model_context.issubset(context):
        return False
    if (
        snapshot["model"] != "kirk"
        and not context.get("delivery_components")
        and not {
        "option_business_days",
        "contract_business_days",
        }.issubset(context)
    ):
        return False
    totals = snapshot["totals"]
    if not {
        "trade_value",
        "unit_structure_value",
        "trade_greeks",
        "unit_structure_greeks",
    }.issubset(totals):
        return False
    if not isinstance(totals["trade_greeks"], dict) or not isinstance(
        totals["unit_structure_greeks"], dict
    ):
        return False
    for leg in snapshot["legs"]:
        if not isinstance(leg, dict) or not {
            "leg_id",
            "name",
            "side",
            "ratio",
            "call_put",
            "strike",
            "unit",
            "trade_contribution",
        }.issubset(leg):
            return False
        if not all(
            isinstance(leg.get(group), dict)
            and "value" in leg[group]
            and isinstance(leg[group].get("greeks"), dict)
            for group in ("unit", "trade_contribution")
        ):
            return False
        quote_fields = (
            {"quote_basis", "entered_premium", "raw_volatility", "volatility_used"}
            if snapshot["model"] in SINGLE_ASSET_MODELS
            else {
                "raw_volatility_asset_1",
                "raw_volatility_asset_2",
                "volatility_asset_1_used",
                "volatility_asset_2_used",
            }
        )
        if not quote_fields.issubset(leg):
            return False
    return True


def _normalize_workspace(workspace):
    if not isinstance(workspace, dict):
        return _default_workspace()
    raw_structures = workspace.get("structures")
    if not isinstance(raw_structures, list):
        return _default_workspace()
    try:
        workspace_schema_version = int(workspace.get("schema_version", 0))
        migrate_legacy_contract_size = (
            workspace_schema_version
            < PRICER_CONTRACT_SIZE_MIGRATION_SCHEMA_VERSION
        )
        migrate_legacy_kirk_context = migrate_legacy_contract_size
        migrate_governed_mapping = (
            workspace_schema_version < PRICER_WORKSPACE_SCHEMA_VERSION
        )
    except (TypeError, ValueError, OverflowError):
        migrate_legacy_contract_size = True
        migrate_legacy_kirk_context = True
        migrate_governed_mapping = True
    structures = []
    seen = set()
    for index, raw_structure in enumerate(raw_structures, start=1):
        if not isinstance(raw_structure, dict):
            continue
        structure_id = str(raw_structure.get("structure_id") or "").strip()
        if not structure_id or structure_id in seen:
            continue
        seen.add(structure_id)
        template = (
            copy.deepcopy(raw_structure.get("template"))
            if isinstance(raw_structure.get("template"), dict)
            else None
        )
        if template is not None:
            _migrate_template_premium_convention(
                template,
                migrate_legacy_contract_size=migrate_legacy_contract_size,
                migrate_legacy_kirk_context=migrate_legacy_kirk_context,
                migrate_governed_mapping=migrate_governed_mapping,
            )
        structures.append(
            {
                "structure_id": structure_id,
                "label": _structure_display_label(structure_id, index),
                "template": template,
            }
        )
    if not structures:
        return _default_workspace()
    numeric_sequences = []
    for structure in structures:
        prefix, separator, suffix = structure["structure_id"].rpartition("-")
        if separator and prefix == "structure" and suffix.isdigit():
            numeric_sequences.append(int(suffix))
    next_sequence = workspace.get("next_structure_sequence")
    try:
        next_sequence = max(
            int(next_sequence),
            len(structures) + 1,
            max(numeric_sequences, default=0) + 1,
        )
    except (TypeError, ValueError):
        next_sequence = max(len(structures) + 1, max(numeric_sequences, default=0) + 1)
    while f"structure-{next_sequence}" in seen:
        next_sequence += 1
    raw_drafts = workspace.get("drafts")
    drafts = {
        structure_id: copy.deepcopy(template)
        for structure_id, template in (
            raw_drafts.items() if isinstance(raw_drafts, dict) else []
        )
        if structure_id in seen and isinstance(template, dict)
    }
    for template in drafts.values():
        _migrate_template_premium_convention(
            template,
            migrate_legacy_contract_size=migrate_legacy_contract_size,
            migrate_legacy_kirk_context=migrate_legacy_kirk_context,
            migrate_governed_mapping=migrate_governed_mapping,
        )
    return {
        "schema_version": PRICER_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": next_sequence,
        "drafts": drafts,
        "structures": structures,
    }


def _reduce_workspace(workspace, action, structure_id=None, template=None):
    normalized = _normalize_workspace(workspace)
    structures = copy.deepcopy(normalized["structures"])
    drafts = copy.deepcopy(normalized["drafts"])
    next_sequence = normalized["next_structure_sequence"]
    if action in {"add", "duplicate"}:
        new_id = f"structure-{next_sequence}"
        structures.append(
            {
                "structure_id": new_id,
                "label": _structure_display_label(new_id, next_sequence),
                "template": copy.deepcopy(template) if action == "duplicate" else None,
            }
        )
        if action == "duplicate" and isinstance(template, dict):
            drafts[new_id] = copy.deepcopy(template)
        next_sequence += 1
    elif action == "remove" and len(structures) > 1:
        structures = [
            structure
            for structure in structures
            if structure["structure_id"] != structure_id
        ]
        drafts.pop(structure_id, None)
    return {
        "schema_version": PRICER_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": next_sequence,
        "drafts": drafts,
        "structures": structures,
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
        title=message,
    )


def _build_pricer_result_card(
    label,
    value,
    detail=None,
    tone="neutral",
    *,
    detail_on_hover=False,
    structure_id=DEFAULT_STRUCTURE_ID,
):
    detail_id = (
        f"pricer-{structure_id}-result-card-{tone}-detail"
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
    heading_component = {
        1: html.H1,
        2: html.H2,
        3: html.H3,
    }.get(heading_level, html.H2)
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


def _build_delivery_shape_field(
    model="black76",
    structure_id=DEFAULT_STRUCTURE_ID,
    value="MONTH",
    asset=DEFAULT_ASSET,
    mapping_id=None,
):
    supports_strips = (
        (asset == "TTF" and model == "black76")
        or (asset == "JKM" and model in {"black76", "asian76"})
        or (
            asset == "NBP"
            and model == "black76"
            and canonical_exchange_mapping_id(mapping_id) == "ICE-NBP-UKF"
        )
    )
    available_shapes = (
        SUPPORTED_DELIVERY_SHAPES if supports_strips else ("MONTH",)
    )
    resolved_value = value if value in available_shapes else "MONTH"
    return _build_pricer_field(
        "Shape",
        dcc.Dropdown(
            id=_context_id(
                model,
                "delivery_shape",
                structure_id=structure_id,
            ),
            options=[
                {
                    "label": COMPACT_DELIVERY_SHAPE_LABELS[shape],
                    "value": shape,
                }
                for shape in available_shapes
            ],
            value=resolved_value,
            clearable=False,
            disabled=not supports_strips,
            persistence=f"pricer-{structure_id}-{model}-delivery-shape",
            persistence_type="session",
            className="pricer-filter-dropdown pricer-shape-dropdown",
        ),
        class_name="pricer-shape-field",
        hint=(
            "Strips use governed monthly expiries and product-specific weights."
            if mapping_id
            else (
                "Strips use exact JKM exchange expiries selected by pricing model."
                if asset == "JKM"
                else "Monthly and seasonal strips use exact TTF TFO expiries."
            )
        ),
    )


def _delivery_month_options(asset, model, as_of, mapping_id=None):
    return [
        {
            "label": delivery_month.strftime("%b-%y"),
            "value": delivery_month.isoformat(),
        }
        for delivery_month in available_delivery_months(
            asset,
            model,
            as_of,
            mapping_id=mapping_id,
        )
    ]


def _resolved_delivery_month(value, options):
    valid_values = {option["value"] for option in options}
    if value:
        try:
            parsed = parse_date(value)
            normalized = date(parsed.year, parsed.month, 1).isoformat()
        except (TypeError, ValueError):
            normalized = None
        if normalized in valid_values:
            return normalized
    return options[0]["value"] if options else None


def _build_delivery_month_field(
    model="black76",
    structure_id=DEFAULT_STRUCTURE_ID,
    value=None,
    *,
    asset=DEFAULT_ASSET,
    delivery_shape="MONTH",
    as_of=None,
    mapping_id=None,
):
    as_of = parse_date(as_of, date.today())
    is_governed_month = (
        str(delivery_shape or "MONTH").strip().upper() == "MONTH"
    )
    options = _delivery_month_options(asset, model, as_of, mapping_id)
    resolved_value = _resolved_delivery_month(value, options)
    return _build_pricer_field(
        "Delivery",
        dcc.Dropdown(
            id=_context_id(
                model,
                "delivery_month",
                structure_id=structure_id,
            ),
            options=options,
            value=resolved_value,
            clearable=False,
            disabled=not is_governed_month or not options,
            persistence=f"pricer-{structure_id}-{model}-delivery-month",
            persistence_type="session",
            className="pricer-filter-dropdown pricer-delivery-month-dropdown",
        ),
        class_name="pricer-delivery-month-field",
        hint="Selects the governed monthly contract and its exchange expiry.",
        field_id=_instance_id("pricer-delivery-month-field", structure_id),
        style={} if is_governed_month else {"display": "none"},
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


def _surface_comparison_figure(view):
    curve_points = view.get("curve_points") or []
    quote_points = view.get("quote_points") or []
    governed = view.get("source_kind") == "governed"
    curve_name = "Calibrated surface" if governed else "Operational surface"
    curve_color = "#1d4ed8" if governed else "#0f766e"
    curve_dash = "solid" if governed else "dash"
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=[point["delta"] for point in curve_points],
            y=[100.0 * point["input_volatility"] for point in curve_points],
            customdata=[
                [
                    point["strike"],
                    100.0 * point["pricing_volatility"],
                    100.0 * point["call_delta"],
                ]
                for point in curve_points
            ],
            mode="lines",
            name=curve_name,
            line={"color": curve_color, "width": 2.2, "dash": curve_dash},
            hovertemplate=(
                f"<b>{curve_name}</b><br>"
                "Strike %{customdata[0]:.4f}<br>"
                "Input IV %{y:.2f}%<br>"
                "Pricing IV %{customdata[1]:.2f}%<br>"
                "Call delta %{customdata[2]:.2f}%<extra></extra>"
            ),
        )
    )
    if quote_points:
        connector_x = []
        connector_y = []
        for point in quote_points:
            connector_x.extend([point["delta"], point["delta"], None])
            connector_y.extend(
                [
                    100.0 * point["reference_volatility"],
                    100.0 * point["contract_volatility"],
                    None,
                ]
            )
        figure.add_trace(
            go.Scatter(
                x=connector_x,
                y=connector_y,
                mode="lines",
                line={"color": "rgba(217, 119, 6, 0.48)", "width": 1.2},
                hoverinfo="skip",
                showlegend=False,
            )
        )
        figure.add_trace(
            go.Scatter(
                x=[point["delta"] for point in quote_points],
                y=[100.0 * point["contract_volatility"] for point in quote_points],
                customdata=[
                    [
                        point["structure_label"],
                        point["leg_label"],
                        "Call" if point["call_put"] == "C" else "Put",
                        point["strike"],
                        point["quote_basis_label"],
                        100.0 * point["contract_volatility"],
                        100.0 * point["reference_volatility"],
                        f"{point['difference_vol_points']:+.2f}",
                        100.0 * point["delta"],
                        point["surface_cob"],
                        point["source"],
                    ]
                    for point in quote_points
                ],
                mode="markers",
                name="Contract vol",
                marker={
                    "color": "#d97706",
                    "size": 9,
                    "symbol": [
                        "triangle-up" if point["call_put"] == "C" else "triangle-down"
                        for point in quote_points
                    ],
                    "line": {"color": "#7c2d12", "width": 1},
                },
                hovertemplate=(
                    "<b>%{customdata[0]} · %{customdata[1]}</b><br>"
                    "%{customdata[2]} · Strike %{customdata[3]:.4f}<br>"
                    "Quote basis %{customdata[4]}<br>"
                    "Contract vol %{customdata[5]:.2f}%<br>"
                    "Reference vol %{customdata[6]:.2f}%<br>"
                    "Difference %{customdata[7]} vol pts<br>"
                    "Model delta %{customdata[8]:.2f}%<br>"
                    "Surface COB %{customdata[9]}<br>"
                    "Source %{customdata[10]}<extra></extra>"
                ),
            )
        )
    figure.update_layout(
        xaxis=_pricer_axis(
            "Delta",
            range=[0.0, 1.0],
            fixedrange=False,
            tickmode="array",
            tickvals=[0.10, 0.25, 0.50, 0.75, 0.90],
            ticktext=["10P", "25P", "ATM", "25C", "10C"],
        ),
        yaxis=_pricer_axis("IV (%)", ticksuffix="%"),
        shapes=[
            {
                "type": "line",
                "xref": "x",
                "yref": "paper",
                "x0": 0.5,
                "x1": 0.5,
                "y0": 0.0,
                "y1": 1.0,
                "line": {"color": "rgba(100, 116, 139, 0.28)", "width": 1},
                "layer": "below",
            }
        ],
        hovermode="closest",
        margin={"l": 48, "r": 12, "t": 8, "b": 52},
        legend={
            "orientation": "h",
            "yanchor": "top",
            "y": -0.16,
            "xanchor": "center",
            "x": 0.5,
            "font": {"size": 9, "color": PRICER_CHART_MUTED},
        },
        uirevision=f"surface-comparison-{view.get('context_key')}",
    )
    return _style_pricer_figure(figure, height=270).update_layout(
        hovermode="closest",
        margin={"l": 48, "r": 12, "t": 8, "b": 52},
        uirevision=f"surface-comparison-{view.get('context_key')}",
    )


def _surface_publication_label(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(value)
    timezone_suffix = " UTC" if parsed.tzinfo is not None else ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M") + timezone_suffix


def _surface_comparison_card(view):
    title = (
        f"{view.get('structure_label')} · {view.get('asset')} · "
        f"{view.get('delivery_label')} · {view.get('model_label')}"
    )
    status = view.get("status")
    card_children = [
        html.Div(
            [html.H3(title, className="pricer-surface-card-title")],
            className="pricer-surface-card-header",
        )
    ]
    if status != "ready":
        tone = "warning" if status == "unsupported" else "danger"
        card_children.append(_build_pricer_message(view.get("message"), tone=tone))
        return html.Section(
            card_children,
            className=f"pricer-surface-card pricer-surface-card-{status}",
            **{"aria-label": title},
        )

    published_label = _surface_publication_label(view.get("published_at"))
    metadata = [
        html.Span(view.get("source_label"), className="pricer-surface-source"),
        html.Span(f"COB {view.get('surface_cob')}"),
    ]
    if published_label:
        metadata.append(html.Span(f"Published {published_label}"))
    card_children.append(
        html.Div(metadata, className="pricer-surface-card-meta")
    )
    warnings = view.get("warnings") or []
    if warnings:
        card_children.append(
            html.Div(
                [html.Span(message) for message in warnings],
                className="pricer-surface-card-warnings",
                role="status",
            )
        )
    card_children.append(
        dcc.Loading(
            dcc.Graph(
                figure=_surface_comparison_figure(view),
                config=PRICER_GRAPH_CONFIG,
                className="pricer-surface-card-graph",
            ),
            type="circle",
        )
    )
    quote_items = []
    for point in view.get("quote_points") or []:
        difference = point["difference_vol_points"]
        quote_items.append(
            html.Span(
                (
                    f"{point['short_label']} "
                    f"{point['contract_volatility']:.2%} vs "
                    f"{point['reference_volatility']:.2%} "
                    f"({difference:+.2f} vol pts)"
                ),
                className=(
                    "pricer-surface-quote-key-item "
                    + (
                        "pricer-surface-quote-above"
                        if difference > 0.0
                        else "pricer-surface-quote-below"
                        if difference < 0.0
                        else "pricer-surface-quote-flat"
                    )
                ),
            )
        )
    if quote_items:
        card_children.append(
            html.Div(quote_items, className="pricer-surface-quote-key")
        )
    return html.Section(
        card_children,
        className="pricer-surface-card",
        **{"aria-label": title},
    )


def _calculated_surface_structures(workspace, persisted_calculations):
    workspace = _normalize_workspace(workspace)
    calculations = (
        persisted_calculations if isinstance(persisted_calculations, dict) else {}
    )
    structures = []
    for structure in workspace["structures"]:
        structure_id = structure["structure_id"]
        snapshot = calculations.get(structure_id)
        if not _is_valid_calculation_snapshot(snapshot):
            continue
        structures.append(
            {
                "structure_id": structure_id,
                "structure_label": structure["label"],
                "snapshot": snapshot,
            }
        )
    return structures


def _build_pricer_field(
    label,
    control,
    class_name=None,
    hint=None,
    *,
    field_id=None,
    style=None,
):
    classes = ["pricer-field"]
    if class_name:
        classes.append(class_name)
    properties = {"className": " ".join(classes)}
    if field_id is not None:
        properties["id"] = field_id
    if style is not None:
        properties["style"] = style
    if hint:
        properties["title"] = hint
    return html.Label(
        [
            html.Span(
                label,
                className="pricer-field-label",
                title=hint,
            ),
            control,
            (
                html.Span(
                    hint,
                    className="pricer-field-hint",
                    title=hint,
                )
                if hint
                else None
            ),
        ],
        **properties,
    )


def _delivery_year_field_style(delivery_shape):
    shape = str(delivery_shape or "MONTH").strip().upper()
    return {} if shape != "MONTH" else {"display": "none"}


def _month_only_field_style(delivery_shape):
    shape = str(delivery_shape or "MONTH").strip().upper()
    return {} if shape == "MONTH" else {"display": "none"}


def _build_pricer_number_input(
    input_id,
    value,
    *,
    minimum=None,
    maximum=None,
    step=None,
    persistence_key=None,
    disabled=False,
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
        disabled=disabled,
        className="pricer-number-input",
    )


def _build_pricer_date_picker(
    picker_id,
    value,
    *,
    minimum=None,
    maximum=None,
    allow_past=False,
    persistence_key=None,
    disabled=False,
):
    resolved_minimum = minimum
    if resolved_minimum is None and not allow_past:
        resolved_minimum = date.today()
    resolved_maximum = maximum or (
        date.today() + timedelta(days=MAX_OPTION_HORIZON_DAYS)
    )
    return dcc.DatePickerSingle(
        id=picker_id,
        min_date_allowed=resolved_minimum,
        max_date_allowed=resolved_maximum,
        date=value,
        display_format="YYYY-MM-DD",
        persistence=True if persistence_key is None else persistence_key,
        persistence_type="session",
        disabled=disabled,
        className="pricer-date-picker",
    )


def _context_id(model, param, is_date=False, structure_id=DEFAULT_STRUCTURE_ID):
    return {
        "type": "pricer-context-date" if is_date else "pricer-context-param",
        "structure_id": structure_id,
        "model": model,
        "param": param,
    }


def _migrated_kirk_context_values(values, legacy_asset=None):
    migrated = copy.deepcopy(values) if isinstance(values, dict) else {}
    if migrated.get("asset_1_forward") is None and migrated.get("asset_1") is not None:
        migrated["asset_1_forward"] = migrated["asset_1"]
    if migrated.get("asset_2_forward") is None and migrated.get("asset_2") is not None:
        migrated["asset_2_forward"] = migrated["asset_2"]
    if not migrated.get("asset_1_code") and legacy_asset in SUPPORTED_ASSETS:
        migrated["asset_1_code"] = legacy_asset
    common_reference_expiry = migrated.get("contract_expiration_date")
    if not migrated.get("asset_1_reference_expiry") and common_reference_expiry:
        migrated["asset_1_reference_expiry"] = common_reference_expiry
    if not migrated.get("asset_2_reference_expiry") and common_reference_expiry:
        migrated["asset_2_reference_expiry"] = common_reference_expiry
    if not migrated.get("contractual_expiry") and migrated.get("expiration_date"):
        migrated["contractual_expiry"] = migrated["expiration_date"]
    migrated.pop("asset_1", None)
    migrated.pop("asset_2", None)
    return migrated


def _surface_proxy_note(
    mapping_id,
    *,
    asset=None,
    model=None,
    show_jkm_vanilla_surface_note=False,
):
    base_class = "pricer-surface-proxy-note"
    if mapping_id == "ICE-JKM-JKZ" or (
        show_jkm_vanilla_surface_note
        and asset == "JKM"
        and model == "black76"
        and mapping_id is None
    ):
        return (
            "JKM APO surface, expiry-adjusted to JKZ.",
            f"{base_class} pricer-jkm-vanilla-surface-note",
        )
    return "", base_class


def _build_context_form(
    model,
    structure_id=DEFAULT_STRUCTURE_ID,
    values=None,
    *,
    include_delivery_shape=True,
    asset=DEFAULT_ASSET,
    show_jkm_vanilla_surface_note=False,
    mapping_id=None,
):
    defaults = default_context(model, date.today())
    mapping = exchange_option_mapping(mapping_id)
    if model == "kirk":
        values = _migrated_kirk_context_values(values)
    if isinstance(values, dict):
        defaults.update(values)
    if mapping is not None:
        defaults["premium_convention"] = mapping.premium_convention
    elif not isinstance(values, dict) or values.get("premium_convention") in (
        None,
        "",
        "product_default",
    ):
        defaults["premium_convention"] = default_premium_convention(asset, model)
    governed_delivery_month = None
    governed_jkm_apo = False
    if (
        model != "kirk"
        and str(defaults.get("delivery_shape") or "MONTH").strip().upper()
        == "MONTH"
    ):
        delivery_options = _delivery_month_options(
            asset,
            model,
            date.today(),
            mapping_id,
        )
        requested_delivery_month = defaults.get("delivery_month")
        governed_delivery_month = _resolved_delivery_month(
            requested_delivery_month,
            delivery_options,
        )
        defaults["delivery_month"] = governed_delivery_month
        if governed_delivery_month:
            component = build_delivery_month_component(
                asset,
                model,
                governed_delivery_month,
                date.today(),
                defaults.get("forward", 1.0),
                mapping_id=mapping_id,
            )
            defaults["contract_expiration_date"] = component[
                "contract_expiration_date"
            ]
            governed_jkm_apo = asset == "JKM" and model == "asian76"
            resolved_requested_month = _resolved_delivery_month(
                requested_delivery_month,
                delivery_options,
            )
            selection_changed = resolved_requested_month != requested_delivery_month
            if governed_jkm_apo:
                defaults["averaging_start_date"] = component[
                    "averaging_start_date"
                ]
            if governed_jkm_apo or selection_changed or mapping is not None:
                defaults["expiration_date"] = component[
                    "option_expiration_date"
                ]
    is_futures_style = defaults.get("premium_convention") == "futures_style"
    persistence_prefix = f"pricer-{structure_id}-{model}"
    surface_proxy_note, surface_proxy_note_class = _surface_proxy_note(
        mapping_id,
        asset=asset,
        model=model,
        show_jkm_vanilla_surface_note=show_jkm_vanilla_surface_note,
    )
    surface_proxy_note_component = html.Span(
        surface_proxy_note,
        id=_instance_id("pricer-surface-proxy-note", structure_id),
        className=surface_proxy_note_class,
        role="note",
    )
    fields = []
    if model in {"black76", "american_futures"}:
        if include_delivery_shape:
            fields.append(
                _build_delivery_shape_field(
                    model,
                    structure_id,
                    defaults["delivery_shape"],
                    asset,
                    mapping_id,
                )
            )
        fields.extend(
            [
                _build_pricer_field(
                    "First year",
                    _build_pricer_number_input(
                        _context_id(model, "delivery_year", structure_id=structure_id),
                        defaults["delivery_year"],
                        minimum=2000,
                        maximum=2100,
                        step=1,
                        persistence_key=f"{persistence_prefix}-delivery-year",
                    ),
                    class_name="pricer-number-field",
                    hint=(
                        "Winter runs from October of the first delivery year "
                        "to March of the following year."
                    ),
                    field_id=_instance_id(
                        "pricer-delivery-year-field",
                        structure_id,
                    ),
                    style=_delivery_year_field_style(
                        defaults.get("delivery_shape")
                    ),
                ),
                _build_pricer_field(
                    "Forward",
                    _build_pricer_number_input(
                        _context_id(model, "forward", structure_id=structure_id),
                        defaults["forward"],
                        minimum=0.01,
                        persistence_key=f"{persistence_prefix}-forward",
                    ),
                    class_name="pricer-number-field pricer-forward-field",
                ),
                surface_proxy_note_component,
                _build_pricer_field(
                    "Option exp",
                    _build_pricer_date_picker(
                        _context_id(model, "expiration_date", True, structure_id),
                        defaults["expiration_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-expiration",
                        disabled=bool(mapping_id and governed_delivery_month),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only; strips derive each monthly expiry.",
                    field_id=_month_only_field_id(
                        structure_id, "option-expiration"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Exchange exp",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "contract_expiration_date",
                            True,
                            structure_id,
                        ),
                        defaults["contract_expiration_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-contract-expiration",
                        disabled=bool(mapping_id and governed_delivery_month),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only.",
                    field_id=_month_only_field_id(
                        structure_id, "contract-expiration"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Rate",
                    _build_pricer_number_input(
                        _context_id(model, "rate", structure_id=structure_id),
                        0.0 if is_futures_style else defaults["rate"],
                        minimum=-1,
                        maximum=2,
                        step=0.000001,
                        persistence_key=f"{persistence_prefix}-rate",
                        disabled=is_futures_style,
                    ),
                    class_name="pricer-number-field pricer-rate-field",
                    field_id=_instance_id("pricer-rate-field", structure_id),
                    hint=(
                        FUTURES_STYLE_RATE_NOTE
                        if is_futures_style
                        else UPFRONT_RATE_NOTE
                    ),
                ),
            ]
        )
    elif model == "asian76":
        if include_delivery_shape:
            fields.append(
                _build_delivery_shape_field(
                    model,
                    structure_id,
                    defaults["delivery_shape"],
                    asset,
                    mapping_id,
                )
            )
        fields.extend(
            [
                _build_pricer_field(
                    "First year",
                    _build_pricer_number_input(
                        _context_id(model, "delivery_year", structure_id=structure_id),
                        defaults["delivery_year"],
                        minimum=2000,
                        maximum=2100,
                        step=1,
                        persistence_key=f"{persistence_prefix}-delivery-year",
                    ),
                    class_name="pricer-number-field",
                    hint=(
                        "Winter runs from October of the first delivery year "
                        "to March of the following year."
                    ),
                    field_id=_instance_id(
                        "pricer-delivery-year-field",
                        structure_id,
                    ),
                    style=_delivery_year_field_style(
                        defaults.get("delivery_shape")
                    ),
                ),
                _build_pricer_field(
                    "Forward",
                    _build_pricer_number_input(
                        _context_id(model, "forward", structure_id=structure_id),
                        defaults["forward"],
                        minimum=0.01,
                        persistence_key=f"{persistence_prefix}-forward",
                    ),
                    class_name="pricer-number-field pricer-forward-field",
                ),
                _build_pricer_field(
                    "Avg start",
                    _build_pricer_date_picker(
                        _context_id(model, "averaging_start_date", True, structure_id),
                        defaults["averaging_start_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-averaging-start",
                        disabled=bool(mapping_id and governed_jkm_apo),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only; strips derive each monthly start.",
                    field_id=_month_only_field_id(
                        structure_id, "averaging-start"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Avg end",
                    _build_pricer_date_picker(
                        _context_id(model, "expiration_date", True, structure_id),
                        defaults["expiration_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-expiration",
                        disabled=bool(mapping_id and governed_jkm_apo),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only; strips derive each monthly expiry.",
                    field_id=_month_only_field_id(
                        structure_id, "option-expiration"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Exchange exp",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "contract_expiration_date",
                            True,
                            structure_id,
                        ),
                        defaults["contract_expiration_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-contract-expiration",
                        disabled=bool(mapping_id and governed_delivery_month),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only.",
                    field_id=_month_only_field_id(
                        structure_id, "contract-expiration"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Rate",
                    _build_pricer_number_input(
                        _context_id(model, "rate", structure_id=structure_id),
                        0.0 if is_futures_style else defaults["rate"],
                        minimum=-1,
                        maximum=2,
                        step=0.000001,
                        persistence_key=f"{persistence_prefix}-rate",
                        disabled=is_futures_style,
                    ),
                    class_name="pricer-number-field pricer-rate-field",
                    field_id=_instance_id("pricer-rate-field", structure_id),
                    hint=(
                        FUTURES_STYLE_RATE_NOTE
                        if is_futures_style
                        else UPFRONT_RATE_NOTE
                    ),
                ),
            ]
        )
        fields.append(surface_proxy_note_component)
    elif model == "kirk":
        fields.extend(
            [
                html.Div(
                    id=_instance_id("pricer-rate-field", structure_id),
                    className="pricer-rate-field",
                    style={"display": "none"},
                ),
                _build_pricer_field(
                    "Asset 1 forward",
                    _build_pricer_number_input(
                        _context_id(
                            model,
                            "asset_1_forward",
                            structure_id=structure_id,
                        ),
                        defaults["asset_1_forward"],
                        minimum=0.01,
                        persistence_key=f"{persistence_prefix}-asset-1-forward-v1",
                    ),
                    class_name="pricer-number-field",
                ),
                _build_pricer_field(
                    "Asset 2 forward",
                    _build_pricer_number_input(
                        _context_id(
                            model,
                            "asset_2_forward",
                            structure_id=structure_id,
                        ),
                        defaults["asset_2_forward"],
                        minimum=0.01,
                        persistence_key=f"{persistence_prefix}-asset-2-forward-v1",
                    ),
                    class_name="pricer-number-field",
                ),
                _build_pricer_field(
                    "Asset 1 vol reference exp",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "asset_1_reference_expiry",
                            True,
                            structure_id,
                        ),
                        defaults["asset_1_reference_expiry"],
                        allow_past=True,
                        persistence_key=(
                            f"{persistence_prefix}-asset-1-reference-expiry-v1"
                        ),
                    ),
                    class_name="pricer-date-field",
                ),
                _build_pricer_field(
                    "Asset 2 vol reference exp",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "asset_2_reference_expiry",
                            True,
                            structure_id,
                        ),
                        defaults["asset_2_reference_expiry"],
                        allow_past=True,
                        persistence_key=(
                            f"{persistence_prefix}-asset-2-reference-expiry-v1"
                        ),
                    ),
                    class_name="pricer-date-field",
                ),
                _build_pricer_field(
                    "Contractual option expiry",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "contractual_expiry",
                            True,
                            structure_id,
                        ),
                        defaults["contractual_expiry"],
                        allow_past=True,
                        persistence_key=(
                            f"{persistence_prefix}-contractual-expiry-v1"
                        ),
                    ),
                    class_name="pricer-date-field",
                ),
                _build_pricer_field(
                    "Corr",
                    _build_pricer_number_input(
                        _context_id(model, "correlation", structure_id=structure_id),
                        defaults["correlation"],
                        minimum=-1,
                        maximum=1,
                        step=0.00001,
                        persistence_key=f"{persistence_prefix}-correlation",
                    ),
                    class_name="pricer-number-field",
                ),
                html.Div(
                    "Kirk is undiscounted, so rate and Rho are not applicable. "
                    "It requires two input vols; PREMIUM quoting is unavailable "
                    "because one premium cannot determine both vols.",
                    className="pricer-inline-method-note",
                    role="note",
                ),
            ]
        )
        fields.append(surface_proxy_note_component)
    return html.Div(fields, className="pricer-context-grid")


def _calculated_value_getter(field):
    return {
        "function": (
            "params.context && params.context.pricingRows && params.data && "
            "params.context.pricingRows[params.data.leg_id] "
            f"? params.context.pricingRows[params.data.leg_id][{field!r}] "
            ": null"
        )
    }


def _surface_value_getter(field):
    return {
        "function": (
            "params.context && params.context.surfaceRows && params.data && "
            "params.context.surfaceRows[params.data.leg_id] "
            f"? params.context.surfaceRows[params.data.leg_id][{field!r}] "
            ": null"
        )
    }


def _surface_or_calculated_value_getter(surface_field, calculated_field=None):
    calculated_lookup = (
        "params.context && params.context.pricingRows && params.data && "
        "params.context.pricingRows[params.data.leg_id] && "
        f"params.context.pricingRows[params.data.leg_id][{calculated_field!r}] "
        "!= null ? "
        f"params.context.pricingRows[params.data.leg_id][{calculated_field!r}] : "
        if calculated_field
        else ""
    )
    return {
        "function": (
            calculated_lookup
            + "params.context && params.context.surfaceRows && params.data && "
            "params.context.surfaceRows[params.data.leg_id] "
            f"? params.context.surfaceRows[params.data.leg_id][{surface_field!r}] "
            ": null"
        )
    }


def _surface_tooltip_getter(field):
    return {
        "function": (
            "params.context && params.context.surfaceRows && params.data && "
            "params.context.surfaceRows[params.data.leg_id] "
            f"? params.context.surfaceRows[params.data.leg_id][{field!r}] "
            ": 'Published surface reference is unavailable.'"
        )
    }


def _published_pricer_volatility_column(
    surface_field,
    header,
    *,
    calculated_field=None,
    width=82,
    tooltip=None,
    sign_coloring=False,
):
    column = _result_numeric_column(
        surface_field,
        header,
        min_width=width,
        sign_coloring=sign_coloring,
    )
    column.pop("field", None)
    column["colId"] = surface_field
    column["width"] = width
    column["editable"] = False
    column["valueGetter"] = _surface_or_calculated_value_getter(
        surface_field,
        calculated_field,
    )
    column["valueFormatter"] = {
        "function": (
            "params.value == null || !isFinite(Number(params.value)) "
            "? '—' : d3.format('.2%')(Number(params.value))"
        )
    }
    column["tooltipValueGetter"] = _surface_tooltip_getter(
        "surface_input_tooltip"
    )
    if tooltip:
        column["headerTooltip"] = tooltip
    return column


def _published_pricer_volatility_columns():
    input_vol_column = _published_pricer_volatility_column(
        "surface_input_vol",
        "Input vol",
        calculated_field="raw_volatility",
        width=82,
        tooltip=(
            "Effective input volatility: published strike-specific volatility plus "
            "the ATM, Skew, and Smile adjustments."
        ),
    )
    input_vol_column["valueGetter"] = _surface_or_calculated_value_getter(
        "surface_effective_input_vol",
        "raw_volatility",
    )
    return {
        "headerName": "Volatility",
        "headerClass": (
            "pricer-result-column-group "
            "pricer-result-column-group-volatility"
        ),
        "children": [
            input_vol_column,
            _published_pricer_volatility_column(
                "surface_atm_input_vol",
                "ATM",
                width=72,
                tooltip="Published 50-delta call ATM contribution to Input vol.",
            ),
            _published_pricer_volatility_column(
                "surface_skew_input_vol",
                "Skew",
                width=72,
                tooltip="Strike-specific skew contribution: Input vol minus ATM.",
                sign_coloring=True,
            ),
        ],
    }


def _published_pricer_pricing_volatility_columns():
    column = _published_pricer_volatility_column(
        "surface_pricing_vol",
        "Pricing vol",
        calculated_field="volatility_used",
        width=82,
        tooltip=(
            "Effective Input vol after the governed contract-date adjustment."
        ),
    )
    column["valueGetter"] = _surface_or_calculated_value_getter(
        "surface_effective_pricing_vol",
        "volatility_used",
    )
    return {
        "headerName": "",
        "headerClass": (
            "pricer-result-column-group "
            "pricer-result-column-group-volatility"
        ),
        "children": [column],
    }


def _volatility_adjustment_column(field, header):
    return {
        "headerName": header,
        "field": field,
        "width": 72,
        "minWidth": 68,
        "type": "numericColumn",
        "editable": {"function": "!params.node.rowPinned"},
        "cellClass": (
            "pricer-editable-cell pricer-table-number-cell "
            "pricer-volatility-adjustment-cell"
        ),
        "valueParser": {"function": "Number(params.newValue)"},
        "valueFormatter": {
            "function": (
                "params.value == null || !isFinite(Number(params.value)) "
                "? '—' : d3.format(',.2f')(Number(params.value))"
            )
        },
        "cellClassRules": {
            "pricer-invalid-cell": (
                "!params.node.rowPinned && (params.value == null || "
                "!isFinite(Number(params.value)) || "
                f"Math.abs(Number(params.value)) > {MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT!r})"
            )
        },
        "headerTooltip": (
            f"{header} adjustment in volatility percentage points; "
            "1.00 adds one vol point and -1.00 removes one vol point."
        ),
    }


def _volatility_adjustment_columns():
    return {
        "headerName": "Volatility adjustment",
        "headerClass": (
            "pricer-result-column-group "
            "pricer-result-column-group-adjustment"
        ),
        "children": [
            _volatility_adjustment_column("atm_vol_adjustment", "ATM"),
            _volatility_adjustment_column("skew_vol_adjustment", "Skew"),
            _volatility_adjustment_column("smile_vol_adjustment", "Smile"),
        ],
    }


def _published_surface_columns():
    columns = []
    for field, tooltip_field, header in (
        (
            "surface_input_vol",
            "surface_input_tooltip",
            "Input vol",
        ),
        (
            "surface_pricing_vol",
            "surface_pricing_tooltip",
            "Pricing vol",
        ),
    ):
        columns.append(
            {
                "headerName": header,
                "colId": field,
                "width": 112,
                "minWidth": 104,
                "type": "numericColumn",
                "editable": False,
                "valueGetter": _surface_value_getter(field),
                "valueFormatter": {
                    "function": (
                        "params.value == null || !isFinite(Number(params.value)) "
                        "? '—' : d3.format('.2%')(Number(params.value))"
                    )
                },
                "tooltipValueGetter": _surface_tooltip_getter(tooltip_field),
                "cellClass": "pricer-table-number-cell",
                "cellClassRules": {
                    "pricer-missing-cell": "params.value == null",
                },
            }
        )
    return {
        "headerName": "Published surface",
        "headerClass": (
            "pricer-result-column-group "
            "pricer-result-column-group-published"
        ),
        "children": columns,
    }


def _unified_result_numeric_column(
    field,
    header,
    *,
    min_width=72,
    decimal_places=None,
    percentage=False,
    sign_coloring=True,
    tooltip=None,
):
    column = _result_numeric_column(
        field,
        header,
        min_width=min_width,
        decimal_places=decimal_places,
        sign_coloring=sign_coloring,
    )
    column["width"] = min_width
    column["valueGetter"] = _calculated_value_getter(field)
    if percentage:
        column["valueFormatter"] = {
            "function": (
                "params.value == null || !isFinite(Number(params.value)) "
                "? '—' : d3.format('.2%')(Number(params.value))"
            )
        }
    if tooltip:
        column["headerTooltip"] = tooltip
    return column


def _compact_greek_label(field):
    return {
        "delta_s1": "Delta 1",
        "delta_s2": "Delta 2",
        "gamma_s1": "Gamma 1",
        "gamma_s2": "Gamma 2",
        "gamma_s1s2": "Cross gamma",
        "vega_sigma1": "Vega 1",
        "vega_sigma2": "Vega 2",
        "corr_sensitivity": "Corr sens.",
        "vega_equiv": "Equiv. vega",
    }.get(field, field.title())


def _model_greek_tooltip(model, field):
    if field == "theta":
        return (
            "Instantaneous annual derivative divided by 365."
            if model == "black76"
            else "One-calendar-day repricing change."
        )
    return GREEK_LABELS[field]


def _unified_result_columns(model, *, use_published_surface=False):
    columns = []
    greek_widths = {
        "gamma_s1s2": 96,
        "corr_sensitivity": 88,
        "vega_equiv": 92,
    }
    if model in SINGLE_ASSET_MODELS and use_published_surface:
        columns.extend(
            [
                _published_pricer_volatility_columns(),
                _volatility_adjustment_columns(),
                _published_pricer_pricing_volatility_columns(),
            ]
        )
    elif model in SINGLE_ASSET_MODELS:
        columns.append(
            {
                "headerName": "Volatility",
                "headerClass": "pricer-result-column-group",
                "children": [
                    _unified_result_numeric_column(
                        "raw_volatility",
                        "Contract vol",
                        min_width=88,
                        percentage=True,
                        sign_coloring=False,
                        tooltip=(
                            "Contract volatility resolved from the published surface."
                            if use_published_surface
                            else (
                                "Volatility entered through Quote input, or implied "
                                "from Quote input when Quote basis is PREMIUM."
                            )
                        ),
                    ),
                    _unified_result_numeric_column(
                        "volatility_used",
                        "Pricing vol",
                        min_width=82,
                        percentage=True,
                        sign_coloring=False,
                        tooltip="Volatility used after the expiry adjustment.",
                    ),
                ],
            }
        )
    greek_fields = GREEK_FIELDS[model]
    if use_published_surface and model in SINGLE_ASSET_MODELS:
        columns.extend(
            [
                {
                    "headerName": "Premium",
                    "headerClass": (
                        "pricer-result-column-group "
                        "pricer-result-column-group-premium"
                    ),
                    "children": [
                        _unified_result_numeric_column(
                            "unit_value",
                            "Premium",
                            decimal_places=4,
                        ),
                        _unified_result_numeric_column(
                            "trade_value",
                            "Value",
                            min_width=88,
                            decimal_places=0,
                        ),
                    ],
                },
                {
                    "headerName": "Unit Greeks",
                    "headerClass": (
                        "pricer-result-column-group "
                        "pricer-result-column-group-unit"
                    ),
                    "children": [
                        _unified_result_numeric_column(
                            f"unit_{field}",
                            _compact_greek_label(field),
                            min_width=greek_widths.get(field, 72),
                            decimal_places=4,
                            tooltip=_model_greek_tooltip(model, field),
                        )
                        for field in greek_fields
                    ],
                },
                {
                    "headerName": "Position Greeks",
                    "headerClass": (
                        "pricer-result-column-group "
                        "pricer-result-column-group-position"
                    ),
                    "children": [
                        _unified_result_numeric_column(
                            f"trade_{field}",
                            _compact_greek_label(field),
                            min_width=max(greek_widths.get(field, 72), 82),
                            decimal_places=0,
                            tooltip=_model_greek_tooltip(model, field),
                        )
                        for field in greek_fields
                    ],
                },
            ]
        )
    else:
        columns.extend(
            [
            {
                "headerName": "Unit analytics",
                "headerClass": (
                    "pricer-result-column-group "
                    "pricer-result-column-group-unit"
                    if use_published_surface
                    else "pricer-result-column-group"
                ),
                "children": [
                    _unified_result_numeric_column(
                        "unit_value",
                        "Premium",
                        decimal_places=4,
                    ),
                    *[
                        _unified_result_numeric_column(
                            f"unit_{field}",
                            _compact_greek_label(field),
                            min_width=greek_widths.get(field, 72),
                            decimal_places=4,
                            tooltip=_model_greek_tooltip(model, field),
                        )
                        for field in greek_fields
                    ],
                ],
            },
            {
                "headerName": "Position contribution",
                "headerClass": (
                    "pricer-result-column-group "
                    "pricer-result-column-group-position"
                ),
                "children": [
                    _unified_result_numeric_column(
                        "trade_value",
                        "Value",
                        min_width=88,
                        decimal_places=0,
                    ),
                    *[
                        _unified_result_numeric_column(
                            f"trade_{field}",
                            _compact_greek_label(field),
                            min_width=max(greek_widths.get(field, 72), 82),
                            decimal_places=0,
                            tooltip=_model_greek_tooltip(model, field),
                        )
                        for field in greek_fields
                    ],
                ],
            },
            ]
        )
    return columns


_CURRENT_PRICER_COLUMN_WIDTHS = {
    "name": (86, 86),
    "ratio": (50, 64),
    "call_put": (70, 82),
    "strike": (60, 60),
    "surface_input_vol": (70, 82),
    "surface_atm_input_vol": (58, 72),
    "surface_skew_input_vol": (58, 72),
    "atm_vol_adjustment": (58, 72),
    "skew_vol_adjustment": (58, 72),
    "smile_vol_adjustment": (58, 72),
    "surface_pricing_vol": (72, 82),
    "unit_value": (68, 80),
    "trade_value": (74, 96),
    "volatility_asset_1": (104, 118),
    "volatility_asset_2": (104, 118),
}

_CURRENT_PRICER_LONG_GREEK_WIDTHS = {
    "gamma_s1s2": 96,
    "corr_sensitivity": 88,
    "vega_equiv": 92,
}


def _append_column_class(column, property_name, class_name):
    existing = column.get(property_name)
    if not existing:
        column[property_name] = class_name
    elif isinstance(existing, str) and class_name not in existing.split():
        column[property_name] = f"{existing} {class_name}"


def _current_pricer_column_width(column_id):
    if column_id in _CURRENT_PRICER_COLUMN_WIDTHS:
        return _CURRENT_PRICER_COLUMN_WIDTHS[column_id]
    for prefix, standard_width in (("unit_", 60), ("trade_", 68)):
        if column_id.startswith(prefix):
            field = column_id.removeprefix(prefix)
            minimum = _CURRENT_PRICER_LONG_GREEK_WIDTHS.get(
                field,
                standard_width,
            )
            return minimum, minimum + 16
    return None


def _apply_current_pricer_leg_geometry(column_defs):
    """Apply `/pricer` density without changing the legacy Pricer grid."""
    for column in column_defs:
        children = column.get("children")
        if isinstance(children, list):
            _apply_current_pricer_leg_geometry(children)
            if children:
                _append_column_class(
                    children[0],
                    "headerClass",
                    "pricer-column-group-start",
                )
                _append_column_class(
                    children[0],
                    "cellClass",
                    "pricer-column-group-start",
                )
            continue

        column_id = str(column.get("field") or column.get("colId") or "")
        width_range = _current_pricer_column_width(column_id)
        if width_range is None:
            continue

        minimum, maximum = width_range
        column["width"] = minimum
        column["minWidth"] = minimum
        column["maxWidth"] = maximum
        if column_id == "name":
            column.pop("flex", None)
        else:
            column["flex"] = 1

        if column_id == "name":
            column["cellRenderer"] = "PricerLegSelector"
            _append_column_class(
                column,
                "headerClass",
                "pricer-table-text-header",
            )
        elif column_id == "call_put":
            _append_column_class(
                column,
                "headerClass",
                "pricer-table-category-header",
            )
            _append_column_class(
                column,
                "cellClass",
                "pricer-table-category-cell",
            )
            _append_column_class(
                column,
                "cellClass",
                "pricer-select-editable-cell",
            )
        else:
            _append_column_class(
                column,
                "headerClass",
                "pricer-table-number-header",
            )

        if "tooltipValueGetter" not in column:
            column["tooltipValueGetter"] = {
                "function": (
                    "params.valueFormatted != null && params.valueFormatted !== '' "
                    "? params.valueFormatted : (params.value == null ? '' : "
                    "String(params.value))"
                )
            }
    return column_defs


def _rows_for_lot_mode(rows, *, signed_lots):
    converted = []
    if not isinstance(rows, list):
        return converted
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        row = copy.deepcopy(raw_row)
        side = str(row.get("side") or "").strip().upper()
        ratio_value = row.get("ratio")
        try:
            ratio_number = (
                None
                if isinstance(ratio_value, bool)
                else float(ratio_value)
            )
        except (TypeError, ValueError, OverflowError):
            ratio_number = None
        if ratio_number is not None and not math.isfinite(ratio_number):
            ratio_number = None

        if signed_lots:
            if ratio_number is not None and side in {"BUY", "SELL"}:
                row["ratio"] = abs(ratio_number) * (
                    -1.0 if side == "SELL" else 1.0
                )
            row.pop("side", None)
        elif not side and ratio_number is not None:
            row["side"] = "SELL" if ratio_number < 0 else "BUY"
            row["ratio"] = abs(ratio_number)
        converted.append(row)
    return converted


def _default_leg_for_lot_mode(
    model,
    sequence,
    *,
    signed_lots,
    use_published_surface=False,
):
    rows = _rows_for_lot_mode(
        [default_leg(model, sequence)],
        signed_lots=signed_lots,
    )
    if use_published_surface:
        rows = _rows_with_volatility_adjustments(model, rows)
    return rows[0]


def _leg_column_defs(
    model,
    *,
    signed_lots=False,
    use_published_surface=False,
):
    text_column = {
        "editable": {"function": "!params.node.rowPinned"},
        "cellClass": "pricer-editable-cell pricer-table-text-cell",
    }
    numeric_column = {
        "editable": {"function": "!params.node.rowPinned"},
        "type": "numericColumn",
        "cellClass": "pricer-editable-cell pricer-table-number-cell",
        "valueParser": {"function": "Number(params.newValue)"},
    }
    positive_rules = {
        "pricer-invalid-cell": (
            "!params.node.rowPinned && (params.value == null || "
            "!isFinite(Number(params.value)) || Number(params.value) <= 0)"
        )
    }
    nonzero_rules = {
        "pricer-invalid-cell": (
            "!params.node.rowPinned && (params.value == null || "
            "!isFinite(Number(params.value)) || Number(params.value) === 0)"
        )
    }
    quote_rules = {
        "pricer-invalid-cell": (
            "!params.node.rowPinned && (params.value == null || "
            "!isFinite(Number(params.value)) || "
            "(params.data.quote_basis === 'PREMIUM' "
            "? Number(params.value) <= 0 "
            ": Number(params.value) < 0.005 || Number(params.value) > 200))"
        )
    }
    volatility_rules = {
        "pricer-invalid-cell": (
            "!params.node.rowPinned && (params.value == null || "
            "!isFinite(Number(params.value)) || Number(params.value) < 0.005 "
            "|| Number(params.value) > 200)"
        )
    }
    columns = [
        {
            "headerName": "Leg",
            "field": "name",
            "pinned": "left",
            "width": 104,
            "minWidth": 88,
            **text_column,
        },
    ]
    if not signed_lots:
        columns.append(
            {
                "headerName": "Side",
                "field": "side",
                "width": 72,
                "editable": {"function": "!params.node.rowPinned"},
                "cellEditor": "agSelectCellEditor",
                "cellEditorParams": {"values": ["BUY", "SELL"]},
                "cellClass": "pricer-editable-cell pricer-table-text-cell",
            }
        )
    columns.extend(
        [
            {
                "headerName": "Lots",
                "field": "ratio",
                "width": 64,
                **numeric_column,
                "cellClassRules": (
                    nonzero_rules if signed_lots else positive_rules
                ),
                **(
                    {"headerTooltip": "Positive = buy; negative = sell."}
                    if signed_lots
                    else {}
                ),
            },
            {
                "headerName": "Call / Put",
                "field": "call_put",
                "width": 82,
                "editable": {"function": "!params.node.rowPinned"},
                "cellEditor": "agSelectCellEditor",
                "cellEditorParams": {"values": ["C", "P"]},
                "cellClass": "pricer-editable-cell pricer-table-text-cell",
            },
            {
                "headerName": "Strike",
                "field": "strike",
                "width": 84,
                "minWidth": 72,
                **numeric_column,
                "cellClassRules": positive_rules if model != "kirk" else {},
            },
        ]
    )
    if model in SINGLE_ASSET_MODELS:
        if not use_published_surface:
            columns.extend(
                [
                    {
                        "headerName": "Quote basis",
                        "field": "quote_basis",
                        "width": 94,
                        "editable": {"function": "!params.node.rowPinned"},
                        "cellEditor": "agSelectCellEditor",
                        "cellEditorParams": {"values": ["VOL", "PREMIUM"]},
                        "cellClass": "pricer-editable-cell pricer-table-text-cell",
                        "headerTooltip": "Choose one input basis for this leg.",
                    },
                    {
                        "headerName": "Quote input",
                        "field": "quote_value",
                        "width": 94,
                        "minWidth": 82,
                        **numeric_column,
                        "cellClassRules": quote_rules,
                        "headerTooltip": (
                            "Vol accepts 0.432 or 43.20 for 43.20%; values up to "
                            "2 are decimals and values above 2 are percentages. "
                            "Premium is a positive unsigned unit price."
                        ),
                    },
                ]
            )
    else:
        columns.extend(
            [
                {
                    "headerName": "Asset 1 input vol",
                    "field": "volatility_asset_1",
                    "width": 118,
                    "minWidth": 104,
                    **numeric_column,
                    "cellClassRules": volatility_rules,
                },
                {
                    "headerName": "Asset 2 input vol",
                    "field": "volatility_asset_2",
                    "width": 118,
                    "minWidth": 104,
                    **numeric_column,
                    "cellClassRules": volatility_rules,
                },
            ]
        )
    output = [
        columns[0],
        {
            "headerName": "Leg inputs",
            "headerClass": (
                "pricer-result-column-group pricer-result-column-group-inputs"
            ),
            "children": columns[1:],
        },
    ]
    result_columns = _unified_result_columns(
        model,
        use_published_surface=use_published_surface,
    )
    output.extend(result_columns[:1])
    if model in SINGLE_ASSET_MODELS and not use_published_surface:
        output.append(_published_surface_columns())
    output.extend(result_columns[1:])
    if use_published_surface:
        return _apply_current_pricer_leg_geometry(output)
    return output


def _quote_ready_rows(model, rows, *, signed_lots=None):
    migrated = []
    if not isinstance(rows, list):
        return migrated
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        if model in SINGLE_ASSET_MODELS:
            if "quote_basis" not in row:
                row["quote_basis"] = "VOL"
                row["quote_value"] = row.get("volatility")
            else:
                quote_basis = str(row.get("quote_basis") or "").strip().upper()
                row["quote_basis"] = (
                    "VOL" if quote_basis == "VOLATILITY" else quote_basis
                )
                if "quote_value" not in row:
                    row["quote_value"] = (
                        row.get("volatility")
                        if row["quote_basis"] == "VOL"
                        else None
                    )
            row.pop("volatility", None)
        migrated.append(row)
    if signed_lots is None:
        return migrated
    return _rows_for_lot_mode(migrated, signed_lots=signed_lots)


def _rows_with_volatility_adjustments(model, rows, *, signed_lots=None):
    normalized = _quote_ready_rows(model, rows, signed_lots=signed_lots)
    if model not in SINGLE_ASSET_MODELS:
        return normalized
    for row in normalized:
        for field in VOLATILITY_ADJUSTMENT_FIELDS:
            row.setdefault(field, 0.0)
    return normalized


def _leg_grid_options(snapshot=None, surface_reference=None, *, compact=False):
    pricing_rows = {}
    pinned_rows = []
    if isinstance(snapshot, dict) and snapshot.get("schema_version") == SCHEMA_VERSION:
        result_rows, total = _combined_result_rows(snapshot)
        pricing_rows = {str(row["leg_id"]): row for row in [*result_rows, total]}
        pinned_rows = [total]
    surface_rows = {}
    if (
        isinstance(surface_reference, dict)
        and surface_reference.get("schema_version") == REFERENCE_SCHEMA_VERSION
        and isinstance(surface_reference.get("rows"), dict)
    ):
        surface_rows = copy.deepcopy(surface_reference["rows"])
    return {
        "domLayout": "autoHeight",
        "rowHeight": 28 if compact else 30,
        "headerHeight": 30 if compact else 34,
        "groupHeaderHeight": 24 if compact else 27,
        "stopEditingWhenCellsLoseFocus": True,
        "enableCellTextSelection": True,
        "ensureDomOrder": True,
        "animateRows": False,
        "maintainColumnOrder": False,
        "context": {
            "pricingRows": pricing_rows,
            "surfaceRows": surface_rows,
        },
        "pinnedBottomRowData": pinned_rows,
        "enableBrowserTooltips": False,
        "tooltipShowDelay": 0,
        "tooltipHideDelay": 3000,
        **({"tooltipShowMode": "whenTruncated"} if compact else {}),
        "rowSelection": {
            "mode": "singleRow",
            "checkboxes": not compact,
            "headerCheckbox": False,
            "enableClickSelection": True,
        },
        **(
            {
                "selectionColumnDef": {
                    "width": 34,
                    "minWidth": 34,
                    "maxWidth": 34,
                    "resizable": False,
                    "suppressHeaderMenuButton": True,
                }
            }
            if not compact
            else {}
        ),
    }


def _build_legs_grid(
    structure_id=DEFAULT_STRUCTURE_ID,
    *,
    model="black76",
    rows=None,
    calculation_snapshot=None,
    signed_lots=False,
    use_published_surface=False,
):
    row_builder = (
        _rows_with_volatility_adjustments
        if use_published_surface
        else _quote_ready_rows
    )
    rows = row_builder(
        model,
        rows or [default_leg(model, 1)],
        signed_lots=signed_lots,
    )
    return dag.AgGrid(
        id=_instance_id("pricer-legs-grid", structure_id),
        rowData=rows,
        columnDefs=_leg_column_defs(
            model,
            signed_lots=signed_lots,
            use_published_surface=use_published_surface,
        ),
        defaultColDef={
            "sortable": False,
            "filter": False,
            "resizable": True,
            "suppressHeaderMenuButton": True,
            "suppressHeaderFilterButton": True,
            "singleClickEdit": True,
        },
        dashGridOptions=_leg_grid_options(
            calculation_snapshot,
            compact=use_published_surface,
        ),
        getRowId="params.data.leg_id",
        persistence=_instance_persistence(structure_id, "structure-legs"),
        persisted_props=["rowData"],
        persistence_type="session",
        selectedRows=[],
        className=(
            "ag-theme-alpine mckinsey-ag-grid pricer-data-grid "
            "pricer-legs-grid pricer-unified-grid"
        ),
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
    price_unit_label = snapshot["context"].get("price_unit_label", "unit")
    trade_currency = snapshot["context"].get("trade_currency", "currency")
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
    if model in SINGLE_ASSET_MODELS:
        columns.extend(
            [
                {
                    "headerName": "Quote",
                    "field": "quote_basis",
                    "minWidth": 76,
                    "cellClass": "pricer-table-text-cell",
                    "headerClass": "pricer-table-text-header",
                },
                _result_numeric_column(
                    "entered_premium",
                    "Input premium",
                    min_width=100,
                    sign_coloring=False,
                ),
                _result_numeric_column(
                    "raw_volatility",
                    "Contract vol",
                    min_width=92,
                    sign_coloring=False,
                ),
                _result_numeric_column(
                    "volatility_used",
                    "Pricing vol",
                    min_width=88,
                    sign_coloring=False,
                ),
            ]
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
                _result_numeric_column(
                    "volatility_asset_1_used",
                    "Asset 1 pricing vol",
                    min_width=104,
                    sign_coloring=False,
                ),
                _result_numeric_column(
                    "volatility_asset_2_used",
                    "Asset 2 pricing vol",
                    min_width=104,
                    sign_coloring=False,
                ),
            ]
        )
    columns.extend(
        [
            {
                "headerName": f"Position · {trade_currency}",
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
                "headerName": f"Unit · {price_unit_label}",
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
    vega_tooltip = (
        "Adjusted pricing vol, 1 point"
        if snapshot["context"].get("vega_basis") == "adjusted_pricing_vol"
        else "Contract vol, 1 point"
    )
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
            "_vega_tooltip": vega_tooltip,
            "_rho_tooltip": "1 rate point",
        }
        if snapshot["model"] in SINGLE_ASSET_MODELS:
            row["quote_basis"] = leg["quote_basis"].title()
            row["entered_premium"] = leg["entered_premium"]
            row["raw_volatility"] = leg["raw_volatility"]
            is_premium_input = str(leg["quote_basis"]).lower() == "premium"
            row["input_volatility"] = (
                None if is_premium_input else leg["raw_volatility"]
            )
            row["implied_volatility"] = (
                leg["raw_volatility"] if is_premium_input else None
            )
            row["volatility_used"] = leg["volatility_used"]
        else:
            row["raw_volatility_asset_1"] = leg["raw_volatility_asset_1"]
            row["raw_volatility_asset_2"] = leg["raw_volatility_asset_2"]
            row["volatility_asset_1_used"] = leg["volatility_asset_1_used"]
            row["volatility_asset_2_used"] = leg["volatility_asset_2_used"]
        rows.append(row)
    total = {
        "leg_id": "__total__",
        "name": "Total",
        "side": "",
        "ratio": None,
        "call_put": "",
        "strike": None,
        "quote_basis": "",
        "entered_premium": None,
        "raw_volatility": None,
        "input_volatility": None,
        "implied_volatility": None,
        "volatility_used": None,
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
        "_vega_tooltip": vega_tooltip,
        "_rho_tooltip": "1 rate point",
    }
    return rows, total


def _build_combined_result_grid(snapshot, structure_id=DEFAULT_STRUCTURE_ID):
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
        id=_instance_id("pricer-combined-results-grid", structure_id),
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


def _build_strip_component_grid(snapshot, structure_id=DEFAULT_STRUCTURE_ID):
    context = snapshot["context"]
    is_jkm = context.get("asset") == "JKM"
    is_nbp = context.get("asset") == "NBP"
    has_exchange_mapping = bool(context.get("exchange_mapping_id"))
    show_product = (
        bool(context.get("exchange_product_code"))
        if has_exchange_mapping
        else is_jkm
    )
    is_asian = snapshot.get("model") == "asian76"
    rows = []
    for leg in snapshot["legs"]:
        for component in leg.get("components") or []:
            rows.append(
                {
                    "row_id": f"{leg['leg_id']}:{component['contract_month']}",
                    "leg": leg["name"],
                    "contract_month": component["contract_month_label"],
                    "delivery_quantity": component.get(
                        "contract_size", component.get("delivery_hours")
                    ),
                    "product_code": component.get("exchange_product_code", "TFO"),
                    "product_detail": " · ".join(
                        str(value)
                        for value in (
                            component.get("exchange_product_name"),
                            (
                                f"ID {component['exchange_product_id']}"
                                if component.get("exchange_product_id")
                                else None
                            ),
                            (
                                f"{component['exercise_style'].title()} exercise"
                                if component.get("exercise_style")
                                else None
                            ),
                        )
                        if value
                    ),
                    "strip_weight_pct": component["weight"] * 100.0,
                    "forward": component["forward"],
                    "averaging_start_date": component.get("averaging_start_date"),
                    "option_expiration_date": component["option_expiration_date"],
                    "expiry_status": (
                        component["expiry_status"]
                        if str(component["expiry_status"]).startswith("TFO ")
                        else component["expiry_status"].title()
                    ),
                    "input_vol_pct": component["input_volatility"] * 100.0,
                    "unit_value": component["unit_value"],
                    "weighted_unit_value": component["weighted_unit_value"],
                    "delta": component["greeks"]["delta"],
                    "vega": component["greeks"]["vega"],
                }
            )
    columns = [
        {
            "headerName": "Leg",
            "field": "leg",
            "pinned": "left",
            "minWidth": 90,
            "cellClass": "pricer-table-text-cell",
        },
        {
            "headerName": "Month",
            "field": "contract_month",
            "minWidth": 76,
            "cellClass": "pricer-table-text-cell",
        },
    ]
    if show_product:
        columns.append(
            {
                "headerName": "Product",
                "field": "product_code",
                "minWidth": 68,
                "cellClass": "pricer-table-text-cell",
                **(
                    {"tooltipField": "product_detail"}
                    if has_exchange_mapping
                    else {}
                ),
            }
        )
    columns.extend(
        [
            _result_numeric_column(
                "delivery_quantity",
                (
                    "MMBtu"
                    if is_jkm
                    else "Therms"
                    if has_exchange_mapping and is_nbp
                    else "Hours"
                ),
                min_width=(
                    72 if is_jkm or (has_exchange_mapping and is_nbp) else 62
                ),
                sign_coloring=False,
                decimal_places=0,
            ),
            _result_numeric_column(
                "strip_weight_pct",
                "Weight %",
                min_width=74,
                sign_coloring=False,
                decimal_places=3,
            ),
            _result_numeric_column(
                "forward",
                "Forward",
                min_width=72,
                sign_coloring=False,
                decimal_places=4,
            ),
        ]
    )
    if is_jkm and is_asian:
        columns.append(
            {
                "headerName": "Averaging start",
                "field": "averaging_start_date",
                "minWidth": 112,
                "cellClass": "pricer-table-text-cell",
            }
        )
    columns.extend(
        [
            {
                "headerName": (
                    (
                        f"{context['exchange_product_code']} expiry"
                        if context.get("exchange_product_code")
                        else "Option expiry"
                    )
                    if has_exchange_mapping
                    else (
                        "APO expiry"
                        if is_jkm and is_asian
                        else "JKZ / TFO expiry"
                        if is_jkm
                        else "TFO expiry"
                    )
                ),
                "field": "option_expiration_date",
                "minWidth": 112 if is_jkm else 104,
                "cellClass": "pricer-table-text-cell",
            },
            {
                "headerName": "Status",
                "field": "expiry_status",
                "minWidth": 72,
                "cellClass": "pricer-table-text-cell",
            },
            _result_numeric_column(
                "input_vol_pct",
                "Input vol %",
                min_width=82,
                sign_coloring=False,
                decimal_places=3,
            ),
            _result_numeric_column(
                "unit_value",
                "Premium",
                min_width=76,
                sign_coloring=False,
                decimal_places=4,
            ),
            _result_numeric_column(
                "weighted_unit_value",
                "Weighted premium",
                min_width=108,
                sign_coloring=False,
                decimal_places=4,
            ),
            _result_numeric_column(
                "delta",
                "Delta",
                min_width=68,
                sign_coloring=False,
                decimal_places=4,
            ),
            _result_numeric_column(
                "vega",
                "Vega",
                min_width=68,
                sign_coloring=False,
                decimal_places=4,
            ),
        ]
    )
    return dag.AgGrid(
        id=_instance_id("pricer-strip-components-grid", structure_id),
        rowData=rows,
        columnDefs=columns,
        defaultColDef={
            "sortable": False,
            "filter": False,
            "resizable": True,
            "suppressHeaderMenuButton": True,
            "suppressHeaderFilterButton": True,
            "wrapHeaderText": True,
            "autoHeaderHeight": True,
        },
        dashGridOptions={
            "domLayout": "autoHeight",
            "rowHeight": 31,
            "headerHeight": 44,
            "enableCellTextSelection": True,
            "ensureDomOrder": True,
            "animateRows": False,
            "suppressColumnVirtualisation": True,
        },
        columnSize="autoSize",
        columnSizeOptions={"skipHeader": False},
        getRowId="params.data.row_id",
        className=(
            "ag-theme-alpine mckinsey-ag-grid pricer-data-grid "
            "pricer-results-grid pricer-strip-components-grid"
        ),
        style={"width": "100%"},
        dangerously_allow_code=True,
    )


def _build_pricing_model_field(
    structure_id=DEFAULT_STRUCTURE_ID,
    value="black76",
    *,
    workflow="legacy",
):
    exchange_workflow = workflow == "exchange"
    model_options = (
        [{"label": MODEL_LABELS[value], "value": value}]
        if exchange_workflow
        else option_types
    )
    return _build_pricer_field(
        html.Span(
            [
                html.Span("Model", className="pricer-field-label-otc"),
                html.Span(
                    "Product",
                    className="pricer-field-label-exchange",
                ),
            ]
        ),
        dcc.Dropdown(
            id=_instance_id("pricer-option-type", structure_id),
            options=model_options,
            value=value,
            clearable=False,
            disabled=exchange_workflow,
            persistence=_instance_persistence(structure_id, "model"),
            persistence_type="session",
            className="pricer-filter-dropdown pricer-option-type-dropdown",
        ),
        class_name="pricer-model-field",
        field_id=_instance_id("pricer-model-field", structure_id),
        hint=(
            "Kirk is undiscounted, so rate and Rho are not applicable. It "
            "requires two input vols; PREMIUM quoting is unavailable because "
            "one premium cannot determine both vols."
            if value == "kirk"
            else None
        ),
    )


def _build_asset_field(
    structure_id=DEFAULT_STRUCTURE_ID,
    value=DEFAULT_ASSET,
    *,
    style=None,
):
    return _build_pricer_field(
        "Asset",
        dcc.Dropdown(
            id=_instance_id("pricer-asset", structure_id),
            options=asset_options,
            value=value,
            clearable=False,
            persistence=_instance_persistence(structure_id, "asset"),
            persistence_type="session",
            className="pricer-filter-dropdown pricer-asset-dropdown",
        ),
        class_name="pricer-asset-field",
        field_id=_instance_id("pricer-asset-field", structure_id),
        style=style,
        hint="Selects the governed variance calendar for this asset.",
    )


def _build_mapping_id_field(
    structure_id=DEFAULT_STRUCTURE_ID,
    value=None,
    *,
    style=None,
):
    return _build_pricer_field(
        "Mapping ID",
        dcc.Dropdown(
            id=_instance_id("pricer-mapping-id", structure_id),
            options=exchange_mapping_options(),
            value=value,
            clearable=False,
            persistence=_instance_persistence(structure_id, "mapping-id-v2"),
            persistence_type="session",
            className="pricer-filter-dropdown pricer-mapping-id-dropdown",
        ),
        class_name="pricer-mapping-id-field",
        field_id=_instance_id("pricer-mapping-id-field", structure_id),
        style=style,
        hint="Canonical exchange-option identifier from the Product Registry.",
    )


def _build_price_unit_field(
    structure_id=DEFAULT_STRUCTURE_ID,
    asset=DEFAULT_ASSET,
    *,
    mapping_id=None,
    style=None,
):
    try:
        spec = asset_price_spec(asset, mapping_id)
        value = spec["price_unit_label"]
        description = spec["description"]
    except StructureValidationError:
        value = "—"
        description = "Price currency and unit are unavailable."
    return html.Div(
        [
            html.Span("Price unit", className="pricer-field-label"),
            html.Div(
                value,
                id=_instance_id("pricer-price-unit", structure_id),
                className="pricer-price-unit-value",
                title=description,
                **{"aria-live": "polite"},
            ),
        ],
        id=_instance_id("pricer-price-unit-field", structure_id),
        className="pricer-field pricer-price-unit-field",
        title="Selected asset price currency and unit.",
        style=style,
    )


def _build_kirk_asset_identity_fields(
    structure_id,
    values,
):
    defaults = default_context("kirk", date.today())
    if isinstance(values, dict):
        defaults.update(values)
    fields = []
    for asset_number in (1, 2):
        code_key = f"asset_{asset_number}_code"
        selected_asset = defaults.get(code_key)
        try:
            if selected_asset not in SUPPORTED_ASSETS:
                raise StructureValidationError("Asset selection is required.")
            spec = asset_price_spec(selected_asset)
            unit_value = spec["price_unit_label"]
            unit_description = spec["description"]
        except StructureValidationError:
            unit_value = "—"
            unit_description = "Select an asset to show its price unit."
        fields.extend(
            [
                _build_pricer_field(
                    f"Asset {asset_number}",
                    dcc.Dropdown(
                        id=_context_id(
                            "kirk",
                            code_key,
                            structure_id=structure_id,
                        ),
                        options=asset_options,
                        value=selected_asset,
                        clearable=True,
                        placeholder="Select",
                        persistence=_instance_persistence(
                            structure_id,
                            f"kirk-{code_key}-v1",
                        ),
                        persistence_type="session",
                        className="pricer-filter-dropdown pricer-asset-dropdown",
                    ),
                    class_name="pricer-asset-field pricer-kirk-asset-field",
                    hint=(
                        f"Select Asset {asset_number} explicitly; its governed "
                        "variance calendar is used only for that asset's volatility."
                    ),
                ),
                html.Div(
                    [
                        html.Span("Price unit", className="pricer-field-label"),
                        html.Div(
                            unit_value,
                            id={
                                "type": "pricer-kirk-price-unit",
                                "structure_id": structure_id,
                                "asset_number": asset_number,
                            },
                            className="pricer-price-unit-value",
                            title=unit_description,
                            **{"aria-live": "polite"},
                        ),
                    ],
                    className=(
                        "pricer-field pricer-price-unit-field "
                        "pricer-kirk-price-unit-field"
                    ),
                    title=f"Asset {asset_number} price currency and unit.",
                ),
            ]
        )
    return fields


def _build_premium_convention_field(
    model="black76",
    structure_id=DEFAULT_STRUCTURE_ID,
    value=None,
    asset=DEFAULT_ASSET,
):
    if value in (None, "", "product_default"):
        value = default_premium_convention(asset, model)
    options = premium_convention_options
    if model == "kirk":
        options = [option for option in options if option["value"] != "upfront"]
        if value == "upfront":
            value = "futures_style"
    return _build_pricer_field(
        "Premium",
        dcc.Dropdown(
            id=_context_id(model, "premium_convention", structure_id=structure_id),
            options=options,
            value=value,
            clearable=False,
            persistence=_instance_persistence(
                structure_id, f"{model}-premium-convention-v2"
            ),
            persistence_type="session",
            className="pricer-filter-dropdown pricer-premium-convention-dropdown",
        ),
        class_name="pricer-premium-convention-field",
        hint=(
            "Asset selection sets the exchange default. Futures-style is "
            "undiscounted; Upfront uses the risk-free rate."
        ),
    )


def _build_structure_header_context(
    model,
    structure_id=DEFAULT_STRUCTURE_ID,
    values=None,
    asset=DEFAULT_ASSET,
    mapping_id=None,
):
    if model == "kirk":
        values = _migrated_kirk_context_values(values)
    defaults = default_context(model, date.today())
    defaults["premium_convention"] = default_premium_convention(asset, model)
    if isinstance(values, dict):
        defaults.update(values)
    mapping = exchange_option_mapping(mapping_id)
    if mapping is not None:
        defaults["premium_convention"] = mapping.premium_convention
    elif defaults.get("premium_convention") in (None, "", "product_default"):
        defaults["premium_convention"] = default_premium_convention(asset, model)
    fields = []
    if model == "kirk":
        fields.extend(_build_kirk_asset_identity_fields(structure_id, defaults))
    fields.append(
        _build_premium_convention_field(
            model,
            structure_id,
            defaults["premium_convention"],
            asset,
        )
    )
    if model == "kirk":
        return fields
    fields.append(
        _build_delivery_shape_field(
            model,
            structure_id,
            defaults["delivery_shape"],
            asset,
            mapping_id,
        )
    )
    fields.append(
        _build_delivery_month_field(
            model,
            structure_id,
            defaults.get("delivery_month"),
            asset=asset,
            delivery_shape=defaults["delivery_shape"],
            mapping_id=mapping_id,
        )
    )
    return fields


def _resolved_contract_size_default(
    asset,
    model,
    context_values=None,
    valuation_date_value=None,
):
    if model == "kirk":
        return 1.0
    valuation_date = parse_date(valuation_date_value, date.today())
    resolved_context = default_context(model, valuation_date)
    if isinstance(context_values, dict):
        resolved_context.update(context_values)
    resolved_context["asset"] = asset
    return default_contract_size(
        asset,
        resolved_context,
        as_of=valuation_date,
    )


def _resolved_mapping_contract_size_default(
    mapping,
    context_values=None,
    valuation_date_value=None,
):
    if mapping is None:
        raise StructureValidationError("Exchange mapping is required.")
    if mapping.sizing_mode == "fixed":
        return mapping.contract_size
    resolved_context = (
        copy.deepcopy(context_values) if isinstance(context_values, dict) else {}
    )
    resolved_context["exchange_mapping_id"] = mapping.mapping_id
    return _resolved_contract_size_default(
        mapping.asset,
        mapping.model,
        resolved_context,
        valuation_date_value,
    )


def _contract_size_hint():
    return (
        "For Kirk this is the editable notional and defaults to one unit. "
        "For single-asset models it is the editable quantity in the denominator "
        "unit shown under Price unit. "
        "TTF defaults to one ICE lot (1 MW across the exact delivery hours); "
        "JKM to 10,000 MMBtu/month; Brent to 1,000 bbl; Henry Hub to 2,500 "
        "MMBtu; and NBP to 1,000 therm/day across the delivery month. Enter "
        "another positive number to override it."
    )


def _build_structure_panel(
    structure,
    *,
    can_remove=True,
    calculation_snapshot=None,
    calculate_all_baseline=0,
    signed_lots=False,
    use_published_surface=False,
    valuation_date_override=None,
    workflow="legacy",
    heading_level=2,
):
    structure_id = structure["structure_id"]
    template = structure.get("template") or {}
    requested_model = template.get("model")
    if requested_model not in MODEL_LABELS:
        requested_model = "black76"
    mapping_id = template.get("mapping_id") if workflow == "exchange" else None
    mapping = exchange_option_mapping(mapping_id)
    if mapping is not None:
        mapping_id = mapping.mapping_id
    if workflow == "exchange" and mapping is None:
        mapping = exchange_mapping_for_asset_model(
            template.get("asset"),
            requested_model,
        )
        if mapping is None:
            mapping = exchange_option_mapping(DEFAULT_EXCHANGE_MAPPING_ID)
        mapping_id = mapping.mapping_id
    model = mapping.model if mapping is not None else requested_model
    template_asset = mapping.asset if mapping is not None else template.get("asset")
    asset = template_asset if template_asset in SUPPORTED_ASSETS else DEFAULT_ASSET
    valuation_date = (
        parse_date(valuation_date_override, date.today()).isoformat()
        if valuation_date_override is not None
        else template.get("valuation_date", date.today().isoformat())
    )
    context_values = template.get("context")
    if model == "kirk":
        context_values = _migrated_kirk_context_values(context_values)
    exchange_contract_size = _resolved_contract_size_default(
        asset,
        model,
        context_values,
        valuation_date,
    )
    if mapping is not None:
        exchange_contract_size = _resolved_mapping_contract_size_default(
            mapping,
            context_values,
            valuation_date,
        )
    contract_multiplier = _coerce_pricer_float(
        template.get("contract_multiplier"),
        exchange_contract_size,
    )
    if contract_multiplier <= 0:
        contract_multiplier = exchange_contract_size
    row_builder = (
        _rows_with_volatility_adjustments
        if use_published_surface
        else _quote_ready_rows
    )
    rows = row_builder(
        model,
        template.get("legs"),
        signed_lots=signed_lots,
    )
    if not rows:
        rows = [
            _default_leg_for_lot_mode(
                model,
                1,
                signed_lots=signed_lots,
                use_published_surface=use_published_surface,
            )
        ]
    next_leg_sequence = template.get("next_leg_sequence")
    try:
        next_leg_sequence = max(int(next_leg_sequence), len(rows) + 1)
    except (TypeError, ValueError, OverflowError):
        next_leg_sequence = len(rows) + 1
    draft = {
        "schema_version": 1,
        "model": model,
        "context": (
            copy.deepcopy(context_values)
            if isinstance(context_values, dict)
            else None
        ),
        "legs": copy.deepcopy(rows),
        "next_leg_sequence": next_leg_sequence,
    }
    if mapping_id is not None:
        draft["mapping_id"] = mapping_id
    supplied_calculation_snapshot = calculation_snapshot
    if not _is_valid_calculation_snapshot(calculation_snapshot) or not (
        _snapshot_matches_template(
            calculation_snapshot,
            {
                "asset": asset,
                "mapping_id": mapping_id,
                "model": model,
                "contract_multiplier": contract_multiplier,
                "valuation_date": valuation_date,
                "context": context_values,
                "legs": rows,
            },
        )
    ):
        calculation_snapshot = None
    restored_status = ""
    if (
        isinstance(calculation_snapshot, dict)
        and calculation_snapshot.get("schema_version") == SCHEMA_VERSION
    ):
        restored_leg_count = len(calculation_snapshot.get("legs") or [])
        restored_leg_label = "leg" if restored_leg_count == 1 else "legs"
        restored_status = _build_pricer_message(
            f"Calculated · {restored_leg_count} {restored_leg_label} · "
            f"{calculation_snapshot.get('model_label', model)}",
            tone="success",
        )
    elif supplied_calculation_snapshot is not None:
        restored_status = _build_pricer_message(
            "Modified · outputs cleared · calculate again",
            tone="warning",
        )
    market_strip = html.Div(
        [
            _build_pricer_field(
                "Valuation",
                _build_pricer_date_picker(
                    _instance_id("pricer-valuation-date", structure_id),
                    valuation_date,
                    allow_past=True,
                    persistence_key=(
                        False
                        if valuation_date_override is not None
                        else _instance_persistence(
                            structure_id, "valuation-date-v1"
                        )
                    ),
                ),
                class_name="pricer-date-field pricer-valuation-date-field",
            ),
            _build_pricer_field(
                html.Span(
                    "Notional" if model == "kirk" else "Contract size",
                    id=_instance_id(
                        "pricer-contract-multiplier-label",
                        structure_id,
                    ),
                ),
                _build_pricer_number_input(
                    _instance_id("pricer-contract-multiplier", structure_id),
                    contract_multiplier,
                    minimum=0.01,
                    step=0.01,
                    persistence_key=_instance_persistence(
                        structure_id,
                        "contract-size-v1",
                    ),
                ),
                class_name="pricer-number-field pricer-contract-size-field",
                hint=_contract_size_hint(),
            ),
            html.Div(
                _build_context_form(
                    model,
                    structure_id,
                    context_values,
                    include_delivery_shape=False,
                    asset=asset,
                    show_jkm_vanilla_surface_note=workflow == "exchange",
                    mapping_id=mapping_id,
                ).children,
                id=_instance_id("pricer-shared-context", structure_id),
                className="pricer-shared-context",
            ),
        ],
        className="pricer-context-with-valuation pricer-market-strip",
    )
    hide_single_asset = workflow == "exchange" or (
        workflow == "otc" and model == "kirk"
    )
    single_asset_style = {"display": "none"} if hide_single_asset else None
    mapping_control = (
        _build_mapping_id_field(structure_id, mapping_id)
        if workflow == "exchange"
        else None
    )
    asset_control = _build_asset_field(
        structure_id,
        asset,
        style=single_asset_style,
    )
    price_unit_control = _build_price_unit_field(
        structure_id,
        asset,
        mapping_id=mapping_id,
        style=single_asset_style,
    )
    model_control = _build_pricing_model_field(
        structure_id,
        model,
        workflow=workflow,
    )
    header_context_control = html.Div(
        _build_structure_header_context(
            model,
            structure_id,
            context_values,
            asset,
            mapping_id,
        ),
        id=_instance_id(
            "pricer-header-context",
            structure_id,
        ),
        className="pricer-header-context",
    )
    ordered_header_controls = (
        [
            model_control,
            asset_control,
            price_unit_control,
            header_context_control,
            market_strip,
        ]
        if workflow == "otc"
        else (
            [
                mapping_control,
                asset_control,
                price_unit_control,
                model_control,
                header_context_control,
                market_strip,
            ]
            if workflow == "exchange"
            else [
                asset_control,
                price_unit_control,
                model_control,
                header_context_control,
                market_strip,
            ]
        )
    )
    return html.Section(
        [
            *(
                []
                if workflow == "exchange"
                else [
                    dcc.Input(
                        id=_instance_id("pricer-mapping-id", structure_id),
                        value=None,
                        type="hidden",
                        style={"display": "none"},
                    )
                ]
            ),
            dcc.Store(
                id=_instance_id("pricer-structure-workflow", structure_id),
                data=workflow,
                storage_type="memory",
            ),
            dcc.Store(
                id=_instance_id("pricer-contract-size-default", structure_id),
                data={
                    "asset": asset,
                    "value": exchange_contract_size,
                    **(
                        {"mapping_id": mapping_id}
                        if mapping_id is not None
                        else {}
                    ),
                },
                storage_type="memory",
            ),
            dcc.Store(
                id=_instance_id("pricer-draft-store", structure_id),
                data=draft,
                storage_type="session",
            ),
            dcc.Store(
                id=_instance_id("pricer-calculation-store", structure_id),
                data=copy.deepcopy(calculation_snapshot),
                storage_type="memory",
            ),
            dcc.Store(
                id=_instance_id(
                    "pricer-grid-pricing-options",
                    structure_id,
                ),
                data=_leg_grid_options(
                    calculation_snapshot,
                    compact=use_published_surface,
                ),
                storage_type="memory",
            ),
            dcc.Store(
                id=_instance_id(
                    "pricer-published-surface-reference",
                    structure_id,
                ),
                storage_type="memory",
            ),
            dcc.Store(
                id=_instance_id("pricer-calculate-all-baseline", structure_id),
                data=_nonnegative_click_count(calculate_all_baseline),
                storage_type="memory",
            ),
            dcc.Store(
                id=_instance_id("pricer-calculate-all-ack", structure_id),
                data=_nonnegative_click_count(calculate_all_baseline),
                storage_type="memory",
            ),
            _build_pricer_section_header(
                structure["label"],
                actions=[
                    html.Div(
                        ordered_header_controls,
                        className="pricer-structure-header-controls",
                    ),
                    html.Div(
                        [
                            html.Div(
                                restored_status,
                                id=_instance_id(
                                    "pricer-calculation-status",
                                    structure_id,
                                ),
                                className=(
                                    "pricer-calculation-status "
                                    "pricer-structure-status"
                                ),
                                role="status",
                            ),
                            html.Button(
                                "Calc",
                                id=_instance_id(
                                    "pricer-calculate-button",
                                    structure_id,
                                ),
                                className=(
                                    "custom-export-btn pricer-calculate-button"
                                ),
                                **{
                                    "aria-label": (
                                        f"Calculate {structure['label']}"
                                    ),
                                    "title": "Calculate structure",
                                },
                            ),
                            html.Button(
                                "Copy",
                                id=_instance_id(
                                    "pricer-duplicate-structure",
                                    structure_id,
                                ),
                                className=(
                                    "custom-export-btn pricer-secondary-button"
                                ),
                                **{
                                    "aria-label": (
                                        f"Duplicate {structure['label']}"
                                    ),
                                    "title": "Duplicate structure",
                                },
                            ),
                            html.Button(
                                "×",
                                id=_instance_id(
                                    "pricer-remove-structure",
                                    structure_id,
                                ),
                                className="pricer-remove-button",
                                disabled=not can_remove,
                                **{
                                    "aria-label": (
                                        f"Remove {structure['label']}"
                                    ),
                                    "title": "Remove structure",
                                },
                            ),
                        ],
                        className="pricer-structure-header-actions",
                    ),
                ],
                heading_level=heading_level,
            ),
            html.Div(
                [
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
                                                id=_instance_id(
                                                    "pricer-add-leg", structure_id
                                                ),
                                                className=(
                                                    "custom-export-btn "
                                                    "pricer-secondary-button"
                                                ),
                                                **{
                                                    "aria-label": (
                                                        "Add leg to "
                                                        f"{structure['label']}"
                                                    )
                                                },
                                            ),
                                            html.Button(
                                                "Duplicate",
                                                id=_instance_id(
                                                    "pricer-duplicate-leg", structure_id
                                                ),
                                                className=(
                                                    "custom-export-btn "
                                                    "pricer-secondary-button"
                                                ),
                                                title="Duplicate the selected leg",
                                                disabled=True,
                                                **{
                                                    "aria-label": (
                                                        "Duplicate selected leg in "
                                                        f"{structure['label']}"
                                                    )
                                                },
                                            ),
                                            html.Button(
                                                "Remove",
                                                id=_instance_id(
                                                    "pricer-remove-leg", structure_id
                                                ),
                                                className="pricer-remove-button",
                                                title="Remove the selected leg",
                                                disabled=True,
                                                **{
                                                    "aria-label": (
                                                        "Remove selected leg from "
                                                        f"{structure['label']}"
                                                    )
                                                },
                                            ),
                                        ],
                                        className="pricer-leg-edit-actions",
                                    ),
                                ],
                                className="pricer-leg-heading",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        id=_instance_id(
                                            "pricer-results-container",
                                            structure_id,
                                        ),
                                        className="pricer-calculation-meta",
                                    ),
                                    html.Div(
                                        id=_instance_id(
                                            "pricer-warning-container",
                                            structure_id,
                                        ),
                                        className="pricer-warning-container",
                                    ),
                                    html.Div(
                                        id=_instance_id(
                                            "pricer-leg-action-status",
                                            structure_id,
                                        ),
                                        className="pricer-action-status",
                                        role="status",
                                    ),
                                ],
                                className="pricer-leg-toolbar-status",
                            ),
                        ],
                        className="pricer-leg-toolbar",
                    ),
                    _build_legs_grid(
                        structure_id,
                        model=model,
                        rows=rows,
                        calculation_snapshot=calculation_snapshot,
                        signed_lots=signed_lots,
                        use_published_surface=use_published_surface,
                    ),
                    html.Div(
                        id=_instance_id(
                            "pricer-unit-results-container",
                            structure_id,
                        ),
                        className="pricer-unit-results-container",
                    ),
                    html.Div(
                        [
                            html.Div(
                                id=_instance_id(
                                    "pricer-model-inputs-used-container",
                                    structure_id,
                                )
                            ),
                            html.Div(
                                id=_instance_id(
                                    "pricer-time-info",
                                    structure_id,
                                )
                            ),
                            html.Div(
                                id=_instance_id(
                                    "pricer-greeks-container",
                                    structure_id,
                                )
                            ),
                        ],
                        className="pricer-compatibility-output",
                    ),
                ],
                className=(
                    "pricer-section-body pricer-config-body pricer-structure-body"
                ),
            ),
        ],
        className=(
            "pricer-section pricer-config-section pricer-structure-panel "
            f"pricer-workflow-{workflow}"
        ),
        **{"data-structure-id": structure_id},
    )


_INITIAL_WORKSPACE = _default_workspace()

layout = html.Main(
    [
        dcc.Store(id="pricer-exchange-workspace-store", data=None),
        html.Div(id="pricer-exchange-structures-container", hidden=True),
        html.Button(
            id="pricer-exchange-calculate-all",
            style={"display": "none"},
            disabled=True,
            tabIndex=-1,
            **{"aria-hidden": "true"},
        ),
        dcc.Store(
            id="pricer-workspace-store",
            data=_INITIAL_WORKSPACE,
            storage_type="session",
        ),
        dcc.Interval(
            id="pricer-workspace-hydration",
            interval=100,
            max_intervals=1,
            n_intervals=0,
        ),
        dcc.Store(id="pricer-workspace-ready-store", data=False),
        dcc.Store(
            id="pricer-calculations-session-store",
            storage_type="session",
        ),
        dcc.Store(id="pricer-draft-autosave-trigger", data=0),
        dcc.Store(
            id="pricer-analysis-selection-store",
            storage_type="session",
        ),
        dcc.Store(id="pricer-calculation-store", storage_type="memory"),
        html.Header(
            [
                html.H1("Pricer Old", className="pricer-workspace-title"),
                html.Div(
                    [
                        html.Div(
                            id="pricer-workspace-status",
                            className="pricer-workspace-status",
                            role="status",
                        ),
                        html.Button(
                            "Calculate all",
                            id="pricer-calculate-all",
                            className="custom-export-btn pricer-calculate-button",
                        ),
                        html.Button(
                            "Add structure",
                            id="pricer-add-structure",
                            className="custom-export-btn pricer-secondary-button",
                        ),
                    ],
                    className="pricer-workspace-actions",
                ),
            ],
            className="pricer-workspace-toolbar",
        ),
        html.Div(
            [
                _build_structure_panel(
                    _INITIAL_WORKSPACE["structures"][0], can_remove=False
                )
            ],
            id="pricer-structures-container",
            className="pricer-structure-list",
        ),
        html.Section(
            [
                _build_pricer_section_header(
                    "Contract vols vs volatility surface"
                ),
                html.Div(
                    _build_pricer_message(
                        "Calculate a structure to compare its contract vols with the surface."
                    ),
                    id="pricer-surface-comparison-grid",
                    className="pricer-surface-comparison-grid",
                ),
            ],
            className="pricer-section pricer-surface-comparison-section",
        ),
        html.Section(
            [
                _build_pricer_section_header(
                    "Detailed analysis",
                    actions=[
                        _build_pricer_field(
                            "Structure",
                            dcc.Dropdown(
                                id="pricer-analysis-structure-select",
                                options=[
                                    {
                                        "label": "S1",
                                        "value": DEFAULT_STRUCTURE_ID,
                                    }
                                ],
                                value=DEFAULT_STRUCTURE_ID,
                                clearable=False,
                                className=(
                                    "pricer-filter-dropdown "
                                    "pricer-analysis-selector"
                                ),
                            ),
                            class_name="pricer-analysis-selector-field",
                        )
                    ],
                ),
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
                            "Calculate the selected structure to see its payoff.",
                            class_name="pricer-wide-chart",
                        ),
                        _build_pricer_chart_card(
                            "volatility-chart",
                            "Parallel volatility shift",
                            "Calculate the selected structure to see volatility sensitivity.",
                        ),
                        _build_pricer_chart_card(
                            "rate-chart",
                            "Risk-free rate sensitivity",
                            "Calculate the selected structure to see rate sensitivity.",
                        ),
                        _build_pricer_chart_card(
                            "correlation-chart",
                            "Correlation sensitivity",
                            "Available for Kirk structures.",
                        ),
                        _build_pricer_chart_card(
                            "extension-chart",
                            "Expiration extension",
                            "Calculate the selected structure to see expiration sensitivity.",
                        ),
                        _build_pricer_chart_card(
                            "time-chart",
                            "Time decay",
                            "Calculate the selected structure to see time decay.",
                            class_name="pricer-wide-chart",
                        ),
                    ],
                    className=(
                        "pricer-section-body pricer-chart-grid "
                        "pricer-detailed-analysis-body"
                    ),
                ),
            ],
            className="pricer-section pricer-detailed-analysis-section",
        ),
    ],
    className="options-dashboard-container pricer-page",
)


def _state_for_structure(values, ids, structure_id, default=None):
    for value, component_id in zip(values or [], ids or []):
        if (
            isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ):
            return value
    return default


def _capture_structure_template(
    structure_id,
    asset_values,
    asset_ids,
    model_values,
    model_ids,
    multiplier_values,
    multiplier_ids,
    valuation_values,
    valuation_ids,
    row_values,
    row_ids,
    draft_values,
    draft_ids,
    param_values,
    param_ids,
    date_values,
    date_ids,
    mapping_values=None,
    mapping_ids=None,
):
    model = _state_for_structure(
        model_values, model_ids, structure_id, "black76"
    )
    context = _context_from_states(
        model,
        [
            value
            for value, component_id in zip(param_values or [], param_ids or [])
            if isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ],
        [
            component_id
            for component_id in param_ids or []
            if isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ],
        [
            value
            for value, component_id in zip(date_values or [], date_ids or [])
            if isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ],
        [
            component_id
            for component_id in date_ids or []
            if isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ],
    )
    draft = _state_for_structure(draft_values, draft_ids, structure_id, {}) or {}
    rows = _state_for_structure(row_values, row_ids, structure_id, []) or []
    template = {
        "asset": _state_for_structure(
            asset_values, asset_ids, structure_id, DEFAULT_ASSET
        ),
        "model": model,
        "contract_multiplier": _state_for_structure(
            multiplier_values, multiplier_ids, structure_id, 1
        ),
        "valuation_date": _state_for_structure(
            valuation_values,
            valuation_ids,
            structure_id,
            date.today().isoformat(),
        ),
        "context": context,
        "legs": copy.deepcopy(rows),
        "next_leg_sequence": draft.get("next_leg_sequence", len(rows) + 1),
    }
    mapping_id = _state_for_structure(
        mapping_values,
        mapping_ids,
        structure_id,
        None,
    )
    if mapping_id is not None:
        template["mapping_id"] = mapping_id
    return template


@callback(
    [
        Output("pricer-workspace-store", "data"),
        Output("pricer-structures-container", "children"),
        Output("pricer-workspace-ready-store", "data"),
    ],
    [
        Input("pricer-workspace-hydration", "n_intervals"),
        Input("pricer-add-structure", "n_clicks"),
        Input(
            {"type": "pricer-duplicate-structure", "structure_id": ALL},
            "n_clicks",
        ),
        Input(
            {"type": "pricer-remove-structure", "structure_id": ALL},
            "n_clicks",
        ),
        Input("pricer-draft-autosave-trigger", "data"),
    ],
    [
        State("pricer-workspace-store", "data"),
        State({"type": "pricer-asset", "structure_id": ALL}, "value"),
        State({"type": "pricer-asset", "structure_id": ALL}, "id"),
        State({"type": "pricer-option-type", "structure_id": ALL}, "value"),
        State({"type": "pricer-option-type", "structure_id": ALL}, "id"),
        State(
            {"type": "pricer-contract-multiplier", "structure_id": ALL},
            "value",
        ),
        State(
            {"type": "pricer-contract-multiplier", "structure_id": ALL}, "id"
        ),
        State(
            {"type": "pricer-valuation-date", "structure_id": ALL}, "date"
        ),
        State({"type": "pricer-valuation-date", "structure_id": ALL}, "id"),
        State({"type": "pricer-legs-grid", "structure_id": ALL}, "rowData"),
        State({"type": "pricer-legs-grid", "structure_id": ALL}, "id"),
        State({"type": "pricer-draft-store", "structure_id": ALL}, "data"),
        State({"type": "pricer-draft-store", "structure_id": ALL}, "id"),
        State(
            {
                "type": "pricer-context-param",
                "structure_id": ALL,
                "model": ALL,
                "param": ALL,
            },
            "value",
        ),
        State(
            {
                "type": "pricer-context-param",
                "structure_id": ALL,
                "model": ALL,
                "param": ALL,
            },
            "id",
        ),
        State(
            {
                "type": "pricer-context-date",
                "structure_id": ALL,
                "model": ALL,
                "param": ALL,
            },
            "date",
        ),
        State(
            {
                "type": "pricer-context-date",
                "structure_id": ALL,
                "model": ALL,
                "param": ALL,
            },
            "id",
        ),
        State("pricer-calculations-session-store", "data"),
        State("pricer-workspace-ready-store", "data"),
        State("pricer-calculate-all", "n_clicks"),
        State("url", "pathname"),
        State("pricer-global-valuation-date", "date"),
    ],
)
def manage_pricer_workspace(
    _hydration,
    _add_clicks,
    _duplicate_clicks,
    _remove_clicks,
    _autosave_tick,
    workspace,
    asset_values,
    asset_ids,
    model_values,
    model_ids,
    multiplier_values,
    multiplier_ids,
    valuation_values,
    valuation_ids,
    row_values,
    row_ids,
    draft_values,
    draft_ids,
    param_values,
    param_ids,
    date_values,
    date_ids,
    persisted_calculations,
    workspace_ready,
    calculate_all_clicks=None,
    pathname=None,
    global_valuation_date=None,
):
    workspace = _normalize_workspace(workspace)
    signed_lots = pathname == "/pricer"
    use_published_surface = pathname == "/pricer"
    workflow = "otc" if pathname == "/pricer" else "legacy"
    valuation_date_override = (
        parse_date(global_valuation_date, date.today()).isoformat()
        if pathname == "/pricer"
        else None
    )
    persisted_calculations = (
        persisted_calculations if isinstance(persisted_calculations, dict) else {}
    )
    triggered = _get_pricer_triggered_id()
    if not isinstance(triggered, dict):
        if triggered == "pricer-draft-autosave-trigger":
            if not workspace_ready:
                return no_update, no_update, no_update
            updated = copy.deepcopy(workspace)
            drafts = copy.deepcopy(workspace["drafts"])
            for structure in workspace["structures"]:
                structure_id = structure["structure_id"]
                if _state_for_structure(
                    model_values, model_ids, structure_id, None
                ) is None:
                    continue
                drafts[structure_id] = _capture_structure_template(
                    structure_id,
                    asset_values,
                    asset_ids,
                    model_values,
                    model_ids,
                    multiplier_values,
                    multiplier_ids,
                    valuation_values,
                    valuation_ids,
                    row_values,
                    row_ids,
                    draft_values,
                    draft_ids,
                    param_values,
                    param_ids,
                    date_values,
                    date_ids,
                )
            if drafts == workspace["drafts"]:
                return no_update, no_update, no_update
            updated["drafts"] = drafts
            return updated, no_update, no_update
        if triggered == "pricer-add-structure":
            updated = _reduce_workspace(workspace, "add")
            patch = Patch()
            patch.append(
                _build_structure_panel(
                    updated["structures"][-1],
                    calculate_all_baseline=calculate_all_clicks,
                    signed_lots=signed_lots,
                    use_published_surface=use_published_surface,
                    valuation_date_override=valuation_date_override,
                    workflow=workflow,
                    heading_level=3 if pathname == "/pricer" else 2,
                )
            )
            return updated, patch, no_update
        panels = [
            _build_structure_panel(
                {
                    **structure,
                    "template": workspace["drafts"].get(
                        structure["structure_id"], structure.get("template")
                    ),
                },
                can_remove=len(workspace["structures"]) > 1,
                calculation_snapshot=persisted_calculations.get(
                    structure["structure_id"]
                ),
                calculate_all_baseline=calculate_all_clicks,
                signed_lots=signed_lots,
                use_published_surface=use_published_surface,
                valuation_date_override=valuation_date_override,
                workflow=workflow,
                heading_level=3 if pathname == "/pricer" else 2,
            )
            for structure in workspace["structures"]
        ]
        return workspace, panels, True
    action_type = triggered.get("type")
    structure_id = triggered.get("structure_id")
    if str(structure_id or "").startswith("exchange-") and action_type in {
        "pricer-duplicate-structure",
        "pricer-remove-structure",
    }:
        return no_update, no_update, no_update
    try:
        triggered_clicks = ctx.triggered[0].get("value")
    except Exception:
        triggered_clicks = None
    if action_type in {
        "pricer-duplicate-structure",
        "pricer-remove-structure",
    } and not triggered_clicks:
        return no_update, no_update, no_update
    if action_type == "pricer-duplicate-structure":
        template = _capture_structure_template(
            structure_id,
            asset_values,
            asset_ids,
            model_values,
            model_ids,
            multiplier_values,
            multiplier_ids,
            valuation_values,
            valuation_ids,
            row_values,
            row_ids,
            draft_values,
            draft_ids,
            param_values,
            param_ids,
            date_values,
            date_ids,
        )
        updated = _reduce_workspace(workspace, "duplicate", structure_id, template)
        patch = Patch()
        patch.append(
            _build_structure_panel(
                updated["structures"][-1],
                calculate_all_baseline=calculate_all_clicks,
                signed_lots=signed_lots,
                use_published_surface=use_published_surface,
                valuation_date_override=valuation_date_override,
                workflow=workflow,
                heading_level=3 if pathname == "/pricer" else 2,
            )
        )
        return updated, patch, no_update
    if action_type == "pricer-remove-structure" and len(workspace["structures"]) > 1:
        remove_index = next(
            (
                index
                for index, structure in enumerate(workspace["structures"])
                if structure["structure_id"] == structure_id
            ),
            None,
        )
        if remove_index is None:
            return no_update, no_update, no_update
        updated = _reduce_workspace(workspace, "remove", structure_id)
        patch = Patch()
        del patch[remove_index]
        return updated, patch, no_update
    return no_update, no_update, no_update


@callback(
    Output(
        {"type": "pricer-valuation-date", "structure_id": ALL},
        "date",
    ),
    [
        Input("pricer-global-valuation-date", "date"),
        Input("url", "pathname"),
    ],
    State(
        {"type": "pricer-valuation-date", "structure_id": ALL},
        "id",
    ),
    prevent_initial_call=True,
)
def sync_pricer_global_valuation_date(
    global_valuation_date,
    pathname,
    valuation_ids,
):
    if pathname != "/pricer":
        return [no_update for _component_id in (valuation_ids or [])]
    resolved_date = parse_date(
        global_valuation_date,
        date.today(),
    ).isoformat()
    return [resolved_date for _component_id in (valuation_ids or [])]


@callback(
    Output("pricer-draft-autosave-trigger", "data"),
    [
        Input({"type": "pricer-mapping-id", "structure_id": ALL}, "value"),
        Input({"type": "pricer-asset", "structure_id": ALL}, "value"),
        Input({"type": "pricer-option-type", "structure_id": ALL}, "value"),
        Input(
            {"type": "pricer-contract-multiplier", "structure_id": ALL},
            "value",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": ALL}, "date"
        ),
        Input({"type": "pricer-legs-grid", "structure_id": ALL}, "rowData"),
        Input({"type": "pricer-draft-store", "structure_id": ALL}, "data"),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": ALL,
                "model": ALL,
                "param": ALL,
            },
            "value",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": ALL,
                "model": ALL,
                "param": ALL,
            },
            "date",
        ),
    ],
    State("pricer-draft-autosave-trigger", "data"),
    prevent_initial_call=True,
)
def signal_pricer_draft_autosave(*values_and_current_tick):
    current_tick = values_and_current_tick[-1] if values_and_current_tick else 0
    return int(current_tick or 0) + 1


@callback(
    Output(
        {
            "type": "pricer-context-param",
            "structure_id": MATCH,
            "model": ALL,
            "param": "premium_convention",
        },
        "value",
    ),
    [
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
    ],
    [
        State({"type": "pricer-option-type", "structure_id": MATCH}, "value"),
        State(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
    ],
    prevent_initial_call=True,
)
def _select_asset_default_premium_convention_dash_callback(
    asset,
    mapping_id,
    model,
    workflow,
):
    return select_asset_default_premium_convention(
        asset,
        model,
        mapping_id=mapping_id,
        workflow=workflow,
    )


def select_asset_default_premium_convention(
    asset,
    model,
    *,
    mapping_id=None,
    workflow="legacy",
):
    try:
        mapping = (
            exchange_option_mapping(mapping_id)
            if workflow == "exchange"
            else None
        )
        selected = (
            mapping.premium_convention
            if mapping is not None
            else default_premium_convention(asset, model)
        )
    except StructureValidationError:
        return no_update
    return [selected]


@callback(
    [
        Output(
            {"type": "pricer-surface-proxy-note", "structure_id": MATCH},
            "children",
        ),
        Output(
            {"type": "pricer-surface-proxy-note", "structure_id": MATCH},
            "className",
        ),
    ],
    Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
)
def sync_exchange_surface_proxy_note(mapping_id):
    """Keep proxy disclosure in sync when two mappings share asset/model."""
    return _surface_proxy_note(mapping_id)


@callback(
    [
        Output(
            {"type": "pricer-price-unit", "structure_id": MATCH},
            "children",
        ),
        Output(
            {"type": "pricer-price-unit", "structure_id": MATCH},
            "title",
        ),
    ],
    [
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
    ],
)
def display_asset_price_unit(asset, mapping_id=None):
    try:
        spec = asset_price_spec(asset, mapping_id)
        return spec["price_unit_label"], spec["description"]
    except StructureValidationError:
        return "—", "Price currency and unit are unavailable."


@callback(
    [
        Output(
            {
                "type": "pricer-kirk-price-unit",
                "structure_id": MATCH,
                "asset_number": ALL,
            },
            "children",
        ),
        Output(
            {
                "type": "pricer-kirk-price-unit",
                "structure_id": MATCH,
                "asset_number": ALL,
            },
            "title",
        ),
    ],
    Input(
        {
            "type": "pricer-context-param",
            "structure_id": MATCH,
            "model": "kirk",
            "param": ALL,
        },
        "value",
    ),
    [
        State(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": "kirk",
                "param": ALL,
            },
            "id",
        ),
        State(
            {
                "type": "pricer-kirk-price-unit",
                "structure_id": MATCH,
                "asset_number": ALL,
            },
            "id",
        ),
    ],
)
def display_kirk_asset_price_units(param_values, param_ids, unit_ids):
    context = _context_from_states("kirk", param_values, param_ids, [], [])
    labels = []
    descriptions = []
    for unit_id in unit_ids or []:
        asset_number = unit_id.get("asset_number") if isinstance(unit_id, dict) else None
        asset = context.get(f"asset_{asset_number}_code")
        try:
            if asset not in SUPPORTED_ASSETS:
                raise StructureValidationError("Asset selection is required.")
            spec = asset_price_spec(asset)
            labels.append(spec["price_unit_label"])
            descriptions.append(spec["description"])
        except StructureValidationError:
            labels.append("—")
            descriptions.append("Select an asset to show its price unit.")
    return labels, descriptions


@callback(
    [
        Output(
            {"type": "pricer-contract-multiplier", "structure_id": MATCH},
            "value",
        ),
        Output(
            {"type": "pricer-contract-size-default", "structure_id": MATCH},
            "data",
        ),
    ],
    [
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-option-type", "structure_id": MATCH}, "value"),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "value",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "date",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": MATCH},
            "date",
        ),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
    ],
    [
        State(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "id",
        ),
        State(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "id",
        ),
        State(
            {"type": "pricer-contract-multiplier", "structure_id": MATCH},
            "value",
        ),
        State(
            {"type": "pricer-contract-size-default", "structure_id": MATCH},
            "data",
        ),
        State(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
    ],
    prevent_initial_call=True,
)
def _sync_exchange_contract_size_dash_callback(
    asset,
    model,
    param_values,
    date_values,
    valuation_date_value,
    mapping_id,
    param_ids,
    date_ids,
    current_contract_size,
    previous_default_state,
    workflow,
):
    return sync_exchange_contract_size(
        asset,
        model,
        param_values,
        date_values,
        valuation_date_value,
        param_ids,
        date_ids,
        current_contract_size,
        previous_default_state,
        mapping_id=mapping_id,
        workflow=workflow,
    )


def sync_exchange_contract_size(
    asset,
    model,
    param_values,
    date_values,
    valuation_date_value,
    param_ids,
    date_ids,
    current_contract_size,
    previous_default_state,
    *,
    mapping_id=None,
    workflow="legacy",
):
    if model not in MODEL_LABELS:
        return no_update, no_update
    context = _context_from_states(
        model,
        param_values,
        param_ids,
        date_values,
        date_ids,
    )
    mapping = (
        exchange_option_mapping(mapping_id) if workflow == "exchange" else None
    )
    if mapping is not None:
        mapping_id = mapping.mapping_id
    try:
        if mapping is not None:
            exchange_default = _resolved_mapping_contract_size_default(
                mapping,
                context,
                valuation_date_value,
            )
        else:
            exchange_default = _resolved_contract_size_default(
                asset,
                model,
                context,
                valuation_date_value,
            )
    except StructureValidationError:
        return no_update, no_update

    previous_default_state = (
        previous_default_state if isinstance(previous_default_state, dict) else {}
    )
    previous_default = _coerce_pricer_float(
        previous_default_state.get("value")
    )
    current_size = _coerce_pricer_float(current_contract_size)
    triggered = _get_pricer_triggered_id()
    asset_triggered = (
        isinstance(triggered, dict)
        and triggered.get("type") in {"pricer-asset", "pricer-mapping-id"}
    )
    uses_previous_default = (
        current_size is not None
        and previous_default is not None
        and math.isclose(
            current_size,
            previous_default,
            rel_tol=1e-12,
            abs_tol=1e-9,
        )
    )
    should_apply_default = (
        asset_triggered
        or previous_default_state.get("asset") != asset
        or previous_default_state.get("mapping_id") != mapping_id
        or current_size is None
        or previous_default is None
        or uses_previous_default
    )
    value_update = (
        exchange_default
        if should_apply_default
        and not (
            current_size is not None
            and math.isclose(
                current_size,
                exchange_default,
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
        )
        else no_update
    )
    default_state = {"asset": asset, "value": exchange_default}
    if mapping_id is not None:
        default_state["mapping_id"] = mapping_id
    return value_update, default_state


@callback(
    Output(
        {"type": "pricer-contract-multiplier-label", "structure_id": MATCH},
        "children",
    ),
    Input({"type": "pricer-option-type", "structure_id": MATCH}, "value"),
)
def display_contract_multiplier_label(model):
    return "Notional" if model == "kirk" else "Contract size"


@callback(
    [
        Output(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": MATCH,
                "param": "rate",
            },
            "value",
        ),
        Output(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": MATCH,
                "param": "rate",
            },
            "disabled",
        ),
    ],
    [
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": MATCH,
                "param": "premium_convention",
            },
            "value",
        ),
        Input(
            {"type": "pricer-mapping-id", "structure_id": MATCH},
            "value",
        ),
    ],
    State(
        {"type": "pricer-structure-workflow", "structure_id": MATCH},
        "data",
    ),
)
def sync_risk_free_rate_control(
    premium_convention,
    mapping_id=None,
    workflow="legacy",
):
    """Keep the visible rate consistent with the selected premium convention."""
    mapping = (
        exchange_option_mapping(mapping_id)
        if workflow == "exchange"
        else None
    )
    if mapping is not None:
        premium_convention = mapping.premium_convention
    if premium_convention == "futures_style":
        return 0.0, True
    if premium_convention == "upfront":
        return no_update, False
    return no_update, no_update


@callback(
    Output(
        {"type": "pricer-rate-field", "structure_id": MATCH},
        "style",
    ),
    Input(
        {
            "type": "pricer-context-param",
            "structure_id": MATCH,
            "model": ALL,
            "param": "premium_convention",
        },
        "value",
    ),
    State(
        {"type": "pricer-structure-workflow", "structure_id": MATCH},
        "data",
    ),
)
def sync_exchange_rate_visibility(premium_convention, workflow):
    """Expose Rate only for exchange mappings whose premium is upfront."""
    if isinstance(premium_convention, list):
        premium_convention = (
            premium_convention[0] if premium_convention else None
        )
    if workflow != "exchange":
        return {}
    return {"display": "flex" if premium_convention == "upfront" else "none"}


@callback(
    Output(
        {"type": "pricer-asset", "structure_id": MATCH},
        "value",
    ),
    Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
    State(
        {"type": "pricer-structure-workflow", "structure_id": MATCH},
        "data",
    ),
    prevent_initial_call=True,
)
def select_exchange_mapping_asset(mapping_id, workflow=None):
    """Keep the hidden asset identity governed by the selected Mapping ID."""
    if workflow != "exchange":
        return no_update
    mapping = exchange_option_mapping(mapping_id)
    return mapping.asset if mapping is not None else no_update


@callback(
    Output(
        {"type": "pricer-option-type", "structure_id": MATCH},
        "value",
    ),
    [
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
    ],
    [
        State(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
    ],
    prevent_initial_call=True,
)
def select_asset_default_model(asset, mapping_id=None, workflow=None):
    """Keep the legacy JKM default while exchange mappings remain authoritative."""
    if workflow == "exchange":
        mapping = exchange_option_mapping(mapping_id)
        if mapping is not None:
            return mapping.model
    if asset != "JKM":
        return no_update
    try:
        return default_model_for_asset(asset)
    except StructureValidationError:
        return no_update


def update_parameters(option_type, structure_id=DEFAULT_STRUCTURE_ID):
    """Compatibility helper retained for direct tests and older callers."""
    return _build_context_form(
        option_type if option_type in MODEL_LABELS else "black76",
        structure_id,
    )


@callback(
    [
        Output(
            {"type": "pricer-shared-context", "structure_id": MATCH},
            "children",
        ),
        Output({"type": "pricer-legs-grid", "structure_id": MATCH}, "columnDefs"),
        Output({"type": "pricer-legs-grid", "structure_id": MATCH}, "rowData"),
        Output({"type": "pricer-legs-grid", "structure_id": MATCH}, "selectedRows"),
        Output({"type": "pricer-draft-store", "structure_id": MATCH}, "data"),
        Output(
            {"type": "pricer-leg-action-status", "structure_id": MATCH},
            "children",
        ),
        Output(
            {"type": "pricer-header-context", "structure_id": MATCH},
            "children",
        ),
        Output(
            {"type": "pricer-legs-grid", "structure_id": MATCH},
            "resetColumnState",
        ),
    ],
    [
        Input({"type": "pricer-option-type", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-add-leg", "structure_id": MATCH}, "n_clicks"),
        Input(
            {"type": "pricer-duplicate-leg", "structure_id": MATCH}, "n_clicks"
        ),
        Input({"type": "pricer-remove-leg", "structure_id": MATCH}, "n_clicks"),
        Input(
            {"type": "pricer-legs-grid", "structure_id": MATCH},
            "cellValueChanged",
        ),
    ],
    [
        State({"type": "pricer-legs-grid", "structure_id": MATCH}, "rowData"),
        State(
            {"type": "pricer-legs-grid", "structure_id": MATCH}, "selectedRows"
        ),
        State({"type": "pricer-draft-store", "structure_id": MATCH}, "data"),
        State({"type": "pricer-option-type", "structure_id": MATCH}, "id"),
        State({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        State("url", "pathname"),
        State(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
        State({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
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
    model_component_id=None,
    asset=DEFAULT_ASSET,
    pathname=None,
    workflow="legacy",
    mapping_id=None,
):
    if model not in MODEL_LABELS:
        return (no_update,) * 8
    triggered = _get_pricer_triggered_id()
    triggered_is_pattern = isinstance(triggered, dict)
    triggered_type = triggered.get("type") if isinstance(triggered, dict) else triggered
    triggered_type = {
        "option-type": "pricer-option-type",
        "pricer-legs-grid": "pricer-legs-grid",
    }.get(triggered_type, triggered_type)
    structure_id = (
        model_component_id.get("structure_id")
        if isinstance(model_component_id, dict)
        else DEFAULT_STRUCTURE_ID
    )
    signed_lots = pathname == "/pricer"
    use_published_surface = pathname == "/pricer"
    rows_were_invalid = rows is not None and not isinstance(rows, list)
    legacy_rows = _quote_ready_rows(model, rows)
    row_builder = (
        _rows_with_volatility_adjustments
        if use_published_surface
        else _quote_ready_rows
    )
    rows = row_builder(model, rows, signed_lots=signed_lots)
    lot_mode_changed = rows != legacy_rows
    draft = dict(draft) if isinstance(draft, dict) else {}
    if (
        (
            triggered_type is None
            or (triggered_is_pattern and triggered_type == "pricer-option-type")
        )
        and draft.get("model") == model
        and isinstance(draft.get("legs"), list)
        and draft.get("legs")
        and not rows_were_invalid
        and not lot_mode_changed
    ):
        return (no_update,) * 8
    if triggered_type in (None, "pricer-option-type"):
        if draft.get("model") == model and draft.get("legs"):
            saved_rows = row_builder(
                model,
                draft["legs"],
                signed_lots=signed_lots,
            )
            if saved_rows:
                rows = saved_rows
                try:
                    next_sequence = max(
                        int(draft.get("next_leg_sequence") or len(rows) + 1),
                        len(rows) + 1,
                    )
                except (TypeError, ValueError, OverflowError):
                    next_sequence = len(rows) + 1
            else:
                rows = [
                    _default_leg_for_lot_mode(
                        model,
                        1,
                        signed_lots=signed_lots,
                        use_published_surface=use_published_surface,
                    )
                ]
                next_sequence = 2
        else:
            rows = [
                _default_leg_for_lot_mode(
                    model,
                    1,
                    signed_lots=signed_lots,
                    use_published_surface=use_published_surface,
                )
            ]
            next_sequence = 2
        saved_context = (
            copy.deepcopy(draft.get("context"))
            if draft.get("model") == model
            and isinstance(draft.get("context"), dict)
            else None
        )
        if model == "kirk" and draft.get("model") == model:
            saved_context = _migrated_kirk_context_values(saved_context)
        mapping = (
            exchange_option_mapping(mapping_id)
            if workflow == "exchange"
            else None
        )
        if mapping is not None:
            saved_context = (
                copy.deepcopy(saved_context)
                if isinstance(saved_context, dict)
                else {}
            )
            saved_context["premium_convention"] = mapping.premium_convention
        new_draft = {
            "schema_version": 1,
            "model": model,
            "context": saved_context,
            "legs": rows,
            "next_leg_sequence": next_sequence,
        }
        if mapping_id is not None:
            new_draft["mapping_id"] = mapping_id
        return (
            _build_context_form(
                model,
                structure_id,
                new_draft.get("context"),
                include_delivery_shape=False,
                asset=asset,
                show_jkm_vanilla_surface_note=workflow == "exchange",
                mapping_id=mapping_id,
            ).children,
            _leg_column_defs(
                model,
                signed_lots=signed_lots,
                use_published_surface=use_published_surface,
            ),
            rows,
            [],
            new_draft,
            (
                "Invalid saved leg state was reset."
                if rows_were_invalid
                else ""
            ),
            _build_structure_header_context(
                model,
                structure_id,
                new_draft.get("context"),
                asset,
                mapping_id,
            ),
            True,
        )

    try:
        next_sequence = max(
            int(draft.get("next_leg_sequence") or len(rows) + 1),
            len(rows) + 1,
        )
    except (TypeError, ValueError, OverflowError):
        next_sequence = len(rows) + 1
    status = ""
    if triggered_type == "pricer-add-leg":
        if len(rows) >= MAX_LEGS:
            status = f"A structure can contain at most {MAX_LEGS} legs."
        else:
            rows.append(
                _default_leg_for_lot_mode(
                    model,
                    next_sequence,
                    signed_lots=signed_lots,
                    use_published_surface=use_published_surface,
                )
            )
            next_sequence += 1
            status = f"Added Leg {next_sequence - 1}."
    elif triggered_type == "pricer-duplicate-leg":
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
    elif triggered_type == "pricer-remove-leg":
        selected = (selected_rows or [None])[0]
        if selected is None:
            status = "Select one leg to remove."
        elif len(rows) <= 1:
            status = "A structure must retain at least one leg."
        else:
            selected_id = selected.get("leg_id")
            rows = [row for row in rows if row.get("leg_id") != selected_id]
            status = f"Removed {selected.get('name') or selected_id}."
    elif triggered_type == "pricer-legs-grid":
        status = ""

    basis_changed = False
    if triggered_type == "pricer-legs-grid":
        latest_event = (
            _cell_event[-1]
            if isinstance(_cell_event, list) and _cell_event
            else _cell_event
        )
    else:
        latest_event = None
    if isinstance(latest_event, dict):
        column = latest_event.get("column")
        column_id = latest_event.get("colId") or (
            column.get("colId") if isinstance(column, dict) else None
        )
        basis_changed = (
            column_id == "quote_basis"
            and latest_event.get("oldValue") != latest_event.get("newValue")
        )
        if basis_changed:
            changed_id = (latest_event.get("data") or {}).get("leg_id")
            for row in rows:
                if row.get("leg_id") == changed_id:
                    row["quote_value"] = None
                    break

    new_draft = {
        "schema_version": 1,
        "model": model,
        "context": (
            copy.deepcopy(draft.get("context"))
            if isinstance(draft.get("context"), dict)
            else None
        ),
        "legs": rows,
        "next_leg_sequence": next_sequence,
    }
    row_output = (
        no_update if triggered_type == "pricer-legs-grid" and not basis_changed else rows
    )
    return (
        no_update,
        no_update,
        row_output,
        [],
        new_draft,
        status,
        no_update,
        no_update,
    )


@callback(
    [
        Output(
            {"type": "pricer-duplicate-leg", "structure_id": MATCH},
            "disabled",
        ),
        Output(
            {"type": "pricer-remove-leg", "structure_id": MATCH},
            "disabled",
        ),
    ],
    [
        Input(
            {"type": "pricer-legs-grid", "structure_id": MATCH},
            "selectedRows",
        ),
        Input(
            {"type": "pricer-legs-grid", "structure_id": MATCH},
            "rowData",
        ),
    ],
)
def toggle_leg_action_buttons(selected_rows, rows):
    rows = rows if isinstance(rows, list) else []
    selected = (
        selected_rows[0]
        if isinstance(selected_rows, list) and selected_rows
        else None
    )
    selected_id = selected.get("leg_id") if isinstance(selected, dict) else None
    has_selection = bool(
        selected_id
        and any(
            isinstance(row, dict) and row.get("leg_id") == selected_id
            for row in rows
        )
    )
    return not has_selection, not (has_selection and len(rows) > 1)


@callback(
    Output(
        {"type": "pricer-delivery-year-field", "structure_id": MATCH},
        "style",
    ),
    Input(
        {
            "type": "pricer-context-param",
            "structure_id": MATCH,
            "model": ALL,
            "param": "delivery_shape",
        },
        "value",
    ),
)
def toggle_delivery_year_field(delivery_shape):
    if isinstance(delivery_shape, list):
        delivery_shape = delivery_shape[0] if delivery_shape else "MONTH"
    return _delivery_year_field_style(delivery_shape)


@callback(
    Output(
        {
            "type": "pricer-month-only-field",
            "structure_id": MATCH,
            "field": ALL,
        },
        "style",
    ),
    Input(
        {
            "type": "pricer-context-param",
            "structure_id": MATCH,
            "model": ALL,
            "param": "delivery_shape",
        },
        "value",
    ),
    State(
        {
            "type": "pricer-month-only-field",
            "structure_id": MATCH,
            "field": ALL,
        },
        "id",
    ),
)
def toggle_month_only_fields(delivery_shape, field_ids):
    if isinstance(delivery_shape, list):
        delivery_shape = delivery_shape[0] if delivery_shape else "MONTH"
    style = _month_only_field_style(delivery_shape)
    return [style.copy() for _ in (field_ids or [])]


@callback(
    [
        Output(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": "delivery_month",
            },
            "options",
        ),
        Output(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": "delivery_month",
            },
            "value",
        ),
        Output(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": "delivery_month",
            },
            "disabled",
        ),
        Output(
            {
                "type": "pricer-delivery-month-field",
                "structure_id": MATCH,
            },
            "style",
        ),
    ],
    [
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-option-type", "structure_id": MATCH}, "value"),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": "delivery_shape",
            },
            "value",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": MATCH},
            "date",
        ),
        Input(
            {"type": "pricer-mapping-id", "structure_id": MATCH},
            "value",
        ),
    ],
    State(
        {
            "type": "pricer-context-param",
            "structure_id": MATCH,
            "model": ALL,
            "param": "delivery_month",
        },
        "value",
    ),
)
def sync_delivery_month_control(
    asset,
    model,
    delivery_shape,
    valuation_date_value,
    mapping_id,
    current_delivery_month,
):
    if model not in MODEL_LABELS or asset not in SUPPORTED_ASSETS:
        return [no_update], [no_update], [no_update], no_update
    if isinstance(delivery_shape, list):
        delivery_shape = delivery_shape[0] if delivery_shape else "MONTH"
    if isinstance(current_delivery_month, list):
        current_delivery_month = (
            current_delivery_month[0] if current_delivery_month else None
        )
    options = _delivery_month_options(
        asset,
        model,
        parse_date(valuation_date_value, date.today()),
        mapping_id,
    )
    selected = _resolved_delivery_month(current_delivery_month, options)
    is_month = str(delivery_shape or "MONTH").strip().upper() == "MONTH"
    return (
        [options],
        [selected],
        [not is_month or not options],
        {} if is_month else {"display": "none"},
    )


def _sync_contract_date(expiration_value, contract_value):
    if not expiration_value:
        return no_update, no_update
    expiration = parse_date(expiration_value)
    contract = parse_date(contract_value, expiration)
    minimum = expiration.isoformat()
    if not contract_value or contract < expiration:
        return minimum, minimum
    return no_update, minimum


def _governed_delivery_component(
    asset,
    model,
    delivery_shape,
    delivery_month,
    valuation_date_value,
    mapping_id=None,
):
    if isinstance(delivery_shape, list):
        delivery_shape = delivery_shape[0] if delivery_shape else "MONTH"
    if (
        str(delivery_shape or "MONTH").strip().upper() != "MONTH"
        or not delivery_month
    ):
        return None
    return build_delivery_month_component(
        asset,
        model,
        delivery_month,
        parse_date(valuation_date_value, date.today()),
        mapping_id=mapping_id,
    )


def _exchange_expiration_should_reset():
    triggered = _get_pricer_triggered_id()
    if triggered is None:
        return True
    if not isinstance(triggered, dict):
        return False
    return triggered.get("type") in {
        "pricer-asset",
        "pricer-mapping-id",
        "pricer-valuation-date",
    } or triggered.get("param") == "delivery_month"


def _sync_governed_month_contract_dates(
    model,
    expiration_value,
    contract_value,
    asset,
    delivery_shape,
    delivery_month,
    valuation_date_value,
    mapping_id=None,
):
    try:
        component = _governed_delivery_component(
            asset,
            model,
            delivery_shape,
            delivery_month,
            valuation_date_value,
            mapping_id,
        )
    except StructureValidationError:
        return (no_update,) * 4 + (True,)
    if component and mapping_id:
        exchange_expiration = component["contract_expiration_date"]
        exchange_date = parse_date(exchange_expiration)
        expiration = parse_date(expiration_value, exchange_date)
        expiration_update = (
            exchange_expiration
            if (
                mapping_id is not None
                or _exchange_expiration_should_reset()
                or expiration > exchange_date
            )
            else no_update
        )
        return (
            expiration_update,
            exchange_expiration,
            exchange_expiration,
            exchange_expiration,
            True,
        )
    contract_update, contract_minimum = _sync_contract_date(
        expiration_value,
        contract_value,
    )
    return no_update, None, contract_update, contract_minimum, False


@callback(
    [
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "black76",
                "param": "expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "black76",
                "param": "expiration_date",
            },
            "max_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "black76",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "black76",
                "param": "contract_expiration_date",
            },
            "min_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "black76",
                "param": "contract_expiration_date",
            },
            "disabled",
        ),
    ],
    [
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "black76",
                "param": "expiration_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "black76",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": "delivery_shape",
            },
            "value",
        ),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": "black76",
                "param": "delivery_month",
            },
            "value",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": MATCH},
            "date",
        ),
    ],
    prevent_initial_call=True,
)
def sync_black76_contract_expiration_date(
    expiration_value,
    contract_value,
    asset,
    mapping_id,
    delivery_shape,
    delivery_month,
    valuation_date_value,
):
    return _sync_governed_month_contract_dates(
        "black76",
        expiration_value,
        contract_value,
        asset,
        delivery_shape,
        delivery_month,
        valuation_date_value,
        mapping_id,
    )


@callback(
    [
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "american_futures",
                "param": "expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "american_futures",
                "param": "expiration_date",
            },
            "max_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "american_futures",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "american_futures",
                "param": "contract_expiration_date",
            },
            "min_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "american_futures",
                "param": "contract_expiration_date",
            },
            "disabled",
        ),
    ],
    [
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "american_futures",
                "param": "expiration_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "american_futures",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": "delivery_shape",
            },
            "value",
        ),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": "american_futures",
                "param": "delivery_month",
            },
            "value",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": MATCH},
            "date",
        ),
    ],
    prevent_initial_call=True,
)
def sync_american_futures_contract_expiration_date(
    expiration_value,
    contract_value,
    asset,
    mapping_id,
    delivery_shape,
    delivery_month,
    valuation_date_value,
):
    return _sync_governed_month_contract_dates(
        "american_futures",
        expiration_value,
        contract_value,
        asset,
        delivery_shape,
        delivery_month,
        valuation_date_value,
        mapping_id,
    )


@callback(
    [
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "averaging_start_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "expiration_date",
            },
            "min_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "expiration_date",
            },
            "max_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "averaging_start_date",
            },
            "max_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "contract_expiration_date",
            },
            "min_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "averaging_start_date",
            },
            "disabled",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "expiration_date",
            },
            "disabled",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "contract_expiration_date",
            },
            "disabled",
        ),
    ],
    [
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "averaging_start_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "expiration_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": "delivery_shape",
            },
            "value",
        ),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": "asian76",
                "param": "delivery_month",
            },
            "value",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": MATCH},
            "date",
        ),
    ],
    prevent_initial_call=True,
)
def sync_asian76_dates(
    averaging_start_value,
    expiration_value,
    contract_value,
    asset,
    mapping_id,
    delivery_shape,
    delivery_month,
    valuation_date_value,
):
    if isinstance(delivery_shape, list):
        delivery_shape = delivery_shape[0] if delivery_shape else "MONTH"
    try:
        component = _governed_delivery_component(
            asset,
            "asian76",
            delivery_shape,
            delivery_month,
            valuation_date_value,
            mapping_id,
        )
    except StructureValidationError:
        return (no_update,) * 7 + (False, False, True)
    if component and mapping_id and asset == "JKM":
        averaging_start = component["averaging_start_date"]
        averaging_end = component["averaging_end_date"]
        contract_expiration = component["contract_expiration_date"]
        return (
            averaging_start,
            averaging_end,
            averaging_start,
            averaging_end,
            averaging_end,
            contract_expiration,
            averaging_end,
            True,
            True,
            True,
        )
    if component and mapping_id:
        exchange_expiration = component["contract_expiration_date"]
        exchange_date = parse_date(exchange_expiration)
        averaging_start = parse_date(averaging_start_value, exchange_date)
        averaging_start_update = (
            exchange_expiration if averaging_start > exchange_date else no_update
        )
        effective_start = min(averaging_start, exchange_date)
        expiration = parse_date(expiration_value, exchange_date)
        corrected_expiration = min(
            exchange_date,
            max(effective_start, expiration),
        )
        expiration_update = (
            exchange_expiration
            if _exchange_expiration_should_reset()
            else (
                corrected_expiration.isoformat()
                if corrected_expiration != expiration
                else no_update
            )
        )
        expiration_for_limits = (
            exchange_date
            if expiration_update == exchange_expiration
            else corrected_expiration
        )
        return (
            averaging_start_update,
            expiration_update,
            effective_start.isoformat(),
            exchange_expiration,
            expiration_for_limits.isoformat(),
            exchange_expiration,
            expiration_for_limits.isoformat(),
            False,
            False,
            True,
        )
    if not averaging_start_value or not expiration_value:
        return (no_update,) * 7 + (False, False, False)
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
        no_update,
        expiration_update,
        averaging_start.isoformat(),
        None,
        corrected_expiration.isoformat(),
        contract_update,
        corrected_expiration.isoformat(),
        False,
        False,
        False,
    )


@callback(
    [
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "kirk",
                "param": "expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "kirk",
                "param": "expiration_date",
            },
            "max_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "kirk",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "kirk",
                "param": "contract_expiration_date",
            },
            "min_date_allowed",
        ),
        Output(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "kirk",
                "param": "contract_expiration_date",
            },
            "disabled",
        ),
    ],
    [
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "kirk",
                "param": "expiration_date",
            },
            "date",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": "kirk",
                "param": "contract_expiration_date",
            },
            "date",
        ),
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": "delivery_shape",
            },
            "value",
        ),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": "kirk",
                "param": "delivery_month",
            },
            "value",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": MATCH},
            "date",
        ),
    ],
    prevent_initial_call=True,
)
def sync_kirk_contract_expiration_date(
    expiration_value,
    contract_value,
    asset,
    delivery_shape,
    delivery_month,
    valuation_date_value,
):
    return _sync_governed_month_contract_dates(
        "kirk",
        expiration_value,
        contract_value,
        asset,
        delivery_shape,
        delivery_month,
        valuation_date_value,
    )


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


def _surface_reference_input_signature(
    asset,
    model,
    rows,
    context,
    valuation_date_value,
):
    """Identify the exact inputs used to resolve each published-surface vol."""
    return {
        "asset": str(asset or ""),
        "model": model,
        "valuation_date": parse_date(
            valuation_date_value,
            date.today(),
        ).isoformat(),
        "context": _normalized_signature_context(context),
        "legs": [
            {
                "leg_id": str(row.get("leg_id") or ""),
                "call_put": copy.deepcopy(row.get("call_put")),
                "strike": copy.deepcopy(row.get("strike")),
            }
            for row in rows or []
            if isinstance(row, dict)
        ],
    }


@callback(
    Output(
        {
            "type": "pricer-published-surface-reference",
            "structure_id": MATCH,
        },
        "data",
    ),
    [
        Input("refresh-options-data", "n_clicks"),
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-option-type", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-legs-grid", "structure_id": MATCH}, "rowData"),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "value",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "date",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": MATCH},
            "date",
        ),
    ],
    [
        State(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "id",
        ),
        State(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "id",
        ),
    ],
)
def update_published_surface_reference(
    _refresh_clicks,
    asset,
    model,
    mapping_id,
    rows,
    param_values,
    date_values,
    valuation_date_value,
    param_ids,
    date_ids,
):
    context = _context_from_states(
        model,
        param_values,
        param_ids,
        date_values,
        date_ids,
    )
    if mapping_id is not None:
        context["exchange_mapping_id"] = mapping_id
    normalized_rows = _quote_ready_rows(model, rows)
    payload = build_published_surface_reference(
        asset,
        model,
        context,
        normalized_rows,
        parse_date(valuation_date_value, date.today()),
        force_refresh=_get_pricer_triggered_id() == "refresh-options-data",
    )
    payload["_ui_reference_signature"] = _surface_reference_input_signature(
        asset,
        model,
        normalized_rows,
        context,
        valuation_date_value,
    )
    return payload


@callback(
    Output(
        {"type": "pricer-legs-grid", "structure_id": MATCH},
        "dashGridOptions",
    ),
    [
        Input(
            {"type": "pricer-grid-pricing-options", "structure_id": MATCH},
            "data",
        ),
        Input(
            {
                "type": "pricer-published-surface-reference",
                "structure_id": MATCH,
            },
            "data",
        ),
    ],
)
def render_leg_grid_options(pricing_options, surface_reference):
    if not isinstance(pricing_options, dict):
        pricing_options = _leg_grid_options()
    rendered = copy.deepcopy(pricing_options)
    context = rendered.setdefault("context", {})
    context.setdefault("pricingRows", {})
    surface_rows = {}
    if (
        isinstance(surface_reference, dict)
        and surface_reference.get("schema_version") == REFERENCE_SCHEMA_VERSION
        and isinstance(surface_reference.get("rows"), dict)
    ):
        surface_rows = copy.deepcopy(surface_reference["rows"])
    context["surfaceRows"] = surface_rows
    return rendered


def _normalized_signature_context(context):
    normalized = {}
    for key, value in (context or {}).items():
        if key in {"structure_type", "asset"}:
            continue
        if key and key.endswith("_date") and value:
            normalized[key] = parse_date(value).isoformat()
        else:
            normalized[key] = value
    delivery_shape = str(
        normalized.get("delivery_shape") or "MONTH"
    ).strip().upper()
    if delivery_shape == "MONTH":
        normalized.pop("delivery_year", None)
    else:
        normalized.pop("averaging_start_date", None)
        normalized.pop("expiration_date", None)
        normalized.pop("contract_expiration_date", None)
        normalized.pop("delivery_month", None)
    return normalized


def _published_surface_calculation_rows(
    asset,
    model,
    rows,
    surface_reference,
    expected_reference_signature,
):
    """Replace legacy quote fields with governed per-leg contract vols."""
    normalized_rows = _rows_with_volatility_adjustments(model, rows or [])
    if model not in SINGLE_ASSET_MODELS:
        return normalized_rows, None
    if (
        not isinstance(surface_reference, dict)
        or surface_reference.get("schema_version") != REFERENCE_SCHEMA_VERSION
    ):
        raise StructureValidationError(
            "Published surface volatility is not available yet. "
            "Wait for the surface to load and calculate again."
        )
    if surface_reference.get("asset") != str(asset or ""):
        raise StructureValidationError(
            "Published surface volatility does not match the selected asset."
        )
    if surface_reference.get("model") != model:
        raise StructureValidationError(
            "Published surface volatility does not match the selected model."
        )
    if surface_reference.get("_ui_reference_signature") != expected_reference_signature:
        raise StructureValidationError(
            "Published surface volatility is still refreshing for the current inputs. "
            "Wait for it to load and calculate again."
        )
    source_kind = str(surface_reference.get("source_kind") or "governed")
    publication_fields = (
        ("publication_id", "publication_cob", "published_at")
        if source_kind == "governed"
        else ("source_revision", "publication_cob", "source_kind", "source")
    )
    if any(
        surface_reference.get(field) in (None, "")
        for field in publication_fields
    ):
        raise StructureValidationError(
            "No published surface revision is available for the selected inputs."
        )
    surface_rows = surface_reference.get("rows")
    if not isinstance(surface_rows, dict):
        raise StructureValidationError(
            "Published surface volatility is unavailable for the option legs."
        )

    resolved_rows = []
    surface_signature_rows = []
    for position, row in enumerate(normalized_rows, start=1):
        leg_id = str(row.get("leg_id") or "")
        surface_row = surface_rows.get(leg_id)
        if not isinstance(surface_row, dict):
            raise StructureValidationError(
                f"Leg {position}: published surface volatility is unavailable."
            )
        input_volatility = surface_row.get("surface_input_vol")
        atm_input_volatility = surface_row.get("surface_atm_input_vol")
        skew_input_volatility = surface_row.get("surface_skew_input_vol")
        pricing_volatility = surface_row.get("surface_pricing_vol")
        try:
            input_volatility = float(input_volatility)
            atm_input_volatility = float(atm_input_volatility)
            skew_input_volatility = float(skew_input_volatility)
            pricing_volatility = float(pricing_volatility)
        except (TypeError, ValueError, OverflowError):
            detail = surface_row.get("surface_input_tooltip")
            raise StructureValidationError(
                f"Leg {position}: {detail or 'published surface volatility is unavailable.'}"
            ) from None
        if not all(
            math.isfinite(value) and 0.005 <= value <= 2.0
            for value in (
                input_volatility,
                atm_input_volatility,
                pricing_volatility,
            )
        ) or not math.isfinite(skew_input_volatility):
            detail = surface_row.get("surface_input_tooltip")
            raise StructureValidationError(
                f"Leg {position}: {detail or 'published surface volatility is outside the supported range.'}"
            )
        if not math.isclose(
            input_volatility,
            atm_input_volatility + skew_input_volatility,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise StructureValidationError(
                f"Leg {position}: published Input vol does not reconcile to ATM plus Skew."
            )
        adjustment_values = {}
        for field, label in zip(
            VOLATILITY_ADJUSTMENT_FIELDS,
            ("ATM", "Skew", "Smile"),
        ):
            raw_adjustment = row.get(field)
            if isinstance(raw_adjustment, bool):
                raw_adjustment = None
            try:
                adjustment = float(raw_adjustment)
            except (TypeError, ValueError, OverflowError):
                raise StructureValidationError(
                    f"Leg {position}: {label} volatility adjustment must be finite."
                ) from None
            if not math.isfinite(adjustment):
                raise StructureValidationError(
                    f"Leg {position}: {label} volatility adjustment must be finite."
                )
            if abs(adjustment) > MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT:
                raise StructureValidationError(
                    f"Leg {position}: {label} volatility adjustment is limited to "
                    f"{MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT:.0f} vol points."
                )
            adjustment_values[field] = adjustment
        total_adjustment = VOLATILITY_ADJUSTMENT_SCALE * sum(
            adjustment_values.values()
        )
        effective_input_volatility = input_volatility + total_adjustment
        effective_pricing_volatility = pricing_volatility * (
            effective_input_volatility / input_volatility
        )
        component_volatilities = []
        raw_component_volatilities = surface_row.get(
            "surface_component_volatilities"
        )
        if raw_component_volatilities is not None:
            if not isinstance(raw_component_volatilities, list):
                raise StructureValidationError(
                    f"Leg {position}: published component volatility metadata is invalid."
                )
            for component_position, component_row in enumerate(
                raw_component_volatilities,
                start=1,
            ):
                if not isinstance(component_row, dict):
                    raise StructureValidationError(
                        f"Leg {position}: published component {component_position} "
                        "volatility metadata is invalid."
                )
                try:
                    contract_month = str(component_row["contract_month"])
                    component_input = float(component_row["input_volatility"])
                    component_pricing = float(component_row["pricing_volatility"])
                    expiry_factor = float(
                        component_row["expiry_adjustment_factor"]
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    raise StructureValidationError(
                        f"Leg {position}: published component {component_position} "
                        "volatility metadata is invalid."
                    ) from None
                effective_component_input = component_input + total_adjustment
                effective_component_pricing = (
                    effective_component_input * expiry_factor
                )
                if not (
                    math.isfinite(component_input)
                    and 0.005 <= component_input <= 2.0
                    and math.isfinite(component_pricing)
                    and 0.005 <= component_pricing <= 2.0
                    and math.isfinite(expiry_factor)
                    and expiry_factor > 0.0
                    and math.isclose(
                        component_pricing,
                        component_input * expiry_factor,
                        rel_tol=1e-10,
                        abs_tol=1e-12,
                    )
                    and math.isfinite(effective_component_input)
                    and 0.005 <= effective_component_input <= 2.0
                    and math.isfinite(effective_component_pricing)
                    and 0.005 <= effective_component_pricing <= 2.0
                ):
                    raise StructureValidationError(
                        f"Leg {position}: published component {component_position} "
                        "volatility or expiry adjustment is outside the supported range."
                    )
                component_volatilities.append(
                    {
                        "contract_month": contract_month,
                        "input_volatility": effective_component_input,
                        "pricing_volatility": effective_component_pricing,
                        "expiry_adjustment_factor": expiry_factor,
                    }
                )
        if component_volatilities:
            try:
                effective_input_volatility = float(
                    surface_row["surface_effective_input_vol"]
                )
                effective_pricing_volatility = float(
                    surface_row["surface_effective_pricing_vol"]
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                raise StructureValidationError(
                    f"Leg {position}: the premium-equivalent Pricing Vol "
                    "could not be resolved."
                ) from None
        if not (
            math.isfinite(effective_input_volatility)
            and 0.005 <= effective_input_volatility <= 2.0
            and math.isfinite(effective_pricing_volatility)
            and 0.005 <= effective_pricing_volatility <= 2.0
        ):
            raise StructureValidationError(
                f"Leg {position}: the volatility adjustments produce an Input vol "
                "outside the supported 0.005–2.0 range."
            )
        resolved = copy.deepcopy(row)
        resolved["quote_basis"] = "VOL"
        resolved["quote_value"] = effective_input_volatility
        resolved["expiry_adjustment_factor"] = (
            effective_pricing_volatility / effective_input_volatility
        )
        resolved.pop("volatility", None)
        if len(component_volatilities) > 1:
            resolved["component_volatilities"] = component_volatilities
        resolved_rows.append(resolved)
        surface_signature_rows.append(
            {
                "leg_id": leg_id,
                "surface_input_vol": input_volatility,
                "surface_atm_input_vol": atm_input_volatility,
                "surface_skew_input_vol": skew_input_volatility,
                "surface_pricing_vol": pricing_volatility,
                **adjustment_values,
                "effective_input_vol": effective_input_volatility,
                "effective_pricing_vol": effective_pricing_volatility,
                **(
                    {
                        "component_volatilities": copy.deepcopy(
                            component_volatilities
                        )
                    }
                    if raw_component_volatilities is not None
                    else {}
                ),
                "surface_expiry_adjustments": copy.deepcopy(
                    surface_row.get("surface_expiry_adjustments") or []
                ),
            }
        )
    return resolved_rows, {
        field: str(surface_reference[field]) for field in publication_fields
    } | {"rows": surface_signature_rows}


def _signature_leg_rows(model, rows):
    common_fields = (
        "leg_id",
        "name",
        "side",
        "ratio",
        "call_put",
        "strike",
    )
    model_fields = (
        ("quote_basis", "quote_value")
        if model in SINGLE_ASSET_MODELS
        else ("volatility_asset_1", "volatility_asset_2")
    )
    fields = (*common_fields, *model_fields)
    return [
        {field: copy.deepcopy(row.get(field)) for field in fields}
        for row in _quote_ready_rows(model, rows or [])
    ]


def _input_signature_from_context(
    asset,
    model,
    contract_multiplier,
    rows,
    context,
    valuation_date_value,
):
    normalized_context = _normalized_signature_context(context)
    premium_convention = normalized_context.get(
        "premium_convention", default_premium_convention(asset, model)
    )
    is_futures_style = premium_convention == "futures_style"
    if is_futures_style and model in SINGLE_ASSET_MODELS:
        normalized_context["rate"] = 0.0
    return {
        "asset": asset,
        "model": model,
        "contract_multiplier": _coerce_pricer_float(contract_multiplier),
        "valuation_date": parse_date(
            valuation_date_value,
            date.today(),
        ).isoformat(),
        "context": normalized_context,
        "legs": _signature_leg_rows(model, rows),
    }


def _calculation_input_signature(
    asset,
    model,
    contract_multiplier,
    rows,
    param_values,
    date_values,
    valuation_date_value,
    param_ids,
    date_ids,
    surface_reference=None,
    use_published_surface=False,
    mapping_id=None,
):
    context = _context_from_states(
        model,
        param_values,
        param_ids,
        date_values,
        date_ids,
    )
    if mapping_id is not None:
        context["exchange_mapping_id"] = mapping_id
    calculation_rows = rows
    surface_signature = None
    if use_published_surface:
        expected_reference_signature = _surface_reference_input_signature(
            asset,
            model,
            rows,
            context,
            valuation_date_value,
        )
        calculation_rows, surface_signature = _published_surface_calculation_rows(
            asset,
            model,
            rows,
            surface_reference,
            expected_reference_signature,
        )
    input_signature = _input_signature_from_context(
        asset,
        model,
        contract_multiplier,
        calculation_rows,
        context,
        valuation_date_value,
    )
    if surface_signature is not None:
        input_signature["published_surface"] = surface_signature
    if mapping_id is not None:
        input_signature["mapping_id"] = mapping_id
    return input_signature


def _template_input_signature(template):
    if not isinstance(template, dict):
        return None
    model = template.get("model")
    if model not in MODEL_LABELS:
        return None
    resolved_context = default_context(model, date.today())
    if isinstance(template.get("context"), dict):
        resolved_context.update(template["context"])
    if template.get("mapping_id") is not None:
        resolved_context["exchange_mapping_id"] = template["mapping_id"]
    signature = _input_signature_from_context(
        template.get("asset", DEFAULT_ASSET),
        model,
        template.get("contract_multiplier", 1),
        template.get("legs") or [default_leg(model, 1)],
        resolved_context,
        template.get("valuation_date", date.today().isoformat()),
    )
    if template.get("mapping_id") is not None:
        signature["mapping_id"] = template["mapping_id"]
    return signature


def _snapshot_matches_template(snapshot, template):
    return (
        isinstance(snapshot, dict)
        and snapshot.get("schema_version") == SCHEMA_VERSION
        and snapshot.get("_ui_input_signature")
        == _template_input_signature(template)
    )


def calculate_structure_callback(
    n_clicks,
    asset,
    model,
    contract_multiplier,
    rows,
    param_values,
    date_values,
    valuation_date_value,
    param_ids,
    date_ids,
    triggered_override=None,
    has_snapshot=None,
    surface_reference=None,
    use_published_surface=False,
    mapping_id=None,
):
    triggered = (
        triggered_override
        if triggered_override is not None
        else _get_pricer_triggered_id()
    )
    if isinstance(triggered, dict):
        triggered = triggered.get("type")
    triggered = {
        "pricer-option-type": "option-type",
        "pricer-calculate-button": "calculate-button",
        "pricer-calculate-all": "calculate-button",
        "pricer-exchange-calculate-all": "calculate-button",
    }.get(triggered, triggered)
    had_calculation = bool(n_clicks) if has_snapshot is None else bool(has_snapshot)
    if triggered == "pricer-asset":
        if not had_calculation:
            return None, ""
        return None, _build_pricer_message(
            "Modified · outputs cleared · calculate again",
            tone="warning",
        )
    if triggered == "option-type":
        if not had_calculation:
            return None, ""
        return None, _build_pricer_message(
            "Model changed · outputs cleared · configure and calculate again",
            tone="warning",
        )
    if triggered != "calculate-button":
        if not had_calculation:
            return None, ""
        return None, _build_pricer_message(
            "Modified · outputs cleared · calculate again",
            tone="warning",
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
    if mapping_id is not None:
        context["exchange_mapping_id"] = mapping_id
    calculation_rows = rows or []
    surface_signature = None
    try:
        if use_published_surface:
            expected_reference_signature = _surface_reference_input_signature(
                asset,
                model,
                calculation_rows,
                context,
                valuation_date_value,
            )
            calculation_rows, surface_signature = (
                _published_surface_calculation_rows(
                    asset,
                    model,
                    calculation_rows,
                    surface_reference,
                    expected_reference_signature,
                )
            )
        input_signature = _input_signature_from_context(
            asset,
            model,
            contract_multiplier,
            calculation_rows,
            context,
            valuation_date_value,
        )
    except StructureValidationError as exc:
        return None, _build_pricer_message(str(exc), tone="danger")
    if surface_signature is not None:
        input_signature["published_surface"] = surface_signature
    if mapping_id is not None:
        input_signature["mapping_id"] = mapping_id
    context = copy.deepcopy(input_signature["context"])
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
            calculation_rows,
            as_of=valuation_date,
        )
    except StructureValidationError as exc:
        return None, _build_pricer_message(str(exc), tone="danger")
    except Exception as exc:
        return None, _build_pricer_message(
            f"Structure calculation failed ({type(exc).__name__}).",
            tone="danger",
        )
    if surface_signature is not None:
        snapshot["surface_expiry_adjustments"] = {
            row["leg_id"]: copy.deepcopy(row["surface_expiry_adjustments"])
            for row in surface_signature["rows"]
        }
        effective_pricing_vols = {
            row["leg_id"]: row["effective_pricing_vol"]
            for row in surface_signature["rows"]
        }
        inconsistent_leg = next(
            (
                index
                for index, leg in enumerate(snapshot["legs"], start=1)
                if not math.isclose(
                    leg["volatility_used"],
                    effective_pricing_vols.get(leg["leg_id"], math.nan),
                    rel_tol=1e-10,
                    abs_tol=1e-12,
                )
            ),
            None,
        )
        if inconsistent_leg is not None:
            return None, _build_pricer_message(
                f"Leg {inconsistent_leg}: the published surface pricing volatility "
                "and volatility adjustments are inconsistent with the "
                "contract-date adjustment.",
                tone="danger",
            )
    snapshot["_ui_input_signature"] = input_signature
    leg_count = len(snapshot["legs"])
    leg_label = "leg" if leg_count == 1 else "legs"
    strip_detail = ""
    if snapshot["context"].get("delivery_components"):
        strip_detail = f" · {snapshot['context']['delivery_component_count']} months"
    return snapshot, _build_pricer_message(
        f"Calculated · {leg_count} {leg_label} · "
        f"{snapshot['model_label']}{strip_detail}",
        tone="success",
    )


@callback(
    [
        Output(
            {"type": "pricer-calculation-store", "structure_id": MATCH},
            "data",
        ),
        Output(
            {"type": "pricer-calculation-status", "structure_id": MATCH},
            "children",
        ),
        Output(
            {"type": "pricer-grid-pricing-options", "structure_id": MATCH},
            "data",
        ),
        Output(
            {"type": "pricer-calculate-all-ack", "structure_id": MATCH},
            "data",
        ),
    ],
    [
        Input(
            {"type": "pricer-calculate-button", "structure_id": MATCH},
            "n_clicks",
        ),
        Input("pricer-calculate-all", "n_clicks"),
        Input("pricer-exchange-calculate-all", "n_clicks"),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-option-type", "structure_id": MATCH}, "value"),
        Input(
            {"type": "pricer-contract-multiplier", "structure_id": MATCH},
            "value",
        ),
        Input({"type": "pricer-legs-grid", "structure_id": MATCH}, "rowData"),
        Input(
            {"type": "pricer-legs-grid", "structure_id": MATCH},
            "cellValueChanged",
        ),
        Input(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "value",
        ),
        Input(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "date",
        ),
        Input(
            {"type": "pricer-valuation-date", "structure_id": MATCH}, "date"
        ),
        Input(
            {
                "type": "pricer-published-surface-reference",
                "structure_id": MATCH,
            },
            "data",
        ),
    ],
    [
        State(
            {
                "type": "pricer-context-param",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "id",
        ),
        State(
            {
                "type": "pricer-context-date",
                "structure_id": MATCH,
                "model": ALL,
                "param": ALL,
            },
            "id",
        ),
        State(
            {"type": "pricer-calculation-store", "structure_id": MATCH},
            "data",
        ),
        State(
            {"type": "pricer-calculate-all-baseline", "structure_id": MATCH},
            "data",
        ),
        State(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
        State("url", "pathname"),
    ],
)
def _calculate_structure_instance_dash_callback(
    local_clicks,
    calculate_all_clicks,
    exchange_calculate_all_clicks,
    mapping_id,
    asset,
    model,
    contract_multiplier,
    rows,
    cell_value_changed,
    param_values,
    date_values,
    valuation_date_value,
    surface_reference,
    param_ids,
    date_ids,
    current_snapshot,
    calculate_all_baseline=0,
    workflow="legacy",
    pathname=None,
):
    triggered = _get_pricer_triggered_id()
    triggered_type = triggered.get("type") if isinstance(triggered, dict) else triggered
    use_published_surface = pathname == "/pricer"
    is_exchange_workflow = workflow == "exchange"
    if is_exchange_workflow and mapping_id is None:
        mapping_id = DEFAULT_EXCHANGE_MAPPING_ID
    active_calculate_all_clicks = (
        exchange_calculate_all_clicks
        if is_exchange_workflow
        else calculate_all_clicks
    )
    calculate_all_trigger = (
        "pricer-exchange-calculate-all"
        if is_exchange_workflow
        else "pricer-calculate-all"
    )
    current_all_count = _nonnegative_click_count(active_calculate_all_clicks)
    baseline_count = _nonnegative_click_count(calculate_all_baseline)
    has_unconsumed_calculate_all = current_all_count > baseline_count
    is_calculate_all = (
        triggered_type in (None, calculate_all_trigger)
        and has_unconsumed_calculate_all
    )
    is_local_calculate = triggered_type == "pricer-calculate-button"
    if triggered_type is None and not is_calculate_all:
        return no_update, no_update, no_update, no_update
    if triggered_type in {
        "pricer-calculate-all",
        "pricer-exchange-calculate-all",
    } and not is_calculate_all:
        return no_update, no_update, no_update, no_update
    if (
        is_exchange_workflow
        and (is_calculate_all or is_local_calculate)
        and not exchange_mapping_pricing_supported(mapping_id)
    ):
        return (
            None,
            _build_pricer_message(
                exchange_mapping_capture_message(mapping_id),
                tone="danger",
            ),
            _leg_grid_options(compact=use_published_surface),
            current_all_count if is_calculate_all else no_update,
        )
    if not is_calculate_all and not is_local_calculate and current_snapshot is None:
        return no_update, no_update, no_update, no_update
    latest_cell_event = (
        cell_value_changed[-1]
        if isinstance(cell_value_changed, list) and cell_value_changed
        else cell_value_changed
    )
    committed_grid_edit = (
        triggered_type == "pricer-legs-grid"
        and isinstance(latest_cell_event, dict)
        and latest_cell_event.get("oldValue") != latest_cell_event.get("newValue")
    )
    if committed_grid_edit and isinstance(current_snapshot, dict):
        return (
            None,
            _build_pricer_message(
                "Modified · outputs cleared · calculate again",
                tone="warning",
            ),
            _leg_grid_options(compact=use_published_surface),
            no_update,
        )
    if not is_calculate_all and not is_local_calculate and isinstance(
        current_snapshot, dict
    ):
        try:
            current_signature = _calculation_input_signature(
                asset,
                model,
                contract_multiplier,
                rows,
                param_values,
                date_values,
                valuation_date_value,
                param_ids,
                date_ids,
                surface_reference=surface_reference,
                use_published_surface=use_published_surface,
                mapping_id=mapping_id,
            )
        except StructureValidationError as exc:
            return (
                None,
                _build_pricer_message(str(exc), tone="danger"),
                _leg_grid_options(compact=use_published_surface),
                no_update,
            )
        if current_signature == current_snapshot.get("_ui_input_signature"):
            return no_update, no_update, no_update, no_update
    effective_clicks = (
        active_calculate_all_clicks if is_calculate_all else local_clicks
    )
    snapshot, status = calculate_structure_callback(
        effective_clicks,
        asset,
        model,
        contract_multiplier,
        rows,
        param_values,
        date_values,
        valuation_date_value,
        param_ids,
        date_ids,
        triggered_override="calculate-button" if is_calculate_all else triggered,
        has_snapshot=current_snapshot is not None,
        surface_reference=surface_reference,
        use_published_surface=use_published_surface,
        mapping_id=mapping_id,
    )
    return (
        snapshot,
        status,
        _leg_grid_options(snapshot, compact=use_published_surface),
        current_all_count if is_calculate_all else no_update,
    )


def calculate_structure_instance_callback(
    local_clicks,
    calculate_all_clicks,
    asset,
    model,
    contract_multiplier,
    rows,
    cell_value_changed,
    param_values,
    date_values,
    valuation_date_value,
    param_ids,
    date_ids,
    current_snapshot,
    calculate_all_baseline=0,
    surface_reference=None,
    pathname=None,
    exchange_calculate_all_clicks=0,
    workflow="legacy",
    mapping_id=None,
):
    """Compatibility entry point for direct tests and non-Dash callers."""
    return _calculate_structure_instance_dash_callback(
        local_clicks,
        calculate_all_clicks,
        exchange_calculate_all_clicks,
        mapping_id,
        asset,
        model,
        contract_multiplier,
        rows,
        cell_value_changed,
        param_values,
        date_values,
        valuation_date_value,
        surface_reference,
        param_ids,
        date_ids,
        current_snapshot,
        calculate_all_baseline,
        workflow,
        pathname,
    )


clientside_callback(
    """
    function (acknowledged, currentBaseline) {
        const acknowledgedCount = Math.max(Number(acknowledged) || 0, 0);
        const baselineCount = Math.max(Number(currentBaseline) || 0, 0);
        if (acknowledgedCount <= baselineCount) {
            return window.dash_clientside.no_update;
        }
        return acknowledgedCount;
    }
    """,
    Output(
        {"type": "pricer-calculate-all-baseline", "structure_id": MATCH},
        "data",
    ),
    Input(
        {"type": "pricer-calculate-all-ack", "structure_id": MATCH},
        "data",
    ),
    State(
        {"type": "pricer-calculate-all-baseline", "structure_id": MATCH},
        "data",
    ),
    prevent_initial_call=True,
)


def calculate_structure_instance(
    local_clicks,
    calculate_all_clicks,
    asset,
    model,
    contract_multiplier,
    rows,
    param_values,
    date_values,
    valuation_date_value,
    param_ids,
    date_ids,
    current_snapshot,
    calculate_all_baseline=0,
    mapping_id=None,
):
    """Compatibility helper for direct tests and non-Dash callers."""
    snapshot, status, _grid_options, _baseline = calculate_structure_instance_callback(
        local_clicks,
        calculate_all_clicks,
        asset,
        model,
        contract_multiplier,
        rows,
        None,
        param_values,
        date_values,
        valuation_date_value,
        param_ids,
        date_ids,
        current_snapshot,
        calculate_all_baseline,
        mapping_id=mapping_id,
    )
    return snapshot, status


def _model_inputs_summary(snapshot):
    context = snapshot["context"]
    cards = [
        *(
            [
                _build_pricer_result_card(
                    "Asset 1",
                    context["asset_1_code"],
                    context["asset_1_price_unit"],
                    tone="market",
                ),
                _build_pricer_result_card(
                    "Asset 2",
                    context["asset_2_code"],
                    context["asset_2_price_unit"],
                    tone="market",
                ),
            ]
            if snapshot["model"] == "kirk"
            else [
                _build_pricer_result_card(
                    "Asset",
                    context["asset"],
                    tone="market",
                )
            ]
        ),
        _build_pricer_result_card(
            "Pricing model",
            snapshot["model_label"],
            tone="basis",
        ),
        _build_pricer_result_card(
            "Premium convention",
            context["resolved_premium_convention_label"],
            tone="basis",
        ),
        _build_pricer_result_card(
            "Valuation date",
            snapshot["calculation_date"],
            tone="context",
        ),
    ]
    is_delivery_strip = bool(context.get("delivery_components"))
    if is_delivery_strip:
        asset = context.get("asset")
        has_exchange_mapping = bool(context.get("exchange_mapping_id"))
        if has_exchange_mapping:
            if context.get("delivery_total_quantity") is not None:
                quantity_unit = "therms" if asset == "NBP" else "MMBtu"
                component_quantity = (
                    f"{context['delivery_total_quantity']:,} {quantity_unit}"
                )
            else:
                component_quantity = (
                    f"{context['delivery_total_hours']:,} delivery hours"
                )
            product_code = context.get("exchange_product_code")
            expiry_label = (
                f"{product_code} expiry range"
                if product_code
                else "Option expiry range"
            )
        else:
            is_jkm = asset == "JKM"
            component_quantity = (
                f"{context['delivery_total_quantity']:,} MMBtu"
                if is_jkm
                else f"{context['delivery_total_hours']:,} delivery hours"
            )
            expiry_label = (
                "APO expiry range"
                if is_jkm and snapshot["model"] == "asian76"
                else "JKZ / TFO expiry range"
                if is_jkm
                else "TFO expiry range"
            )
        cards.extend(
            [
                _build_pricer_result_card(
                    "Delivery strip",
                    context["delivery_period_label"],
                    DELIVERY_SHAPE_LABELS[context["delivery_shape"]],
                    tone="market",
                ),
                _build_pricer_result_card(
                    "Flat monthly forward",
                    _format_number(context["forward"]),
                    tone="market",
                ),
                _build_pricer_result_card(
                    "Monthly components",
                    str(context["delivery_component_count"]),
                    component_quantity,
                    tone="context",
                ),
                _build_pricer_result_card(
                    expiry_label,
                    (
                        f"{context['first_expiration_date']} → "
                        f"{context['last_expiration_date']}"
                    ),
                    "Exact monthly option expiries",
                    tone="context",
                ),
            ]
        )
    elif snapshot["model"] in SINGLE_ASSET_MODELS:
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
                    "Exchange option expiration",
                    context["contract_expiration_date"],
                    tone="context",
                ),
            ]
        )
        if context.get("margin_style") != "futures_style":
            cards.append(
                _build_pricer_result_card(
                    "Risk-free rate",
                    f"{context['rate']:.4%}",
                    tone="context",
                )
            )
    else:
        cards.extend(
            [
                _build_pricer_result_card(
                    "Asset 1 forward",
                    _format_number(context["asset_1_forward"]),
                    context["asset_1_price_unit"],
                    tone="market",
                ),
                _build_pricer_result_card(
                    "Asset 2 forward",
                    _format_number(context["asset_2_forward"]),
                    context["asset_2_price_unit"],
                    tone="market",
                ),
                _build_pricer_result_card(
                    "Contractual option expiry",
                    context["contractual_expiry"],
                    tone="context",
                ),
                _build_pricer_result_card(
                    "Volatility-reference expiries",
                    (
                        f"{context['asset_1_reference_expiry']} / "
                        f"{context['asset_2_reference_expiry']}"
                    ),
                    "Asset 1 / Asset 2",
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
        Output(
            {"type": "pricer-results-container", "structure_id": MATCH},
            "children",
        ),
        Output(
            {"type": "pricer-unit-results-container", "structure_id": MATCH},
            "children",
        ),
        Output(
            {"type": "pricer-greeks-container", "structure_id": MATCH},
            "children",
        ),
        Output(
            {"type": "pricer-time-info", "structure_id": MATCH}, "children"
        ),
        Output(
            {
                "type": "pricer-model-inputs-used-container",
                "structure_id": MATCH,
            },
            "children",
        ),
        Output(
            {"type": "pricer-warning-container", "structure_id": MATCH},
            "children",
        ),
    ],
    Input(
        {"type": "pricer-calculation-store", "structure_id": MATCH}, "data"
    ),
    State({"type": "pricer-calculation-store", "structure_id": MATCH}, "id"),
)
def render_structure_results(snapshot, calculation_store_id=None):
    structure_id = (
        calculation_store_id.get("structure_id")
        if isinstance(calculation_store_id, dict)
        else DEFAULT_STRUCTURE_ID
    )
    if not snapshot:
        return "", "", "", "", "", ""
    if not _is_valid_calculation_snapshot(snapshot):
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
    is_delivery_strip = bool(snapshot["context"].get("delivery_components"))
    time_to_expiry_value = f"{snapshot['context']['time_to_expiry']:.6f}y"
    time_detail = None
    if is_delivery_strip:
        expiry_name = (
            "JKM APO"
            if snapshot["context"].get("asset") == "JKM"
            and snapshot["model"] == "asian76"
            else "JKZ / TFO"
            if snapshot["context"].get("asset") == "JKM"
            else "TFO"
        )
        time_detail = (
            f"Component-weighted time; first/last {expiry_name} expiry "
            f"{snapshot['context']['first_expiration_date']} / "
            f"{snapshot['context']['last_expiration_date']}"
        )
    elif snapshot["model"] == "asian76":
        time_detail = (
            f"Averaging starts {snapshot['context']['averaging_start_date']} "
            f"("
            f"{round(snapshot['context']['time_to_averaging_start'] * 365)} days"
            f")"
        )
    if is_delivery_strip:
        weighting_label = (
            "equal monthly 10,000 MMBtu lots"
            if snapshot["context"].get("asset") == "JKM"
            else "delivery-hour weighted"
        )
        volatility_adjustment_detail = (
            "No scalar date adjustment; each month uses its governed expiry; "
            f"{snapshot['context']['variance_calendar_code']}; "
            f"{snapshot['context']['day_count_basis']}; {weighting_label}"
        )
    elif snapshot["model"] == "kirk":
        volatility_adjustment_detail = (
            f"Asset 1: √({snapshot['context']['asset_1_contractual_business_days']} "
            f"days / {snapshot['context']['asset_1_reference_business_days']} "
            f"reference days), {snapshot['context']['asset_1_calendar_code']}; "
            f"Asset 2: √({snapshot['context']['asset_2_contractual_business_days']} "
            f"days / {snapshot['context']['asset_2_reference_business_days']} "
            f"reference days), {snapshot['context']['asset_2_calendar_code']}; "
            f"{snapshot['context']['day_count_basis']}"
        )
    else:
        volatility_adjustment_detail = (
            f"√({snapshot['context']['option_business_days']} days / "
            f"{snapshot['context']['contract_business_days']} contract days); "
            f"{snapshot['context']['variance_calendar_code']}; "
            f"{snapshot['context']['day_count_basis']}"
        )
    warning_children = [
        _build_pricer_message(warning, tone="warning")
        for warning in snapshot.get("warnings") or []
        if warning != FUTURES_STYLE_RATE_NOTE
    ]
    volatility_adjustment_value = (
        (
            f"{snapshot['context']['asset_1_vol_adjustment_factor']:.6f}× / "
            f"{snapshot['context']['asset_2_vol_adjustment_factor']:.6f}×"
        )
        if snapshot["model"] == "kirk"
        else f"{snapshot['context']['vol_adjustment_factor']:.6f}×"
    )
    output_cards = [
        html.Span(
            [
                html.Span("T", className="pricer-calculation-meta-label"),
                time_to_expiry_value,
            ],
            className="pricer-calculation-meta-item",
            title=time_detail or "Time to expiry",
        ),
        html.Span(
            [
                html.Span(
                    "Vol adj",
                    className="pricer-calculation-meta-label",
                ),
                volatility_adjustment_value,
            ],
            className="pricer-calculation-meta-item",
            title=volatility_adjustment_detail,
        ),
    ]
    if snapshot["context"].get("exchange_product_code"):
        product_context = snapshot["context"]
        has_exchange_mapping = bool(product_context.get("exchange_mapping_id"))
        product_value = product_context["exchange_product_code"]
        if has_exchange_mapping and product_context.get("exchange_product_id"):
            product_value = (
                f"{product_value} · ID {product_context['exchange_product_id']}"
            )
        product_detail_fields = [product_context.get("exchange_product_name")]
        if has_exchange_mapping:
            product_detail_fields.extend(
                (
                    f"{product_context['exercise_style'].title()} exercise"
                    if product_context.get("exercise_style")
                    else None,
                    product_context.get("pricing_engine_label"),
                    (
                        "Implementation status: "
                        f"{product_context['implementation_status']}"
                        if product_context.get("implementation_status")
                        else None
                    ),
                    (
                        "Current listing evidence conditional"
                        if product_context.get("listing_evidence_status")
                        == "conditional"
                        else None
                    ),
                    (
                        "Current premium evidence conditional"
                        if product_context.get("premium_evidence_status")
                        == "conditional"
                        else None
                    ),
                )
            )
        product_detail = " · ".join(
            str(value) for value in product_detail_fields if value
        )
        output_cards.insert(
            0,
            html.Span(
                [
                    html.Span(
                        "Product",
                        className="pricer-calculation-meta-label",
                    ),
                    product_value,
                ],
                className="pricer-calculation-meta-item",
                title=product_detail,
            ),
        )
    unit_results = ""
    if is_delivery_strip:
        component_note = (
            "Premium contributions use equal 10,000 MMBtu monthly lots; "
            "monthly Delta and Vega are shown before strip weighting."
            if snapshot["context"].get("asset") == "JKM"
            else "Premium contributions are weighted by exact NBP delivery-day therms; "
            "monthly Delta and Vega are shown before strip weighting."
            if snapshot["context"].get("asset") == "NBP"
            else "Premium contributions use equal monthly contract lots; "
            "monthly Delta and Vega are shown before strip weighting."
            if snapshot["context"].get("component_weight_basis")
            == "equal_contract_lots"
            else "Premium contributions are weighted by TTF delivery hours; "
            "monthly Delta and Vega are shown before strip weighting."
        )
        unit_results = html.Details(
            [
                html.Summary(
                    "Monthly strip components",
                    className=(
                        "pricer-result-subsection-title "
                        "pricer-strip-details-summary"
                    ),
                ),
                html.Div(
                    [
                        html.P(
                            component_note,
                            className="pricer-result-subsection-note",
                        ),
                        _build_strip_component_grid(snapshot, structure_id),
                    ],
                    className="pricer-strip-details-content",
                ),
            ],
            className="pricer-strip-details",
        )
    return (
        output_cards,
        unit_results,
        "",
        "",
        "",
        warning_children,
    )


@callback(
    Output("pricer-calculations-session-store", "data"),
    [
        Input("pricer-workspace-store", "data"),
        Input(
            {"type": "pricer-calculation-store", "structure_id": ALL}, "data"
        ),
    ],
    [
        State({"type": "pricer-calculation-store", "structure_id": ALL}, "id"),
        State(
            {"type": "pricer-calculation-status", "structure_id": ALL},
            "children",
        ),
        State(
            {"type": "pricer-calculation-status", "structure_id": ALL},
            "id",
        ),
        State("pricer-calculations-session-store", "data"),
    ],
)
def persist_pricer_calculations(
    workspace,
    snapshots,
    snapshot_ids,
    calculation_statuses,
    calculation_status_ids,
    persisted_calculations,
):
    workspace = _normalize_workspace(workspace)
    valid_ids = {
        structure["structure_id"] for structure in workspace["structures"]
    }
    existing = (
        copy.deepcopy(persisted_calculations)
        if isinstance(persisted_calculations, dict)
        else {}
    )
    updated = {
        structure_id: snapshot
        for structure_id, snapshot in existing.items()
        if structure_id in valid_ids
        and _is_valid_calculation_snapshot(snapshot)
    }
    if not any(isinstance(snapshot, dict) for snapshot in (snapshots or [])) and not any(
        calculation_statuses or []
    ):
        return no_update if updated == existing else updated
    for snapshot, component_id in zip(
        snapshots or [],
        snapshot_ids or [],
    ):
        if not isinstance(component_id, dict):
            continue
        structure_id = component_id.get("structure_id")
        if structure_id not in valid_ids:
            continue
        status = _state_for_structure(
            calculation_statuses,
            calculation_status_ids,
            structure_id,
            None,
        )
        if _is_valid_calculation_snapshot(snapshot):
            updated[structure_id] = snapshot
        elif status or snapshot is not None:
            updated.pop(structure_id, None)
    return no_update if updated == existing else updated


@callback(
    Output("pricer-surface-comparison-grid", "children"),
    [
        Input("pricer-workspace-store", "data"),
        Input("pricer-calculations-session-store", "data"),
        Input("refresh-options-data", "n_clicks"),
    ],
    [State("url", "pathname")],
)
def render_surface_comparison_cards(
    workspace,
    persisted_calculations,
    _refresh_clicks=None,
    pathname=None,
):
    if pathname == "/pricer":
        return no_update
    structures = _calculated_surface_structures(
        workspace,
        persisted_calculations,
    )
    if not structures:
        return _build_pricer_message(
            "Calculate a structure to compare its contract vols with the surface."
        )
    views = build_surface_comparison_views(
        structures,
        force_refresh=_get_pricer_triggered_id() == "refresh-options-data",
    )
    if not views:
        return _build_pricer_message(
            "No valid calculated pricing contexts are available.",
            tone="warning",
        )
    return [_surface_comparison_card(view) for view in views]


@callback(
    [
        Output("pricer-analysis-structure-select", "options"),
        Output("pricer-analysis-structure-select", "value"),
    ],
    [
        Input("pricer-workspace-store", "data"),
        Input("pricer-workspace-ready-store", "data"),
    ],
    [
        State("pricer-analysis-structure-select", "value"),
        State("pricer-analysis-selection-store", "data"),
    ],
)
def sync_analysis_structure_selector(
    workspace,
    workspace_ready=True,
    selected_structure_id=None,
    persisted_selection=None,
):
    if not workspace_ready:
        return no_update, no_update
    workspace = _normalize_workspace(workspace)
    options = [
        {
            "label": structure["label"],
            "value": structure["structure_id"],
        }
        for structure in workspace["structures"]
    ]
    valid_ids = {option["value"] for option in options}
    selected = (
        persisted_selection
        if persisted_selection in valid_ids
        else selected_structure_id
        if selected_structure_id in valid_ids
        else options[0]["value"]
    )
    return options, selected


@callback(
    Output("pricer-analysis-selection-store", "data"),
    Input("pricer-analysis-structure-select", "value"),
    prevent_initial_call=True,
)
def persist_analysis_structure_selection(selected_structure_id):
    return selected_structure_id or no_update


@callback(
    Output(
        {"type": "pricer-remove-structure", "structure_id": ALL}, "disabled"
    ),
    [
        Input("pricer-workspace-store", "data"),
        Input("pricer-exchange-workspace-store", "data"),
    ],
    State({"type": "pricer-remove-structure", "structure_id": ALL}, "id"),
)
def sync_remove_structure_buttons(
    workspace,
    exchange_workspace,
    remove_button_ids,
):
    workspace = _normalize_workspace(workspace)
    exchange_structures = (
        exchange_workspace.get("structures", [])
        if isinstance(exchange_workspace, dict)
        else []
    )
    otc_disabled = len(workspace["structures"]) <= 1
    exchange_disabled = len(exchange_structures) <= 1
    return [
        exchange_disabled
        if str((_component_id or {}).get("structure_id", "")).startswith(
            "exchange-"
        )
        else otc_disabled
        for _component_id in (remove_button_ids or [])
    ]


@callback(
    Output("pricer-calculation-store", "data"),
    [
        Input("pricer-analysis-structure-select", "value"),
        Input(
            {"type": "pricer-calculation-store", "structure_id": ALL}, "data"
        ),
    ],
    [
        State({"type": "pricer-calculation-store", "structure_id": ALL}, "id"),
        State("pricer-calculation-store", "data"),
    ],
)
def route_selected_structure_calculation(
    selected_structure_id,
    calculation_snapshots,
    calculation_store_ids,
    current_routed_snapshot=None,
):
    selected_snapshot = _state_for_structure(
        calculation_snapshots,
        calculation_store_ids,
        selected_structure_id,
        None,
    )
    return (
        no_update
        if selected_snapshot == current_routed_snapshot
        else selected_snapshot
    )


@callback(
    Output("pricer-workspace-status", "children"),
    [
        Input("pricer-workspace-store", "data"),
        Input("pricer-calculations-session-store", "data"),
    ],
)
def render_pricer_workspace_status(
    workspace,
    persisted_calculations,
    snapshot_ids=None,
):
    workspace = _normalize_workspace(workspace)
    structure_ids = {
        structure["structure_id"] for structure in workspace["structures"]
    }
    if snapshot_ids is not None:
        persisted_calculations = {
            component_id.get("structure_id"): snapshot
            for snapshot, component_id in zip(
                persisted_calculations or [], snapshot_ids or []
            )
            if isinstance(component_id, dict)
        }
    elif not isinstance(persisted_calculations, dict):
        persisted_calculations = {}
    calculated = sum(
        1
        for structure_id, snapshot in persisted_calculations.items()
        if structure_id in structure_ids
        and isinstance(snapshot, dict)
        and snapshot.get("schema_version") == SCHEMA_VERSION
    )
    total = len(structure_ids)
    structure_label = "structure" if total == 1 else "structures"
    return f"{total} {structure_label} · {calculated} calculated"


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
                name=series["payoff_label"],
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
        "premium_convention": "upfront",
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
        "premium_convention": "upfront",
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
