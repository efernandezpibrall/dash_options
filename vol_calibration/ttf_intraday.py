"""Manual intraday TTF option trades and their chart coordinates."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Mapping
from uuid import uuid4

import pandas as pd
from sqlalchemy import inspect, text

from options.ttf_volatility import (
    DAY_COUNT_LABEL,
    DELTA_CONVENTION,
    black76_call_delta,
    implied_volatility_from_settlement,
    year_fraction,
)
from vol_calibration.auth import Identity, Permission, authorize


TTF_INTRADAY_TRADE_TABLE = "at_lng.vol_calibration_intraday_trades"
TTF_INTRADAY_METHOD = "manual_ttf_black76_v1"
TTF_INTRADAY_PREMIUM_METHOD = "manual_ttf_black76_premium_inversion_v1"


def _positive_number(value, label: str, *, required: bool = True) -> float | None:
    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if not math.isfinite(float(parsed)) if pd.notna(parsed) else True:
        if required:
            raise ValueError(f"{label} must be finite and positive.")
        return None
    parsed = float(parsed)
    if parsed <= 0:
        if required:
            raise ValueError(f"{label} must be finite and positive.")
        return None
    return parsed


def normalize_ttf_intraday_trade(
    values: Mapping,
    *,
    entered_by: str,
    now: datetime | None = None,
) -> dict:
    """Validate a manual mark and compute its governed IV and call delta."""
    business_date = pd.to_datetime(values.get("business_date"), errors="coerce")
    contract_date = pd.to_datetime(values.get("contract_date"), errors="coerce")
    option_expiry = pd.to_datetime(
        values.get("option_expiration_date"), errors="coerce"
    )
    if pd.isna(business_date) or pd.isna(contract_date) or pd.isna(option_expiry):
        raise ValueError("Trading date, contract month, and option expiry are required.")
    business_date = business_date.normalize()
    contract_date = contract_date.normalize()
    option_expiry = option_expiry.normalize()
    option_type = str(values.get("put_call") or "").strip().upper()
    if option_type not in {"C", "P"}:
        raise ValueError("Option type must be Call or Put.")
    if not str(entered_by or "").strip():
        raise ValueError("The manual trade requires an identified trader.")
    strike = _positive_number(values.get("strike"), "Strike")
    forward = _positive_number(values.get("forward"), "Working forward")
    volume = _positive_number(values.get("volume"), "Volume", required=False)
    premium = _positive_number(values.get("mark_price"), "Premium", required=False)
    mark_iv = _positive_number(values.get("mark_iv"), "Implied volatility", required=False)
    if mark_iv is not None and premium is not None:
        raise ValueError("Enter implied volatility or premium, not both.")
    if mark_iv is not None and mark_iv > 5.0:
        mark_iv /= 100.0
    if mark_iv is not None and mark_iv >= 2.0:
        raise ValueError("Implied volatility must be below 200%.")

    tte = year_fraction(business_date, option_expiry)
    iv_was_derived = mark_iv is None
    if iv_was_derived:
        if premium is None:
            raise ValueError("Enter either implied volatility or option premium.")
        mark_iv = implied_volatility_from_settlement(
            option_type,
            forward,
            strike,
            tte,
            premium,
        )
    call_delta = black76_call_delta(forward, strike, tte, mark_iv)
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    forward_observed_at = pd.to_datetime(
        values.get("forward_observed_at"), errors="coerce", utc=True
    )
    if pd.isna(forward_observed_at):
        forward_observed_at = pd.Timestamp(observed_at)

    return {
        "trade_id": str(values.get("trade_id") or uuid4()),
        "commodity": "TTF",
        "business_date": business_date.date().isoformat(),
        "observed_at": pd.Timestamp(observed_at).isoformat(),
        "contract_date": contract_date.date().isoformat(),
        "option_expiration_date": option_expiry.date().isoformat(),
        "put_call": option_type,
        "strike": strike,
        "mark_price": premium,
        "mark_iv": float(mark_iv),
        "volume": volume,
        "forward": forward,
        "forward_observed_at": forward_observed_at.isoformat(),
        "dte": float((option_expiry - business_date).days),
        "call_delta": call_delta,
        "log_moneyness": float(math.log(strike / forward)),
        "day_count": DAY_COUNT_LABEL,
        "delta_convention": DELTA_CONVENTION,
        "pricing_model": "Black-76 futures-style",
        "method": (
            TTF_INTRADAY_PREMIUM_METHOD
            if iv_was_derived
            else TTF_INTRADAY_METHOD
        ),
        "iv_source": "Premium inversion" if iv_was_derived else "Entered IV",
        "entered_by": str(entered_by).strip(),
        "status": "active",
        "supersedes_trade_id": values.get("supersedes_trade_id"),
        "notes": str(values.get("notes") or "").strip() or None,
    }


def _table_available(engine) -> bool:
    try:
        return inspect(engine).has_table(
            "vol_calibration_intraday_trades", schema="at_lng"
        )
    except Exception:
        return False


def load_ttf_intraday_trades(engine, business_date) -> list[dict]:
    """Load active manual trades for one exact trading date."""
    if engine is None or not _table_available(engine):
        return []
    query = text(
        """
        SELECT trade_id, commodity, business_date, observed_at, contract_date,
               option_expiration_date, put_call, strike, mark_price, mark_iv,
               volume, forward, forward_observed_at, dte, call_delta,
               log_moneyness, day_count, delta_convention, pricing_model,
               method, entered_by, status, supersedes_trade_id, notes, created_at
        FROM at_lng.vol_calibration_intraday_trades
        WHERE commodity = 'TTF' AND business_date = :business_date
          AND status = 'active'
        ORDER BY observed_at, created_at
        """
    )
    frame = pd.read_sql(
        query,
        engine,
        params={"business_date": pd.Timestamp(business_date).date()},
    )
    for column in (
        "trade_id",
        "supersedes_trade_id",
    ):
        if column in frame.columns:
            frame[column] = frame[column].astype(str)
    for column in (
        "business_date",
        "contract_date",
        "option_expiration_date",
        "observed_at",
        "forward_observed_at",
        "created_at",
    ):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce").astype(str)
    return frame.to_dict("records")


def persist_ttf_intraday_trade(engine, trade: Mapping, identity: Identity) -> dict:
    """Append one authorized manual trade; corrections must be new records."""
    authorize(identity, Permission.CREATE_DRAFT)
    if engine is None or not _table_available(engine):
        raise RuntimeError("TTF intraday trade storage is not migrated.")
    if identity.subject != trade.get("entered_by"):
        raise PermissionError("The authenticated trader must match entered_by.")
    columns = [
        "trade_id", "commodity", "business_date", "observed_at", "contract_date",
        "option_expiration_date", "put_call", "strike", "mark_price", "mark_iv",
        "volume", "forward", "forward_observed_at", "dte", "call_delta",
        "log_moneyness", "day_count", "delta_convention", "pricing_model", "method",
        "entered_by", "status", "supersedes_trade_id", "notes",
    ]
    values = {name: trade.get(name) for name in columns}
    statement = text(
        f"""
        INSERT INTO {TTF_INTRADAY_TRADE_TABLE} ({', '.join(columns)})
        VALUES ({', '.join(':' + name for name in columns)})
        RETURNING trade_id
        """
    )
    with engine.begin() as connection:
        returned = connection.execute(statement, values).scalar_one()
    return {**dict(trade), "trade_id": str(returned), "persisted": True}
