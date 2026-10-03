"""Trade tape for the Vol Trades workspace."""

from __future__ import annotations

from vol_trades_workspace import chart_data

from typing import Any
import numpy as np
import pandas as pd
import vol_trades_data as market_data
from options.trade_marker_opacity import trade_volume_opacities


_TRADE_TYPE_LABELS = {
    "regular": "Regular trade", "block": "Block trade",
    "system_priced": "System-priced trade", "split_leg": "Vertical split leg",
    "block_split_leg": "Block trade · vertical split leg", "unknown": "Unclassified trade",
}


def _trade_type_label(row):
    classification = row.get("trade_classification")
    if classification is None or pd.isna(classification):
        return str(row.get("condition_codes") or "Regular trade")
    return _TRADE_TYPE_LABELS.get(classification, str(classification))


def _trade_iv_label(row):
    lifecycle = row.get("trade_lifecycle")
    audit_labels = {"cancelled": "Cancelled", "cancellation": "Cancellation record",
                    "superseded": "Superseded", "unverified_correction": "Correction pending"}
    if lifecycle in audit_labels:
        return audit_labels[lifecycle]
    status = row.get("trade_iv_status")
    if status == "resolved":
        return {"system_priced": "System-priced IV", "split_leg": "Split-leg IV",
                "block_split_leg": "Split-leg IV", "block": "Block trade IV"}.get(
                    row.get("trade_classification"), "IV calculated")
    reason = str(row.get("trade_iv_exclusion_reason") or "").lower()
    if "future mid" in reason:
        return "Underlying match unavailable"
    if "execution timestamp" in reason:
        return "Execution time unavailable"
    if "condition" in reason and row.get("policy_version") == "tfo-execution-trade-iv-v1":
        return "Condition review"
    return "IV unavailable" if status == "unresolved" else "Excluded (legacy policy)"





def _seconds_since_midnight_gst(values: pd.Series) -> pd.Series:
    localized = pd.to_datetime(values, errors="coerce", utc=True).dt.tz_convert(
        "Asia/Dubai"
    )
    return (
        localized.dt.hour * 3600
        + localized.dt.minute * 60
        + localized.dt.second
        + localized.dt.microsecond / 1_000_000
    )


def filter_trade_window(
    trade_tape: pd.DataFrame,
    start_second: float | int | None,
) -> pd.DataFrame:
    if trade_tape is None or trade_tape.empty:
        return pd.DataFrame(columns=(trade_tape.columns if trade_tape is not None else []))
    start = max(0.0, float(start_second or 0.0))
    seconds = _seconds_since_midnight_gst(trade_tape["trade_at"])
    return trade_tape.loc[seconds.ge(start)].copy()


def _trade_event_axis_x(row: pd.Series, x_axis: str) -> float:
    if chart_data._normalize_x_axis(x_axis) == chart_data.X_AXIS_STRIKE:
        return float(row["strike"])
    business_date = pd.Timestamp(row["business_date"]).date()
    expiration = pd.Timestamp(row["option_expiration_date"]).date()
    dte = (expiration - business_date).days
    return chart_data._delta_x_from_market_inputs(
        strike=row.get("strike"),
        forward=row.get("future_match_price"),
        volatility=row.get("trade_iv"),
        dte=dte,
        put_call=row.get("put_call"),
    )




def _trade_quote_age_seconds(trade_at: Any, quote_at: Any) -> float | None:
    trade_time = pd.to_datetime(trade_at, errors="coerce", utc=True)
    quote_time = pd.to_datetime(quote_at, errors="coerce", utc=True)
    if pd.isna(trade_time) or pd.isna(quote_time):
        return None
    age = float((trade_time - quote_time).total_seconds())
    return age if age >= 0.0 else None


def _trade_quote_age_label(
    trade_at: Any,
    future_bid_at: Any,
    future_ask_at: Any,
) -> str:
    def format_age(value: float | None) -> str:
        return "—" if value is None else f"{value:.1f}s"

    bid_age = _trade_quote_age_seconds(trade_at, future_bid_at)
    ask_age = _trade_quote_age_seconds(trade_at, future_ask_at)
    if bid_age is None and ask_age is None:
        return "—"
    return f"B {format_age(bid_age)} / A {format_age(ask_age)}"


