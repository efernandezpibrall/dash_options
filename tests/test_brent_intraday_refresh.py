from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import brent_option_chain_refresh as gateway
from pages import brent_vol_history as history
from vol_calibration.auth import (
    AuthorizationError,
    Identity,
    Permission,
    Role,
    authorize,
)


def _worker_readiness(product="BRENT", *, ready=True, reason="ready"):
    return gateway.WorkerReadiness(
        product=product,
        ready=ready,
        reason=reason,
        worker_id="worker-1" if ready else None,
        lifecycle_status="idle" if ready else None,
        bloomberg_session_status="not_started" if ready else None,
        heartbeat_at="2026-08-24T12:00:00+00:00" if ready else None,
    )


@pytest.fixture(autouse=True)
def _default_ready_worker(monkeypatch):
    monkeypatch.setattr(
        history,
        "get_worker_readiness",
        lambda product: _worker_readiness(product),
    )
    monkeypatch.setattr(
        history,
        "get_worker_readiness_many",
        lambda products: {
            product: _worker_readiness(product) for product in products
        },
    )


def test_intraday_refresh_flag_defaults_off_and_accepts_explicit_env(monkeypatch):
    monkeypatch.delenv("BBG_OPTION_CHAIN_INTRADAY_REFRESH_ENABLED", raising=False)
    monkeypatch.setattr(gateway, "config_bool", lambda *args, **kwargs: False)
    assert gateway.intraday_refresh_enabled() is False
    monkeypatch.setenv("BBG_OPTION_CHAIN_INTRADAY_REFRESH_ENABLED", "true")
    assert gateway.intraday_refresh_enabled() is True


def test_settlement_refresh_has_an_independent_default_off_feature_flag(monkeypatch):
    monkeypatch.delenv("BBG_OPTION_CHAIN_SETTLEMENT_REFRESH_ENABLED", raising=False)
    monkeypatch.setattr(gateway, "config_bool", lambda *args, **kwargs: False)
    assert gateway.settlement_refresh_enabled() is False
    monkeypatch.setenv("BBG_OPTION_CHAIN_SETTLEMENT_REFRESH_ENABLED", "true")
    assert gateway.settlement_refresh_enabled() is True


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row

    def all(self):
        if self.row is None:
            return []
        return self.row if isinstance(self.row, list) else [self.row]


class _Connection:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []

    def execute(self, statement, parameters):
        self.calls.append((str(statement), dict(parameters)))
        return _Rows(next(self.rows))


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class _Engine:
    def __init__(self, rows):
        self.connection = _Connection(rows)

    def begin(self):
        return _ConnectionContext(self.connection)

    def connect(self):
        return _ConnectionContext(self.connection)


def _job_row(
    request_kind,
    *,
    product="BRENT",
    job_id="00000000-0000-0000-0000-000000000010",
    status="queued",
):
    return {
        "job_id": job_id,
        "product": product,
        "business_date": date(2026, 8, 17),
        "status": status,
        "stage": "complete" if status == "succeeded" else status,
        "requested_by": "cal",
        "request_kind": request_kind,
        "result_snapshot_id": None,
        "result_status": None,
        "previous_snapshot_id": None,
        "metrics": {},
        "last_error": None,
        "created_at": None,
        "updated_at": None,
    }


def test_worker_readiness_is_product_scoped_and_uses_database_time():
    heartbeat = pd.Timestamp("2026-08-24T12:00:00Z").to_pydatetime()
    engine = _Engine(
        [
            {
                "product": "TFO",
                "worker_id": "worker-1",
                "lifecycle_status": "idle",
                "bloomberg_session_status": "not_started",
                "heartbeat_at": heartbeat,
                "lifecycle_is_available": True,
                "heartbeat_is_fresh": True,
            }
        ]
    )

    readiness = gateway.get_worker_readiness("TFO", engine=engine)

    assert readiness.ready is True
    assert readiness.product == "TFO"
    assert readiness.worker_id == "worker-1"
    sql, parameters = engine.connection.calls[0]
    assert "requested.product = ANY(enabled_products)" in sql
    assert "CURRENT_TIMESTAMP" in sql
    assert "'starting', 'idle', 'running'" in sql
    assert parameters == {"products": ["TFO"], "freshness_seconds": 30}


def test_worker_readiness_fails_closed_when_registry_is_missing():
    readiness = gateway.get_worker_readiness("BRENT", engine=object())

    assert readiness.ready is False
    assert readiness.reason == "registry_unavailable"


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (None, "no_eligible_worker"),
        (
            {
                "worker_id": "worker-1",
                "lifecycle_status": "idle",
                "bloomberg_session_status": "not_started",
                "heartbeat_at": pd.Timestamp(
                    "2026-08-24T11:00:00Z"
                ).to_pydatetime(),
                "lifecycle_is_available": True,
                "heartbeat_is_fresh": False,
            },
            "stale_heartbeat",
        ),
        (
            {
                "worker_id": "worker-1",
                "lifecycle_status": "stopped",
                "bloomberg_session_status": "stopped",
                "heartbeat_at": pd.Timestamp(
                    "2026-08-24T12:00:00Z"
                ).to_pydatetime(),
                "lifecycle_is_available": False,
                "heartbeat_is_fresh": True,
            },
            "worker_not_running",
        ),
    ],
)
def test_worker_readiness_requires_eligible_fresh_running_worker(row, reason):
    if row is not None:
        row = {"product": "BRENT", **row}
    readiness = gateway.get_worker_readiness("BRENT", engine=_Engine([row]))

    assert readiness.ready is False
    assert readiness.reason == reason


