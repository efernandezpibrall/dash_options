"""Gas pages must share job behavior without applying stale candidates or writes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from dash import no_update
from dash.exceptions import PreventUpdate
import pandas as pd
import pytest

from app import server
from options.vol_calibration.api import digest
from vol_calibration import batch_job_runner
from vol_calibration.jobs import JobRecord, JobStatus
from vol_calibration.pages import jkm, ttf


@pytest.fixture(params=[jkm, ttf], ids=["JKM", "TTF"])
def workspace(request, monkeypatch):
    page = request.param
    monkeypatch.setattr(batch_job_runner, "background_jobs_enabled", lambda: True)
    monkeypatch.setattr(batch_job_runner, "code_fingerprint", lambda: "test-engine")
    market = pd.DataFrame({
        "expiry": [pd.Timestamp("2026-11-01")],
        "option_expiration_date": [pd.Timestamp("2026-10-28")],
        "delta": [0.5], "iv": [0.4],
    }).to_json(date_format="iso", orient="split")
    rows = [{"expiry": "Nov-26", "vr": 0.4}]
    publication = {"publication_id": "base-publication"}
    nodes = {"Nov-26": {"nodes": {"ATM": 0.41}}} if page is ttf else None
    controller = page._batch_job_controller(nodes) if page is ttf else page._batch_job_controller()
    payload = controller._payload("2026-09-25", market, rows, False, publication)
    job = JobRecord(
        job_id=uuid4(), run_id=None, status=JobStatus.RUNNING, payload=payload,
        attempts=1, max_attempts=3, cancellation_requested=False,
        completed_items=2, total_items=4,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    reference = {"job_id": str(job.job_id), "payload_fingerprint": digest(payload)}
    return SimpleNamespace(
        page=page, controller=controller, market=market, rows=rows,
        publication=publication, nodes=nodes, job=job, reference=reference,
    )


def poll(workspace, **overrides):
    args = {
        "reference": workspace.reference, "market_json": workspace.market,
        "rows": workspace.rows, "trade_date": "2026-09-25",
        "publication": workspace.publication, "skip_options": [],
        "is_open": True, "batch_state": None,
        **overrides,
    }
    return workspace.controller.poll(**args)


def repository(monkeypatch, job):
    monkeypatch.setattr(batch_job_runner, "job_repository", lambda: SimpleNamespace(get=lambda **_: job))


@pytest.mark.parametrize("change", ["market", "rows", "date", "publication", "skip"])
def test_completed_job_rejects_changed_page_inputs(workspace, monkeypatch, change):
    repository(monkeypatch, replace(workspace.job, status=JobStatus.SUCCEEDED))
    monkeypatch.setattr(batch_job_runner, "completed_batch", lambda *_: pytest.fail("stale results applied"))
    changes = {
        "market": {"market_json": workspace.market.replace("0.4", "0.5")},
        "rows": {"rows": [{"expiry": "Nov-26", "vr": 0.5}]},
        "date": {"trade_date": "2026-09-24"},
        "publication": {"publication": {"publication_id": "new-publication"}},
        "skip": {"skip_options": ["skip_good"]},
    }
    result = poll(workspace, **changes[change])
    assert "Inputs changed" in result[2]
    assert result[5] is None and result[6] is no_update
    assert result[7].color == "warning"


def test_ttf_node_edits_cannot_receive_an_older_candidate(monkeypatch):
    monkeypatch.setattr(batch_job_runner, "background_jobs_enabled", lambda: True)
    monkeypatch.setattr(batch_job_runner, "code_fingerprint", lambda: "test-engine")
    market = pd.DataFrame({
        "expiry": [pd.Timestamp("2026-11-01")],
        "option_expiration_date": [pd.Timestamp("2026-10-28")],
    }).to_json(date_format="iso", orient="split")
    rows = [{"expiry": "Nov-26", "vr": 0.4}]
    original = ttf._batch_job_controller({"Nov-26": {"nodes": {"ATM": 0.4}}})
    changed = ttf._batch_job_controller({"Nov-26": {"nodes": {"ATM": 0.5}}})
    payload = original._payload("2026-09-25", market, rows, False, {})
    job = JobRecord(uuid4(), None, JobStatus.SUCCEEDED, payload, 1, 3, False, 1, 1)
    repository(monkeypatch, job)
    monkeypatch.setattr(batch_job_runner, "completed_batch", lambda *_: pytest.fail("stale nodes applied"))
    result = changed.poll(
        {"job_id": str(job.job_id), "payload_fingerprint": digest(payload)},
        market, rows, "2026-09-25", {}, [], True, None,
    )
    assert "Inputs changed" in result[2]
    assert result[6] is no_update


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
@pytest.mark.parametrize("expired", [False, True])
def test_only_queued_or_expired_jobs_restart_workers(workspace, monkeypatch, status, expired):
    lease = datetime.now(timezone.utc) + timedelta(minutes=-5 if expired else 5)
    repository(monkeypatch, replace(workspace.job, status=status, lease_expires_at=lease))
    launched = []
    monkeypatch.setattr(batch_job_runner, "start_worker", launched.append)
    result = poll(workspace)
    assert launched == ([workspace.job.job_id] if status == JobStatus.QUEUED or expired else [])
    assert result[1] == 50 and result[6] is no_update and result[8] is False


def test_worker_start_failure_keeps_candidate_unapplied(workspace, monkeypatch):
    repository(monkeypatch, replace(workspace.job, status=JobStatus.QUEUED))
    def fail(_job_id):
        raise OSError("worker unavailable")
    monkeypatch.setattr(batch_job_runner, "start_worker", fail)
    result = poll(workspace)
    assert "Could not start calibration worker" in result[2]
    assert result[5] is no_update and result[6] is no_update


@pytest.mark.parametrize("status", [JobStatus.FAILED, JobStatus.CANCELLED])
def test_terminal_failure_clears_candidate_without_replacing_parameters(workspace, monkeypatch, status):
    repository(monkeypatch, replace(workspace.job, status=status, last_error="job stopped"))
    result = poll(workspace)
    assert result[2] == "job stopped"
    assert result[5] is None and result[6] is no_update and result[8] is True


@pytest.mark.parametrize("unavailable", ["missing", "database"])
def test_unavailable_jobs_do_not_replace_parameters(workspace, monkeypatch, unavailable):
    if unavailable == "missing":
        repository(monkeypatch, None)
    else:
        def fail():
            raise ConnectionError("offline")
        monkeypatch.setattr(batch_job_runner, "job_repository", fail)
    result = poll(workspace)
    assert ("missing" if unavailable == "missing" else "Waiting") in result[2]
    assert result[6] is no_update


def test_cancellation_uses_server_identity_and_owner_guard(workspace, monkeypatch):
    calls = []
    monkeypatch.setattr(batch_job_runner, "job_repository", lambda: SimpleNamespace(
        request_cancel=lambda **kwargs: calls.append(kwargs) or None,
    ))
    from vol_calibration import auth
    monkeypatch.setattr(auth, "resolve_request_identity", lambda *_args, **_kwargs: SimpleNamespace(subject="owner"))
    with server.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert workspace.controller.cancel(workspace.reference) == "Cancellation unavailable."
    assert calls == [{"job_id": workspace.job.job_id, "created_by": "owner"}]


def test_disabled_jobs_cannot_poll_or_cancel(workspace, monkeypatch):
    monkeypatch.setattr(batch_job_runner, "background_jobs_enabled", lambda: False)
    monkeypatch.setattr(batch_job_runner, "job_repository", lambda: pytest.fail("disabled job accessed"))
    with pytest.raises(PreventUpdate):
        poll(workspace)
    with pytest.raises(PreventUpdate):
        workspace.controller.cancel(workspace.reference)


def test_close_and_unloaded_inputs_do_not_start_a_job(workspace, monkeypatch):
    monkeypatch.setattr(batch_job_runner, "submit_batch", lambda *_args, **_kwargs: pytest.fail("unexpected job"))
    prefix = workspace.page.COMMODITY.lower()
    closed = workspace.controller.run(f"{prefix}-batch-progress-close-btn", None, None, [], None, None)
    assert closed[0] is False and closed[5] is no_update
    waiting = workspace.controller.run(f"{prefix}-batch-confirm-btn", None, [], [], None, None)
    assert "Waiting" in waiting[2] and waiting[8] is None
