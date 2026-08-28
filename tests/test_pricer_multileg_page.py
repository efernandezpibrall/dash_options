import copy
import datetime as dt
import inspect
import json

import pytest
from dash import dcc, html, no_update

from pages import pricer


class FrozenDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 7, 29)


@pytest.fixture(autouse=True)
def freeze_pricer_date(monkeypatch):
    monkeypatch.setattr(pricer, "date", FrozenDate)
    monkeypatch.setattr(
        component_by_id("pricer-valuation-date"),
        "date",
        FrozenDate.today().isoformat(),
    )


def walk(component):
    yield component
    children = getattr(component, "children", None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if child is not None:
            yield from walk(child)


def component_by_id(component_id):
    exact = [
        item
        for item in walk(pricer.layout)
        if getattr(item, "id", None) == component_id
    ]
    if exact:
        return exact[0]
    return component_by_pattern(component_id)


def component_by_pattern(
    component_type,
    structure_id=pricer.DEFAULT_STRUCTURE_ID,
    root=None,
):
    return next(
        item
        for item in walk(root or pricer.layout)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == component_type
        and item.id.get("structure_id") == structure_id
    )


def canonical_id(component_id):
    if isinstance(component_id, dict):
        return json.dumps(component_id, sort_keys=True)
    return str(component_id)


def leaf_columns(column_defs):
    return [
        child
        for column in column_defs
        for child in (column.get("children") or [column])
    ]


def unified_grid_state(snapshot):
    return pricer._leg_grid_options(snapshot)


def unified_pricing_rows(snapshot):
    return unified_grid_state(snapshot)["context"]["pricingRows"]


def black_context_states(structure_id=pricer.DEFAULT_STRUCTURE_ID):
    params = ["futures_style", "MONTH", 100.0, 0.03]
    param_ids = [
        {
            "type": "pricer-context-param",
            "structure_id": structure_id,
            "model": "black76",
            "param": "premium_convention",
        },
        {
            "type": "pricer-context-param",
            "structure_id": structure_id,
            "model": "black76",
            "param": "delivery_shape",
        },
        {
            "type": "pricer-context-param",
            "structure_id": structure_id,
            "model": "black76",
            "param": "forward",
        },
        {
            "type": "pricer-context-param",
            "structure_id": structure_id,
            "model": "black76",
            "param": "rate",
        },
    ]
    dates = ["2026-10-29", "2027-01-29"]
    date_ids = [
        {
            "type": "pricer-context-date",
            "structure_id": structure_id,
            "model": "black76",
            "param": "expiration_date",
        },
        {
            "type": "pricer-context-date",
            "structure_id": structure_id,
            "model": "black76",
            "param": "contract_expiration_date",
        },
    ]
    return params, param_ids, dates, date_ids


def two_leg_rows():
    return [
        {
            "leg_id": "leg-1",
            "name": "95 call",
            "side": "BUY",
            "ratio": 1,
            "call_put": "C",
            "strike": 95,
            "volatility": 0.19,
        },
        {
            "leg_id": "leg-2",
            "name": "105 call",
            "side": "SELL",
            "ratio": 1,
            "call_put": "C",
            "strike": 105,
            "volatility": 0.24,
        },
    ]


def calculate_two_leg_snapshot(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "calculate-button")
    params, param_ids, dates, date_ids = black_context_states()
    rows = two_leg_rows()
    rows[0]["ratio"] = 2
    snapshot, status = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        100,
        rows,
        params,
        dates,
        "2026-07-29",
        param_ids,
        date_ids,
    )
    return snapshot, status


def calculate_instance(
    monkeypatch,
    structure_id,
    *,
    forward=100.0,
    rows=None,
    trigger="pricer-calculate-all",
    current_snapshot=None,
):
    params, param_ids, dates, date_ids = black_context_states(structure_id)
    params[2] = forward
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: trigger)
    return pricer.calculate_structure_instance(
        1,
        1,
        "TTF",
        "black76",
        1,
        rows if rows is not None else two_leg_rows()[:1],
        params,
        dates,
        "2026-07-29",
        param_ids,
        date_ids,
        current_snapshot,
    )


def model_instance_state(model, structure_id=pricer.DEFAULT_STRUCTURE_ID):
    context = copy.deepcopy(pricer.default_context(model, FrozenDate.today()))
    if model == "kirk":
        context["asset_1_code"] = "JKM"
        context["asset_2_code"] = "HH"
    asset = context.pop("asset", pricer.DEFAULT_ASSET)
    param_values = []
    param_ids = []
    date_values = []
    date_ids = []
    for param, value in context.items():
        component_id = {
            "type": (
                "pricer-context-date"
                if param.endswith(("_date", "_expiry"))
                else "pricer-context-param"
            ),
            "structure_id": structure_id,
            "model": model,
            "param": param,
        }
        if param.endswith(("_date", "_expiry")):
            date_values.append(value)
            date_ids.append(component_id)
        else:
            param_values.append(value)
            param_ids.append(component_id)
    return {
        "structure_id": structure_id,
        "asset": asset,
        "model": model,
        "contract_multiplier": 1,
        "rows": [pricer.default_leg(model, 1)],
        "param_values": param_values,
        "date_values": date_values,
        "valuation_date": FrozenDate.today().isoformat(),
        "param_ids": param_ids,
        "date_ids": date_ids,
    }


def invoke_instance_state(
    monkeypatch,
    state,
    *,
    trigger,
    current_snapshot=None,
    local_clicks=1,
    calculate_all_clicks=1,
):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: trigger)
    return pricer.calculate_structure_instance(
        local_clicks,
        calculate_all_clicks,
        state["asset"],
        state["model"],
        state["contract_multiplier"],
        state["rows"],
        state["param_values"],
        state["date_values"],
        state["valuation_date"],
        state["param_ids"],
        state["date_ids"],
        current_snapshot,
    )


def set_context_state_value(state, param, value, *, is_date=False):
    ids_key = "date_ids" if is_date else "param_ids"
    values_key = "date_values" if is_date else "param_values"
    index = next(
        index
        for index, component_id in enumerate(state[ids_key])
        if component_id["param"] == param
    )
    state[values_key][index] = value
    return state[ids_key][index]


def assert_output_is_masked(snapshot, structure_id=pricer.DEFAULT_STRUCTURE_ID):
    assert snapshot is None
    grid_state = unified_grid_state(snapshot)
    assert grid_state["context"]["pricingRows"] == {}
    assert grid_state["pinnedBottomRowData"] == []
    assert pricer.render_structure_results(
        snapshot,
        pricer._instance_id("pricer-calculation-store", structure_id),
    ) == ("", "", "", "", "", "")


def test_layout_has_semantic_heading_session_stores_and_editable_leg_grid():
    headings = [item for item in walk(pricer.layout) if isinstance(item, html.H1)]
    assert len(headings) == 1
    assert headings[0].children == "Pricer Old"
    assert not any(isinstance(item, html.P) for item in walk(pricer.layout))
    subsection_titles = [
        item.children
        for item in walk(pricer.layout)
        if isinstance(item, html.H3)
    ]
    assert "Market and sizing" not in subsection_titles
    assert "Leg analytics and position totals" not in subsection_titles
    assert "Shared market context" not in subsection_titles
    assert "Trade sizing" not in subsection_titles
    assert "Individual leg unit analytics" not in subsection_titles
    assert "Signed trade contributions and totals" not in subsection_titles
    assert "Pricing output" not in subsection_titles
    layout_text = " ".join(
        item for item in walk(pricer.layout) if isinstance(item, str)
    )
    assert "Option structure pricer" not in layout_text
    assert "Shared context" not in layout_text
    assert "Trade sizing" not in layout_text
    assert "Leg unit metrics are unweighted" not in layout_text
    assert "Settlement" not in layout_text

    workspace_store = component_by_id("pricer-workspace-store")
    calculations_session_store = component_by_id(
        "pricer-calculations-session-store"
    )
    calculation_store = component_by_pattern("pricer-calculation-store")
    draft_store = component_by_pattern("pricer-draft-store")
    premium_convention = next(
        item
        for item in walk(pricer.layout)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-context-param"
        and item.id.get("param") == "premium_convention"
    )
    asset = component_by_pattern("pricer-asset")
    pricing_model = component_by_pattern("pricer-option-type")
    valuation_date = component_by_pattern("pricer-valuation-date")
    context_row = next(
        item
        for item in walk(pricer.layout)
        if "pricer-market-strip" in str(getattr(item, "className", ""))
    )
    context_component_ids = [
        getattr(item, "id", None)
        for child in context_row.children
        for item in walk(child)
        if getattr(item, "id", None) is not None
        and not (
            isinstance(item.id, dict)
            and item.id.get("type") == "pricer-contract-multiplier-label"
        )
    ]
    leg_toolbar = next(
        item
        for item in walk(pricer.layout)
        if getattr(item, "className", None) == "pricer-leg-toolbar"
    )
    leg_heading = leg_toolbar.children[0]
    leg_edit_actions = next(
        item
        for item in walk(leg_heading)
        if getattr(item, "className", None) == "pricer-leg-edit-actions"
    )
    heading_ids = [
        getattr(item, "id", None)
        for item in walk(leg_heading)
        if getattr(item, "id", None) is not None
    ]
    edit_action_ids = [
        getattr(item, "id", None)
        for item in walk(leg_edit_actions)
        if getattr(item, "id", None) is not None
    ]
    grid = component_by_pattern("pricer-legs-grid")
    panel = next(
        item
        for item in walk(pricer.layout)
        if "pricer-structure-panel"
        in str(getattr(item, "className", "")).split()
    )
    structure_header = next(
        item
        for item in walk(panel)
        if getattr(item, "className", None) == "pricer-section-header"
    )
    panel_action_types = [
        item.id["type"]
        for item in walk(panel)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type")
        in {
            "pricer-calculation-status",
            "pricer-calculate-button",
            "pricer-duplicate-structure",
            "pricer-remove-structure",
        }
    ]
    panel_action_buttons = [
        item
        for item in walk(panel)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type")
        in {
            "pricer-calculate-button",
            "pricer-duplicate-structure",
            "pricer-remove-structure",
        }
    ]
    assert isinstance(workspace_store, dcc.Store)
    assert workspace_store.storage_type == "session"
    assert workspace_store.data == pricer._default_workspace()
    assert isinstance(calculation_store, dcc.Store)
    assert calculation_store.storage_type == "memory"
    assert draft_store.storage_type == "session"
    assert calculations_session_store.storage_type == "session"
    assert isinstance(premium_convention, dcc.Dropdown)
    assert premium_convention.value == "futures_style"
    assert premium_convention.clearable is False
    assert premium_convention.persistence == (
        "pricer-structure-1-black76-premium-convention-v2"
    )
    assert premium_convention.persistence_type == "session"
    assert [option["value"] for option in premium_convention.options] == [
        "futures_style",
        "upfront",
    ]
    assert [option["label"] for option in premium_convention.options] == [
        "Futures-style",
        "Upfront",
    ]
    assert "Product default" not in layout_text
    assert "Upfront premium" not in layout_text
    assert isinstance(asset, dcc.Dropdown)
    assert asset.value == "TTF"
    assert asset.clearable is False
    assert asset.persistence == "pricer-structure-1-asset"
    assert asset.persistence_type == "session"
    assert [option["value"] for option in asset.options] == [
        "TTF",
        "JKM",
        "HH",
        "Brent",
        "NBP",
    ]
    assert isinstance(pricing_model, dcc.Dropdown)
    header_controls = next(
        item
        for item in walk(panel)
        if getattr(item, "className", None) == "pricer-structure-header-controls"
    )
    header_control_types = [
        item.id["type"]
        for item in walk(header_controls)
        if isinstance(getattr(item, "id", None), dict)
            and item.id.get("type")
            not in {
                "pricer-header-context",
                "pricer-model-field",
                "pricer-asset-field",
                "pricer-price-unit-field",
                "pricer-contract-multiplier-label",
                "pricer-rate-field",
            }
    ]
    assert header_control_types[:5] == [
        "pricer-asset",
        "pricer-price-unit",
        "pricer-option-type",
        "pricer-context-param",
        "pricer-context-param",
    ]
    assert header_control_types[5:9] == [
        "pricer-delivery-month-field",
        "pricer-context-param",
        "pricer-valuation-date",
        "pricer-contract-multiplier",
    ]
    header_params = [
        item.id.get("param")
        for item in walk(header_controls)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-context-param"
    ]
    assert header_params[:2] == ["premium_convention", "delivery_shape"]
    assert header_params[2:] == [
        "delivery_month",
        "delivery_year",
        "forward",
        "rate",
    ]
    def field_label(item):
        label = item.children[0].children
        if isinstance(label, html.Span):
            return next(
                child.children
                for child in walk(label)
                if getattr(child, "className", None) == "pricer-field-label-otc"
            )
        return label

    header_labels = [
        field_label(item)
        for item in header_controls.children
        if isinstance(item, html.Label)
    ]
    header_labels.extend(
        field_label(item)
        for item in header_controls.children[3].children
        if isinstance(item, html.Label)
    )
    assert header_labels == [
        "Asset",
        "Model",
        "Premium",
        "Shape",
        "Delivery",
    ]
    assert [component_id["type"] for component_id in context_component_ids[:2]] == [
        "pricer-valuation-date",
        "pricer-contract-multiplier",
    ]
    assert all(
        component_id["structure_id"] == pricer.DEFAULT_STRUCTURE_ID
        for component_id in context_component_ids
        if isinstance(component_id, dict)
    )
    assert context_row.children[-1].id == {
        "type": "pricer-shared-context",
        "structure_id": pricer.DEFAULT_STRUCTURE_ID,
    }
    assert context_row.children[0].children[0].children == "Valuation"
    assert context_row.children[1].children[0].children.children == "Contract size"
    assert "exact delivery hours" in context_row.children[1].title
    price_unit = component_by_pattern("pricer-price-unit", root=header_controls)
    price_unit_field = next(
        item
        for item in header_controls.children
        if getattr(item, "className", None)
        == "pricer-field pricer-price-unit-field"
    )
    assert price_unit_field.children[0].children == "Price unit"
    assert price_unit.children == "EUR/MWh"
    assert price_unit.title == "Euro per megawatt-hour."
    assert context_row is header_controls.children[-1]
    assert structure_header.children[0].children == "S1"
    assert structure_header.children[1].children[0] is header_controls
    assert context_row in list(walk(structure_header))
    assert not any(
        getattr(item, "className", None) == "pricer-market-sizing-layout"
        for item in walk(pricer.layout)
    )
    config_body = next(
        item
        for item in walk(pricer.layout)
        if getattr(item, "className", None)
        == "pricer-section-body pricer-config-body pricer-structure-body"
    )
    assert context_row not in list(walk(config_body))
    assert config_body.children[1].id == {
        "type": "pricer-legs-grid",
        "structure_id": pricer.DEFAULT_STRUCTURE_ID,
    }
    assert config_body.children[2].id == {
        "type": "pricer-unit-results-container",
        "structure_id": pricer.DEFAULT_STRUCTURE_ID,
    }
    assert not any(
        getattr(item, "className", None)
        == "pricer-section pricer-output-section"
        for item in walk(pricer.layout)
    )
    assert [component_id["type"] for component_id in heading_ids] == [
        "pricer-add-leg",
        "pricer-duplicate-leg",
        "pricer-remove-leg",
    ]
    toolbar_status_types = [
        item.id["type"]
        for item in walk(leg_toolbar.children[1])
        if isinstance(getattr(item, "id", None), dict)
    ]
    assert toolbar_status_types == [
        "pricer-results-container",
        "pricer-warning-container",
        "pricer-leg-action-status",
    ]
    assert [component_id["type"] for component_id in edit_action_ids] == [
        "pricer-add-leg",
        "pricer-duplicate-leg",
        "pricer-remove-leg",
    ]
    assert panel_action_types == [
        "pricer-calculation-status",
        "pricer-calculate-button",
        "pricer-duplicate-structure",
        "pricer-remove-structure",
    ]
    assert [button.children for button in panel_action_buttons] == [
        "Calc",
        "Copy",
        "×",
    ]
    assert [button.title for button in panel_action_buttons] == [
        "Calculate structure",
        "Duplicate structure",
        "Remove structure",
    ]
    assert isinstance(valuation_date, dcc.DatePickerSingle)
    assert valuation_date.date == "2026-07-29"
    assert valuation_date.min_date_allowed is None
    assert valuation_date.max_date_allowed.isoformat() == (
        dt.date.today() + dt.timedelta(days=pricer.MAX_OPTION_HORIZON_DAYS)
    ).isoformat()
    assert valuation_date.persistence == "pricer-structure-1-valuation-date-v1"
    assert valuation_date.persistence_type == "session"
    assert grid.persistence_type == "session"
    assert grid.persisted_props == ["rowData"]
    assert grid.rowData[0]["leg_id"] == "leg-1"
    assert grid.dashGridOptions["stopEditingWhenCellsLoseFocus"] is True
    assert grid.dashGridOptions["selectionColumnDef"] == {
        "width": 34,
        "minWidth": 34,
        "maxWidth": 34,
        "resizable": False,
        "suppressHeaderMenuButton": True,
    }
    assert not any(
        isinstance(getattr(component, "id", None), dict)
        and component.id.get("type") == "pricer-structure-quantity"
        for component in walk(pricer.layout)
    )
    assert next(
        column
        for column in leaf_columns(grid.columnDefs)
        if column.get("field") == "ratio"
    )["headerName"] == "Lots"
    quote_basis_column = next(
        column
        for column in leaf_columns(grid.columnDefs)
        if column.get("field") == "quote_basis"
    )
    quote_input_column = next(
        column
        for column in leaf_columns(grid.columnDefs)
        if column.get("field") == "quote_value"
    )
    assert quote_basis_column["headerName"] == "Quote basis"
    assert quote_basis_column["cellEditorParams"]["values"] == ["VOL", "PREMIUM"]
    assert quote_input_column["headerName"] == "Quote input"
    assert "43.20" in quote_input_column["headerTooltip"]
    assert "unsigned unit price" in quote_input_column["headerTooltip"]
    compact_widths = {
        column["field"]: column["width"]
        for column in leaf_columns(grid.columnDefs)
        if column.get("field")
    }
    assert compact_widths == {
        "name": 104,
        "side": 72,
        "ratio": 64,
        "call_put": 82,
        "strike": 84,
        "quote_basis": 94,
        "quote_value": 94,
        "raw_volatility": 88,
        "volatility_used": 82,
        "trade_value": 88,
        "trade_delta": 82,
        "trade_gamma": 82,
        "trade_theta": 82,
        "trade_vega": 82,
        "trade_rho": 82,
        "unit_value": 72,
        "unit_delta": 72,
        "unit_gamma": 72,
        "unit_theta": 72,
        "unit_vega": 72,
        "unit_rho": 72,
    }
    assert grid.rowData[0]["quote_basis"] == "VOL"
    assert grid.rowData[0]["quote_value"] == 0.2
    kirk_columns = {
        column.get("field"): column
        for column in leaf_columns(pricer._leg_column_defs("kirk"))
        if column.get("field")
    }
    kirk_fields = set(kirk_columns)
    assert "quote_basis" not in kirk_fields
    assert "quote_value" not in kirk_fields
    assert kirk_columns["volatility_asset_1"]["width"] == 118
    assert kirk_columns["volatility_asset_2"]["width"] == 118
    assert kirk_columns["trade_gamma_s1s2"]["width"] == 96
    assert kirk_columns["trade_corr_sensitivity"]["width"] == 88
    assert kirk_columns["trade_vega_equiv"]["width"] == 92


