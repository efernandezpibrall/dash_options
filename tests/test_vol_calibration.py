import base64
from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest
from dash.exceptions import PreventUpdate
import numpy as np

from pages import vol_calibration
from vol_calibration.components import comparison_modal, smile_grid
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
    assert ttf.update_param_table(None, None, state, "2026-07-08") == rows


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
    summary = pd.read_excel(workbook, sheet_name="Summary")

    assert summary.loc[0, "Model Version"] == DEFAULT_CALIBRATION_MODEL_VERSION


@pytest.mark.parametrize("module", PRODUCT_MODULES)
def test_comparison_save_is_rejected_server_side_when_writes_disabled(monkeypatch, module):
    monkeypatch.setattr(module, "writes_enabled", lambda: False)
    monkeypatch.setattr(
        module,
        "ctx",
        SimpleNamespace(triggered_id=f"{module.COMMODITY_LOWER}-comparison-save-btn"),
    )
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


@pytest.mark.parametrize("module", PRODUCT_MODULES)
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
    )
    monkeypatch.setattr(module, "evaluate_fit", lambda *args, **kwargs: {"rmse": 0.02})
    monkeypatch.setattr(
        module,
        "calibrate",
        lambda *args, **kwargs: {"params": {"vr": 0.25}, "rmse": 0.01},
    )
    monkeypatch.setattr(module, "update_arb_status_in_row", lambda *args, **kwargs: "Pass")

    expiry = pd.Timestamp("2026-09-01")
    if module is ttf:
        market_data = pd.DataFrame(
            {
                "expiry": expiry,
                "option_expiration_date": pd.Timestamp("2026-08-27"),
                "forward": 1.0,
                "strike": np.nan,
                "iv": [0.27, 0.26, 0.25, 0.26, 0.27],
                "delta": [0.10, 0.25, 0.50, 0.75, 0.90],
                "dte": 50.0,
                "delta_convention": "undiscounted_call_delta",
                "weight": 1.0,
            }
        )
    else:
        market_data = pd.DataFrame(
            [{"expiry": expiry, "forward": 1.0, "strike": 1.0, "iv": 0.25, "delta": 0.5}]
        )
    table_data = [{"expiry": "Sep-26", "vr": 0.20, "rmse": "2.00%", "arb_status": "Pass"}]

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
    assert result[5][0]["status"] == "Success"


def test_save_controls_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VOL_CALIBRATION_WRITES_ENABLED", raising=False)
    layout = vol_calibration.create_layout("?product=ttf")
    components = _components_by_id(layout)

    assert components["ttf-save-all-btn"].disabled is True
    assert components["ttf-comparison-save-btn"].disabled is True
    auto_save_option = components["ttf-batch-auto-save"].options[0]
    assert auto_save_option["disabled"] is True


def test_host_app_registers_route_callbacks_without_overwriting_url_search():
    import index_options
    from app import app

    app._setup_server()
    assert len(app.callback_map) >= 109
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