def trade_trace_payloads(
    trade_tape: pd.DataFrame,
    expiry: pd.Timestamp,
    x_axis: str,
    *,
    volume_reference: pd.DataFrame | None = None,
) -> dict[str, dict[str, Any]]:
    empty = {
        side: {
            "x": [], "y": [], "customdata": [], "size": [], "symbol": [],
            "line_color": [], "line_width": [], "opacity": [],
        }
        for side in ("C", "P")
    }
    if trade_tape is None or trade_tape.empty:
        return empty
    selected = trade_tape.loc[
        chart_data._expiry_mask(trade_tape, expiry, "underlying_contract_month")
        & trade_tape["trade_iv_status"].astype(str).eq("resolved")
        & pd.to_numeric(trade_tape["trade_iv"], errors="coerce").notna()
    ].copy()
    if selected.empty:
        return empty
    selected["_axis_x"] = selected.apply(
        _trade_event_axis_x, axis=1, x_axis=x_axis
    )
    selected = selected.loc[selected["_axis_x"].notna()].sort_values(
        ["trade_at", "option_security", "occurrence_ordinal"]
    )
    if selected.empty:
        return empty
    reference = trade_tape if volume_reference is None else volume_reference
    reference_sizes = reference.get("trade_size", pd.Series(dtype=float))
    selected["trade_time_gst"] = pd.to_datetime(
        selected["trade_at"], errors="coerce", utc=True
    ).dt.tz_convert("Asia/Dubai").dt.strftime("%H:%M:%S GST")
    selected["reported_time_gst"] = pd.to_datetime(
        selected.get("reported_at", selected["trade_at"]), errors="coerce", utc=True
    ).dt.tz_convert("Asia/Dubai").dt.strftime("%H:%M:%S GST")
    selected["trade_type_label"] = selected.apply(_trade_type_label, axis=1)
    selected["trade_iv_label"] = selected.apply(_trade_iv_label, axis=1)
    selected["time_basis_label"] = selected.get(
        "execution_at", pd.Series(pd.NaT, index=selected.index)
    ).map(lambda value: "Executed" if pd.notna(value) else "Reported (legacy)")
    result = dict(empty)
    for put_call in ("C", "P"):
        side = selected.loc[selected["put_call"].eq(put_call)].copy()
        if side.empty:
            continue
        sizes = pd.to_numeric(side["trade_size"], errors="coerce")
        size_labels = [
            f"{value:,.0f}" if pd.notna(value) and np.isfinite(value) and value > 0
            else "Size unavailable"
            for value in sizes
        ]
        source = side["future_match_source"].fillna("").astype(str).str.upper()
        source_labels = source.map(chart_data._trade_match_source_label)
        future_bid_times = side.get(
            "future_bid_at", pd.Series(pd.NaT, index=side.index)
        )
        future_ask_times = side.get(
            "future_ask_at", pd.Series(pd.NaT, index=side.index)
        )
        quote_age_labels = [
            _trade_quote_age_label(trade_at, bid_at, ask_at)
            for trade_at, bid_at, ask_at in zip(
                side["trade_at"], future_bid_times, future_ask_times
            )
        ]
        result[put_call] = {
            "x": side["_axis_x"].astype(float).tolist(),
            "y": (100.0 * pd.to_numeric(side["trade_iv"], errors="coerce")).tolist(),
            "customdata": np.column_stack(
                [
                    side["strike"], side["trade_price"], side["trade_size"],
                    side["trade_time_gst"], side["future_match_price"],
                    source_labels, side["future_match_lag_ms"],
                    quote_age_labels,
                    side["condition_codes"].fillna("regular"),
                    size_labels,
                    side["trade_type_label"], side["reported_time_gst"], side["trade_iv_label"],
                    side["time_basis_label"],
                ]
            ).tolist(),
            "size": [6] * len(side),
            "symbol": ["circle" if put_call == "C" else "circle-open"] * len(side),
            "opacity": trade_volume_opacities(sizes, reference_volumes=reference_sizes),
            "line_color": ["#FFFFFF" if put_call == "C" else "#0F766E"] * len(side),
            "line_width": [0.5 if put_call == "C" else 1.0] * len(side),
        }
    return result


def _trade_tape_rows(
    trade_tape: pd.DataFrame,
    expiry_value: str | None,
    start_second: float | int | None,
) -> list[dict[str, Any]]:
    if trade_tape is None or trade_tape.empty or not expiry_value:
        return []
    selected = filter_trade_window(trade_tape, start_second)
    expiry = pd.Timestamp(expiry_value).normalize()
    selected = selected.loc[
        chart_data._expiry_mask(selected, expiry, "underlying_contract_month")
    ].sort_values("trade_at", ascending=False)
    rows = []
    for row in selected.itertuples(index=False):
        trade_at = pd.Timestamp(row.trade_at).tz_convert("Asia/Dubai")
        reported = pd.to_datetime(getattr(row, "reported_at", row.trade_at), errors="coerce", utc=True)
        executed = pd.to_datetime(getattr(row, "execution_at", None), errors="coerce", utc=True)
        quote_age_label = _trade_quote_age_label(
            row.trade_at,
            getattr(row, "future_bid_at", None),
            getattr(row, "future_ask_at", None),
        )
        rows.append(
            {
                "event_id": f"{row.event_fingerprint}:{int(row.occurrence_ordinal)}",
                "trade_time_gst": trade_at.strftime("%H:%M:%S.%f")[:-3],
                "execution_time_gst": None if pd.isna(executed) else executed.tz_convert("Asia/Dubai").strftime("%H:%M:%S.%f")[:-3],
                "reported_time_gst": None if pd.isna(reported) else reported.tz_convert("Asia/Dubai").strftime("%H:%M:%S.%f")[:-3],
                "trade_type_label": _trade_type_label(row._asdict()),
                "trade_iv_label": _trade_iv_label(row._asdict()),
                "option_security": row.option_security,
                "put_call": row.put_call,
                "strike": market_data._numeric_or_none(row.strike),
                "trade_price": market_data._numeric_or_none(row.trade_price),
                "trade_size": market_data._numeric_or_none(row.trade_size),
                "condition_codes": row.condition_codes or "regular",
                "future_match_price": market_data._numeric_or_none(row.future_match_price),
                "future_match_source": chart_data._trade_match_source_label(
                    row.future_match_source
                ),
                "future_match_lag_ms": market_data._numeric_or_none(row.future_match_lag_ms),
                "future_quote_ages": quote_age_label,
                "trade_iv_pct": (
                    None if market_data._numeric_or_none(row.trade_iv) is None
                    else 100.0 * float(row.trade_iv)
                ),
                "trade_iv_status": row.trade_iv_status,
                "trade_iv_exclusion_reason": row.trade_iv_exclusion_reason,
            }
        )
    return rows
