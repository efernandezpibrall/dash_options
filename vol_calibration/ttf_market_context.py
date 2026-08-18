"""Trading-date context for the TTF intraday calibration workspace.

The page has three deliberately different clocks:

* ``trading_date`` is the date the trader is working on;
* ``settlement_cob`` is the latest governed official surface on or before it;
* intraday trades and publications are loaded for/as-of the trading date.

Keeping those dates explicit prevents a quiet prior-day fallback from being
presented as today's market data.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

import pandas as pd

from options.calibration_engine.io.loaders import load_market_data_with_metadata


def _date(value, *, field: str) -> date:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"A valid {field} is required.")
    return parsed.date()


def load_ttf_trading_context(
    trading_date,
    *,
    refresh: bool = False,
    snapshot_loader: Callable | None = None,
    market_loader: Callable = load_market_data_with_metadata,
) -> dict:
    """Load the latest official TTF settlement available to a trading date."""
    requested = _date(trading_date, field="TTF trading date")
    if snapshot_loader is None:
        from pages.vol_surface import get_operational_surface_snapshot

        snapshot_loader = get_operational_surface_snapshot

    snapshot = snapshot_loader("TTF", requested, refresh=refresh)
    actual_value = snapshot.get("actual_cob")
    if actual_value is None:
        return {
            "trading_date": requested.isoformat(),
            "settlement_cob": None,
            "date_fallback_used": False,
            "market_data": pd.DataFrame(),
            "surface_snapshot": snapshot,
            "source": snapshot.get("source") or "unavailable",
            "last_update": None,
            "error": snapshot.get("error")
            or "No official TTF settlement exists on or before the trading date.",
        }

    settlement_cob = _date(actual_value, field="TTF settlement COB")
    if settlement_cob > requested:
        raise ValueError("TTF settlement resolution attempted to look into the future.")

    loaded = market_loader(
        "TTF",
        settlement_cob,
        allow_synthetic_fallback=False,
    )
    market_data = loaded.get("data")
    if not isinstance(market_data, pd.DataFrame):
        market_data = pd.DataFrame()
    error = loaded.get("error")
    if market_data.empty and not error:
        error = f"No governed TTF calibration inputs exist for {settlement_cob}."
    if not market_data.empty:
        market_data = market_data.copy()
        for source, backup in (
            ("dte", "settlement_dte"),
            ("forward", "settlement_forward"),
            ("strike", "settlement_strike"),
        ):
            if source in market_data.columns and backup not in market_data.columns:
                market_data[backup] = market_data[source]
        if "option_expiration_date" in market_data.columns:
            option_expiry = pd.to_datetime(
                market_data["option_expiration_date"], errors="coerce"
            ).dt.normalize()
            intraday_dte = (option_expiry - pd.Timestamp(requested)).dt.days.astype(float)
            valid_intraday_dte = intraday_dte > 0
            market_data.loc[valid_intraday_dte, "dte"] = intraday_dte.loc[
                valid_intraday_dte
            ]
        market_data["settlement_cob"] = pd.Timestamp(settlement_cob)
        market_data["trading_date"] = pd.Timestamp(requested)

    return {
        "trading_date": requested.isoformat(),
        "settlement_cob": settlement_cob.isoformat(),
        "date_fallback_used": settlement_cob != requested,
        "market_data": market_data,
        "surface_snapshot": snapshot,
        "source": loaded.get("source") or snapshot.get("source") or "unknown",
        "last_update": loaded.get("last_update"),
        "snapshot_id": loaded.get("snapshot_id"),
        "observed_at": loaded.get("observed_at"),
        "provenance_complete": bool(loaded.get("provenance_complete", False)),
        "message": loaded.get("message"),
        "error": error,
    }


def serialize_ttf_trading_context(context: dict) -> dict:
    """Return a Dash-store-safe context without duplicating DataFrames."""
    market_data = context.get("market_data")
    payload = {
        key: value
        for key, value in context.items()
        if key not in {"market_data", "surface_snapshot", "last_update"}
    }
    payload["last_update"] = (
        pd.Timestamp(context["last_update"]).isoformat()
        if context.get("last_update") is not None
        and not pd.isna(context.get("last_update"))
        else None
    )
    payload["market_row_count"] = (
        int(len(market_data)) if isinstance(market_data, pd.DataFrame) else 0
    )
    return payload
