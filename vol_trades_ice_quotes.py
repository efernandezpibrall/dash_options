"""Normalize monthly outright ICE events once before expiry-chart rendering."""

from __future__ import annotations

import pandas as pd

from vol_trades_market_window import filter_event_window


def prepare_overlay_events(rows, product, window, graphs):
    counts = {key: 0 for key in (
        "outside_window", "unsupported_instrument", "invalid_instrument",
        "outside_display", "expiry_mismatch", "superseded", "blocked_valuation",
    )}
    frame = pd.DataFrame(rows)
    if frame.empty or product not in {"BRENT", "TFO"}:
        return {}, counts
    codes = frame.get("product_code", pd.Series("B", index=frame.index)).fillna("B").astype(str).str.upper()
    frame = frame.loc[codes.isin({"B", "BRENT"} if product == "BRENT" else {"TFM", "TFO"})].copy()
    in_window = filter_event_window(frame, "observed_at", window)
    counts["outside_window"] = len(frame) - len(in_window)
    frame = in_window
    if frame.empty:
        return {}, counts
    month = pd.to_datetime(frame.get("contract_month", pd.Series(index=frame.index, dtype=object)), errors="coerce")
    frame["_month"] = month.dt.strftime("%Y-%m-%d")
    monthly_label = month.dt.strftime("%b-%y")
    structure = frame.get("structure_code", pd.Series("", index=frame.index)).fillna("").ne("")
    strip = frame.get("strip_label", pd.Series("", index=frame.index)).fillna("")
    unsupported = structure | (strip.ne("") & strip.ne(monthly_label))
    counts["unsupported_instrument"] = int(unsupported.sum())
    frame = frame.loc[~unsupported].copy()
    if frame.empty:
        return {}, counts
    for name in ("option_type", "sender_handle", "source_channel", "event_id"):
        if name not in frame:
            frame[name] = ""
    frame["option_type"] = frame["option_type"].fillna("").str.upper()
    frame["strike"] = pd.to_numeric(frame.get("strike", pd.Series(index=frame.index, dtype=float)), errors="coerce")
    valid = frame["_month"].notna() & frame["option_type"].isin(["C", "P"]) & frame["strike"].gt(0) & frame["strike"].lt(float("inf"))
    counts["invalid_instrument"] = int((~valid).sum())
    frame = frame.loc[valid].copy()
    shown = frame["_month"].isin(graphs)
    counts["outside_display"] = int((~shown).sum())
    frame = frame.loc[shown].copy()
    if frame.empty:
        return {}, counts
    frame["_expiry"] = pd.to_datetime(frame.get("option_expiration_date", pd.Series(index=frame.index, dtype=object)), errors="coerce").dt.strftime("%Y-%m-%d")
    expected = frame["_month"].map({key: value.get("option_expiration_date") for key, value in graphs.items()})
    matches = frame["_expiry"].notna() & (expected.isna() | frame["_expiry"].eq(expected))
    counts["expiry_mismatch"] = int((~matches).sum())
    frame = frame.loc[matches].copy()
    frame["_observed"] = pd.to_datetime(frame["observed_at"], utc=True)
    frame["event_id"] = frame["event_id"].fillna("").astype(str)
    key = ["_month", "_expiry", "option_type", "strike", "sender_handle", "source_channel"]
    latest = frame.sort_values(["_observed", "event_id"], ascending=False).drop_duplicates(key, keep="first")
    counts["superseded"] = len(frame) - len(latest)
    # Supersession happens before status/edge filtering: a blocked update must
    # never resurrect an older usable indication from the same broker.
    if "valuation_status" in latest:
        blocked = latest["valuation_status"].fillna("valued").ne("valued")
        counts["blocked_valuation"] = int(blocked.sum())
        latest = latest.loc[~blocked]
    groups = {}
    for month_key, group in latest.groupby("_month", sort=False):
        groups[month_key] = group.drop(columns=["_month", "_expiry", "_observed"]).to_dict("records")
    return groups, counts
