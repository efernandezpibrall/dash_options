"""Behavioural gates for detached, checkpointed gas calibration."""

from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace
from datetime import date
from uuid import uuid4

import pandas as pd
import pytest

from vol_calibration.batch_checkpoints import digest, make_checkpoint
from vol_calibration.batch_job_runner import (
    build_payload,
    changed_source_expiry,
    completed_batch,
    submit_batch,
)
from vol_calibration.jobs import JobRecord, JobStatus
from vol_calibration.pages import jkm, ttf
from options.calibration_engine.config.defaults import get_defaults


def test_batch_payload_is_immutable_json_with_live_page_nulls():
    market = pd.DataFrame({
        "expiry": [pd.Timestamp("2026-11-01")],
        "option_expiration_date": [pd.Timestamp("2026-10-28")],
        "delta": [0.5],
        "iv": [0.4],
    })
    payload = build_payload(
        product="JKM",
        trade_date="2026-09-25",
        market_data_json=market.to_json(date_format="iso", orient="split"),
        table_data=[{"expiry": "Nov-26", "vr": 0.4, "left_blend_width": float("nan")}],
        skip_good=False,
        publication_payload={
            "publication_id": "revision-1",
            "input_manifest_fingerprint": "source-hash",
            "data": "dense-surface" * 100000,
            "expiry_results": [{"large": True}],
        },
    )
    assert payload["table_data"][0]["left_blend_width"] is None
    assert payload["code_fingerprint"]
    assert payload["publication_payload"]["publication_id"] == "revision-1"
    assert "data" not in payload["publication_payload"]
    assert len(json.dumps(payload)) < 3000
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload


def test_explicit_resubmission_after_cancel_creates_a_new_job():
    market = pd.DataFrame({
        "expiry": [pd.Timestamp("2026-11-01")],
        "option_expiration_date": [pd.Timestamp("2026-10-28")],
    })
    payload = build_payload(
        product="JKM", trade_date="2026-09-25",
        market_data_json=market.to_json(date_format="iso", orient="split"),
        table_data=[{"expiry": "Nov-26"}], skip_good=False,
    )

    class Repository:
        def __init__(self):
            self.keys = []

        def submit(self, **kwargs):
            self.keys.append(kwargs["idempotency_key"])
            return JobRecord(
                job_id=uuid4(), run_id=None,
                status=JobStatus.CANCELLED if len(self.keys) == 1 else JobStatus.QUEUED,
                payload=payload, attempts=0, max_attempts=3,
                cancellation_requested=len(self.keys) == 1,
            )

    repo = Repository()
    assert submit_batch(repo, payload, created_by="tester").status == JobStatus.QUEUED
    assert len(repo.keys) == 2
    assert repo.keys[0] != repo.keys[1]


def test_progress_poll_uses_compact_publication_reference():
    from dash._callback import GLOBAL_CALLBACK_LIST

    for product in ("jkm", "ttf"):
        matches = [
            spec for spec in GLOBAL_CALLBACK_LIST
            if f"{product}-batch-progress-cancel-btn.disabled" in spec["output"]
            and f"{product}-batch-results-container.children" in spec["output"]
        ]
        assert len(matches) == 1
        state_ids = {state["id"] for state in matches[0]["state"]}
        assert f"{product}-batch-base-publication-store" in state_ids
        assert f"{product}-published-surface-store" not in state_ids


def test_source_change_finds_first_affected_expiry_without_type_false_positive():
    fresh = pd.DataFrame({
        "expiry": [pd.Timestamp("2026-11-01"), pd.Timestamp("2026-12-01")],
        "option_expiration_date": [
            pd.Timestamp("2026-10-28"), pd.Timestamp("2026-11-27")
        ],
        "dte": [20.0, 50.0],
        "delta": [0.5, 0.5],
        "iv": [0.4, 0.5],
    })
    snapshot = pd.read_json(
        StringIO(fresh.to_json(date_format="iso", orient="split")), orient="split"
    )
    assert changed_source_expiry(snapshot, fresh) is None
    changed = fresh.copy()
    changed.loc[1, "iv"] = 0.51
    assert changed_source_expiry(snapshot, changed) == pd.Timestamp("2026-12-01")


