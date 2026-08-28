from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from dash import html

import index_options
from pages import brent_vol_history as history


def _walk(component):
    yield component
    children = getattr(component, "children", None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if child is not None:
            yield from _walk(child)


def _column_leaves(column_defs):
    leaves = []
    for column in column_defs:
        if "children" in column:
            leaves.extend(_column_leaves(column["children"]))
        else:
            leaves.append(column)
    return leaves


def _chain_frame():
    rows = []
    for security, put_call, strike, settlement, volume, open_interest, iv in (
        ("COZ6P 70 Comdty", "P", 70.0, 1.25, 20, 250, 0.31),
        ("COZ6C 90 Comdty", "C", 90.0, 1.10, 0, 180, 0.29),
        ("COZ6P 50 Comdty", "P", 50.0, None, None, 10, None),
    ):
        rows.append(
            {
                "snapshot_id": "00000000-0000-0000-0000-000000000001",
                "product": "BRENT",
                "business_date": "2026-08-10",
                "observed_at": "2026-08-10T12:00:00Z",
                "discovery_method": "OPT_CHAIN",
                "bloomberg_description": "Brent option",
                "underlying_type": "FUTURE",
                "underlying_security": "COZ6 Comdty",
                "underlying_global_id": "BBG-FUTURE",
                "underlying_contract_month": "2026-12-01",
                "underlying_last_tradeable_date": "2026-10-30",
                "underlying_price": 80.0,
                "option_security": security,
                "option_global_id": f"BBG-{security}",
                "put_call": put_call,
                "strike": strike,
                "option_expiration_date": "2026-10-27",
                "option_last_tradeable_date": "2026-10-27",
                "option_style": "AMERICAN",
                "premium_style": "FUTURES_STYLE",
                "exchange_code": "ICE",
                "currency": "USD",
                "price_unit": "USD/BBL",
                "contract_multiplier": 1000,
                "settlement_price": settlement,
                "volume": volume,
                "open_interest": open_interest,
                "implied_volatility": iv,
                "pricing_model": "american_on_futures_futures_style",
                "pricing_model_version": "american_futures_crr_margin_style_v2",
                "iv_status": "resolved" if iv is not None else "missing_settlement",
                "iv_exclusion_reason": None if iv is not None else "missing settlement",
                "ingested_at": "2026-08-10T12:00:00Z",
                "input_fingerprint": "abc123",
                "source_name": "Bloomberg option-chain settlement",
                "source_revision": "v1",
                "snapshot_metadata": {},
            }
        )
    return history._normalize_chain_frame(pd.DataFrame(rows))


def _published_frame():
    return pd.DataFrame(
        {
            "cob_date": pd.to_datetime(["2026-08-10", "2026-08-10"]),
            "contract_date": pd.to_datetime(["2026-12-01", "2026-12-01"]),
            "option_expiration_date": pd.to_datetime(["2026-10-27", "2026-10-27"]),
            "put_call": ["put", "call"],
            "delta": [0.25, 0.25],
            "volatility": [0.32, 0.28],
            "forward_value": [80.0, 80.0],
            "source_name": ["published", "published"],
        }
    )


def _calibrated_frame():
    frame = pd.DataFrame(
        {
            "contract_date": pd.to_datetime(["2026-12-01"] * 3),
            "option_expiration_date": pd.to_datetime(["2026-10-27"] * 3),
            "strike": [70.0, 80.0, 90.0],
            "delta": [0.75, 0.50, 0.25],
            "put_call": ["C", "C", "C"],
            "volatility": [0.32, 0.30, 0.28],
            "forward_value": [80.0, 80.0, 80.0],
            "source_name": ["TTF PCHIP/Wing"] * 3,
            "calibration_basis": ["observed"] * 3,
            "created_at": pd.to_datetime(["2026-08-25T09:33:19Z"] * 3),
            "publication_id": ["9572ae7f-90ca-4c8f-aecd-fbaff1ed081d"] * 3,
            "run_id": ["1fb16e4f-d1c7-4bc9-9e32-6dc4899ebf9a"] * 3,
            "commodity": ["TTF"] * 3,
            "publication_cob_date": pd.to_datetime(["2026-08-24"] * 3),
            "published_at": pd.to_datetime(["2026-08-25T09:33:19Z"] * 3),
            "published_by": ["publisher"] * 3,
        }
    )
    frame.attrs["publication_status"] = "available"
    frame.attrs["publication_metadata"] = {
        "publication_id": "9572ae7f-90ca-4c8f-aecd-fbaff1ed081d",
        "run_id": "1fb16e4f-d1c7-4bc9-9e32-6dc4899ebf9a",
        "commodity": "TTF",
        "cob_date": pd.Timestamp("2026-08-24"),
        "published_at": pd.Timestamp("2026-08-25T09:33:19Z"),
        "published_by": "publisher",
    }
    return frame


def _intraday_missing_quote_frame():
    frame = _chain_frame().iloc[[0]].copy()
    frame["snapshot_metadata"] = [{"snapshot_kind": "INTRADAY"}]
    frame["business_date"] = pd.Timestamp("2026-08-12")
    frame["observed_at"] = pd.Timestamp("2026-08-12T07:28:55Z")
    frame["underlying_security"] = "COM8 Comdty"
    frame["underlying_contract_month"] = pd.Timestamp("2028-06-01")
    frame["underlying_price"] = 73.795
    frame["underlying_mid"] = 73.795
    frame["option_security"] = "COM8P 60 Comdty"
    frame["put_call"] = "P"
    frame["strike"] = 60.0
    frame["option_expiration_date"] = pd.Timestamp("2028-04-25")
    frame["settlement_price"] = None
    frame["last_price"] = 4.67
    frame["last_trade_date"] = pd.NaT
    frame["option_bid"] = None
    frame["option_ask"] = None
    frame["option_mid"] = None
    frame["volume"] = 500
    frame["open_interest"] = 2_625
    frame["implied_volatility"] = None
    frame["executable_iv_mid"] = None
    frame["executable_iv_status"] = "unavailable"
    frame["executable_iv_exclusion_reason"] = "missing_two_sided_quote"
    return history._normalize_chain_frame(frame)


def _tfo_apr27_intraday_frame():
    base = _chain_frame().iloc[0].to_dict()
    rows = []
    prices = {
        48.0: {"C": 10.0, "P": 9.667},
        49.0: {"C": 9.037, "P": 9.704},
        50.0: {"C": 8.707001, "P": 10.374001},
    }
    for strike, sides in prices.items():
        for put_call, last_price in sides.items():
            rows.append(
                {
                    **base,
                    "product": "TFO",
                    "business_date": "2026-08-24",
                    "observed_at": "2026-08-24T13:18:21Z",
                    "underlying_security": "FJSJ7 Comdty",
                    "pricing_underlying_security": "TZTJ7 Comdty",
                    "underlying_contract_month": "2027-04-01",
                    "underlying_price": 49.7925,
                    "underlying_bid": 49.695,
                    "underlying_mid": 49.7925,
                    "underlying_ask": 49.890,
                    "option_security": f"FJSJ7{put_call} {strike:g} Comdty",
                    "option_global_id": f"BBG-FJSJ7-{put_call}-{strike:g}",
                    "put_call": put_call,
                    "strike": strike,
                    "option_expiration_date": "2027-03-25",
                    "option_style": "EUROPEAN",
                    "currency": "EUR",
                    "price_unit": "EUR/MWH",
                    "settlement_price": None,
                    "last_price": last_price,
                    "last_trade_date": pd.NaT,
                    "intraday_open_interest": 100.0 + strike,
                    "intraday_open_interest_date": "2026-08-24",
                    "option_bid": None,
                    "option_mid": None,
                    "option_ask": None,
                    "executable_iv_bid": None,
                    "executable_iv_mid": None,
                    "executable_iv_ask": None,
                    "executable_iv_status": "unavailable",
                    "implied_volatility": None,
                    "iv_status": "unavailable",
                    "snapshot_metadata": {"snapshot_kind": "INTRADAY"},
                }
            )
    return history._normalize_chain_frame(pd.DataFrame(rows))


def _tfo_apr27_prior_settlement_frame():
    frame = _tfo_apr27_intraday_frame()
    frame["snapshot_metadata"] = [{"snapshot_kind": "SETTLEMENT"}] * len(frame)
    frame["business_date"] = pd.Timestamp("2026-08-17")
    frame["underlying_price"] = 48.333
    frame["settlement_price"] = frame["last_price"]
    frame["implied_volatility"] = frame["strike"].map(
        {48.0: 0.633951, 49.0: 0.638026, 50.0: 0.641815}
    )
    frame["iv_status"] = "resolved"
    return history._normalize_chain_frame(frame)


def test_layout_has_one_semantic_h1_and_auditable_components():
    items = list(_walk(history.layout))
    headings = [item for item in items if isinstance(item, html.H1)]
    ids = {getattr(item, "id", None) for item in items}
    trade_slider = next(
        item
        for item in items
        if getattr(item, "id", None) == "brent-vol-history-trade-start"
    )
    trade_presets = [
        item
        for item in items
        if getattr(item, "id", None)
        in {
            "brent-vol-history-trade-all",
            "brent-vol-history-trade-4h",
            "brent-vol-history-trade-1h",
            "brent-vol-history-trade-15m",
            "brent-vol-history-trade-latest",
        }
    ]
    worker_poll = next(
        item
        for item in items
        if getattr(item, "id", None) == "brent-vol-history-worker-poll"
    )
    assert len(headings) == 1
    assert headings[0].children == "Vol trades"
    assert headings[0].className == "brent-vol-history-visually-hidden-heading"
    assert trade_slider.allow_direct_input is False
    assert len(trade_presets) == 5
    assert all(button.disabled for button in trade_presets)
    assert worker_poll.interval == 10_000
    assert worker_poll.disabled is False
    assert "brent-vol-history-summary" not in ids
    assert "brent-vol-history-trade-status" not in ids
    assert "brent-vol-history-status" not in ids
    assert {
        "brent-vol-history-product",
        "brent-vol-history-date",
        "brent-vol-history-x-axis",
        "brent-vol-history-refresh-button",
        "brent-vol-history-settlement-refresh-button",
        "brent-vol-history-trade-start",
        "brent-vol-history-market-data-status",
        "brent-vol-history-trade-grid",
        "brent-vol-history-plots",
        "brent-vol-history-grid",
        "brent-vol-history-detail-expiry",
    }.issubset(ids)

    refresh_control = next(
        item
        for item in items
        if "brent-vol-history-refresh-control"
        in str(getattr(item, "className", ""))
    )
    refresh_items = list(_walk(refresh_control))
    refresh_ids = [getattr(item, "id", None) for item in refresh_items]
    assert refresh_ids.index("brent-vol-history-refresh-button") < refresh_ids.index(
        "brent-vol-history-settlement-refresh-button"
    )
    assert refresh_ids.index(
        "brent-vol-history-settlement-refresh-button"
    ) < refresh_ids.index(
        "brent-vol-history-refresh-status"
    )


def test_refresh_buttons_have_primary_secondary_focus_and_mobile_contracts():
    items = list(_walk(history.layout))
    intraday = next(
        item
        for item in items
        if getattr(item, "id", None) == "brent-vol-history-refresh-button"
    )
    settlement = next(
        item
        for item in items
        if getattr(item, "id", None)
        == "brent-vol-history-settlement-refresh-button"
    )
    assert intraday.children == "Refresh Bloomberg"
    assert settlement.children == "Refresh settlements"
    assert "refresh-button-primary" in intraday.className
    assert "refresh-button-secondary" in settlement.className
    assert intraday.title and settlement.title

    css = (
        Path(__file__).resolve().parents[1] / "assets" / "styles.css"
    ).read_text(encoding="utf-8")
    assert ".brent-vol-history-refresh-button:focus-visible" in css
    assert ".brent-vol-history-refresh-button-secondary" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css


def test_product_selector_keeps_all_products_on_one_desktop_row():
    css = (
        Path(__file__).resolve().parents[1] / "assets" / "styles.css"
    ).read_text(encoding="utf-8")
    assert ".brent-vol-history-product-options label" in css
    assert "flex: 1 1 0;" in css
    assert "white-space: nowrap;" in css
    assert "margin-right: 0;" in css
    assert "grid-template-columns:\n        312px\n        168px" in css
    assert "@media (min-width: 1281px) and (max-width: 1760px)" in css


def test_trade_and_chain_tables_use_grouped_trader_focused_format():
    items = list(_walk(history.layout))
    trade_grid = next(
        item
        for item in items
        if getattr(item, "id", None) == "brent-vol-history-trade-grid"
    )
    chain_grid = next(
        item
        for item in items
        if getattr(item, "id", None) == "brent-vol-history-grid"
    )

    assert [group["headerName"] for group in history.TRADE_TAPE_COLUMN_DEFS] == [
        "Trade",
        "Print",
        "Matched future",
        "Trade volatility",
    ]
    assert [group["headerName"] for group in history.DETAIL_COLUMN_DEFS] == [
        "Contract",
        "Option premium",
        "Volatility (%)",
        "Activity (contracts)",
        "Pricing future",
        "Last exact trade",
        "Quality & source",
    ]
    assert all(group["marryChildren"] for group in history.DETAIL_COLUMN_DEFS)

    detail_leaves = _column_leaves(history.DETAIL_COLUMN_DEFS)
    detail_by_field = {column["field"]: column for column in detail_leaves}
    trade_by_field = {
        column["field"]: column
        for column in _column_leaves(history.TRADE_TAPE_COLUMN_DEFS)
    }
    detail_row_fields = set(history._detail_rows(_chain_frame(), "2026-12-01")[0])
    assert set(detail_by_field) == detail_row_fields
    assert len(detail_leaves) == 59
    assert detail_by_field["option_security"]["pinned"] == "left"
    assert detail_by_field["put_call"]["pinned"] == "left"
    assert detail_by_field["strike"]["pinned"] == "left"
    assert detail_by_field["option_bid"]["columnGroupShow"] == "open"
    assert detail_by_field["executable_iv_mid_pct"]["valueFormatter"][
        "function"
    ].endswith("toFixed(2)")
    assert detail_by_field["volume"]["valueFormatter"] == history._GRID_INTEGER
    assert trade_by_field["future_match_source"]["headerName"] == "Method"
    assert trade_by_field["future_quote_ages"]["headerName"] == "Bid / ask age"
    assert detail_by_field["last_trade_underlying_source"]["headerName"] == (
        "Match method"
    )
    assert history._trade_match_source_label("TRADE") == "Future trade"

    assert trade_grid.dashGridOptions["groupHeaderHeight"] == 28
    assert chain_grid.dashGridOptions["groupHeaderHeight"] == 28
    assert "No exact trades" in trade_grid.dashGridOptions[
        "overlayNoRowsTemplate"
    ]
    assert "vol-trades-trade-grid" in trade_grid.className
    assert "vol-trades-chain-grid" in chain_grid.className
    assert "Bloomberg exact option trade tape" in trade_grid.eventListeners[
        "modelUpdated"
    ][0]
    assert "Bloomberg option-chain detail" in chain_grid.eventListeners[
        "firstDataRendered"
    ][0]
    assert trade_grid.defaultColDef["suppressHeaderFilterButton"] is True
    assert chain_grid.defaultColDef["suppressHeaderFilterButton"] is True
    assert trade_grid.style["height"] == chain_grid.style["height"] == "560px"

    subtitles = [
        item.children
        for item in items
        if getattr(item, "className", None)
        == "brent-vol-history-table-subtitle"
    ]
    assert len(subtitles) == 2
    assert any("selected expiry and trade window" in text for text in subtitles)
    assert any("Expand grouped headers" in text for text in subtitles)

    css = (
        Path(__file__).resolve().parents[1] / "assets" / "styles.css"
    ).read_text(encoding="utf-8")
    assert ".brent-vol-history-table-section" in css
    assert ".vol-trades-group-premium" in css
    assert ".vol-trades-call-cell" in css
    assert ".vol-trades-trade-grid:has(.ag-overlay-no-rows-wrapper)" not in css


def test_layout_includes_sourced_ice_open_interest_timing_note():
    items = list(_walk(history.layout))
    note = next(
        item
        for item in items
        if getattr(item, "className", None)
        == "brent-vol-history-methodology-note"
    )
    note_items = list(_walk(note))
    heading = next(
        item
        for item in note_items
        if getattr(item, "id", None)
        == "brent-vol-history-oi-methodology-title"
    )
    paragraph = next(item for item in note_items if isinstance(item, html.P))
    source = next(item for item in note_items if isinstance(item, html.A))

    assert note.role == "note"
    assert (
        note.to_plotly_json()["props"]["aria-labelledby"]
        == "brent-vol-history-oi-methodology-title"
    )
    assert heading.children == "ICE open-interest timing"
    assert "10:00 UK position-maintenance cutoff" in paragraph.children
    assert "does not carry forward a prior-day figure" in paragraph.children
    assert "does not revise settlement prices or reported volume" in paragraph.children
    assert source.children == "ICE Futures Europe position-maintenance guidance"
    assert source.href.startswith("https://www.ice.com/publicdocs/futures/")
    assert source.rel == "noopener noreferrer"


def test_intraday_universe_is_policy_filtered_while_settlement_stays_complete():
    months = pd.date_range("2026-08-01", "2028-12-01", freq="MS")
    frame = pd.DataFrame(
        {
            "business_date": ["2026-08-10"] * len(months),
            "underlying_contract_month": months,
            "option_security": [f"OPTION-{index}" for index in range(len(months))],
            "snapshot_metadata": [{} for _ in months],
        }
    )
    expected = {
        "2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01",
        "2026-12-01", "2027-01-01", "2027-06-01", "2027-12-01",
        "2028-06-01", "2028-12-01",
    }

    intraday, legacy_info = history.select_history_universe(frame, "INTRADAY")
    settlement, settlement_info = history.select_history_universe(
        frame, "SETTLEMENT"
    )
    assert set(
        pd.to_datetime(intraday["underlying_contract_month"])
        .dt.date.astype(str)
    ) == expected
    assert legacy_info["legacy_fallback"] is True
    assert len(settlement) == len(frame)
    assert settlement_info["scope"] == "ALL_AVAILABLE"

    governed = frame.copy()
    governed["snapshot_metadata"] = [
        {
            "intraday_universe": {
                "policy_version": "custom-product-policy-v1",
                "requested_contract_months": ["2026-10-01", "2027-06-01"],
                "selected_underlying_count": 2,
                "future_chain_count": len(months),
            }
        }
        for _ in months
    ]
    selected, governed_info = history.select_history_universe(
        governed, "INTRADAY"
    )
    assert set(
        pd.to_datetime(selected["underlying_contract_month"])
        .dt.date.astype(str)
    ) == {"2026-10-01", "2027-06-01"}
    assert governed_info["legacy_fallback"] is False
    assert governed_info["policy_version"] == "custom-product-policy-v1"


def test_tfo_intraday_universe_is_front_twelve_plus_quarters_to_year_two():
    months = pd.date_range("2026-08-01", "2028-12-01", freq="MS")
    frame = pd.DataFrame(
        {
            "business_date": ["2026-08-10"] * len(months),
            "underlying_contract_month": months,
            "option_security": [f"FJS-{index}" for index in range(len(months))],
            "snapshot_metadata": [{} for _ in months],
        }
    )
    selected, info = history.select_history_universe(
        frame, "INTRADAY", product="TFO"
    )
    selected_months = set(
        pd.to_datetime(selected["underlying_contract_month"])
        .dt.date.astype(str)
    )
    assert {f"2026-{month:02d}-01" for month in range(8, 13)} <= selected_months
    assert {f"2027-{month:02d}-01" for month in range(1, 8)} <= selected_months
    assert {
        "2027-09-01", "2027-12-01",
        "2028-03-01", "2028-06-01", "2028-09-01", "2028-12-01",
    } <= selected_months
    assert info["policy_version"] == "tfo-front12-quarterly-y2-v1"


def test_trade_tape_loader_is_snapshot_and_product_scoped(monkeypatch):
    captured = {}

    def fake_read_sql(query, engine, params):
        captured["sql"] = str(query)
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(history.pd, "read_sql", fake_read_sql)
    history.load_trade_tape("00000000-0000-0000-0000-000000000001", engine=object())
    assert "e.snapshot_id = CAST(:snapshot_id AS uuid)" in captured["sql"]
    assert "e.product = :product" in captured["sql"]
    assert "metadata ->> 'snapshot_kind'" in captured["sql"]
    assert captured["params"]["product"] == "BRENT"
    assert captured["params"]["snapshot_kind"] == "INTRADAY"


def test_trade_tape_exposes_individual_future_quote_ages():
    trade_at = pd.Timestamp("2026-08-10T08:00:00Z")
    trade_tape = pd.DataFrame(
        [
            {
                "business_date": pd.Timestamp("2026-08-10"),
                "option_security": "COZ6C 80 Comdty",
                "underlying_contract_month": pd.Timestamp("2026-12-01"),
                "option_expiration_date": pd.Timestamp("2026-10-27"),
                "put_call": "C",
                "strike": 80.0,
                "trade_at": trade_at,
                "trade_price": 2.5,
                "trade_size": 3.0,
                "condition_codes": None,
                "future_bid_at": trade_at - pd.Timedelta(seconds=141),
                "future_ask_at": trade_at - pd.Timedelta(seconds=52),
                "future_match_price": 78.0,
                "future_match_source": "PREVAILING_MID",
                "future_match_lag_ms": 52_000,
                "trade_iv": 0.494023,
                "trade_iv_status": "resolved",
                "trade_iv_exclusion_reason": None,
                "event_fingerprint": "a" * 64,
                "occurrence_ordinal": 1,
            }
        ]
    )

    payload = history.trade_trace_payloads(
        trade_tape, pd.Timestamp("2026-12-01"), "strike"
    )["C"]
    rows = history._trade_tape_rows(trade_tape, "2026-12-01", 0)
    chain = _chain_frame()
    chain["snapshot_kind"] = "INTRADAY"
    figure = history.build_expiry_figure(
        chain,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.Timestamp("2026-12-01"),
        trade_tape=trade_tape,
    )
    trace = next(
        item for item in figure.data if item.name == "Trade-time IV · Calls"
    )

    assert payload["customdata"][0][7] == "B 141.0s / A 52.0s"
    assert rows[0]["future_quote_ages"] == "B 141.0s / A 52.0s"
    assert "Quote ages %{customdata[7]}" in trace.hovertemplate


def test_navigation_routes_to_history_page():
    assert index_options.display_page("/brent_vol_history", None) is history.layout


def test_available_snapshot_query_is_product_scoped_and_latest_per_date(monkeypatch):
    captured = {}

    def fake_read_sql(query, engine, params):
        captured["sql"] = str(query)
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(history.pd, "read_sql", fake_read_sql)
    history.load_available_snapshots(engine=object(), limit=20)
    assert "PARTITION BY s.business_date" in captured["sql"]
    assert "c.product = :product" in captured["sql"]
    assert captured["params"] == {
        "product": "BRENT",
        "limit": 20,
        "settlement_kind": "SETTLEMENT",
        "intraday_kind": "INTRADAY",
    }


def test_snapshot_selector_fails_closed_when_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        history, "load_available_snapshots",
        lambda _product: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    assert history.update_history_dates(0, None, "BRENT", None) == ([], None)


def test_published_surface_query_requires_exact_cob(monkeypatch):
    captured = {}

    def fake_read_sql(query, engine, params):
        captured["sql"] = str(query)
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(history.pd, "read_sql", fake_read_sql)
    history.load_published_surface("2026-08-10", engine=object())
    assert "cob_date = :cob_date" in captured["sql"]
    assert "<=" not in captured["sql"]
    assert captured["params"]["cob_date"] == date(2026, 8, 10)
    assert captured["params"]["snapshot_kind"] == "SETTLEMENT"


def test_tfo_published_overlay_queries_ttf_without_merging_products(monkeypatch):
    captured = []

    def fake_read_sql(query, engine, params):
        captured.append((str(query), dict(params)))
        return pd.DataFrame()

    monkeypatch.setattr(history.pd, "read_sql", fake_read_sql)
    history.load_published_surface(
        "2026-08-10", engine=object(), product="TFO", snapshot_kind="SETTLEMENT"
    )
    assert captured[0][1]["product"] == "TTF"
    assert captured[0][1]["snapshot_kind"] == "SETTLEMENT"
    assert history.load_published_surface(
        "2026-08-10", engine=object(), product="TFO", snapshot_kind="INTRADAY"
    ).empty
    assert len(captured) == 1


@pytest.mark.parametrize("product", ["ON", "LNE"])
def test_henry_hub_published_overlay_queries_hh_without_merging_products(
    monkeypatch, product
):
    captured = []

    def fake_read_sql(query, engine, params):
        captured.append((str(query), dict(params)))
        return pd.DataFrame()

    monkeypatch.setattr(history.pd, "read_sql", fake_read_sql)
    history.load_published_surface(
        "2026-08-10", engine=object(), product=product, snapshot_kind="SETTLEMENT"
    )
    assert captured[0][1]["product"] == "HH"
    assert captured[0][1]["snapshot_kind"] == "SETTLEMENT"


def test_henry_hub_products_use_ng_units_and_governed_pipeline_iv():
    for product in ("ON", "LNE"):
        spec = history.PRODUCT_SPECS[product]
        assert spec["price_unit"] == "USD/MMBtu"
        assert spec["underlying_label"] == "NG"
        chain = _chain_frame()
        chain["product"] = product
        assert history.prepare_market_observations(chain, product=product).empty


def test_latest_calibrated_surface_uses_latest_active_publication(
    monkeypatch,
):
    captured = []

    def fake_read_sql(query, engine, params):
        captured.append((str(query), dict(params)))
        if len(captured) == 1:
            return pd.DataFrame(
                {
                    "publication_id": ["9572ae7f-90ca-4c8f-aecd-fbaff1ed081d"],
                    "run_id": ["1fb16e4f-d1c7-4bc9-9e32-6dc4899ebf9a"],
                    "commodity": ["TTF"],
                    "publication_cob_date": [date(2026, 8, 24)],
                    "published_at": [pd.Timestamp("2026-08-25T09:33:19Z")],
                    "published_by": ["publisher"],
                }
            )
        return _calibrated_frame().drop(
            columns=[
                "publication_id",
                "run_id",
                "commodity",
                "publication_cob_date",
                "published_at",
                "published_by",
            ]
        )

    monkeypatch.setattr(history.pd, "read_sql", fake_read_sql)
    surface = history.load_latest_calibrated_surface(
        "2026-08-26",
        ["2026-12-01"],
        engine=object(),
        product="TFO",
    )

    assert "p.status = 'published'" in captured[0][0]
    assert "p.is_active" in captured[0][0]
    assert "p.cob_date <=" not in captured[0][0]
    assert captured[0][1] == {"commodity": "TTF"}
    assert "s.contract_date IN" in captured[1][0]
    assert captured[1][1]["contract_dates"] == (date(2026, 12, 1),)
    assert surface.attrs["publication_status"] == "available"
    assert history.calibrated_publication_metadata(surface)["publication_id"] == (
        "9572ae7f-90ca-4c8f-aecd-fbaff1ed081d"
    )


def test_missing_calibrated_publication_is_explicit(monkeypatch):
    monkeypatch.setattr(
        history.pd,
        "read_sql",
        lambda query, engine, params: pd.DataFrame(),
    )
    surface = history.load_latest_calibrated_surface(
        "2026-08-26",
        ["2026-12-01"],
        engine=object(),
        product="BRENT",
    )
    assert surface.attrs["publication_status"] == "no_publication"
    cards = history.build_plot_cards(
        _chain_frame(),
        pd.DataFrame(),
        product="BRENT",
        calibrated=surface,
    )
    contract = history._expiry_legend_contract(cards)
    assert "calibrated" not in contract["available_layers"]


def test_chain_query_requires_product_and_snapshot_kind(monkeypatch):
    captured = []

    def fake_read_sql(query, engine, params):
        captured.append((str(query), dict(params)))
        return pd.DataFrame()

    monkeypatch.setattr(history.pd, "read_sql", fake_read_sql)
    history.load_chain_snapshot(
        "00000000-0000-0000-0000-000000000001",
        engine=object(),
        product="TFO",
        snapshot_kind="INTRADAY",
    )
    assert captured[0][1] == {
        "snapshot_id": "00000000-0000-0000-0000-000000000001",
        "product": "TFO",
        "snapshot_kind": "INTRADAY",
    }
    assert "metadata ->> 'snapshot_kind'" in captured[0][0]


def test_published_delta_nodes_convert_to_strike():
    nodes = history.published_strike_nodes(
        _published_frame(),
        {pd.Timestamp("2026-12-01"): 80.0},
        pd.Timestamp("2026-08-10"),
    )
    assert len(nodes) == 2
    assert nodes["strike"].notna().all()
    assert nodes["forward"].tolist() == [80.0, 80.0]
    assert nodes.iloc[0]["strike"] < 80.0
    assert nodes.iloc[1]["strike"] > 80.0


def test_expiry_figure_overlays_smile_volume_and_open_interest():
    chain = _chain_frame()
    prepared = history.prepare_market_observations(chain)
    published = history.published_strike_nodes(
        _published_frame(),
        {pd.Timestamp("2026-12-01"): 80.0},
        pd.Timestamp("2026-08-10"),
    )
    figure = history.build_expiry_figure(
        chain,
        prepared,
        published,
        pd.Timestamp("2026-12-01"),
    )
    trace_names = {trace.name for trace in figure.data}
    assert "Bloomberg settlement IV" in trace_names
    assert "Settlement eligible" not in trace_names
    assert "Settlement excluded" not in trace_names
    assert "Published Brent exact COB" in trace_names
    assert "Volume · calls" in trace_names
    assert "Volume · puts" in trace_names
    assert "Open interest · calls" in trace_names
    assert "Open interest · puts" in trace_names
    assert figure.layout.barmode == "overlay"
    assert figure.layout.xaxis.title.text == "Strike (USD/bbl)"
    assert figure.layout.yaxis.title.text == "IV (%)"
    assert figure.layout.yaxis2.title.text == "Activity (contracts)"
    assert figure.layout.yaxis2.overlaying == "y"
    assert figure.layout.margin.r == 44
    assert figure.layout.showlegend is False
    traces = {trace.name: trace for trace in figure.data}
    assert traces["Open interest · calls"].width > traces["Volume · calls"].width
    assert traces["Open interest · calls"].opacity < traces["Volume · calls"].opacity
    assert traces["Open interest · calls"].yaxis == "y2"
    assert traces["Volume · calls"].yaxis == "y2"
    assert traces["Volume · calls"].meta["legend_layer"] == "volume-calls"
    assert traces["Volume · puts"].meta["legend_layer"] == "volume-puts"
    assert (
        traces["Open interest · calls"].meta["legend_layer"]
        == "open-interest-calls"
    )
    assert (
        traces["Open interest · puts"].meta["legend_layer"]
        == "open-interest-puts"
    )
    assert (
        traces["Bloomberg settlement IV"].meta["legend_layer"]
        == "bloomberg-settlement"
    )
    assert figure.layout.shapes[0].name == "pricing-reference"
    for trace_name in (
        "Open interest · calls",
        "Volume · calls",
        "Bloomberg settlement IV",
    ):
        assert "Underlying" in traces[trace_name].hovertemplate
        assert "Volume / OI" in traces[trace_name].hovertemplate
    assert "Underlying" in traces["Published Brent exact COB"].hovertemplate
    assert figure.layout.hovermode == "closest"
    assert figure.layout.hoverlabel.bgcolor == "#0F172A"
    assert figure.layout.hoverlabel.font.color == "#F8FAFC"
    assert set(traces["Open interest · calls"].customdata[:, 4]) == {80.0}
    assert list(traces["Bloomberg settlement IV"].customdata[:, 5]) == [80.0, 80.0]
    assert list(traces["Bloomberg settlement IV"].customdata[:, 6]) == [
        "Eligible · OI and moneyness gates passed",
        "Eligible · OI and moneyness gates passed",
    ]
    assert (
        "Premium <b>%{customdata[2]:.4f} USD/bbl</b>"
        in traces["Bloomberg settlement IV"].hovertemplate
    )
    assert "<b>Settlement from Bloomberg</b>" in (
        traces["Bloomberg settlement IV"].hovertemplate
    )
    assert "Calibration: <b>%{customdata[6]}</b>" in (
        traces["Bloomberg settlement IV"].hovertemplate
    )
    assert list(traces["Published Brent exact COB"].customdata[:, 4]) == [80.0, 80.0]
    trace_order = [trace.name for trace in figure.data]
    assert trace_order.index("Open interest · calls") < trace_order.index("Volume · calls")
    assert trace_order.index("Volume · calls") < trace_order.index(
        "Bloomberg settlement IV"
    )


def test_tfo_figure_uses_tzt_hovers_eur_units_and_separate_ttf_overlay():
    chain = _chain_frame().copy()
    chain["product"] = "TFO"
    chain["underlying_security"] = "FJSZ6 Comdty"
    chain["pricing_underlying_security"] = "TZTZ6 Comdty"
    chain["option_style"] = "EUROPEAN"
    chain["currency"] = "EUR"
    chain["price_unit"] = "EUR/MWH"
    published = history.published_strike_nodes(
        _published_frame(),
        {pd.Timestamp("2026-12-01"): 80.0},
        pd.Timestamp("2026-08-10"),
    )
    figure = history.build_expiry_figure(
        chain,
        history.prepare_market_observations(chain, product="TFO"),
        published,
        pd.Timestamp("2026-12-01"),
        product="TFO",
        calibrated_nodes=_calibrated_frame(),
    )
    traces = {trace.name: trace for trace in figure.data}
    assert figure.layout.xaxis.title.text == "Strike (EUR/MWh)"
    assert "Published TTF exact COB" in traces
    calibrated_name = "Calibrated TTF · COB 24 Aug 2026"
    assert calibrated_name in traces
    assert traces[calibrated_name].line.color == "#7C3AED"
    assert traces[calibrated_name].line.dash == "dash"
    assert "Revision 9572ae7f-90ca-4c8f-aecd-fbaff1ed081d" in (
        traces[calibrated_name].hovertemplate
    )
    assert "Bloomberg settlement IV" in traces
    assert "Settlement eligible" not in traces
    assert "TZT %{customdata[4]:.3f}" in traces["Published TTF exact COB"].hovertemplate
    assert "TZT %{customdata[5]:.3f}" in traces["Bloomberg settlement IV"].hovertemplate
    assert "Volume / OI" in traces["Bloomberg settlement IV"].hovertemplate
    assert (
        "Premium <b>%{customdata[2]:.4f} EUR/MWh</b>"
        in traces["Bloomberg settlement IV"].hovertemplate
    )
    detail = history._detail_rows(chain, "2026-12-01")
    assert {row["native_option_underlier"] for row in detail} == {"FJSZ6 Comdty"}
    assert {row["pricing_future"] for row in detail} == {"TZTZ6 Comdty"}


def test_tfo_settlement_axis_uses_exchange_range_not_calibrated_tail_extremes():
    chain = _chain_frame().copy()
    low_strike = chain.iloc[[0]].copy()
    low_strike["option_security"] = "FJSZ6P 5 Comdty"
    low_strike["option_global_id"] = "BBG-FJSZ6P-5"
    low_strike["put_call"] = "P"
    low_strike["strike"] = 5.0
    low_strike["settlement_price"] = 0.01
    low_strike["implied_volatility"] = 1.50
    high_strike = chain.iloc[[1]].copy()
    high_strike["option_security"] = "FJSZ6C 300 Comdty"
    high_strike["option_global_id"] = "BBG-FJSZ6C-300"
    high_strike["put_call"] = "C"
    high_strike["strike"] = 300.0
    high_strike["settlement_price"] = 0.01
    high_strike["implied_volatility"] = 1.50
    chain = pd.concat([chain, low_strike, high_strike], ignore_index=True)
    chain["product"] = "TFO"
    chain["underlying_security"] = "FJSZ6 Comdty"
    chain["pricing_underlying_security"] = "TZTZ6 Comdty"
    chain["option_style"] = "EUROPEAN"
    chain["currency"] = "EUR"
    chain["price_unit"] = "EUR/MWH"

    calibrated = _calibrated_frame()
    calibrated.loc[calibrated.index[-1], "strike"] = 500.0
    figure = history.build_expiry_figure(
        chain,
        history.prepare_market_observations(chain, product="TFO"),
        pd.DataFrame(),
        pd.Timestamp("2026-12-01"),
        product="TFO",
        calibrated_nodes=calibrated,
    )

    settlement = next(
        trace for trace in figure.data if trace.name == "Bloomberg settlement IV"
    )
    assert list(settlement.customdata[:, 0]) == [5.0, 70.0, 90.0, 300.0]
    assert figure.layout.xaxis.range[0] <= 5.0
    assert figure.layout.xaxis.range[1] > 300.0
    assert figure.layout.xaxis.range[1] < 500.0


def test_settlement_smile_never_substitutes_intrinsic_itm_option_for_unresolved_otm():
    chain = _chain_frame().copy()
    high_call = chain.iloc[[1]].copy()
    high_call["option_security"] = "FJSZ6C 200 Comdty"
    high_call["option_global_id"] = "BBG-FJSZ6C-200"
    high_call["put_call"] = "C"
    high_call["strike"] = 200.0
    high_call["settlement_price"] = 0.001
    high_call["implied_volatility"] = None
    high_call["iv_status"] = "unresolved"
    high_call["iv_exclusion_reason"] = (
        "ICE settlement does not imply a Black-76 volatility in (0.5%, 200%)"
    )
    high_put = high_call.copy()
    high_put["option_security"] = "FJSZ6P 200 Comdty"
    high_put["option_global_id"] = "BBG-FJSZ6P-200"
    high_put["put_call"] = "P"
    high_put["settlement_price"] = 120.0
    high_put["implied_volatility"] = 0.005
    high_put["iv_status"] = "resolved"
    high_put["iv_exclusion_reason"] = None
    chain = pd.concat([chain, high_call, high_put], ignore_index=True)
    chain["product"] = "TFO"
    chain["underlying_security"] = "FJSZ6 Comdty"
    chain["pricing_underlying_security"] = "TZTZ6 Comdty"
    chain["option_style"] = "EUROPEAN"
    chain["currency"] = "EUR"
    chain["price_unit"] = "EUR/MWH"
    chain = history._normalize_chain_frame(chain)

    selected, excluded = history._settlement_reference_selection(chain, "strike")

    assert 200.0 not in set(selected["strike"])
    exclusion = excluded.loc[excluded["strike"].eq(200.0)].iloc[0]
    assert exclusion["put_call"] == "C"
    assert exclusion["option_security"] == "FJSZ6C 200 Comdty"
    assert exclusion["settlement_price"] == pytest.approx(0.001)
    assert exclusion["reason_code"] == "otm_iv_outside_supported_range"

    cards = history.build_plot_cards(
        chain,
        pd.DataFrame(),
        product="TFO",
    )
    note = cards[-1]
    note_text = " ".join(item for item in _walk(note) if isinstance(item, str))
    assert note.className == "brent-vol-history-settlement-exclusion-note"
    assert note.role == "note"
    assert "Excluded from settlement IV charts" in note_text
    assert "2 excluded" in note_text
    assert "1 expiry" in note_text
    assert "OTM premium does not imply IV within 0.5%–200%" in note_text
    assert "Dec-26" in note_text
    assert "Puts" in note_text
    assert "Calls" in note_text
    assert "50" in note_text
    assert "200" in note_text
    assert "Official premiums remain available in Option-chain detail" in note_text
    note_items = list(_walk(note))
    strike_chips = [
        item
        for item in note_items
        if getattr(item, "className", None)
        == "brent-vol-history-exclusion-strike-chip"
    ]
    assert [item.children for item in strike_chips] == ["50", "200"]
    assert [item.title for item in strike_chips] == ["50P", "200C"]
    assert any(
        "brent-vol-history-exclusion-metric-primary"
        in str(getattr(item, "className", "")).split()
        for item in note_items
    )


def test_expiry_section_uses_one_shared_chart_legend():
    items = list(_walk(history.layout))
    legend = next(
        item
        for item in items
        if getattr(item, "className", None) == "brent-vol-history-common-legend"
    )
    checklist, reset = legend.children
    assert checklist.id == "brent-vol-history-expiry-layers"
    assert checklist.options == []
    assert checklist.value == []
    assert reset.children == "Reset"
    assert legend.role == "group"
    options = history._expiry_legend_options(
        [
            "call-mid",
            "put-mid",
            "trades",
            "prior-settlement",
            "pricing-reference",
            "volume-calls",
            "volume-puts",
            "open-interest-calls",
            "open-interest-puts",
        ],
        new_volume_layers=["volume-puts"],
    )
    assert [option["value"] for option in options] == [
        "call-mid",
        "put-mid",
        "trades",
        "prior-settlement",
        "pricing-reference",
        "volume-calls",
        "volume-puts",
        "open-interest-calls",
        "open-interest-puts",
    ]
    assert [option["label"].children[1].children for option in options] == [
        "Call mid",
        "Put mid",
        "Trades",
        "Prior settle",
        "ATM / future",
        "Volume calls",
        "Volume puts · new edge",
        "OI calls",
        "OI puts",
    ]


def test_expiry_legend_contract_only_exposes_plotted_layers():
    cards = history.build_plot_cards(
        _chain_frame(),
        pd.DataFrame(),
        product="BRENT",
    )
    contract = history._expiry_legend_contract(cards)

    assert contract["available_layers"] == [
        "bloomberg-settlement",
        "pricing-reference",
        "volume-calls",
        "volume-puts",
        "open-interest-calls",
        "open-interest-puts",
    ]
    assert contract["new_volume_layers"] == []
    assert "trades" not in contract["available_layers"]
    assert "prior-settlement" not in contract["available_layers"]
    assert "published" not in contract["available_layers"]
    assert contract["graphs"]["2026-12-01"]["shapes"] == [
        {"index": 0, "layer": "pricing-reference"}
    ]
    assert all(
        entry["layer"] in history.EXPIRY_LEGEND_LAYER_SPECS
        for entry in contract["graphs"]["2026-12-01"]["traces"]
    )


def test_expiry_quality_summary_is_inside_the_contract_header():
    cards = history.build_plot_cards(
        _tfo_apr27_intraday_frame(),
        pd.DataFrame(),
        x_axis="delta",
        prior_settlement_chain=_tfo_apr27_prior_settlement_frame(),
        product="TFO",
    )
    card = cards[0]
    header = card.children[0]

    assert header.className == "brent-vol-history-card-header"
    assert header.children[0].children == "Apr-27"
    assert header.children[0].className == "brent-vol-history-card-title"
    assert header.children[1].className == "brent-vol-history-card-quality"
    assert header.children[1].role == "status"
    assert header.children[1].children[0].children == "Prior settle · 17 Aug 2026"
    assert header.children[1].children[1].className == (
        "brent-vol-history-quality-detail"
    )
    assert header.children[1].title.startswith("No reliable current IV")
    assert len(card.children) == 2


def test_expiry_legend_tracks_new_volume_edge_by_option_side():
    chain = _chain_frame()
    chain["snapshot_kind"] = "INTRADAY"
    chain["volume_delta"] = 0.0
    chain.loc[chain["put_call"].eq("P"), "volume_delta"] = 5.0

    contract = history._expiry_legend_contract(
        history.build_plot_cards(chain, pd.DataFrame(), product="BRENT")
    )

    assert contract["new_volume_layers"] == ["volume-puts"]


def test_bloomberg_settlement_legend_uses_plain_label_and_source_tooltip():
    option = history._expiry_legend_options(["bloomberg-settlement"])[0]

    assert option["label"].children[1].children == "Settlement"
    assert option["label"].title == "Settlement from Bloomberg"


def test_expiry_layer_selection_preserves_existing_and_enables_new_layers():
    selected = history._selected_expiry_layers(
        ["call-mid", "trades", "volume-calls"],
        [{"value": "call-mid"}, {"value": "volume-calls"}],
        ["call-mid"],
    )
    assert selected == ["call-mid", "trades"]


def test_expiry_layer_defaults_hide_mids_but_preserve_manual_selection():
    available = ["call-mid", "put-mid", "trades", "volume-calls"]

    assert history._selected_expiry_layers(available, [], []) == [
        "trades",
        "volume-calls",
    ]
    assert history._selected_expiry_layers(
        available,
        [{"value": layer} for layer in available],
        ["call-mid", "trades", "volume-calls"],
    ) == ["call-mid", "trades", "volume-calls"]
    assert history._selected_expiry_layers(
        available,
        [{"value": "trades"}, {"value": "volume-calls"}],
        ["trades", "volume-calls"],
    ) == ["trades", "volume-calls"]


def test_expiry_layer_visibility_uses_small_visibility_only_patches():
    manifest = {
        "available_layers": [
            "volume-calls",
            "volume-puts",
            "pricing-reference",
        ],
        "graphs": {
            "2026-12-01": {
                "traces": [
                    {"index": 2, "layer": "volume-calls"},
                    {"index": 3, "layer": "volume-puts"},
                ],
                "shapes": [{"index": 0, "layer": "pricing-reference"}],
            }
        },
    }
    updates = history.update_expiry_layer_visibility(
        ["volume-puts", "pricing-reference"],
        manifest,
        [
            {
                "type": "brent-vol-history-expiry-graph",
                "expiry": "2026-12-01",
            }
        ],
    )
    operations = updates[0].to_plotly_json()["operations"]
    assert operations == [
        {
            "operation": "Assign",
            "location": ["data", 2, "visible"],
            "params": {"value": False},
        },
        {
            "operation": "Assign",
            "location": ["data", 3, "visible"],
            "params": {"value": True},
        },
        {
            "operation": "Assign",
            "location": ["layout", "shapes", 0, "visible"],
            "params": {"value": True},
        },
    ]
    assert history.reset_expiry_layers(
        1,
        [
            {"value": "call-mid"},
            {"value": "put-mid"},
            {"value": "volume-calls"},
            {"value": "volume-puts"},
            {"value": "open-interest-calls"},
            {"value": "open-interest-puts"},
        ],
    ) == [
        "volume-calls",
        "volume-puts",
        "open-interest-calls",
        "open-interest-puts",
    ]


def test_settlement_iv_remains_visible_without_volume_or_open_interest():
    chain = _chain_frame()
    for column in (
        "volume",
        "source_volume",
        "open_interest",
        "source_open_interest",
        "settlement_open_interest",
    ):
        chain[column] = None
    prepared = history.prepare_market_observations(chain)
    figure = history.build_expiry_figure(
        chain,
        prepared,
        pd.DataFrame(),
        pd.Timestamp("2026-12-01"),
    )
    traces = {trace.name: trace for trace in figure.data}

    assert "Settlement eligible" not in traces
    assert "Settlement excluded" not in traces
    assert "Bloomberg settlement IV" in traces
    assert list(traces["Bloomberg settlement IV"].customdata[:, 0]) == [70.0, 90.0]
    assert list(traces["Bloomberg settlement IV"].customdata[:, 3]) == ["—", "—"]
    assert list(traces["Bloomberg settlement IV"].customdata[:, 4]) == ["—", "—"]
    assert list(traces["Bloomberg settlement IV"].customdata[:, 5]) == [80.0, 80.0]
    assert list(traces["Bloomberg settlement IV"].customdata[:, 6]) == [
        "Not assessed · OI unavailable",
        "Not assessed · OI unavailable",
    ]
    assert "Calibration: <b>%{customdata[6]}</b>" in (
        traces["Bloomberg settlement IV"].hovertemplate
    )


def test_missing_bid_ask_never_turns_last_price_into_a_current_iv_curve():
    chain = _intraday_missing_quote_frame()
    figure = history.build_expiry_figure(
        chain,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.Timestamp("2028-06-01"),
        x_axis="delta",
    )
    trace_names = {trace.name for trace in figure.data}
    assert "Indicative last-price IV" not in trace_names
    assert not any(name.startswith("Executable mid IV") for name in trace_names)
    assert not any(trace.name.startswith("Volume ·") for trace in figure.data)
    assert not any(
        trace.name.startswith("Open interest ·") for trace in figure.data
    )
    assert dict(figure.layout.meta)["quality"]["status"] == "No reliable IV"


def test_apr27_last_prices_are_audit_only_and_prior_settlement_drives_delta():
    chain = _tfo_apr27_intraday_frame()
    prior = _tfo_apr27_prior_settlement_frame()
    quality = history._last_price_parity_quality(chain)

    assert quality["status"] == "coherent_historical"
    assert quality["pair_count"] == 3
    assert quality["parity_forward"] == pytest.approx(48.333, abs=1e-9)
    assert quality["parity_mad"] == pytest.approx(0.0, abs=1e-9)
    assert quality["live_forward"] == pytest.approx(49.7925)
    assert quality["gap"] == pytest.approx(1.4595)

    figure = history.build_expiry_figure(
        chain,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.Timestamp("2027-04-01"),
        x_axis="delta",
        prior_settlement_chain=prior,
        product="TFO",
    )
    traces = {trace.name: trace for trace in figure.data}
    prior_name = "Prior settlement IV · 17 Aug 2026"
    assert "Indicative last-price IV" not in traces
    assert prior_name in traces
    assert "Volume / OI" not in traces[prior_name].hovertemplate
    assert any(trace.type == "bar" for trace in figure.data)
    delta_sources = {
        str(row[2])
        for trace in figure.data
        if trace.type == "bar"
        for row in trace.customdata
    }
    assert all("Prior settlement 17 Aug 2026" in source for source in delta_sources)
    figure_quality = dict(figure.layout.meta)["quality"]
    assert figure_quality["status"] == "Prior settle · 17 Aug 2026"
    assert "No reliable current IV" in figure_quality["detail"]
    assert "48.333" in figure_quality["detail"]
    assert "49.7925" in figure_quality["detail"]

    cards = history.build_plot_cards(
        chain,
        pd.DataFrame(),
        x_axis="delta",
        prior_settlement_chain=prior,
        product="TFO",
    )
    visible_text = " ".join(item for item in _walk(cards[0]) if isinstance(item, str))
    assert "Prior settle · 17 Aug 2026" in visible_text
    assert "FJS last-price parity implies TZT 48.3330" in visible_text


def test_expiry_figure_keeps_extreme_activity_but_focuses_on_smile():
    chain = _chain_frame()
    extreme_mask = chain["option_security"].eq("COZ6P 50 Comdty")
    chain.loc[extreme_mask, "option_security"] = "COZ6P 400 Comdty"
    chain.loc[extreme_mask, "option_global_id"] = "BBG-EXTREME"
    chain.loc[extreme_mask, "strike"] = 400.0
    chain.loc[extreme_mask, "settlement_price"] = 0.01
    chain.loc[extreme_mask, "volume"] = 1
    chain.loc[extreme_mask, "open_interest"] = 2
    chain.loc[extreme_mask, "implied_volatility"] = None
    chain.loc[extreme_mask, "iv_status"] = "excluded"
    chain.loc[extreme_mask, "iv_exclusion_reason"] = "outside governed display band"
    prepared = history.prepare_market_observations(chain)
    published = history.published_strike_nodes(
        _published_frame(),
        {pd.Timestamp("2026-12-01"): 80.0},
        pd.Timestamp("2026-08-10"),
    )
    figure = history.build_expiry_figure(
        chain,
        prepared,
        published,
        pd.Timestamp("2026-12-01"),
    )
    open_interest = next(
        trace for trace in figure.data if trace.name == "Open interest · puts"
    )
    assert 400.0 in list(open_interest.x)
    assert figure.layout.xaxis.range[1] < 400.0


def test_delta_axis_uses_put_atm_call_convention_and_keeps_activity_only_strikes():
    chain = _chain_frame()
    prepared = history.prepare_market_observations(chain)
    published = history.published_strike_nodes(
        _published_frame(),
        {pd.Timestamp("2026-12-01"): 80.0},
        pd.Timestamp("2026-08-10"),
    )
    figure = history.build_expiry_figure(
        chain,
        prepared,
        published,
        pd.Timestamp("2026-12-01"),
        x_axis="delta",
    )

    assert figure.layout.xaxis.title.text == "Delta (put wing → call wing)"
    assert list(figure.layout.xaxis.range) == [0.0, 1.0]
    assert list(figure.layout.xaxis.ticktext) == [
        "0Δ put",
        "10Δ put",
        "25Δ put",
        "ATM",
        "25Δ call",
        "10Δ call",
        "0Δ call",
    ]
    for trace in figure.data:
        if len(trace.x):
            assert all(0.0 <= float(value) <= 1.0 for value in trace.x)

    put_open_interest = next(
        trace for trace in figure.data if trace.name == "Open interest · puts"
    )
    plotted_strikes = [float(row[0]) for row in put_open_interest.customdata]
    assert 50.0 in plotted_strikes
    placement_by_strike = {
        float(row[0]): row[2] for row in put_open_interest.customdata
    }
    assert placement_by_strike[50.0] == "Nearest current executable smile wing"
    published_trace = next(
        trace for trace in figure.data if trace.name == "Published Brent exact COB"
    )
    assert list(published_trace.x) == [0.25, 0.75]


def test_detail_rows_distinguish_zero_from_missing_and_keep_status():
    rows = history._detail_rows(_chain_frame(), "2026-12-01")
    by_security = {row["option_security"]: row for row in rows}
    assert by_security["COZ6C 90 Comdty"]["volume"] == 0
    assert by_security["COZ6P 50 Comdty"]["volume"] is None
    assert by_security["COZ6P 50 Comdty"]["iv_status"] == "missing_settlement"


def test_intraday_activity_excludes_prior_volume_but_labels_stale_realtime_oi():
    frame = _chain_frame()
    frame["snapshot_metadata"] = [{"snapshot_kind": "INTRADAY"}] * len(frame)
    frame["business_date"] = pd.Timestamp("2026-08-12")
    frame["last_trade_date"] = pd.to_datetime(
        ["2026-08-12", "2026-08-11", None]
    )
    frame["settlement_open_interest"] = frame["open_interest"]
    frame["settlement_open_interest_date"] = pd.Timestamp("2026-08-11")
    frame["intraday_open_interest"] = frame["open_interest"]
    frame["intraday_open_interest_date"] = pd.to_datetime(
        ["2026-08-12", "2026-08-11", None]
    )

    normalized = history._normalize_chain_frame(frame)
    by_security = normalized.set_index("option_security")

    same_day = by_security.loc["COZ6P 70 Comdty"]
    assert same_day["volume"] == 20
    assert same_day["open_interest"] == 250
    assert same_day["settlement_open_interest"] == 250
    assert same_day["intraday_open_interest"] == 250
    assert same_day["open_interest_source"] == "Bloomberg intraday RT_OPEN_INTEREST"
    assert same_day["volume_scope_status"] == "same_day"
    assert same_day["open_interest_scope_status"] == "same_day"

    prior_session = by_security.loc["COZ6C 90 Comdty"]
    assert pd.isna(prior_session["volume"])
    assert prior_session["open_interest"] == 180
    assert prior_session["volume_scope_status"] == "prior_session_excluded"
    assert prior_session["open_interest_scope_status"] == "stale"

    missing_dates = by_security.loc["COZ6P 50 Comdty"]
    assert pd.isna(missing_dates["volume"])
    assert missing_dates["open_interest"] == 10
    assert missing_dates["volume_scope_status"] == "unavailable"
    assert (
        missing_dates["open_interest_scope_status"]
        == "effective_date_unavailable"
    )


def test_intraday_open_interest_never_falls_back_to_settlement_column():
    frame = _chain_frame().iloc[[0]].copy()
    frame["snapshot_metadata"] = [{"snapshot_kind": "INTRADAY"}]
    frame["business_date"] = pd.Timestamp("2026-08-12")
    frame["last_trade_date"] = pd.Timestamp("2026-08-12")
    frame["settlement_open_interest"] = 9_999
    frame["settlement_open_interest_date"] = pd.Timestamp("2026-08-11")
    frame["intraday_open_interest"] = None
    frame["intraday_open_interest_date"] = None
    normalized = history._normalize_chain_frame(frame).iloc[0]
    assert normalized["settlement_open_interest"] == 9_999
    assert pd.isna(normalized["source_open_interest"])
    assert pd.isna(normalized["open_interest"])
    assert normalized["open_interest_scope_status"] == "unavailable"


def test_settlement_snapshot_uses_official_open_interest_source():
    normalized = _chain_frame()
    row = normalized.iloc[0]
    assert row["open_interest"] == row["settlement_open_interest"] == 250
    assert row["open_interest_date"] == pd.Timestamp("2026-08-10")
    assert row["open_interest_source"] == "Bloomberg settlement OPEN_INT"


def test_publication_coverage_ignores_non_chain_maturities():
    published = pd.concat(
        [
            _published_frame(),
            _published_frame().assign(contract_date=pd.Timestamp("2027-01-01")),
        ],
        ignore_index=True,
    )
    assert history.publication_coverage(_chain_frame(), published) == (1, 1)
