"""The parallel fitting scheduler must preserve a reliable serial path."""

import os

import numpy as np
import pytest
import pandas as pd
from options.vol_calibration import api

from dash_options.calibration.execution import fit_pool as observed_fit_pool


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


def test_resource_profiles_respect_visible_cpu_budget(monkeypatch):
    monkeypatch.setattr(os, "process_cpu_count", lambda: 1, raising=False)
    monkeypatch.setenv("VOL_CALIBRATION_WORKERS", "4")
    assert observed_fit_pool.worker_budget("TEST_OBSERVED_WORKERS") == 1
    monkeypatch.setattr(os, "process_cpu_count", lambda: 16)
    monkeypatch.setenv("VOL_CALIBRATION_WORKERS", "2")
    monkeypatch.setenv("TEST_OBSERVED_WORKERS", "4")
    assert observed_fit_pool.worker_budget("TEST_OBSERVED_WORKERS") <= 2


def test_streamed_results_do_not_wait_for_later_expiries_and_close_cancels(monkeypatch):
    from concurrent.futures import Future
    futures = []
    class Pool:
        def submit(self, fit, task):
            future = Future()
            if not futures:
                future.set_result((task[0], (True, "accepted")))
            futures.append(future)
            return future
    monkeypatch.setattr(observed_fit_pool, "_session_executor", lambda workers: (Pool(), False))
    result = observed_fit_pool._ObservedResults([(i,) for i in range(8)], object(), 2, None)
    assert result.get(0) == (True, "accepted")
    assert all(not f.done() for f in futures[1:])
    result.close()
    assert all(f.cancelled() for f in futures[1:])


def test_streamed_cancellation_propagates_without_refitting(monkeypatch):
    from concurrent.futures import Future
    futures = []
    class Pool:
        def submit(self, fit, task):
            f = Future(); futures.append(f); return f
    monkeypatch.setattr(observed_fit_pool, "_session_executor", lambda workers: (Pool(), False))
    def cancel():
        raise RuntimeError("lease cancelled")
    result = observed_fit_pool._ObservedResults([(i,) for i in range(8)], object(), 2, cancel)
    with pytest.raises(RuntimeError, match="lease cancelled"):
        result.get(0)
    assert all(f.cancelled() for f in futures)


def test_three_and_six_starts_reuse_one_compute_budget(monkeypatch):
    from concurrent.futures import Future
    pools = []
    class Pool:
        def __init__(self, **kwargs):
            pools.append(kwargs["max_workers"])
        def submit(self, fit, task):
            f = Future(); f.set_result(fit(task)); return f
        def shutdown(self, **kwargs):
            pass
    monkeypatch.setattr(observed_fit_pool, "ProcessPoolExecutor", Pool)
    monkeypatch.setattr(observed_fit_pool, "worker_budget", lambda env: 4)
    with observed_fit_pool.observed_fit_session():
        assert observed_fit_pool.execute_hybrid_starts(list(range(3)), str) == ["0", "1", "2"]
        assert observed_fit_pool.execute_hybrid_starts(list(range(6)), str) == list(map(str, range(6)))
    assert pools == [4]


def _numeric_probe(task):
    key, product, observations = task
    result = api.fit_gas_hybrid_candidate(observations, api.get_defaults(product), commodity=product)
    return key, {"parameters": result["params"], "minimum_density": result["validation"]["min_g"],
                 "valid": result["validation"]["is_valid"]}


@pytest.mark.parametrize("product", ["TTF", "JKM", "NBP"])
def test_real_fit_has_serial_parallel_parity(product, monkeypatch):
    x = np.array([-.60,-.40,-.25,-.12,-.04,0.,.08,.20,.40,.70,1.10])
    observations = pd.DataFrame({"expiry": pd.Timestamp("2026-10-01"),
        "option_expiration_date": pd.Timestamp("2026-09-25"), "forward": 50., "strike": 50 * np.exp(x),
        "iv": .70 + .10*x + .03*x*x, "delta": [.99,.90,.80,.70,.60,.50,.40,.30,.20,.10,.01],
        "dte": 90., "delta_convention": "undiscounted_call_delta", "source_name": "official",
        "quote_class": "observed", "weight": 1., "calibration_basis": "observed"})
    tasks = [(key, product, observations) for key in range(8)]
    expected = _numeric_probe(tasks[0])[1]
    monkeypatch.setenv("TEST_OBSERVED_WORKERS", "2")
    monkeypatch.setenv("VOL_CALIBRATION_WORKERS", "2")
    with observed_fit_pool.observed_fit_session():
        parallel = observed_fit_pool.prefit_observed_expiries(tasks, _numeric_probe,
                                                           environment_variable="TEST_OBSERVED_WORKERS")
    assert set(parallel) == set(range(8))
    for actual in parallel.values():
        assert actual["valid"] and expected["valid"]
        assert actual["parameters"] == pytest.approx(expected["parameters"], abs=1e-10)
        assert actual["minimum_density"] == pytest.approx(expected["minimum_density"], abs=1e-12)
