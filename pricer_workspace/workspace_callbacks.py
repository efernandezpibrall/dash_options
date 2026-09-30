"""Pricer exchange and OTC workspace lifecycle callbacks."""

from __future__ import annotations

import copy

from copy import deepcopy
from dash import (
    ALL,
    Input,
    Output,
    Patch,
    State,
    callback,
    ctx,
    no_update,
)
from dash.exceptions import PreventUpdate
from datetime import date
from pricer_structure import SCHEMA_VERSION
from . import (
    callback_context,
    state,
)
from .components import (
    _build_exchange_panel,
    _build_structure_panel,
)
from .state import (
    _capture_structure_template,
    _is_valid_calculation_snapshot,
    _normalize_workspace,
    _reduce_workspace,
    _state_for_structure,
    parse_date,
)


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
    triggered = callback_context._get_pricer_triggered_id()
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
        Output("pricer-exchange-workspace-store", "data"),
        Output("pricer-exchange-structures-container", "children"),
    ],
    [
        Input(
            "pricer-exchange-workspace-hydration",
            "n_intervals",
            allow_optional=True,
        ),
        Input(
            "pricer-exchange-add-structure",
            "n_clicks",
            allow_optional=True,
        ),
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
        State("pricer-exchange-workspace-store", "data"),
        State({"type": "pricer-mapping-id", "structure_id": ALL}, "value"),
        State({"type": "pricer-mapping-id", "structure_id": ALL}, "id"),
        State({"type": "pricer-asset", "structure_id": ALL}, "value"),
        State({"type": "pricer-asset", "structure_id": ALL}, "id"),
        State(
            {"type": "pricer-option-type", "structure_id": ALL}, "value"
        ),
        State({"type": "pricer-option-type", "structure_id": ALL}, "id"),
        State(
            {"type": "pricer-contract-multiplier", "structure_id": ALL},
            "value",
        ),
        State(
            {"type": "pricer-contract-multiplier", "structure_id": ALL},
            "id",
        ),
        State(
            {"type": "pricer-valuation-date", "structure_id": ALL}, "date"
        ),
        State(
            {"type": "pricer-valuation-date", "structure_id": ALL}, "id"
        ),
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
        State("pricer-exchange-calculate-all", "n_clicks"),
        State("pricer-global-valuation-date", "date"),
    ],
)
def manage_exchange_workspace(
    _hydration,
    _add_clicks,
    _duplicate_clicks,
    _remove_clicks,
    _autosave_tick,
    workspace,
    mapping_values,
    mapping_ids,
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
    calculate_all_clicks=None,
    global_valuation_date=None,
):
    if _hydration is None and _add_clicks is None:
        raise PreventUpdate
    workspace = state._normalize_exchange_workspace(workspace)
    valuation_date = state.parse_date(
        global_valuation_date,
        date.today(),
    ).isoformat()
    triggered = ctx.triggered_id

    def capture_template(structure_id):
        return state._capture_structure_template(
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
            mapping_values=mapping_values,
            mapping_ids=mapping_ids,
        )

    def build_panel(structure, can_remove):
        structure_id = structure["structure_id"]
        return _build_exchange_panel(
            {
                **structure,
                "template": workspace["drafts"].get(
                    structure_id,
                    structure.get("template"),
                ),
            },
            can_remove=can_remove,
            calculate_all_baseline=calculate_all_clicks,
            valuation_date=valuation_date,
        )

    if not isinstance(triggered, dict):
        if triggered == "pricer-draft-autosave-trigger":
            if not _hydration:
                return no_update, no_update
            updated = deepcopy(workspace)
            drafts = deepcopy(workspace["drafts"])
            for structure in workspace["structures"]:
                structure_id = structure["structure_id"]
                if state._state_for_structure(
                    model_values,
                    model_ids,
                    structure_id,
                    None,
                ) is None:
                    continue
                drafts[structure_id] = capture_template(structure_id)
            if drafts == workspace["drafts"]:
                return no_update, no_update
            updated["drafts"] = drafts
            return updated, no_update
        if triggered == "pricer-exchange-add-structure":
            updated = state._reduce_exchange_workspace(workspace, "add")
            patch = Patch()
            new_structure = updated["structures"][-1]
            patch.append(
                _build_exchange_panel(
                    new_structure,
                    can_remove=True,
                    calculate_all_baseline=calculate_all_clicks,
                    valuation_date=valuation_date,
                )
            )
            return updated, patch
        panels = [
            build_panel(
                structure,
                can_remove=len(workspace["structures"]) > 1,
            )
            for structure in workspace["structures"]
        ]
        return workspace, panels

    action_type = triggered.get("type")
    structure_id = triggered.get("structure_id")
    if not str(structure_id or "").startswith("exchange-structure-"):
        return no_update, no_update
    try:
        triggered_clicks = ctx.triggered[0].get("value")
    except Exception:
        triggered_clicks = None
    if not triggered_clicks:
        return no_update, no_update
    if action_type == "pricer-duplicate-structure":
        template = capture_template(structure_id)
        updated = state._reduce_exchange_workspace(
            workspace,
            "duplicate",
            structure_id,
            template,
        )
        patch = Patch()
        patch.append(
            _build_exchange_panel(
                updated["structures"][-1],
                can_remove=True,
                calculate_all_baseline=calculate_all_clicks,
                valuation_date=valuation_date,
            )
        )
        return updated, patch
    if (
        action_type == "pricer-remove-structure"
        and len(workspace["structures"]) > 1
    ):
        remove_index = next(
            (
                index
                for index, structure in enumerate(workspace["structures"])
                if structure["structure_id"] == structure_id
            ),
            None,
        )
        if remove_index is None:
            return no_update, no_update
        updated = state._reduce_exchange_workspace(
            workspace,
            "remove",
            structure_id,
        )
        patch = Patch()
        del patch[remove_index]
        return updated, patch
    return no_update, no_update


@callback(
    Output("pricer-exchange-workspace-status", "children"),
    [
        Input("pricer-exchange-workspace-store", "data"),
        Input(
            {"type": "pricer-calculation-store", "structure_id": ALL},
            "data",
        ),
    ],
    State(
        {"type": "pricer-calculation-store", "structure_id": ALL},
        "id",
    ),
)
def render_exchange_workspace_status(
    workspace,
    calculation_snapshots,
    calculation_store_ids,
):
    workspace = state._normalize_exchange_workspace(workspace)
    calculated = sum(
        state._is_valid_calculation_snapshot(
            state._state_for_structure(
                calculation_snapshots,
                calculation_store_ids,
                structure["structure_id"],
                None,
            )
        )
        for structure in workspace["structures"]
    )
    structure_count = len(workspace["structures"])
    structure_label = "structure" if structure_count == 1 else "structures"
    return f"{structure_count} {structure_label} · {calculated} calculated"
