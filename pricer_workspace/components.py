"""Pricer page and workspace layout assembly."""

from __future__ import annotations

import copy

from dash import (
    dcc,
    html,
)
from datetime import date
from pricer_exchange_registry import (
    DEFAULT_EXCHANGE_MAPPING_ID,
    exchange_mapping_for_asset_model,
    exchange_option_mapping,
)
from pricer_structure import (
    DEFAULT_ASSET,
    MODEL_LABELS,
    SCHEMA_VERSION,
    SUPPORTED_ASSETS,
)
from .charts import _build_pricer_chart_card
from .constants import (
    DEFAULT_STRUCTURE_ID,
    EXCHANGE_WORKFLOW,
)
from .controls import (
    _build_asset_field,
    _build_context_form,
    _build_mapping_id_field,
    _build_price_unit_field,
    _build_pricer_date_picker,
    _build_pricer_field,
    _build_pricer_message,
    _build_pricer_number_input,
    _build_pricer_section_header,
    _build_pricing_model_field,
    _build_structure_header_context,
    _contract_size_hint,
)
from .grids import (
    _build_legs_grid,
    _leg_grid_options,
)
from .state import (
    _INITIAL_WORKSPACE,
    _coerce_pricer_float,
    _default_leg_for_lot_mode,
    _instance_id,
    _instance_persistence,
    _is_valid_calculation_snapshot,
    _migrated_kirk_context_values,
    _nonnegative_click_count,
    _quote_ready_rows,
    _resolved_contract_size_default,
    _resolved_mapping_contract_size_default,
    _rows_with_volatility_adjustments,
    _snapshot_matches_template,
    parse_date,
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
                id=_instance_id("pricer-grid-refresh-ack", structure_id),
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


def build_workspace_stores():
    """Build session state consumed by the current Exchange/OTC workspace."""
    return [
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
    ]


def build_workspace_actions():
    """Build the OTC workspace controls with their existing callback IDs."""
    return html.Div(
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
    )


def build_detailed_analysis():
    """Build the OTC analysis controls and charts without a duplicate page."""
    return html.Section(
        [
            _build_pricer_section_header(
                "Detailed analysis",
                heading_level=3,
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
    )


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
    return _build_structure_panel(
        structure,
        can_remove=can_remove,
        calculate_all_baseline=calculate_all_baseline,
        signed_lots=True,
        use_published_surface=True,
        valuation_date_override=valuation_date or date.today().isoformat(),
        workflow=EXCHANGE_WORKFLOW,
        heading_level=3,
    )
