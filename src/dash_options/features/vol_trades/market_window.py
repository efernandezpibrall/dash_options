"""Shared, timezone-aware event windows for Vol Trades and ICE observations."""

from __future__ import annotations

import hashlib
import json

import pandas as pd


MARKET_TIMEZONE = "Asia/Dubai"
LIVE_QUOTE_PRODUCTS = frozenset({"BRENT", "TFO"})
PRESET_SECONDS = {"all": None, "4h": 14400, "1h": 3600, "15m": 900}


def utc_timestamp(value):
    return pd.to_datetime(value, errors="coerce", utc=True)


def history_identity(history):
    history = history or {}
    fields = {key: history.get(key) for key in (
        "product", "snapshot_id", "business_date", "snapshot_kind", "observed_at", "is_latest",
    )}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()[:20]


def market_context(history, *, now=None):
    """Latest supported views follow the market day; older selections stay frozen."""
    if not history:
        return None
    current = utc_timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    observed = utc_timestamp(history.get("observed_at"))
    day = pd.to_datetime(history.get("business_date"), errors="coerce")
    if pd.isna(current) or pd.isna(day):
        return None
    live = history.get("product") in LIVE_QUOTE_PRODUCTS and history.get("is_latest", False)
    local_day = current.tz_convert(MARKET_TIMEZONE).normalize() if live else pd.Timestamp(day.date(), tz=MARKET_TIMEZONE)
    day_start = local_day.tz_convert("UTC")
    day_end = (local_day + pd.Timedelta(days=1)).tz_convert("UTC") - pd.Timedelta(microseconds=1)
    if live:
        cutoff = current
    elif pd.notna(observed):
        cutoff = min(observed, day_end)
    else:
        return None
    cutoff = min(cutoff, current)
    if cutoff < day_start:
        return None
    return {
        "history_key": history_identity(history),
        "product": history.get("product"),
        "market_date": local_day.date().isoformat(),
        "day_start_at": day_start.isoformat(),
        "cutoff_at": cutoff.isoformat(),
        "mode": "live" if live else "snapshot",
        "reference_date": str(history.get("business_date") or ""),
    }


def select_market_window(context, *, previous=None, preset=None, manual_start=None):
    if not context:
        return None
    previous = previous or {}
    same_day = (previous.get("product"), previous.get("market_date")) == (
        context.get("product"), context.get("market_date"),
    )
    maximum = max(0.0, (utc_timestamp(context["cutoff_at"]) - utc_timestamp(context["day_start_at"])).total_seconds())
    selected = preset or (previous.get("preset", "all") if same_day else "all")
    if manual_start is not None:
        selected = "manual"
        start = float(manual_start)
    elif selected == "manual":
        start = float(previous.get("start", 0))
    else:
        lookback = PRESET_SECONDS.get(selected)
        start = 0.0 if lookback is None else maximum - lookback
    start = min(maximum, max(0.0, start))
    return {
        **context,
        "preset": selected,
        "start": start,
        "maximum": maximum,
        "start_at": (utc_timestamp(context["day_start_at"]) + pd.Timedelta(seconds=start)).isoformat(),
    }


def filter_event_window(frame, timestamp_column, window):
    if frame.empty or not window or timestamp_column not in frame:
        return frame.iloc[0:0].copy()
    start, cutoff = utc_timestamp(window.get("start_at")), utc_timestamp(window.get("cutoff_at"))
    if pd.isna(start) or pd.isna(cutoff):
        return frame.iloc[0:0].copy()
    timestamps = pd.to_datetime(frame[timestamp_column], errors="coerce", utc=True)
    return frame.loc[timestamps.ge(start) & timestamps.le(cutoff)].copy()


def quote_table_rows(snapshot):
    """The tape's lookback is independent of the shared expiry-chart window."""
    snapshot = snapshot or {}
    rows = snapshot.get("rows") or []
    cutoff = snapshot.get("table_cutoff_at")
    if not cutoff:
        return rows
    frame = filter_event_window(pd.DataFrame(rows), "observed_at", {
        "start_at": cutoff, "cutoff_at": snapshot.get("loaded_at"),
    })
    return frame.to_dict("records")
