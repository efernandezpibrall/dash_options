"""Current Pricer page with stacked exchange and OTC pricing sections."""

from copy import deepcopy
from datetime import date

from dash import (
    ALL,
    MATCH,
    Input,
    Output,
    Patch,
    State,
    callback,
    ctx,
    dcc,
    html,
    no_update,
)

from pages import pricer as pricer_old
from pricer_exchange_registry import (
    exchange_mapping_options,
    exchange_option_mapping,
)


EXCHANGE_WORKFLOW = "exchange"
OTC_WORKFLOW = "otc"
LEGACY_WORKFLOW = "legacy"
EXCHANGE_STRUCTURE_ID = "exchange-structure-1"
EXCHANGE_WORKSPACE_SCHEMA_VERSION = 2
EXCHANGE_MAPPING_OPTIONS = tuple(exchange_mapping_options())
EXCHANGE_MODEL_OPTIONS = {
    "TTF": ({"label": "ICE TTF option", "value": "black76"},),
    "JKM": (
        {"label": "JKM average price option", "value": "asian76"},
        {"label": "JKM vanilla option", "value": "black76"},
    ),
    "HH": ({"label": "ICE Henry Hub PHE option", "value": "black76"},),
    "Brent": ({"label": "ICE Brent option", "value": "black76"},),
    "NBP": ({"label": "ICE NBP option", "value": "black76"},),
}


def _default_exchange_workspace():
    return {
        "schema_version": EXCHANGE_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": 2,
        "drafts": {},
        "structures": [
            {
                "structure_id": EXCHANGE_STRUCTURE_ID,
                "label": "E1",
                "template": None,
            }
        ],
    }


def _exchange_structure_label(structure_id, fallback_sequence=1):
    prefix, separator, suffix = str(structure_id or "").rpartition("-")
    if separator and prefix == "exchange-structure" and suffix.isdigit():
        return f"E{int(suffix)}"
    return f"E{fallback_sequence}"


def _normalize_exchange_workspace(workspace):
    if not isinstance(workspace, dict):
        return _default_exchange_workspace()
    raw_structures = workspace.get("structures")
    if not isinstance(raw_structures, list):
        return _default_exchange_workspace()
    try:
        migrate_governed_mapping = (
            int(workspace.get("schema_version", 0))
            < EXCHANGE_WORKSPACE_SCHEMA_VERSION
        )
    except (TypeError, ValueError, OverflowError):
        migrate_governed_mapping = True
    structures = []
    seen = set()
    for index, raw_structure in enumerate(raw_structures, start=1):
        if not isinstance(raw_structure, dict):
            continue
        structure_id = str(raw_structure.get("structure_id") or "").strip()
        if (
            not structure_id.startswith("exchange-structure-")
            or structure_id in seen
        ):
            continue
        seen.add(structure_id)
        template = (
            deepcopy(raw_structure.get("template"))
            if isinstance(raw_structure.get("template"), dict)
            else None
        )
        if template is not None:
            pricer_old._migrate_template_premium_convention(
                template,
                migrate_governed_mapping=migrate_governed_mapping,
            )
        structures.append(
            {
                "structure_id": structure_id,
                "label": _exchange_structure_label(structure_id, index),
                "template": template,
            }
        )
    if not structures:
        return _default_exchange_workspace()
    numeric_sequences = []
    for structure in structures:
        suffix = structure["structure_id"].removeprefix(
            "exchange-structure-"
        )
        if suffix.isdigit():
            numeric_sequences.append(int(suffix))
    try:
        next_sequence = max(
            int(workspace.get("next_structure_sequence", 0)),
            max(numeric_sequences, default=0) + 1,
        )
    except (TypeError, ValueError, OverflowError):
        next_sequence = max(numeric_sequences, default=0) + 1
    drafts = {}
    for structure_id, template in (
        workspace.get("drafts", {}).items()
        if isinstance(workspace.get("drafts"), dict)
        else []
    ):
        if structure_id not in seen or not isinstance(template, dict):
            continue
        migrated_template = deepcopy(template)
        pricer_old._migrate_template_premium_convention(
            migrated_template,
            migrate_governed_mapping=migrate_governed_mapping,
        )
        drafts[structure_id] = migrated_template
    return {
        "schema_version": EXCHANGE_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": next_sequence,
        "drafts": drafts,
        "structures": structures,
    }