def test_pricing_model_dropdown_uses_compact_model_names():
    field = pricer._build_pricing_model_field()
    dropdown = component_by_pattern("pricer-option-type", root=field)
    assert dropdown.options == [
        {"label": "Black-76", "value": "black76"},
        {"label": "Asian-76", "value": "asian76"},
        {"label": "Kirk", "value": "kirk"},
    ]


def test_signed_lot_grid_removes_side_and_preserves_legacy_page_contract():
    signed_columns = pricer._leg_column_defs("black76", signed_lots=True)
    signed_fields = {
        column.get("field") for column in leaf_columns(signed_columns)
    }
    legacy_fields = {
        column.get("field")
        for column in leaf_columns(pricer._leg_column_defs("black76"))
    }
    signed_lots = next(
        column
        for column in leaf_columns(signed_columns)
        if column.get("field") == "ratio"
    )

    assert "side" not in signed_fields
    assert "side" in legacy_fields
    assert signed_lots["headerTooltip"] == "Positive = buy; negative = sell."
    assert "=== 0" in signed_lots["cellClassRules"]["pricer-invalid-cell"]

    sell_row = {
        **pricer.default_leg("black76", 1),
        "side": "SELL",
        "ratio": 2,
    }
    signed_rows = pricer._rows_for_lot_mode([sell_row], signed_lots=True)
    assert signed_rows[0]["ratio"] == -2
    assert "side" not in signed_rows[0]
    assert pricer._rows_for_lot_mode(signed_rows, signed_lots=False)[0] == sell_row


def test_structure_panel_uses_route_specific_lot_mode():
    structure = {
        "structure_id": pricer.DEFAULT_STRUCTURE_ID,
        "label": "S1",
        "template": {
            "model": "black76",
            "legs": [
                {
                    **pricer.default_leg("black76", 1),
                    "side": "SELL",
                    "ratio": 3,
                }
            ],
        },
    }

    signed_panel = pricer._build_structure_panel(
        structure,
        signed_lots=True,
        use_published_surface=True,
    )
    legacy_panel = pricer._build_structure_panel(structure)
    signed_grid = component_by_pattern("pricer-legs-grid", root=signed_panel)
    legacy_grid = component_by_pattern("pricer-legs-grid", root=legacy_panel)

    assert signed_grid.rowData[0]["ratio"] == -3
    assert "side" not in signed_grid.rowData[0]
    signed_fields = {
        column.get("field") for column in leaf_columns(signed_grid.columnDefs)
    }
    assert "quote_basis" not in signed_fields
    assert "quote_value" not in signed_fields
    assert "volatility_asset_1" not in signed_fields
    assert "volatility_asset_2" not in signed_fields
    assert signed_grid.rowData[0]["atm_vol_adjustment"] == 0.0
    assert signed_grid.rowData[0]["skew_vol_adjustment"] == 0.0
    assert signed_grid.rowData[0]["smile_vol_adjustment"] == 0.0
    assert legacy_grid.rowData[0]["side"] == "SELL"
    assert legacy_grid.rowData[0]["ratio"] == 3
    assert "atm_vol_adjustment" not in legacy_grid.rowData[0]


def test_model_switch_keeps_signed_lot_columns_on_new_pricer(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: {"type": "pricer-option-type", "structure_id": "structure-1"},
    )

    outputs = pricer.manage_structure_legs(
        "kirk",
        None,
        None,
        None,
        None,
        [pricer.default_leg("black76", 1)],
        [],
        {"model": "black76", "legs": []},
        pricer._instance_id("pricer-option-type", "structure-1"),
        "TTF",
        "/pricer",
    )

    fields = {
        column.get("field") for column in leaf_columns(outputs[1])
    }
    assert "side" not in fields
    assert outputs[2][0]["ratio"] == 1
    assert "side" not in outputs[2][0]


def test_jkm_vanilla_surface_note_is_exchange_only_and_follows_forward():
    exchange_panel = pricer._build_structure_panel(
        {
            "structure_id": "exchange-structure-1",
            "label": "Structure 1",
            "template": {"asset": "JKM", "model": "black76"},
        },
        workflow="exchange",
        signed_lots=True,
        use_published_surface=True,
    )
    shared_context = component_by_pattern(
        "pricer-shared-context",
        structure_id="exchange-structure-1",
        root=exchange_panel,
    )
    children = list(shared_context.children)
    note_index = next(
        index
        for index, child in enumerate(children)
        if "pricer-jkm-vanilla-surface-note"
        in str(getattr(child, "className", ""))
    )

    assert (
        children[note_index].children
        == "JKM APO surface, expiry-adjusted to JKZ."
    )
    assert "pricer-forward-field" in children[note_index - 1].className

    bzo_panel = pricer._build_structure_panel(
        {
            "structure_id": "exchange-bzo-1",
            "label": "Structure 1",
            "template": {"mapping_id": "CME-BRENT-BZO"},
        },
        workflow="exchange",
        signed_lots=True,
        use_published_surface=True,
    )
    bzo_context = component_by_pattern(
        "pricer-shared-context",
        structure_id="exchange-bzo-1",
        root=bzo_panel,
    )
    bzo_children = list(bzo_context.children)
    bzo_note = next(
        child
        for child in bzo_children
        if isinstance(getattr(child, "id", None), dict)
        and child.id.get("type") == "pricer-surface-proxy-note"
    )

    assert bzo_note.children == ""
    assert bzo_note.className == "pricer-surface-proxy-note"
    assert pricer.sync_exchange_surface_proxy_note("CME-BRENT-BZO") == (
        "",
        "pricer-surface-proxy-note",
    )

    for asset, model, workflow in (
        ("JKM", "asian76", "exchange"),
        ("TTF", "black76", "exchange"),
        ("JKM", "black76", "otc"),
        ("JKM", "black76", "legacy"),
    ):
        panel = pricer._build_structure_panel(
            {
                "structure_id": f"{workflow}-{asset}-{model}",
                "label": "Structure 1",
                "template": {"asset": asset, "model": model},
            },
            workflow=workflow,
        )
        assert not any(
            "pricer-jkm-vanilla-surface-note"
            in str(getattr(component, "className", ""))
            for component in walk(panel)
        )


def test_otc_header_starts_with_model_while_exchange_order_is_unchanged():
    structure = {
        "structure_id": "test-structure",
        "label": "Structure 1",
        "template": {"asset": "TTF", "model": "black76"},
    }
    otc_panel = pricer._build_structure_panel(structure, workflow="otc")
    exchange_panel = pricer._build_structure_panel(structure, workflow="exchange")

    def direct_control_types(panel):
        controls = next(
            item
            for item in walk(panel)
            if getattr(item, "className", None)
            == "pricer-structure-header-controls"
        )
        return [
            child.id["type"]
            for child in controls.children
            if isinstance(getattr(child, "id", None), dict)
        ]

    assert direct_control_types(otc_panel)[:3] == [
        "pricer-model-field",
        "pricer-asset-field",
        "pricer-price-unit-field",
    ]
    assert direct_control_types(exchange_panel)[:4] == [
        "pricer-mapping-id-field",
        "pricer-asset-field",
        "pricer-price-unit-field",
        "pricer-model-field",
    ]


def test_otc_kirk_form_has_explicit_two_asset_inputs_and_unit_notional_default():
    panel = pricer._build_structure_panel(
        {
            "structure_id": "otc-kirk",
            "label": "Structure 1",
            "template": {"model": "kirk"},
        },
        workflow="otc",
        signed_lots=True,
        use_published_surface=True,
    )
    kirk_components = [
        item
        for item in walk(panel)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("model") == "kirk"
        and item.id.get("type")
        in {"pricer-context-param", "pricer-context-date"}
    ]
    by_param = {item.id["param"]: item for item in kirk_components}

    assert {
        "asset_1_code",
        "asset_2_code",
    } == {
        key for key, item in by_param.items() if isinstance(item, dcc.Dropdown)
        and key.startswith("asset_") and key.endswith("_code")
    }
    assert by_param["asset_1_code"].value is None
    assert by_param["asset_2_code"].value is None
    assert by_param["asset_1_code"].clearable is True
    assert by_param["asset_2_code"].clearable is True
    assert by_param["asset_1_code"].placeholder == "Select"
    assert by_param["asset_2_code"].placeholder == "Select"
    assert {"asset_1_forward", "asset_2_forward"}.issubset(by_param)
    assert {
        "asset_1_reference_expiry",
        "asset_2_reference_expiry",
        "contractual_expiry",
    }.issubset(by_param)
    assert "correlation" in by_param
    assert "delivery_shape" not in by_param
    assert "delivery_month" not in by_param
    shared_context = component_by_pattern(
        "pricer-shared-context",
        structure_id="otc-kirk",
        root=panel,
    )
    shared_labels = [
        item.children[0].children
        for item in shared_context.children
        if isinstance(item, html.Label)
    ]
    assert "Asset 1 forward" in shared_labels
    assert "Asset 2 forward" in shared_labels
    kirk_unit_components = [
        item
        for item in walk(panel)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-kirk-price-unit"
    ]
    assert len(kirk_unit_components) == 2
    assert [item.children for item in kirk_unit_components] == ["—", "—"]
    assert component_by_pattern(
        "pricer-contract-multiplier",
        structure_id="otc-kirk",
        root=panel,
    ).value == 1.0
    assert component_by_pattern(
        "pricer-asset-field",
        structure_id="otc-kirk",
        root=panel,
    ).style == {"display": "none"}
    assert component_by_pattern(
        "pricer-price-unit-field",
        structure_id="otc-kirk",
        root=panel,
    ).style == {"display": "none"}

    labels, _descriptions = pricer.display_kirk_asset_price_units(
        ["JKM", "HH"],
        [
            pricer._context_id("kirk", "asset_1_code", structure_id="otc-kirk"),
            pricer._context_id("kirk", "asset_2_code", structure_id="otc-kirk"),
        ],
        [
            {
                "type": "pricer-kirk-price-unit",
                "structure_id": "otc-kirk",
                "asset_number": 1,
            },
            {
                "type": "pricer-kirk-price-unit",
                "structure_id": "otc-kirk",
                "asset_number": 2,
            },
        ],
    )
    assert labels == ["USD/MMBtu", "USD/MMBtu"]


def test_dashboard_calculation_treats_negative_lots_as_sell(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "calculate-button",
    )
    params, param_ids, dates, date_ids = black_context_states()
    sell_leg = pricer._rows_for_lot_mode(
        [
            {
                **pricer.default_leg("black76", 1),
                "side": "SELL",
                "ratio": 2,
            }
        ],
        signed_lots=True,
    )

    snapshot, status = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        100,
        sell_leg,
        params,
        dates,
        "2026-07-29",
        param_ids,
        date_ids,
    )

    assert snapshot["legs"][0]["side"] == "SELL"
    assert snapshot["legs"][0]["ratio"] == 2
    assert snapshot["legs"][0]["weight"] == -2
    assert snapshot["legs"][0]["trade_contribution"]["value"] < 0
    assert status.className.endswith("success")


