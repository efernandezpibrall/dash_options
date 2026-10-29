"""Dashboard execution adapter for the canonical JKM batch engine."""

from options.vol_calibration.calibration import jkm_batch as _implementation
from dash_options.calibration.adapters.batch import execute_batch
from dash_options.calibration.execution.fit_pool import execute_hybrid_starts


def __getattr__(name):
    return getattr(_implementation, name)


def _run_jkm_candidate(*args, **kwargs):
    kwargs.setdefault("start_executor", execute_hybrid_starts)
    return _implementation._run_jkm_candidate(*args, **kwargs)


def calibrate_jkm_batch(market_data, table_data, **kwargs):
    return execute_batch("JKM", market_data, table_data, **kwargs)
