from datetime import datetime, timezone
from pathlib import Path
import uuid

import pandas as pd
from dash import dcc, html

import index_options
from pages import ice_chat_quotes as quotes


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


def _column_fields(column_defs):
    fields = []
    for definition in column_defs:
        if definition.get("field"):
            fields.append(definition["field"])
        fields.extend(_column_fields(definition.get("children", [])))
    return fields


def _columns_by_field(column_defs):
    definitions = {}
    for definition in column_defs:
        if definition.get("field"):
            definitions[definition["field"]] = definition
        definitions.update(_columns_by_field(definition.get("children", [])))
    return definitions


def _rows():
    frame = pd.DataFrame(
        [
            {
                "event_id": "event-1",
                "observed_at": "2026-08-17T08:00:00Z",
                "received_at": "2026-08-17T08:00:01Z",
                "source_channel": "message",
                "recovered": False,
                "message_id": "message-1",
                "pricing_request_id": None,
                "market_id": "market-1",
                "quote_id": "quote-1",
                "sender_handle": "desk-user",
                "user_handle": "bot",
                "normalization_status": "accepted",
                "normalization_error": None,
                "contract_month": "2026-10-01",
                "option_type": "C",
                "strike": 90.0,
                "bid": 5.10,
                "bid_size": 25,
                "offer": 5.40,
                "offer_size": 30,
                "single_price": None,
                "single_size": None,
                "valuation_status": "valued",
                "valuation_reason": None,
                "valued_at": "2026-08-17T08:00:02Z",
                "theoretical_price": 5.00,
                "our_volatility": 0.58,
                "bid_implied_volatility": 0.59,
                "offer_implied_volatility": 0.62,
                "single_implied_volatility": None,
                "bid_sell_edge": 0.10,
                "offer_buy_edge": -0.40,
                "single_deviation": None,
                "bid_volatility_edge": 0.01,
                "offer_volatility_edge": -0.04,
                "bid_edge_ticks": 10.0,
                "offer_edge_ticks": -40.0,
                "best_action": "SELL",
                "best_edge": 0.10,
                "best_edge_ticks": 10.0,
                "forward": 86.88,
                "forward_source": "at_lng.cleared_oil",
                "forward_cob_date": "2026-08-16",
                "surface_cob_date": "2026-08-16",
                "surface_business_day_age": 1,
                "surface_source": "governed",
                "option_expiration_date": "2026-08-25",
                "pricing_model": "american_on_futures_futures_style",
                "pricing_model_version": "american_futures_crr_margin_style_v2",
                "outbound_channel": "sendMessage",
                "outbound_status": "acknowledged",
                "request_seq_id": 4,
                "outbound_acknowledged_at": "2026-08-17T08:00:03Z",
                "outbound_error_code": None,
                "outbound_error_message": None,
            }
        ]
    )
    return quotes._serialize_frame(frame)


