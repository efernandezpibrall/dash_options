"""The parallel fitting scheduler must preserve a reliable serial path."""

import os

import numpy as np
import pytest

from vol_calibration import observed_fit_pool


def _worker_probe(value):
    from threadpoolctl import threadpool_info

    np.dot(np.ones((2, 2)), np.ones((2, 2)))
    return value, (
        os.getpid(), observed_fit_pool._BLAS_LIMIT is not None,
        [pool["num_threads"] for pool in threadpool_info()],
    )


def test_observed_pool_runs_in_children_with_bounded_numerical_threads(monkeypatch):
    monkeypatch.setenv("TEST_OBSERVED_WORKERS", "2")
    result = observed_fit_pool.prefit_observed_expiries(
        list(range(8)), _worker_probe, environment_variable="TEST_OBSERVED_WORKERS"
    )
    assert set(result) == set(range(8))
    for pid, initialized, thread_counts in result.values():
        assert pid != os.getpid()
        assert initialized
        # Accelerate on macOS is not enumerated by threadpoolctl.
        assert all(count == 1 for count in thread_counts)


def test_observed_pool_serial_switch_and_process_failure(monkeypatch):
    tasks = [(index,) for index in range(8)]
    monkeypatch.setenv("TEST_OBSERVED_WORKERS", "1")
    assert observed_fit_pool.prefit_observed_expiries(
        tasks, object(), environment_variable="TEST_OBSERVED_WORKERS"
    ) == {}

    monkeypatch.setenv("TEST_OBSERVED_WORKERS", "4")

    class FailedPool:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            raise OSError("workers unavailable")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(observed_fit_pool, "ProcessPoolExecutor", FailedPool)
    assert observed_fit_pool.prefit_observed_expiries(
        tasks, object(), environment_variable="TEST_OBSERVED_WORKERS"
    ) == {}


def test_observed_pool_rejects_unbounded_workers(monkeypatch):
    monkeypatch.setenv("TEST_OBSERVED_WORKERS", "8")
    with pytest.raises(ValueError, match="between 1 and 4"):
        observed_fit_pool.prefit_observed_expiries(
            [], object(), environment_variable="TEST_OBSERVED_WORKERS"
        )