@pytest.mark.parametrize(
    ("request_kind", "active_kind", "request_family"),
    [
        (gateway.INTRADAY_REQUEST_KIND, "daily_tape_seed", "intraday"),
        (
            gateway.SETTLEMENT_REQUEST_KIND,
            gateway.SETTLEMENT_REQUEST_KIND,
            "settlement",
        ),
    ],
)
def test_submit_refresh_job_coalesces_only_with_the_correct_job_family(
    monkeypatch,
    request_kind,
    active_kind,
    request_family,
):
    monkeypatch.setattr(gateway, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(gateway, "settlement_refresh_enabled", lambda _product: True)
    engine = _Engine([None, None, None, _job_row(active_kind)])

    job, created = gateway.submit_refresh_job(
        "cal",
        product="BRENT",
        engine=engine,
        business_date=date(2026, 8, 17),
        request_kind=request_kind,
    )

    assert created is False
    assert job.request_kind == active_kind
    assert "pg_advisory_xact_lock" in engine.connection.calls[0][0]
    assert engine.connection.calls[2][1]["request_kind"] == request_kind
    assert engine.connection.calls[3][1]["request_family"] == request_family
    active_sql = engine.connection.calls[3][0]
    assert "request_kind = 'settlement_refresh'" in active_sql
    assert "request_kind IN ('user_refresh', 'daily_tape_seed')" in active_sql


def test_worker_readiness_many_reads_all_products_in_one_query():
    heartbeat = pd.Timestamp("2026-08-24T12:00:00Z").to_pydatetime()
    engine = _Engine(
        [
            [
                {
                    "product": "BRENT",
                    "worker_id": "worker-1",
                    "lifecycle_status": "idle",
                    "bloomberg_session_status": "started",
                    "heartbeat_at": heartbeat,
                    "lifecycle_is_available": True,
                    "heartbeat_is_fresh": True,
                },
                {
                    "product": "TFO",
                    "worker_id": None,
                    "lifecycle_status": None,
                    "bloomberg_session_status": None,
                    "heartbeat_at": None,
                    "lifecycle_is_available": None,
                    "heartbeat_is_fresh": None,
                },
            ]
        ]
    )

    readiness = gateway.get_worker_readiness_many(
        ["BRENT", "TFO"],
        engine=engine,
    )

    assert readiness["BRENT"].ready is True
    assert readiness["TFO"].reason == "no_eligible_worker"
    assert len(engine.connection.calls) == 1
    sql, parameters = engine.connection.calls[0]
    assert "LEFT JOIN LATERAL" in sql
    assert parameters == {
        "products": ["BRENT", "TFO"],
        "freshness_seconds": 30,
    }


def test_settlement_batch_atomically_inserts_or_joins_all_products(monkeypatch):
    products = gateway.SETTLEMENT_REFRESH_PRODUCTS
    rows = [
        _job_row(
            gateway.SETTLEMENT_REQUEST_KIND,
            product=product,
            job_id=f"00000000-0000-0000-0000-{index:012d}",
        )
        for index, product in enumerate(products, start=1)
    ]
    engine = _Engine(
        [None, None, None, None, [], [rows[0], rows[2]], rows]
    )
    monkeypatch.setattr(gateway, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(
        gateway,
        "get_worker_readiness_many",
        lambda requested, *, engine: {
            product: _worker_readiness(product) for product in requested
        },
    )

    jobs = gateway.submit_settlement_refresh_jobs(
        "cal",
        engine=engine,
        business_date=date(2026, 8, 17),
    )

    assert tuple(jobs) == products
    assert [jobs[product][1] for product in products] == [True, False, True, False]
    assert len(engine.connection.calls) == 7
    assert [
        call[1]["lock_key"] for call in engine.connection.calls[:4]
    ] == [
        "bbg_option_chain_refresh:BRENT",
        "bbg_option_chain_refresh:TFO",
        "bbg_option_chain_refresh:ON",
        "bbg_option_chain_refresh:LNE",
    ]
    assert "request_kind IN ('user_refresh', 'daily_tape_seed')" in (
        engine.connection.calls[4][0]
    )
    assert "INSERT INTO" in engine.connection.calls[5][0]
    assert "ON CONFLICT DO NOTHING" in engine.connection.calls[5][0]
    assert engine.connection.calls[6][1]["products"] == list(products)


def test_settlement_batch_rejects_active_intraday_before_inserting(monkeypatch):
    engine = _Engine(
        [None, None, None, None, [{"product": "TFO"}]]
    )
    monkeypatch.setattr(gateway, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(
        gateway,
        "get_worker_readiness_many",
        lambda requested, *, engine: {
            product: _worker_readiness(product) for product in requested
        },
    )

    with pytest.raises(
        gateway.SettlementRefreshBatchError,
        match="intraday refresh is already active for TFO",
    ):
        gateway.submit_settlement_refresh_jobs(
            "cal",
            engine=engine,
            business_date=date(2026, 8, 17),
        )

    assert len(engine.connection.calls) == 5
    assert not any(
        "INSERT INTO" in sql for sql, _parameters in engine.connection.calls
    )


def test_intraday_submission_rejects_active_settlement_under_product_lock(
    monkeypatch,
):
    engine = _Engine(
        [
            None,
            {
                "product": "BRENT",
                "request_kind": gateway.SETTLEMENT_REQUEST_KIND,
            },
        ]
    )
    monkeypatch.setattr(gateway, "intraday_refresh_enabled", lambda _product: True)

    with pytest.raises(
        gateway.SettlementRefreshBatchError,
        match="settlement refresh is already active for BRENT",
    ):
        gateway.submit_refresh_job(
            "cal",
            product="BRENT",
            engine=engine,
            business_date=date(2026, 8, 17),
        )

    assert len(engine.connection.calls) == 2
    assert "pg_advisory_xact_lock" in engine.connection.calls[0][0]
    assert "request_kind = 'settlement_refresh'" in engine.connection.calls[1][0]
    assert not any(
        "INSERT INTO" in sql for sql, _parameters in engine.connection.calls
    )


def test_get_refresh_jobs_reads_product_job_pairs_in_one_query():
    rows = [
        _job_row(
            gateway.SETTLEMENT_REQUEST_KIND,
            product="BRENT",
            job_id="00000000-0000-0000-0000-000000000001",
        ),
        _job_row(
            gateway.SETTLEMENT_REQUEST_KIND,
            product="TFO",
            job_id="00000000-0000-0000-0000-000000000002",
        ),
    ]
    engine = _Engine([rows])

    jobs = gateway.get_refresh_jobs(
        {
            "BRENT": rows[0]["job_id"],
            "TFO": rows[1]["job_id"],
        },
        engine=engine,
    )

    assert tuple(jobs) == ("BRENT", "TFO")
    assert len(engine.connection.calls) == 1
    sql, parameters = engine.connection.calls[0]
    assert "requested.job_id = jobs.job_id" in sql
    assert parameters["products"] == ["BRENT", "TFO"]


def test_only_calibrator_role_can_refresh_bloomberg():
    calibrator = Identity("cal", frozenset({Role.CALIBRATOR}), True, "test")
    publisher = Identity("pub", frozenset({Role.PUBLISHER}), True, "test")
    authorize(calibrator, Permission.REFRESH_BLOOMBERG)
    try:
        authorize(publisher, Permission.REFRESH_BLOOMBERG)
    except AuthorizationError:
        pass
    else:
        raise AssertionError("publisher must not receive calibrator refresh permission")


def test_snapshot_dropdown_uses_exact_ids_and_prioritizes_completed_intraday(monkeypatch):
    snapshots = pd.DataFrame(
        [
            {
                "snapshot_id": "intraday-id",
                "business_date": date(2026, 8, 10),
                "observed_at": "2026-08-10T08:30:00Z",
                "snapshot_kind": "INTRADAY",
            },
            {
                "snapshot_id": "settlement-id",
                "business_date": date(2026, 8, 7),
                "observed_at": "2026-08-07T18:00:00Z",
                "snapshot_kind": "SETTLEMENT",
            },
        ]
    )
    monkeypatch.setattr(
        history, "load_available_snapshots", lambda _product: snapshots
    )
    options, selected = history.update_history_dates(
        0,
        {"result_snapshot_id": "intraday-id"},
        "BRENT",
        "settlement-id",
    )
    assert [option["value"] for option in options] == [
        "intraday-id",
        "settlement-id",
    ]
    assert options[0]["label"] == "Intraday 12:30 GST"
    assert selected == "intraday-id"


def test_settlement_completion_reloads_and_selects_its_result_snapshot(monkeypatch):
    snapshots = pd.DataFrame(
        [
            {
                "snapshot_id": "new-settlement-id",
                "business_date": date(2026, 8, 14),
                "observed_at": "2026-08-17T08:30:00Z",
                "snapshot_kind": "SETTLEMENT",
            },
            {
                "snapshot_id": "old-settlement-id",
                "business_date": date(2026, 8, 13),
                "observed_at": "2026-08-16T18:00:00Z",
                "snapshot_kind": "SETTLEMENT",
            },
        ]
    )
    monkeypatch.setattr(
        history, "load_available_snapshots", lambda _product: snapshots
    )

    options, selected = history.update_history_dates(
        0,
        {
            "request_kind": gateway.SETTLEMENT_REQUEST_KIND,
            "result_snapshot_id": "new-settlement-id",
        },
        "BRENT",
        "old-settlement-id",
    )

    assert options[0] == {"label": "14 Aug 2026", "value": "new-settlement-id"}
    assert selected == "new-settlement-id"


def test_refresh_poll_reports_fresh_reuse_and_selects_reused_snapshot(monkeypatch):
    job = SimpleNamespace(
        job_id="job-id",
        status="succeeded",
        result_snapshot_id="snapshot-id",
        result_status="fresh_reuse",
        updated_at="2026-08-10T08:30:15Z",
        as_dict=lambda: {"job_id": "job-id", "status": "succeeded"},
    )
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: False)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history, "get_refresh_jobs", lambda _job_ids: {"BRENT": job}
    )

    result = history.manage_bloomberg_refresh(
        1,
        0,
        1,
        0,
        "BRENT",
        {"job_id": "job-id"},
        {"result_snapshot_id": "previous-snapshot"},
    )
    assert result[1]["BRENT"]["result_snapshot_id"] == "snapshot-id"
    assert result[1]["BRENT"]["result_status"] == "fresh_reuse"
    assert result[2:5] == (True, False, True)
    assert result[5] == (
        "Fresh complete Bloomberg snapshot reused—no new Bloomberg request was needed."
    )


def test_capacity_failure_preserves_displayed_snapshot_and_recovers_polling(monkeypatch):
    completion = {"result_snapshot_id": "displayed-snapshot"}
    job = SimpleNamespace(
        status="failed",
        metrics={"failure_category": "daily_capacity_reached"},
        last_error="redacted provider error",
        as_dict=lambda: {"job_id": "job-id", "status": "failed"},
    )
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: False)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history, "get_refresh_jobs", lambda _job_ids: {"BRENT": job}
    )

    result = history.manage_bloomberg_refresh(
        1,
        0,
        2,
        0,
        "BRENT",
        {"job_id": "job-id"},
        completion,
    )
    assert result[1] is history.no_update
    assert result[2:5] == (True, False, True)
    assert result[5] == (
        "Bloomberg daily request capacity has been reached. The displayed snapshot "
        "is unchanged; refresh again after Bloomberg resets the entitlement."
    )
    assert "redacted provider error" not in result[5]


