import pandas as pd

from vol_trades_workspace import grids, trade_tape


def frame():
    base = dict(business_date="2026-10-02", underlying_contract_month=pd.Timestamp("2026-11-01"),
                option_expiration_date="2026-10-27", put_call="C", strike=75., trade_price=5.,
                trade_size=100., trade_iv=.5, trade_iv_status="resolved", trade_iv_exclusion_reason=None,
                future_match_price=70., future_match_source="QUOTE_MID", future_match_lag_ms=100,
                condition_codes="BL", event_fingerprint="id", occurrence_ordinal=1,
                execution_at="2026-10-02T12:00:00Z", reported_at="2026-10-02T12:04:00Z",
                trade_at="2026-10-02T12:00:00Z", trade_lifecycle="active",
                policy_version="tfo-execution-trade-iv-v1")
    return pd.DataFrame([dict(base, option_security="BLOCK", trade_classification="block"),
                         dict(base, option_security="SPLIT", trade_classification="block_split_leg", put_call="P"),
                         dict(base, option_security="CANCEL", trade_lifecycle="cancelled", trade_iv_status="non_regular", trade_iv=None),
                         dict(base, option_security="UNMATCHED", trade_classification="block_split_leg", trade_iv_status="unresolved", trade_iv=None,
                              trade_iv_exclusion_reason="no valid prevailing future mid")])


def test_resolved_special_trades_keep_call_put_markers_and_execution_report_provenance():
    payload = trade_tape.trade_trace_payloads(frame(), pd.Timestamp("2026-11-01"), "strike")
    assert payload["C"]["symbol"] == ["circle"]
    assert payload["P"]["symbol"] == ["circle-open"]
    data = payload["C"]["customdata"][0]
    assert data[3] == "16:00:00 GST" and data[11] == "16:04:00 GST"
    assert data[12] == "Block trade IV"


def test_every_tape_record_keeps_specific_status_and_both_times():
    rows = trade_tape._trade_tape_rows(frame(), "2026-11-01", 0)
    assert len(rows) == 4
    labels = {r["option_security"]: r["trade_iv_label"] for r in rows}
    assert labels == {"BLOCK":"Block trade IV", "SPLIT":"Split-leg IV", "CANCEL":"Cancelled", "UNMATCHED":"Underlying match unavailable"}
    assert rows[0]["execution_time_gst"] == "16:00:00.000"
    assert rows[0]["reported_time_gst"] == "16:04:00.000"
    assert trade_tape.filter_trade_window(frame(), 16*3600+60).empty
    columns = {c["field"] for g in grids.TRADE_TAPE_COLUMN_DEFS for c in g["children"]}
    assert {"execution_time_gst", "reported_time_gst", "trade_iv_label", "trade_type_label"} <= columns
