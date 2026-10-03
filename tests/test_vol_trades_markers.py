import pandas as pd
import pytest
from dash import dcc, html
import plotly.graph_objects as go

from pages import brent_vol_history as history
from vol_trades_market_window import history_identity
from vol_trades_workspace import history_charts, overlays, quote_charts, trade_tape


def test_rebuilt_base_charts_do_not_reuse_the_previous_ice_overlay_generation():
    def cards():
        return [html.Section(dcc.Graph(
            id={"type": "brent-vol-history-expiry-graph", "expiry": "2027-01-01"},
            figure=go.Figure(layout={"uirevision": "pinned-market"}),
        ))]
    first, rebuilt = cards(), cards()
    identity = dict(snapshot_id="pinned", product="TFO", x_axis="strike", publication_id="published")
    old_generation = history_charts._stamp_plot_generation(first, **identity)
    new_generation = history_charts._stamp_plot_generation(rebuilt, **identity)
    assert old_generation != new_generation
    graph = history_charts._plot_card_graphs(rebuilt)[0]
    assert graph.id["generation"] == new_generation
    assert graph.figure.layout.meta["generation"] == new_generation
    assert graph.figure.layout.uirevision == "pinned-market"


def tape():
    base = dict(
        business_date="2026-10-02", underlying_contract_month=pd.Timestamp("2027-01-01"),
        option_expiration_date="2026-12-24", put_call="C", strike=75.0,
        trade_price=5.0, trade_iv=0.65, trade_iv_status="resolved",
        future_match_price=70.0, future_match_source="PREVAILING_MID",
        future_match_lag_ms=100, condition_codes="regular",
    )
    return pd.DataFrame([
        dict(base, option_security=f"TFO-{i}", occurrence_ordinal=i,
             trade_at=f"2026-10-02T{8+i:02d}:00:00Z", trade_size=size,
             put_call="P" if i == 1 else "C")
        for i, size in enumerate((1, 10, 100))
    ])


def test_bloomberg_symbols_follow_option_side_and_retain_price_matching_provenance():
    frame = tape()
    frame.loc[2, "future_match_source"] = "TRADE"
    payload = trade_tape.trade_trace_payloads(frame, pd.Timestamp("2027-01-01"), "strike")
    assert payload["C"]["symbol"] == ["circle", "circle"]
    assert payload["P"]["symbol"] == ["circle-open"]
    assert payload["C"]["size"] == [6, 6]
    assert payload["P"]["size"] == [6]
    assert payload["C"]["x"] == [75, 75]
    assert payload["C"]["y"] == [65, 65]
    assert [row[5] for row in payload["C"]["customdata"]] == ["Prevailing mid", "Future trade"]
    assert "#F97316" not in payload["C"]["line_color"]
    assert payload["C"]["line_width"] == [0.5, 0.5]


def test_missing_trade_size_is_neutral_and_explicit_in_hover_payload():
    frame = tape()
    frame.loc[0, "trade_size"] = float("nan")
    payload = trade_tape.trade_trace_payloads(frame, pd.Timestamp("2027-01-01"), "strike")["C"]
    assert payload["opacity"][0] == 0.55
    assert payload["customdata"][0][9] == "Size unavailable"
    assert trade_tape.trade_trace_payloads(frame.iloc[:0], pd.Timestamp("2027-01-01"), "strike")["C"]["opacity"] == []


def test_market_window_patch_keeps_the_full_snapshot_volume_scale(monkeypatch):
    frame = tape()
    monkeypatch.setattr(history.market_data, "load_trade_tape", lambda *a, **k: frame)
    snapshot = dict(product="TFO", snapshot_id="pinned", snapshot_kind="INTRADAY",
                    business_date="2026-10-02", display_expiries=["2027-01-01"])
    window = dict(history_key=history_identity(snapshot), product="TFO",
                  start_at="2026-10-02T09:30:00Z", cutoff_at="2026-10-02T11:00:00Z")
    figure = dict(layout=dict(meta=dict(expiry="2027-01-01")), data=[
        dict(meta=dict(role="trade-tape", put_call="C")),
        dict(meta=dict(role="trade-tape", put_call="P")),
    ])
    patch, _ = history.update_trade_window(window, None, snapshot, [figure], "strike", 1)
    operations = patch[0].to_plotly_json()["operations"]
    styles = {tuple(op["location"]): op["params"]["value"] for op in operations}
    full = trade_tape.trade_trace_payloads(frame, pd.Timestamp("2027-01-01"), "strike")
    assert styles[("data", 0, "marker", "opacity")] == [full["C"]["opacity"][1]]
    assert styles[("data", 1, "marker", "opacity")] == []


def test_ice_expiry_opacity_is_independent_of_size_and_age():
    base = dict(product_code="TFM", contract_month="2027-01-01", option_type="C",
                strike=75, bid=5, bid_implied_volatility=0.65, valuation_status="valued")
    rows = [dict(base, bid_size=1, observed_at="2026-10-02T01:00:00Z"),
            dict(base, bid_size=100, option_type="P", observed_at="2026-10-02T10:00:00Z")]
    window = dict(product="TFO", start_at="2026-10-02T00:00:00Z", cutoff_at="2026-10-02T11:00:00Z")
    payload, omitted = overlays.ice_quote_overlay_points(rows, dict(product="TFO"), "2027-01-01", "strike", market_window=window)
    assert omitted == 0
    assert payload["ice-bid"]["opacity"] == [0.85, 0.85]
    assert payload["ice-bid"]["symbol"] == ["triangle-down", "triangle-down-open"]
    assert "Quoted size 100" in payload["ice-bid"]["text"][1]


def test_ice_comparison_symbols_use_call_put_and_do_not_enlarge_latest_event():
    base = dict(product_label="TFO", contract_label="Jan-27", option_label="Call",
                option_type="C", strike=75, price_unit_label="EUR/MWh", bid=5, offer=6,
                single_price=5.5, theoretical_price=5.2, bid_iv_pct=65, offer_iv_pct=66,
                single_iv_pct=65.5, our_iv_pct=65.2, bid_iv_edge_pp=-0.2,
                offer_iv_edge_pp=0.8, single_iv_deviation_pp=0.3, forward=70,
                surface_cob_date="2026-10-01", sender_handle="broker", outbound_status="",
                observed_at="2026-10-02T08:00:00Z", bid_size=1, offer_size=10, single_size=50)
    frame = pd.DataFrame([base, dict(base, option_type="P", option_label="Put",
                                    observed_at="2026-10-02T10:00:00Z", bid_size=100)])
    figure = quote_charts.build_all_quotes_figure(frame)
    for trace, symbol in zip(figure.data, ("triangle-down", "triangle-up", "diamond")):
        assert list(trace.marker.symbol) == [symbol, symbol + "-open"]
        assert list(trace.marker.size) == [6, 6]
        assert trace.marker.opacity == pytest.approx(0.85)
    assert figure.data[2].name == "ICE single price"