def test_settlement_click_uses_shared_job_store_and_disables_both_buttons(monkeypatch):
    submitted = []
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(
        history,
        "_authorize_refresh",
        lambda: SimpleNamespace(subject="cal"),
    )
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(
            triggered_id="brent-vol-history-settlement-refresh-button"
        ),
    )

    def _submit(requested_by, *, products):
        submitted.append((requested_by, products))
        return {
            product: (
                SimpleNamespace(
                    as_dict=lambda product=product: {
                        "job_id": f"settlement-job-{product}",
                        "product": product,
                        "request_kind": gateway.SETTLEMENT_REQUEST_KIND,
                        "status": "queued",
                    }
                ),
                True,
            )
            for product in products
        }

    monkeypatch.setattr(history, "submit_settlement_refresh_jobs", _submit)
    result = history.manage_bloomberg_refresh(0, 1, 0, 0, "BRENT", None, None)

    assert submitted == [("cal", ("BRENT", "TFO", "ON", "LNE"))]
    assert set(result[0]) == {"BRENT", "TFO", "ON", "LNE"}
    assert result[0]["BRENT"]["job_id"] == "settlement-job-BRENT"
    assert result[1] == {}
    assert result[2:5] == (False, True, True)
    assert result[5] == "Settlement refresh queued for BRENT, TFO, ON, and LNE."


