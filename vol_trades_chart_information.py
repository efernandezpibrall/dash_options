"""Compact quote coverage and observed tape activity for expiry headers."""

import pandas as pd
from dash import html, no_update
import dash_bootstrap_components as dbc


def expiry_information(quality, raw, trade_tape):
    quality = dict(quality)
    detail = quality.get("detail", "")
    approved = raw.get("executable_iv_status", pd.Series(index=raw.index, dtype=object)).eq("resolved")
    approved &= pd.to_numeric(raw.get("executable_iv_mid", pd.Series(index=raw.index, dtype=float)), errors="coerce").gt(0)
    executable_count = int(approved.sum())
    # Keep the header small; precise failures and tape activity live in its title.
    reasons = raw.get("executable_iv_exclusion_reason", pd.Series(index=raw.index, dtype=object)).fillna("").astype(str)
    pairs = pd.to_numeric(raw.get("option_bid", pd.Series(index=raw.index, dtype=float)), errors="coerce").gt(0) & pd.to_numeric(raw.get("option_ask", pd.Series(index=raw.index, dtype=float)), errors="coerce").gt(0)
    if executable_count:
        band_label = "Bid/ask"
    elif reasons.str.contains("crossed_quote", regex=False).any():
        band_label = "Crossed quotes"
    elif not pairs.any():
        band_label = "No bid/ask"
    elif reasons.str.contains("future.*missing|corresponding future.*crossed", regex=True).any():
        band_label = "No future quote"
    else:
        band_label = "Quotes filtered"
    failures = reasons[~approved & reasons.ne("")].value_counts()
    readable = {
        "missing_two_sided_quote": "option bid/ask unavailable",
        "crossed_quote": "crossed option bid/ask",
    }
    detail += f" · Bloomberg band: {executable_count:,} approved option quotes"
    if not failures.empty:
        detail += "; " + "; ".join(f"{readable.get(reason, reason)} ({count})" for reason, count in failures.items())
    activity = []
    active_count = unresolved_count = 0
    lots = None
    latest = None
    if trade_tape is not None:
        active = trade_tape.copy()
        lifecycle = active.get("trade_lifecycle", pd.Series(index=active.index, dtype=object))
        regular = active.get("is_regular", pd.Series(False, index=active.index)).fillna(False)
        active = active.loc[lifecycle.eq("active") | (lifecycle.isna() & regular)]
        active_count = len(active)
        unresolved_count = int(active.get("trade_iv_status", pd.Series(index=active.index, dtype=object)).ne("resolved").sum())
        sizes = pd.to_numeric(active.get("trade_size", pd.Series(index=active.index, dtype=float)), errors="coerce")
        lots = float(sizes.sum()) if not active.empty and sizes.notna().all() else None
        latest = pd.to_datetime(active.get("trade_at", pd.Series(index=active.index, dtype=object)), errors="coerce", utc=True).max()
        activity.append(f"{active_count:,} {'trade' if active_count == 1 else 'trades'}")
        if lots is not None:
            activity.append(f"{lots:,.0f} lots")
        detail += f" · Recorded active prints: {active_count:,}; IV unavailable: {unresolved_count:,}"
        if pd.notna(latest):
            detail += f"; latest execution {latest.tz_convert('Asia/Dubai'):%H:%M:%S} GST"
        if not trade_tape.empty and "coverage_status" in trade_tape:
            detail += f"; tape coverage {trade_tape['coverage_status'].iloc[0]}"
    summary = " · ".join(activity) or (f"{executable_count:,} quoted options" if executable_count else "")
    return {**quality, "detail": detail.strip(" ·"), "band_label": band_label, "summary": summary,
            "active_trade_count": active_count, "active_trade_lots": lots,
            "unresolved_trade_count": unresolved_count,
            "latest_execution_at": None if pd.isna(latest) else latest.isoformat()}


def information_children(quality):
    return [dbc.Badge(quality["band_label"], color="success" if quality["band_label"] == "Bid/ask" else "secondary",
                      pill=True, className="brent-vol-history-quality-badge"),
            html.Span(quality["summary"], className="brent-vol-history-quality-detail")]


def expiry_information_updates(snapshot, window, ids):
    from vol_trades_market_window import history_identity, filter_event_window
    import vol_trades_data as data
    if not ids:
        return [], []
    if not snapshot or snapshot.get("product") != "TFO" or snapshot.get("snapshot_kind") != "INTRADAY" or not window or window.get("history_key") != history_identity(snapshot):
        return [no_update] * len(ids), [no_update] * len(ids)
    chain = data.load_chain_snapshot(snapshot["snapshot_id"], product="TFO", snapshot_kind="INTRADAY")
    tape = filter_event_window(data.load_trade_tape(snapshot["snapshot_id"], product="TFO"), "trade_at", window)
    children, titles = [], []
    for identity in ids:
        month = pd.Timestamp(identity["expiry"])
        raw = chain.loc[chain["underlying_contract_month"].eq(month)]
        trades = tape.loc[tape["underlying_contract_month"].eq(month)] if not tape.empty else tape
        info = expiry_information({}, raw, trades)
        children.append(information_children(info))
        titles.append(info["detail"])
    return children, titles
