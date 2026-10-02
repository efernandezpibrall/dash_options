"""Normalize monthly outright ICE events once before expiry-chart rendering."""

from __future__ import annotations

import pandas as pd
import json

from vol_trades_market_window import filter_event_window


def quarter_season_identity(row):
    """Use explicit ICE delivery months; a label alone is insufficient coverage."""
    from options.ttf_strip_charts import strip_period

    months = row.get("strip_months") or []
    try:
        if isinstance(months, str):
            months = json.loads(months)
        return strip_period(months)
    except (ValueError, TypeError):
        return None


def prepare_strip_events(rows, product, window):
    """Select latest quarter/season outrights before status or table filters."""
    if product != "TFO":
        return {}
    frame = filter_event_window(pd.DataFrame(rows), "observed_at", window)
    if frame.empty:
        return {}
    eligible = []
    for row in frame.to_dict("records"):
        if str(row.get("product_code") or "").upper() not in {"TFM", "TFO"} or row.get("structure_code"):
            continue
        identity = quarter_season_identity(row)
        if identity is None:
            continue
        key, label, months = identity
        if str(row.get("contract_month") or "")[:10] != months[0].isoformat():
            continue
        row.update(_strip_key=key, _strip_label=label, _strip_months=months)
        eligible.append(row)
    if not eligible:
        return {}
    frame = pd.DataFrame(eligible)
    frame["_observed"] = pd.to_datetime(frame["observed_at"], utc=True)
    for field in ("event_id", "sender_handle", "source_channel", "option_type"):
        frame[field] = frame.get(field, pd.Series("", index=frame.index)).fillna("").astype(str)
    frame["strike"] = pd.to_numeric(frame.get("strike"), errors="coerce")
    latest = frame.sort_values(["_observed", "event_id"], ascending=False).drop_duplicates(
        ["_strip_key", "option_type", "strike", "sender_handle", "source_channel"], keep="first",
    )
    # A new blocked event suppresses its preceding usable broker quote.
    latest = latest.loc[latest.get("valuation_status", pd.Series("valued", index=latest.index)).fillna("valued").eq("valued")]
    latest = latest.loc[latest["option_type"].isin(["C", "P"]) & latest["strike"].gt(0) & latest["strike"].lt(float("inf"))]
    return {key: subset.drop(columns="_observed").to_dict("records") for key, subset in latest.groupby("_strip_key", sort=False)}


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
    recognized_strips = pd.Series(False, index=frame.index)
    if product == "TFO":
        recognized_strips = frame.apply(lambda row: not row.get("structure_code") and quarter_season_identity(row) is not None, axis=1)
    counts["quarter_season_events"] = int(recognized_strips.sum())
    counts["unsupported_instrument"] = int((unsupported & ~recognized_strips).sum())
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
