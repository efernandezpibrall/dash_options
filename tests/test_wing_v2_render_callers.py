import numpy as np
import pandas as pd
import pytest

from vol_calibration.components.comparison_modal import create_comparison_plot
from vol_calibration.components.smile_grid import delta_curve_to_strike_iv
from options.calibration_engine.converters.delta import strike_to_delta
from options.calibration_engine.models.wing_model import wing_model_iv
from options.vol_calibration import api as calibration_api


@pytest.mark.parametrize(
    ("x_axis", "dte", "expected_deep_call_iv_pct"),
    [
        ("log_moneyness", 45.0, 69.90422739296535),
        ("log_moneyness", 180.0, 41.316406569931175),
        ("moneyness", 45.0, 33.377235638018085),
        ("moneyness", 180.0, 27.638834358723347),
        ("delta", 45.0, 136.67808942273334),
        ("delta", 180.0, 76.81049013348635),
    ],
)
def test_comparison_plot_propagates_actual_dte_and_preserves_deep_call_values(
    monkeypatch, x_axis, dte, expected_deep_call_iv_pct,
):
    calls = []
    real_model = calibration_api.wing_model_iv

    def recorded_model(*args, **kwargs):
        calls.append((kwargs.get("dte"), kwargs.get("model_version")))
        return real_model(*args, **kwargs)

    monkeypatch.setattr(calibration_api, "wing_model_iv", recorded_model)
    market_data = pd.DataFrame(
        {
            "forward": [100.0, 100.0, 100.0],
            "strike": [80.0, 100.0, 120.0],
            "iv": [0.32, 0.25, 0.29],
            "delta": [-0.25, 0.50, 0.25],
            "dte": [dte, dte, dte],
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
        "put_wing_power": 0.2,
        "call_wing_power": 0.2,
    }

    figure = create_comparison_plot(
        market_data,
        params,
        params,
        params,
        "Sep-26",
        x_axis=x_axis,
        model_version="wing_v2",
    )

    assert [trace.name for trace in figure.data] == ["Market", "Current", "Candidate", "Final"]
    assert len(calls) >= 3
    assert set(calls) == {(dte, "wing_v2")}
    # Frozen independently from the specified deep-call equation: boundary
    # x=0.22, sigma=0.2544, scale=0.02 and total-variance slope=0.2.
    # Delta-axis values solve Black-76 call delta=0.005 using that equation.
    tolerance = 1e-4 if x_axis == "delta" else 1e-11
    for trace in figure.data[1:]:
        assert np.isfinite(np.asarray(trace.y, dtype=float)).all()
        assert trace.y[-1] == pytest.approx(expected_deep_call_iv_pct, rel=tolerance)


def test_feb27_extreme_call_delta_is_not_clipped_at_five_forwards():
    params = {
        "vr": 0.8168231078222267,
        "sr": 0.371405453676184,
        "pc": 0.0007992663670776708,
        "cc": 0.0,
        "dc": -0.4753985732518675,
        "uc": 0.7064379666366815,
        "dsm": 0.5,
        "usm": 0.5,
        "vcr": -0.15,
        "scr": 0.05,
        "ssr": 1.0,
        "put_wing_power": 0.005866142634613864,
        "call_wing_power": 0.23511190488256495,
    }
    target, strikes, ivs = delta_curve_to_strike_iv(
        np.asarray([0.01]),
        55.069,
        181.0,
        params,
        wing_model_iv,
        is_put=False,
        model_version="wing_v2",
    )

    assert target == pytest.approx([0.01])
    assert strikes[0] > 5.0 * 55.069
    assert ivs[0] > 1.35
    assert strike_to_delta(
        strikes[0], 55.069, ivs[0], 181.0, "call"
    ) == pytest.approx(0.01, abs=1e-6)