def test_settlement_click_surfaces_safe_batch_preflight_rejection(monkeypatch):
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(
        history,
        "_authorize_refresh",
        lambda: SimpleNamespace(subject="cal"),
    )
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(
            triggered_id="brent-vol-history-settlement-refresh-button"
        ),
    )
    monkeypatch.setattr(
        history,
        "submit_settlement_refresh_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            gateway.SettlementRefreshBatchError(
                "An intraday refresh is already active for TFO."
            )
        ),
    )

    result = history.manage_bloomberg_refresh(0, 1, 0, 0, "BRENT", None, None)

    assert result[0] == {}
    assert result[2:5] == (True, False, False)
    assert result[5] == "An intraday refresh is already active for TFO."
    assert result[6].endswith("brent-vol-history-refresh-status-danger")


def test_settlement_completion_selects_result_and_reports_pending_oi(monkeypatch):
    job = SimpleNamespace(
        job_id="settlement-job",
        request_kind=gateway.SETTLEMENT_REQUEST_KIND,
        status="succeeded",
        result_snapshot_id="settlement-snapshot",
        result_status="refreshed",
        metrics={
            "completed_dates": ["2026-08-12", "2026-08-13", "2026-08-14"],
            "oi_pending_dates": ["2026-08-14"],
        },
        updated_at="2026-08-17T08:30:15Z",
        as_dict=lambda: {
            "job_id": "settlement-job",
            "request_kind": gateway.SETTLEMENT_REQUEST_KIND,
            "status": "succeeded",
        },
    )
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history, "get_refresh_jobs", lambda _job_ids: {"BRENT": job}
    )

    result = history.manage_bloomberg_refresh(
        0,
        1,
        3,
        0,
        "BRENT",
        {"job_id": "settlement-job", "request_kind": "settlement_refresh"},
        {"result_snapshot_id": "previous-snapshot"},
    )

    assert result[1]["BRENT"]["result_snapshot_id"] == "settlement-snapshot"
    assert result[1]["BRENT"]["request_kind"] == gateway.SETTLEMENT_REQUEST_KIND
    assert result[2:5] == (True, False, False)
    assert result[5] == (
        "Settlements refreshed through 14 Aug · OI pending for 14 Aug."
    )


def _page_job(
    product,
    status,
    *,
    stage="queued",
    result_status=None,
    metrics=None,
):
    payload = {
        "job_id": f"job-{product}",
        "product": product,
        "business_date": "2026-08-17",
        "status": status,
        "stage": stage,
        "requested_by": "cal",
        "request_kind": gateway.SETTLEMENT_REQUEST_KIND,
        "result_snapshot_id": (
            f"snapshot-{product}" if status == "succeeded" else None
        ),
        "result_status": result_status,
        "previous_snapshot_id": None,
        "metrics": dict(metrics or {}),
        "last_error": "provider failure" if status == "failed" else None,
        "created_at": "2026-08-17T08:30:00Z",
        "updated_at": "2026-08-17T08:30:15Z",
    }
    return SimpleNamespace(**payload, as_dict=lambda: dict(payload))


def test_settlement_batch_poll_reports_every_product_and_keeps_polling(monkeypatch):
    jobs = {
        "BRENT": _page_job("BRENT", "succeeded", result_status="refreshed"),
        "TFO": _page_job("TFO", "failed"),
        "ON": _page_job("ON", "queued"),
        "LNE": _page_job("LNE", "running", stage="settlement_history"),
    }
    calls = []
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )

    def _get_jobs(job_ids):
        calls.append(dict(job_ids))
        return jobs

    monkeypatch.setattr(history, "get_refresh_jobs", _get_jobs)
    active = {product: job.as_dict() for product, job in jobs.items()}

    result = history.manage_bloomberg_refresh(
        0,
        1,
        3,
        0,
        "BRENT",
        active,
        {"TFO": {"result_snapshot_id": "old-tfo"}},
    )

    assert len(calls) == 1
    assert set(calls[0]) == {"BRENT", "TFO", "ON", "LNE"}
    assert result[1]["BRENT"]["result_snapshot_id"] == "snapshot-BRENT"
    assert result[1]["TFO"]["result_snapshot_id"] == "old-tfo"
    assert result[2:5] == (False, True, True)
    assert result[5].startswith("Settlements 2/4 finished")
    assert all(f"{product}:" in result[5] for product in jobs)
    assert result[6].endswith("brent-vol-history-refresh-status-warning")


def test_settlement_batch_is_danger_only_when_every_product_failed():
    jobs = {
        product: _page_job(product, "failed")
        for product in gateway.SETTLEMENT_REFRESH_PRODUCTS
    }

    message, status_class = history._settlement_batch_status(jobs)

    assert message.startswith("Settlements 4/4 finished")
    assert status_class.endswith("brent-vol-history-refresh-status-danger")


def test_nonselected_settlement_completion_does_not_trigger_page_reload(monkeypatch):
    jobs = {
        "BRENT": _page_job("BRENT", "running", stage="settlement_history"),
        "TFO": _page_job("TFO", "succeeded", result_status="refreshed"),
        "ON": _page_job("ON", "running", stage="queued"),
        "LNE": _page_job("LNE", "running", stage="queued"),
    }
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(history, "get_refresh_jobs", lambda _job_ids: jobs)
    active = {
        product: {**job.as_dict(), "status": "queued"}
        for product, job in jobs.items()
    }

    result = history.manage_bloomberg_refresh(
        0,
        1,
        3,
        0,
        "BRENT",
        active,
        {"BRENT": {"result_snapshot_id": "displayed-brent"}},
    )

    assert result[0] is not history.no_update
    assert result[1] is history.no_update
    assert result[2] is False


