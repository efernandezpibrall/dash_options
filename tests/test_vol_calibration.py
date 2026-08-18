import base64
from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest
from dash.exceptions import PreventUpdate
import numpy as np

from pages import vol_calibration
from vol_calibration.components import comparison_modal, smile_grid
from vol_calibration.components.parameter_table import create_parameter_table
from vol_calibration.model_version import DEFAULT_CALIBRATION_MODEL_VERSION
from vol_calibration.pages import brent, hh, jkm, ttf
from vol_calibration.session_state import persist_product_table, restore_product_table


PRODUCT_MODULES = (brent, hh, ttf, jkm)


def _walk(component):
    yield component
    children = getattr(component, "children", None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if hasattr(child, "children") or getattr(child, "id", None) is not None:
            yield from _walk(child)


def _components_by_id(component):
    return {
        item.id: item
        for item in _walk(component)
        if getattr(item, "id", None) is not None
    }


def test_query_parsing_defaults_and_preserves_deep_link_fields():
    assert vol_calibration.parse_calibration_query(None)["product"] == "ttf"
    invalid = vol_calibration.parse_calibration_query("?product=power")
    assert invalid["product"] == "ttf"
    assert invalid["invalid_product"] == "power"

    updated = vol_calibration.update_product_query(
        "?product=ttf&cob_date=2026-07-08&expiry=Sep-26",
        "brent",
    )
    assert updated == "?product=brent&cob_date=2026-07-08&expiry=Sep-26"


def test_layout_lazily_renders_only_requested_product_and_applies_cob_date():
    layout = vol_calibration.create_layout(
        "?product=brent&cob_date=2026-07-08&expiry=Sep-26"
    )
    components = _components_by_id(layout)

    assert components["vol-calibration-product-tabs"].active_tab == "brent"
    assert components["brent-date-picker"].date == "2026-07-08"
    assert components["vol-calibration-requested-expiry"].data == "Sep-26"
    assert components["vol-calibration-session-state"].storage_type == "session"
    assert "ttf-date-picker" not in components
    assert "hh-date-picker" not in components
    assert "jkm-date-picker" not in components


def test_product_edits_restore_only_for_the_same_session_product_and_cob():
    rows = [{"expiry": "Sep-26", "vr": 0.31}]
    state = persist_product_table(None, "ttf", "2026-07-08", rows)

    restored = restore_product_table(state, "TTF", "2026-07-08")
    assert restored == rows
    assert restored is not rows
    assert restore_product_table(state, "jkm", "2026-07-08") is None
    assert restore_product_table(state, "ttf", "2026-07-09") is None
    # TTF deliberately rebuilds from the latest immutable publication instead
    # of restoring a browser-only parameter snapshot.
    assert ttf.update_param_table(None, None, None) == []


def test_all_smile_visuals_pass_wing_v2_explicitly(monkeypatch):
    from options.calibration_engine.models import wing_model

    model_versions = []

    def record_wing_model(*, strike, forward, model_version, **params):
        del forward, params
        model_versions.append(model_version)
        return np.full_like(np.asarray(strike, dtype=float), 0.25)

    monkeypatch.setattr(wing_model, "wing_model_iv", record_wing_model)
    expiry = pd.Timestamp("2026-09-01")
    market_data = pd.DataFrame(
        {
            "expiry": [expiry, expiry, expiry],
            "forward": [100.0, 100.0, 100.0],
            "strike": [90.0, 100.0, 110.0],
            "iv": [0.28, 0.25, 0.27],
            "delta": [-0.25, 0.50, 0.25],
            "dte": [45, 45, 45],
        }
    )
    params = {
        "vr": 0.25,
        "sr": 0.0,
        "pc": 0.1,
        "cc": 0.1,
        "dc": -0.2,
        "uc": 0.2,
        "dsm": 0.1,
        "usm": 0.1,
        "vcr": 0.0,
        "scr": 0.0,
        "ssr": 1.0,
        "put_wing_power": 0.5,
        "call_wing_power": 0.5,
    }
    params_df = pd.DataFrame([{"expiry": expiry, **params}])

    smile_grid.create_smile_grid_figure(market_data, params_df)
    smile_grid.create_single_smile_plot(market_data, params, "Sep-26")
    comparison_modal.create_comparison_plot(
        market_data,
        params,
        params,
        params,
        "Sep-26",
    )

    assert model_versions
    assert set(model_versions) == {DEFAULT_CALIBRATION_MODEL_VERSION}


@pytest.mark.parametrize("module", PRODUCT_MODULES)
def test_excel_summary_records_model_version(module):
    download = module.export_to_excel(
        1,
        [{"expiry": "Sep-26", "vr": 0.25, "rmse": "1.00%"}],
        None,
        "2026-07-08",
    )
    workbook = BytesIO(base64.b64decode(download["content"]))
    excel_file = pd.ExcelFile(workbook)
    summary = pd.read_excel(excel_file, sheet_name="Summary")

    expected_version = (
        brent.BRENT_ADJUSTMENT_MODEL_VERSION
        if module is brent
        else DEFAULT_CALIBRATION_MODEL_VERSION
    )
    assert summary.loc[0, "Model Version"] == expected_version
    assert excel_file.sheet_names == ["Parameters", "Summary"]


@pytest.mark.parametrize("module", (hh, ttf, jkm))
def test_comparison_save_is_rejected_server_side_when_writes_disabled(monkeypatch, module):
    monkeypatch.setattr(module, "writes_enabled", lambda: False)
    monkeypatch.setattr(
        module,
        "ctx",
        SimpleNamespace(triggered_id=f"{module.COMMODITY_LOWER}-comparison-save-btn"),
    )
    if hasattr(module, "ParameterStore"):
        monkeypatch.setattr(
            module,
            "ParameterStore",
            lambda *args, **kwargs: pytest.fail("ParameterStore must not be constructed"),
        )

    with pytest.raises(PreventUpdate):
        module.handle_calibration(
            None,
            None,
            1,
            None,
            None,
            None,
            None,
            None,
            None,
            True,
            {},
            "log_moneyness",
            "2026-07-08",
            None,
        )


@pytest.mark.parametrize("module", (hh, ttf, jkm))
def test_batch_auto_save_cannot_create_a_store_when_writes_disabled(monkeypatch, module):
    monkeypatch.setattr(module, "writes_enabled", lambda: False)
    monkeypatch.setattr(
        module,
        "ctx",
        SimpleNamespace(triggered_id=f"{module.COMMODITY_LOWER}-batch-confirm-btn"),
    )
    monkeypatch.setattr(
        module,
        "get_database_engine",
        lambda: pytest.fail("database engine must not be requested"),
    )
    monkeypatch.setattr(
        module,
        "ParameterStore",
        lambda *args, **kwargs: pytest.fail("ParameterStore must not be constructed"),
        raising=False,
    )
    if module is ttf:
        monkeypatch.setattr(
            module,
            "fit_ttf_hybrid_candidate",
            lambda observations, initial_params, **kwargs: {
                "params": initial_params,
                "core_tv_rmse": 0.0,
                "tail_fit_tv_rmse": 0.001,
                "iv_rmse": 0.01,
                "left_blend_width": 0.10,
                "right_blend_width": 0.10,
                "success": True,
                "butterfly": {"is_valid": True},
                "validation": {"is_valid": True, "min_g": 0.01},
            },
        )
    else:
        monkeypatch.setattr(
            module,
            "evaluate_fit",
            lambda *args, **kwargs: {"rmse": 0.02},
        )
        monkeypatch.setattr(
            module,
            "calibrate",
            lambda *args, **kwargs: {
                "params": kwargs.get("initial_params", {"vr": 0.25}),
                "rmse": 0.01,
                "success": True,
                "butterfly": {"is_valid": True},
            },
        )
        monkeypatch.setattr(
            module,
            "update_arb_status_in_row",
            lambda *args, **kwargs: "Pass",
        )

    expiry = pd.Timestamp("2026-09-01")
    if module is ttf:
        deltas = np.asarray(
            [0.01, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.99]
        )
        market_data = pd.DataFrame(
            {
                "expiry": expiry,
                "option_expiration_date": pd.Timestamp("2026-08-27"),
                "forward": 1.0,
                "strike": np.nan,
                "iv": np.linspace(0.30, 0.24, len(deltas)),
                "delta": deltas,
                "dte": 50.0,
                "delta_convention": "undiscounted_call_delta",
                "source_name": "official",
                "quote_class": "observed",
                "weight": 1.0,
            }
        )
    else:
        market_data = pd.DataFrame(
            [{"expiry": expiry, "forward": 1.0, "strike": 1.0, "iv": 0.25, "delta": 0.5}]
        )
    table_data = [
        {
            "expiry": "Sep-26",
            **(ttf.get_defaults("TTF") if module is ttf else {"vr": 0.20}),
            "calibration_basis": "Observed" if module is ttf else "",
            "rmse": "2.00%",
            "arb_status": "Pass",
        }
    ]

    result = module.run_batch_calibration(
        1,
        None,
        market_data.to_json(date_format="iso", orient="split"),
        table_data,
        ["auto_save"],
        [],
        "2026-07-08",
        False,
    )
    assert result[1] == 100
    batch_results = (
        result[5]["results"] if module is ttf else result[5]
    )
    assert batch_results[0]["status"] == "Success"


def test_save_controls_are_disabled_by_default(monkeypatch):
    monkeypatch.setenv("VOL_CALIBRATION_WRITES_ENABLED", "false")
    layout = vol_calibration.create_layout("?product=ttf")
    components = _components_by_id(layout)

    assert components["ttf-save-all-btn"].disabled is True
    assert components["ttf-comparison-save-btn"].disabled is True
    auto_save_option = components["ttf-batch-auto-save"].options[0]
    assert auto_save_option["disabled"] is True


def test_ttf_batch_save_requires_one_accepted_result_per_expiry(monkeypatch):
    expected = ["Oct-26", "Nov-26"]
    complete = [
        {"expiry": "2026-10-01", "status": "Success"},
        {"expiry": "2026-11-01", "status": "Skipped"},
    ]

    assert ttf._batch_results_ready(complete, expected) is True
    assert ttf._batch_results_ready([], expected) is False
    assert ttf._batch_results_ready(complete[:1], expected) is False
    assert ttf._batch_results_ready(
        [complete[0], {**complete[1], "status": "Failed"}], expected
    ) is False
    assert ttf._batch_results_ready([complete[0], complete[0]], expected) is False

    monkeypatch.setattr(ttf, "ttf_publication_enabled", lambda: True)
    rows = []
    for value in expected:
        rows.append(
            {
                "expiry": value,
                **ttf.get_defaults("TTF"),
                "calibration_basis": "Observed",
                "left_blend_width": 0.1,
                "right_blend_width": 0.1,
                "core_tv_rmse": 0.0,
                "tail_fit_tv_rmse": 0.001,
                "iv_rmse": 0.01,
                "arb_status": "Pass",
                "calibration_method": ttf.TTF_HYBRID_METHOD,
                "calibration_policy_version": ttf.TTF_HYBRID_POLICY_VERSION,
            }
        )
    market_json = "governed-market-json"
    batch_state = ttf._build_ttf_batch_state(
        "2026-07-30", market_json, rows, {}, {}, complete
    )
    disabled, _ = ttf.enable_ttf_batch_save(
        batch_state,
        rows,
        "2026-07-30",
        market_json,
        {},
        {},
    )
    assert disabled is False

    stale_state = {**batch_state, "trading_date": "2026-07-29"}
    disabled, title = ttf.enable_ttf_batch_save(
        stale_state,
        rows,
        "2026-07-30",
        market_json,
        {},
        {},
    )
    assert disabled is True
    assert "different trading date" in title

    edited_rows = [dict(row) for row in rows]
    edited_rows[0]["vr"] += 0.01
    disabled, title = ttf.enable_ttf_batch_save(
        batch_state,
        edited_rows,
        "2026-07-30",
        market_json,
        {},
        {},
    )
    assert disabled is True
    assert "parameter table changed" in title.lower()

    disabled, title = ttf.enable_ttf_batch_save(
        complete,
        rows,
        "2026-07-30",
        market_json,
        {},
        {},
    )
    assert disabled is True
    assert "Run Calibrate All" in title


def test_ttf_batch_save_uses_authenticated_operator_as_creator():
    identity = SimpleNamespace(subject="publisher@example.com")
    results = [{"expiry": "2026-10-01", "status": "Success"}]

    assert ttf._publication_created_by(
        identity,
        {},
        results,
        ["Oct-26"],
    ) == "publisher@example.com"
    assert ttf._publication_created_by(
        identity,
        {"2026-10": {"created_by": "trader@example.com"}},
        [],
        ["Oct-26"],
    ) == "trader@example.com"

    with pytest.raises(ValueError, match="Calibrate all expiries"):
        ttf._publication_created_by(identity, {}, [], ["Oct-26"])


def test_ttf_batch_state_invalidates_every_surface_input():
    rows = [
        {
            "expiry": "Oct-26",
            **ttf.get_defaults("TTF"),
            "calibration_basis": "Observed",
            "left_blend_width": 0.1,
            "right_blend_width": 0.1,
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.01,
            "arb_status": "Pass",
            "calibration_method": ttf.TTF_HYBRID_METHOD,
            "calibration_policy_version": ttf.TTF_HYBRID_POLICY_VERSION,
        }
    ]
    results = [{"expiry": "2026-10-01", "status": "Success"}]
    state = ttf._build_ttf_batch_state(
        "2026-07-30",
        "market-a",
        rows,
        {"2026-10": [{"delta": 0.65, "iv": 0.4}]},
        {"publication_id": "base-a"},
        results,
    )

    ready, reason = ttf._batch_state_ready(
        state,
        "2026-07-30",
        "market-a",
        rows,
        {"2026-10": [{"delta": 0.65, "iv": 0.4}]},
        {"publication_id": "base-a"},
    )
    assert ready is True
    assert reason is None

    changes = [
        ("market-b", {"2026-10": [{"delta": 0.65, "iv": 0.4}]}, {"publication_id": "base-a"}),
        ("market-a", {"2026-10": [{"delta": 0.65, "iv": 0.41}]}, {"publication_id": "base-a"}),
        ("market-a", {"2026-10": [{"delta": 0.65, "iv": 0.4}]}, {"publication_id": "base-b"}),
    ]
    for market_json, node_store, publication in changes:
        ready, reason = ttf._batch_state_ready(
            state,
            "2026-07-30",
            market_json,
            rows,
            node_store,
            publication,
        )
        assert ready is False
        assert "changed" in reason

    failed_state = ttf._build_ttf_batch_state(
        "2026-07-30",
        "market-a",
        rows,
        {"2026-10": [{"delta": 0.65, "iv": 0.4}]},
        {"publication_id": "base-a"},
        [{"expiry": "2026-10-01", "status": "Failed"}],
    )
    ready, reason = ttf._batch_state_ready(
        failed_state,
        "2026-07-30",
        "market-a",
        rows,
        {"2026-10": [{"delta": 0.65, "iv": 0.4}]},
        {"publication_id": "base-a"},
    )
    assert ready is False
    assert "Every expiry" in reason


def test_ttf_date_or_reload_clears_the_calibration_snapshot():
    assert ttf.clear_ttf_unsaved_state_on_date_change("2026-08-05", 1) == (
        {},
        {},
        0.0,
        0.0,
        0.0,
        0.0,
        [],
        [],
        {},
    )


def test_basis_is_exposed_only_in_ttf_shared_components():
    ttf_table = _components_by_id(create_parameter_table("TTF"))["ttf-param-table"]
    jkm_table = _components_by_id(create_parameter_table("JKM"))["jkm-param-table"]
    assert [column["id"] for column in ttf_table.columns][1] == "calibration_basis"
    assert "calibration_basis" not in {
        column["id"] for column in jkm_table.columns
    }

    ttf_modal = _components_by_id(
        comparison_modal.create_comparison_modal("TTF", show_basis=True)
    )
    jkm_modal = _components_by_id(
        comparison_modal.create_comparison_modal("JKM")
    )
    assert "ttf-comparison-basis" in ttf_modal
    assert "jkm-comparison-basis" not in jkm_modal


def test_host_app_registers_route_callbacks_without_overwriting_url_search():
    import index_options
    from app import app

    app._setup_server()
    assert "page-content.children" in app.callback_map
    assert "nav-active-sink.children" in app.callback_map
    url_search_callbacks = [
        callback_id for callback_id in app.callback_map if callback_id.startswith("url.search")
    ]
    assert len(url_search_callbacks) == 1
    assert index_options.display_page(
        "/vol_calibration",
        "?product=jkm",
    ).className == "vol-calibration-page"

    validation_components = _components_by_id(app.validation_layout)
    assert "vol-calibration-product-tabs" in validation_components
    assert "vol-calibration-workspace" in validation_components
    assert "vol-calibration-requested-expiry" in validation_components