def test_new_pricer_calculates_from_published_surface_not_hidden_quote(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: {"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    params, param_ids, dates, date_ids = black_context_states()
    rows = pricer._rows_for_lot_mode(
        [
            {
                **pricer.default_leg("black76", 1),
                "quote_basis": "PREMIUM",
                "quote_value": 999.0,
            }
        ],
        signed_lots=True,
    )
    context = pricer._context_from_states(
        "black76",
        params,
        param_ids,
        dates,
        date_ids,
    )
    reference_signature = pricer._surface_reference_input_signature(
        "TTF",
        "black76",
        rows,
        context,
        "2026-07-29",
    )
    pricing_volatility = 0.31 * pricer.volatility_adjustment(
        FrozenDate(2026, 7, 29),
        FrozenDate(2026, 10, 29),
        FrozenDate(2027, 1, 29),
        asset="TTF",
    )[0]
    surface_reference = {
        "schema_version": pricer.REFERENCE_SCHEMA_VERSION,
        "asset": "TTF",
        "model": "black76",
        "publication_id": "published-42",
        "publication_cob": "2026-07-28",
        "published_at": "2026-07-29T07:00:00+00:00",
        "_ui_reference_signature": reference_signature,
        "rows": {
            "leg-1": {
                "surface_input_vol": 0.31,
                "surface_atm_input_vol": 0.29,
                "surface_skew_input_vol": 0.02,
                "surface_pricing_vol": pricing_volatility,
                "surface_input_tooltip": "Published: 2026-07-29 07:00 UTC",
            }
        },
    }

    snapshot, status, grid_options, _baseline = (
        pricer.calculate_structure_instance_callback(
            1,
            0,
            "TTF",
            "black76",
            100,
            rows,
            None,
            params,
            dates,
            "2026-07-29",
            param_ids,
            date_ids,
            None,
            surface_reference=surface_reference,
            pathname="/pricer",
        )
    )

    assert status.className.endswith("success")
    assert snapshot["legs"][0]["quote_basis"] == "VOL"
    assert snapshot["legs"][0]["raw_volatility"] == pytest.approx(0.31)
    assert snapshot["legs"][0]["quote_input"] == pytest.approx(0.31)
    assert snapshot["_ui_input_signature"]["published_surface"] == {
        "publication_id": "published-42",
        "publication_cob": "2026-07-28",
        "published_at": "2026-07-29T07:00:00+00:00",
        "rows": [
            {
                "leg_id": "leg-1",
                "surface_input_vol": 0.31,
                "surface_atm_input_vol": 0.29,
                "surface_skew_input_vol": 0.02,
                "surface_pricing_vol": pricing_volatility,
                "atm_vol_adjustment": 0.0,
                "skew_vol_adjustment": 0.0,
                "smile_vol_adjustment": 0.0,
                    "effective_input_vol": 0.31,
                    "effective_pricing_vol": pricing_volatility,
                    "surface_expiry_adjustments": [],
            }
        ],
    }
    assert grid_options["context"]["pricingRows"]["leg-1"][
        "raw_volatility"
    ] == pytest.approx(0.31)

    adjusted_rows = copy.deepcopy(rows)
    adjusted_rows[0].update(
        {
            "atm_vol_adjustment": 1.0,
            "skew_vol_adjustment": -0.25,
            "smile_vol_adjustment": 0.5,
        }
    )
    adjusted_snapshot, adjusted_status, _grid_options, _baseline = (
        pricer.calculate_structure_instance_callback(
            2,
            0,
            "TTF",
            "black76",
            100,
            adjusted_rows,
            None,
            params,
            dates,
            "2026-07-29",
            param_ids,
            date_ids,
            None,
            surface_reference=surface_reference,
            pathname="/pricer",
        )
    )

    effective_input_vol = 0.31 + 0.01 * (1.0 - 0.25 + 0.5)
    assert adjusted_status.className.endswith("success")
    assert adjusted_snapshot["legs"][0]["raw_volatility"] == pytest.approx(
        effective_input_vol
    )
    assert adjusted_snapshot["legs"][0]["volatility_used"] == pytest.approx(
        pricing_volatility * effective_input_vol / 0.31
    )
    assert adjusted_snapshot["legs"][0]["unit"]["value"] > snapshot["legs"][0][
        "unit"
    ]["value"]


def test_new_pricer_blocks_calculation_when_published_surface_is_missing(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: {"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    params, param_ids, dates, date_ids = black_context_states()
    rows = pricer._rows_for_lot_mode(
        [pricer.default_leg("black76", 1)],
        signed_lots=True,
    )

    snapshot, status, grid_options, _baseline = (
        pricer.calculate_structure_instance_callback(
            1,
            0,
            "TTF",
            "black76",
            100,
            rows,
            None,
            params,
            dates,
            "2026-07-29",
            param_ids,
            date_ids,
            None,
            surface_reference=None,
            pathname="/pricer",
        )
    )

    assert snapshot is None
    assert status.className.endswith("danger")
    assert "published surface volatility is not available yet" in status.children.lower()
    assert grid_options["context"]["pricingRows"] == {}


def test_structure_panels_have_unique_scoped_ids_and_persistence_keys():
    panels = [
        pricer._build_structure_panel(
            {
                "structure_id": structure_id,
                "label": label,
                "template": None,
            }
        )
        for structure_id, label in (
            ("structure-1", "S1"),
            ("structure-2", "S2"),
        )
    ]
    component_ids = [
        item.id
        for panel in panels
        for item in walk(panel)
        if getattr(item, "id", None) is not None
    ]

    assert len(component_ids) == len({canonical_id(value) for value in component_ids})
    for panel, structure_id in zip(panels, ("structure-1", "structure-2")):
        pattern_ids = [
            item.id
            for item in walk(panel)
            if isinstance(getattr(item, "id", None), dict)
        ]
        assert pattern_ids
        assert {value["structure_id"] for value in pattern_ids} == {structure_id}
        persistence_keys = {
            item.persistence
            for item in walk(panel)
            if getattr(item, "persistence", None)
        }
        assert persistence_keys
        assert all(
            str(key).startswith(f"pricer-{structure_id}-")
            for key in persistence_keys
        )
        stores = [item for item in walk(panel) if isinstance(item, dcc.Store)]
        assert {item.id["type"] for item in stores} == {
            "pricer-structure-workflow",
            "pricer-contract-size-default",
            "pricer-draft-store",
            "pricer-calculation-store",
            "pricer-grid-pricing-options",
            "pricer-published-surface-reference",
            "pricer-calculate-all-baseline",
            "pricer-calculate-all-ack",
        }
        workflow_store = next(
            item
            for item in stores
            if item.id["type"] == "pricer-structure-workflow"
        )
        assert workflow_store.data == "legacy"
        storage_by_type = {
            item.id["type"]: item.storage_type for item in stores
        }
        assert storage_by_type == {
            "pricer-structure-workflow": "memory",
            "pricer-contract-size-default": "memory",
            "pricer-draft-store": "session",
            "pricer-calculation-store": "memory",
            "pricer-grid-pricing-options": "memory",
            "pricer-published-surface-reference": "memory",
            "pricer-calculate-all-baseline": "memory",
            "pricer-calculate-all-ack": "memory",
        }

    first_keys = {
        item.persistence
        for item in walk(panels[0])
        if getattr(item, "persistence", None)
    }
    second_keys = {
        item.persistence
        for item in walk(panels[1])
        if getattr(item, "persistence", None)
    }
    assert first_keys.isdisjoint(second_keys)


def test_global_valuation_date_overrides_structure_dates_without_persistence():
    panel = pricer._build_structure_panel(
        {
            "structure_id": "structure-1",
            "label": "S1",
            "template": {"valuation_date": "2025-01-15"},
        },
        valuation_date_override="2026-07-29",
    )
    valuation = component_by_pattern("pricer-valuation-date", root=panel)
    assert valuation.date == "2026-07-29"
    assert valuation.persistence is False

    valuation_ids = [
        pricer._instance_id("pricer-valuation-date", "structure-1"),
        pricer._instance_id("pricer-valuation-date", "structure-2"),
    ]
    assert pricer.sync_pricer_global_valuation_date(
        "2026-07-28",
        "/pricer",
        valuation_ids,
    ) == ["2026-07-28", "2026-07-28"]
    assert all(
        value is no_update
        for value in pricer.sync_pricer_global_valuation_date(
            "2026-07-28",
            "/pricer_old",
            valuation_ids,
        )
    )


def test_workspace_reducer_adds_duplicates_and_removes_without_aliasing_state():
    base = pricer._normalize_workspace(None)
    assert [structure["label"] for structure in base["structures"]] == ["S1"]
    added = pricer._reduce_workspace(base, "add")
    assert [
        structure["structure_id"] for structure in added["structures"]
    ] == ["structure-1", "structure-2"]
    assert [structure["label"] for structure in added["structures"]] == [
        "S1",
        "S2",
    ]
    assert added["structures"][1]["template"] is None

    template = {
        "model": "black76",
        "asset": "TTF",
        "context": {"forward": 103.5},
        "legs": two_leg_rows(),
        "next_leg_sequence": 3,
    }
    duplicated = pricer._reduce_workspace(
        added,
        "duplicate",
        "structure-1",
        template,
    )
    duplicate = duplicated["structures"][-1]
    assert duplicate["structure_id"] == "structure-3"
    assert duplicate["label"] == "S3"
    assert duplicate["template"] == template
    template["context"]["forward"] = 999
    template["legs"][0]["strike"] = 1
    assert duplicate["template"]["context"]["forward"] == 103.5
    assert duplicate["template"]["legs"][0]["strike"] == 95

    removed = pricer._reduce_workspace(duplicated, "remove", "structure-2")
    assert [
        structure["structure_id"] for structure in removed["structures"]
    ] == ["structure-1", "structure-3"]
    assert pricer._reduce_workspace(base, "remove", "structure-1") == base


def test_instance_calculation_and_input_invalidation_are_isolated(monkeypatch):
    snapshot_1, status_1 = calculate_instance(monkeypatch, "structure-1", forward=100)
    snapshot_2, status_2 = calculate_instance(monkeypatch, "structure-2", forward=112)
    assert snapshot_1["context"]["forward"] == pytest.approx(100)
    assert snapshot_2["context"]["forward"] == pytest.approx(112)
    assert snapshot_1["totals"]["trade_value"] != pytest.approx(
        snapshot_2["totals"]["trade_value"]
    )
    assert status_1.className.endswith("success")
    assert status_2.className.endswith("success")

    cleared_1, changed_status = calculate_instance(
        monkeypatch,
        "structure-1",
        trigger={"type": "pricer-legs-grid", "structure_id": "structure-1"},
        current_snapshot=snapshot_1,
        rows=[{**two_leg_rows()[0], "strike": 101}],
    )
    assert_output_is_masked(cleared_1, "structure-1")
    assert changed_status.children == "Modified · outputs cleared · calculate again"
    calculation_store_ids = [
        pricer._instance_id("pricer-calculation-store", "structure-1"),
        pricer._instance_id("pricer-calculation-store", "structure-2"),
    ]
    assert pricer.route_selected_structure_calculation(
        "structure-1",
        [cleared_1, snapshot_2],
        calculation_store_ids,
    ) is no_update
    assert pricer.route_selected_structure_calculation(
        "structure-2",
        [cleared_1, snapshot_2],
        calculation_store_ids,
    ) is snapshot_2

    assert unified_pricing_rows(snapshot_2)["leg-1"]["strike"] == pytest.approx(95)

    recalculated_1, recalculated_status = calculate_instance(
        monkeypatch,
        "structure-1",
        trigger={
            "type": "pricer-calculate-button",
            "structure_id": "structure-1",
        },
        rows=[{**two_leg_rows()[0], "strike": 101}],
    )
    assert recalculated_status.className.endswith("success")
    assert recalculated_1["legs"][0]["strike"] == pytest.approx(101)
    assert unified_pricing_rows(recalculated_1)["leg-1"]["strike"] == pytest.approx(
        101
    )


def test_hydrated_matching_inputs_preserve_the_persisted_snapshot(monkeypatch):
    snapshot, _status = calculate_instance(monkeypatch, "structure-1")

    preserved_snapshot, preserved_status = calculate_instance(
        monkeypatch,
        "structure-1",
        trigger={
            "type": "pricer-valuation-date",
            "structure_id": "structure-1",
        },
        current_snapshot=snapshot,
    )

    assert preserved_snapshot is no_update
    assert preserved_status is no_update


def test_committed_grid_event_clears_outputs_even_before_rowdata_updates(monkeypatch):
    state = model_instance_state("black76")
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: {"type": "pricer-legs-grid", "structure_id": "structure-1"},
    )

    invalidated, status, grid_state, baseline = (
        pricer.calculate_structure_instance_callback(
            1,
            1,
            state["asset"],
            state["model"],
            state["contract_multiplier"],
            copy.deepcopy(state["rows"]),
            {"oldValue": 100.0, "newValue": 101.0, "colId": "strike"},
            state["param_values"],
            state["date_values"],
            state["valuation_date"],
            state["param_ids"],
            state["date_ids"],
            snapshot,
        )
    )

    assert invalidated is None
    assert status.children == "Modified · outputs cleared · calculate again"
    assert grid_state["context"]["pricingRows"] == {}
    assert grid_state["pinnedBottomRowData"] == []
    assert baseline is no_update


def test_panel_restore_rejects_snapshot_whose_draft_signature_changed(monkeypatch):
    state = model_instance_state("black76")
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    context = pricer._context_from_states(
        state["model"],
        state["param_values"],
        state["param_ids"],
        state["date_values"],
        state["date_ids"],
    )
    template = {
        "asset": state["asset"],
        "model": state["model"],
        "contract_multiplier": state["contract_multiplier"],
        "valuation_date": state["valuation_date"],
        "context": context,
        "legs": copy.deepcopy(state["rows"]),
        "next_leg_sequence": 2,
    }
    assert pricer._snapshot_matches_template(snapshot, template)

    current_panel = pricer._build_structure_panel(
        {
            "structure_id": "structure-1",
            "label": "Structure 1",
            "template": template,
        },
        calculation_snapshot=snapshot,
    )
    assert component_by_pattern(
        "pricer-calculation-store",
        root=current_panel,
    ).data == snapshot
    assert component_by_pattern("pricer-legs-grid", root=current_panel).dashGridOptions[
        "pinnedBottomRowData"
    ]

    stale_template = copy.deepcopy(template)
    stale_template["legs"][0]["strike"] += 1
    stale_panel = pricer._build_structure_panel(
        {
            "structure_id": "structure-1",
            "label": "Structure 1",
            "template": stale_template,
        },
        calculation_snapshot=snapshot,
    )
    assert component_by_pattern(
        "pricer-calculation-store",
        root=stale_panel,
    ).data is None
    stale_grid = component_by_pattern("pricer-legs-grid", root=stale_panel)
    assert stale_grid.dashGridOptions["context"]["pricingRows"] == {}
    assert stale_grid.dashGridOptions["pinnedBottomRowData"] == []
    assert "outputs cleared" in component_by_pattern(
        "pricer-calculation-status",
        root=stale_panel,
    ).children.children


def test_modified_input_status_prunes_the_session_snapshot(monkeypatch):
    snapshot, _status = calculate_instance(monkeypatch, "structure-1")
    modified_status = pricer._build_pricer_message(
        "Modified · outputs cleared · calculate again"
    )
    workspace = pricer._default_workspace()

    persisted = pricer.persist_pricer_calculations(
        workspace,
        [None],
        [pricer._instance_id("pricer-calculation-store", "structure-1")],
        [modified_status],
        [pricer._instance_id("pricer-calculation-status", "structure-1")],
        {"structure-1": snapshot},
    )

    assert persisted == {}


@pytest.mark.parametrize(
    (
        "model",
        "location",
        "field",
        "new_value",
        "trigger_type",
    ),
    [
        (
            "black76",
            "param",
            "premium_convention",
            "upfront",
            "pricer-context-param",
        ),
        ("black76", "top", "asset", "Brent", "pricer-asset"),
        (
            "black76",
            "top",
            "contract_multiplier",
            2,
            "pricer-contract-multiplier",
        ),
        (
            "black76",
            "top",
            "valuation_date",
            "2026-07-30",
            "pricer-valuation-date",
        ),
        ("black76", "param", "forward", 101.5, "pricer-context-param"),
        ("black76", "param", "delivery_shape", "Q1", "pricer-context-param"),
        (
            "black76",
            "date",
            "expiration_date",
            "2026-08-20",
            "pricer-context-date",
        ),
        (
            "black76",
            "date",
            "contract_expiration_date",
            "2026-09-30",
            "pricer-context-date",
        ),
        (
            "asian76",
            "date",
            "averaging_start_date",
            "2026-08-06",
            "pricer-context-date",
        ),
        ("kirk", "param", "asset_1_forward", 105.0, "pricer-context-param"),
        ("kirk", "param", "asset_2_forward", 92.0, "pricer-context-param"),
        ("kirk", "param", "correlation", 0.25, "pricer-context-param"),
    ],
)
def test_header_date_and_model_specific_changes_mask_then_restore_output(
    monkeypatch,
    model,
    location,
    field,
    new_value,
    trigger_type,
):
    state = model_instance_state(model)
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={
            "type": "pricer-calculate-button",
            "structure_id": state["structure_id"],
        },
    )
    changed_state = copy.deepcopy(state)
    if location == "top":
        changed_state[field] = new_value
        trigger = {
            "type": trigger_type,
            "structure_id": state["structure_id"],
        }
    else:
        trigger = set_context_state_value(
            changed_state,
            field,
            new_value,
            is_date=location == "date",
        )

    invalidated, invalidated_status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger=trigger,
        current_snapshot=snapshot,
    )
    assert_output_is_masked(invalidated, state["structure_id"])
    assert "calculate again" in invalidated_status.children.lower()

    recalculated, recalculated_status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger={
            "type": "pricer-calculate-button",
            "structure_id": state["structure_id"],
        },
    )
    expected_signature = pricer._calculation_input_signature(
        changed_state["asset"],
        changed_state["model"],
        changed_state["contract_multiplier"],
        changed_state["rows"],
        changed_state["param_values"],
        changed_state["date_values"],
        changed_state["valuation_date"],
        changed_state["param_ids"],
        changed_state["date_ids"],
    )
    assert recalculated["_ui_input_signature"] == expected_signature
    assert recalculated_status.className.endswith("success")
    assert unified_grid_state(recalculated)["pinnedBottomRowData"]


def test_hidden_month_delivery_year_change_preserves_current_calculation(monkeypatch):
    state = model_instance_state("black76")
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={
            "type": "pricer-calculate-button",
            "structure_id": state["structure_id"],
        },
    )
    changed_state = copy.deepcopy(state)
    trigger = set_context_state_value(changed_state, "delivery_year", 2030)

    preserved, status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger=trigger,
        current_snapshot=snapshot,
    )

    assert preserved is no_update
    assert status is no_update


def test_ttf_black_rate_change_preserves_current_calculation(monkeypatch):
    state = model_instance_state("black76")
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={
            "type": "pricer-calculate-button",
            "structure_id": state["structure_id"],
        },
    )
    changed_state = copy.deepcopy(state)
    trigger = set_context_state_value(changed_state, "rate", 0.04)

    preserved, status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger=trigger,
        current_snapshot=snapshot,
    )

    assert preserved is no_update
    assert status is no_update


def test_visible_strip_delivery_year_change_clears_current_calculation(monkeypatch):
    state = model_instance_state("black76")
    set_context_state_value(state, "delivery_shape", "SUM")
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={
            "type": "pricer-calculate-button",
            "structure_id": state["structure_id"],
        },
    )
    changed_state = copy.deepcopy(state)
    trigger = set_context_state_value(changed_state, "delivery_year", 2028)

    invalidated, status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger=trigger,
        current_snapshot=snapshot,
    )

    assert_output_is_masked(invalidated)
    assert "calculate again" in status.children.lower()