def _reduce_exchange_workspace(
    workspace,
    action,
    structure_id=None,
    template=None,
):
    normalized = _normalize_exchange_workspace(workspace)
    structures = deepcopy(normalized["structures"])
    drafts = deepcopy(normalized["drafts"])
    next_sequence = normalized["next_structure_sequence"]
    if action in {"add", "duplicate"}:
        new_id = f"exchange-structure-{next_sequence}"
        structures.append(
            {
                "structure_id": new_id,
                "label": _exchange_structure_label(new_id, next_sequence),
                "template": (
                    deepcopy(template) if action == "duplicate" else None
                ),
            }
        )
        if action == "duplicate" and isinstance(template, dict):
            drafts[new_id] = deepcopy(template)
        next_sequence += 1
    elif action == "remove" and len(structures) > 1:
        structures = [
            structure
            for structure in structures
            if structure["structure_id"] != structure_id
        ]
        drafts.pop(structure_id, None)
    return {
        "schema_version": EXCHANGE_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": next_sequence,
        "drafts": drafts,
        "structures": structures,
    }


def _walk(component):
    yield component
    children = getattr(component, "children", None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if child is not None:
            yield from _walk(child)


def _normalized_workflow(value):
    return (
        value
        if value in {EXCHANGE_WORKFLOW, OTC_WORKFLOW, LEGACY_WORKFLOW}
        else EXCHANGE_WORKFLOW
    )


def _workflow_model_options(workflow, asset, mapping_id=None):
    if _normalized_workflow(workflow) in {OTC_WORKFLOW, LEGACY_WORKFLOW}:
        return deepcopy(pricer_old.option_types)
    mapping = exchange_option_mapping(mapping_id)
    if mapping is None:
        return [
            dict(option)
            for option in EXCHANGE_MODEL_OPTIONS.get(
                asset,
                EXCHANGE_MODEL_OPTIONS[pricer_old.DEFAULT_ASSET],
            )
        ]
    return [{"label": mapping.product, "value": mapping.model}]


def _workflow_model_style(workflow, asset):
    if _normalized_workflow(workflow) in {
        OTC_WORKFLOW,
        LEGACY_WORKFLOW,
    } or asset == "JKM":
        return {"display": "flex"}
    return {"display": "none"}


def _workflow_rate_style(workflow, asset):
    if _normalized_workflow(workflow) in {
        OTC_WORKFLOW,
        LEGACY_WORKFLOW,
    } or asset == "HH":
        return {"display": "flex"}
    return {"display": "none"}


def _workflow_model_value(
    workflow,
    asset,
    current_model,
    triggered_id=None,
    mapping_id=None,
):
    if _normalized_workflow(workflow) in {OTC_WORKFLOW, LEGACY_WORKFLOW}:
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
        selected = pricer_old.default_model_for_asset(asset)
    return no_update if current_model == selected else selected


def _component_with_id(root, component_id):
    matches = [
        component
        for component in _walk(root)
        if getattr(component, "id", None) == component_id
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Pricer layout must contain exactly one {component_id!r}"
        )
    return matches[0]


def _component_with_class(root, class_name):
    matches = [
        component
        for component in _walk(root)
        if class_name in str(getattr(component, "className", "")).split()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Pricer layout must contain exactly one .{class_name}"
        )
    return matches[0]


def _build_workflow_header(
    title,
    title_id,
    *,
    actions=None,
):
    return html.Div(
        [
            html.Div(
                [
                    html.H2(
                        title,
                        id=title_id,
                        className="pricer-workflow-section-title",
                    ),
                ],
                className="pricer-workflow-section-copy",
            ),
            html.Div(
                actions or [],
                className="pricer-workflow-section-actions",
            ),
        ],
        className="pricer-workflow-section-header",
    )


def _build_exchange_panel(
    structure,
    *,
    can_remove,
    calculate_all_baseline=0,
    valuation_date=None,
):
    return pricer_old._build_structure_panel(
        structure,
        can_remove=can_remove,
        calculate_all_baseline=calculate_all_baseline,
        signed_lots=True,
        use_published_surface=True,
        valuation_date_override=valuation_date or date.today().isoformat(),
        workflow=EXCHANGE_WORKFLOW,
        heading_level=3,
    )


# Keep a distinct component tree while sharing Pricer Old's registered callbacks.
_legacy_layout = deepcopy(pricer_old.layout)
_workspace_toolbar = _component_with_class(
    _legacy_layout, "pricer-workspace-toolbar"
)
_workspace_actions = _component_with_class(
    _workspace_toolbar, "pricer-workspace-actions"
)
_structures_container = _component_with_id(
    _legacy_layout, "pricer-structures-container"
)
_surface_section = _component_with_class(
    _legacy_layout, "pricer-surface-comparison-section"
)
_surface_grid = _component_with_id(
    _surface_section, "pricer-surface-comparison-grid"
)
_surface_grid.style = {"display": "none"}
_detailed_analysis = _component_with_class(
    _legacy_layout, "pricer-detailed-analysis-section"
)

_detail_header = _detailed_analysis.children[0]
_detail_heading = _detail_header.children[0]
_detail_header.children[0] = html.H3(
    _detail_heading.children,
    className=_detail_heading.className,
)

_fixed_components = [
    component
    for component in _legacy_layout.children
    if getattr(component, "id", None)
    in {
        "pricer-workspace-store",
        "pricer-workspace-hydration",
        "pricer-workspace-ready-store",
        "pricer-calculations-session-store",
        "pricer-draft-autosave-trigger",
        "pricer-analysis-selection-store",
        "pricer-calculation-store",
    }
]

_initial_exchange_workspace = _default_exchange_workspace()
_exchange_structures_container = html.Div(
    [
        _build_exchange_panel(
            _initial_exchange_workspace["structures"][0],
            can_remove=False,
        )
    ],
    id="pricer-exchange-structures-container",
    className="pricer-structure-list pricer-exchange-structure-list",
)
_exchange_workspace_actions = html.Div(
    [
        html.Div(
            "1 structure · 0 calculated",
            id="pricer-exchange-workspace-status",
            className="pricer-workspace-status",
            role="status",
        ),
        html.Button(
            "Calculate all",
            id="pricer-exchange-calculate-all",
            className="custom-export-btn pricer-calculate-button",
        ),
        html.Button(
            "Add structure",
            id="pricer-exchange-add-structure",
            className="custom-export-btn pricer-secondary-button",
        ),
    ],
    className="pricer-workspace-actions",
)
_structures_container.children = [
    pricer_old._build_structure_panel(
        pricer_old._INITIAL_WORKSPACE["structures"][0],
        can_remove=False,
        signed_lots=True,
        use_published_surface=True,
        valuation_date_override=date.today().isoformat(),
        workflow=OTC_WORKFLOW,
        heading_level=3,
    )
]

_exchange_section = html.Section(
    [
        _build_workflow_header(
            "Exchange Traded Options",
            "pricer-exchange-section-title",
            actions=[_exchange_workspace_actions],
        ),
        html.Div(
            [_exchange_structures_container],
            className=(
                "pricer-workflow-section-body "
                "pricer-exchange-section-body"
            ),
        ),
    ],
    className="pricer-workflow-section pricer-exchange-section",
    **{"aria-labelledby": "pricer-exchange-section-title"},
)

_otc_section = html.Section(
    [
        _build_workflow_header(
            "OTC Structured Options",
            "pricer-otc-section-title",
            actions=[_workspace_actions],
        ),
        html.Div(
            [_structures_container, _detailed_analysis],
            className="pricer-workflow-section-body pricer-otc-section-body",
        ),
    ],
    className="pricer-workflow-section pricer-otc-section",
    **{"aria-labelledby": "pricer-otc-section-title"},
)

layout = html.Main(
    [
        *_fixed_components,
        dcc.Store(
            id="pricer-exchange-workspace-store",
            data=_initial_exchange_workspace,
            storage_type="session",
        ),
        dcc.Interval(
            id="pricer-exchange-workspace-hydration",
            interval=1000,
            max_intervals=1,
            n_intervals=0,
        ),
        _surface_grid,
        html.H1("Pricer", className="pricer-visually-hidden-title"),
        _exchange_section,
        _otc_section,
    ],
    id="pricer-current-page",
    className="options-dashboard-container pricer-page pricer-page-current",
)


@callback(
    [
        Output("pricer-exchange-workspace-store", "data"),
        Output("pricer-exchange-structures-container", "children"),
    ],
    [
        Input("pricer-exchange-workspace-hydration", "n_intervals"),
        Input("pricer-exchange-add-structure", "n_clicks"),
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
    workspace = _normalize_exchange_workspace(workspace)
    valuation_date = pricer_old.parse_date(
        global_valuation_date,
        date.today(),
    ).isoformat()
    triggered = ctx.triggered_id

    def capture_template(structure_id):
        return pricer_old._capture_structure_template(
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
                if pricer_old._state_for_structure(
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
            updated = _reduce_exchange_workspace(workspace, "add")
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
        updated = _reduce_exchange_workspace(
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
        updated = _reduce_exchange_workspace(
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
    workspace = _normalize_exchange_workspace(workspace)
    calculated = sum(
        pricer_old._is_valid_calculation_snapshot(
            pricer_old._state_for_structure(
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
        Output(
            {"type": "pricer-rate-field", "structure_id": MATCH},
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
        _workflow_rate_style(workflow, effective_asset),
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
def select_exchange_mapping_asset(workflow, mapping_id, current_asset):
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
