"""Pricer identity, delivery, model and surface-reference input callbacks."""

from __future__ import annotations

import copy
import math

from copy import deepcopy
from dash import (
    ALL,
    Input,
    MATCH,
    Output,
    State,
    callback,
    clientside_callback,
    ctx,
    no_update,
)
from datetime import date
from pricer_exchange_registry import exchange_option_mapping
from pricer_structure import (
    DEFAULT_ASSET,
    MAX_LEGS,
    MODEL_LABELS,
    SUPPORTED_ASSETS,
    StructureValidationError,
    asset_price_spec,
    build_delivery_month_component,
    default_model_for_asset,
    default_premium_convention,
)
from . import (
    callback_context,
    constants,
)
from .constants import (
    DEFAULT_STRUCTURE_ID,
    EXCHANGE_MODEL_OPTIONS,
    EXCHANGE_WORKFLOW,
    OTC_WORKFLOW,
    VOLATILITY_ADJUSTMENT_FIELDS,
)
from .controls import (
    _build_context_form,
    _build_structure_header_context,
    _delivery_year_field_style,
    _month_only_field_style,
    _surface_proxy_note,
)
from .grids import _leg_column_defs
from .state import (
    _coerce_pricer_float,
    _context_from_states,
    _default_leg_for_lot_mode,
    _delivery_month_options,
    _migrated_kirk_context_values,
    _normalized_workflow,
    _quote_ready_rows,
    _resolved_contract_size_default,
    _resolved_delivery_month,
    _resolved_mapping_contract_size_default,
    _rows_with_volatility_adjustments,
    parse_date,
)


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
    triggered = callback_context._get_pricer_triggered_id()
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
    triggered = callback_context._get_pricer_triggered_id()
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
            added_row = _default_leg_for_lot_mode(
                model,
                next_sequence,
                signed_lots=signed_lots,
                use_published_surface=use_published_surface,
            )
            if use_published_surface and rows:
                for field in VOLATILITY_ADJUSTMENT_FIELDS:
                    added_row[field] = rows[0][field]
            rows.append(added_row)
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
    committed_edit = False
    if isinstance(latest_event, dict):
        column = latest_event.get("column")
        column_id = latest_event.get("colId") or (
            column.get("colId") if isinstance(column, dict) else None
        )
        edited_leg_id = (latest_event.get("data") or {}).get("leg_id")
        if (
            edited_leg_id
            and column_id not in (None, "leg_id")
            and "newValue" in latest_event
            and latest_event.get("oldValue") != latest_event["newValue"]
        ):
            shared_adjustment = (
                use_published_surface
                and column_id in VOLATILITY_ADJUSTMENT_FIELDS
            )
            for row in rows:
                if (
                    column_id in row
                    and (shared_adjustment or row.get("leg_id") == edited_leg_id)
                ):
                    # AG Grid may emit cellValueChanged before its rowData prop
                    # reaches this callback. Preserve the committed edit when
                    # returning rowData and persisting the draft.
                    committed_edit |= row[column_id] != latest_event["newValue"]
                    row[column_id] = latest_event["newValue"]
                    if not shared_adjustment:
                        break
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
        no_update
        if triggered_type == "pricer-legs-grid"
        and not (basis_changed or committed_edit)
        else rows
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
    triggered = callback_context._get_pricer_triggered_id()
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


clientside_callback(
    """async function (options, gridId) {
        if (!options || !gridId || !options.context) {
            return window.dash_clientside.no_update;
        }
        const id = JSON.stringify({
            structure_id: gridId.structure_id,
            type: 'pricer-legs-grid'
        });
        const api = await dash_ag_grid.getApiAsync(id);
        api.setGridOption('context', options.context);
        api.refreshCells({force: true});
        return window.dash_clientside.no_update;
    }""",
    Output(
        {"type": "pricer-grid-refresh-ack", "structure_id": MATCH},
        "data",
    ),
    Input(
        {"type": "pricer-legs-grid", "structure_id": MATCH},
        "dashGridOptions",
    ),
    State(
        {"type": "pricer-legs-grid", "structure_id": MATCH},
        "id",
    ),
)


