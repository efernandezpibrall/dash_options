from types import SimpleNamespace

import pandas as pd
import pytest
from dash.exceptions import PreventUpdate

from pages import vol_calibration
from vol_calibration.pages import brent, hh, jkm, ttf


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
    assert "ttf-date-picker" not in components
    assert "hh-date-picker" not in components
    assert "jkm-date-picker" not in components


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
