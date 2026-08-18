"""Trader-facing adjustments for the TTF PCHIP smile core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from vol_calibration.ttf_hybrid_surface import build_ttf_pchip_core


VOL_POINT_SCALE = 0.01
MAX_ABSOLUTE_CONTROL_VOL_POINTS = 50.0


@dataclass(frozen=True)
class TTFAdjustmentRecipe:
    level: float = 0.0
    skew: float = 0.0
    put_curvature: float = 0.0
    call_curvature: float = 0.0

    @classmethod
    def from_mapping(cls, values: Mapping | None):
        values = values or {}
        parsed = {}
        for name in ("level", "skew", "put_curvature", "call_curvature"):
            value = pd.to_numeric(pd.Series([values.get(name, 0.0)]), errors="coerce").iloc[0]
            if not np.isfinite(value):
                raise ValueError(f"TTF {name.replace('_', ' ')} must be finite.")
            if abs(float(value)) > MAX_ABSOLUTE_CONTROL_VOL_POINTS:
                raise ValueError(
                    f"TTF {name.replace('_', ' ')} is limited to "
                    f"{MAX_ABSOLUTE_CONTROL_VOL_POINTS:.0f} vol points."
                )
            parsed[name] = float(value)
        return cls(**parsed)


def _shape_changes(deltas: np.ndarray, recipe: TTFAdjustmentRecipe) -> np.ndarray:
    """Return decimal-IV changes on the governed call-delta grid.

    Positive skew raises the put wing (high call delta) and lowers the call
    wing (low call delta). Curvature controls are zero at 50D and localized to
    their named side, making the four controls identifiable to a trader.
    """
    centered = np.asarray(deltas, dtype=float) - 0.5
    put_coordinate = np.clip(centered / 0.5, 0.0, 1.0)
    call_coordinate = np.clip(-centered / 0.5, 0.0, 1.0)
    changes_vol_points = (
        recipe.level
        + recipe.skew * (2.0 * centered)
        + recipe.put_curvature * put_coordinate**2
        + recipe.call_curvature * call_coordinate**2
    )
    return VOL_POINT_SCALE * changes_vol_points


def _local_trade_shape(x_nodes: np.ndarray, target_x: float) -> np.ndarray:
    distances = np.diff(np.sort(x_nodes))
    positive = distances[distances > 1e-10]
    if positive.size == 0:
        raise ValueError("TTF trade targeting requires distinct smile strikes.")
    bandwidth = max(float(np.median(positive)) * 1.5, 0.035)
    normalized = np.abs((x_nodes - target_x) / bandwidth)
    # Compact C2 bump: exactly zero outside two bandwidths.
    shape = np.where(normalized < 2.0, (1.0 - (normalized / 2.0) ** 2) ** 3, 0.0)
    if float(np.max(shape)) <= 1e-10:
        raise ValueError("Selected trade is too far from the governed TTF core.")
    return shape


def _core_iv_at_strike(observations: pd.DataFrame, strike: float) -> float:
    core = build_ttf_pchip_core(observations)
    x = float(np.log(float(strike) / core.forward))
    if x < core.xmin - 1e-12 or x > core.xmax + 1e-12:
        raise ValueError(
            "Selected-trade targeting is available only inside the official "
            "1D-99D strike range; use expert tails outside it."
        )
    total_variance = float(core.total_variance(x))
    return float(np.sqrt(total_variance / core.time_to_expiry))


def apply_ttf_smile_adjustments(
    observations: pd.DataFrame,
    controls: Mapping | TTFAdjustmentRecipe | None,
    *,
    selected_trade: Mapping | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Apply level/skew/curvature and an optional exact-strike trade target."""
    recipe = (
        controls
        if isinstance(controls, TTFAdjustmentRecipe)
        else TTFAdjustmentRecipe.from_mapping(controls)
    )
    original_core = build_ttf_pchip_core(observations)
    adjusted = observations.copy()
    original_iv = pd.to_numeric(adjusted["iv"], errors="coerce").to_numpy(dtype=float)
    deltas = pd.to_numeric(adjusted["delta"], errors="coerce").to_numpy(dtype=float)
    candidate_iv = original_iv + _shape_changes(deltas, recipe)
    if not np.all(np.isfinite(candidate_iv)) or np.any(candidate_iv <= 0):
        raise ValueError("The TTF adjustment produced a non-positive node volatility.")
    adjusted["iv"] = candidate_iv

    trade_diagnostics = None
    if selected_trade:
        strike = pd.to_numeric(
            pd.Series([selected_trade.get("strike")]), errors="coerce"
        ).iloc[0]
        target_iv = pd.to_numeric(
            pd.Series([
                selected_trade.get("mark_iv", selected_trade.get("volatility"))
            ]),
            errors="coerce",
        ).iloc[0]
        if not np.isfinite(strike) or float(strike) <= 0:
            raise ValueError("Selected trade requires a finite positive strike.")
        if not np.isfinite(target_iv) or float(target_iv) <= 0:
            raise ValueError("Selected trade requires a finite positive implied volatility.")
        target_iv = float(target_iv)
        if target_iv > 5.0:
            target_iv /= 100.0

        x_nodes = np.log(
            pd.to_numeric(adjusted["strike"], errors="coerce").to_numpy(dtype=float)
            / original_core.forward
        )
        target_x = float(np.log(float(strike) / original_core.forward))
        if target_x < original_core.xmin - 1e-12 or target_x > original_core.xmax + 1e-12:
            raise ValueError(
                "Selected-trade targeting is available only inside the official "
                "1D-99D strike range; use expert tails outside it."
            )
        bump = _local_trade_shape(x_nodes, target_x)
        base_iv = pd.to_numeric(adjusted["iv"], errors="coerce").to_numpy(dtype=float)

        def objective(amplitude: float) -> float:
            trial_iv = base_iv + float(amplitude) * bump
            if np.any(trial_iv <= 0):
                return -10.0
            trial = adjusted.copy()
            trial["iv"] = trial_iv
            return _core_iv_at_strike(trial, float(strike)) - target_iv

        lower = max(-1.5, -float(np.min(base_iv[bump > 0])) + 1e-6)
        upper = 1.5
        lower_value = objective(lower)
        upper_value = objective(upper)
        if lower_value == 0:
            amplitude = lower
        elif upper_value == 0:
            amplitude = upper
        elif lower_value * upper_value > 0:
            raise ValueError(
                "The selected trade cannot be matched with a bounded local smile move."
            )
        else:
            amplitude = float(brentq(objective, lower, upper, xtol=1e-12, rtol=1e-12))
        adjusted["iv"] = base_iv + amplitude * bump
        matched_iv = _core_iv_at_strike(adjusted, float(strike))
        trade_diagnostics = {
            "trade_id": selected_trade.get("trade_id"),
            "strike": float(strike),
            "target_iv": target_iv,
            "matched_iv": matched_iv,
            "local_amplitude_vol_points": amplitude / VOL_POINT_SCALE,
        }

    build_ttf_pchip_core(adjusted)
    diagnostics = {
        "recipe": {
            "level": recipe.level,
            "skew": recipe.skew,
            "put_curvature": recipe.put_curvature,
            "call_curvature": recipe.call_curvature,
            "unit": "volatility percentage points",
        },
        "selected_trade": trade_diagnostics,
        "max_abs_node_change_vol_points": float(
            np.max(np.abs(adjusted["iv"].to_numpy(dtype=float) - original_iv))
            / VOL_POINT_SCALE
        ),
    }
    return adjusted, diagnostics