@pytest.mark.parametrize(
    ("field", "new_value"),
    [
        ("side", "SELL"),
        ("ratio", 2),
        ("call_put", "P"),
        ("strike", 105.0),
        ("quote_value", 0.27),
    ],
)
def test_each_pricing_grid_edit_masks_then_restores_current_rows(
    monkeypatch,
    field,
    new_value,
):
    state = model_instance_state("black76")
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={
            "type": "pricer-calculate-button",
            "structure_id": state["structure_id"],
        },
    )
    changed_state = copy.deepcopy(state)
    changed_state["rows"][0][field] = new_value

    invalidated, invalidated_status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger={
            "type": "pricer-legs-grid",
            "structure_id": state["structure_id"],
        },
        current_snapshot=snapshot,
    )
    assert_output_is_masked(invalidated)
    assert invalidated_status.children == "Modified · outputs cleared · calculate again"

    recalculated, _status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger={
            "type": "pricer-calculate-button",
            "structure_id": state["structure_id"],
        },
    )
    result_row = unified_pricing_rows(recalculated)["leg-1"]
    expected_field = "raw_volatility" if field == "quote_value" else field
    if isinstance(new_value, (int, float)):
        assert result_row[expected_field] == pytest.approx(new_value)
    else:
        assert result_row[expected_field] == new_value


def test_quote_basis_change_masks_output_until_valid_premium_recalculation(
    monkeypatch,
):
    state = model_instance_state("black76")
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    premium = snapshot["legs"][0]["unit"]["value"]
    premium_state = copy.deepcopy(state)
    premium_state["rows"][0]["quote_basis"] = "PREMIUM"
    premium_state["rows"][0]["quote_value"] = None

    invalidated, _status = invoke_instance_state(
        monkeypatch,
        premium_state,
        trigger={"type": "pricer-legs-grid", "structure_id": "structure-1"},
        current_snapshot=snapshot,
    )
    assert_output_is_masked(invalidated)

    premium_state["rows"][0]["quote_value"] = premium
    recalculated, recalculated_status = invoke_instance_state(
        monkeypatch,
        premium_state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    result_row = unified_pricing_rows(recalculated)["leg-1"]
    assert recalculated_status.className.endswith("success")
    assert result_row["quote_basis"] == "Premium"
    assert result_row["entered_premium"] == pytest.approx(premium)


@pytest.mark.parametrize(
    ("field", "result_field", "new_value"),
    [
        ("volatility_asset_1", "raw_volatility_asset_1", 0.24),
        ("volatility_asset_2", "raw_volatility_asset_2", 0.18),
    ],
)
def test_kirk_grid_volatility_changes_mask_then_restore_output(
    monkeypatch,
    field,
    result_field,
    new_value,
):
    state = model_instance_state("kirk")
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    changed_state = copy.deepcopy(state)
    changed_state["rows"][0][field] = new_value

    invalidated, _status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger={"type": "pricer-legs-grid", "structure_id": "structure-1"},
        current_snapshot=snapshot,
    )
    assert_output_is_masked(invalidated)

    recalculated, recalculated_status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    assert recalculated_status.className.endswith("success")
    assert unified_pricing_rows(recalculated)["leg-1"][result_field] == pytest.approx(
        new_value
    )


def test_model_switch_masks_black_output_and_recalculation_restores_kirk(
    monkeypatch,
):
    black_state = model_instance_state("black76")
    black_snapshot, _status = invoke_instance_state(
        monkeypatch,
        black_state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    kirk_state = model_instance_state("kirk")

    invalidated, invalidated_status = invoke_instance_state(
        monkeypatch,
        kirk_state,
        trigger={"type": "pricer-option-type", "structure_id": "structure-1"},
        current_snapshot=black_snapshot,
    )
    assert_output_is_masked(invalidated)
    assert "Model changed" in invalidated_status.children

    kirk_snapshot, kirk_status = invoke_instance_state(
        monkeypatch,
        kirk_state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    assert kirk_snapshot["model"] == "kirk"
    assert kirk_status.className.endswith("success")
    assert unified_pricing_rows(kirk_snapshot)["leg-1"]["strike"] == pytest.approx(
        5.0
    )


@pytest.mark.parametrize(
    ("action_type", "starting_leg_count", "expected_leg_count"),
    [
        ("pricer-add-leg", 1, 2),
        ("pricer-duplicate-leg", 1, 2),
        ("pricer-remove-leg", 2, 1),
    ],
)
def test_leg_actions_mask_outputs_and_recalculation_uses_the_new_leg_set(
    monkeypatch,
    action_type,
    starting_leg_count,
    expected_leg_count,
):
    state = model_instance_state("black76")
    if starting_leg_count == 2:
        state["rows"] = two_leg_rows()
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    selected_rows = (
        [state["rows"][-1]]
        if action_type in {"pricer-duplicate-leg", "pricer-remove-leg"}
        else []
    )
    draft = {
        "schema_version": 1,
        "model": "black76",
        "context": None,
        "legs": copy.deepcopy(state["rows"]),
        "next_leg_sequence": starting_leg_count + 1,
    }
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: {"type": action_type, "structure_id": "structure-1"},
    )
    managed = pricer.manage_structure_legs(
        "black76",
        1,
        1,
        1,
        None,
        copy.deepcopy(state["rows"]),
        selected_rows,
        draft,
        pricer._instance_id("pricer-option-type", "structure-1"),
    )
    changed_state = copy.deepcopy(state)
    changed_state["rows"] = managed[2]
    assert len(changed_state["rows"]) == expected_leg_count

    invalidated, invalidated_status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger={"type": "pricer-legs-grid", "structure_id": "structure-1"},
        current_snapshot=snapshot,
    )
    assert_output_is_masked(invalidated)
    assert invalidated_status.children == "Modified · outputs cleared · calculate again"

    recalculated, recalculated_status = invoke_instance_state(
        monkeypatch,
        changed_state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    assert recalculated_status.className.endswith("success")
    assert len(recalculated["legs"]) == expected_leg_count
    assert len(
        [
            row_id
            for row_id in unified_pricing_rows(recalculated)
            if row_id != "__total__"
        ]
    ) == expected_leg_count


def test_calculate_all_keeps_valid_results_when_another_structure_fails(monkeypatch):
    valid_snapshot, valid_status = calculate_instance(monkeypatch, "structure-1")
    prior_invalid_structure_snapshot, _prior_status = calculate_instance(
        monkeypatch,
        "structure-2",
    )
    invalid_rows = [{**two_leg_rows()[0], "volatility": None}]
    invalid_snapshot, invalid_status = calculate_instance(
        monkeypatch,
        "structure-2",
        rows=invalid_rows,
        current_snapshot=prior_invalid_structure_snapshot,
    )

    assert valid_snapshot is not None
    assert valid_status.className.endswith("success")
    assert_output_is_masked(invalid_snapshot, "structure-2")
    assert invalid_status.className.endswith("danger")
    assert "volatility is required" in invalid_status.children

    workspace = pricer._reduce_workspace(pricer._default_workspace(), "add")
    store_ids = [
        pricer._instance_id("pricer-calculation-store", structure_id)
        for structure_id in ("structure-1", "structure-2")
    ]
    assert pricer.render_pricer_workspace_status(
        workspace,
        [valid_snapshot, invalid_snapshot],
        store_ids,
    ) == "2 structures · 1 calculated"

    persisted = pricer.persist_pricer_calculations(
        workspace,
        [valid_snapshot, invalid_snapshot],
        store_ids,
        [valid_status, invalid_status],
        [
            pricer._instance_id("pricer-calculation-status", "structure-1"),
            pricer._instance_id("pricer-calculation-status", "structure-2"),
        ],
        {
            "structure-1": valid_snapshot,
            "structure-2": prior_invalid_structure_snapshot,
        },
    )
    assert persisted == {"structure-1": valid_snapshot}

    restored_snapshot, restored_status = calculate_instance(
        monkeypatch,
        "structure-2",
        rows=two_leg_rows()[:1],
    )
    assert restored_snapshot is not None
    assert restored_status.className.endswith("success")
    assert unified_grid_state(restored_snapshot)["pinnedBottomRowData"]


def test_session_snapshot_map_prunes_removed_structures(monkeypatch):
    removed_snapshot, _status = calculate_instance(monkeypatch, "structure-2")
    workspace = pricer._default_workspace()

    persisted = pricer.persist_pricer_calculations(
        workspace,
        [None],
        [pricer._instance_id("pricer-calculation-store", "structure-1")],
        [""],
        [pricer._instance_id("pricer-calculation-status", "structure-1")],
        {"structure-2": removed_snapshot},
    )

    assert persisted == {}


def test_analysis_selector_and_snapshot_routing_follow_workspace_membership(monkeypatch):
    snapshot_1, _ = calculate_instance(monkeypatch, "structure-1", forward=100)
    snapshot_2, _ = calculate_instance(monkeypatch, "structure-2", forward=108)
    workspace = pricer._reduce_workspace(pricer._default_workspace(), "add")

    options, selected = pricer.sync_analysis_structure_selector(
        workspace,
        persisted_selection="structure-2",
    )
    assert [option["value"] for option in options] == ["structure-1", "structure-2"]
    assert selected == "structure-2"
    store_ids = [
        pricer._instance_id("pricer-calculation-store", structure_id)
        for structure_id in ("structure-1", "structure-2")
    ]
    routed = pricer.route_selected_structure_calculation(
        selected,
        [snapshot_1, snapshot_2],
        store_ids,
    )
    assert routed is snapshot_2
    assert pricer.update_payoff_chart(routed, None, 50).data[0].name == (
        "Total expiration payoff"
    )

    reduced = pricer._reduce_workspace(workspace, "remove", "structure-2")
    reduced_options, reduced_selected = pricer.sync_analysis_structure_selector(
        reduced,
        selected,
    )
    assert reduced_options == [{"label": "S1", "value": "structure-1"}]
    assert reduced_selected == "structure-1"


def test_pricer_number_inputs_publish_edits_without_debounce():
    controls = [
        component
        for model in ("black76", "asian76", "kirk")
        for component in walk(pricer._build_context_form(model))
        if isinstance(component, dcc.Input) and component.type == "number"
    ]
    controls.append(component_by_id("pricer-contract-multiplier"))

    assert controls
    assert all(control.debounce is False for control in controls)
    forward = next(
        control
        for control in controls
        if isinstance(control.id, dict) and control.id.get("param") == "forward"
    )
    rate = next(
        control
        for control in controls
        if isinstance(control.id, dict)
        and control.id.get("model") == "black76"
        and control.id.get("param") == "rate"
    )
    assert forward.step == "any"
    assert forward.persistence == "pricer-structure-1-black76-forward-step-any-v2"
    forward_field = next(
        component
        for component in walk(pricer._build_context_form("black76"))
        if isinstance(component, html.Label)
        and any(
            isinstance(descendant, dcc.Input)
            and isinstance(descendant.id, dict)
            and descendant.id.get("param") == "forward"
            for descendant in walk(component)
        )
    )
    assert forward_field.className == (
        "pricer-field pricer-number-field pricer-forward-field"
    )
    assert rate.step == 0.000001
    assert rate.persistence == "pricer-structure-1-black76-rate"
    multiplier = component_by_id("pricer-contract-multiplier")
    assert multiplier.min == 0.01
    assert multiplier.step == 0.01
    assert multiplier.persistence == (
        "pricer-structure-1-contract-size-v1"
    )


@pytest.mark.parametrize(
    ("asset", "model", "context", "expected"),
    [
        (
            "TTF",
            "black76",
            {"delivery_shape": "MONTH", "expiration_date": "2026-09-25"},
            745.0,
        ),
        (
            "JKM",
            "asian76",
            {"delivery_shape": "MONTH"},
            10_000.0,
        ),
        ("Brent", "black76", {"delivery_shape": "MONTH"}, 1_000.0),
        ("HH", "black76", {"delivery_shape": "MONTH"}, 2_500.0),
        (
            "NBP",
            "black76",
            {"delivery_shape": "MONTH", "delivery_month": "2026-11-01"},
            30_000.0,
        ),
    ],
)
def test_new_structure_panel_uses_exchange_contract_size_default(
    asset,
    model,
    context,
    expected,
):
    panel = pricer._build_structure_panel(
        {
            "structure_id": "structure-9",
            "label": "S9",
            "template": {
                "asset": asset,
                "model": model,
                "valuation_date": "2026-07-29",
                "context": context,
            },
        }
    )

    size_input = component_by_pattern(
        "pricer-contract-multiplier",
        "structure-9",
        panel,
    )
    default_store = component_by_pattern(
        "pricer-contract-size-default",
        "structure-9",
        panel,
    )
    assert size_input.value == pytest.approx(expected)
    assert default_store.data == {"asset": asset, "value": expected}


def test_context_date_pickers_open_on_the_current_selected_date():
    controls = [
        component
        for model in ("black76", "asian76", "kirk")
        for component in walk(pricer._build_context_form(model))
        if isinstance(component, dcc.DatePickerSingle)
    ]

    assert controls
    assert all(
        getattr(control, "initial_visible_month", None) is None
        for control in controls
    )


def test_leg_reducer_adds_duplicates_removes_and_never_removes_last(monkeypatch):
    draft = {
        "schema_version": 1,
        "model": "black76",
        "legs": two_leg_rows()[:1],
        "next_leg_sequence": 2,
    }
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "pricer-add-leg")
    output = pricer.manage_structure_legs(
        "black76", 1, None, None, None, draft["legs"], [], draft
    )
    rows = output[2]
    assert [row["leg_id"] for row in rows] == ["leg-1", "leg-2"]

    draft = output[4]
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-duplicate-leg",
    )
    output = pricer.manage_structure_legs(
        "black76", 1, 1, None, None, rows, [rows[0]], draft
    )
    rows = output[2]
    assert rows[-1]["leg_id"] == "leg-3"
    assert rows[-1]["name"] == "Leg 3"

    draft = output[4]
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "pricer-remove-leg")
    output = pricer.manage_structure_legs(
        "black76", 1, 1, 1, None, rows, [rows[-1]], draft
    )
    rows = output[2]
    assert len(rows) == 2

    one_row = rows[:1]
    output = pricer.manage_structure_legs(
        "black76",
        1,
        1,
        2,
        None,
        one_row,
        [one_row[0]],
        {**draft, "legs": one_row},
    )
    assert output[2] == one_row
    assert "at least one" in output[5]


def test_leg_reducer_enforces_maximum_leg_count(monkeypatch):
    rows = [
        pricer.default_leg("black76", sequence)
        for sequence in range(1, pricer.MAX_LEGS + 1)
    ]
    draft = {
        "schema_version": 1,
        "model": "black76",
        "legs": rows,
        "next_leg_sequence": pricer.MAX_LEGS + 1,
    }
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "pricer-add-leg")
    output = pricer.manage_structure_legs(
        "black76",
        1,
        None,
        None,
        None,
        rows,
        [],
        draft,
    )
    assert len(output[2]) == pricer.MAX_LEGS
    assert f"at most {pricer.MAX_LEGS}" in output[5]


def test_model_switch_resets_incompatible_legs_and_context(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "option-type")
    output = pricer.manage_structure_legs(
        "kirk",
        None,
        None,
        None,
        None,
        two_leg_rows(),
        [],
        {
            "schema_version": 1,
            "model": "black76",
            "legs": two_leg_rows(),
            "next_leg_sequence": 3,
        },
    )
    assert output[2] == [pricer.default_leg("kirk", 1)]
    assert output[4]["model"] == "kirk"
    assert output[3] == []


