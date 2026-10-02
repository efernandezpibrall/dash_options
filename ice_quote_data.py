"""Read-only ICE quote loading and persisted valuation normalization.

Dash callbacks and charts consume these rows; importing this service does not
register callbacks or construct layouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from typing import Any

import pandas as pd
from sqlalchemy import text

from options.ice_quote_interpretation import interpret_quote_record

from runtime_config import get_database_engine
from vol_trades_market_window import market_context, utc_timestamp

QUOTE_VIEW = "at_lng.ice_chat_quote_dashboard"


SERVICE_TABLE = "at_lng.ice_chat_service_status"


ROW_LIMIT = 10_000


QUOTE_COLUMNS = (
    "event_id",
    "observed_at",
    "received_at",
    "source_channel",
    "recovered",
    "sender_handle",
    "normalization_status",
    "normalization_error",
    "contract_month",
    "option_type",
    "strike",
    "bid",
    "bid_size",
    "offer",
    "offer_size",
    "single_price",
    "single_size",
    "valuation_status",
    "valuation_reason",
    "valued_at",
    "theoretical_price",
    "our_volatility",
    "bid_implied_volatility",
    "offer_implied_volatility",
    "single_implied_volatility",
    "bid_sell_edge",
    "offer_buy_edge",
    "single_deviation",
    "bid_volatility_edge",
    "offer_volatility_edge",
    "bid_edge_ticks",
    "offer_edge_ticks",
    "best_action",
    "best_edge",
    "best_edge_ticks",
    "forward",
    "forward_source",
    "forward_cob_date",
    "surface_cob_date",
    "surface_business_day_age",
    "surface_source",
    "option_expiration_date",
    "pricing_model",
    "pricing_model_version",
    "surface_publication_id",
    "strip_label",
    "structure_code",
    "structure_label",
    "hedge_ratio",
    "hedge_direction",
    "broker_indication_price",
    "broker_indication_side",
    "broker_indication_source",
    "outbound_channel",
    "outbound_status",
    "request_seq_id",
    "outbound_acknowledged_at",
    "outbound_error_code",
    "outbound_error_message",
    "edge_assessment",
)


# Read optional product/unit metadata while preserving historical Brent rows.
QUOTE_METADATA_EXPRESSIONS = (
    "COALESCE(to_jsonb(quote_row)->'strip_months', '[]'::jsonb) AS strip_months",
    "COALESCE(NULLIF(to_jsonb(quote_row)->>'product_code', ''), 'B') AS product_code",
    "COALESCE(NULLIF(to_jsonb(quote_row)->>'product_label', ''), "
    "NULLIF(to_jsonb(quote_row)->>'product_name', ''), 'Brent') AS product_label",
    "COALESCE(NULLIF(to_jsonb(quote_row)->>'currency_code', ''), 'USD') AS currency_code",
    "COALESCE(NULLIF(to_jsonb(quote_row)->>'price_unit', ''), 'bbl') AS price_unit",
    "CASE WHEN COALESCE(to_jsonb(quote_row)->>'tick_size', '') "
    "~ '^[0-9]+([.][0-9]+)?$' "
    "THEN (to_jsonb(quote_row)->>'tick_size')::numeric ELSE 0.01 END AS tick_size",
)


TIME_WINDOWS = {
    "2h": timedelta(hours=2),
    "8h": timedelta(hours=8),
    "7d": timedelta(days=7),
}


PRODUCT_LABELS = {
    "BRENT": "Brent",
    "TFO": "TFO",
    "ON": "HH · ON",
    "LNE": "HH · LNE",
    "JKM": "JKM",
}



# Only products with an ICE Chat quote valuation path belong in the feed.
QUOTE_FEED_PRODUCTS = frozenset({"BRENT", "TFO", "JKM"})


def _selected_product(value: Any) -> str:
    product = str(value or "BRENT").strip().upper()
    return product if product in PRODUCT_LABELS else "BRENT"


def _row_product(value: Any) -> str:
    code = str(value or "B").strip().upper()
    if code in {"B", "BRENT"}:
        return "BRENT"
    return "TFO" if code in {"TFM", "TFO"} else code


@dataclass(frozen=True)
class QuoteLoadResult:
    rows: list[dict]
    service: dict
    error: str | None
    loaded_at: str
    truncated: bool


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: ICE Chat quote data could not be loaded"


def _tick_decimal_places(value: Any) -> int:
    try:
        exponent = Decimal(str(value)).normalize().as_tuple().exponent
    except Exception:
        return 2
    return min(6, max(0, -int(exponent)))


def _compact_number(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.6f}".rstrip("0").rstrip(".")


def _cutoff(window: str, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if window == "today":
        dubai = pd.Timestamp(current).tz_convert("Asia/Dubai")
        return dubai.normalize().tz_convert("UTC").to_pydatetime()
    return current - TIME_WINDOWS.get(window, TIME_WINDOWS["8h"])


def _serialize_frame(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    normalized = frame.copy()
    # Older persisted/normalized vanilla rows predate the structure metadata.
    for column, default in (("structure_code", ""), ("structure_label", ""),
                            ("broker_indication_price", math.nan)):
        if column not in normalized:
            normalized[column] = default
    if "event_id" in normalized:
        normalized["event_id"] = normalized["event_id"].astype(str)
    for column in (
        "observed_at",
        "received_at",
        "valued_at",
        "outbound_acknowledged_at",
    ):
        if column in normalized:
            normalized[column] = pd.to_datetime(
                normalized[column], errors="coerce", utc=True
            )
    for column in (
        "contract_month",
        "forward_cob_date",
        "surface_cob_date",
        "option_expiration_date",
    ):
        if column in normalized:
            normalized[column] = pd.to_datetime(
                normalized[column], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
    for column in (
        "strike",
        "bid",
        "bid_size",
        "offer",
        "offer_size",
        "single_price",
        "single_size",
        "theoretical_price",
        "broker_indication_price",
        "our_volatility",
        "bid_implied_volatility",
        "offer_implied_volatility",
        "single_implied_volatility",
        "bid_sell_edge",
        "offer_buy_edge",
        "single_deviation",
        "bid_volatility_edge",
        "offer_volatility_edge",
        "bid_edge_ticks",
        "offer_edge_ticks",
        "best_edge",
        "best_edge_ticks",
        "forward",
        "tick_size",
    ):
        if column in normalized:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if "surface_publication_id" in normalized:
        normalized["surface_publication_id"] = normalized["surface_publication_id"].map(
            lambda value: str(value) if pd.notna(value) else None
        )
    if "product_code" not in normalized:
        normalized["product_code"] = "B"
    normalized["product_code"] = normalized["product_code"].replace("", pd.NA).fillna("B")
    if "product_label" not in normalized:
        normalized["product_label"] = pd.NA
    product_fallback = normalized["product_code"].map({"B": "Brent"}).fillna(
        normalized["product_code"]
    )
    normalized["product_label"] = (
        normalized["product_label"].replace("", pd.NA).fillna(product_fallback)
    )
    for column, fallback in (("currency_code", "USD"), ("price_unit", "bbl")):
        if column not in normalized:
            normalized[column] = fallback
        normalized[column] = normalized[column].replace("", pd.NA).fillna(fallback)
    if "tick_size" not in normalized:
        normalized["tick_size"] = 0.01
    normalized["tick_size"] = pd.to_numeric(
        normalized["tick_size"], errors="coerce"
    ).where(lambda values: values > 0, 0.01).fillna(0.01)
    normalized["price_decimals"] = normalized["tick_size"].map(_tick_decimal_places)
    normalized["price_unit_label"] = (
        normalized["currency_code"].astype(str)
        + "/"
        + normalized["price_unit"].astype(str)
    )
    monthly_contract_label = pd.to_datetime(
        normalized.get("contract_month"), errors="coerce"
    ).dt.strftime("%b-%y")
    normalized["contract_label"] = normalized.get(
        "strip_label", pd.Series(index=normalized.index, dtype="object")
    ).replace("", pd.NA).fillna(monthly_contract_label)
    normalized["option_label"] = normalized.get("option_type", pd.Series(dtype=str)).map(
        {"C": "Call", "P": "Put"}
    )
    normalized["strike_label"] = normalized["strike"].map(_compact_number)
    fence = normalized["structure_code"].eq("CLLR")
    spread = normalized["structure_code"].isin(["CALLSPR", "PUTSPR"])
    cso = normalized["structure_code"].isin(["CSO3", "CSO4"])
    package = normalized["structure_code"].isin(["CFLY", "STNGL", "STRDL", "PUT_SPREAD_VS_CALL", "DIAGONAL_CALL_SPREAD", "CALL_SPREAD_VS_PUT_SPREAD"])
    structure = fence | spread | cso | package
    normalized.loc[package, "option_label"] = normalized.loc[package, "structure_code"].map({"CFLY": "Butterfly", "STNGL": "Strangle", "STRDL": "Straddle", "DIAGONAL_CALL_SPREAD": "Diagonal call spread", "PUT_SPREAD_VS_CALL": "Put spread vs call", "CALL_SPREAD_VS_PUT_SPREAD": "Call spread vs put spread"})
    normalized.loc[fence, "option_label"] = "Fence"
    normalized.loc[spread, "option_label"] = normalized.loc[spread, "structure_code"].map(
        {"CALLSPR": "Call spread", "PUTSPR": "Put spread"}
    )
    normalized.loc[cso, "option_label"] = "CSO " + normalized.loc[cso, "option_type"]
    normalized.loc[structure, "strike_label"] = normalized.loc[structure, "structure_label"]
    normalized.loc[spread, "strike_label"] = normalized.loc[spread, "structure_label"].str.replace(
        " call spread", "", regex=False
    ).str.replace(" put spread", "", regex=False)
    normalized.loc[package, "strike_label"] = normalized.loc[package, "structure_label"].str.replace(
        " butterfly", "", regex=False
    ).str.replace(" strangle", "", regex=False).str.replace(" straddle", "", regex=False).str.replace(" diagonal", "", regex=False)
    normalized["option_filter_key"] = normalized["option_type"]
    normalized.loc[structure, "option_filter_key"] = normalized.loc[structure, "structure_code"]
    normalized.loc[cso, "option_filter_key"] = (
        normalized.loc[cso, "structure_code"] + "_" + normalized.loc[cso, "option_type"]
    )
    normalized["strike_filter_key"] = normalized["strike"].map(_compact_number)
    normalized.loc[structure, "strike_filter_key"] = normalized.loc[structure, "structure_label"]
    interpretation_time = datetime.now(timezone.utc)
    interpretations = pd.DataFrame(
        [interpret_quote_record(row, now=interpretation_time) for row in normalized.to_dict("records")],
        index=normalized.index,
    )
    for field in interpretations:
        normalized[field] = interpretations[field]
    def display_indication(row):
        magnitude = row["broker_indication_magnitude"]
        if pd.isna(magnitude):
            return "—"
        orientation = row["fence_premium_orientation"]
        side = {"to_put": "to put", "to_call": "to call"}.get(orientation, "side?")
        return f"{float(magnitude):.{int(row['price_decimals'])}f} {side}*"
    normalized["broker_indication_display"] = normalized.apply(display_indication, axis=1)
    def display_theoretical(row):
        value = row["theoretical_cash_premium"]
        if pd.isna(value):
            return "—"
        valuation_decimals = int(row["price_decimals"]) + 1
        rounded = Decimal(str(abs(value))).quantize(Decimal(1).scaleb(-valuation_decimals), rounding=ROUND_HALF_UP)
        amount = f"{rounded:.{valuation_decimals}f}"
        if row["structure_code"] == "CLLR":
            side = {"to_put": "to put", "to_call": "to call"}.get(row["fence_premium_orientation"], "side?")
            return f"{amount} {side}*"
        if row["structure_code"] in {"CALLSPR", "PUTSPR", "CFLY", "STNGL", "STRDL", "PUT_SPREAD_VS_CALL", "DIAGONAL_CALL_SPREAD", "CALL_SPREAD_VS_PUT_SPREAD"}:
            return f"{amount} {row['premium_cashflow']}"
        return amount
    normalized["theoretical_display"] = normalized.apply(display_theoretical, axis=1)
    normalized["instrument_label"] = (
        normalized["contract_label"].fillna("—")
        + " · "
        + normalized["strike_label"]
        + " "
        + normalized["option_label"].fillna(normalized["option_type"])
    )
    normalized["processing_status"] = normalized.get("valuation_status").fillna(
        normalized.get("normalization_status")
    )
    normalized["display_error"] = (
        normalized.get("valuation_reason")
        .fillna(normalized.get("normalization_error"))
        .fillna(normalized.get("outbound_error_message"))
    )
    for source, target in (
        ("our_volatility", "our_iv_pct"),
        ("bid_implied_volatility", "bid_iv_pct"),
        ("offer_implied_volatility", "offer_iv_pct"),
        ("single_implied_volatility", "single_iv_pct"),
        ("bid_volatility_edge", "bid_iv_edge_pp"),
        ("offer_volatility_edge", "offer_iv_edge_pp"),
    ):
        normalized[target] = normalized[source] * 100.0
    normalized["observed_display"] = normalized["observed_at"].dt.tz_convert(
        "Asia/Dubai"
    ).dt.strftime("%d %b %H:%M:%S")
    return json.loads(normalized.to_json(orient="records", date_format="iso"))


def load_quote_snapshot(
    window: str,
    *,
    engine=None,
    now: datetime | None = None,
    history_snapshot: dict | None = None,
) -> QuoteLoadResult:
    loaded_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    context = market_context(history_snapshot, now=loaded_at)
    table_cutoff = _cutoff(window, loaded_at)
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        return QuoteLoadResult(
            rows=[],
            service={},
            error="Database configuration is unavailable",
            loaded_at=loaded_at.isoformat(),
            truncated=False,
        )
    quote_query = text(
        f"""
        SELECT {", ".join(QUOTE_COLUMNS)},
               {", ".join(QUOTE_METADATA_EXPRESSIONS)}
        FROM {QUOTE_VIEW} AS quote_row
        WHERE (observed_at >= :cutoff AND observed_at <= :loaded_at)
           OR (CAST(:market_start AS timestamptz) IS NOT NULL AND observed_at >= :market_start
               AND observed_at <= :market_cutoff)
        ORDER BY observed_at DESC, event_id DESC
        LIMIT :row_limit
        """
    )
    service_query = text(
        f"""
        SELECT environment, connection_state, outbound_enabled, session_id,
               connected_at, disconnected_at, last_heartbeat_at, last_event_at,
               surface_cob_date, surface_business_day_age, queue_depth,
               last_request_seq_id, last_error_code, last_error_message,
               updated_at
        FROM {SERVICE_TABLE}
        ORDER BY updated_at DESC
        LIMIT 1
        """
    )
    surface_query = text(
        """
        SELECT max(cob_date) AS surface_cob_date
        FROM at_lng.implied_volatility_surface_from_prices
        WHERE upper(product)='BRENT'
        """
    )
    ttf_publication_query = text(
        """
        SELECT cob_date
        FROM at_lng.vol_surface_publications
        WHERE upper(commodity) = 'TTF' AND status = 'published' AND is_active
        ORDER BY cob_date DESC, published_at DESC, created_at DESC
        LIMIT 1
        """
    )
    jkm_publication_query = text(
        """
        SELECT cob_date FROM at_lng.vol_surface_publications
        WHERE upper(commodity) = 'JKM' AND status = 'published' AND is_active
          AND published_at <= :loaded_at
        ORDER BY cob_date DESC, published_at DESC, created_at DESC LIMIT 1
        """
    )
    try:
        with db_engine.connect() as connection:
            frame = pd.read_sql(
                quote_query,
                connection,
                params={
                    "cutoff": table_cutoff, "loaded_at": loaded_at,
                    "market_start": utc_timestamp(context["day_start_at"]).to_pydatetime() if context else None,
                    "market_cutoff": utc_timestamp(context["cutoff_at"]).to_pydatetime() if context else None,
                    "row_limit": ROW_LIMIT + 1,
                },
            )
            service_row = connection.execute(service_query).mappings().first()
            surface_cob = connection.execute(surface_query).scalar()
            ttf_surface_cob = connection.execute(ttf_publication_query).scalar()
            jkm_surface_cob = connection.execute(jkm_publication_query, {"loaded_at": loaded_at}).scalar()
    except Exception as exc:
        return QuoteLoadResult(
            rows=[],
            service={},
            error=_safe_error(exc),
            loaded_at=loaded_at.isoformat(),
            truncated=False,
        )
    truncated = len(frame) > ROW_LIMIT
    frame = frame.iloc[:ROW_LIMIT].copy()
    service = dict(service_row) if service_row else {}
    for key, value in list(service.items()):
        if isinstance(value, (datetime, pd.Timestamp)):
            service[key] = pd.Timestamp(value).isoformat()
    service["surface_cob_date"] = str(
        service.get("surface_cob_date") or surface_cob
    ) if (service.get("surface_cob_date") or surface_cob) else None
    service["ttf_surface_cob_date"] = str(ttf_surface_cob) if ttf_surface_cob else None
    service["jkm_surface_cob_date"] = str(jkm_surface_cob) if jkm_surface_cob else None
    return QuoteLoadResult(
        rows=_serialize_frame(frame),
        service=service,
        error=None,
        loaded_at=loaded_at.isoformat(),
        truncated=truncated,
    )



def load_quote_reply(
    event_id: str,
    product: str = "BRENT",
    *,
    engine=None,
) -> tuple[dict | None, str | None]:
    """Read persisted delivery evidence for one quote in the selected product."""
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        return None, "Reply delivery could not be loaded: database unavailable."
    try:
        with db_engine.connect() as connection:
            record = connection.execute(text(f"""
                SELECT outbound_status, outbound_attempted_at, outbound_acknowledged_at,
                       outbound_error_message, outbound_message_text, outbound_batch_reference,
                       edge_assessment, quote_context, surface_cob_date,
                       surface_publication_id, pricing_model, single_price, bid, offer
                FROM {QUOTE_VIEW}
                WHERE event_id = CAST(:event_id AS uuid) AND product_code = :product_code
            """), {"event_id": str(event_id),
                     "product_code": {"TFO": "TFM", "JKM": "JKM"}.get(_selected_product(product), "B")}).mappings().first()
    except Exception as exc:
        return None, _safe_error(exc)
    return dict(record) if record else None, None
