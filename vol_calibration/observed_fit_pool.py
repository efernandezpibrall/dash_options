"""Bounded parallel execution for independent observed gas expiries."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import logging
from multiprocessing import current_process
import os


_LOGGER = logging.getLogger(__name__)
_BLAS_LIMIT = None


def _limit_worker_blas_threads():
    """Keep numerical libraries from oversubscribing each fit process."""
    global _BLAS_LIMIT
    from threadpoolctl import threadpool_limits

    _BLAS_LIMIT = threadpool_limits(limits=1)


def execute_hybrid_starts(tasks, fit_task):
    """Execute engine-supplied starts, or select its existing serial path."""
    if current_process().name != "MainProcess":
        return None
    workers = int(os.getenv("GAS_START_WORKERS", "4"))
    if not 1 <= workers <= 4:
        raise ValueError("GAS_START_WORKERS must be between 1 and 4")
    if workers == 1:
        return None
    try:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(tasks)),
            initializer=_limit_worker_blas_threads,
        ) as executor:
            return list(executor.map(fit_task, tasks))
    except Exception as exc:
        _LOGGER.warning("Hybrid-start process pool unavailable; fitting serially: %s", exc)
        return None


def prefit_observed_expiries(tasks, fit_task, *, environment_variable):
    """Return ordered per-expiry results, or defer to the caller's serial path.

    Each task already contains its exact selected observations and initial
    parameters. Workers perform no database writes or publication actions.
    A process-pool infrastructure failure leaves the original serial path
    available; individual fitting failures are returned by ``fit_task``.
    """
    workers = int(os.getenv(environment_variable, "4"))
    if not 1 <= workers <= 4:
        raise ValueError(f"{environment_variable} must be between 1 and 4")
    if workers == 1 or len(tasks) < 8:
        return {}
    try:
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_limit_worker_blas_threads
        ) as executor:
            return dict(executor.map(fit_task, tasks))
    except Exception as exc:
        _LOGGER.warning("Observed-fit process pool unavailable; fitting serially: %s", exc)
        return {}