def _workflow_model_options(workflow, asset, mapping_id=None):
    if _normalized_workflow(workflow) == OTC_WORKFLOW:
        return deepcopy(constants.option_types)
    mapping = exchange_option_mapping(mapping_id)
    if mapping is None:
        return [
            dict(option)
            for option in EXCHANGE_MODEL_OPTIONS.get(
                asset,
                EXCHANGE_MODEL_OPTIONS[DEFAULT_ASSET],
            )
        ]
    return [{"label": mapping.product, "value": mapping.model}]


def _workflow_model_style(workflow, asset):
    if _normalized_workflow(workflow) == OTC_WORKFLOW or asset == "JKM":
        return {"display": "flex"}
    return {"display": "none"}


def _workflow_model_value(
    workflow,
    asset,
    current_model,
    triggered_id=None,
    mapping_id=None,
):
    if _normalized_workflow(workflow) == OTC_WORKFLOW:
        return no_update
    mapping = exchange_option_mapping(mapping_id)
    if mapping is not None:
        return mapping.model
    else:
        allowed = {
            option["value"]
            for option in _workflow_model_options(workflow, asset)
        }
        asset_changed = (
            isinstance(triggered_id, dict)
            and triggered_id.get("type") == "pricer-asset"
        )
        if asset == "JKM" and current_model in allowed and not asset_changed:
            return no_update
        selected = default_model_for_asset(asset)
    return no_update if current_model == selected else selected


@callback(
    [
        Output(
            {"type": "pricer-option-type", "structure_id": MATCH},
            "options",
        ),
        Output(
            {"type": "pricer-model-field", "structure_id": MATCH},
            "style",
        ),
    ],
    [
        Input(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
    ],
)
def configure_pricer_workflow_controls(workflow, mapping_id, asset):
    mapping = exchange_option_mapping(mapping_id)
    effective_asset = mapping.asset if mapping is not None else asset
    return (
        _workflow_model_options(workflow, effective_asset, mapping_id),
        _workflow_model_style(workflow, effective_asset),
    )


@callback(
    [
        Output(
            {"type": "pricer-asset-field", "structure_id": MATCH},
            "style",
        ),
        Output(
            {"type": "pricer-price-unit-field", "structure_id": MATCH},
            "style",
        ),
    ],
    [
        Input(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
        Input(
            {"type": "pricer-option-type", "structure_id": MATCH},
            "value",
        ),
    ],
)
def configure_otc_asset_identity_controls(workflow, model):
    normalized = _normalized_workflow(workflow)
    if normalized == EXCHANGE_WORKFLOW:
        return {"display": "none"}, {"display": "flex"}
    style = {"display": "none"} if model == "kirk" else {"display": "flex"}
    return style, style


@callback(
    Output(
        {"type": "pricer-asset", "structure_id": MATCH},
        "value",
        allow_duplicate=True,
    ),
    [
        Input(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
    ],
    State({"type": "pricer-asset", "structure_id": MATCH}, "value"),
    prevent_initial_call=True,
)
def select_workflow_exchange_mapping_asset(workflow, mapping_id, current_asset):
    if _normalized_workflow(workflow) != EXCHANGE_WORKFLOW:
        return no_update
    mapping = exchange_option_mapping(mapping_id)
    if mapping is None or mapping.asset == current_asset:
        return no_update
    return mapping.asset


@callback(
    Output(
        {"type": "pricer-option-type", "structure_id": MATCH},
        "value",
        allow_duplicate=True,
    ),
    [
        Input(
            {"type": "pricer-structure-workflow", "structure_id": MATCH},
            "data",
        ),
        Input({"type": "pricer-mapping-id", "structure_id": MATCH}, "value"),
        Input({"type": "pricer-asset", "structure_id": MATCH}, "value"),
    ],
    State(
        {"type": "pricer-option-type", "structure_id": MATCH},
        "value",
    ),
    prevent_initial_call=True,
)
def select_pricer_workflow_model(workflow, mapping_id, asset, current_model):
    return _workflow_model_value(
        workflow,
        asset,
        current_model,
        ctx.triggered_id,
        mapping_id,
    )