def test_layout_has_one_h1_filters_polling_chart_and_stable_grid():
    items = list(_walk(quotes.layout))
    headings = [item for item in items if isinstance(item, html.H1)]
    ids = {getattr(item, "id", None) for item in items}
    interval = next(item for item in items if getattr(item, "id", None) == "ice-chat-refresh-interval")
    grid = next(item for item in items if getattr(item, "id", None) == "ice-chat-quote-grid")
    assert len(headings) == 1
    assert headings[0].children == "ICE Chat quotes"
    assert "Executable option edge against our saved volatility marks" not in {
        item for item in items if isinstance(item, str)
    }
    assert not any(isinstance(item, html.Details) for item in items)
    assert "More filters" not in {
        item for item in items if isinstance(item, str)
    }
    assert interval.interval == 10_000
    signal_placeholder = next(
        item for item in items if getattr(item, "id", None) == "ice-chat-signal-summary"
    )
    assert signal_placeholder.hidden is True
    assert "Current signal" not in {
        item for item in items if isinstance(item, str)
    }
    assert "Broker quotes executable edge" in {
        item for item in items if isinstance(item, str)
    }
    assert "Executable edge" not in {
        item for item in items if isinstance(item, str)
    }
    edge_card = next(
        item for item in items
        if getattr(item, "className", None) == "ice-chat-card ice-chat-edge-card"
    )
    assert edge_card is not None
    chart = next(item for item in items if getattr(item, "id", None) == "ice-chat-quote-chart")
    assert chart.config["displayModeBar"] == "hover"
    assert chart.config["modeBarButtonsToRemove"] == ["select2d", "lasso2d"]
    assert {
        "ice-chat-window",
        "ice-chat-contract",
        "ice-chat-option-type",
        "ice-chat-strike",
        "ice-chat-sender",
        "ice-chat-source-channel",
        "ice-chat-status-filter",
        "ice-chat-positive-only",
        "ice-chat-signal-summary",
        "ice-chat-quote-chart",
        "ice-chat-chart-title",
        "ice-chat-chart-hint",
        "ice-chat-quote-grid",
    }.issubset(ids)
    assert grid.dashGridOptions["getRowId"]["function"] == "params.data.event_id"
    assert grid.dashGridOptions["ariaLabel"] == "ICE Chat option quote tape"
    assert grid.defaultColDef["filter"] is False
    assert grid.defaultColDef["suppressHeaderMenuButton"] is True
    assert grid.defaultColDef["suppressHeaderFilterButton"] is True
    assert getattr(grid, "columnSize", None) is None
    assert grid.style == {"height": "180px"}
    assert grid.dashGridOptions["pagination"] is False
    assert grid.dashGridOptions["suppressPaginationPanel"] is True
    assert grid.dashGridOptions["suppressMovableColumns"] is True
    assert "processUnpinnedColumns" not in grid.dashGridOptions
    assert grid.dashGridOptions["rowSelection"]["checkboxes"] is False
    fields = _column_fields(grid.columnDefs)
    assert fields[:10] == [
        "product_label",
        "contract_label",
        "option_label",
        "strike",
        "observed_display",
        "price_unit_label",
        "bid_size",
        "bid",
        "offer",
        "offer_size",
    ]
    by_field = _columns_by_field(quotes.QUOTE_COLUMN_DEFS)
    assert by_field["product_label"]["pinned"] == "left"
    assert by_field["contract_label"]["pinned"] == "left"
    assert by_field["option_label"]["pinned"] == "left"
    assert by_field["strike"]["pinned"] == "left"
    assert all(
        "pinned" not in by_field[field]
        for field in ("observed_display", "price_unit_label")
    )
    assert fields.index("bid_size") < fields.index("bid") < fields.index("offer")
    assert fields.index("offer") < fields.index("offer_size") < fields.index("single_price")
    assert fields.index("offer") < fields.index("bid_iv_pct")
    assert "surface_business_day_age" not in fields
    assert {"source_channel", "processing_status", "display_error"}.isdisjoint(fields)
    market = next(
        definition for definition in quotes.QUOTE_COLUMN_DEFS
        if definition.get("headerName") == "Market"
    )
    assert all("USD/bbl" not in definition.get("headerName", "") for definition in quotes.QUOTE_COLUMN_DEFS)
    assert by_field["product_label"]["width"] == 72
    assert by_field["contract_label"]["width"] == 66
    assert by_field["observed_display"]["width"] == 112
    assert by_field["sender_handle"]["width"] == 110
    assert by_field["product_label"].get("pinned") == "left"
    assert by_field["contract_label"].get("pinned") == "left"
    assert by_field["option_label"].get("pinned") == "left"
    assert by_field["strike"].get("pinned") == "left"
    assert all(
        by_field[field].get("width")
        for field in ("bid", "bid_size", "offer", "offer_size", "single_price", "single_size")
    )
    assert {"product_label", "contract_label", "option_label", "strike", "price_unit_label", "signal_price_edge"}.issubset(fields)
    assert [definition["headerName"] for definition in quotes.QUOTE_COLUMN_DEFS] == [
        "Instrument",
        "Context",
        "Market",
        "Market IV (%)",
        "Our valuation",
        "Edge",
        "Workflow",
        "Reference",
    ]
    assert [definition["headerName"] for definition in market["children"]] == [
        "Bid qty",
        "Bid",
        "Offer",
        "Offer qty",
        "Single",
        "Qty",
    ]
    assert "ice-chat-row-blocked" in grid.dashGridOptions["rowClassRules"]
    signal_group = next(
        definition for definition in quotes.QUOTE_COLUMN_DEFS
        if definition.get("headerName") == "Edge"
    )
    assert [definition["headerName"] for definition in signal_group["children"]] == [
        "Action",
        "Margin",
        "Vol pts",
    ]
    assert "signal_edge_ticks" not in fields
    action_column = next(
        definition for definition in signal_group["children"]
        if definition.get("field") == "signal_label"
    )
    assert "ice-chat-cell-buy" in action_column["cellClassRules"]


