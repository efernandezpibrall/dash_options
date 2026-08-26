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


def _job_row(request_kind):
    return {
        "job_id": "00000000-0000-0000-0000-000000000010",
        "product": "BRENT",
        "business_date": date(2026, 8, 17),
        "status": "queued",
        "stage": "queued",
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
    assert ":product = ANY(enabled_products)" in sql
    assert "CURRENT_TIMESTAMP" in sql
    assert "('starting', 'idle', 'running')" in sql
    assert parameters == {"product": "TFO", "freshness_seconds": 30}


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
    engine = _Engine([None, _job_row(active_kind)])

    job, created = gateway.submit_refresh_job(
        "cal",
        product="BRENT",
        engine=engine,
        business_date=date(2026, 8, 17),
        request_kind=request_kind,
    )

    assert created is False
    assert job.request_kind == active_kind
    assert engine.connection.calls[0][1]["request_kind"] == request_kind
    assert engine.connection.calls[1][1]["request_family"] == request_family
    active_sql = engine.connection.calls[1][0]
    assert "request_kind = 'settlement_refresh'" in active_sql
    assert "request_kind IN ('user_refresh', 'daily_tape_seed')" in active_sql


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
        history, "get_refresh_job", lambda _job_id, *, product: job
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
        history, "get_refresh_job", lambda _job_id, *, product: job
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
    job = SimpleNamespace(
        as_dict=lambda: {
            "job_id": "settlement-job",
            "request_kind": gateway.SETTLEMENT_REQUEST_KIND,
            "status": "queued",
        }
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
        "ctx",
        SimpleNamespace(
            triggered_id="brent-vol-history-settlement-refresh-button"
        ),
    )

    def _submit(requested_by, *, product, request_kind):
        submitted.append((requested_by, product, request_kind))
        return job, True

    monkeypatch.setattr(history, "submit_refresh_job", _submit)
    result = history.manage_bloomberg_refresh(0, 1, 0, 0, "BRENT", None, None)

    assert submitted == [("cal", "BRENT", gateway.SETTLEMENT_REQUEST_KIND)]
    assert result[0]["BRENT"]["job_id"] == "settlement-job"
    assert result[1] == {}
    assert result[2:5] == (False, True, True)
    assert result[5] == "Settlement refresh queued."


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
        history, "get_refresh_job", lambda _job_id, *, product: job
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
        history, "get_refresh_job", lambda _job_id, *, product: job
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
        history, "get_refresh_job", lambda _job_id, *, product: job
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
        history, "get_refresh_job", lambda _job_id, *, product: job
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
        history, "get_refresh_job", lambda _job_id, *, product: job
    )
    monkeypatch.setattr(
        history,
        "get_worker_readiness",
        lambda product: _worker_readiness(
            product, ready=False, reason="stale_heartbeat"
        ),
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
                "future_match_source": "TRADE",
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
    assert figure.layout.yaxis.range is not None

    recent = history.filter_trade_window(tape, 12 * 3600 + 20 * 60)
    assert recent["option_security"].tolist() == ["COZ6P 75 Comdty"]
    rows = history._trade_tape_rows(recent, "2026-12-01", 0)
    assert len(rows) == 1
    assert rows[0]["trade_size"] == 20.0


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
