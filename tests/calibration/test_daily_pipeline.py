"""Deterministic overlap, fencing and result ordering at the batch boundary."""

from contextlib import nullcontext
from threading import Event, Lock
from types import SimpleNamespace

from dash_options.calibration.execution import daily_runner as runner
from dash_options.calibration.execution.repository import JobStatus
from dash_options.calibration.execution.fit_pool import _SESSION


def test_next_fit_overlaps_publication_with_one_budget_and_writer(monkeypatch):
    jobs = {name: SimpleNamespace(job_id=name, payload={"product": product, "mode": "publish"},
                                 created_by="owner", max_attempts=1, attempts=1, status=JobStatus.SUCCEEDED)
            for name, product in [("first", "BRENT"), ("second", "HH"), ("third", "TTF")]}
    engine = SimpleNamespace(begin=lambda: nullcontext(object()))
    monkeypatch.setattr(runner, "PostgresJobRepository", lambda engine: SimpleNamespace(get=lambda job_id: jobs[job_id]))
    monkeypatch.setattr(runner, "_authorize", lambda *a, **k: None)
    monkeypatch.setattr(runner.DailyStore, "get", lambda *a: {"old": "unchanged"})
    monkeypatch.setattr(runner.DailyStore, "insert", lambda *a: None)
    monkeypatch.setattr(runner.api, "historical_content_snapshot", lambda *a: {"old": "unchanged"})
    first_publication, next_fit = Event(), Event()
    count_lock = Lock()
    running = {"fit": 0, "publication": 0}
    pool_contexts = []

    def execute(engine, job_id, *, identity):
        if job_id != "first":
            assert first_publication.wait(3)
        pool_contexts.append(id(_SESSION.get()))
        for kind in ("fit", "publication"):
            with runner._execution_slot(kind, jobs[job_id].payload["product"], lambda: None):
                with count_lock:
                    running[kind] += 1
                    assert running[kind] == 1
                if kind == "fit" and job_id == "second":
                    next_fit.set()
                if kind == "publication" and job_id == "first":
                    first_publication.set()
                    assert next_fit.wait(3), "Next product did not fit during publication"
                with count_lock:
                    running[kind] -= 1
        return jobs[job_id]

    monkeypatch.setattr(runner, "run_daily", execute)
    result = runner.run_daily_batch(engine, list(jobs), identity=object())
    assert [job.job_id for job in result] == list(jobs)
    assert len(set(pool_contexts)) == 1
    assert _SESSION.get() is None and runner._EXECUTION_SLOTS.get() is None


def test_waiting_slot_checks_cancellation_and_releases_after_failure():
    import pytest
    held = Lock()
    held.acquire()
    token = runner._EXECUTION_SLOTS.set({"fit": held})
    def cancelled():
        raise runner.LeaseLost("cancelled while queued")
    try:
        with pytest.raises(runner.LeaseLost):
            with runner._execution_slot("fit", "HH", cancelled):
                pytest.fail("Cancelled work acquired the fit slot")
        held.release()
        with pytest.raises(runner.LeaseLost):
            with runner._execution_slot("fit", "HH", cancelled):
                pytest.fail("Cancelled work ran after acquiring the slot")
        assert held.acquire(blocking=False)
        held.release()
    finally:
        runner._EXECUTION_SLOTS.reset(token)