def test_route_and_navigation_are_registered():
    assert index_options.display_page("/ice_chat_quotes", None) is quotes.layout
    links = [item for item in _walk(index_options.nav_links) if isinstance(item, dcc.Link)]
    ice_link = next(link for link in links if link.children == "ICE Quotes")
    assert ice_link.href == "/ice_chat_quotes"
    assert ice_link.id == "nav-ice-chat-quotes"


def test_serialization_filtering_and_all_quote_figure_preserve_trader_signs():
    rows = _rows()
    assert rows[0]["product_code"] == "B"
    assert rows[0]["product_label"] == "Brent"
    assert rows[0]["currency_code"] == "USD"
    assert rows[0]["price_unit"] == "bbl"
    assert rows[0]["price_unit_label"] == "USD/bbl"
    assert rows[0]["instrument_label"] == "Oct-26 · 90 Call"
    assert rows[0]["contract_label"] == "Oct-26"
    assert rows[0]["option_label"] == "Call"
    assert rows[0]["tick_size"] == 0.01
    assert rows[0]["price_decimals"] == 2
    assert rows[0]["our_iv_pct"] == 58.0
    assert rows[0]["bid_iv_edge_pp"] == 1.0
    assert rows[0]["single_edge_ticks"] is None
    assert rows[0]["signal_label"] == "SELL"
    assert rows[0]["signal_edge_ticks"] == 10.0
    assert rows[0]["signal_price_edge"] == 0.1
    assert rows[0]["signal_iv_edge_pp"] == 1.0
    frame = quotes.filter_quote_rows(rows, positive_only=True)
    assert len(frame) == 1
    assert frame.iloc[0]["best_action"] == "SELL"
    figure = quotes.build_all_quotes_figure(frame)
    names = [trace.name for trace in figure.data]
    assert "Sell to bid" in names
    assert "Buy at offer" in names
    bid_trace = next(trace for trace in figure.data if trace.name == "Sell to bid")
    assert list(bid_trace.y) == [1.0]
    assert bid_trace.marker.color == "#b42318"
    assert list(bid_trace.marker.size) == [13]
    assert figure.layout.height == 320
    assert figure.layout.yaxis.title.text == "Vol edge vs our mark (vol pts)"
    assert figure.layout.xaxis.title.text is None
    assert figure.layout.xaxis.showgrid is False
    assert "yaxis2" not in figure.layout
    assert figure.layout.yaxis.range[0] < 0.0 < figure.layout.yaxis.range[1]
    assert [shape.type for shape in figure.layout.shapes] == ["rect", "rect", "line"]
    assert any(annotation.text == "OUR VOL" for annotation in figure.layout.annotations)
    assert "Broker quote:" in bid_trace.hovertemplate
    assert all(
        "Actionable" not in str(getattr(annotation, "text", ""))
        for annotation in figure.layout.annotations
    )
    assert float(figure.layout.shapes[-1].y0) == 0.0


def test_explicit_quote_convention_controls_single_quote_ticks():
    frame = pd.DataFrame(_rows())
    frame.loc[0, ["product_code", "product_label", "currency_code", "price_unit"]] = [
        "T",
        "TTF Gas",
        "EUR",
        "MWh",
    ]
    frame.loc[0, "tick_size"] = 0.005
    frame.loc[0, ["bid", "offer", "bid_edge_ticks", "offer_edge_ticks"]] = None
    frame.loc[0, ["single_price", "single_deviation"]] = [1.25, 0.015]
    frame.loc[0, "best_action"] = None
    row = quotes._serialize_frame(frame)[0]
    assert row["product_label"] == "TTF Gas"
    assert row["price_unit_label"] == "EUR/MWh"
    assert row["price_decimals"] == 3
    assert row["signal_label"] == "SINGLE"
    assert row["single_edge_ticks"] == 3.0
    assert row["signal_price_edge"] == 0.015


