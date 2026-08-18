from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from options.calibration_engine.converters.delta import delta_to_strike, strike_to_delta
from vol_calibration.brent_intraday import (
    ADJUSTMENT_PARAMS,
    BrentAdjustmentError,
    calibrate_adjustment,
    select_surface_slice,
)
from vol_calibration.components.brent_adjustment_plot import (
    create_brent_adjustment_grid,
)
from vol_calibration.components.brent_adjustment_table import (
    create_brent_adjustment_table,
)
from vol_calibration.pages import brent


def _surface(expiry="2026-11-01"):
    call_delta = np.array([0.01, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.99])
    volatility = 0.30 + 0.08 * (call_delta - 0.50) ** 2
    return pd.DataFrame(
        {
            "cob_date": pd.Timestamp("2026-08-03"),
            "code": "Brent",
            "contract_date": pd.Timestamp(expiry),
            "option_expiration_date": pd.Timestamp("2026-10-27"),
            "delta": call_delta,
            "delta_abs": call_delta,
            "put_call": "call",
            "volatility": volatility,
            "delta_bucket": [f"{int(value * 100)}C" for value in call_delta],
            "delta_sort_key": np.arange(len(call_delta)),
            "delta_pct": call_delta * 100,
        }
    )


def _market(surface, shift=0.02, include_excluded=True):
    forward = 80.0
    dte = 85.0
    rows = []
    for row in surface.iloc[1:-1].itertuples(index=False):
        strike = delta_to_strike(
            float(row.delta_abs),
            forward,
            float(row.volatility),
            dte,
            option_type="call",
        )
        option_type = "put" if strike < forward else "call"
        signed_delta = strike_to_delta(
            strike,
            forward,
            float(row.volatility) + shift,
            dte,
            option_type=option_type,
        )
        rows.append(
            {
                "expiry": row.contract_date,
                "dte": dte,
                "delta": signed_delta,
                "iv": float(row.volatility) + shift,
                "strike": strike,
                "forward": forward,
                "weight": 100.0,
                "calibration_eligible": True,
                "exclusion_reason": "",
                "iv_source": "american_on_futures_futures_style",
            }
        )
    if include_excluded:
        rows.append(
            {
                "expiry": surface.iloc[0]["contract_date"],
                "dte": dte,
                "delta": 0.001,
                "iv": 1.50,
                "strike": 140.0,
                "forward": forward,
                "weight": 0.0,
                "calibration_eligible": False,
                "exclusion_reason": "outside_supported_moneyness",
                "iv_source": "vendor_option_volatility_reference",
            }
        )
    return pd.DataFrame(rows)


def test_intraday_fit_adjusts_svi_baseline_and_ignores_excluded_extreme():
    surface = _surface()
    with_extreme = _market(surface, include_excluded=True)
    without_extreme = _market(surface, include_excluded=False)
    surface_slice = select_surface_slice(surface, "2026-11-01")

    result = calibrate_adjustment(
        with_extreme,
        surface_slice,
        expiry="2026-11-01",
        full_surface=surface,
        cob_date="2026-08-03",
    )
    clean_result = calibrate_adjustment(
        without_extreme,
        surface_slice,
        expiry="2026-11-01",
        full_surface=surface,
        cob_date="2026-08-03",
    )

    assert result["success"] is True
    assert result["rmse"] < result["baseline_rmse"]
    assert result["validation"]["is_valid"] is True
    assert result["n_points"] == 9
    assert result["params"] == pytest.approx(clean_result["params"])
    assert result["params"]["atm_shift"] > 0


def test_grid_distinguishes_eligible_points_from_excluded_references():
    surface = _surface()
    market = _market(surface)
    params = pd.DataFrame(
        [{"expiry": pd.Timestamp("2026-11-01"), **{name: 0.0 for name in ADJUSTMENT_PARAMS}}]
    )
    figure = create_brent_adjustment_grid(
        market,
        params,
        "delta",
        None,
        surface,
        {
            "product": "BRENT",
            "requested_cob": "2026-08-03",
            "actual_cob": "2026-08-03",
            "source": "test",
        },
    )

    names = [trace.name for trace in figure.data]
    assert "Eligible observed" in names
    assert "Excluded reference" in names
    assert "Operational Surface" in names


def test_main_adjustment_table_cannot_bypass_validation_by_direct_edit():
    table = create_brent_adjustment_table().children

    assert table.editable is False
    columns = {column["id"]: column for column in table.columns}
    assert all(columns[name]["editable"] is False for name in ADJUSTMENT_PARAMS)


def test_calibration_exception_is_not_reported_as_zero_rmse(monkeypatch):
    monkeypatch.setattr(
        brent,
        "ctx",
        SimpleNamespace(triggered_id="brent-calibrate-all-btn"),
    )
    monkeypatch.setattr(
        brent,
        "calibrate_adjustment",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            BrentAdjustmentError("deliberate failure")
        ),
    )
    surface = _surface()
    market = _market(surface)
    table = [
        {
            "expiry": "Nov-26",
            **{name: 0.0 for name in ADJUSTMENT_PARAMS},
            "rmse": "1.00%",
            "validation": "Pass",
        }
    ]
    payload = {
        "data": surface.to_json(date_format="iso", orient="split"),
        "requested_cob": "2026-08-03",
        "actual_cob": "2026-08-03",
    }

    outputs = brent.handle_calibration(
        1,
        None,
        None,
        None,
        None,
        None,
        market.to_json(date_format="iso", orient="split"),
        table,
        [0],
        False,
        None,
        "delta",
        "2026-08-03",
        None,
        payload,
    )

    assert outputs[0] is False
    assert outputs[6:9] == ("", "", "")
    assert outputs[9].color == "danger"


def test_valid_final_adjustment_applies_to_session_draft_without_database_write(monkeypatch):
    monkeypatch.setattr(
        brent,
        "ctx",
        SimpleNamespace(triggered_id="brent-comparison-save-btn"),
    )
    surface = _surface()
    market = _market(surface)
    candidate = calibrate_adjustment(
        market,
        select_surface_slice(surface, "2026-11-01"),
        expiry="2026-11-01",
        full_surface=surface,
        cob_date="2026-08-03",
    )
    current = {name: 0.0 for name in ADJUSTMENT_PARAMS}
    table = [
        {
            "expiry": "Nov-26",
            **current,
            "rmse": "2.00%",
            "validation": "Pass",
            "message": "Official SVI baseline",
        }
    ]
    comparison_table = [
        {
            "param": name,
            "label": name,
            "current": 0.0,
            "candidate": value,
            "final": value,
        }
        for name, value in candidate["params"].items()
    ]
    payload = {
        "data": surface.to_json(date_format="iso", orient="split"),
        "requested_cob": "2026-08-03",
        "actual_cob": "2026-08-03",
    }

    outputs = brent.handle_calibration(
        None,
        None,
        1,
        None,
        None,
        comparison_table,
        market.to_json(date_format="iso", orient="split"),
        table,
        [0],
        True,
        {
            "expiry": "Nov-26",
            "forward": 80.0,
            "current_params": current,
            "candidate_params": candidate["params"],
            "final_params": candidate["params"],
            "current_rmse": candidate["baseline_rmse"],
            "candidate_rmse": candidate["rmse"],
            "row_idx": 0,
        },
        "delta",
        "2026-08-03",
        None,
        payload,
    )

    assert outputs[0] is False
    assert outputs[1] is None
    assert outputs[9].color == "success"
    assert "not published" in outputs[10][0]["message"]
    assert outputs[10][0]["atm_shift"] == pytest.approx(
        candidate["params"]["atm_shift"]
    )
