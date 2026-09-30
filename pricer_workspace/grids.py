"""Pricer leg and strip grids, columns and display options."""

from __future__ import annotations

import copy
import dash_ag_grid as dag

from pricer_structure import (
    GREEK_FIELDS,
    GREEK_LABELS,
    SCHEMA_VERSION,
    SINGLE_ASSET_MODELS,
    default_leg,
)
from pricer_surface_reference import (
    REFERENCE_SCHEMA_VERSION,
    volatility_overlay_points,
)
from .constants import (
    DEFAULT_STRUCTURE_ID,
    MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT,
    VOLATILITY_ADJUSTMENT_FIELDS,
    _CURRENT_PRICER_COLUMN_WIDTHS,
    _CURRENT_PRICER_LONG_GREEK_WIDTHS,
)
from .state import (
    _combined_result_rows,
    _instance_id,
    _instance_persistence,
    _quote_ready_rows,
    _rows_with_volatility_adjustments,
)


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
    tooltip_field="surface_input_tooltip",
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
    column["tooltipValueGetter"] = _surface_tooltip_getter(tooltip_field)
    if tooltip:
        column["headerTooltip"] = tooltip
    return column


def _published_kirk_volatility_column(asset_number):
    return _published_pricer_volatility_column(
        f"surface_volatility_asset_{asset_number}",
        f"Asset {asset_number} surface vol",
        calculated_field=f"raw_volatility_asset_{asset_number}",
        width=118,
        tooltip=(
            f"Governed 50-call-delta volatility for Asset {asset_number} at its "
            "selected volatility-reference expiry."
        ),
        tooltip_field=f"surface_asset_{asset_number}_tooltip",
    )


def _published_pricer_volatility_columns():
    input_vol_column = _published_pricer_volatility_column(
        "surface_input_vol",
        "Input vol",
        width=82,
        tooltip=(
            "Original published strike-specific volatility before ATM, Skew, "
            "and Smile adjustments."
        ),
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
            "Published volatility plus the ATM, Skew, and Smile adjustments, "
            "then the governed contract-date adjustment."
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
    explanation = {
        "atm_vol_adjustment": (
            "ATM moves the whole published surface up or down. Enter +1 to add "
            "one volatility percentage point at every strike."
        ),
        "skew_vol_adjustment": (
            "Skew tilts the surface. Enter +1 to add 0.5 volatility points "
            "at the 25-delta call strike and remove 0.5 at the 25-delta put "
            "strike. The original ATM point stays the same. Negative values "
            "reverse the tilt."
        ),
        "smile_vol_adjustment": (
            "Smile moves both wings relative to the middle. Enter +1 to add "
            "one volatility point at both 25-delta wing strikes. The original "
            "ATM point stays the same. Negative values lower both wings."
        ),
    }[field]
    note = (
        f"{header} adjustment in volatility percentage points. {explanation} "
        "Editing one leg updates every leg in this structure. Input vol stays "
        "original; Pricing vol shows the adjustment."
    )
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
        "headerTooltip": note,
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
    """Apply the compact geometry used by the current Pricer."""
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

        if (
            column_id not in VOLATILITY_ADJUSTMENT_FIELDS
            and "tooltipValueGetter" not in column
        ):
            column["tooltipValueGetter"] = {
                "function": (
                    "params.valueFormatted != null && params.valueFormatted !== '' "
                    "? params.valueFormatted : (params.value == null ? '' : "
                    "String(params.value))"
                )
            }
    return column_defs


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
        if use_published_surface:
            columns.extend(
                [
                    _published_kirk_volatility_column(1),
                    _published_kirk_volatility_column(2),
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


def _build_strip_component_grid(snapshot, structure_id=DEFAULT_STRUCTURE_ID):
    context = snapshot["context"]
    published_surface = (snapshot.get("_ui_input_signature") or {}).get(
        "published_surface"
    ) or {}
    surface_rows = {
        row["leg_id"]: row
        for row in published_surface.get("rows") or []
        if isinstance(row, dict) and row.get("leg_id")
    }
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
        surface_row = surface_rows.get(leg["leg_id"], {})
        component_adjustments = {
            item["contract_month"]: 0.01 * volatility_overlay_points(
                surface_row, item["smile_coordinate"]
            )
            for item in surface_row.get("component_volatilities") or []
            if item.get("smile_coordinate") is not None
        }
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
                    "input_vol_pct": (
                        component["input_volatility"]
                        - component_adjustments.get(component["contract_month"], 0.0)
                    ) * 100.0,
                    "pricing_vol_pct": component.get(
                        "pricing_volatility", component["input_volatility"]
                    ) * 100.0,
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
                "pricing_vol_pct",
                "Pricing vol %",
                min_width=88,
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
