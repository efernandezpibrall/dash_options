import numpy as np
import pandas as pd
import pytest

from vol_calibration.components.comparison_modal import create_comparison_plot
from vol_calibration.components.smile_grid import delta_curve_to_strike_iv
from options.calibration_engine.converters.delta import strike_to_delta
from options.calibration_engine.models.wing_model import wing_model_iv


@pytest.mark.parametrize("x_axis", ("log_moneyness", "moneyness", "delta"))
def test_comparison_plot_propagates_expiry_dte_to_wing_v2(x_axis):
    market_data = pd.DataFrame(
        {
            "forward": [100.0, 100.0, 100.0],
            "strike": [80.0, 100.0, 120.0],
            "iv": [0.32, 0.25, 0.29],
            "delta": [-0.25, 0.50, 0.25],
            "dte": [45.0, 45.0, 45.0],
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

    assert len(figure.data) == 4
    for trace in figure.data[1:]:
        assert np.isfinite(np.asarray(trace.y, dtype=float)).all()


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