def test_calculation_and_result_grids_reconcile_two_leg_trade(monkeypatch):
    snapshot, _status = calculate_two_leg_snapshot(monkeypatch)
    assert len(snapshot["legs"]) == 2
    assert snapshot["sizing"]["structure_quantity"] == 1
    assert snapshot["sizing"]["position_scale"] == 100

    rendered = pricer.render_structure_results(snapshot)
    meta = rendered[0]
    assert [item.children[0].children for item in meta] == ["T", "Vol adj"]
    assert meta[0].children[1] == f"{snapshot['context']['time_to_expiry']:.6f}y"
    assert meta[1].children[1] == (
        f"{snapshot['context']['vol_adjustment_factor']:.6f}×"
    )
    assert "contract days" in meta[1].title
    assert rendered[1:5] == ("", "", "", "")

    grid_state = unified_grid_state(snapshot)
    pricing_rows = grid_state["context"]["pricingRows"]
    first = pricing_rows["leg-1"]
    assert len([key for key in pricing_rows if key != "__total__"]) == 2
    assert first["unit_value"] == pytest.approx(
        snapshot["legs"][0]["unit"]["value"]
    )
    assert first["trade_value"] == pytest.approx(
        snapshot["legs"][0]["trade_contribution"]["value"]
    )
    assert first["ratio"] == 2
    assert first["trade_value"] == pytest.approx(
        first["unit_value"] * 2 * 100
    )
    column_defs = pricer._leg_column_defs("black76")
    assert [group["headerName"] for group in column_defs] == [
        "Leg",
        "Leg inputs",
        "Volatility",
        "Published surface",
        "Unit analytics",
        "Position contribution",
    ]
    quote_columns = {
        column.get("field"): column["headerName"]
        for column in leaf_columns(column_defs)
    }
    assert quote_columns["quote_basis"] == "Quote basis"
    assert "entered_premium" not in quote_columns
    assert quote_columns["raw_volatility"] == "Contract vol"
    assert "input_volatility" not in quote_columns
    assert "implied_volatility" not in quote_columns
    assert quote_columns["volatility_used"] == "Pricing vol"
    volatility_columns = column_defs[2]["children"]
    assert [column["field"] for column in volatility_columns] == [
        "raw_volatility",
        "volatility_used",
    ]
    assert all(
        "d3.format('.2%')" in column["valueFormatter"]["function"]
        for column in volatility_columns
    )
    assert first["quote_basis"] == "Vol"
    assert first["raw_volatility"] == pytest.approx(0.19)
    assert first["volatility_used"] == pytest.approx(
        snapshot["legs"][0]["volatility_used"]
    )
    assert "suppressColumnVirtualisation" not in grid_state
    assert grid_state["enableBrowserTooltips"] is False
    unit_columns = column_defs[-2]["children"]
    assert all(
        "d3.format(',.4f')" in column["valueFormatter"]["function"]
        for column in unit_columns
    )
    position_columns = column_defs[-1]["children"]
    assert all(
        "d3.format(',.0f')" in column["valueFormatter"]["function"]
        for column in position_columns
    )
    delta_column = next(
        column for column in position_columns if column["field"] == "trade_delta"
    )
    assert delta_column["minWidth"] == 82
    value_getter = delta_column["valueGetter"]["function"]
    assert "pricingRows" in value_getter
    assert "const " not in value_getter
    assert ";" not in value_getter
    validation_rules = [
        rule
        for column in column_defs[1]["children"]
        for rule in column.get("cellClassRules", {}).values()
    ]
    assert validation_rules
    assert all("rowPinned" in rule for rule in validation_rules)
    pinned = grid_state["pinnedBottomRowData"][0]
    assert pinned["name"] == "Total"
    assert pinned["trade_value"] == pytest.approx(snapshot["totals"]["trade_value"])
    assert pinned["trade_delta"] == pytest.approx(
        snapshot["totals"]["trade_greeks"]["delta"]
    )
    assert pinned["unit_value"] == pytest.approx(
        snapshot["totals"]["unit_structure_value"]
    )
    for field in snapshot["greek_fields"]:
        expected = snapshot["totals"]["unit_structure_greeks"][field]
        if expected is None:
            assert pinned[f"unit_{field}"] is None
        else:
            assert pinned[f"unit_{field}"] == pytest.approx(expected)


def test_kirk_unified_grid_keeps_two_asset_analytics_without_summary_cards():
    as_of = FrozenDate(2026, 7, 29)
    context = pricer.default_context("kirk", as_of)
    context["asset_1_code"] = "JKM"
    context["asset_2_code"] = "HH"
    snapshot = pricer.calculate_structure(
        "kirk",
        context,
        {"structure_quantity": 1, "contract_multiplier": 1},
        [pricer.default_leg("kirk", 1)],
        as_of=as_of,
    )

    assert [group["headerName"] for group in pricer._leg_column_defs("kirk")] == [
        "Leg",
        "Leg inputs",
        "Unit analytics",
        "Position contribution",
    ]
    row = unified_pricing_rows(snapshot)["leg-1"]
    assert row["raw_volatility_asset_1"] == pytest.approx(0.2)
    assert row["raw_volatility_asset_2"] == pytest.approx(0.15)
    assert row["trade_delta_s1"] is not None
    assert row["trade_delta_s2"] is not None
    meta = pricer.render_structure_results(snapshot)[0]
    assert meta[0].children[1] == f"{snapshot['context']['time_to_expiry']:.6f}y"


def test_premium_quoted_leg_result_exposes_solved_contract_vol():
    as_of = FrozenDate(2026, 7, 29)
    context = pricer.default_context("black76", as_of)
    baseline = pricer.calculate_structure(
        "black76",
        context,
        {"structure_quantity": 1, "contract_multiplier": 1},
        [pricer.default_leg("black76", 1)],
        as_of=as_of,
    )
    premium = baseline["legs"][0]["unit"]["value"]
    premium_leg = {
        **pricer.default_leg("black76", 1),
        "quote_basis": "PREMIUM",
        "quote_value": premium,
    }
    snapshot = pricer.calculate_structure(
        "black76",
        context,
        {"structure_quantity": 1, "contract_multiplier": 1},
        [premium_leg],
        as_of=as_of,
    )

    rows, _total = pricer._combined_result_rows(snapshot)
    assert rows[0]["quote_basis"] == "Premium"
    assert rows[0]["entered_premium"] == pytest.approx(premium)
    assert rows[0]["raw_volatility"] == pytest.approx(0.2)
    assert rows[0]["volatility_used"] == pytest.approx(
        snapshot["legs"][0]["volatility_used"]
    )


def test_asian_compact_meta_keeps_averaging_context():
    as_of = FrozenDate(2026, 7, 29)
    snapshot = pricer.calculate_structure(
        "asian76",
        pricer.default_context("asian76", as_of),
        {"structure_quantity": 1, "contract_multiplier": 1},
        [pricer.default_leg("asian76", 1)],
        as_of=as_of,
    )

    meta = pricer.render_structure_results(snapshot)[0]
    assert [item.children[0].children for item in meta] == ["T", "Vol adj"]
    assert snapshot["context"]["averaging_start_date"] in meta[0].title


def test_valuation_date_drives_time_basis_and_allows_past_or_future(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "calculate-button")
    params, param_ids, dates, date_ids = black_context_states()
    rows = two_leg_rows()[:1]

    past_snapshot, _ = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        1,
        rows,
        params,
        dates,
        "2026-01-29",
        param_ids,
        date_ids,
    )
    future_snapshot, _ = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        1,
        rows,
        params,
        dates,
        "2026-09-29",
        param_ids,
        date_ids,
    )

    assert past_snapshot["calculation_date"] == "2026-01-29"
    assert future_snapshot["calculation_date"] == "2026-09-29"
    assert past_snapshot["context"]["time_to_expiry"] == pytest.approx(
        273 / 365.25
    )
    assert future_snapshot["context"]["time_to_expiry"] == pytest.approx(
        30 / 365.25
    )
    assert past_snapshot["totals"]["trade_value"] != pytest.approx(
        future_snapshot["totals"]["trade_value"]
    )


def test_valuation_date_after_expiry_blocks_calculation(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "calculate-button")
    params, param_ids, dates, date_ids = black_context_states()
    snapshot, status = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        1,
        two_leg_rows()[:1],
        params,
        dates,
        "2026-10-29",
        param_ids,
        date_ids,
    )

    assert snapshot is None
    assert "after the valuation date" in status.children


def test_empty_and_stale_output_use_one_consolidated_message():
    empty = pricer.render_structure_results(None)
    assert empty == ("", "", "", "", "", "")

    stale = pricer.render_structure_results({"schema_version": 1})
    assert stale[0] == ""
    assert "stale" in stale[1].children
    assert stale[2:5] == ("", "", "")


def test_invalid_leg_clears_snapshot_and_exposes_actionable_error(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "calculate-button")
    params, param_ids, dates, date_ids = black_context_states()
    bad_rows = [{**two_leg_rows()[0], "volatility": None}]
    snapshot, status = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        1,
        bad_rows,
        params,
        dates,
        "2026-07-29",
        param_ids,
        date_ids,
    )
    assert snapshot is None
    assert "volatility is required" in status.children
    assert status.className.endswith("danger")


def test_structure_charts_only_expose_aggregate_traces(monkeypatch):
    snapshot, _status = calculate_two_leg_snapshot(monkeypatch)
    payoff = pricer.update_payoff_chart(snapshot, None, 50)
    assert [trace.name for trace in payoff.data] == ["Total expiration payoff"]

    volatility, rate, time, extension, correlation = (
        pricer.render_structure_sensitivity_charts(snapshot)
    )
    for figure in (volatility, time, extension):
        assert all("Leg " not in (trace.name or "") for trace in figure.data)
        assert any(trace.name == "Total structure value" for trace in figure.data)
    assert not rate.data
    assert "futures-style" in rate.layout.annotations[0].text
    assert "only available for Kirk" in correlation.layout.annotations[0].text


def test_model_change_clears_calculation_snapshot(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "option-type")
    output = pricer.calculate_structure_callback(
        1,
        "TTF",
        "kirk",
        1,
        [],
        [],
        [],
        "2026-07-29",
        [],
        [],
    )
    assert output[0] is None
    assert "Model changed" in output[1].children


def test_asset_change_clears_calculation_snapshot(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "pricer-asset")
    output = pricer.calculate_structure_callback(
        1,
        "Brent",
        "black76",
        1,
        two_leg_rows(),
        [],
        [],
        "2026-07-29",
        [],
        [],
    )
    assert output[0] is None
    assert output[1].children == "Modified · outputs cleared · calculate again"


@pytest.mark.parametrize(
    ("asset", "model", "expected"),
    [
        ("TTF", "black76", "futures_style"),
        ("JKM", "asian76", "futures_style"),
        ("HH", "black76", "upfront"),
        ("Brent", "black76", "futures_style"),
        ("NBP", "asian76", "futures_style"),
        ("HH", "kirk", "futures_style"),
    ],
)
def test_asset_change_selects_the_concrete_premium_default(asset, model, expected):
    assert pricer.select_asset_default_premium_convention(asset, model) == [expected]


@pytest.mark.parametrize(
    ("asset", "expected"),
    [
        ("TTF", "EUR/MWh"),
        ("JKM", "USD/MMBtu"),
        ("HH", "USD/MMBtu"),
        ("Brent", "USD/bbl"),
        ("NBP", "GBp/therm"),
    ],
)
def test_asset_change_updates_visible_price_unit(asset, expected):
    label, description = pricer.display_asset_price_unit(asset)
    assert label == expected
    assert description


def _contract_size_state(
    model,
    shape,
    *,
    delivery_year=None,
    delivery_month=None,
    expiration=None,
):
    param_values = [shape]
    param_ids = [
        pricer._context_id(
            model,
            "delivery_shape",
            structure_id="structure-1",
        )
    ]
    if delivery_year is not None:
        param_values.append(delivery_year)
        param_ids.append(
            pricer._context_id(
                model,
                "delivery_year",
                structure_id="structure-1",
            )
        )
    if delivery_month is not None:
        param_values.append(delivery_month)
        param_ids.append(
            pricer._context_id(
                model,
                "delivery_month",
                structure_id="structure-1",
            )
        )
    date_values = []
    date_ids = []
    if expiration is not None:
        date_values.append(expiration)
        date_ids.append(
            pricer._context_id(
                model,
                "expiration_date",
                is_date=True,
                structure_id="structure-1",
            )
        )
    return param_values, date_values, param_ids, date_ids


def test_exchange_contract_size_tracks_strip_when_current_value_is_automatic(
    monkeypatch,
):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: pricer._context_id(
            "asian76",
            "delivery_shape",
            structure_id="structure-1",
        ),
    )
    param_values, date_values, param_ids, date_ids = _contract_size_state(
        "asian76",
        "Q3",
        delivery_year=2027,
    )

    value, default_state = pricer.sync_exchange_contract_size(
        "JKM",
        "asian76",
        param_values,
        date_values,
        "2026-07-29",
        param_ids,
        date_ids,
        10_000,
        {"asset": "JKM", "value": 10_000},
    )

    assert value == pytest.approx(30_000.0)
    assert default_state == {"asset": "JKM", "value": 30_000.0}


def test_manual_contract_size_override_survives_delivery_change(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: pricer._context_id(
            "asian76",
            "delivery_shape",
            structure_id="structure-1",
        ),
    )
    param_values, date_values, param_ids, date_ids = _contract_size_state(
        "asian76",
        "Q3",
        delivery_year=2027,
    )

    value, default_state = pricer.sync_exchange_contract_size(
        "JKM",
        "asian76",
        param_values,
        date_values,
        "2026-07-29",
        param_ids,
        date_ids,
        12_500,
        {"asset": "JKM", "value": 10_000},
    )

    assert value is no_update
    assert default_state == {"asset": "JKM", "value": 30_000.0}


@pytest.mark.parametrize(
    ("current_size", "expected_value"),
    [(30_000, 31_000.0), (12_500, no_update)],
)
def test_nbp_contract_size_tracks_delivery_days_without_overwriting_manual_override(
    monkeypatch,
    current_size,
    expected_value,
):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: pricer._context_id(
            "black76",
            "delivery_month",
            structure_id="structure-1",
        ),
    )
    param_values, date_values, param_ids, date_ids = _contract_size_state(
        "black76",
        "MONTH",
        delivery_month="2027-01-01",
    )

    value, default_state = pricer.sync_exchange_contract_size(
        "NBP",
        "black76",
        param_values,
        date_values,
        "2026-07-29",
        param_ids,
        date_ids,
        current_size,
        {"asset": "NBP", "value": 30_000},
    )

    assert value is expected_value or value == pytest.approx(expected_value)
    assert default_state == {"asset": "NBP", "value": 31_000.0}


def test_asset_change_resets_contract_size_to_new_exchange_unit(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: pricer._instance_id("pricer-asset", "structure-1"),
    )
    param_values, date_values, param_ids, date_ids = _contract_size_state(
        "black76",
        "MONTH",
        expiration="2026-09-25",
    )

    value, default_state = pricer.sync_exchange_contract_size(
        "TTF",
        "black76",
        param_values,
        date_values,
        "2026-07-29",
        param_ids,
        date_ids,
        12_500,
        {"asset": "JKM", "value": 10_000},
    )

    assert value == pytest.approx(745.0)
    assert default_state == {"asset": "TTF", "value": 745.0}


def test_jkm_asset_change_defaults_to_average_price_option_model():
    assert pricer.select_asset_default_model("JKM") == "asian76"
    assert pricer.select_asset_default_model("TTF") is pricer.no_update
    assert (
        pricer.select_asset_default_model("JKM", "ICE-JKM-JKZ", "exchange")
        == "black76"
    )
    assert (
        pricer.select_asset_default_model("JKM", "ICE-JKM-APO", "exchange")
        == "asian76"
    )


def test_header_uses_the_selected_asset_default_without_a_saved_override():
    header = pricer._build_structure_header_context(
        "black76",
        "structure-1",
        None,
        "HH",
    )
    dropdowns = [
        item for root in header for item in walk(root) if isinstance(item, dcc.Dropdown)
    ]
    assert dropdowns[0].value == "upfront"


@pytest.mark.parametrize("model", ["black76", "asian76"])
def test_futures_style_rate_is_zero_and_disabled_in_context_form(model):
    form = pricer._build_context_form(
        model,
        "structure-1",
        {"premium_convention": "futures_style", "rate": 0.08},
        asset="TTF",
    )
    rate = next(
        item
        for item in walk(form)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("param") == "rate"
    )
    rate_field = next(
        item
        for item in walk(form)
        if isinstance(item, html.Label)
        and any(child is rate for child in walk(item))
    )

    assert rate.value == 0.0
    assert rate.disabled is True
    assert rate_field.title == pricer.FUTURES_STYLE_RATE_NOTE
    assert rate_field.children[0].title == pricer.FUTURES_STYLE_RATE_NOTE
    assert rate_field.children[-1].children == pricer.FUTURES_STYLE_RATE_NOTE


@pytest.mark.parametrize("model", ["black76", "asian76"])
def test_upfront_rate_remains_editable_in_context_form(model):
    form = pricer._build_context_form(
        model,
        "structure-1",
        {"premium_convention": "upfront", "rate": 0.08},
        asset="HH",
    )
    rate = next(
        item
        for item in walk(form)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("param") == "rate"
    )
    rate_field = next(
        item
        for item in walk(form)
        if isinstance(item, html.Label)
        and any(child is rate for child in walk(item))
    )

    assert rate.value == pytest.approx(0.08)
    assert rate.disabled is False
    assert rate_field.title == pricer.UPFRONT_RATE_NOTE


def test_premium_convention_controls_visible_risk_free_rate():
    assert pricer.sync_risk_free_rate_control("futures_style") == (0.0, True)

    upfront_value, upfront_disabled = pricer.sync_risk_free_rate_control("upfront")
    assert upfront_value is no_update
    assert upfront_disabled is False

    invalid_value, invalid_disabled = pricer.sync_risk_free_rate_control(None)
    assert invalid_value is no_update
    assert invalid_disabled is no_update
    assert pricer.sync_risk_free_rate_control(
        "futures_style",
        "CME-TTF-TTO",
        "exchange",
    ) == (no_update, False)


@pytest.mark.parametrize(
    ("premium_convention", "workflow", "expected"),
    [
        ("upfront", "exchange", {"display": "flex"}),
        ("futures_style", "exchange", {"display": "none"}),
        ("upfront", "otc", {}),
        ("futures_style", "otc", {}),
    ],
)
def test_exchange_rate_visibility_follows_mapping_premium(
    premium_convention, workflow, expected
):
    assert pricer.sync_exchange_rate_visibility(premium_convention, workflow) == expected


def test_premium_convention_change_clears_calculation_snapshot(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-context-param",
    )
    output = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        1,
        two_leg_rows(),
        [],
        [],
        "2026-07-29",
        [],
        [],
    )
    assert output[0] is None
    assert output[1].children == "Modified · outputs cleared · calculate again"


