from types import SimpleNamespace

from dash_options.features.vol_trades import page as history


def test_refresh_message_distinguishes_collection_from_iv_and_coverage():
    job = SimpleNamespace(result_status="partial", metrics={
        "active_event_count": 6, "resolved_event_count": 0,
        "coverage_status": "partial", "coverage_reasons": ["volume reference unavailable"],
    })
    message = history._intraday_success_message(job)
    assert "6 active trades collected" in message and "0 IVs resolved" in message
    assert "6 IVs unavailable" in message and "tape coverage partial" in message
    assert "volume reference unavailable" in message
    assert "ready" not in message


def test_tape_context_names_selected_expiry_and_unresolved_reason():
    message = history.update_trade_tape_context([
        {"trade_iv_status": "resolved"},
        {"trade_iv_status": "unresolved", "trade_iv_exclusion_reason": "future quote unavailable"},
    ], "2026-11-01")
    assert "Nov-26" in message and "2 records" in message and "1 IVs resolved" in message
    assert "future quote unavailable" in message
    assert "Dec-26 · 0 records" in history.update_trade_tape_context([], "2026-12-01")


def test_worker_availability_does_not_claim_bloomberg_is_connected():
    status = SimpleNamespace(ready=True, bloomberg_session_status="not_started")
    assert "connects when refresh starts" in history._worker_readiness_message(status, "TFO")
    status.ready = False; status.reason = "worker_not_running"; status.lifecycle_status = "error"
    assert "recovering" in history._worker_readiness_message(status, "TFO")


def test_late_product_readiness_response_cannot_clear_new_refresh_job(monkeypatch):
    ready = SimpleNamespace(ready=True, bloomberg_session_status="not_started")
    monkeypatch.setattr(history, "ctx", SimpleNamespace(triggered_id="brent-vol-history-product"))
    monkeypatch.setattr(history, "_authorize_refresh", lambda: object())
    monkeypatch.setattr(history, "intraday_refresh_enabled", lambda _: True)
    monkeypatch.setattr(history, "settlement_refresh_enabled", lambda _: True)
    monkeypatch.setattr(history, "get_worker_readiness_many", lambda products: {p: ready for p in products})
    result = history.manage_bloomberg_refresh(0, 0, 0, 0, "TFO", {}, {})
    assert result[0] is history.no_update and result[1] is history.no_update
