"""Pricer calculation orchestration and result rendering callbacks."""

from __future__ import annotations

import copy
import math

from dash import (
    ALL,
    Input,
    MATCH,
    Output,
    State,
    callback,
    clientside_callback,
    html,
    no_update,
)
from datetime import date
from pricer_exchange_registry import (
    DEFAULT_EXCHANGE_MAPPING_ID,
    exchange_mapping_capture_message,
    exchange_mapping_pricing_supported,
)
from pricer_structure import (
    StructureValidationError,
    calculate_structure,
)
from pricer_surface_reference import (
    REFERENCE_SCHEMA_VERSION,
    build_published_surface_reference,
)
from . import callback_context
from .constants import (
    DEFAULT_STRUCTURE_ID,
    FUTURES_STYLE_RATE_NOTE,
)
from .controls import _build_pricer_message
from .grids import (
    _build_strip_component_grid,
    _leg_grid_options,
)
from .pricing import (
    _calculation_input_signature,
    _published_surface_calculation_rows,
)
from .state import (
    _context_from_states,
    _input_signature_from_context,
    _is_valid_calculation_snapshot,
    _nonnegative_click_count,
    _quote_ready_rows,
    _rows_with_committed_leg_edit,
    _surface_reference_input_signature,
    parse_date,
)


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
    cell_value_changed,
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
    normalized_rows = _quote_ready_rows(
        model, _rows_with_committed_leg_edit(rows, cell_value_changed)
    )
    payload = build_published_surface_reference(
        asset,
        model,
        context,
        normalized_rows,
        parse_date(valuation_date_value, date.today()),
        force_refresh=callback_context._get_pricer_triggered_id() == "refresh-options-data",
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
        else callback_context._get_pricer_triggered_id()
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
        if model == "kirk":
            surface_rows = {
                row["leg_id"]: row for row in surface_signature["rows"]
            }
            inconsistent_leg = next(
                (
                    index
                    for index, leg in enumerate(snapshot["legs"], start=1)
                    if any(
                        not math.isclose(
                            leg[f"volatility_asset_{asset_number}_used"],
                            float(
                                surface_rows.get(leg["leg_id"], {}).get(
                                    f"surface_pricing_volatility_asset_{asset_number}",
                                    math.nan,
                                )
                            ),
                            rel_tol=1e-10,
                            abs_tol=1e-12,
                        )
                        for asset_number in (1, 2)
                    )
                ),
                None,
            )
        else:
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
        for warning in surface_signature.get("warnings") or []:
            governed_warning = f"Governed surface: {warning}"
            if governed_warning not in snapshot["warnings"]:
                snapshot["warnings"].append(governed_warning)
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
    rows = _rows_with_committed_leg_edit(rows, cell_value_changed)
    triggered = callback_context._get_pricer_triggered_id()
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
    triggered_properties = callback_context._get_pricer_triggered_properties()
    committed_grid_edit = (
        triggered_type == "pricer-legs-grid"
        and (
            triggered_properties is None
            or "cellValueChanged" in triggered_properties
        )
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
        return "", "", ""
    if not _is_valid_calculation_snapshot(snapshot):
        return (
            "",
            _build_pricer_message(
                "Stored calculation is stale. Calculate the structure again.",
                tone="warning",
            ),
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
    return output_cards, unit_results, warning_children
