"""Manual TTF trade persistence and authorization adapter."""
from typing import Mapping
import pandas as pd
from sqlalchemy import inspect, text
from options.vol_calibration.api import (  # noqa: F401 - compatibility exports
    normalize_ttf_intraday_trade, TTF_INTRADAY_METHOD, TTF_INTRADAY_PREMIUM_METHOD,
)
from vol_calibration.auth import Identity, Permission, authorize

TTF_INTRADAY_TRADE_TABLE = "at_lng.vol_calibration_intraday_trades"

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
