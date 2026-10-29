from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time
from uuid import uuid4

import pytest
from dash import no_update

from dash_options.features.vol_trades.intraday import jobs as job_backends
from dash_options.core.snapshots import SnapshotStore
from options.vol_calibration import api
from options.vol_calibration.intraday_tfo import identity


@pytest.fixture(params=["brent_jobs", "ttf_jobs"])
def jobs(request):
    return getattr(job_backends, request.param)


@pytest.fixture
def harness(tmp_path, monkeypatch, jobs):
    store = SnapshotStore(tmp_path / "cache")
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(jobs, "snapshot_store", store)
    monkeypatch.setattr(jobs, "_POOL", pool)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "source_config_fingerprint", lambda: "test-source")
    source = {
        "manifest": {
            "snapshot_id": "snapshot",
            "evidence_hash": "unchanged",
            "expected_intraday_id": None,
            "implementation": api.implementation_fingerprint(),
        }
    }
    counts = {"load": 0, "fit": 0}

    def load(*args, **kwargs):
        counts["load"] += 1
        return deepcopy(source)

    def fit(inputs, **kwargs):
        counts["fit"] += 1
        value = {"manifest": deepcopy(inputs["manifest"]), "surface": []}
        value["candidate_id"] = identity(value)
        return value

    monkeypatch.setattr(jobs, "load_inputs", load)
    monkeypatch.setattr(jobs, "fit", fit)
    request = {"scope": str(uuid4()), "snapshot_id": "snapshot", "start": "start", "end": "end", "key": "first"}
    yield source, counts, request, load
    pool.shutdown(wait=True)
    store.cache.close()


def completed(ticket, jobs, actor="alice"):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = jobs.poll(ticket, actor)
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    pytest.fail("Intraday test job did not finish")


def test_concurrent_requests_claim_once_and_completed_result_survives_five_minutes(harness, jobs):
    _, counts, request, _ = harness
    with ThreadPoolExecutor(max_workers=8) as pool:
        tickets = list(pool.map(lambda _: jobs.submit(None, request, "alice"), range(8)))
    assert len({t["key"] for t in tickets}) == 1
    state = completed(tickets[0], jobs)
    assert state["status"] == "complete"
    state["started"] -= 301
    jobs.snapshot_store.cache.set(tickets[0]["key"], state, expire=3600)
    assert jobs.submit(None, request, "alice") == tickets[0]
    assert counts == {"load": 1, "fit": 1}
    with pytest.raises(ValueError):
        jobs.poll(tickets[0], "bob")
    with pytest.raises(Exception):
        jobs.resolve(state["ref"], "bob")


def test_publication_change_reuses_fit_but_source_change_recalculates(harness, jobs):
    source, counts, request, _ = harness
    first = completed(jobs.submit(None, request, "alice"), jobs)
    source["manifest"]["expected_intraday_id"] = "published"
    second = completed(
        jobs.submit(None, dict(request, key="second", display_publication_id="published"), "alice"), jobs
    )
    assert second["status"] == "complete", second
    assert second["fit_reused"] and counts == {"load": 2, "fit": 1}
    before = jobs.resolve(first["ref"], "alice")
    after = jobs.resolve(second["ref"], "alice")
    assert before["manifest"]["expected_intraday_id"] is None
    assert after["manifest"]["expected_intraday_id"] == "published"
    assert before["candidate_id"] != after["candidate_id"]
    source["manifest"]["evidence_hash"] = "corrected"
    third = completed(jobs.submit(None, dict(request, key="third"), "alice"), jobs)
    assert not third["fit_reused"] and counts["fit"] == 2


def test_superseded_scope_cancels_without_cancelling_another_tab(harness, monkeypatch, jobs):
    _, counts, request, load = harness
    entered = threading.Event()
    release = threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return load(*args, **kwargs)

    monkeypatch.setattr(jobs, "load_inputs", blocked)
    old = jobs.submit(None, request, "alice")
    assert entered.wait(5)
    new = jobs.submit(None, dict(request, key="new"), "alice")
    other = jobs.submit(None, dict(request, key="other", scope=str(uuid4())), "alice")
    release.set()
    assert completed(old, jobs)["status"] == "failed"
    assert completed(new, jobs)["status"] == "complete"
    assert completed(other, jobs)["status"] == "complete"
    assert counts["fit"] == 1


@pytest.mark.parametrize("product", ["brent", "ttf"])
def test_closed_review_does_no_source_or_pricing_work(monkeypatch, product):
    from importlib import import_module

    ui = import_module(f"dash_options.features.vol_trades.intraday.{product}_workspace")

    def unexpected(*args, **kwargs):
        raise AssertionError("Closed modal performed work")

    monkeypatch.setattr(ui, "review_candidate", unexpected)
    assert ui.chart("2026-11-01", {"ref": "candidate"}, None, {}, False) == (no_update, no_update)