def test_selecting_completed_product_emits_only_its_completion(monkeypatch):
    jobs = {
        product: _page_job(product, "succeeded", result_status="refreshed")
        for product in gateway.SETTLEMENT_REFRESH_PRODUCTS
    }
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-product"),
    )
    monkeypatch.setattr(history, "get_refresh_jobs", lambda _job_ids: jobs)

    result = history.manage_bloomberg_refresh(
        0,
        1,
        4,
        0,
        "TFO",
        {product: job.as_dict() for product, job in jobs.items()},
        {"BRENT": {"result_snapshot_id": "snapshot-BRENT"}},
    )

    assert result[1]["TFO"]["result_snapshot_id"] == "snapshot-TFO"
    assert set(result[1]) == {"BRENT", "TFO"}
    assert result[2] is True
    assert result[5].startswith("Settlements 4/4 finished")


def test_intraday_after_completed_batch_clears_stale_settlement_jobs(monkeypatch):
    completed_settlements = {
        product: _page_job(product, "succeeded", result_status="refreshed")
        for product in gateway.SETTLEMENT_REFRESH_PRODUCTS
    }
    intraday_payload = {
        "job_id": "intraday-BRENT",
        "product": "BRENT",
        "status": "queued",
        "stage": "queued",
        "request_kind": gateway.INTRADAY_REQUEST_KIND,
        "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    intraday_job = SimpleNamespace(
        **intraday_payload,
        as_dict=lambda: dict(intraday_payload),
    )
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(
        history,
        "_authorize_refresh",
        lambda: SimpleNamespace(subject="cal"),
    )
    monkeypatch.setattr(
        history,
        "submit_refresh_job",
        lambda *_args, **_kwargs: (intraday_job, True),
    )
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-button"),
    )
    completions = {
        product: {"result_snapshot_id": f"snapshot-{product}"}
        for product in gateway.SETTLEMENT_REFRESH_PRODUCTS
    }

    submitted = history.manage_bloomberg_refresh(
        1,
        1,
        4,
        0,
        "BRENT",
        {
            product: job.as_dict()
            for product, job in completed_settlements.items()
        },
        completions,
    )

    assert set(submitted[0]) == {"BRENT"}
    assert submitted[1] == completions

    running_payload = {
        **intraday_payload,
        "status": "running",
        "stage": "market_data",
    }
    running_job = SimpleNamespace(
        **running_payload,
        result_status=None,
        metrics={},
        last_error=None,
        as_dict=lambda: dict(running_payload),
    )
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history,
        "get_refresh_jobs",
        lambda _job_ids: {"BRENT": running_job},
    )

    polled = history.manage_bloomberg_refresh(
        1,
        1,
        5,
        0,
        "BRENT",
        submitted[0],
        submitted[1],
    )

    assert polled[5] == "Fetching market data…"
    assert not polled[5].startswith("Settlements")
    assert polled[2] is False


def test_missing_batch_job_becomes_terminal_failure_without_losing_snapshot(
    monkeypatch,
):
    jobs = {
        product: _page_job(product, "succeeded", result_status="refreshed")
        for product in gateway.SETTLEMENT_REFRESH_PRODUCTS
    }
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history,
        "get_refresh_jobs",
        lambda _job_ids: {
            product: job for product, job in jobs.items() if product != "TFO"
        },
    )
    prior_completion = {
        "TFO": {
            "job_id": "old-tfo-job",
            "result_snapshot_id": "old-tfo-snapshot",
        }
    }

    result = history.manage_bloomberg_refresh(
        0,
        1,
        4,
        0,
        "BRENT",
        {product: job.as_dict() for product, job in jobs.items()},
        prior_completion,
    )

    assert result[0]["TFO"]["status"] == "failed"
    assert result[1]["TFO"]["result_snapshot_id"] == "old-tfo-snapshot"
    assert result[2] is True
    assert "TFO: failed" in result[5]


def test_offline_worker_disables_refresh_and_does_not_submit(monkeypatch):
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(
        history,
        "_authorize_refresh",
        lambda: SimpleNamespace(subject="cal"),
    )
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-button"),
    )
    monkeypatch.setattr(
        history,
        "get_worker_readiness",
        lambda product: _worker_readiness(
            product, ready=False, reason="registry_unavailable"
        ),
    )
    monkeypatch.setattr(
        history,
        "submit_refresh_job",
        lambda *_args, **_kwargs: pytest.fail("offline worker must not queue a job"),
    )

    result = history.manage_bloomberg_refresh(1, 0, 0, 0, "BRENT", None, None)

    assert result[0] == {}
    assert result[2:5] == (True, True, True)
    assert result[5] == (
        "Bloomberg worker status is unavailable. Apply migration 010 and start "
        "the worker."
    )


def test_worker_health_poll_reenables_refresh_when_heartbeat_returns(monkeypatch):
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-worker-poll"),
    )

    result = history.manage_bloomberg_refresh(0, 0, 0, 4, "TFO", None, None)

    assert result[0] is history.no_update
    assert result[1] is history.no_update
    assert result[2:5] == (True, False, False)
    assert result[5] == "Bloomberg TFO worker ready."


def test_active_poll_does_not_reemit_unchanged_job_or_completion(monkeypatch):
    job_payload = {
        "job_id": "queued-job",
        "status": "queued",
        "request_kind": gateway.INTRADAY_REQUEST_KIND,
        "created_at": "2026-08-24T12:00:00Z",
    }
    completion = {
        "result_snapshot_id": "displayed-snapshot",
        "request_kind": gateway.INTRADAY_REQUEST_KIND,
    }
    job = SimpleNamespace(
        job_id="queued-job",
        status="queued",
        stage="queued",
        request_kind=gateway.INTRADAY_REQUEST_KIND,
        created_at="2026-08-24T12:00:00Z",
        as_dict=lambda: dict(job_payload),
    )
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history, "get_refresh_jobs", lambda _job_ids: {"BRENT": job}
    )

    result = history.manage_bloomberg_refresh(
        0,
        0,
        1,
        0,
        "BRENT",
        {"BRENT": job_payload},
        {"BRENT": completion},
    )

    assert result[0] is history.no_update
    assert result[1] is history.no_update
    assert result[2] is False


