"""JKM configuration for the shared PCHIP-core / Wing-v2-tail surface."""

from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from options.calibration_engine.config.defaults import get_defaults

from vol_calibration.ttf_hybrid_surface import (
    build_ttf_pchip_core as build_jkm_pchip_core,  # noqa: F401 - public alias
    evaluate_ttf_hybrid_candidate,
    fit_ttf_hybrid_candidate,
    hybrid_iv,  # noqa: F401 - public facade export
    operational_surface_frame,  # noqa: F401 - public facade export
    validate_ttf_hybrid as validate_jkm_hybrid,  # noqa: F401 - public alias
)


JKM_HYBRID_METHOD = "PCHIP-core/Wing-v2-tail hybrid"
JKM_HYBRID_POLICY_VERSION = "jkm_pchip_core_wing_tail_hybrid_v1"


def _jkm_initial_params(values: Mapping[str, float]) -> dict[str, float]:
    params = get_defaults("JKM")
    params.update(dict(values))
    return params


def _retag(result: Mapping[str, Any]) -> dict[str, Any]:
    tagged = dict(result)
    tagged["calibration_method"] = JKM_HYBRID_METHOD
    tagged["calibration_policy_version"] = JKM_HYBRID_POLICY_VERSION
    return tagged


def fit_jkm_hybrid_candidate(
    observations: pd.DataFrame,
    initial_params: Mapping[str, float],
    *,
    n_starts: int = 3,
    seed: int = 42,
) -> dict[str, Any]:
    """Fit the JKM Wing tails to the authoritative total-variance PCHIP core."""
    return _retag(
        fit_ttf_hybrid_candidate(
            observations,
            _jkm_initial_params(initial_params),
            n_starts=n_starts,
            seed=seed,
            commodity="JKM",
        )
    )


def evaluate_jkm_hybrid_candidate(
    observations: pd.DataFrame,
    params: Mapping[str, float],
    *,
    left_blend_width: float | None = None,
    right_blend_width: float | None = None,
) -> dict[str, Any]:
    """Evaluate one existing JKM tail row against its exact PCHIP core."""
    return _retag(
        evaluate_ttf_hybrid_candidate(
            observations,
            _jkm_initial_params(params),
            left_blend_width=left_blend_width,
            right_blend_width=right_blend_width,
            commodity="JKM",
        )
    )
