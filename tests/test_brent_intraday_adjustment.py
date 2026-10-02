"""Retain numerical Brent adjustment coverage through the canonical analytics API."""

import numpy as np
import pandas as pd
import pytest

from options.calibration_engine.converters.delta import delta_to_strike, strike_to_delta
from options.vol_calibration.api import (
    calibrate_adjustment,
    select_surface_slice,
)


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