def test_queued_job_over_30_seconds_reports_ready_worker_as_busy(monkeypatch):
    job = SimpleNamespace(
        job_id="queued-job",
        status="queued",
        stage="queued",
        request_kind=gateway.INTRADAY_REQUEST_KIND,
        created_at="2000-01-01T00:00:00Z",
        as_dict=lambda: {
            "job_id": "queued-job",
            "status": "queued",
            "request_kind": gateway.INTRADAY_REQUEST_KIND,
            "created_at": "2000-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history, "get_refresh_jobs", lambda _job_ids: {"BRENT": job}
    )

    result = history.manage_bloomberg_refresh(
        0,
        0,
        31,
        3,
        "BRENT",
        {"job_id": "queued-job"},
        None,
    )

    assert result[0]["BRENT"]["job_id"] == "queued-job"
    assert result[2:5] == (False, True, True)
    assert result[5] == (
        "Bloomberg Brent worker is online—this request is queued behind another "
        "refresh. The page will keep monitoring it."
    )
    assert result[6].endswith("brent-vol-history-refresh-status-active")


def test_fresh_queued_job_briefly_reports_waiting_for_ready_worker(monkeypatch):
    created_at = pd.Timestamp.now(tz="UTC").isoformat()
    job = SimpleNamespace(
        job_id="queued-job",
        status="queued",
        stage="queued",
        request_kind=gateway.INTRADAY_REQUEST_KIND,
        created_at=created_at,
        as_dict=lambda: {
            "job_id": "queued-job",
            "status": "queued",
            "request_kind": gateway.INTRADAY_REQUEST_KIND,
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: False)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history, "get_refresh_jobs", lambda _job_ids: {"BRENT": job}
    )

    result = history.manage_bloomberg_refresh(
        0,
        0,
        1,
        0,
        "BRENT",
        {"job_id": "queued-job"},
        None,
    )

    assert result[2:5] == (False, True, True)
    assert result[5] == "Waiting for Bloomberg worker…"


def test_offline_queued_job_remains_under_active_polling(monkeypatch):
    created_at = pd.Timestamp.now(tz="UTC").isoformat()
    job = SimpleNamespace(
        job_id="queued-job",
        status="queued",
        stage="queued",
        request_kind=gateway.INTRADAY_REQUEST_KIND,
        created_at=created_at,
        as_dict=lambda: {
            "job_id": "queued-job",
            "status": "queued",
            "request_kind": gateway.INTRADAY_REQUEST_KIND,
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _product: True)
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(
        history,
        "ctx",
        SimpleNamespace(triggered_id="brent-vol-history-refresh-poll"),
    )
    monkeypatch.setattr(
        history, "get_refresh_jobs", lambda _job_ids: {"BRENT": job}
    )
    monkeypatch.setattr(
        history,
        "get_worker_readiness_many",
        lambda products: {
            product: _worker_readiness(
                product, ready=False, reason="stale_heartbeat"
            )
            for product in products
        },
    )

    result = history.manage_bloomberg_refresh(
        0,
        0,
        1,
        0,
        "BRENT",
        {"job_id": "queued-job"},
        None,
    )

    assert result[0]["BRENT"]["job_id"] == "queued-job"
    assert result[2:5] == (False, True, True)
    assert "worker is offline" in result[5]
    assert "no heartbeat in 30 seconds" in result[5]


def test_settlement_current_and_progress_messages_are_concise():
    current = SimpleNamespace(
        result_status="noop",
        metrics={"latest_settlement_date": "2026-08-14"},
    )
    saving = SimpleNamespace(
        stage="settlement_persistence",
        metrics={
            "planned_dates": ["2026-08-12", "2026-08-13", "2026-08-14"],
            "completed_dates": ["2026-08-12", "2026-08-13"],
        },
    )
    assert history._settlement_success_message(current) == (
        "Settlements already current through 14 Aug."
    )
    assert history._refresh_stage_label(
        saving, gateway.SETTLEMENT_REQUEST_KIND
    ) == "Saving settlements (2/3)"


def test_partial_settlement_message_reports_only_persisted_through_date():
    partial = SimpleNamespace(
        result_status="partial",
        metrics={
            "latest_settlement_date": "2026-08-13",
            "failed_dates": ["2026-08-14"],
            "oi_pending_dates": ["2026-08-13"],
        },
    )
    assert history._settlement_success_message(partial) == (
        "Settlements partially refreshed through 13 Aug · 14 Aug unavailable · "
        "OI pending for 13 Aug."
    )


def _calibrated_catalog(publication_id):
    return pd.DataFrame(
        [
            {
                "publication_id": publication_id,
                "run_id": "run-1",
                "commodity": "BRENT",
                "publication_cob_date": "2026-08-14",
                "published_at": "2026-08-14T18:00:00Z",
                "published_by": "cal",
            }
        ]
    )


def _calibrated_points(volatility=0.30):
    return pd.DataFrame(
        [
            {
                "contract_date": "2026-12-01",
                "option_expiration_date": "2026-10-25",
                "strike": 75.0,
                "delta": 0.25,
                "put_call": "C",
                "volatility": volatility,
                "forward_value": 78.0,
                "source_name": "exchange",
                "calibration_basis": "settlement",
                "surface_region": "market",
                "blend_classification": "observed",
                "calibration_method": "spline",
                "calibration_policy_version": "v1",
                "input_fingerprint": "fingerprint",
                "created_at": "2026-08-14T18:00:00Z",
            }
        ]
    )


def test_calibrated_points_cache_is_bounded_copied_and_publication_keyed(monkeypatch):
    history._cached_calibrated_surface_points.cache_clear()
    publication_ids = iter(["publication-1", "publication-1", "publication-2"])
    calls = {"catalog": 0, "points": 0}
    engine = object()
    monkeypatch.setattr(history, "get_database_engine", lambda **_kwargs: engine)

    def _read_sql(query, _engine, params):
        sql = str(query)
        if history.CALIBRATED_PUBLICATION_TABLE in sql:
            calls["catalog"] += 1
            return _calibrated_catalog(next(publication_ids))
        calls["points"] += 1
        assert params["contract_dates"] == (date(2026, 12, 1),)
        return _calibrated_points()

    monkeypatch.setattr(history.pd, "read_sql", _read_sql)

    first = history.load_latest_calibrated_surface(
        "2026-08-14",
        ["2026-12-01", "2026-12-01"],
    )
    first.loc[first.index[0], "volatility"] = 9.0
    second = history.load_latest_calibrated_surface(
        "2026-08-14",
        ["2026-12-01"],
    )
    third = history.load_latest_calibrated_surface(
        "2026-08-14",
        ["2026-12-01"],
    )

    assert calls == {"catalog": 3, "points": 2}
    assert second.iloc[0]["volatility"] == pytest.approx(0.30)
    assert history.calibrated_publication_metadata(second)["publication_id"] == (
        "publication-1"
    )
    assert history.calibrated_publication_metadata(third)["publication_id"] == (
        "publication-2"
    )
    assert history._cached_calibrated_surface_points.cache_info().maxsize == 8


def test_calibrated_cache_preserves_invalid_points_status(monkeypatch):
    reads = iter([_calibrated_catalog("publication-1"), _calibrated_points(-1.0)])
    monkeypatch.setattr(history.pd, "read_sql", lambda *_args, **_kwargs: next(reads))

    surface = history.load_latest_calibrated_surface(
        "2026-08-14",
        ["2026-12-01"],
        engine=object(),
    )

    assert surface.empty
    assert surface.attrs["publication_status"] == "invalid_points"


def _intraday_chain() -> pd.DataFrame:
    rows = []
    for put_call, strike, volume, delta, oi in (
        ("C", 75.0, 12.0, 4.0, 100.0),
        ("P", 75.0, 8.0, 0.0, 80.0),
        ("C", 80.0, 5.0, np.nan, 60.0),
    ):
        rows.append(
            {
                "business_date": date(2026, 8, 10),
                "observed_at": "2026-08-10T08:30:00Z",
                "underlying_contract_month": date(2026, 12, 1),
                "option_expiration_date": date(2026, 10, 25),
                "put_call": put_call,
                "strike": strike,
                "last_price": 3.5,
                "option_bid": 3.4,
                "option_mid": 3.5,
                "option_ask": 3.6,
                "option_spread": 0.2,
                "option_spread_pct": 0.2 / 3.5,
                "underlying_bid": 77.99,
                "underlying_mid": 78.0,
                "underlying_ask": 78.01,
                "underlying_spread": 0.02,
                "quote_capture_skew_ms": 240,
                "executable_iv_bid": 0.29,
                "executable_iv_mid": 0.30,
                "executable_iv_ask": 0.31,
                "executable_iv_status": "resolved",
                "executable_iv_exclusion_reason": None,
                "settlement_price": None,
                "effective_price": 3.5,
                "underlying_price": 78.0,
                "volume": volume,
                "volume_delta": delta,
                "open_interest": oi,
                "implied_volatility": 0.30,
                "iv_status": "resolved",
                "last_trade_price": 3.55 if put_call == "C" and strike == 75.0 else None,
                "last_trade_at": (
                    "2026-08-10T08:29:30Z"
                    if put_call == "C" and strike == 75.0
                    else None
                ),
                "last_trade_underlying_price": (
                    78.0 if put_call == "C" and strike == 75.0 else None
                ),
                "last_trade_underlying_source": (
                    "QUOTE_MID" if put_call == "C" and strike == 75.0 else None
                ),
                "last_trade_match_lag_ms": (
                    120 if put_call == "C" and strike == 75.0 else None
                ),
                "last_trade_condition_codes": None,
                "last_trade_iv": 0.305 if put_call == "C" and strike == 75.0 else None,
                "last_trade_iv_status": (
                    "resolved" if put_call == "C" and strike == 75.0 else "not_applicable"
                ),
                "last_trade_match_source_snapshot_id": None,
                "snapshot_kind": "INTRADAY",
            }
        )
    return pd.DataFrame(rows)


def _trade_tape() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "business_date": date(2026, 8, 10),
                "option_security": "COZ6C 75 Comdty",
                "underlying_contract_month": pd.Timestamp("2026-12-01"),
                "option_expiration_date": pd.Timestamp("2026-10-25"),
                "put_call": "C",
                "strike": 75.0,
                "trade_at": pd.Timestamp("2026-08-10T06:00:00Z"),
                "trade_price": 3.4,
                "trade_size": 5.0,
                "condition_codes": None,
                "future_match_price": 78.0,
                "future_match_source": "QUOTE_MID",
                "future_match_lag_ms": 100,
                "trade_iv": 0.30,
                "trade_iv_status": "resolved",
                "trade_iv_exclusion_reason": None,
                "event_fingerprint": "a" * 64,
                "occurrence_ordinal": 1,
                "cutoff_at": pd.Timestamp("2026-08-10T08:30:00Z"),
                "coverage_status": "complete",
            },
            {
                "business_date": date(2026, 8, 10),
                "option_security": "COZ6P 75 Comdty",
                "underlying_contract_month": pd.Timestamp("2026-12-01"),
                "option_expiration_date": pd.Timestamp("2026-10-25"),
                "put_call": "P",
                "strike": 75.0,
                "trade_at": pd.Timestamp("2026-08-10T08:29:00Z"),
                "trade_price": 2.1,
                "trade_size": 20.0,
                "condition_codes": None,
                "future_match_price": 78.0,
                "future_match_source": "PREVAILING_MID",
                "future_match_lag_ms": 800,
                "trade_iv": 0.31,
                "trade_iv_status": "resolved",
                "trade_iv_exclusion_reason": None,
                "event_fingerprint": "b" * 64,
                "occurrence_ordinal": 1,
                "cutoff_at": pd.Timestamp("2026-08-10T08:30:00Z"),
                "coverage_status": "complete",
            },
        ]
    )


