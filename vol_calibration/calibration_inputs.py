"""Fail-closed preparation of one-expiry calibration observations."""

from __future__ import annotations

from datetime import datetime
import re

import numpy as np
import pandas as pd


UNDISCOUNTED_CALL_DELTA = "undiscounted_call_delta"
MIN_OBSERVED_POINTS = 5
TTF_CALL_DELTA_NODES = np.asarray(
    [0.01, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.99],
    dtype=float,
)
TTF_OBSERVED_SOURCE = "official"
TTF_EXTRAPOLATED_SOURCE_PATTERN = re.compile(
    r"^official_surface_ttf_shape_smile_template_v\d+:extrap$",
    flags=re.IGNORECASE,
)


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
    *,
    include_extrapolated: bool = False,
) -> pd.DataFrame:
    """Select one delivery month under the governed calibration contract.

    The default path preserves the historical observed-only behavior used by
    JKM and other callers.  TTF opts into ``include_extrapolated`` explicitly;
    that path validates a complete homogeneous official smile before assigning
    positive optimizer weights to an approved extrapolated copy.
    """
    if market_data is None or market_data.empty or "expiry" not in market_data:
        return pd.DataFrame()
    target = expiry_month(expiry_label)
    expiry_periods = pd.to_datetime(
        market_data["expiry"],
        errors="coerce",
    ).dt.to_period("M")
    selected = market_data.loc[expiry_periods == target].copy()

    if not include_extrapolated:
        if "weight" in selected.columns:
            selected = selected.loc[
                pd.to_numeric(selected["weight"], errors="coerce").fillna(0.0)
                > 0
            ].copy()
        return selected.reset_index(drop=True)

    required_provenance = {"quote_class", "source_name"}
    missing_provenance = sorted(required_provenance.difference(selected.columns))
    if missing_provenance:
        raise ValueError(
            "TTF calibration provenance is missing: "
            + ", ".join(missing_provenance)
            + "."
        )
    if selected.empty:
        return selected.reset_index(drop=True)

    quote_classes = {
        value
        for value in selected["quote_class"].astype(str).str.strip().str.lower()
        if value
    }
    source_names = {
        value
        for value in selected["source_name"].astype(str).str.strip()
        if value
    }
    if len(quote_classes) != 1 or len(source_names) != 1:
        raise ValueError(
            "TTF calibration requires one homogeneous quote class and source "
            "per expiry."
        )

    basis = next(iter(quote_classes))
    source_name = next(iter(source_names))
    if basis == "observed":
        if source_name.lower() != TTF_OBSERVED_SOURCE:
            raise ValueError(
                f"Unsupported observed TTF calibration source: {source_name}."
            )
        if "weight" not in selected.columns:
            raise ValueError("Observed TTF calibration rows require source weights.")
        source_weights = pd.to_numeric(selected["weight"], errors="coerce")
        if not np.all(np.isfinite(source_weights)) or np.any(source_weights <= 0):
            raise ValueError(
                "Observed TTF calibration weights must be finite and positive."
            )
    elif basis == "extrapolated":
        if TTF_EXTRAPOLATED_SOURCE_PATTERN.fullmatch(source_name) is None:
            raise ValueError(
                f"Unsupported extrapolated TTF calibration source: {source_name}."
            )
        selected["weight"] = 1.0
    else:
        raise ValueError(f"Unsupported TTF calibration quote class: {basis}.")

    selected["calibration_basis"] = basis
    eligibility_error = calibration_eligibility_error(selected)
    if eligibility_error:
        raise ValueError(eligibility_error)
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

    if "calibration_basis" in observations.columns:
        bases = set(
            observations["calibration_basis"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.lower()
        )
        if bases not in ({"observed"}, {"extrapolated"}):
            return "TTF calibration inputs must have one governed calibration basis."
        ordered_delta = np.sort(delta.to_numpy(dtype=float))
        if (
            len(ordered_delta) != len(TTF_CALL_DELTA_NODES)
            or not np.allclose(
                ordered_delta,
                TTF_CALL_DELTA_NODES,
                rtol=0.0,
                atol=1e-10,
            )
        ):
            return "TTF calibration requires the complete governed 11-node delta grid."

        if "weight" not in observations.columns:
            return "TTF calibration inputs require optimizer weights."
        weights = pd.to_numeric(observations["weight"], errors="coerce")
        if not np.all(np.isfinite(weights)) or np.any(weights <= 0):
            return "TTF calibration weights must be finite and positive."
        if (
            bases == {"extrapolated"}
            and not np.allclose(weights, 1.0, rtol=0.0, atol=1e-12)
        ):
            return "Extrapolated TTF calibration weights must be uniformly 1.0."

    iv = pd.to_numeric(observations["iv"], errors="coerce")
    if not np.all(np.isfinite(iv)) or np.any(iv <= 0):
        return "Market implied volatilities must be finite and positive."

    if "delta_convention" in observations.columns:
        conventions = set(observations["delta_convention"].dropna().astype(str))
        if conventions != {UNDISCOUNTED_CALL_DELTA}:
            return "The TTF surface must use the undiscounted call-delta convention."
    return None


def calibration_readiness(
    market_data: pd.DataFrame,
    *,
    include_extrapolated: bool = False,
) -> tuple[bool, str]:
    """Return whether at least one observed expiry is eligible."""
    if market_data is None or market_data.empty:
        return False, "No exact-COB TTF volatility surface is available."
    errors = []
    for expiry in sorted(market_data["expiry"].dropna().unique()):
        try:
            observations = select_expiry_observations(
                market_data,
                expiry,
                include_extrapolated=include_extrapolated,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        error = calibration_eligibility_error(observations)
        if error is None:
            return True, "Calibrate selected expiry"
        errors.append(error)
    reason = errors[0] if errors else "No eligible TTF expiry is available."
    return False, reason
