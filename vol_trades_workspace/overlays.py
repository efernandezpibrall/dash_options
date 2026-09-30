"""Overlays for the Vol Trades workspace."""

from __future__ import annotations

from vol_trades_workspace import chart_data, quote_charts

import math
from html import escape
import numpy as np
import pandas as pd
from dash import (
    Patch,
    no_update,
)
from vol_trades_market_window import (
    MARKET_TIMEZONE, history_identity,
    market_context, select_market_window, utc_timestamp,
)
from vol_trades_ice_quotes import prepare_overlay_events
import vol_trades_data as market_data
import ice_quote_data as quote_data

ICE_QUOTE_LAYERS = {
    "ice-bid": ("bid", "bid_implied_volatility"),
    "ice-offer": ("offer", "offer_implied_volatility"),
    "ice-single": ("single_price", "single_implied_volatility"),
}


def ice_quote_overlay_points(
    rows: list[dict],
    snapshot: dict | None,
    expiry: str,
    x_axis: str,
    option_expiration_date: str | None = None,
    market_window: dict | None = None,
    diagnostics: dict | None = None,
) -> tuple[dict[str, dict], int]:
    """Project persisted IVs using the shared market window and quote-time inputs."""
    empty = {layer: {"x": [], "y": [], "text": [], "opacity": [], "symbol": []}
             for layer in ICE_QUOTE_LAYERS}
    product_code = {"BRENT": "B", "TFO": "TFM"}.get((snapshot or {}).get("product"))
    if product_code is None:
        return empty, 0
    window = market_window or select_market_window(market_context(snapshot, now=snapshot.get("observed_at")))
    if not window or window.get("product") != snapshot.get("product"):
        return empty, 0
    start, cutoff = utc_timestamp(window.get("start_at")), utc_timestamp(window.get("cutoff_at"))
    if pd.isna(start) or pd.isna(cutoff):
        return empty, 0
    diagnostics = diagnostics if diagnostics is not None else {}
    omitted = 0
    for row in rows:
        row_code = str(row.get("product_code") or "").upper()
        if row_code not in ({"B", "BRENT"} if product_code == "B" else {"TFM", "TFO"}):
            continue
        # A strip flat IV is not an observation on its first monthly smile.
        month = pd.to_datetime(row.get("contract_month"), errors="coerce")
        monthly_label = month.strftime("%b-%y") if pd.notna(month) else None
        if row.get("structure_code"):
            omitted += 1
            continue
        if row.get("strip_label") and row.get("strip_label") != monthly_label:
            continue
        if str(row.get("contract_month") or "")[:10] != expiry:
            continue
        if (
            option_expiration_date
            and str(row.get("option_expiration_date") or "")[:10]
            != option_expiration_date
        ):
            continue
        observed = pd.to_datetime(row.get("observed_at"), errors="coerce", utc=True)
        if pd.isna(observed) or observed > cutoff or observed < start:
            continue
        if row.get("valuation_status") not in {None, "valued"}:
            continue
        expected_currency, expected_unit = ("EUR", "MWH") if product_code == "TFM" else ("USD", "BBL")
        if (str(row.get("currency_code") or expected_currency).upper() != expected_currency
                or str(row.get("price_unit") or expected_unit).upper() != expected_unit):
            diagnostics["invalid_units"] = diagnostics.get("invalid_units", 0) + 1
            omitted += 1
            continue
        strike = market_data._numeric_or_none(row.get("strike"))
        side = str(row.get("option_type") or "").upper()
        if strike is None or side not in {"C", "P"}:
            omitted += 1
            continue
        for layer, (price_field, iv_field) in ICE_QUOTE_LAYERS.items():
            price = market_data._numeric_or_none(row.get(price_field))
            iv = market_data._numeric_or_none(row.get(iv_field))
            if price is None:
                continue
            if iv is None or iv <= 0:
                omitted += 1
                diagnostics["missing_iv"] = diagnostics.get("missing_iv", 0) + 1
                continue
            x = strike
            if chart_data._normalize_x_axis(x_axis) == chart_data.X_AXIS_DELTA:
                expiration = pd.to_datetime(
                    row.get("option_expiration_date"), errors="coerce"
                )
                dte = (
                    (expiration.date() - observed.tz_convert("Asia/Dubai").date()).days
                    if pd.notna(expiration) else None
                )
                x = chart_data._delta_x_from_market_inputs(
                    strike=strike, forward=row.get("forward"),
                    volatility=iv, dte=dte, put_call=side,
                )
                if not np.isfinite(x):
                    omitted += 1
                    diagnostics["missing_delta"] = diagnostics.get("missing_delta", 0) + 1
                    continue
            age_hours = max(0.0, math.floor((cutoff - observed).total_seconds() / 60.0) / 60.0)
            time_label = observed.tz_convert("Asia/Dubai").strftime("%d %b %Y %H:%M:%S GST")
            sender = escape(str(row.get("sender_handle") or "—"))
            channel = escape(str(row.get("source_channel") or "—"))
            surface = escape(str(row.get("surface_cob_date") or "—"))
            publication = escape(str(row.get("surface_publication_id") or "—"))
            forward_source = escape(str(row.get("forward_source") or "—"))
            label = ICE_QUOTE_LAYERS[layer][0].replace("_", " ").title()
            hover = (
                f"<b>ICE {label} · {'Call' if side == 'C' else 'Put'}</b>"
                f"<br>{time_label}<br>Strike {strike:.2f}"
                f" · Premium {price:.4f} {escape(str(row.get('price_unit_label') or market_data._product_spec(snapshot.get('product'))['price_unit']))}"
                f"<br>IV {100.0 * iv:.2f}% · Sender {sender}"
                f"<br>Channel {channel} · Surface COB {surface}"
                f"<br>Forward {escape(str(row.get('forward') or '—'))} · {forward_source}"
                f"<br>Publication {publication} · Quote event {escape(str(row.get('event_id') or '—'))}"
            )
            empty[layer]["x"].append(float(x))
            empty[layer]["y"].append(100.0 * iv)
            empty[layer]["text"].append(hover)
            empty[layer]["opacity"].append(max(0.70, 0.95 - age_hours / 24.0))
            symbol = {"ice-bid": "triangle-down", "ice-offer": "triangle-up", "ice-single": "diamond"}[layer]
            empty[layer]["symbol"].append(symbol + ("-open" if side == "P" else ""))
    return empty, omitted