def test_pricing_input_change_clears_calculation_snapshot(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-legs-grid",
    )
    output = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        1,
        two_leg_rows(),
        [],
        [],
        "2026-07-29",
        [],
        [],
    )
    assert output[0] is None
    assert output[1].children == "Modified · outputs cleared · calculate again"


def test_initial_pricing_input_hydration_keeps_status_area_empty(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-legs-grid",
    )
    output = pricer.calculate_structure_callback(
        None,
        "TTF",
        "black76",
        1,
        two_leg_rows(),
        [],
        [],
        "2026-07-29",
        [],
        [],
    )
    assert output[0] is None
    assert output[1] == ""


def test_cell_edit_only_updates_draft_without_replacing_grid(monkeypatch):
    rows = two_leg_rows()
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "pricer-legs-grid")
    output = pricer.manage_structure_legs(
        "black76",
        None,
        None,
        None,
        {"value": 0.25},
        rows,
        [],
        {
            "schema_version": 1,
            "model": "black76",
            "legs": rows,
            "next_leg_sequence": 3,
        },
    )
    assert output[0] is no_update
    assert output[1] is no_update
    assert output[2] is no_update
    assert output[4]["legs"] == pricer._quote_ready_rows("black76", rows)


def test_legacy_rows_migrate_and_changing_quote_basis_clears_the_quote(monkeypatch):
    rows = two_leg_rows()[:1]
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "option-type")
    restored = pricer.manage_structure_legs(
        "black76",
        None,
        None,
        None,
        None,
        rows,
        [],
        {
            "schema_version": 1,
            "model": "black76",
            "legs": rows,
            "next_leg_sequence": 2,
        },
    )
    assert restored[2][0]["quote_basis"] == "VOL"
    assert restored[2][0]["quote_value"] == 0.19
    assert "volatility" not in restored[2][0]

    premium_rows = [
        {**restored[2][0], "quote_basis": "PREMIUM", "quote_value": 4.25}
    ]
    monkeypatch.setattr(
        pricer, "_get_pricer_triggered_id", lambda: "pricer-legs-grid"
    )
    changed = pricer.manage_structure_legs(
        "black76",
        None,
        None,
        None,
        [
            {
                "colId": "quote_basis",
                "oldValue": "VOL",
                "newValue": "PREMIUM",
                "data": premium_rows[0],
            }
        ],
        premium_rows,
        [],
        restored[4],
    )
    assert changed[2][0]["quote_basis"] == "PREMIUM"
    assert changed[2][0]["quote_value"] is None
    assert changed[4]["legs"] == changed[2]


def test_black_context_exposes_persisted_delivery_shape_and_year_controls():
    form = pricer._build_context_form("black76")
    delivery_year_field = component_by_pattern(
        "pricer-delivery-year-field",
        root=form,
    )
    winter_year_field = component_by_pattern(
        "pricer-delivery-year-field",
        root=pricer._build_context_form(
            "black76",
            values={"delivery_shape": "WIN", "delivery_year": 2028},
        ),
    )
    shape = next(
        item
        for item in walk(form)
        if getattr(item, "id", None)
        == {
            "type": "pricer-context-param",
            "structure_id": pricer.DEFAULT_STRUCTURE_ID,
            "model": "black76",
            "param": "delivery_shape",
        }
    )
    assert isinstance(shape, dcc.Dropdown)
    assert [option["value"] for option in shape.options] == [
        "MONTH",
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "SUM",
        "WIN",
    ]
    assert [option["label"] for option in shape.options] == [
        "Month",
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "Summer",
        "Winter",
    ]
    assert shape.persistence == "pricer-structure-1-black76-delivery-shape"
    assert delivery_year_field.children[0].children == "First year"
    assert delivery_year_field.style == {"display": "none"}
    assert winter_year_field.style == {}


def test_jkm_asian_context_enables_exchange_delivery_strips():
    form = pricer._build_context_form("asian76", asset="JKM")
    shape = next(
        item
        for item in walk(form)
        if getattr(item, "id", None)
        == {
            "type": "pricer-context-param",
            "structure_id": pricer.DEFAULT_STRUCTURE_ID,
            "model": "asian76",
            "param": "delivery_shape",
        }
    )
    delivery_year = next(
        item
        for item in walk(form)
        if getattr(item, "id", None)
        == {
            "type": "pricer-context-param",
            "structure_id": pricer.DEFAULT_STRUCTURE_ID,
            "model": "asian76",
            "param": "delivery_year",
        }
    )

    assert shape.disabled is False
    assert [option["value"] for option in shape.options] == [
        "MONTH",
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "SUM",
        "WIN",
    ]
    assert delivery_year.value >= dt.date.today().year
    assert isinstance(delivery_year, dcc.Input)
    assert delivery_year.step == 1
    assert delivery_year.persistence_type == "session"
    assert pricer.toggle_delivery_year_field("MONTH") == {"display": "none"}
    assert pricer.toggle_delivery_year_field("Q1") == {}
    assert pricer.toggle_delivery_year_field("SUM") == {}
    assert pricer.toggle_delivery_year_field("WIN") == {}


def test_jkm_asian_month_context_keeps_otc_governed_defaults_editable():
    values = {"delivery_shape": "MONTH", "delivery_month": "2027-01-01"}
    header = html.Div(
        pricer._build_structure_header_context(
            "asian76",
            values=values,
            asset="JKM",
        )
    )
    form = pricer._build_context_form(
        "asian76",
        values=values,
        asset="JKM",
    )
    delivery = next(
        item
        for item in walk(header)
        if getattr(item, "id", None)
        == {
            "type": "pricer-context-param",
            "structure_id": pricer.DEFAULT_STRUCTURE_ID,
            "model": "asian76",
            "param": "delivery_month",
        }
    )
    dates = {
        item.id["param"]: item
        for item in walk(form)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-context-date"
        and item.id.get("model") == "asian76"
    }

    assert delivery.value == "2027-01-01"
    assert delivery.disabled is False
    assert {option["value"] for option in delivery.options} >= {"2027-01-01"}
    assert [
        item.children[0].children
        for item in header.children
        if isinstance(item, html.Label)
    ] == ["Premium", "Shape", "Delivery"]
    assert not any(
        isinstance(getattr(item, "id", None), dict)
        and item.id.get("param") == "delivery_month"
        for item in walk(form)
    )
    assert dates["averaging_start_date"].date == "2026-11-16"
    assert dates["expiration_date"].date == "2026-12-15"
    assert dates["contract_expiration_date"].date == "2026-12-15"
    assert not any(item.disabled for item in dates.values())


def test_jkm_apo_exchange_month_locks_all_governed_dates():
    form = pricer._build_context_form(
        "asian76",
        values={"delivery_shape": "MONTH", "delivery_month": "2027-01-01"},
        asset="JKM",
        mapping_id="ICE-JKM-APO",
    )
    dates = [
        item
        for item in walk(form)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-context-date"
        and item.id.get("model") == "asian76"
    ]

    assert len(dates) == 3
    assert all(item.disabled for item in dates)


@pytest.mark.parametrize(
    ("asset", "model"),
    [
        ("TTF", "black76"),
        ("JKM", "asian76"),
        ("HH", "black76"),
        ("Brent", "black76"),
        ("NBP", "black76"),
    ],
)
def test_every_asset_model_header_has_delivery_immediately_after_shape(asset, model):
    header = pricer._build_structure_header_context(
        model,
        values={"delivery_shape": "MONTH"},
        asset=asset,
    )
    labels = [
        item.children[0].children
        for item in header
        if isinstance(item, html.Label)
    ]
    delivery = next(
        item
        for root in header
        for item in walk(root)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("param") == "delivery_month"
    )

    assert labels == ["Premium", "Shape", "Delivery"]
    assert delivery.id["model"] == model
    assert delivery.disabled is False
    assert delivery.options


def test_jkm_month_delivery_callbacks_preserve_selection_and_sync_dates():
    options, selected, disabled, style = pricer.sync_delivery_month_control(
        "JKM",
        "asian76",
        ["MONTH"],
        "2026-08-21",
        None,
        ["2027-01-01"],
    )
    options = options[0]
    selected = selected[0]
    disabled = disabled[0]

    assert selected == "2027-01-01"
    assert disabled is False
    assert style == {}
    assert {option["value"] for option in options} >= {"2027-01-01"}
    assert pricer.sync_asian76_dates(
        "2026-09-01",
        "2026-10-01",
        "2027-01-01",
        "JKM",
        None,
        ["MONTH"],
        selected,
        "2026-08-21",
    ) == (
        no_update,
        no_update,
        "2026-09-01",
        None,
        "2026-10-01",
        no_update,
        "2026-10-01",
        False,
        False,
        False,
    )

    strip = pricer.sync_delivery_month_control(
        "JKM",
        "asian76",
        ["Q1"],
        "2026-08-21",
        None,
        [selected],
    )
    assert strip[1:] == (
        ["2027-01-01"],
        [True],
        {"display": "none"},
    )


def test_asian76_governed_date_callback_passes_mapping_id():
    from app import app

    app._setup_server()
    callback = next(
        item
        for output, item in app.callback_map.items()
        if "asian76" in output and "contract_expiration_date" in output
    )
    input_types = [json.loads(item["id"])["type"] for item in callback["inputs"]]

    assert "pricer-mapping-id" in input_types
    assert len(callback["inputs"]) == len(
        inspect.signature(pricer.sync_asian76_dates).parameters
    )


def test_model_dependent_callbacks_ignore_transient_hydration_state():
    assert pricer.manage_structure_legs(
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ) == (no_update,) * 8
    assert pricer.sync_delivery_month_control(
        "HH",
        None,
        ["MONTH"],
        "2026-08-27",
        "CME-HH-ON",
        ["2026-10-01"],
    ) == ([no_update], [no_update], [no_update], no_update)


def test_otc_delivery_callback_keeps_contract_dates_editable():
    assert pricer.sync_black76_contract_expiration_date(
        "2027-01-01",
        "2027-01-31",
        "TTF",
        None,
        ["MONTH"],
        "2026-09-01",
        "2026-08-21",
    ) == (
        no_update,
        None,
        no_update,
        "2027-01-01",
        False,
    )


@pytest.mark.parametrize(
    ("model", "asset", "expected_month_only_fields"),
    [
        ("black76", "TTF", 2),
        ("asian76", "JKM", 3),
    ],
)
def test_delivery_strip_hides_manual_date_fields(
    model,
    asset,
    expected_month_only_fields,
):
    month_form = pricer._build_context_form(
        model,
        values={"delivery_shape": "MONTH"},
        asset=asset,
    )
    strip_form = pricer._build_context_form(
        model,
        values={"delivery_shape": "Q3", "delivery_year": 2027},
        asset=asset,
    )

    def month_only_fields(form):
        return [
            item
            for item in walk(form)
            if isinstance(getattr(item, "id", None), dict)
            and item.id.get("type") == "pricer-month-only-field"
        ]

    month_fields = month_only_fields(month_form)
    strip_fields = month_only_fields(strip_form)
    assert len(month_fields) == expected_month_only_fields
    assert len(strip_fields) == expected_month_only_fields
    assert all(item.style == {} for item in month_fields)
    assert all(item.style == {"display": "none"} for item in strip_fields)

    field_ids = [item.id for item in strip_fields]
    assert pricer.toggle_month_only_fields(["MONTH"], field_ids) == [
        {}
    ] * expected_month_only_fields
    assert pricer.toggle_month_only_fields(["Q3"], field_ids) == [
        {"display": "none"}
    ] * expected_month_only_fields


def test_month_signature_ignores_hidden_delivery_year_but_strip_signature_keeps_it():
    month_2027 = pricer._normalized_signature_context(
        {"delivery_shape": "MONTH", "delivery_year": 2027, "forward": 100}
    )
    month_2030 = pricer._normalized_signature_context(
        {"delivery_shape": "MONTH", "delivery_year": 2030, "forward": 100}
    )
    strip_2027 = pricer._normalized_signature_context(
        {"delivery_shape": "WIN", "delivery_year": 2027, "forward": 100}
    )

    assert month_2027 == month_2030
    assert "delivery_year" not in month_2027
    assert strip_2027["delivery_year"] == 2027


def test_strip_signature_ignores_hidden_manual_expiration_dates():
    first = pricer._normalized_signature_context(
        {
            "delivery_shape": "Q1",
            "delivery_year": 2027,
            "averaging_start_date": "2026-08-16",
            "expiration_date": "2026-09-30",
            "contract_expiration_date": "2026-10-31",
            "delivery_month": "2027-01-01",
        }
    )
    second = pricer._normalized_signature_context(
        {
            "delivery_shape": "Q1",
            "delivery_year": 2027,
            "averaging_start_date": "2027-01-02",
            "expiration_date": "2027-01-15",
            "contract_expiration_date": "2027-02-15",
            "delivery_month": "2028-01-01",
        }
    )

    assert first == second == {"delivery_shape": "Q1", "delivery_year": 2027}


def test_ttf_sum27_callback_renders_monthly_component_audit(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "calculate-button")
    param_values = ["SUM", 2027, 42.9, 0.05]
    param_ids = [
        {
            "type": "pricer-context-param",
            "model": "black76",
            "param": "delivery_shape",
        },
        {
            "type": "pricer-context-param",
            "model": "black76",
            "param": "delivery_year",
        },
        {
            "type": "pricer-context-param",
            "model": "black76",
            "param": "forward",
        },
        {
            "type": "pricer-context-param",
            "model": "black76",
            "param": "rate",
        },
    ]
    dates = ["2027-12-31", "2027-12-31"]
    date_ids = [
        {
            "type": "pricer-context-date",
            "model": "black76",
            "param": "expiration_date",
        },
        {
            "type": "pricer-context-date",
            "model": "black76",
            "param": "contract_expiration_date",
        },
    ]
    snapshot, status = pricer.calculate_structure_callback(
        1,
        "TTF",
        "black76",
        1,
        [
            {
                "leg_id": "leg-1",
                "name": "40 put",
                "side": "BUY",
                "ratio": 1,
                "call_put": "P",
                "strike": 40,
                "volatility": 49.56,
            }
        ],
        param_values,
        dates,
        "2026-08-21",
        param_ids,
        date_ids,
    )

    assert snapshot["totals"]["unit_structure_value"] == pytest.approx(
        5.894909897031116
    )
    assert status.children == "Calculated · 1 leg · Black-76 · 6 months"
    rendered = pricer.render_structure_results(snapshot)
    assert [item.children[0].children for item in rendered[0]] == ["T", "Vol adj"]
    assert snapshot["context"]["delivery_component_count"] == 6
    assert snapshot["context"]["margin_style"] == "futures_style"
    assert pricer.FUTURES_STYLE_RATE_NOTE in snapshot["warnings"]
    warning_badges = rendered[5]
    assert len(warning_badges) == 1
    assert all(
        badge.children != pricer.FUTURES_STYLE_RATE_NOTE
        for badge in warning_badges
    )
    assert all(badge.title == badge.children for badge in warning_badges)
    strip_details = next(
        item for item in walk(rendered[1]) if isinstance(item, html.Details)
    )
    assert getattr(strip_details, "open", None) in (None, False)
    assert isinstance(strip_details.children[0], html.Summary)
    assert strip_details.children[0].children == "Monthly strip components"
    assert strip_details.children[1].className == "pricer-strip-details-content"
    component_grid = next(
        item
        for item in walk(rendered[1])
        if getattr(item, "id", None)
        == {
            "type": "pricer-strip-components-grid",
            "structure_id": pricer.DEFAULT_STRUCTURE_ID,
        }
    )
    assert len(component_grid.rowData) == 6
    assert component_grid.rowData[0]["contract_month"] == "Apr-27"
    assert sum(row["strip_weight_pct"] for row in component_grid.rowData) == (
        pytest.approx(100.0)
    )
    assert pricer.sync_payoff_valuation_limit(snapshot, None)[:2] == (
        "2026-08-21",
        "2027-03-25",
    )
    payoff = pricer.update_payoff_chart(snapshot, None, 50)
    assert [trace.name for trace in payoff.data] == [
        "Total structure value",
        "Parallel strip intrinsic benchmark",
        "Selected Valuation",
    ]