def test_intraday_chart_keeps_oi_behind_volume_and_highlights_new_volume():
    chain = _intraday_chain()
    prepared = pd.DataFrame(
        {
            "expiry": [pd.Timestamp("2026-12-01")],
            "strike": [75.0],
            "iv": [0.30],
            "volume": [12.0],
            "open_interest": [100.0],
            "calibration_eligible": [True],
            "exclusion_reason": [""],
        }
    )
    figure = history.build_expiry_figure(
        chain,
        prepared,
        pd.DataFrame(),
        pd.Timestamp("2026-12-01"),
    )
    names = [trace.name for trace in figure.data]
    assert names[:4] == [
        "Open interest · calls",
        "Open interest · puts",
        "Volume · calls",
        "Volume · puts",
    ]
    assert "Executable mid IV · Calls" in names
    assert "Executable IV band · Calls" in names
    assert "New matched trade IV" in names
    assert figure.data[names.index("Executable mid IV · Calls")].mode == "lines"
    assert figure.data[names.index("Executable mid IV · Puts")].mode == "lines"
    call_volume = figure.data[names.index("Volume · calls")]
    assert "New <b>" in call_volume.hovertemplate
    assert "Underlying" in call_volume.hovertemplate
    assert "Volume / OI" in call_volume.hovertemplate
    assert set(call_volume.customdata[:, 4]) == {78.0}
    executable_band = figure.data[names.index("Executable IV band · Calls")]
    executable_mid = figure.data[names.index("Executable mid IV · Calls")]
    matched_trade = figure.data[names.index("New matched trade IV")]
    assert executable_band.hoverinfo == "skip"
    assert "Underlying B/M/A" in executable_mid.hovertemplate
    assert "Volume / OI" in executable_mid.hovertemplate
    assert "Underlying" in matched_trade.hovertemplate
    assert "Volume / OI" in matched_trade.hovertemplate
    assert max(call_volume.marker.line.width) == 2.2
    assert figure.layout.barmode == "overlay"


