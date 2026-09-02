"""TTF PCHIP-core / Wing-v2-tail operational smile.

The governed 11-node TTF smile is authoritative between its minimum and
maximum strikes.  A shape-preserving PCHIP interpolates total variance in
log-moneyness there.  Wing-v2 is calibrated only as a tail model and joined to
the core outside the quoted range with C1 cubic-Hermite transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator
from scipy.optimize import minimize
from scipy.stats import norm

from options.calibration_engine.config.bounds import BOUNDS
from options.calibration_engine.config.defaults import PARAM_ORDER, get_defaults
from options.calibration_engine.config.calibration_policies import (
    TTF_WING_V2_FREE_PARAMS,
    TTF_WING_V2_OPTIMIZER_OPTIONS,
    TTF_WING_V2_PARAMETER_BOUNDS,
)
from options.calibration_engine.models.wing_model import WING_V2, wing_model_iv
from options.calibration_engine.validation.arbitrage import compute_g_function

from vol_calibration.calibration_inputs import calibration_eligibility_error


DAYS_PER_YEAR = 365.0
GAS_HYBRID_METHOD = "PCHIP-core/Wing-v2-tail hybrid"
GAS_HYBRID_POLICY_VERSIONS = {
    "TTF": "ttf_pchip_core_wing_tail_hybrid_v1",
    "JKM": "jkm_pchip_core_wing_tail_hybrid_v1",
    "NBP": "nbp_pchip_core_wing_tail_hybrid_v1",
}
BRENT_HYBRID_METHOD = (
    "SVI anchor + PCHIP-core/Wing-v2-tail hybrid "
    "(bounded to governed 1%-99% delta range)"
)
BRENT_HYBRID_POLICY_VERSION = "brent_svi_projected_pchip_core_hybrid_v2"
HH_HYBRID_METHOD = (
    "SVI-body/seasonal-relative LNE settlement + "
    "PCHIP-core/Wing-v2-tail hybrid (bounded to governed 1%-99% delta range)"
)
HH_HYBRID_POLICY_VERSION = "hh_lne_projected_pchip_core_hybrid_v2"
HYBRID_POLICIES = {
    "BRENT": (BRENT_HYBRID_METHOD, BRENT_HYBRID_POLICY_VERSION),
    "HH": (HH_HYBRID_METHOD, HH_HYBRID_POLICY_VERSION),
    **{
        product: (GAS_HYBRID_METHOD, version)
        for product, version in GAS_HYBRID_POLICY_VERSIONS.items()
    },
}
TTF_HYBRID_POLICY_VERSION = GAS_HYBRID_POLICY_VERSIONS["TTF"]
TTF_HYBRID_METHOD = GAS_HYBRID_METHOD
TTF_CORE_SAMPLE_COUNT = 201
CANONICAL_SURFACE_POINT_COUNT = 401
TTF_VALIDATION_POINT_COUNT = 4001
TTF_BLEND_WIDTHS = (0.10, 0.15, 0.20, 0.30)
TTF_BUTTERFLY_MARGIN = 0.006
TTF_VALIDATION_EXTENSION = 0.30
TTF_VALIDATION_FLOOR = -0.50
TTF_VALIDATION_CEILING = 0.50


def gas_hybrid_policy(commodity: str) -> tuple[str, str]:
    """Return the common method and explicit product policy version."""
    product = str(commodity).strip().upper()
    if product not in GAS_HYBRID_POLICY_VERSIONS:
        raise ValueError(f"Unsupported governed gas hybrid product: {commodity}.")
    return hybrid_policy(product)


def hybrid_policy(commodity: str) -> tuple[str, str]:
    """Return the finalizer method and policy for any governed product."""
    product = str(commodity).strip().upper()
    try:
        return HYBRID_POLICIES[product]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported governed hybrid product: {commodity}."
        ) from exc


@dataclass(frozen=True)
class TTFPchipCore:
    """Validated one-expiry total-variance PCHIP core."""

    x_nodes: np.ndarray
    strike_nodes: np.ndarray
    iv_nodes: np.ndarray
    delta_nodes: np.ndarray
    total_variance_nodes: np.ndarray
    forward: float
    dte: float
    source_name: str
    calibration_basis: str
    interpolator: PchipInterpolator
    commodity: str = "TTF"

    @property
    def time_to_expiry(self) -> float:
        return self.dte / DAYS_PER_YEAR

    @property
    def xmin(self) -> float:
        return float(self.x_nodes[0])

    @property
    def xmax(self) -> float:
        return float(self.x_nodes[-1])

    def total_variance(self, x: Iterable[float] | float) -> np.ndarray:
        return np.asarray(self.interpolator(x), dtype=float)

    def derivative(self, x: float) -> float:
        return float(self.interpolator.derivative()(x))


def build_ttf_pchip_core(
    observations: pd.DataFrame,
    *,
    commodity: str = "TTF",
) -> TTFPchipCore:
    """Validate governed observations and build the authoritative PCHIP core."""
    product = str(commodity).strip().upper()
    hybrid_policy(product)
    eligibility_error = calibration_eligibility_error(
        observations,
        commodity=product,
    )
    if eligibility_error:
        raise ValueError(eligibility_error)
    if "strike" not in observations.columns:
        raise ValueError(
            f"{product} hybrid inputs require original governed strikes."
        )

    strikes = pd.to_numeric(observations["strike"], errors="coerce").to_numpy()
    if (
        not np.all(np.isfinite(strikes))
        or np.any(strikes <= 0)
        or np.unique(np.round(strikes, decimals=12)).size != len(strikes)
    ):
        raise ValueError(
            f"{product} hybrid inputs require distinct, finite, positive official strikes."
        )

    forwards = pd.to_numeric(observations["forward"], errors="coerce").to_numpy()
    dtes = pd.to_numeric(observations["dte"], errors="coerce").to_numpy()
    ivs = pd.to_numeric(observations["iv"], errors="coerce").to_numpy()
    deltas = pd.to_numeric(observations["delta"], errors="coerce").to_numpy()
    forward = float(forwards[0])
    dte = float(dtes[0])
    time_to_expiry = dte / DAYS_PER_YEAR
    x = np.log(strikes / forward)
    total_variance = time_to_expiry * ivs**2

    if not np.all(np.isfinite(x)) or np.unique(np.round(x, 12)).size != len(x):
        raise ValueError(
            f"{product} hybrid log-moneyness nodes must be finite and distinct."
        )
    if not np.all(np.isfinite(total_variance)) or np.any(total_variance <= 0):
        raise ValueError(
            f"{product} hybrid total-variance nodes must be finite and positive."
        )

    order = np.argsort(x)
    x = x[order]
    strikes = strikes[order]
    ivs = ivs[order]
    deltas = deltas[order]
    total_variance = total_variance[order]
    interpolator = PchipInterpolator(x, total_variance, extrapolate=False)
    reproduced = np.asarray(interpolator(x), dtype=float)
    if not np.allclose(reproduced, total_variance, rtol=0.0, atol=1e-13):
        raise ValueError(
            f"{product} PCHIP core did not reproduce the governed nodes exactly."
        )

    source_names = observations["source_name"].astype(str).str.strip().unique()
    bases = observations["calibration_basis"].astype(str).str.strip().str.lower().unique()
    if len(source_names) != 1 or len(bases) != 1:
        raise ValueError(
            f"{product} hybrid inputs require homogeneous source provenance."
        )

    return TTFPchipCore(
        x_nodes=x,
        strike_nodes=strikes,
        iv_nodes=ivs,
        delta_nodes=deltas,
        total_variance_nodes=total_variance,
        forward=forward,
        dte=dte,
        source_name=str(source_names[0]),
        calibration_basis=str(bases[0]),
        interpolator=interpolator,
        commodity=product,
    )


def _hybrid_validation_range(core: TTFPchipCore) -> tuple[float, float]:
    """Return the governed strike range for complete-smile validation.

    Brent and HH already supply 1%-99% source-specific fixed-delta tails, whose
    long-dated log-moneyness can be much wider than the Wing-v2 cutoff domain.
    Their dense authority is bounded to those governed endpoints. The three
    same-source gas products retain the established Wing-v2 extension.
    """

    if core.commodity in {"BRENT", "HH"}:
        return core.xmin, core.xmax
    return (
        min(TTF_VALIDATION_FLOOR, core.xmin - TTF_VALIDATION_EXTENSION),
        max(TTF_VALIDATION_CEILING, core.xmax + TTF_VALIDATION_EXTENSION),
    )


def _wing_total_variance(
    x: np.ndarray,
    core: TTFPchipCore,
    params: Mapping[str, float],
) -> np.ndarray:
    strikes = core.forward * np.exp(np.asarray(x, dtype=float))
    iv = np.asarray(
        wing_model_iv(
            strike=strikes,
            forward=core.forward,
            dte=core.dte,
            model_version=WING_V2,
            **{name: float(params[name]) for name in PARAM_ORDER},
        ),
        dtype=float,
    )
    return core.time_to_expiry * iv**2


def _wing_boundary_value_and_derivative(
    x: float,
    core: TTFPchipCore,
    params: Mapping[str, float],
) -> tuple[float, float]:
    scale = max(1.0, abs(float(x)))
    step = 1e-5 * scale
    samples = np.asarray([x - step, x, x + step], dtype=float)
    values = _wing_total_variance(samples, core, params)
    derivative = (values[2] - values[0]) / (2.0 * step)
    return float(values[1]), float(derivative)


def _hermite_values(
    x: np.ndarray,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    dy0: float,
    dy1: float,
) -> np.ndarray:
    """Evaluate a cubic Hermite segment from values and x-derivatives."""
    width = float(x1 - x0)
    if not np.isfinite(width) or width <= 0:
        raise ValueError("TTF hybrid blend endpoints must be increasing.")
    u = (np.asarray(x, dtype=float) - x0) / width
    h00 = 2.0 * u**3 - 3.0 * u**2 + 1.0
    h10 = u**3 - 2.0 * u**2 + u
    h01 = -2.0 * u**3 + 3.0 * u**2
    h11 = u**3 - u**2
    return h00 * y0 + h10 * width * dy0 + h01 * y1 + h11 * width * dy1


def hybrid_total_variance(
    x: Iterable[float] | float,
    core: TTFPchipCore,
    params: Mapping[str, float],
    *,
    left_blend_width: float,
    right_blend_width: float,
) -> np.ndarray:
    """Evaluate the complete PCHIP-core / Hermite-blend / Wing-tail smile."""
    x_values = np.atleast_1d(np.asarray(x, dtype=float))
    if not np.all(np.isfinite(x_values)):
        raise ValueError("TTF hybrid evaluation points must be finite.")
    left_width = float(left_blend_width)
    right_width = float(right_blend_width)
    if left_width <= 0 or right_width <= 0:
        raise ValueError("TTF hybrid blend widths must be strictly positive.")

    left_tail_end = core.xmin - left_width
    right_tail_end = core.xmax + right_width
    result = np.empty_like(x_values)

    core_mask = (x_values >= core.xmin) & (x_values <= core.xmax)
    left_tail_mask = x_values <= left_tail_end
    right_tail_mask = x_values >= right_tail_end
    left_blend_mask = ~(core_mask | left_tail_mask | right_tail_mask) & (
        x_values < core.xmin
    )
    right_blend_mask = ~(core_mask | left_tail_mask | right_tail_mask) & (
        x_values > core.xmax
    )

    if np.any(core_mask):
        result[core_mask] = core.total_variance(x_values[core_mask])
    if np.any(left_tail_mask):
        result[left_tail_mask] = _wing_total_variance(
            x_values[left_tail_mask], core, params
        )
    if np.any(right_tail_mask):
        result[right_tail_mask] = _wing_total_variance(
            x_values[right_tail_mask], core, params
        )

    if np.any(left_blend_mask):
        wing_value, wing_derivative = _wing_boundary_value_and_derivative(
            left_tail_end, core, params
        )
        core_value = float(core.total_variance(core.xmin))
        core_derivative = core.derivative(core.xmin)
        result[left_blend_mask] = _hermite_values(
            x_values[left_blend_mask],
            left_tail_end,
            core.xmin,
            wing_value,
            core_value,
            wing_derivative,
            core_derivative,
        )
    if np.any(right_blend_mask):
        wing_value, wing_derivative = _wing_boundary_value_and_derivative(
            right_tail_end, core, params
        )
        core_value = float(core.total_variance(core.xmax))
        core_derivative = core.derivative(core.xmax)
        result[right_blend_mask] = _hermite_values(
            x_values[right_blend_mask],
            core.xmax,
            right_tail_end,
            core_value,
            wing_value,
            core_derivative,
            wing_derivative,
        )
    return result


def hybrid_iv(
    strike: Iterable[float] | float,
    core: TTFPchipCore,
    params: Mapping[str, float],
    *,
    left_blend_width: float,
    right_blend_width: float,
) -> np.ndarray:
    strikes = np.atleast_1d(np.asarray(strike, dtype=float))
    if not np.all(np.isfinite(strikes)) or np.any(strikes <= 0):
        raise ValueError("TTF hybrid strikes must be finite and strictly positive.")
    x = np.log(strikes / core.forward)
    total_variance = hybrid_total_variance(
        x,
        core,
        params,
        left_blend_width=left_blend_width,
        right_blend_width=right_blend_width,
    )
    with np.errstate(invalid="ignore"):
        return np.sqrt(total_variance / core.time_to_expiry)


def _black_call_prices_from_total_variance(
    x: np.ndarray,
    total_variance: np.ndarray,
    forward: float,
) -> tuple[np.ndarray, np.ndarray]:
    sqrt_w = np.sqrt(total_variance)
    d1 = -x / sqrt_w + 0.5 * sqrt_w
    d2 = d1 - sqrt_w
    strikes = forward * np.exp(x)
    calls = forward * norm.cdf(d1) - strikes * norm.cdf(d2)
    return strikes, calls


def validate_ttf_hybrid(
    core: TTFPchipCore,
    params: Mapping[str, float],
    *,
    left_blend_width: float,
    right_blend_width: float,
    n_points: int = TTF_VALIDATION_POINT_COUNT,
    butterfly_margin: float = TTF_BUTTERFLY_MARGIN,
) -> dict[str, Any]:
    """Fail-closed validation of the complete operational smile."""
    x_min, x_max = _hybrid_validation_range(core)
    x = np.linspace(x_min, x_max, int(n_points))
    try:
        total_variance = hybrid_total_variance(
            x,
            core,
            params,
            left_blend_width=left_blend_width,
            right_blend_width=right_blend_width,
        )
    except Exception as exc:
        return {
            "is_valid": False,
            "error": str(exc),
            "min_g": None,
            "n_points": int(n_points),
            "moneyness_range": (float(x_min), float(x_max)),
        }

    variance_valid = bool(
        np.all(np.isfinite(total_variance)) and np.all(total_variance > 0)
    )
    slopes = np.asarray(
        [params.get("put_wing_power"), params.get("call_wing_power")],
        dtype=float,
    )
    lee_valid = bool(
        np.all(np.isfinite(slopes)) and np.all(slopes >= 0) and np.all(slopes < 2)
    )
    if variance_valid:
        g_values = compute_g_function(x, total_variance)
        g_finite = bool(np.all(np.isfinite(g_values)))
        min_g = float(np.min(g_values)) if g_finite else None
        butterfly_valid = bool(
            g_finite and min_g >= float(butterfly_margin) - 1e-8
        )
        strikes, calls = _black_call_prices_from_total_variance(
            x, total_variance, core.forward
        )
        call_slopes = np.diff(calls) / np.diff(strikes)
        decreasing_calls = bool(np.all(np.diff(calls) <= 1e-10 * core.forward))
        convex_calls = bool(
            np.all(np.diff(call_slopes) >= -1e-8 / max(core.forward, 1.0))
        )
    else:
        min_g = None
        butterfly_valid = False
        decreasing_calls = False
        convex_calls = False

    is_valid = bool(
        variance_valid
        and lee_valid
        and butterfly_valid
        and decreasing_calls
        and convex_calls
    )
    return {
        "is_valid": is_valid,
        "positive_variance": variance_valid,
        "lee_compatible": lee_valid,
        "butterfly_valid": butterfly_valid,
        "decreasing_calls": decreasing_calls,
        "convex_calls": convex_calls,
        "min_g": min_g,
        "butterfly_margin": float(butterfly_margin),
        "n_points": int(n_points),
        "moneyness_range": (float(x_min), float(x_max)),
        "left_join_x": float(core.xmin),
        "right_join_x": float(core.xmax),
        "left_blend_width": float(left_blend_width),
        "right_blend_width": float(right_blend_width),
    }


def select_valid_blend(
    core: TTFPchipCore,
    params: Mapping[str, float],
    *,
    blend_widths: Sequence[float] = TTF_BLEND_WIDTHS,
) -> tuple[float, float, dict[str, Any]]:
    """Return the first deterministic symmetric blend that passes all gates."""
    diagnostics = []
    for width in blend_widths:
        validation = validate_ttf_hybrid(
            core,
            params,
            left_blend_width=float(width),
            right_blend_width=float(width),
        )
        diagnostics.append(validation)
        if validation["is_valid"]:
            validation["blend_attempts"] = diagnostics
            return float(width), float(width), validation
    min_g_values = [
        item.get("min_g")
        for item in diagnostics
        if item.get("min_g") is not None
    ]
    worst = min(min_g_values) if min_g_values else None
    raise ValueError(
        "No governed TTF PCHIP/Wing blend passed the complete arbitrage gate"
        + (f" (lowest min g={worst:.6f})." if worst is not None else ".")
    )


def _effective_bounds() -> dict[str, tuple[float, float]]:
    bounds = dict(BOUNDS)
    bounds.update(TTF_WING_V2_PARAMETER_BOUNDS)
    return bounds


def _full_initial_params(
    initial_params: Mapping[str, float],
    *,
    commodity: str = "TTF",
) -> dict[str, float]:
    product = str(commodity).strip().upper()
    hybrid_policy(product)
    params = get_defaults(product)
    params.update({key: value for key, value in initial_params.items() if key in PARAM_ORDER})
    bounds = _effective_bounds()
    for name in PARAM_ORDER:
        value = float(params[name])
        lower, upper = bounds[name]
        if not np.isfinite(value) or not lower <= value <= upper:
            raise ValueError(
                f"Initial {product} Wing parameter {name}={value!r} is outside "
                f"[{lower}, {upper}]."
            )
        params[name] = value
    return params


def _params_from_vector(
    values: np.ndarray,
    initial_params: Mapping[str, float],
) -> dict[str, float]:
    params = dict(initial_params)
    params.update(
        {
            name: float(value)
            for name, value in zip(TTF_WING_V2_FREE_PARAMS, values)
        }
    )
    return params


def _wing_gate_values(
    values: np.ndarray,
    initial_params: Mapping[str, float],
    core: TTFPchipCore,
    x_grid: np.ndarray,
) -> np.ndarray:
    params = _params_from_vector(values, initial_params)
    try:
        total_variance = _wing_total_variance(x_grid, core, params)
        g = compute_g_function(x_grid, total_variance) - TTF_BUTTERFLY_MARGIN
    except Exception:
        return np.full(len(x_grid), -1e12, dtype=float)
    return np.where(np.isfinite(g), g, -1e12)


def _hybrid_gate_values(
    values: np.ndarray,
    initial_params: Mapping[str, float],
    core: TTFPchipCore,
    x_grid: np.ndarray,
    blend_width: float,
) -> np.ndarray:
    """Return the complete hybrid g-margin constraint on the final grid."""
    params = _params_from_vector(values, initial_params)
    try:
        total_variance = hybrid_total_variance(
            x_grid,
            core,
            params,
            left_blend_width=blend_width,
            right_blend_width=blend_width,
        )
        g = compute_g_function(x_grid, total_variance) - TTF_BUTTERFLY_MARGIN
    except Exception:
        return np.full(len(x_grid), -1e12, dtype=float)
    return np.where(np.isfinite(g), g, -1e12)


def fit_ttf_hybrid_candidate(
    observations: pd.DataFrame,
    initial_params: Mapping[str, float],
    *,
    n_starts: int = 1,
    seed: int = 42,
    commodity: str = "TTF",
) -> dict[str, Any]:
    """Fit Wing-v2 to the PCHIP target in total-variance/log-K space."""
    product = str(commodity).strip().upper()
    method, policy_version = hybrid_policy(product)
    core = build_ttf_pchip_core(observations, commodity=product)
    params0 = _full_initial_params(initial_params, commodity=product)
    if product in {"BRENT", "HH"}:
        # These source-specific builders have already performed the joint
        # butterfly/calendar projection on the full 1%-99% delta range. No
        # ungoverned strike is added beyond those wide governed endpoints.
        validation = validate_ttf_hybrid(
            core,
            params0,
            left_blend_width=TTF_BLEND_WIDTHS[0],
            right_blend_width=TTF_BLEND_WIDTHS[0],
        )
        if not validation["is_valid"]:
            raise ValueError(
                f"{product} jointly projected PCHIP core failed the complete arbitrage gate."
            )
        return {
            "success": True,
            "params": params0,
            "core": core,
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.0,
            "iv_rmse": 0.0,
            "left_blend_width": float(TTF_BLEND_WIDTHS[0]),
            "right_blend_width": float(TTF_BLEND_WIDTHS[0]),
            "validation": validation,
            "solver_converged": True,
            "accepted_feasible_candidate": False,
            "start": 0,
            "rmse": 0.0,
            "butterfly": {
                "is_valid": True,
                "min_g": validation.get("min_g"),
                "margin": TTF_BUTTERFLY_MARGIN,
            },
            "starts": 1,
            "attempts": [
                {
                    "start": 0,
                    "solver_success": True,
                    "accepted_feasible": False,
                    "objective": 0.0,
                    "iterations": 0,
                    "message": (
                        f"jointly projected {product} core; external tail disabled"
                    ),
                }
            ],
            "calibration_method": method,
            "calibration_policy_version": policy_version,
        }
    bounds_by_name = _effective_bounds()
    free_names = tuple(TTF_WING_V2_FREE_PARAMS)
    bounds = [bounds_by_name[name] for name in free_names]
    x0 = np.asarray([params0[name] for name in free_names], dtype=float)
    x_core = np.linspace(core.xmin, core.xmax, TTF_CORE_SAMPLE_COUNT)
    target_total_variance = core.total_variance(x_core)
    validation_min, validation_max = _hybrid_validation_range(core)
    x_gate = np.linspace(
        validation_min,
        validation_max,
        int(TTF_WING_V2_OPTIMIZER_OPTIONS.get("butterfly_n_points", 1001)),
    )

    def objective(values: np.ndarray) -> float:
        params = _params_from_vector(values, params0)
        try:
            fitted_total_variance = _wing_total_variance(x_core, core, params)
        except Exception:
            # SLSQP's finite-difference line search does not tolerate an
            # infinite objective when a trial vector temporarily makes an
            # internal Wing boundary non-positive.
            return 1e6
        residuals = fitted_total_variance - target_total_variance
        value = np.mean(residuals**2)
        return float(value) if np.isfinite(value) else float("inf")

    count = int(n_starts)
    if count < 1:
        raise ValueError(f"{product} hybrid n_starts must be at least one.")
    rng = np.random.default_rng(int(seed))
    starts = [x0]
    starts.extend(
        np.asarray([rng.uniform(lower, upper) for lower, upper in bounds])
        for _ in range(1, count)
    )
    solver_options = {
        "maxiter": int(TTF_WING_V2_OPTIMIZER_OPTIONS.get("maxiter", 2000)),
        "ftol": float(TTF_WING_V2_OPTIMIZER_OPTIONS.get("ftol", 1e-10)),
    }
    wing_constraints = (
        {
            "type": "ineq",
            "fun": lambda values: _wing_gate_values(values, params0, core, x_gate),
        },
    )

    attempts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for start_index, start in enumerate(starts):
        try:
            solver_result = minimize(
                objective,
                start,
                method="SLSQP",
                bounds=bounds,
                constraints=wing_constraints,
                options=solver_options,
            )
            finite = bool(
                np.isfinite(solver_result.fun)
                and np.all(np.isfinite(solver_result.x))
            )
            attempt = {
                "start": start_index,
                "solver_success": bool(solver_result.success),
                "accepted_feasible": finite and not bool(solver_result.success),
                "objective": float(solver_result.fun) if finite else None,
                "iterations": int(getattr(solver_result, "nit", -1)),
                "message": str(getattr(solver_result, "message", "")),
            }
            attempts.append(attempt)
            if not finite:
                continue
            # The Wing-only fit is the base approximation.  The operational
            # constraint is the complete hybrid, so retry the governed blend
            # widths from the same deterministic base result.  This adjustment
            # still minimizes the same core total-variance objective; it merely
            # selects a feasible tail geometry for the join.
            hybrid_result = None
            hybrid_validation = None
            selected_width = None
            x_validation = np.linspace(
                validation_min,
                validation_max,
                TTF_VALIDATION_POINT_COUNT,
            )
            for blend_width in TTF_BLEND_WIDTHS:
                # Every width is retried from the same fitted Wing base so a
                # failed narrow transition cannot contaminate the next start.
                hybrid_start = np.asarray(solver_result.x, dtype=float)
                base_params = _params_from_vector(hybrid_start, params0)
                base_validation = validate_ttf_hybrid(
                    core,
                    base_params,
                    left_blend_width=blend_width,
                    right_blend_width=blend_width,
                )
                if base_validation["is_valid"]:
                    hybrid_result = solver_result
                    hybrid_validation = base_validation
                    selected_width = float(blend_width)
                    break
                constrained_result = minimize(
                    objective,
                    hybrid_start,
                    method="SLSQP",
                    bounds=bounds,
                    constraints=(
                        {
                            "type": "ineq",
                            "fun": lambda values, width=blend_width: (
                                _hybrid_gate_values(
                                    values,
                                    params0,
                                    core,
                                    x_validation,
                                    float(width),
                                )
                            ),
                        },
                    ),
                    options=solver_options,
                )
                hybrid_finite = bool(
                    np.isfinite(constrained_result.fun)
                    and np.all(np.isfinite(constrained_result.x))
                )
                if hybrid_finite:
                    hybrid_start = np.asarray(constrained_result.x, dtype=float)
                    constrained_params = _params_from_vector(hybrid_start, params0)
                    constrained_validation = validate_ttf_hybrid(
                        core,
                        constrained_params,
                        left_blend_width=blend_width,
                        right_blend_width=blend_width,
                    )
                    if constrained_validation["is_valid"]:
                        hybrid_result = constrained_result
                        hybrid_validation = constrained_validation
                        selected_width = float(blend_width)
                        break
            if hybrid_result is None or hybrid_validation is None:
                continue
            params = _params_from_vector(hybrid_result.x, params0)
            left_width = selected_width
            right_width = selected_width
            validation = hybrid_validation
            fitted_total_variance = _wing_total_variance(x_core, core, params)
            tv_residuals = fitted_total_variance - target_total_variance
            wing_iv = np.sqrt(fitted_total_variance / core.time_to_expiry)
            core_iv = np.sqrt(target_total_variance / core.time_to_expiry)
            candidates.append(
                {
                    "params": params,
                    "core": core,
                    "core_tv_rmse": 0.0,
                    "tail_fit_tv_rmse": float(np.sqrt(np.mean(tv_residuals**2))),
                    "iv_rmse": float(np.sqrt(np.mean((wing_iv - core_iv) ** 2))),
                    "left_blend_width": left_width,
                    "right_blend_width": right_width,
                    "validation": validation,
                    "solver_converged": bool(hybrid_result.success),
                    "accepted_feasible_candidate": not bool(hybrid_result.success),
                    "start": start_index,
                }
            )
        except Exception as exc:
            attempts.append(
                {
                    "start": start_index,
                    "solver_success": False,
                    "accepted_feasible": False,
                    "objective": None,
                    "iterations": -1,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )

    if not candidates:
        raise ValueError(
            f"{product} tail fit produced no complete hybrid that passed validation."
        )
    best = min(candidates, key=lambda item: item["tail_fit_tv_rmse"])
    best.update(
        {
            "success": True,
            "rmse": best["tail_fit_tv_rmse"],
            "butterfly": {
                "is_valid": bool(best["validation"]["is_valid"]),
                "min_g": best["validation"].get("min_g"),
                "margin": TTF_BUTTERFLY_MARGIN,
            },
            "starts": len(starts),
            "attempts": attempts,
            "calibration_method": method,
            "calibration_policy_version": policy_version,
        }
    )
    return best


def evaluate_ttf_hybrid_candidate(
    observations: pd.DataFrame,
    params: Mapping[str, float],
    *,
    left_blend_width: float | None = None,
    right_blend_width: float | None = None,
    commodity: str = "TTF",
) -> dict[str, Any]:
    """Evaluate an existing tail parameter row against its authoritative core."""
    product = str(commodity).strip().upper()
    method, policy_version = hybrid_policy(product)
    core = build_ttf_pchip_core(observations, commodity=product)
    full_params = _full_initial_params(params, commodity=product)
    if left_blend_width is None or right_blend_width is None:
        left_width, right_width, validation = select_valid_blend(core, full_params)
    else:
        left_width = float(left_blend_width)
        right_width = float(right_blend_width)
        validation = validate_ttf_hybrid(
            core,
            full_params,
            left_blend_width=left_width,
            right_blend_width=right_width,
        )
        if not validation["is_valid"]:
            raise ValueError(
                f"The edited {product} hybrid failed the complete arbitrage gate."
            )
    x_core = np.linspace(core.xmin, core.xmax, TTF_CORE_SAMPLE_COUNT)
    target_w = core.total_variance(x_core)
    wing_w = _wing_total_variance(x_core, core, full_params)
    return {
        "success": True,
        "params": full_params,
        "core": core,
        "core_tv_rmse": 0.0,
        "tail_fit_tv_rmse": float(np.sqrt(np.mean((wing_w - target_w) ** 2))),
        "iv_rmse": float(
            np.sqrt(
                np.mean(
                    (
                        np.sqrt(wing_w / core.time_to_expiry)
                        - np.sqrt(target_w / core.time_to_expiry)
                    )
                    ** 2
                )
            )
        ),
        "left_blend_width": left_width,
        "right_blend_width": right_width,
        "validation": validation,
        "butterfly": {
            "is_valid": bool(validation["is_valid"]),
            "min_g": validation.get("min_g"),
            "margin": TTF_BUTTERFLY_MARGIN,
        },
        "calibration_method": method,
        "calibration_policy_version": policy_version,
    }


def fit_gas_hybrid_candidate(
    observations: pd.DataFrame,
    initial_params: Mapping[str, float],
    *,
    commodity: str,
    n_starts: int = 1,
    seed: int = 42,
) -> dict[str, Any]:
    """Product-aware facade for the shared TTF/JKM/NBP hybrid fit."""
    gas_hybrid_policy(commodity)
    return fit_hybrid_candidate(
        observations,
        initial_params,
        commodity=commodity,
        n_starts=n_starts,
        seed=seed,
    )


def evaluate_gas_hybrid_candidate(
    observations: pd.DataFrame,
    params: Mapping[str, float],
    *,
    commodity: str,
    left_blend_width: float | None = None,
    right_blend_width: float | None = None,
) -> dict[str, Any]:
    """Product-aware facade for an existing TTF/JKM/NBP hybrid row."""
    gas_hybrid_policy(commodity)
    return evaluate_hybrid_candidate(
        observations,
        params,
        commodity=commodity,
        left_blend_width=left_blend_width,
        right_blend_width=right_blend_width,
    )


def fit_hybrid_candidate(
    observations: pd.DataFrame,
    initial_params: Mapping[str, float],
    *,
    commodity: str,
    n_starts: int = 1,
    seed: int = 42,
) -> dict[str, Any]:
    """Fit the common finalizer while retaining product-specific provenance."""
    return fit_ttf_hybrid_candidate(
        observations,
        initial_params,
        n_starts=n_starts,
        seed=seed,
        commodity=commodity,
    )


def evaluate_hybrid_candidate(
    observations: pd.DataFrame,
    params: Mapping[str, float],
    *,
    commodity: str,
    left_blend_width: float | None = None,
    right_blend_width: float | None = None,
) -> dict[str, Any]:
    """Evaluate one common-finalizer row with product-specific provenance."""
    return evaluate_ttf_hybrid_candidate(
        observations,
        params,
        left_blend_width=left_blend_width,
        right_blend_width=right_blend_width,
        commodity=commodity,
    )


def _canonical_surface_grid(core: TTFPchipCore, n_points: int) -> np.ndarray:
    """Return a stable dense grid that contains every governed core node."""
    count = int(n_points)
    if count < len(core.x_nodes):
        raise ValueError(
            "Dense hybrid output cannot contain every governed anchor with fewer "
            f"than {len(core.x_nodes)} points."
        )
    x_min, x_max = _hybrid_validation_range(core)
    grid = np.linspace(x_min, x_max, count)
    reserved: set[int] = set()
    for node in core.x_nodes:
        for index in np.argsort(np.abs(grid - node)):
            candidate = int(index)
            if candidate not in reserved:
                grid[candidate] = float(node)
                reserved.add(candidate)
                break
    grid.sort()
    if len(grid) != count or np.any(np.diff(grid) <= 0):
        raise ValueError("Could not build a distinct canonical hybrid surface grid.")
    return grid


def operational_surface_frame(
    observations: pd.DataFrame,
    params: Mapping[str, float],
    *,
    left_blend_width: float,
    right_blend_width: float,
    n_points: int = CANONICAL_SURFACE_POINT_COUNT,
    commodity: str = "TTF",
) -> pd.DataFrame:
    """Create an export/chart-ready sample of one complete hybrid smile."""
    product = str(commodity).strip().upper()
    hybrid_policy(product)
    core = build_ttf_pchip_core(observations, commodity=product)
    x = _canonical_surface_grid(core, n_points)
    strikes = core.forward * np.exp(x)
    total_variance = hybrid_total_variance(
        x,
        core,
        params,
        left_blend_width=left_blend_width,
        right_blend_width=right_blend_width,
    )
    iv = np.sqrt(total_variance / core.time_to_expiry)
    sqrt_w = np.sqrt(total_variance)
    call_delta = norm.cdf(-x / sqrt_w + 0.5 * sqrt_w)
    for node_index, node in enumerate(core.x_nodes):
        dense_index = int(np.flatnonzero(x == node)[0])
        total_variance[dense_index] = core.total_variance_nodes[node_index]
        iv[dense_index] = core.iv_nodes[node_index]
        call_delta[dense_index] = core.delta_nodes[node_index]
    left_tail_end = core.xmin - left_blend_width
    right_tail_end = core.xmax + right_blend_width
    core_or_tail = np.where(
        (x >= core.xmin) & (x <= core.xmax),
        "core",
        "tail",
    )
    blend_classification = np.select(
        [
            x < left_tail_end,
            (x >= left_tail_end) & (x < core.xmin),
            (x >= core.xmin) & (x <= core.xmax),
            (x > core.xmax) & (x <= right_tail_end),
            x > right_tail_end,
        ],
        ["wing_left", "left_blend", "pchip_core", "right_blend", "wing_right"],
        default="invalid",
    )
    expiry = observations["expiry"].iloc[0]
    return pd.DataFrame(
        {
            "expiry": expiry,
            "calibration_basis": core.calibration_basis,
            "delta": call_delta,
            "strike": strikes,
            "log_moneyness": x,
            "iv": iv,
            "total_variance": total_variance,
            "core_tail_classification": core_or_tail,
            "blend_classification": blend_classification,
            "source_name": core.source_name,
        }
    )


def densify_bounded_source_surface(
    source_surface: pd.DataFrame,
    *,
    commodity: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Convert validated BRENT/HH 11-node term structures to 401-point slices."""

    product = str(commodity).strip().upper()
    if product not in {"BRENT", "HH"}:
        raise ValueError("Bounded source densification is supported for BRENT and HH.")
    required = {
        "contract_date",
        "option_expiration_date",
        "delta",
        "strike",
        "volatility",
        "total_variance",
        "working_forward",
        "calibration_basis",
        "source_name",
    }
    missing = sorted(required - set(source_surface.columns))
    if missing:
        raise ValueError(
            f"{product} bounded source surface is missing: " + ", ".join(missing)
        )

    dense_slices: list[pd.DataFrame] = []
    expiry_results: list[dict[str, Any]] = []
    for contract_date, group in source_surface.groupby("contract_date", sort=True):
        group = group.sort_values("delta").copy()
        volatility = group["volatility"].to_numpy(dtype=float)
        total_variance = group["total_variance"].to_numpy(dtype=float)
        time_to_expiry = total_variance / volatility**2
        observations = pd.DataFrame(
            {
                "expiry": pd.Timestamp(contract_date),
                "option_expiration_date": pd.to_datetime(
                    group["option_expiration_date"], errors="coerce"
                ),
                "forward": group["working_forward"].to_numpy(dtype=float),
                "dte": time_to_expiry * DAYS_PER_YEAR,
                "delta": group["delta"].to_numpy(dtype=float),
                "iv": volatility,
                "strike": group["strike"].to_numpy(dtype=float),
                "source_name": group["source_name"].astype(str).to_numpy(),
                "calibration_basis": group["calibration_basis"].astype(str).to_numpy(),
                "weight": np.ones(len(group), dtype=float),
            }
        )
        result = fit_hybrid_candidate(
            observations,
            get_defaults(product),
            commodity=product,
            n_starts=1,
        )
        dense = operational_surface_frame(
            observations,
            result["params"],
            left_blend_width=result["left_blend_width"],
            right_blend_width=result["right_blend_width"],
            commodity=product,
        )
        dense["contract_date"] = pd.Timestamp(contract_date).normalize()
        dense["option_expiration_date"] = pd.to_datetime(
            group["option_expiration_date"].iloc[0]
        ).normalize()
        dense["working_forward"] = float(group["working_forward"].iloc[0])
        dense_slices.append(dense)
        expiry_results.append(
            {
                "option_expiration_date": dense["option_expiration_date"].iloc[0].date(),
                "parameters": result["params"],
                "diagnostics": {
                    "point_count": len(dense),
                    "calibration_basis": str(group["calibration_basis"].iloc[0]),
                    "min_g": result["validation"].get("min_g"),
                    "bounded_source_range": True,
                },
                "validation": result["validation"],
                "weighted_rmse": 0.0,
                "unweighted_rmse": 0.0,
                "max_error": None,
                "optimizer_success": True,
            }
        )
    if not dense_slices:
        raise ValueError(f"{product} bounded source surface is empty.")
    return (
        pd.concat(dense_slices, ignore_index=True).sort_values(
            ["contract_date", "strike"]
        ).reset_index(drop=True),
        expiry_results,
    )
