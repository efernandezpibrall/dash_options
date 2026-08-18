"""SVI-anchored Brent intraday smile adjustments.

The governed operational SVI surface remains the zero-adjustment baseline.
Only four low-dimensional residual terms may move the liquid body intraday;
the fit is regularized toward zero and shrunk until slice and calendar checks
pass.  Raw wing observations are diagnostics, never calibration targets.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

import numpy as np
import pandas as pd
from scipy.optimize import LinearConstraint, minimize

from options.calibration_engine.converters.delta import (
    black76_price,
    delta_to_strike,
)


ADJUSTMENT_PARAMS = (
    "atm_shift",
    "skew_shift",
    "put_curvature_shift",
    "call_curvature_shift",
)
ADJUSTMENT_LABELS = {
    "atm_shift": "ATM shift",
    "skew_shift": "Skew shift",
    "put_curvature_shift": "Put curvature",
    "call_curvature_shift": "Call curvature",
}
ZERO_ADJUSTMENT = {name: 0.0 for name in ADJUSTMENT_PARAMS}
PARAMETER_BOUNDS = {
    "atm_shift": (-0.08, 0.08),
    "skew_shift": (-0.50, 0.50),
    "put_curvature_shift": (-2.00, 2.00),
    "call_curvature_shift": (-2.00, 2.00),
}
PRIOR_SCALES = np.array([0.025, 0.12, 0.50, 0.50], dtype=float)
MIN_ELIGIBLE_STRIKES = 8
MAX_ABS_NODE_SHIFT = 0.10


class BrentAdjustmentError(RuntimeError):
    """Raised when an intraday adjustment is not identifiable or safe."""

    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def adjustment_basis(log_moneyness) -> np.ndarray:
    k = np.asarray(log_moneyness, dtype=float)
    return np.column_stack(
        [
            np.ones_like(k),
            k,
            np.minimum(k, 0.0) ** 2,
            np.maximum(k, 0.0) ** 2,
        ]
    )


def adjustment_values(params: Mapping[str, float], log_moneyness) -> np.ndarray:
    coefficients = np.asarray(
        [float(params.get(name, 0.0)) for name in ADJUSTMENT_PARAMS],
        dtype=float,
    )
    return adjustment_basis(log_moneyness) @ coefficients


def _expiry_timestamp(value) -> pd.Timestamp:
    if isinstance(value, str):
        try:
            return pd.Timestamp(datetime.strptime(value.strip(), "%b-%y")).normalize()
        except ValueError:
            pass
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise BrentAdjustmentError(f"Invalid Brent expiry: {value!r}")
    return pd.Timestamp(parsed).normalize()


def select_expiry_rows(data: pd.DataFrame, expiry) -> pd.DataFrame:
    if data is None or data.empty:
        return pd.DataFrame()
    target = _expiry_timestamp(expiry).to_period("M")
    periods = pd.to_datetime(data["expiry"], errors="coerce").dt.to_period("M")
    return data.loc[periods == target].copy().reset_index(drop=True)


def select_surface_slice(surface: pd.DataFrame, expiry) -> pd.DataFrame:
    if surface is None or surface.empty:
        return pd.DataFrame()
    target = _expiry_timestamp(expiry).to_period("M")
    periods = pd.to_datetime(
        surface["contract_date"], errors="coerce"
    ).dt.to_period("M")
    selected = surface.loc[periods == target].copy()
    if selected.empty:
        return selected
    selected["call_delta"] = pd.to_numeric(
        selected.get("delta_abs", selected.get("delta")), errors="coerce"
    )
    selected["baseline_iv"] = pd.to_numeric(
        selected["volatility"], errors="coerce"
    )
    selected = selected.dropna(subset=["call_delta", "baseline_iv"])
    selected = selected[
        selected["call_delta"].between(0.0, 1.0, inclusive="neither")
        & (selected["baseline_iv"] > 0)
    ]
    return selected.sort_values("call_delta").drop_duplicates("call_delta")


def surface_nodes(
    surface_slice: pd.DataFrame,
    forward: float,
    dte: float,
) -> pd.DataFrame:
    if surface_slice is None or surface_slice.empty:
        raise BrentAdjustmentError("No exact-COB Brent SVI baseline exists for this expiry.")
    if not np.isfinite(forward) or forward <= 0 or not np.isfinite(dte) or dte <= 0:
        raise BrentAdjustmentError("A positive same-snapshot forward and actual DTE are required.")

    rows = []
    for row in surface_slice.itertuples(index=False):
        call_delta = float(row.call_delta)
        baseline_iv = float(row.baseline_iv)
        strike = delta_to_strike(
            call_delta,
            float(forward),
            baseline_iv,
            float(dte),
            option_type="call",
        )
        rows.append(
            {
                "call_delta": call_delta,
                "baseline_iv": baseline_iv,
                "strike": strike,
                "log_moneyness": float(np.log(strike / forward)),
            }
        )
    return pd.DataFrame(rows).sort_values("call_delta").reset_index(drop=True)


def _market_call_delta(observations: pd.DataFrame) -> pd.Series:
    signed = pd.to_numeric(observations["delta"], errors="coerce")
    return signed.where(signed > 0, signed + 1.0)


def prepare_adjustment_fit(
    market_expiry: pd.DataFrame,
    surface_slice: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if market_expiry is None or market_expiry.empty:
        raise BrentAdjustmentError("No observed Brent option quotes exist for this expiry.")
    required = {"strike", "forward", "dte", "delta", "iv"}
    missing = sorted(required.difference(market_expiry.columns))
    if missing:
        raise BrentAdjustmentError(
            "Brent calibration inputs are missing: " + ", ".join(missing)
        )

    eligible = market_expiry.copy()
    if "calibration_eligible" in eligible.columns:
        eligible = eligible.loc[eligible["calibration_eligible"].fillna(False)]
    if "weight" in eligible.columns:
        eligible = eligible.loc[
            pd.to_numeric(eligible["weight"], errors="coerce").fillna(0.0) > 0
        ]

    for column in ("strike", "forward", "dte", "delta", "iv"):
        eligible[column] = pd.to_numeric(eligible[column], errors="coerce")
    eligible = eligible.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["strike", "forward", "dte", "delta", "iv"]
    )
    eligible = eligible[
        (eligible["strike"] > 0)
        & (eligible["forward"] > 0)
        & (eligible["dte"] > 0)
        & (eligible["iv"] > 0)
    ].copy()
    eligible = eligible.sort_values("strike").drop_duplicates("strike")
    if len(eligible) < MIN_ELIGIBLE_STRIKES:
        raise BrentAdjustmentError(
            f"Only {len(eligible)} eligible Brent strikes remain; "
            f"at least {MIN_ELIGIBLE_STRIKES} are required."
        )
    if not np.allclose(eligible["forward"], eligible["forward"].iloc[0]):
        raise BrentAdjustmentError("Eligible quotes do not share one snapshot forward.")
    if not np.allclose(eligible["dte"], eligible["dte"].iloc[0]):
        raise BrentAdjustmentError("Eligible quotes do not share one actual option expiry.")

    nodes = surface_nodes(
        surface_slice,
        float(eligible["forward"].iloc[0]),
        float(eligible["dte"].iloc[0]),
    )
    call_delta = _market_call_delta(eligible)
    if (
        ~np.isfinite(call_delta)
        | ~call_delta.between(
            float(nodes["call_delta"].min()),
            float(nodes["call_delta"].max()),
            inclusive="both",
        )
    ).any():
        raise BrentAdjustmentError(
            "Eligible quotes fall outside the governed SVI delta support."
        )

    eligible["call_delta"] = call_delta
    eligible["baseline_iv"] = np.interp(
        call_delta,
        nodes["call_delta"],
        nodes["baseline_iv"],
    )
    eligible["log_moneyness"] = np.log(
        eligible["strike"] / eligible["forward"]
    )
    eligible["baseline_residual"] = eligible["iv"] - eligible["baseline_iv"]
    raw_weight = (
        pd.to_numeric(eligible.get("weight", 1.0), errors="coerce")
        .fillna(0.0)
        .to_numpy(dtype=float)
    )
    positive = raw_weight[raw_weight > 0]
    cap = float(np.quantile(positive, 0.95)) if len(positive) else 1.0
    fit_weight = np.sqrt(np.clip(raw_weight, 0.0, cap))
    fit_weight = fit_weight / max(float(np.mean(fit_weight)), 1e-12)
    eligible["fit_weight"] = fit_weight
    return eligible.reset_index(drop=True), nodes


def evaluate_adjustment(
    params: Mapping[str, float],
    prepared: pd.DataFrame,
) -> dict[str, Any]:
    adjustment = adjustment_values(params, prepared["log_moneyness"])
    fitted = prepared["baseline_iv"].to_numpy(dtype=float) + adjustment
    observed = prepared["iv"].to_numpy(dtype=float)
    residual = fitted - observed
    weights = prepared["fit_weight"].to_numpy(dtype=float)
    return {
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "weighted_rmse": float(
            np.sqrt(np.sum(weights * residual**2) / np.sum(weights))
        ),
        "mae": float(np.mean(np.abs(residual))),
        "max_error": float(np.max(np.abs(residual))),
        "fitted_iv": fitted,
        "residuals": residual,
        "n_points": int(len(prepared)),
    }


def _slice_is_arbitrage_safe(
    params: Mapping[str, float],
    nodes: pd.DataFrame,
    forward: float,
    dte: float,
) -> tuple[bool, str | None, pd.DataFrame]:
    checked = nodes.copy()
    checked["adjustment"] = adjustment_values(params, checked["log_moneyness"])
    checked["candidate_iv"] = checked["baseline_iv"] + checked["adjustment"]
    if (
        not np.isfinite(checked["candidate_iv"]).all()
        or (checked["candidate_iv"] <= 0.01).any()
        or (checked["candidate_iv"] >= 2.0).any()
    ):
        return False, "candidate volatility is outside governed bounds", checked
    if checked["adjustment"].abs().max() > MAX_ABS_NODE_SHIFT + 1e-12:
        return False, "candidate moves a governed node by more than 10 vol points", checked

    checked["candidate_strike"] = [
        delta_to_strike(
            float(delta),
            float(forward),
            float(volatility),
            float(dte),
            option_type="call",
        )
        for delta, volatility in zip(
            checked["call_delta"], checked["candidate_iv"]
        )
    ]
    checked["call_price"] = [
        black76_price(
            float(forward),
            float(strike),
            float(volatility),
            float(dte),
            option_type="call",
        )
        for strike, volatility in zip(
            checked["candidate_strike"], checked["candidate_iv"]
        )
    ]
    by_strike = checked.sort_values("candidate_strike")
    strikes = by_strike["candidate_strike"].to_numpy(dtype=float)
    prices = by_strike["call_price"].to_numpy(dtype=float)
    if np.any(np.diff(prices) > 1e-7):
        return False, "candidate call prices are not decreasing in strike", checked
    slopes = np.diff(prices) / np.diff(strikes)
    if np.any(np.diff(slopes) < -1e-5):
        return False, "candidate slice fails discrete butterfly convexity", checked
    return True, None, checked


def _calendar_is_safe(
    checked_nodes: pd.DataFrame,
    expiry,
    full_surface: pd.DataFrame | None,
    cob_date,
) -> tuple[bool, str | None]:
    if full_surface is None or full_surface.empty or cob_date is None:
        return True, None
    cob = _expiry_timestamp(cob_date)
    target_expiry = _expiry_timestamp(expiry)
    target_t = None
    surface_parts = []
    for contract_date, group in full_surface.groupby("contract_date"):
        selected = select_surface_slice(group, contract_date)
        if selected.empty:
            continue
        expiration = pd.to_datetime(
            selected["option_expiration_date"], errors="coerce"
        ).dropna()
        if expiration.empty:
            continue
        time_value = (expiration.iloc[0].normalize() - cob).days / 365.25
        if time_value <= 0:
            continue
        part = selected[["call_delta", "baseline_iv"]].copy()
        part["contract_date"] = _expiry_timestamp(contract_date)
        part["time"] = time_value
        surface_parts.append(part)
        if _expiry_timestamp(contract_date).to_period("M") == target_expiry.to_period("M"):
            target_t = time_value
    if not surface_parts or target_t is None:
        return True, None

    surface = pd.concat(surface_parts, ignore_index=True)
    candidate = checked_nodes[["call_delta", "candidate_iv"]].copy()
    candidate["total_variance"] = candidate["candidate_iv"] ** 2 * target_t
    for row in candidate.itertuples(index=False):
        same_delta = surface[np.isclose(surface["call_delta"], row.call_delta)].copy()
        previous = same_delta[same_delta["time"] < target_t].sort_values("time")
        following = same_delta[same_delta["time"] > target_t].sort_values("time")
        if not previous.empty:
            prev = previous.iloc[-1]
            prev_variance = float(prev["baseline_iv"]) ** 2 * float(prev["time"])
            if row.total_variance < prev_variance - 1e-8:
                return False, "candidate total variance falls below the prior expiry"
        if not following.empty:
            nxt = following.iloc[0]
            next_variance = float(nxt["baseline_iv"]) ** 2 * float(nxt["time"])
            if row.total_variance > next_variance + 1e-8:
                return False, "candidate total variance exceeds the next expiry"
    return True, None


def _calendar_adjustment_bounds(
    nodes: pd.DataFrame,
    expiry,
    full_surface: pd.DataFrame | None,
    cob_date,
) -> tuple[np.ndarray, np.ndarray]:
    """Return node-level adjustment bounds implied by adjacent expiries."""
    lower = np.full(len(nodes), -MAX_ABS_NODE_SHIFT, dtype=float)
    upper = np.full(len(nodes), MAX_ABS_NODE_SHIFT, dtype=float)
    baseline = nodes["baseline_iv"].to_numpy(dtype=float)
    lower = np.maximum(lower, 0.010001 - baseline)
    upper = np.minimum(upper, 1.999999 - baseline)
    if full_surface is None or full_surface.empty or cob_date is None or expiry is None:
        return lower, upper

    cob = _expiry_timestamp(cob_date)
    target_expiry = _expiry_timestamp(expiry)
    surface_parts = []
    target_t = None
    for contract_date, group in full_surface.groupby("contract_date"):
        selected = select_surface_slice(group, contract_date)
        if selected.empty:
            continue
        expiration = pd.to_datetime(
            selected["option_expiration_date"], errors="coerce"
        ).dropna()
        if expiration.empty:
            continue
        time_value = (expiration.iloc[0].normalize() - cob).days / 365.25
        if time_value <= 0:
            continue
        part = selected[["call_delta", "baseline_iv"]].copy()
        part["time"] = time_value
        part["contract_date"] = _expiry_timestamp(contract_date)
        surface_parts.append(part)
        if _expiry_timestamp(contract_date).to_period("M") == target_expiry.to_period("M"):
            target_t = time_value
    if not surface_parts or target_t is None:
        return lower, upper

    surface = pd.concat(surface_parts, ignore_index=True)
    for index, node in nodes.reset_index(drop=True).iterrows():
        same_delta = surface[
            np.isclose(surface["call_delta"], float(node["call_delta"]))
        ]
        previous = same_delta[same_delta["time"] < target_t].sort_values("time")
        following = same_delta[same_delta["time"] > target_t].sort_values("time")
        if not previous.empty:
            prev = previous.iloc[-1]
            minimum_iv = np.sqrt(
                float(prev["baseline_iv"]) ** 2 * float(prev["time"]) / target_t
            )
            lower[index] = max(lower[index], minimum_iv - baseline[index])
        if not following.empty:
            nxt = following.iloc[0]
            maximum_iv = np.sqrt(
                float(nxt["baseline_iv"]) ** 2 * float(nxt["time"]) / target_t
            )
            upper[index] = min(upper[index], maximum_iv - baseline[index])
        # The official baseline is the governed feasible point.  Keep it in the
        # interval when adjacent total variances are equal to numerical noise.
        lower[index] = min(lower[index], 0.0)
        upper[index] = max(upper[index], 0.0)
    return lower, upper


def validate_adjustment(
    params: Mapping[str, float],
    nodes: pd.DataFrame,
    *,
    forward: float,
    dte: float,
    expiry=None,
    full_surface: pd.DataFrame | None = None,
    cob_date=None,
) -> dict[str, Any]:
    slice_valid, reason, checked = _slice_is_arbitrage_safe(
        params, nodes, forward, dte
    )
    if not slice_valid:
        return {"is_valid": False, "reason": reason, "nodes": checked}
    calendar_valid, calendar_reason = _calendar_is_safe(
        checked, expiry, full_surface, cob_date
    )
    return {
        "is_valid": bool(calendar_valid),
        "reason": calendar_reason,
        "nodes": checked,
    }


def calibrate_adjustment(
    market_expiry: pd.DataFrame,
    surface_slice: pd.DataFrame,
    *,
    expiry=None,
    full_surface: pd.DataFrame | None = None,
    cob_date=None,
    prior_strength: float = 0.001,
) -> dict[str, Any]:
    """Fit and validate a regularized residual adjustment to the SVI baseline."""
    prepared, nodes = prepare_adjustment_fit(market_expiry, surface_slice)
    basis = adjustment_basis(prepared["log_moneyness"])
    target = prepared["baseline_residual"].to_numpy(dtype=float)
    fit_weight = prepared["fit_weight"].to_numpy(dtype=float)
    sqrt_weight = np.sqrt(fit_weight)
    lower = np.asarray([PARAMETER_BOUNDS[name][0] for name in ADJUSTMENT_PARAMS])
    upper = np.asarray([PARAMETER_BOUNDS[name][1] for name in ADJUSTMENT_PARAMS])

    del sqrt_weight

    def objective(coefficients):
        market_residual = basis @ coefficients - target
        market_loss = np.sum(fit_weight * market_residual**2)
        prior_loss = prior_strength * np.sum((coefficients / PRIOR_SCALES) ** 2)
        return float(market_loss + prior_loss)

    node_basis = adjustment_basis(nodes["log_moneyness"])
    node_lower, node_upper = _calendar_adjustment_bounds(
        nodes, expiry, full_surface, cob_date
    )
    result = minimize(
        objective,
        x0=np.zeros(len(ADJUSTMENT_PARAMS)),
        method="SLSQP",
        bounds=list(zip(lower, upper)),
        constraints=[LinearConstraint(node_basis, node_lower, node_upper)],
        options={"maxiter": 1000, "ftol": 1e-12},
    )
    if not result.success or not np.isfinite(result.x).all():
        raise BrentAdjustmentError(
            "Brent intraday adjustment optimizer did not converge.",
            diagnostics={"message": str(result.message)},
        )

    raw_params = {
        name: float(value) for name, value in zip(ADJUSTMENT_PARAMS, result.x)
    }
    forward = float(prepared["forward"].iloc[0])
    dte = float(prepared["dte"].iloc[0])
    applied_scale = 1.0
    validation = validate_adjustment(
        raw_params,
        nodes,
        forward=forward,
        dte=dte,
        expiry=expiry,
        full_surface=full_surface,
        cob_date=cob_date,
    )
    while not validation["is_valid"] and applied_scale > 1.0 / 128.0:
        applied_scale *= 0.5
        scaled = {name: value * applied_scale for name, value in raw_params.items()}
        validation = validate_adjustment(
            scaled,
            nodes,
            forward=forward,
            dte=dte,
            expiry=expiry,
            full_surface=full_surface,
            cob_date=cob_date,
        )
    if not validation["is_valid"]:
        raise BrentAdjustmentError(
            "No non-zero Brent adjustment passed slice and calendar validation.",
            diagnostics={"validation_reason": validation.get("reason")},
        )

    params = {name: value * applied_scale for name, value in raw_params.items()}
    baseline_fit = evaluate_adjustment(ZERO_ADJUSTMENT, prepared)
    candidate_fit = evaluate_adjustment(params, prepared)
    return {
        "params": params,
        "raw_params": raw_params,
        "baseline_rmse": baseline_fit["rmse"],
        "rmse": candidate_fit["rmse"],
        "weighted_rmse": candidate_fit["weighted_rmse"],
        "max_error": candidate_fit["max_error"],
        "n_points": candidate_fit["n_points"],
        "success": True,
        "message": str(result.message),
        "shrink_factor": applied_scale,
        "validation": {
            "is_valid": True,
            "reason": None,
            "max_abs_node_shift": float(
                validation["nodes"]["adjustment"].abs().max()
            ),
        },
        "prepared": prepared,
        "nodes": validation["nodes"],
    }


def baseline_row(
    market_expiry: pd.DataFrame,
    surface_slice: pd.DataFrame,
    expiry,
) -> dict[str, Any]:
    eligible_count = int(
        market_expiry.get(
            "calibration_eligible",
            pd.Series(False, index=market_expiry.index),
        ).fillna(False).sum()
    )
    excluded_count = int(len(market_expiry) - eligible_count)
    row: dict[str, Any] = {
        "expiry": _expiry_timestamp(expiry),
        **ZERO_ADJUSTMENT,
        "eligible_points": eligible_count,
        "excluded_points": excluded_count,
        "validation": "Blocked",
        "rmse": np.nan,
        "message": "",
    }
    try:
        prepared, nodes = prepare_adjustment_fit(market_expiry, surface_slice)
        fit = evaluate_adjustment(ZERO_ADJUSTMENT, prepared)
        validation = validate_adjustment(
            ZERO_ADJUSTMENT,
            nodes,
            forward=float(prepared["forward"].iloc[0]),
            dte=float(prepared["dte"].iloc[0]),
        )
        row["eligible_points"] = int(len(prepared))
        row["rmse"] = fit["rmse"]
        row["validation"] = "Pass" if validation["is_valid"] else "Fail"
        row["message"] = validation.get("reason") or "SVI baseline ready"
    except BrentAdjustmentError as exc:
        row["message"] = str(exc)
    return row
