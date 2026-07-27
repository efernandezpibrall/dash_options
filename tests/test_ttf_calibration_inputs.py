from types import SimpleNamespace

import numpy as np
import pandas as pd

from vol_calibration.calibration_inputs import (
    UNDISCOUNTED_CALL_DELTA,
    calibration_eligibility_error,
    expiry_month,
    select_expiry_observations,
)
from vol_calibration.data_cache import clear_workspace_load_cache
from vol_calibration.pages import ttf


def _valid_ttf_observations():
    return pd.DataFrame(
        {
            "expiry": pd.Timestamp("2026-09-01"),
            "option_expiration_date": pd.Timestamp("2026-08-27"),
            "forward": 50.0,
            "strike": np.nan,
            "iv": [0.57, 0.54, 0.52, 0.53, 0.56],
            "delta": [0.10, 0.25, 0.50, 0.75, 0.90],
            "dte": 78.0,
            "delta_convention": UNDISCOUNTED_CALL_DELTA,
            "source_name": "official",
            "quote_class": "observed",
            "weight": 1.0,
        }
    )


def test_month_label_is_parsed_as_2026_not_year_26():
    assert expiry_month("Sep-26") == pd.Period("2026-09", freq="M")


def test_expiry_selection_excludes_zero_weight_extrapolated_rows():
    market_data = pd.concat(
        [
            _valid_ttf_observations(),
            _valid_ttf_observations().assign(
                source_name="official_template:extrap",
                quote_class="extrapolated",
                weight=0.0,
            ),
        ],
        ignore_index=True,
    )

    selected = select_expiry_observations(market_data, "Sep-26")

    assert len(selected) == 5
    assert set(selected["quote_class"]) == {"observed"}
    assert calibration_eligibility_error(selected) is None


def test_missing_exact_forward_and_option_expiry_fail_closed():
    observations = _valid_ttf_observations().assign(
        forward=np.nan,
        dte=np.nan,
        option_expiration_date=pd.NaT,
    )

    assert calibration_eligibility_error(observations) == (
        "An exact, finite same-COB forward is required for this expiry."
    )


def test_unavailable_cob_disables_calibration_without_synthetic_data(monkeypatch):
    clear_workspace_load_cache()
    monkeypatch.setattr(
        ttf,
        "load_market_data_with_metadata",
        lambda *args, **kwargs: {
            "data": pd.DataFrame(),
            "source": "unavailable",
            "is_synthetic": False,
            "last_update": None,
            "message": "No eligible TTF market data for 2026-06-10",
            "error": "No unified TTF volatility surface for exact COB 2026-06-10",
        },
    )

    result = ttf.load_data("2026-06-10", 0)

    assert result[4] is True
    assert result[6] is True
    assert "No exact-COB TTF volatility surface" in result[5]
    assert "2026-06-10" in result[3]


def test_selected_calibration_accepts_sep_26_and_passes_delta_convention(monkeypatch):
    calls = []

    def fake_calibrate(*args, **kwargs):
        calls.append(kwargs)
        return {
            "params": kwargs["initial_params"],
            "rmse": 0.01,
        }

    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-calibrate-all-btn"),
    )
    monkeypatch.setattr(ttf, "calibrate", fake_calibrate)
    monkeypatch.setattr(ttf, "evaluate_fit", lambda *args, **kwargs: {"rmse": 0.02})
    monkeypatch.setattr(ttf, "create_comparison_plot", lambda *args, **kwargs: {})

    table_data = [
        {
            "expiry": "Sep-26",
            "vr": 0.52,
            "sr": 0.07,
            "pc": 0.31,
            "cc": 0.49,
            "dc": -0.25,
            "uc": 0.30,
            "dsm": 0.50,
            "usm": 0.50,
            "vcr": -0.15,
            "scr": 0.05,
            "ssr": 1.0,
            "put_wing_power": 0.5,
            "call_wing_power": 0.5,
            "rmse": "",
            "arb_status": "Warn",
        }
    ]
    market_json = _valid_ttf_observations().to_json(
        date_format="iso",
        orient="split",
    )

    result = ttf.handle_calibration(
        1,
        None,
        None,
        None,
        None,
        None,
        market_json,
        table_data,
        [0],
        False,
        None,
        "delta",
        "2026-06-10",
        None,
    )

    assert result[0] is True
    assert result[2] == "Expiry: Sep-26"
    assert result[3] == "€50.00/MWh"
    assert calls[0]["delta_convention"] == UNDISCOUNTED_CALL_DELTA
