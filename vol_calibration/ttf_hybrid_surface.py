"""Compatibility adapter for options.vol_calibration.models.gas_hybrid."""

from options.vol_calibration.models.gas_hybrid import *  # noqa: F403
from options.vol_calibration.models import gas_hybrid as _implementation

from vol_calibration.observed_fit_pool import execute_hybrid_starts


def __getattr__(name):
    return getattr(_implementation, name)


def fit_ttf_hybrid_candidate(*args, **kwargs):
    kwargs.setdefault("start_executor", execute_hybrid_starts)
    return _implementation.fit_ttf_hybrid_candidate(*args, **kwargs)


def fit_gas_hybrid_candidate(*args, **kwargs):
    kwargs.setdefault("start_executor", execute_hybrid_starts)
    return _implementation.fit_gas_hybrid_candidate(*args, **kwargs)


def fit_hybrid_candidate(*args, **kwargs):
    kwargs.setdefault("start_executor", execute_hybrid_starts)
    return _implementation.fit_hybrid_candidate(*args, **kwargs)
