import datetime as dt

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
    return next(
        item for item in walk(pricer.layout) if getattr(item, "id", None) == component_id
    )


def black_context_states():
    params = [100.0, 0.03]
    param_ids = [
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
    dates = ["2026-10-29", "2027-01-29"]
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
        "Financial",
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


def test_layout_has_semantic_heading_session_stores_and_editable_leg_grid():
    headings = [item for item in walk(pricer.layout) if isinstance(item, html.H1)]
    assert len(headings) == 1
    assert headings[0].children == "Structure configuration"
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
    layout_text = " ".join(
        item for item in walk(pricer.layout) if isinstance(item, str)
    )
    assert "Option structure pricer" not in layout_text
    assert "Shared context" not in layout_text
    assert "Trade sizing" not in layout_text
    assert "Leg unit metrics are unweighted" not in layout_text

    calculation_store = component_by_id("pricer-calculation-store")
    draft_store = component_by_id("pricer-draft-store")
    structure_type = component_by_id("pricer-structure-type")
    asset = component_by_id("pricer-asset")
    pricing_model = component_by_id("option-type")
    valuation_date = component_by_id("pricer-valuation-date")
    context_row = next(
        item
        for item in walk(pricer.layout)
        if getattr(item, "className", None) == "pricer-context-with-valuation"
    )
    context_component_ids = [
        getattr(item, "id", None)
        for child in context_row.children
        for item in walk(child)
        if getattr(item, "id", None) is not None
    ]
    leg_toolbar = next(
        item
        for item in walk(pricer.layout)
        if getattr(item, "className", None) == "pricer-leg-toolbar"
    )
    leg_heading, leg_actions = leg_toolbar.children
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
    action_ids = [
        getattr(item, "id", None)
        for item in walk(leg_actions)
        if getattr(item, "id", None) is not None
    ]
    grid = component_by_id("pricer-legs-grid")
    assert isinstance(calculation_store, dcc.Store)
    assert calculation_store.storage_type == "session"
    assert draft_store.storage_type == "session"
    assert isinstance(structure_type, dcc.Dropdown)
    assert structure_type.value == "Financial"
    assert structure_type.clearable is False
    assert structure_type.persistence == "pricer-structure-type"
    assert structure_type.persistence_type == "session"
    assert [option["value"] for option in structure_type.options] == [
        "Financial",
        "Physical",
    ]
    assert isinstance(asset, dcc.Dropdown)
    assert asset.value == "TTF"
    assert asset.clearable is False
    assert asset.persistence == "pricer-asset"
    assert asset.persistence_type == "session"
    assert [option["value"] for option in asset.options] == [
        "TTF",
        "JKM",
        "HH",
        "Brent",
        "NBP",
    ]
    assert isinstance(pricing_model, dcc.Dropdown)
    assert context_component_ids[:4] == [
        "pricer-structure-type",
        "pricer-asset",
        "option-type",
        "pricer-valuation-date",
    ]
    output_overview = next(
        item
        for item in walk(pricer.layout)
        if getattr(item, "className", None) == "pricer-output-overview"
    )
    assert [child.id for child in output_overview.children] == [
        "model-inputs-used-container",
        "results-container",
        "time-info",
    ]
    assert heading_ids == [
        "pricer-add-leg",
        "pricer-duplicate-leg",
        "pricer-remove-leg",
        "pricer-leg-action-status",
    ]
    assert edit_action_ids == [
        "pricer-add-leg",
        "pricer-duplicate-leg",
        "pricer-remove-leg",
    ]
    assert action_ids == [
        "pricer-calculation-status",
        "calculate-button",
    ]
    assert isinstance(valuation_date, dcc.DatePickerSingle)
    assert valuation_date.date == "2026-07-29"
    assert valuation_date.min_date_allowed is None
    assert getattr(valuation_date, "max_date_allowed", None) is None
    assert valuation_date.persistence == "pricer-valuation-date-v1"
    assert valuation_date.persistence_type == "session"
    assert grid.persistence_type == "session"
    assert grid.persisted_props == ["rowData"]
    assert grid.rowData[0]["leg_id"] == "leg-1"
    assert grid.dashGridOptions["stopEditingWhenCellsLoseFocus"] is True
    assert not any(
        getattr(component, "id", None) == "pricer-structure-quantity"
        for component in walk(pricer.layout)
    )
    assert next(
        column
        for column in grid.columnDefs
        if column.get("field") == "ratio"
    )["headerName"] == "Lots"


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
    assert forward.persistence == "pricer-black76-forward-step-any-v2"
    assert rate.step == 0.000001
    assert rate.persistence == "pricer-black76-rate"
    multiplier = component_by_id("pricer-contract-multiplier")
    assert multiplier.min == 0
    assert multiplier.step == 0.01
    assert multiplier.persistence == "pricer-contract-multiplier-aligned-v2"


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
    assert "at least one" in output[-1]


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
    summary = rendered[0]
    combined_grid = rendered[1]
    assert len(summary) == 12
    assert [card.children[0].children for card in summary] == [
        "Type",
        "Asset",
        "Pricing model",
        "Valuation date",
        "Forward price used",
        "Option expiration",
        "Contract expiration",
        "Risk-free rate",
        "Unit structure value",
        "Total trade value",
        "Time to expiry",
        "Volatility adjustment",
    ]
    assert summary[0].children[0].children == "Type"
    assert summary[0].children[1].children == "Financial"
    assert summary[1].children[0].children == "Asset"
    assert summary[1].children[1].children == "TTF"
    assert summary[4].children[0].children == "Forward price used"
    assert summary[4].children[1].children == "100.0000"
    assert summary[4].children[2] is None
    assert summary[8].children[2] is None
    assert summary[9].children[2] is None
    assert "pricer-result-card-unit" in summary[8].className
    assert "pricer-result-card-trade" in summary[9].className
    assert "pricer-result-card-context" in summary[0].className
    assert "pricer-result-card-market" in summary[1].className
    assert "pricer-result-card-market" in summary[4].className
    time_card, adjustment_card = summary[-2:]
    assert time_card.children[0].children == "Time to expiry"
    assert time_card.children[1].children == (
        f"{snapshot['context']['time_to_expiry']:.6f}y"
    )
    assert time_card.children[2] is None
    assert "pricer-result-card-time" in time_card.className
    assert adjustment_card.children[0].children == "Volatility adjustment"
    assert adjustment_card.children[1].children == (
        f"{snapshot['context']['vol_adjustment_factor']:.6f}×"
    )
    assert (
        f"{snapshot['context']['option_business_days']} days"
        in adjustment_card.children[2].children
    )
    assert (
        f"{snapshot['context']['contract_business_days']} contract days"
        in adjustment_card.children[2].children
    )
    assert "business days" not in adjustment_card.children[2].children
    assert adjustment_card.children[2].children.startswith("√(")
    assert "Delivery-horizon scaling" not in adjustment_card.children[2].children
    assert "pricer-result-card-adjustment" in adjustment_card.className
    assert "pricer-result-card-has-hover-detail" in adjustment_card.className
    assert adjustment_card.children[2].role == "tooltip"
    assert adjustment_card.tabIndex == 0
    assert adjustment_card.to_plotly_json()["props"]["aria-describedby"] == (
        adjustment_card.children[2].id
    )
    assert [card.children[1].children for card in summary[:8]] == [
        "Financial",
        "TTF",
        "Black-76",
        "2026-07-29",
        "100.0000",
        snapshot["context"]["expiration_date"],
        snapshot["context"]["contract_expiration_date"],
        f"{snapshot['context']['rate']:.4%}",
    ]
    assert "pricer-result-card-basis" in summary[2].className
    assert all(
        "pricer-result-card-context" in card.className
        for card in (summary[0], summary[3], *summary[5:8])
    )
    assert len(combined_grid.rowData) == 2
    assert rendered[2] == ""
    assert rendered[3] == ""
    assert rendered[4] == ""
    assert combined_grid.rowData[0]["unit_value"] == pytest.approx(
        snapshot["legs"][0]["unit"]["value"]
    )
    assert combined_grid.rowData[0]["trade_value"] == pytest.approx(
        snapshot["legs"][0]["trade_contribution"]["value"]
    )
    assert combined_grid.rowData[0]["ratio"] == 2
    assert combined_grid.rowData[0]["trade_value"] == pytest.approx(
        combined_grid.rowData[0]["unit_value"] * 2 * 100
    )
    groups = [
        column["headerName"]
        for column in combined_grid.columnDefs
        if "children" in column
    ]
    assert groups == ["Position contribution", "Unit analytics"]
    assert [
        column["headerName"]
        for column in combined_grid.columnDefs
        if "children" not in column
    ].count("Strike") == 1
    assert next(
        column
        for column in combined_grid.columnDefs
        if column.get("field") == "ratio"
    )["headerName"] == "Lots"
    assert combined_grid.columnSize == "autoSize"
    assert combined_grid.columnSizeOptions == {"skipHeader": False}
    assert combined_grid.dashGridOptions["suppressColumnVirtualisation"] is True
    assert combined_grid.dashGridOptions["enableBrowserTooltips"] is False
    assert combined_grid.dashGridOptions["tooltipShowDelay"] == 0
    assert combined_grid.dashGridOptions["tooltipHideDelay"] == 3000
    position_columns = combined_grid.columnDefs[-2]["children"]
    assert all(
        "d3.format(',.2f')" in column["valueFormatter"]["function"]
        for column in position_columns
    )
    unit_columns = combined_grid.columnDefs[-1]["children"]
    assert all(
        "d3.format(',.4f')" in column["valueFormatter"]["function"]
        for column in unit_columns
    )
    for columns in (position_columns, unit_columns):
        vega_column = next(
            column for column in columns if column["field"].endswith("_vega")
        )
        rho_column = next(
            column for column in columns if column["field"].endswith("_rho")
        )
        assert vega_column["headerName"] == "Vega"
        assert vega_column["tooltipField"] == "_vega_tooltip"
        assert "pricer-metric-tooltip-cell" in vega_column["cellClass"]
        assert rho_column["headerName"] == "Rho"
        assert rho_column["tooltipField"] == "_rho_tooltip"
        assert "pricer-metric-tooltip-cell" in rho_column["cellClass"]
    assert combined_grid.rowData[0]["_vega_tooltip"] == "Input vol, 1 point"
    assert combined_grid.rowData[0]["_rho_tooltip"] == "1 rate point"
    delta_column = next(
        column for column in position_columns if column["field"] == "trade_delta"
    )
    assert delta_column["minWidth"] == 72
    pinned = combined_grid.dashGridOptions["pinnedBottomRowData"][0]
    assert pinned["name"] == "Total"
    assert pinned["_vega_tooltip"] == "Input vol, 1 point"
    assert pinned["_rho_tooltip"] == "1 rate point"
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


def test_kirk_summary_uses_an_explicit_two_asset_cross_card():
    as_of = FrozenDate(2026, 7, 29)
    snapshot = pricer.calculate_structure(
        "kirk",
        pricer.default_context("kirk", as_of),
        {"structure_quantity": 1, "contract_multiplier": 1},
        [pricer.default_leg("kirk", 1)],
        as_of=as_of,
    )

    summary = pricer.render_structure_results(snapshot)[0]
    cross_card = summary[4]
    assert cross_card.children[0].children == "Asset price cross used"
    assert cross_card.children[1].children == "100.0000 / 90.0000"
    assert cross_card.children[2].children == "Asset 1 / Asset 2"
    time_card, adjustment_card = summary[-2:]
    assert time_card.children[1].children == (
        f"{snapshot['context']['time_to_expiry']:.6f}y"
    )
    assert time_card.children[2] is None
    assert adjustment_card.children[0].children == "Volatility adjustment"
    assert [card.children[0].children for card in summary] == [
        "Type",
        "Asset",
        "Pricing model",
        "Valuation date",
        "Asset price cross used",
        "Option expiration",
        "Contract expiration",
        "Correlation",
        "Unit structure value",
        "Total trade value",
        "Time to expiry",
        "Volatility adjustment",
    ]
    assert summary[0].children[1].children == "Financial"
    assert summary[1].children[1].children == "TTF"
    assert summary[2].children[1].children == "Kirk"
    assert summary[7].children[1].children == "0.5000"


def test_asian_summary_follows_configuration_order_and_keeps_averaging_context():
    as_of = FrozenDate(2026, 7, 29)
    snapshot = pricer.calculate_structure(
        "asian76",
        pricer.default_context("asian76", as_of),
        {"structure_quantity": 1, "contract_multiplier": 1},
        [pricer.default_leg("asian76", 1)],
        as_of=as_of,
    )

    summary = pricer.render_structure_results(snapshot)[0]
    assert [card.children[0].children for card in summary] == [
        "Type",
        "Asset",
        "Pricing model",
        "Valuation date",
        "Forward price used",
        "Expiration / averaging end",
        "Contract expiration",
        "Risk-free rate",
        "Unit structure value",
        "Total trade value",
        "Time to expiry",
        "Volatility adjustment",
    ]
    assert snapshot["context"]["averaging_start_date"] in summary[-2].children[2].children


def test_valuation_date_drives_time_basis_and_allows_past_or_future(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "calculate-button")
    params, param_ids, dates, date_ids = black_context_states()
    rows = two_leg_rows()[:1]

    past_snapshot, _ = pricer.calculate_structure_callback(
        1,
        "Financial",
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
        "Financial",
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
    assert past_snapshot["context"]["time_to_expiry"] == pytest.approx(273 / 365)
    assert future_snapshot["context"]["time_to_expiry"] == pytest.approx(30 / 365)
    assert past_snapshot["totals"]["trade_value"] != pytest.approx(
        future_snapshot["totals"]["trade_value"]
    )


def test_valuation_date_after_expiry_blocks_calculation(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "calculate-button")
    params, param_ids, dates, date_ids = black_context_states()
    snapshot, status = pricer.calculate_structure_callback(
        1,
        "Financial",
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
        "Financial",
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
    for figure in (volatility, rate, time, extension):
        assert all("Leg " not in (trace.name or "") for trace in figure.data)
        assert any(trace.name == "Total structure value" for trace in figure.data)
    assert "only available for Kirk" in correlation.layout.annotations[0].text


def test_model_change_clears_calculation_snapshot(monkeypatch):
    monkeypatch.setattr(pricer, "_get_pricer_triggered_id", lambda: "option-type")
    output = pricer.calculate_structure_callback(
        1,
        "Financial",
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
        "Financial",
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
    assert "Asset changed" in output[1].children


def test_structure_type_change_clears_calculation_snapshot(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-structure-type",
    )
    output = pricer.calculate_structure_callback(
        1,
        "Physical",
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
    assert "Type changed" in output[1].children


def test_pricing_input_change_clears_calculation_snapshot(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-legs-grid",
    )
    output = pricer.calculate_structure_callback(
        1,
        "Financial",
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
    assert "Inputs changed" in output[1].children


def test_initial_pricing_input_hydration_keeps_status_area_empty(monkeypatch):
    monkeypatch.setattr(
        pricer,
        "_get_pricer_triggered_id",
        lambda: "pricer-legs-grid",
    )
    output = pricer.calculate_structure_callback(
        None,
        "Financial",
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
    assert output[4]["legs"] == rows
