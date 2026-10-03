import pandas as pd
from dash import no_update

from vol_trades_chart_information import expiry_information, expiry_information_updates
from vol_trades_market_window import history_identity
from vol_trades_quote_ranges import RANGE_LAYER, quote_age_label, range_visible
from vol_trades_workspace.overlays import ice_quote_overlay_points


def test_activity_counts_active_prints_including_unresolved_but_not_audit_rows():
    raw = pd.DataFrame([dict(option_bid=None, option_ask=None, executable_iv_exclusion_reason="missing_two_sided_quote")])
    tape = pd.DataFrame([
        dict(trade_lifecycle="active", is_regular=False, trade_size=50, trade_iv_status="resolved", trade_at="2026-10-02T08:00:00Z"),
        dict(trade_lifecycle="active", is_regular=False, trade_size=140, trade_iv_status="unavailable", trade_at="2026-10-02T09:00:00Z"),
        dict(trade_lifecycle="cancelled", is_regular=True, trade_size=900, trade_iv_status="resolved", trade_at="2026-10-02T10:00:00Z"),
    ])
    info = expiry_information({}, raw, tape)
    assert info["band_label"] == "No bid/ask"
    assert info["summary"] == "2 trades · 190 lots"
    assert info["unresolved_trade_count"] == 1
    assert "latest execution 13:00:00 GST" in info["detail"]
    assert "option bid/ask unavailable (1)" in info["detail"]


def test_unavailable_lots_are_not_a_partial_total_and_missing_iv_not_a_band():
    raw = pd.DataFrame([dict(option_bid=1, option_ask=2, executable_iv_status="resolved", executable_iv_mid=None)])
    tape = pd.DataFrame([dict(trade_lifecycle="active", trade_size=None)])
    info = expiry_information({}, raw, tape)
    assert info["band_label"] == "Quotes filtered"
    assert info["summary"] == "1 trade"
    assert info["active_trade_lots"] is None


def test_present_but_unusable_quotes_keep_the_actual_band_failure():
    for reason, label in [
        ("crossed_quote", "Crossed quotes"),
        ("corresponding future bid/ask is missing or crossed", "No future quote"),
        ("option spread exceeds governed executable-width limit", "Quotes filtered"),
        ("option quote is stale", "Quotes filtered"),
    ]:
        raw = pd.DataFrame([dict(option_bid=1, option_ask=2, executable_iv_exclusion_reason=reason)])
        info = expiry_information({}, raw, None)
        assert info["band_label"] == label
        assert reason in info["detail"] or reason == "crossed_quote"


def test_headers_follow_execution_window_and_snapshot_identity(monkeypatch):
    import vol_trades_data as data
    history = dict(product="TFO", snapshot_kind="INTRADAY", snapshot_id="pinned")
    month = pd.Timestamp("2027-04-01")
    monkeypatch.setattr(data, "load_chain_snapshot", lambda *a, **k: pd.DataFrame([dict(underlying_contract_month=month)]))
    monkeypatch.setattr(data, "load_trade_tape", lambda *a, **k: pd.DataFrame([
        dict(underlying_contract_month=month, trade_lifecycle="active", trade_size=100, trade_at="2026-10-02T08:00:00Z"),
        dict(underlying_contract_month=month, trade_lifecycle="active", trade_size=90, trade_at="2026-10-02T09:50:00Z"),
    ]))
    window = dict(history_key=history_identity(history), start_at="2026-10-02T09:45:00Z", cutoff_at="2026-10-02T10:00:00Z")
    ids = [dict(expiry="2027-04-01")]
    children, _ = expiry_information_updates(history, window, ids)
    assert children[0][1].children == "1 trade · 90 lots"
    assert expiry_information_updates(history, {**window, "history_key": "old"}, ids) == ([no_update], [no_update])
    assert expiry_information_updates(history, window, []) == ([], [])


def test_broker_range_uses_same_event_and_separates_sparse_observations():
    history = dict(product="TFO", business_date="2026-10-02", observed_at="2026-10-02T10:00:00Z")
    row = dict(product_code="TFM", contract_month="2027-05-01", option_type="C", strike=60,
               observed_at="2026-10-02T08:00:00Z", option_expiration_date="2027-04-26", forward=40,
               bid=2, offer=3, bid_implied_volatility=.58, offer_implied_volatility=.62,
               sender_handle="broker", source_channel="chat")
    points, _ = ice_quote_overlay_points([row, {**row, "strike": 70}], history, "2027-05-01", "strike")
    assert points[RANGE_LAYER]["x"] == [60, 60, None, 70, 70, None]
    assert "2h 00m old" in points[RANGE_LAYER]["text"][0]
    assert "Sender broker · chat" in points[RANGE_LAYER]["text"][0]
    one_sided = [{**row, "offer": None}, {**row, "bid": None}]
    assert ice_quote_overlay_points(one_sided, history, "2027-05-01", "strike")[0][RANGE_LAYER]["x"] == []
    crossed = {**row, "bid": 4}
    assert ice_quote_overlay_points([crossed], history, "2027-05-01", "strike")[0][RANGE_LAYER]["x"] == []
    delta, _ = ice_quote_overlay_points([row], history, "2027-05-01", "delta")
    assert delta[RANGE_LAYER]["x"] == [delta["ice-bid"]["x"][0], delta["ice-offer"]["x"][0], None]
    assert not range_visible(["ice-bid"])
    assert range_visible(["ice-bid", "ice-offer"])
    assert quote_age_label(None, history["observed_at"]) == "Age unavailable"
