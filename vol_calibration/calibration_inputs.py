"""Fail-closed preparation of one-expiry calibration observations."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd


UNDISCOUNTED_CALL_DELTA = "undiscounted_call_delta"
MIN_OBSERVED_POINTS = 5


def expiry_month(value) -> pd.Period:
    """Parse a table expiry such as ``Sep-26`` without year-26 ambiguity."""
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return pd.Period(datetime.strptime(stripped, "%b-%y"), freq="M")
        except ValueError:
            pass
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Invalid expiry label: {value!r}")
    return pd.Period(parsed, freq="M")


def select_expiry_observations(
    market_data: pd.DataFrame,
    expiry_label,
) -> pd.DataFrame:
    """Select one delivery month and retain observed calibration rows only."""
    if market_data is None or market_data.empty or "expiry" not in market_data:
        return pd.DataFrame()
    target = expiry_month(expiry_label)
    expiry_periods = pd.to_datetime(
        market_data["expiry"],
        errors="coerce",
    ).dt.to_period("M")
    selected = market_data.loc[expiry_periods == target].copy()
    if "weight" in selected.columns:
        selected = selected.loc[
            pd.to_numeric(selected["weight"], errors="coerce").fillna(0.0) > 0
        ].copy()
    return selected.reset_index(drop=True)


def calibration_eligibility_error(
    observations: pd.DataFrame,
) -> str | None:
    """Return the first reason a real-market calibration must be blocked."""
    if observations is None or observations.empty:
        return "No observed TTF smile is available for the selected COB and expiry."
    if len(observations) < MIN_OBSERVED_POINTS:
        return (
            f"Only {len(observations)} observed quotes are available; "
            f"at least {MIN_OBSERVED_POINTS} are required."
        )

    required = ("forward", "dte", "delta", "iv")
    missing = [name for name in required if name not in observations.columns]
    if missing:
        return "Calibration inputs are missing: " + ", ".join(missing) + "."

    forward = pd.to_numeric(observations["forward"], errors="coerce")
    if (
        not np.all(np.isfinite(forward))
        or np.any(forward <= 0)
        or not np.allclose(forward, forward.iloc[0], rtol=1e-10, atol=1e-12)
    ):
        return "An exact, finite same-COB forward is required for this expiry."

    dte = pd.to_numeric(observations["dte"], errors="coerce")
    if (
        not np.all(np.isfinite(dte))
        or np.any(dte <= 0)
        or not np.allclose(dte, dte.iloc[0], rtol=0, atol=1e-9)
    ):
        return (
            "A verified option-expiration date and positive actual DTE are "
            "required for this expiry."
        )

    delta = pd.to_numeric(observations["delta"], errors="coerce")
    if (
        not np.all(np.isfinite(delta))
        or np.any(delta <= 0)
        or np.any(delta >= 1)
        or delta.duplicated().any()
    ):
        return "Call-delta observations must be distinct and strictly between 0 and 1."

    iv = pd.to_numeric(observations["iv"], errors="coerce")
    if not np.all(np.isfinite(iv)) or np.any(iv <= 0):
        return "Market implied volatilities must be finite and positive."

    if "delta_convention" in observations.columns:
        conventions = set(observations["delta_convention"].dropna().astype(str))
        if conventions != {UNDISCOUNTED_CALL_DELTA}:
            return "The TTF surface must use the undiscounted call-delta convention."
    return None


def calibration_readiness(market_data: pd.DataFrame) -> tuple[bool, str]:
    """Return whether at least one observed expiry is eligible."""
    if market_data is None or market_data.empty:
        return False, "No exact-COB TTF volatility surface is available."
    errors = []
    for expiry in sorted(market_data["expiry"].dropna().unique()):
        observations = select_expiry_observations(market_data, expiry)
        error = calibration_eligibility_error(observations)
        if error is None:
            return True, "Calibrate selected expiry"
        errors.append(error)
    reason = errors[0] if errors else "No eligible TTF expiry is available."
    return False, reason
