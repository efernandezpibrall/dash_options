"""The parallel fitting scheduler must preserve a reliable serial path."""

import pytest

from vol_calibration import observed_fit_pool


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