@pytest.mark.parametrize(
    "hydration_trigger",
    [
        None,
        {"type": "pricer-option-type", "structure_id": "structure-1"},
    ],
)
def test_same_model_hydration_is_a_noop_for_context_legs_and_snapshot(
    monkeypatch,
    hydration_trigger,
):
    state = model_instance_state("black76")
    state["rows"] = [
        pricer.default_leg("black76", 1),
        pricer.default_leg("black76", 2),
    ]
    set_context_state_value(state, "forward", 123.5)
    snapshot, _status = invoke_instance_state(
        monkeypatch,
        state,
        trigger={"type": "pricer-calculate-button", "structure_id": "structure-1"},
    )
    context = pricer._context_from_states(
        state["model"],
        state["param_values"],
        state["param_ids"],
        state["date_values"],
        state["date_ids"],
    )
    draft = {
        "schema_version": 1,
        "model": state["model"],
        "context": copy.deepcopy(context),
        "legs": copy.deepcopy(state["rows"]),
        "next_leg_sequence": 3,
    }
    original_draft = copy.deepcopy(draft)
    original_snapshot = copy.deepcopy(snapshot)
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: hydration_trigger,
    )

    leg_outputs = pricer.manage_structure_legs(
        state["model"],
        None,
        None,
        None,
        None,
        copy.deepcopy(state["rows"]),
        [],
        draft,
        pricer._instance_id("pricer-option-type", "structure-1"),
    )
    calculation_outputs = pricer.calculate_structure_instance_callback(
        1,
        7,
        state["asset"],
        state["model"],
        state["contract_multiplier"],
        state["rows"],
        None,
        state["param_values"],
        state["date_values"],
        state["valuation_date"],
        state["param_ids"],
        state["date_ids"],
        snapshot,
        7,
    )

    assert leg_outputs == (no_update,) * 8
    assert calculation_outputs == (no_update,) * 4
    assert draft == original_draft
    assert snapshot == original_snapshot
    assert snapshot["context"]["forward"] == pytest.approx(123.5)
    assert len(snapshot["legs"]) == 2


def test_actual_model_switch_replaces_columns_rows_context_and_column_state(
    monkeypatch,
):
    black_rows = [
        pricer.default_leg("black76", 1),
        pricer.default_leg("black76", 2),
    ]
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: {"type": "pricer-option-type", "structure_id": "structure-1"},
    )

    outputs = pricer.manage_structure_legs(
        "kirk",
        None,
        None,
        None,
        None,
        black_rows,
        [],
        {
            "schema_version": 1,
            "model": "black76",
            "context": {"forward": 123.5},
            "legs": copy.deepcopy(black_rows),
            "next_leg_sequence": 3,
        },
        pricer._instance_id("pricer-option-type", "structure-1"),
    )

    context_children, columns, rows, selected, draft, status, header, reset = outputs
    context_ids = {
        item.id["model"]
        for root in context_children
        for item in walk(root)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") in {"pricer-context-param", "pricer-context-date"}
    }
    assert context_ids == {"kirk"}
    assert columns == pricer._leg_column_defs("kirk")
    assert rows == [pricer.default_leg("kirk", 1)]
    assert selected == []
    assert draft == {
        "schema_version": 1,
        "model": "kirk",
        "context": None,
        "legs": rows,
        "next_leg_sequence": 2,
    }
    assert status == ""
    header_params = [
        item.id["param"]
        for root in header
        for item in walk(root)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-context-param"
    ]
    assert header_params == [
        "asset_1_code",
        "asset_2_code",
        "premium_convention",
    ]
    header_dropdowns = [
        item for root in header for item in walk(root) if isinstance(item, dcc.Dropdown)
    ]
    assert [item.value for item in header_dropdowns] == [
        None,
        None,
        "futures_style",
    ]
    assert [option["value"] for option in header_dropdowns[2].options] == [
        "futures_style",
    ]
    assert reset is True


def test_switching_away_from_kirk_removes_two_asset_context_from_visible_state(
    monkeypatch,
):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: {"type": "pricer-option-type", "structure_id": "structure-1"},
    )
    kirk_rows = [pricer.default_leg("kirk", 1)]
    outputs = pricer.manage_structure_legs(
        "black76",
        None,
        None,
        None,
        None,
        kirk_rows,
        [],
        {
            "schema_version": 1,
            "model": "kirk",
            "context": {
                "asset_1_code": "JKM",
                "asset_2_code": "HH",
                "asset_1_forward": 12.0,
                "asset_2_forward": 4.0,
            },
            "legs": kirk_rows,
            "next_leg_sequence": 2,
        },
        pricer._instance_id("pricer-option-type", "structure-1"),
        "TTF",
    )

    visible_params = {
        item.id["param"]
        for root in (outputs[0], outputs[6])
        for item in walk(root)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type")
        in {"pricer-context-param", "pricer-context-date"}
    }
    assert not {
        "asset_1_code",
        "asset_2_code",
        "asset_1_forward",
        "asset_2_forward",
        "asset_1_reference_expiry",
        "asset_2_reference_expiry",
        "contractual_expiry",
    } & visible_params
    assert outputs[4]["model"] == "black76"
    assert outputs[4]["context"] is None


def test_calculate_all_baseline_blocks_stale_replay_and_consumes_once(monkeypatch):
    state = model_instance_state("black76")
    panel = pricer._build_structure_panel(
        {
            "structure_id": "structure-1",
            "label": "Structure 1",
            "template": None,
        },
        calculate_all_baseline=9,
    )
    assert component_by_pattern(
        "pricer-calculate-all-baseline",
        root=panel,
    ).data == 9

    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: None)
    assert pricer.calculate_structure_instance_callback(
        0,
        9,
        state["asset"],
        state["model"],
        state["contract_multiplier"],
        state["rows"],
        None,
        state["param_values"],
        state["date_values"],
        state["valuation_date"],
        state["param_ids"],
        state["date_ids"],
        None,
        9,
    ) == (no_update,) * 4

    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-calculate-all",
    )
    assert pricer.calculate_structure_instance_callback(
        0,
        8,
        state["asset"],
        state["model"],
        state["contract_multiplier"],
        state["rows"],
        None,
        state["param_values"],
        state["date_values"],
        state["valuation_date"],
        state["param_ids"],
        state["date_ids"],
        None,
        9,
    ) == (no_update,) * 4

    snapshot, status, grid_options, baseline = (
        pricer.calculate_structure_instance_callback(
            0,
            10,
            state["asset"],
            state["model"],
            state["contract_multiplier"],
            state["rows"],
            None,
            state["param_values"],
            state["date_values"],
            state["valuation_date"],
            state["param_ids"],
            state["date_ids"],
            None,
            9,
        )
    )
    assert snapshot["model"] == "black76"
    assert status.className.endswith("success")
    assert grid_options["pinnedBottomRowData"]
    assert baseline == 10
    assert pricer.calculate_structure_instance_callback(
        0,
        10,
        state["asset"],
        state["model"],
        state["contract_multiplier"],
        state["rows"],
        None,
        state["param_values"],
        state["date_values"],
        state["valuation_date"],
        state["param_ids"],
        state["date_ids"],
        snapshot,
        baseline,
    ) == (no_update,) * 4


def test_exchange_calculate_all_routes_only_to_exchange_structures(monkeypatch):
    state = model_instance_state("black76")
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-exchange-calculate-all",
    )
    snapshot, status, _grid_options, baseline = (
        pricer.calculate_structure_instance_callback(
            0,
            0,
            state["asset"],
            state["model"],
            state["contract_multiplier"],
            state["rows"],
            None,
            state["param_values"],
            state["date_values"],
            state["valuation_date"],
            state["param_ids"],
            state["date_ids"],
            None,
            exchange_calculate_all_clicks=1,
            workflow="exchange",
        )
    )
    assert snapshot["model"] == "black76"
    assert status.className.endswith("success")
    assert baseline == 1

    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-calculate-all",
    )
    assert pricer.calculate_structure_instance_callback(
        0,
        1,
        state["asset"],
        state["model"],
        state["contract_multiplier"],
        state["rows"],
        None,
        state["param_values"],
        state["date_values"],
        state["valuation_date"],
        state["param_ids"],
        state["date_ids"],
        None,
        exchange_calculate_all_clicks=1,
        workflow="exchange",
    ) == (no_update,) * 4


def test_exchange_mapping_id_restores_registry_identity_and_contract_size():
    panel = pricer._build_structure_panel(
        {
            "structure_id": "exchange-structure-9",
            "label": "E9",
            "template": {"mapping_id": "CME-HH-ON"},
        },
        workflow="exchange",
        signed_lots=True,
        use_published_surface=True,
    )
    components = list(walk(panel))
    mapping_selector = next(
        item
        for item in components
        if isinstance(item, dcc.Dropdown)
        and item.id.get("type") == "pricer-mapping-id"
    )
    asset_selector = next(
        item
        for item in components
        if isinstance(item, dcc.Dropdown)
        and item.id.get("type") == "pricer-asset"
    )
    contract_size = next(
        item
        for item in components
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-contract-multiplier"
    )
    asset_field = next(
        item
        for item in components
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-asset-field"
    )
    model_selector = next(
        item
        for item in components
        if isinstance(item, dcc.Dropdown)
        and item.id.get("type") == "pricer-option-type"
    )
    price_unit = next(
        item
        for item in components
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-price-unit"
    )

    assert mapping_selector.value == "CME-HH-ON"
    assert len(mapping_selector.options) == 19
    assert mapping_selector.persistence == (
        "pricer-exchange-structure-9-mapping-id-v2"
    )
    assert asset_selector.value == "HH"
    assert asset_field.style == {"display": "none"}
    assert model_selector.value == "american_futures"
    assert model_selector.disabled is True
    assert model_selector.options == [
        {"label": "American futures", "value": "american_futures"}
    ]
    assert price_unit.children == "USD/MMBtu"
    assert contract_size.value == 10_000.0


def test_american_futures_is_not_exposed_in_the_otc_model_selector():
    panel = pricer._build_structure_panel(
        {"structure_id": "otc-structure-1", "label": "O1", "template": None},
        workflow="otc",
    )
    model_selector = component_by_pattern(
        "pricer-option-type", "otc-structure-1", panel
    )

    assert model_selector.disabled is False
    assert "american_futures" not in {
        option["value"] for option in model_selector.options
    }


def test_hh_exchange_default_is_the_ready_cme_lne_mapping():
    mapping = pricer.exchange_mapping_for_asset_model("HH", "black76")
    assert mapping.mapping_id == "CME-HH-LNE"
    assert mapping.contract_size == 10_000.0
    assert mapping.premium_convention == "upfront"
    assert pricer.exchange_mapping_pricing_supported("CME-HH-LNE")
    assert pricer.exchange_mapping_pricing_supported("ICE-HH-CURRENT")
    assert pricer.exchange_option_mapping("ICE-HH-CURRENT").mapping_id == (
        "ICE-HH-PHE"
    )
    assert pricer.exchange_mapping_pricing_supported("CME-HH-ON")


def test_ice_brent_uses_the_governed_futures_style_black76_equivalent():
    mapping = pricer.exchange_option_mapping("ICE-BRENT-B")

    assert mapping.asset == "Brent"
    assert mapping.model == "black76"
    assert mapping.premium_convention == "futures_style"
    assert mapping.contract_size == 1_000.0
    assert mapping.implementation_status == "Ready"
    assert mapping.pricing_supported
    assert pricer.exchange_mapping_for_asset_model("Brent", "black76") == mapping


def test_cme_bzo_is_ready_with_the_futures_style_brent_proxy_workflow():
    mapping = pricer.exchange_option_mapping("CME-BRENT-BZO")

    assert mapping.asset == "Brent"
    assert mapping.model == "black76"
    assert mapping.premium_convention == "futures_style"
    assert mapping.contract_size == 1_000.0
    assert mapping.implementation_status == "Ready"
    assert mapping.pricing_supported


def test_exchange_registry_premium_and_size_override_asset_defaults(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: {"type": "pricer-mapping-id"},
    )
    assert pricer.select_asset_default_premium_convention(
        "TTF",
        "black76",
        mapping_id="CME-TTF-TTO",
        workflow="exchange",
    ) == ["upfront"]
    value, default_state = pricer.sync_exchange_contract_size(
        "HH",
        "black76",
        [],
        [],
        "2026-07-29",
        [],
        [],
        2_500.0,
        {"asset": "HH", "mapping_id": "ICE-HH-CURRENT", "value": 2_500.0},
        mapping_id="CME-HH-ON",
        workflow="exchange",
    )
    assert value == 10_000.0
    assert default_state == {
        "asset": "HH",
        "mapping_id": "CME-HH-ON",
        "value": 10_000.0,
    }


@pytest.mark.parametrize(
    ("mapping_id", "delivery_month", "premium", "expected_size"),
    [
        ("ICE-HH-PHE", "2027-01-01", "upfront", 2_500.0),
        ("CME-BRENT-BE", "2027-01-01", "upfront", 1_000.0),
        ("CME-TTF-TFO", "2027-01-01", "futures_style", 744.0),
        ("CME-TTF-TTO", "2027-01-01", "upfront", 744.0),
        ("CME-NBP-UKO", "2027-01-01", "upfront", 31_000.0),
        ("CME-NBP-UFO", "2027-01-01", "futures_style", 31_000.0),
        ("CME-TTF-TTL", "2027-01-01", "futures_style", 744.0),
        ("CME-TTF-TFP", "2027-01-01", "upfront", 10_000.0),
        ("CME-TTF-TFF", "2027-01-01", "futures_style", 10_000.0),
        ("CME-JKM-JKO", "2027-03-01", "upfront", 10_000.0),
        ("CME-JKM-JFO", "2027-03-01", "futures_style", 10_000.0),
        ("CME-HH-ON", "2027-01-01", "upfront", 10_000.0),
    ],
)
def test_ready_exchange_mapping_initializes_premium_rate_size_and_governed_dates(
    mapping_id,
    delivery_month,
    premium,
    expected_size,
):
    mapping = pricer.exchange_option_mapping(mapping_id)
    panel = pricer._build_structure_panel(
        {
            "structure_id": "exchange-structure-1",
            "label": "E1",
            "template": {
                "mapping_id": mapping_id,
                "valuation_date": "2026-08-27",
                "context": {
                    "delivery_shape": "MONTH",
                    "delivery_month": delivery_month,
                    "expiration_date": "2027-01-31",
                    "contract_expiration_date": "2027-01-31",
                    "rate": 0.05,
                },
            },
        },
        workflow="exchange",
        signed_lots=True,
        use_published_surface=True,
    )
    premium_input = next(
        item
        for item in walk(panel)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("param") == "premium_convention"
    )
    rate_input = next(
        item
        for item in walk(panel)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("param") == "rate"
    )
    dates = {
        item.id["param"]: item
        for item in walk(panel)
        if isinstance(getattr(item, "id", None), dict)
        and item.id.get("type") == "pricer-context-date"
        and item.id.get("model") == mapping.model
    }
    expected_component = pricer.build_delivery_month_component(
        mapping.asset,
        mapping.model,
        delivery_month,
        dt.date(2026, 8, 27),
        mapping_id=mapping_id,
    )

    assert premium_input.value == premium
    assert rate_input.disabled is (premium == "futures_style")
    assert component_by_pattern(
        "pricer-contract-multiplier",
        "exchange-structure-1",
        panel,
    ).value == pytest.approx(expected_size)
    assert dates["expiration_date"].date == expected_component[
        "option_expiration_date"
    ]
    assert dates["contract_expiration_date"].date == expected_component[
        "contract_expiration_date"
    ]
    assert dates["expiration_date"].disabled is True
    assert dates["contract_expiration_date"].disabled is True


def test_workspace_and_direct_panel_migrate_the_retired_phe_mapping_id():
    legacy_template = {
        "mapping_id": "ICE-HH-CURRENT",
        "asset": "HH",
        "model": "black76",
        "contract_multiplier": 1.0,
        "context": {
            "exchange_mapping_id": "ICE-HH-CURRENT",
            "premium_convention": "upfront",
        },
    }
    normalized = pricer._normalize_workspace(
        {
            "schema_version": 7,
            "next_structure_sequence": 2,
            "structures": [
                {
                    "structure_id": "structure-1",
                    "label": "S1",
                    "template": copy.deepcopy(legacy_template),
                }
            ],
            "drafts": {"structure-1": copy.deepcopy(legacy_template)},
        }
    )

    assert normalized["schema_version"] == 9
    for template in (
        normalized["structures"][0]["template"],
        normalized["drafts"]["structure-1"],
    ):
        assert template["mapping_id"] == "ICE-HH-PHE"
        assert template["context"]["exchange_mapping_id"] == "ICE-HH-PHE"
        assert template["contract_multiplier"] == 2_500.0

    panel = pricer._build_structure_panel(
        {
            "structure_id": "exchange-structure-1",
            "label": "E1",
            "template": copy.deepcopy(legacy_template),
        },
        workflow="exchange",
        signed_lots=True,
        use_published_surface=True,
    )
    assert component_by_pattern(
        "pricer-mapping-id",
        "exchange-structure-1",
        panel,
    ).value == "ICE-HH-PHE"


def test_all_registry_mappings_are_ready_for_pricing():
    options = pricer.exchange_mapping_options()

    assert len(options) == 19
    assert all(
        pricer.exchange_mapping_pricing_supported(option["value"])
        for option in options
    )