def update_ice_quote_overlays(
    quote_snapshot, contract, option_type, strike, sender, source_channel,
    status, positive_only, history_snapshot, manifest, x_axis,
    selected_layers, graph_ids, market_window=None, relayout_data=None,
):
    if not graph_ids or not manifest:
        return [], ""
    quote_snapshot, history_snapshot = quote_snapshot or {}, history_snapshot or {}
    explicit_window = market_window is not None
    window = market_window or select_market_window(market_context(history_snapshot, now=history_snapshot.get("observed_at")))
    if explicit_window and window.get("history_key") != history_identity(history_snapshot):
        return [no_update for _ in graph_ids], "Waiting for the selected market window"
    quote_context = quote_snapshot.get("market_context")
    if quote_context and quote_context.get("history_key") != history_identity(history_snapshot):
        return [no_update for _ in graph_ids], "Loading ICE quotes for the selected market window"
    if (explicit_window and quote_context and window.get("mode") == "live"
            and not quote_snapshot.get("error")
            and quote_context.get("cutoff_at") != window.get("cutoff_at")):
        return [no_update for _ in graph_ids], "Updating the shared market window"
    graphs = manifest.get("graphs") or {}
    groups, counts = prepare_overlay_events(quote_snapshot.get("rows") or [], history_snapshot.get("product"), window, graphs)
    # The latest broker event is chosen before table status/edge filters.
    eligible_rows = [row for rows in groups.values() for row in rows]
    for row in eligible_rows:
        row.setdefault("option_filter_key", row.get("option_type"))
        row.setdefault("strike_filter_key", quote_data._compact_number(row.get("strike")))
        row.setdefault("contract_label", pd.Timestamp(row["contract_month"]).strftime("%b-%y"))
    filtered = quote_charts.filter_quote_rows(
        eligible_rows, product=history_snapshot.get("product"), contract=contract,
        option_type=option_type, strike=strike, sender=sender, source_channel=source_channel,
        status=status, positive_only="positive" in (positive_only or []),
    )
    groups = ({str(month)[:10]: subset.to_dict("records")
               for month, subset in filtered.groupby("contract_month", sort=False)}
              if not filtered.empty else {})
    selected = set(manifest.get("available_layers") or []) if selected_layers is None else set(selected_layers)
    updates, plotted, diagnostics = [], 0, {}
    for position, graph_id in enumerate(graph_ids):
        expiry = str((graph_id or {}).get("expiry") or "")
        graph_contract = graphs.get(expiry) or {}
        if graph_id.get("generation") and graph_id["generation"] != graph_contract.get("generation"):
            updates.append(no_update)
            continue
        points, _ = ice_quote_overlay_points(
            groups.get(expiry, []), history_snapshot, expiry, x_axis,
            graph_contract.get("option_expiration_date"), window, diagnostics,
        )
        patch = Patch()
        for entry in graph_contract.get("traces") or []:
            layer = entry["layer"]
            if layer not in ICE_QUOTE_LAYERS:
                continue
            index, data = int(entry["index"]), points[layer]
            plotted += len(data["x"])
            for key in ("x", "y", "text"):
                patch["data"][index][key] = data[key]
            for key in ("opacity", "symbol"):
                patch["data"][index]["marker"][key] = data[key]
            color = {"ice-bid": "#B42318", "ice-offer": "#067647", "ice-single": "#6941C6"}[layer]
            # Open put symbols need a colored outline; filled calls retain a white halo.
            patch["data"][index]["marker"]["line"]["color"] = [
                color if symbol.endswith("-open") else "#FFFFFF"
                for symbol in data["symbol"]
            ]
            patch["data"][index]["marker"]["line"]["width"] = [
                2 if symbol.endswith("-open") else 1
                for symbol in data["symbol"]
            ]
            patch["data"][index]["visible"] = layer in selected
        relayout = (relayout_data or [])[position] if position < len(relayout_data or []) else {}
        relayout = relayout or {}
        for axis, coordinate, range_key in (("yaxis", "y", "iv_range"), ("xaxis", "x", "strike_range")):
            if axis == "xaxis" and chart_data._normalize_x_axis(x_axis) != chart_data.X_AXIS_STRIKE:
                continue
            manual = (f"{axis}.range" in relayout or f"{axis}.range[0]" in relayout) and not relayout.get(f"{axis}.autorange", False)
            base = graph_contract.get(range_key)
            if base and not manual:
                values = [v for layer, data in points.items() if layer in selected for v in data[coordinate]]
                margin = max(0.5 if axis == "yaxis" else 0.1, (max(values) - min(values)) * 0.05) if values else 0
                patch["layout"][axis]["range"] = [
                    max(0.0, min(base[0], min(values) - margin)) if values else base[0],
                    max(base[1], max(values) + margin) if values else base[1],
                ]
        updates.append(patch)
    product = history_snapshot.get("product")
    if product not in quote_data.QUOTE_FEED_PRODUCTS:
        message = f"No ICE quote feed is configured for {quote_data.PRODUCT_LABELS[quote_data._selected_product(product)]}."
    elif quote_snapshot.get("error"):
        message = quote_snapshot["error"]
    else:
        message = f"ICE quote markers · {plotted:,} plotted"
        names = {
            "outside_window": "outside market window",
            "unsupported_instrument": "structure/strip events excluded",
            "invalid_instrument": "invalid instruments", "outside_display": "outside displayed expiries",
            "expiry_mismatch": "expiry mismatches", "superseded": "superseded events",
            "blocked_valuation": "blocked valuations", "missing_iv": "quote sides without IV",
            "missing_delta": "quote sides without delta", "invalid_units": "unit mismatches",
        }
        for key, label in names.items():
            count = counts.get(key, 0) + diagnostics.get(key, 0)
            if count:
                message += f" · {count:,} {label}"
        if quote_snapshot.get("truncated"):
            message += " · Quote coverage truncated at 10,000 events"
        if explicit_window:
            message += f" · Reference {window.get('reference_date')}"
            quote_cut = utc_timestamp((quote_context or {}).get("cutoff_at"))
            trade_cut = utc_timestamp((history_snapshot.get("trade_coverage") or {}).get("cutoff_at"))
            if pd.notna(quote_cut):
                message += f" · ICE read through {quote_cut.tz_convert(MARKET_TIMEZONE):%d %b %H:%M:%S} GST"
            if pd.notna(trade_cut):
                message += f" · Bloomberg tape through {trade_cut.tz_convert(MARKET_TIMEZONE):%d %b %H:%M:%S} GST"
            else:
                message += " · No Bloomberg trade tape in this reference"
        elif not plotted:
            message += " for the selected snapshot date and quote window"
    return updates, message