def test_intraday_delta_axis_uses_trade_time_future_for_trade_marker():
    chain = _intraday_chain()
    trade_row = chain["last_trade_iv_status"].eq("resolved")
    chain.loc[trade_row, "last_trade_underlying_price"] = 90.0
    prepared = pd.DataFrame()
    figure = history.build_expiry_figure(
        chain,
        prepared,
        pd.DataFrame(),
        pd.Timestamp("2026-12-01"),
        x_axis="delta",
    )
    traces = {trace.name: trace for trace in figure.data}
    executable_x = float(traces["Executable mid IV · Calls"].x[0])
    trade_x = float(traces["New matched trade IV"].x[0])
    assert trade_x != executable_x
    assert 0.0 <= trade_x <= 1.0
    assert "Δ %{x:.3f}" in traces["New matched trade IV"].hovertemplate


def test_trade_window_filters_only_exact_trade_overlays_and_rows():
    chain = _intraday_chain()
    tape = _trade_tape()
    figure = history.build_expiry_figure(
        chain,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.Timestamp("2026-12-01"),
        trade_tape=tape,
    )
    traces = {trace.name: trace for trace in figure.data}
    assert len(traces["Trade-time IV · Calls"].x) == 1
    assert len(traces["Trade-time IV · Puts"].x) == 1
    assert traces["Trade-time IV · Calls"].marker.symbol[0] == "circle"
    assert traces["Trade-time IV · Puts"].marker.symbol[0] == "circle-open"
    assert "Underlying" in traces["Trade-time IV · Calls"].hovertemplate
    assert float(traces["Trade-time IV · Calls"].customdata[0][4]) == 78.0
    assert traces["Trade-time IV · Calls"].customdata[0][5] == "Exact mid"
    assert traces["Trade-time IV · Puts"].customdata[0][5] == "Prevailing mid"
    assert figure.layout.yaxis.range is not None

    recent = history.filter_trade_window(tape, 12 * 3600 + 20 * 60)
    assert recent["option_security"].tolist() == ["COZ6P 75 Comdty"]
    rows = history._trade_tape_rows(recent, "2026-12-01", 0)
    assert len(rows) == 1
    assert rows[0]["trade_size"] == 20.0
    assert rows[0]["future_match_source"] == "Prevailing mid"


def test_delta_axis_does_not_use_untimed_last_price_as_an_iv_reference():
    chain = _intraday_chain()
    chain["executable_iv_status"] = "unavailable"
    chain[["executable_iv_bid", "executable_iv_mid", "executable_iv_ask"]] = np.nan
    figure = history.build_expiry_figure(
        chain,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.Timestamp("2026-12-01"),
        x_axis="delta",
    )
    assert not any(trace.type == "bar" for trace in figure.data)
    assert not any(trace.name == "Indicative last-price IV" for trace in figure.data)
    assert any(
        "activity strikes unavailable on Delta" in annotation.text
        for annotation in figure.layout.annotations
    )


def test_layout_orders_primary_and_trade_controls_in_sticky_toolbar():
    header = history.layout.children[1]
    toolbar = header.children[0]
    assert "brent-vol-history-sticky-filter-bar" in header.className
    assert toolbar.children[0].children[1].id == "brent-vol-history-product"
    assert [
        item["value"] for item in toolbar.children[0].children[1].options
    ] == ["BRENT", "TFO", "ON", "LNE", "JKM"]
    assert gateway.SUPPORTED_PRODUCTS == frozenset({"BRENT", "TFO", "ON", "LNE"})
    assert toolbar.children[1].children[1].id == "brent-vol-history-x-axis"
    refresh_row = toolbar.children[2].children[1]
    assert refresh_row.className == "brent-vol-history-refresh-row"
    assert refresh_row.children[0].id == "brent-vol-history-refresh-button"
    assert refresh_row.children[1].id == (
        "brent-vol-history-settlement-refresh-button"
    )
    assert refresh_row.children[1].children == "Refresh settlements"
    assert refresh_row.children[2].id == "brent-vol-history-refresh-status"
    assert toolbar.children[3].children[1].id == "brent-vol-history-date"
    assert toolbar.children[4].children[1].children.id == "brent-vol-history-trade-start"
    assert toolbar.children[5].children[1].children[0].id == "brent-vol-history-trade-all"
    assert toolbar.children[5].children[1].children[-2].id == "brent-vol-history-trade-latest"
    assert (
        toolbar.children[5].children[1].children[-1].id
        == "brent-vol-history-market-data-status"
    )