def test_corrupt_persisted_snapshot_is_pruned_and_panel_recovers(monkeypatch):
    state = model_instance_state("black76")
    context = pricer._context_from_states(
        state["model"],
        state["param_values"],
        state["param_ids"],
        state["date_values"],
        state["date_ids"],
    )
    template = {
        "asset": state["asset"],
        "model": state["model"],
        "contract_multiplier": state["contract_multiplier"],
        "valuation_date": state["valuation_date"],
        "context": context,
        "legs": copy.deepcopy(state["rows"]),
        "next_leg_sequence": 2,
    }
    corrupt_snapshot = {
        "schema_version": pricer.SCHEMA_VERSION - 1,
        "model": "black76",
        "context": {},
        "legs": [],
        "totals": {},
    }

    panel = pricer._build_structure_panel(
        {
            "structure_id": "structure-1",
            "label": "Structure 1",
            "template": template,
        },
        calculation_snapshot=corrupt_snapshot,
    )

    assert component_by_pattern("pricer-calculation-store", root=panel).data is None
    grid = component_by_pattern("pricer-legs-grid", root=panel)
    assert grid.rowData == pricer._quote_ready_rows("black76", state["rows"])
    assert grid.dashGridOptions["context"]["pricingRows"] == {}
    assert grid.dashGridOptions["pinnedBottomRowData"] == []
    assert "outputs cleared" in component_by_pattern(
        "pricer-calculation-status",
        root=panel,
    ).children.children
    assert pricer.persist_pricer_calculations(
        pricer._default_workspace(),
        [None],
        [pricer._instance_id("pricer-calculation-store", "structure-1")],
        [""],
        [pricer._instance_id("pricer-calculation-status", "structure-1")],
        {"structure-1": corrupt_snapshot},
    ) == {}


def test_current_schema_snapshot_with_missing_nested_fields_is_rejected():
    corrupt_snapshot = {
        "schema_version": pricer.SCHEMA_VERSION,
        "model": "black76",
        "model_label": "Black-76",
        "calculation_date": "2026-07-29",
        "context": {},
        "legs": [{}],
        "totals": {},
        "greek_fields": [],
        "greek_labels": {},
    }

    assert pricer._is_valid_calculation_snapshot(corrupt_snapshot) is False
    assert "stale" in pricer.render_structure_results(corrupt_snapshot)[1].children


def test_unchanged_selected_snapshot_routes_no_update(monkeypatch):
    snapshot, _status = calculate_instance(monkeypatch, "structure-1")
    equal_snapshot = copy.deepcopy(snapshot)

    routed = pricer.route_selected_structure_calculation(
        "structure-1",
        [equal_snapshot],
        [pricer._instance_id("pricer-calculation-store", "structure-1")],
        current_routed_snapshot=snapshot,
    )

    assert equal_snapshot is not snapshot
    assert routed is no_update


def test_invalid_persisted_rows_restore_valid_draft_and_bad_draft_types_are_pruned(
    monkeypatch,
):
    saved_rows = [
        pricer.default_leg("black76", 1),
        pricer.default_leg("black76", 2),
    ]
    saved_context = {"forward": 123.5}
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: None)

    outputs = pricer.manage_structure_legs(
        "black76",
        None,
        None,
        None,
        None,
        {"corrupt": "rowData"},
        [],
        {
            "schema_version": 1,
            "model": "black76",
            "context": copy.deepcopy(saved_context),
            "legs": copy.deepcopy(saved_rows),
            "next_leg_sequence": 3,
        },
        pricer._instance_id("pricer-option-type", "structure-1"),
    )

    assert outputs[2] == pricer._quote_ready_rows("black76", saved_rows)
    assert outputs[4]["context"] == saved_context
    assert len(outputs[4]["legs"]) == 2
    assert outputs[5] == "Invalid saved leg state was reset."
    assert outputs[7] is True

    normalized = pricer._normalize_workspace(
        {
            "schema_version": pricer.PRICER_WORKSPACE_SCHEMA_VERSION,
            "next_structure_sequence": 2,
            "structures": [
                {
                    "structure_id": "structure-1",
                    "label": "Structure 1",
                    "template": None,
                }
            ],
            "drafts": {
                "structure-1": "corrupt draft",
                "removed-structure": {"model": "black76"},
            },
        }
    )
    assert normalized["drafts"] == {}
    assert normalized["structures"][0]["label"] == "S1"


def test_legacy_settlement_workspace_migrates_to_asset_default_convention():
    legacy_template = {
        "structure_type": "Physical",
        "asset": "TTF",
        "model": "black76",
        "context": {
            "structure_type": "Physical",
            "premium_convention": "product_default",
            "forward": 42.9,
        },
        "legs": [pricer.default_leg("black76", 1)],
    }
    normalized = pricer._normalize_workspace(
        {
            "schema_version": 1,
            "next_structure_sequence": 2,
            "structures": [
                {
                    "structure_id": "structure-1",
                    "label": "Structure 1",
                    "template": copy.deepcopy(legacy_template),
                }
            ],
            "drafts": {"structure-1": copy.deepcopy(legacy_template)},
        }
    )

    assert normalized["schema_version"] == pricer.PRICER_WORKSPACE_SCHEMA_VERSION
    for template in (
        normalized["structures"][0]["template"],
        normalized["drafts"]["structure-1"],
    ):
        assert "structure_type" not in template
        assert "structure_type" not in template["context"]
        assert template["context"]["premium_convention"] == "futures_style"
        assert template["context"]["delivery_shape"] == "MONTH"


@pytest.mark.parametrize(
    ("asset", "context", "expected"),
    [
        (
            "TTF",
            {"delivery_shape": "MONTH", "expiration_date": "2026-09-25"},
            745.0,
        ),
        (
            "JKM",
            {"delivery_shape": "Q3", "delivery_year": 2027},
            30_000.0,
        ),
        ("Brent", {"delivery_shape": "MONTH"}, 1_000.0),
        ("HH", {"delivery_shape": "MONTH"}, 2_500.0),
        (
            "NBP",
            {"delivery_shape": "MONTH", "delivery_month": "2026-11-01"},
            30_000.0,
        ),
    ],
)
def test_legacy_default_multiplier_migrates_to_exchange_contract_size(
    asset,
    context,
    expected,
):
    template = {
        "asset": asset,
        "model": "asian76" if asset == "JKM" else "black76",
        "contract_multiplier": 1,
        "valuation_date": "2026-07-29",
        "context": context,
        "legs": [
            pricer.default_leg(
                "asian76" if asset == "JKM" else "black76",
                1,
            )
        ],
    }
    normalized = pricer._normalize_workspace(
        {
            "schema_version": 3,
            "next_structure_sequence": 2,
            "structures": [
                {
                    "structure_id": "structure-1",
                    "label": "S1",
                    "template": copy.deepcopy(template),
                }
            ],
            "drafts": {"structure-1": copy.deepcopy(template)},
        }
    )

    assert normalized["structures"][0]["template"][
        "contract_multiplier"
    ] == pytest.approx(expected)
    assert normalized["drafts"]["structure-1"][
        "contract_multiplier"
    ] == pytest.approx(expected)


def test_legacy_hh_workspace_migrates_to_upfront():
    template = {
        "asset": "HH",
        "model": "asian76",
        "context": {"premium_convention": "product_default"},
    }
    migrated = pricer._migrate_template_premium_convention(copy.deepcopy(template))
    assert migrated["context"]["premium_convention"] == "upfront"


def test_futures_style_workspace_migration_zeroes_saved_rate():
    template = {
        "asset": "TTF",
        "model": "black76",
        "context": {
            "premium_convention": "futures_style",
            "rate": 0.08,
        },
    }

    migrated = pricer._migrate_template_premium_convention(copy.deepcopy(template))

    assert migrated["context"]["rate"] == 0.0


def test_legacy_kirk_draft_migrates_forwards_dates_and_only_defensible_asset_code():
    template = {
        "asset": "JKM",
        "model": "kirk",
        "contract_multiplier": 10_000,
        "context": {
            "asset_1": 12.5,
            "asset_2": 8.75,
            "expiration_date": "2027-02-25",
            "contract_expiration_date": "2027-03-15",
            "correlation": 0.8,
        },
    }

    migrated = pricer._migrate_template_premium_convention(
        copy.deepcopy(template),
        migrate_legacy_contract_size=True,
    )

    assert migrated["context"]["asset_1_forward"] == 12.5
    assert migrated["context"]["asset_2_forward"] == 8.75
    assert migrated["context"]["asset_1_code"] == "JKM"
    assert "asset_2_code" not in migrated["context"]
    assert migrated["context"]["contractual_expiry"] == "2027-02-25"
    assert migrated["context"]["asset_1_reference_expiry"] == "2027-03-15"
    assert migrated["context"]["asset_2_reference_expiry"] == "2027-03-15"
    assert "asset_1" not in migrated["context"]
    assert "asset_2" not in migrated["context"]
    assert migrated["contract_multiplier"] == 10_000


def test_current_kirk_workspace_does_not_infer_asset_1_from_hidden_legacy_asset():
    workspace = {
        "schema_version": pricer.PRICER_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": 2,
        "drafts": {},
        "structures": [
            {
                "structure_id": "structure-1",
                "label": "S1",
                "template": {
                    "asset": "TTF",
                    "model": "kirk",
                    "context": {
                        "asset_1_forward": 100.0,
                        "asset_2_forward": 90.0,
                    },
                },
            }
        ],
    }

    normalized = pricer._normalize_workspace(workspace)

    context = normalized["structures"][0]["template"]["context"]
    assert "asset_1_code" not in context
    assert "asset_2_code" not in context


def test_fully_corrupt_draft_recovers_one_default_leg_and_finite_sequence(
    monkeypatch,
):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: None)

    outputs = pricer.manage_structure_legs(
        "black76",
        None,
        None,
        None,
        None,
        {"corrupt": "rowData"},
        [],
        {
            "model": "black76",
            "context": "bad",
            "legs": "bad",
            "next_leg_sequence": float("inf"),
        },
        pricer._instance_id("pricer-option-type", "structure-1"),
    )

    assert outputs[2] == [pricer.default_leg("black76", 1)]
    assert outputs[4]["context"] is None
    assert outputs[4]["next_leg_sequence"] == 2
    assert outputs[5] == "Invalid saved leg state was reset."

    panel = pricer._build_structure_panel(
        {
            "structure_id": "structure-1",
            "label": "Structure 1",
            "template": {
                "model": "black76",
                "next_leg_sequence": float("inf"),
            },
        }
    )
    assert component_by_pattern("pricer-draft-store", root=panel).data[
        "next_leg_sequence"
    ] == 2


def test_leg_action_buttons_only_enable_for_valid_selected_rows():
    one_row = [pricer.default_leg("black76", 1)]
    two_rows = [*one_row, pricer.default_leg("black76", 2)]

    assert pricer.toggle_leg_action_buttons([], one_row) == (True, True)
    assert pricer.toggle_leg_action_buttons([one_row[0]], one_row) == (False, True)
    assert pricer.toggle_leg_action_buttons([one_row[0]], two_rows) == (False, False)
    assert pricer.toggle_leg_action_buttons([{"leg_id": "missing"}], two_rows) == (
        True,
        True,
    )


def _surface_view_fixture():
    return {
        "schema_version": 1,
        "context_key": "context-1",
        "status": "ready",
        "structure_ids": ["structure-1"],
        "structure_labels": ["S1"],
        "structure_label": "S1",
        "asset": "JKM",
        "model": "asian76",
        "model_label": "Asian-76",
        "delivery_label": "Apr-27",
        "valuation_date": "2026-07-29",
        "source_kind": "governed",
        "source_label": "Governed calibrated publication",
        "surface_cob": "2026-07-28",
        "published_at": "2026-07-29T12:34:56+00:00",
        "warnings": [
            "Prior COB: using 2026-07-28 for valuation 2026-07-29."
        ],
        "curve_points": [
            {
                "delta": 0.10,
                "call_delta": 0.90,
                "strike": 14.0,
                "input_volatility": 0.58,
                "pricing_volatility": 0.58,
            },
            {
                "delta": 0.50,
                "call_delta": 0.50,
                "strike": 17.0,
                "input_volatility": 0.56,
                "pricing_volatility": 0.56,
            },
            {
                "delta": 0.90,
                "call_delta": 0.10,
                "strike": 20.0,
                "input_volatility": 0.60,
                "pricing_volatility": 0.60,
            },
        ],
        "quote_points": [
            {
                "structure_id": "structure-1",
                "structure_label": "S1",
                "leg_id": "leg-1",
                "leg_label": "Leg 1",
                "short_label": "S1 L1",
                "call_put": "C",
                "strike": 17.0,
                "quote_basis": "PREMIUM",
                "quote_basis_label": "Premium-implied",
                "contract_volatility": 0.5646,
                "reference_volatility": 0.5607,
                "pricing_volatility": 0.5607,
                "difference_vol_points": 0.39,
                "delta": 0.50,
                "call_delta": 0.50,
                "surface_cob": "2026-07-28",
                "source": "Governed calibrated publication",
            }
        ],
    }


def test_surface_comparison_section_precedes_detailed_analysis_and_starts_empty():
    h2_titles = [
        item.children
        for item in walk(pricer.layout)
        if isinstance(item, html.H2)
    ]
    assert h2_titles.index("Contract vols vs volatility surface") < h2_titles.index(
        "Detailed analysis"
    )
    grid = component_by_id("pricer-surface-comparison-grid")
    assert "Calculate a structure" in grid.children.children
    assert pricer.render_surface_comparison_cards(
        pricer._default_workspace(),
        {},
    ).children.startswith("Calculate a structure")
    assert (
        pricer.render_surface_comparison_cards(
            pricer._default_workspace(),
            {},
            pathname="/pricer",
        )
        is no_update
    )


def test_surface_comparison_figure_has_fixed_trader_delta_axis_and_raw_quote():
    view = _surface_view_fixture()
    figure = pricer._surface_comparison_figure(view)

    assert figure.layout.xaxis.tickvals == (0.10, 0.25, 0.50, 0.75, 0.90)
    assert figure.layout.xaxis.ticktext == ("10P", "25P", "ATM", "25C", "10C")
    assert figure.layout.xaxis.range == (0.0, 1.0)
    assert figure.layout.yaxis.title.text == "IV (%)"
    assert [trace.name for trace in figure.data] == [
        "Calibrated surface",
        None,
        "Contract vol",
    ]
    assert figure.data[-1].y[0] == pytest.approx(56.46)
    assert figure.data[-1].x[0] == pytest.approx(0.50)
    assert figure.data[-1].customdata[0][7] == "+0.39"
    assert "+.2f" not in figure.data[-1].hovertemplate


def test_surface_comparison_card_shows_source_minute_warning_and_quote_key():
    card = pricer._surface_comparison_card(_surface_view_fixture())
    text = " ".join(item for item in walk(card) if isinstance(item, str))

    assert "S1 · JKM · Apr-27 · Asian-76" in text
    assert "Governed calibrated publication" in text
    assert "Published 2026-07-29 12:34 UTC" in text
    assert "Prior COB" in text
    assert "S1 L1 56.46% vs 56.07% (+0.39 vol pts)" in text
    assert any(isinstance(item, dcc.Graph) for item in walk(card))


def test_surface_comparison_callback_filters_removed_and_invalid_snapshots(
    monkeypatch,
):
    snapshot_1, _ = calculate_instance(monkeypatch, "structure-1", forward=100)
    snapshot_2, _ = calculate_instance(monkeypatch, "structure-2", forward=108)
    workspace = pricer._reduce_workspace(pricer._default_workspace(), "add")

    structures = pricer._calculated_surface_structures(
        workspace,
        {
            "structure-1": snapshot_1,
            "structure-2": snapshot_2,
            "removed": snapshot_1,
            "invalid": {"schema_version": -1},
        },
    )
    assert [item["structure_id"] for item in structures] == [
        "structure-1",
        "structure-2",
    ]

    captured = {}

    def build(items, *, force_refresh=False):
        captured["items"] = items
        captured["force_refresh"] = force_refresh
        return [_surface_view_fixture()]

    monkeypatch.setattr(pricer, "build_surface_comparison_views", build)
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "refresh-options-data",
    )
    cards = pricer.render_surface_comparison_cards(
        workspace,
        {"structure-1": snapshot_1, "structure-2": snapshot_2},
        1,
    )

    assert len(captured["items"]) == 2
    assert captured["force_refresh"] is True
    assert len(cards) == 1
    assert "pricer-surface-card" in cards[0].className


def test_surface_comparison_error_and_kirk_cards_are_isolated():
    error = {
        **_surface_view_fixture(),
        "status": "error",
        "message": "Exact contract surface unavailable.",
    }
    unsupported = {
        **_surface_view_fixture(),
        "status": "unsupported",
        "message": "Kirk has two volatility inputs.",
    }

    error_card = pricer._surface_comparison_card(error)
    kirk_card = pricer._surface_comparison_card(unsupported)

    assert "Exact contract surface unavailable." in " ".join(
        item for item in walk(error_card) if isinstance(item, str)
    )
    assert "Kirk has two volatility inputs." in " ".join(
        item for item in walk(kirk_card) if isinstance(item, str)
    )
    assert "pricer-surface-card-error" in error_card.className
    assert "pricer-surface-card-unsupported" in kirk_card.className
