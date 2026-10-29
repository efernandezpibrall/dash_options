"""Bounded parallel execution for independent observed gas expiries."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, TimeoutError
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from multiprocessing import current_process, get_context
import os


_LOGGER = logging.getLogger(__name__)
_BLAS_LIMIT = None
_SESSION = ContextVar("observed_fit_session", default=None)


def worker_budget(environment_variable):
    """One process budget, bounded by visible CPUs and portable configuration."""
    cpu_count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    try:
        cpu_count = min(cpu_count, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    default = min(4, cpu_count)
    for pages in ("SC_AVPHYS_PAGES", "SC_PHYS_PAGES"):
        try:
            memory = os.sysconf(pages) * os.sysconf("SC_PAGE_SIZE")
            default = min(default, max(1, (memory - 512 * 1024**2) // (512 * 1024**2)))
            break
        except (AttributeError, OSError, ValueError):
            pass
    total = int(os.getenv("VOL_CALIBRATION_WORKERS", str(default)))
    selected = int(os.getenv(environment_variable, str(total)))
    if not 1 <= selected <= 4 or not 1 <= total <= 4:
        raise ValueError(f"{environment_variable} and VOL_CALIBRATION_WORKERS must be between 1 and 4")
    return min(selected, total, cpu_count)


@contextmanager
def observed_fit_session():
    """Reuse spawned workers across products; never retain a pool after the batch."""
    if _SESSION.get() is not None:
        yield
        return
    state = {}
    token = _SESSION.set(state)
    try:
        yield
    finally:
        _SESSION.reset(token)
        if state.get("executor") is not None:
            state["executor"].shutdown(wait=True, cancel_futures=True)


def _session_executor(workers):
    state = _SESSION.get()
    options = dict(max_workers=workers, initializer=_limit_worker_blas_threads,
                   mp_context=get_context("spawn"))
    if state is None:
        return ProcessPoolExecutor(**options), True
    if state.get("workers") != workers:
        if state.get("executor") is not None:
            state["executor"].shutdown(wait=True, cancel_futures=True)
        state["executor"] = ProcessPoolExecutor(**options)
        state["workers"] = workers
    return state["executor"], False


def _discard_executor(executor):
    executor.shutdown(wait=True, cancel_futures=True)
    state = _SESSION.get()
    if state is not None and state.get("executor") is executor:
        state.clear()


def _wait(future, cancellation_check):
    while True:
        if cancellation_check is not None:
            cancellation_check()
        try:
            return future.result(timeout=0.25)
        except TimeoutError:
            # A task's own TimeoutError is a result, not an observation timeout.
            if future.done():
                raise


def _map(tasks, fit_task, workers, cancellation_check=None):
    executor, owned = _session_executor(workers)
    futures = []
    try:
        futures = [executor.submit(fit_task, task) for task in tasks]
        return [_wait(future, cancellation_check) for future in futures]
    except Exception:
        _discard_executor(executor)
        owned = False
        raise
    finally:
        for future in futures:
            future.cancel()
        if owned:
            executor.shutdown(wait=True, cancel_futures=True)


class _ObservedResults:
    """Consume independent results in source order without a whole-batch barrier."""

    def __init__(self, tasks, fit_task, workers, cancellation_check):
        self.executor, self.owned = _session_executor(workers)
        self.cancellation_check = cancellation_check
        self.futures = {}
        try:
            for task in tasks:
                if task[0] in self.futures:
                    raise ValueError("Duplicate observed expiry task")
                self.futures[task[0]] = self.executor.submit(fit_task, task)
        except Exception:
            self.close()
            raise

    def get(self, key, default=None):
        future = self.futures.pop(key, None)
        if future is None:
            return default
        # Cancellation exceptions must propagate, never enter the serial
        # fallback intended only for unavailable worker infrastructure.
        cancelled = False
        def check():
            nonlocal cancelled
            try:
                if self.cancellation_check is not None:
                    self.cancellation_check()
            except BaseException:
                cancelled = True
                raise
        try:
            returned_key, result = _wait(future, check)
        except Exception as exc:
            future.cancel()
            self.close()
            if cancelled:
                raise
            _discard_executor(self.executor)
            self.owned = False
            _LOGGER.warning("Observed-fit process pool unavailable; fitting remaining expiries serially: %s", exc)
            return default
        if returned_key != key:
            raise ValueError("Observed fit returned a different expiry")
        return result

    def close(self):
        for future in self.futures.values():
            future.cancel()
        self.futures.clear()
        if self.owned:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.owned = False


def _limit_worker_blas_threads():
    """Keep numerical libraries from oversubscribing each fit process."""
    global _BLAS_LIMIT
    from threadpoolctl import threadpool_limits

    _BLAS_LIMIT = threadpool_limits(limits=1)


def execute_hybrid_starts(tasks, fit_task, *, cancellation_check=None):
    """Execute engine-supplied starts, or select its existing serial path."""
    if current_process().name != "MainProcess":
        return None
    workers = worker_budget("GAS_START_WORKERS")
    if workers == 1:
        return None
    cancelled = False
    def check():
        nonlocal cancelled
        try:
            if cancellation_check is not None:
                cancellation_check()
        except BaseException:
            cancelled = True
            raise
    try:
        # Keep the same pool budget when a month needs three starts and the
        # following recovery needs six; fewer tasks simply leave workers idle.
        return _map(tasks, fit_task, workers, check)
    except Exception as exc:
        if cancelled:
            raise
        _LOGGER.warning("Hybrid-start process pool unavailable; fitting serially: %s", exc)
        return None


def stream_observed_expiries(tasks, fit_task, *, environment_variable, cancellation_check=None):
    """Daily-only lazy results; the analytics lifecycle owns deterministic close."""
    workers = worker_budget(environment_variable)
    if current_process().name != "MainProcess" or workers == 1 or len(tasks) < 8:
        return {}
    if cancellation_check is not None:
        cancellation_check()
    try:
        return _ObservedResults(tasks, fit_task, workers, cancellation_check)
    except Exception as exc:
        _LOGGER.warning("Observed-fit process pool unavailable; fitting serially: %s", exc)
        return {}


def prefit_observed_expiries(tasks, fit_task, *, environment_variable):
    """Return ordered per-expiry results, or defer to the caller's serial path.

    Each task already contains its exact selected observations and initial
    parameters. Workers perform no database writes or publication actions.
    A process-pool infrastructure failure leaves the original serial path
    available; individual fitting failures are returned by ``fit_task``.
    """
    workers = worker_budget(environment_variable)
    if current_process().name != "MainProcess":
        return {}
    if workers == 1 or len(tasks) < 8:
        return {}
    try:
        return dict(_map(tasks, fit_task, workers))
    except Exception as exc:
        _LOGGER.warning("Observed-fit process pool unavailable; fitting serially: %s", exc)
        return {}