def test_completed_batch_verifies_saved_checkpoint_integrity():
    job_id = uuid4()
    payload = {"table_data": [{"expiry": "Nov-26", "vr": 0.4}]}
    job = JobRecord(
        job_id=job_id, run_id=None, status=JobStatus.SUCCEEDED,
        payload=payload, attempts=1, max_attempts=3,
        cancellation_requested=False, completed_items=1, total_items=1,
    )
    saved = make_checkpoint(
        expiry="2026-11-01",
        result_row={"expiry": "2026-11-01", "status": "Success"},
        updated_row={"expiry": "Nov-26", "vr": 0.5},
        warm_start_after={"vr": 0.5},
        input_fingerprint=digest("input"),
        dependency_fingerprint=digest(None),
    )

    class Repository:
        def get(self, *, job_id):
            return job

        def list_items(self, *, job_id):
            return [{
                "status": "succeeded",
                "result_payload": saved,
                "input_fingerprint": saved["input_fingerprint"],
                "dependency_fingerprint": saved["dependency_fingerprint"],
                "option_expiration_date": date(2026, 10, 28),
            }]

    repo = Repository()
    assert completed_batch(repo, job_id)["table_data"][0]["vr"] == 0.5
    saved["updated_row"]["vr"] = 0.6
    with pytest.raises(ValueError, match="stale"):
        completed_batch(repo, job_id)


@pytest.mark.parametrize("page", [jkm, ttf])
def test_page_submits_detached_job_without_running_calibration(monkeypatch, page):
    product = page.COMMODITY
    market = pd.DataFrame({
        "expiry": [pd.Timestamp("2026-11-01")],
        "option_expiration_date": [pd.Timestamp("2026-10-28")],
        "delta": [0.5],
        "iv": [0.4],
    })
    market_json = market.to_json(date_format="iso", orient="split")
    table = [{"expiry": "Nov-26", "vr": 0.4, "left_blend_width": None}]
    job = JobRecord(
        job_id=uuid4(), run_id=None, status=JobStatus.QUEUED,
        payload={}, attempts=0, max_attempts=3,
        cancellation_requested=False, completed_items=0, total_items=1,
    )
    launched = []
    monkeypatch.setattr(page, "background_jobs_enabled", lambda: True)
    monkeypatch.setattr(page, "ctx", SimpleNamespace(
        triggered_id=f"{product.lower()}-batch-confirm-btn"
    ))
    monkeypatch.setattr(page, "job_repository", lambda: object())
    monkeypatch.setattr(page, "submit_batch", lambda *_args, **_kwargs: job)
    monkeypatch.setattr(page, "start_worker", lambda job_id: launched.append(job_id))
    monkeypatch.setattr(
        page,
        "calibrate_jkm_batch" if product == "JKM" else "calibrate_ttf_batch",
        lambda *_args, **_kwargs: pytest.fail("The Dash callback ran calibration synchronously"),
    )
    args = (1, None, market_json, table, [], [], "2026-09-25", False)
    if product == "TTF":
        args += (None, None)
    else:
        args += (None,)
    result = page.run_batch_calibration(*args)
    assert len(result) == 9
    assert result[1] == 0
    assert result[5] is None
    assert result[8]["job_id"] == str(job.job_id)
    assert launched == [job.job_id]


