"""Dashboard execution adapter for the canonical TTF batch engine."""

from options.vol_calibration.calibration import ttf_batch as _implementation
from dash_options.calibration.adapters.batch import execute_batch
from dash_options.calibration.execution.fit_pool import execute_hybrid_starts


def __getattr__(name):
    return getattr(_implementation, name)


def _run_ttf_candidate(*args, **kwargs):
    kwargs.setdefault("start_executor", execute_hybrid_starts)
    return _implementation._run_ttf_candidate(*args, **kwargs)


def calibrate_ttf_batch(market_data, table_data, **kwargs):
    return execute_batch("TTF", market_data, table_data, **kwargs)