def test_database_uuid_row_ids_are_serialized_for_dash_json():
    frame = pd.DataFrame(_rows())
    frame["event_id"] = [uuid.UUID("00000000-0000-4000-8000-000000000001")]
    assert quotes._serialize_frame(frame)[0]["event_id"] == (
        "00000000-0000-4000-8000-000000000001"
    )


def test_selected_instrument_shows_premium_and_iv_history():
    rows = _rows()
    frame = pd.DataFrame(rows)
    figure = quotes.build_instrument_figure(frame, rows[0])
    names = [trace.name for trace in figure.data]
    assert {"Bid", "Offer", "Our theo", "Bid IV", "Offer IV", "Our IV"}.issubset(names)
    assert figure.layout.yaxis.title.text == "Premium (USD/bbl)"

    other_product = dict(rows[0])
    other_product.update(
        event_id="event-2",
        product_code="T",
        product_label="TTF Gas",
        currency_code="EUR",
        price_unit="MWh",
        price_unit_label="EUR/MWh",
    )
    separated = quotes.build_instrument_figure(
        pd.DataFrame([rows[0], other_product]), rows[0]
    )
    bid_trace = next(trace for trace in separated.data if trace.name == "Bid")
    assert len(bid_trace.x) == 1
    empty = quotes.build_instrument_figure(pd.DataFrame(), rows[0])
    assert empty.layout.annotations[0].text == "Selected instrument has no visible history"


def test_empty_and_error_states_are_explicit():
    strip = quotes.build_service_strip(
        {},
        error="Database unavailable",
        loaded_at="2026-08-17T08:00:00+00:00",
    )
    assert "ice-chat-service-strip-danger" in strip.className
    assert len(strip.children) == 4
    figure = quotes.build_all_quotes_figure(pd.DataFrame())
    assert figure.layout.annotations[0].text == "No ICE Chat quotes in the selected window"


def test_grid_height_is_compact_for_short_tapes_and_bounded_for_long_tapes():
    rows = _rows()
    snapshot = {
        "rows": rows,
        "error": None,
        "loaded_at": "2026-08-17T08:00:00+00:00",
        "truncated": False,
    }
    result = quotes.render_quote_dashboard(
        snapshot, {}, None, None, None, None, None, None, [], []
    )
    assert result[3] == {"height": "180px"}
    assert result[5] == "Broker quotes executable edge"
    assert result[6] == "Above 0 favors us on bid/offer · single quotes are neutral"

    selected_result = quotes.render_quote_dashboard(
        snapshot, {}, None, None, None, None, None, None, [], [rows[0]]
    )
    assert selected_result[5] == "Instrument quote history"
    assert selected_result[6] == "Oct-26 · 90 Call · broker premium and IV vs our marks"

    snapshot["rows"] = [dict(rows[0], event_id=f"event-{index}") for index in range(20)]
    result = quotes.render_quote_dashboard(
        snapshot, {}, None, None, None, None, None, None, [], []
    )
    assert result[3] == {"height": "418px"}


def test_css_is_page_scoped_and_has_responsive_states():
    css = (Path(__file__).resolve().parents[1] / "assets" / "ice_chat_quotes.css").read_text(
        encoding="utf-8"
    )
    assert ".ice-chat-quotes-page" in css
    assert ".ice-chat-signal-card" not in css
    assert ".ice-chat-cell-stale" not in css
    assert ".ice-chat-group-edge" in css
    assert ".ice-chat-edge-positive" in css
    assert ".ice-chat-edge-card" in css
    assert ".ice-chat-chart-shell" in css
    assert ".ice-chat-grid-shell" in css
    assert "max-width: none" in css
    assert "max-width: 1540px" not in css
    assert ".ice-chat-advanced-filters" not in css
    assert "@media (max-width: 1100px)" in css
    assert "@media (max-width: 640px)" in css
    assert "@media (max-width: 360px)" in css


def test_live_empty_database_contract_loads_without_recalculation():
    result = quotes.load_quote_snapshot(
        "today",
        now=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
    )
    assert result.error is None
    assert isinstance(result.rows, list)
    assert result.service["surface_cob_date"]