def test_jkm_page_applies_completed_job_once_and_enables_existing_save_gate(monkeypatch):
    market = pd.DataFrame({
        "expiry": [pd.Timestamp("2026-11-01")],
        "option_expiration_date": [pd.Timestamp("2026-10-28")],
        "delta": [0.5],
        "iv": [0.4],
    })
    market_json = market.to_json(date_format="iso", orient="split")
    base = [{"expiry": "Nov-26", "vr": 0.4, "arb_status": "Uncalibrated"}]
    accepted = [{
        **base[0], "vr": 0.5, "arb_status": "Pass",
        "calibration_method": jkm.JKM_HYBRID_METHOD,
    }]
    result_rows = [{"expiry": "2026-11-01", "status": "Success"}]
    payload = build_payload(
        product="JKM", trade_date="2026-09-25",
        market_data_json=market_json, table_data=base, skip_good=False,
        publication_payload={"publication_id": None},
    )
    job = JobRecord(
        job_id=uuid4(), run_id=None, status=JobStatus.SUCCEEDED,
        payload=payload, attempts=1, max_attempts=3,
        cancellation_requested=False, completed_items=1, total_items=1,
    )

    class Repository:
        def get(self, *, job_id):
            return job

    monkeypatch.setattr(jkm, "background_jobs_enabled", lambda: True)
    monkeypatch.setattr(jkm, "job_repository", Repository)
    monkeypatch.setattr(jkm, "completed_batch", lambda *_args: {
        "results": result_rows, "table_data": accepted,
        "success_count": 1, "skip_count": 0, "fail_count": 0,
    })
    reference = {"job_id": str(job.job_id), "payload_fingerprint": digest(payload)}
    first = jkm.poll_jkm_batch_job(
        1, reference, market_json, base, "2026-09-25",
        {"publication_id": None}, [], True, None,
    )
    assert first[1] == 100
    assert first[5]["background_job_id"] == str(job.job_id)
    assert first[6] == accepted
    assert jkm._batch_state_ready(
        first[5], "2026-09-25", market_json, accepted, None,
    ) == (True, None)
    second = jkm.poll_jkm_batch_job(
        2, reference, market_json, accepted, "2026-09-25",
        {"publication_id": None}, [], True, first[5],
    )
    from dash import no_update
    assert second[5] is no_update
    assert second[6] is no_update


def test_ttf_page_applies_completed_job_to_existing_save_gate(monkeypatch):
    market = pd.DataFrame({
        "expiry": [pd.Timestamp("2026-11-01")],
        "option_expiration_date": [pd.Timestamp("2026-10-28")],
        "delta": [0.5],
        "iv": [0.4],
    })
    market_json = market.to_json(date_format="iso", orient="split")
    base = [{"expiry": "Nov-26", **get_defaults("TTF"), "arb_status": "Uncalibrated"}]
    accepted = [{
        **base[0], "arb_status": "Pass",
        "calibration_method": ttf.TTF_HYBRID_METHOD,
        "left_blend_width": 0.1, "right_blend_width": 0.1,
        "core_tv_rmse": 0.0, "tail_fit_tv_rmse": 0.001,
        "iv_rmse": 0.01,
    }]
    result_rows = [{"expiry": "2026-11-01", "status": "Success"}]
    payload = build_payload(
        product="TTF", trade_date="2026-09-25",
        market_data_json=market_json, table_data=base, skip_good=False,
        publication_payload={"publication_id": None},
    )
    job = JobRecord(
        job_id=uuid4(), run_id=None, status=JobStatus.SUCCEEDED,
        payload=payload, attempts=1, max_attempts=3,
        cancellation_requested=False, completed_items=1, total_items=1,
    )

    class Repository:
        def get(self, *, job_id):
            return job

    monkeypatch.setattr(ttf, "background_jobs_enabled", lambda: True)
    monkeypatch.setattr(ttf, "job_repository", Repository)
    monkeypatch.setattr(ttf, "completed_batch", lambda *_args: {
        "results": result_rows, "table_data": accepted,
        "success_count": 1, "skip_count": 0, "fail_count": 0,
    })
    reference = {"job_id": str(job.job_id), "payload_fingerprint": digest(payload)}
    first = ttf.poll_ttf_batch_job(
        1, reference, market_json, base, "2026-09-25",
        None, {"publication_id": None}, [], True, None,
    )
    assert first[1] == 100
    assert first[5]["background_job_id"] == str(job.job_id)
    assert first[6] == accepted
    assert ttf._batch_state_ready(
        first[5], "2026-09-25", market_json, accepted, None, None,
    ) == (True, None)
