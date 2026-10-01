"""Immutable Vol Trades market snapshots and shared market preparation.

This module owns product/source mappings and bounded process-local loaders. It
has no Dash layout or callback registration, so calibration processes can use the
same snapshot contract as the dashboard without importing a page.
"""

from __future__ import annotations

import json
import math
from datetime import date
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, text

from options.vol_calibration.api import delta_to_strike
from options.vol_calibration.api import prepare_brent_calibration_observations
from runtime_config import get_database_engine

PRODUCT = "BRENT"


PRODUCT_SPECS = {
    "BRENT": {
        "label": "Brent",
        "price_unit": "USD/bbl",
        "published_product": "BRENT",
        "published_label": "Brent",
        "underlying_label": "Underlying",
        "intraday_policy_version": "brent-front6-jun-dec-y2-v1",
        "front_count": 6,
        "anchor_months": (6, 12),
        "through_year_offset": 2,
    },
    "TFO": {
        "label": "TFO",
        "price_unit": "EUR/MWh",
        "published_product": "TTF",
        "published_label": "TTF",
        "underlying_label": "TZT",
        "intraday_policy_version": "tfo-front12-quarterly-y2-v1",
        "front_count": 12,
        "anchor_months": (3, 6, 9, 12),
        "through_year_offset": 2,
    },
    "ON": {
        "label": "HH · ON",
        "price_unit": "USD/MMBtu",
        "published_product": "HH",
        "published_label": "HH",
        "underlying_label": "NG",
        "intraday_policy_version": "on-front12-quarterly-y2-v1",
        "front_count": 12,
        "anchor_months": (3, 6, 9, 12),
        "through_year_offset": 2,
    },
    "LNE": {
        "label": "HH · LNE",
        "price_unit": "USD/MMBtu",
        "published_product": "HH",
        "published_label": "HH",
        "underlying_label": "NG",
        "intraday_policy_version": "lne-front12-quarterly-y2-v1",
        "front_count": 12,
        "anchor_months": (3, 6, 9, 12),
        "through_year_offset": 2,
    },
    "JKM": {
        "label": "JKM",
        "price_unit": "USD/MMBtu",
        "published_product": "JKM",
        "published_label": "JKM",
        "underlying_label": "ICE JKM MO",
        "intraday_policy_version": "jkm-official-icap-ice-forward-v1",
        "front_count": 0,
        "anchor_months": (),
        "through_year_offset": 0,
    },
}


SNAPSHOT_LIMIT = 20


CHAIN_TABLE = "at_lng.bbg_option_chain"


TRADE_EVENT_TABLE = "at_lng.bbg_option_trade_events"


TRADE_TAPE_RUN_TABLE = "at_lng.bbg_option_trade_tape_runs"


SNAPSHOT_TABLE = "at_lng.vol_market_snapshots"


PUBLISHED_TABLE = "at_lng.implied_volatility_surface_from_prices"


CALIBRATED_PUBLICATION_TABLE = "at_lng.vol_surface_publications"


CALIBRATED_SURFACE_TABLE = "at_lng.implied_volatility_surface_calibrated"


EXACT_COB_SURFACE_PRODUCTS = frozenset({"BRENT", "LNE", "ON"})


def _normalize_product(product: Any) -> str:
    normalized = str(product or PRODUCT).strip().upper()
    return normalized if normalized in PRODUCT_SPECS else PRODUCT


def _product_spec(product: Any) -> dict[str, Any]:
    return PRODUCT_SPECS[_normalize_product(product)]


def _normalize_snapshot_kind(snapshot_kind: Any) -> str:
    normalized = str(snapshot_kind or "").strip().upper()
    if normalized not in {"INTRADAY", "SETTLEMENT"}:
        raise ValueError(f"Unsupported snapshot kind {snapshot_kind!r}")
    return normalized


def load_available_snapshots(
    product: str = PRODUCT,
    engine=None,
    *,
    limit: int = SNAPSHOT_LIMIT,
) -> pd.DataFrame:
    product = _normalize_product(product)
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        return pd.DataFrame()
    query = text(
        f"""
        WITH available AS (
            SELECT s.snapshot_id,
                   s.business_date,
                   s.observed_at,
                   s.input_fingerprint,
                   s.option_quote_count,
                   s.forward_count,
                   s.metadata,
                   COALESCE(s.metadata ->> 'snapshot_kind', 'SETTLEMENT') AS snapshot_kind,
                   s.created_at,
                   EXISTS (
                       SELECT 1
                       FROM {CHAIN_TABLE} AS c
                       WHERE c.snapshot_id = s.snapshot_id
                         AND c.product = :product
                   ) AS has_chain
            FROM {SNAPSHOT_TABLE} AS s
            WHERE s.commodity = :product
              AND s.status = 'complete'
        ), settlement_ranked AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY s.business_date
                       ORDER BY s.observed_at DESC, s.created_at DESC, s.snapshot_id DESC
                   ) AS date_rank
            FROM available AS s
            WHERE s.snapshot_kind = :settlement_kind AND s.has_chain
        ), settlements AS (
            SELECT *
            FROM settlement_ranked
            WHERE date_rank = 1
            ORDER BY business_date DESC
            LIMIT :limit
        ), recent_intraday AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY business_date
                       ORDER BY observed_at DESC, created_at DESC, snapshot_id DESC
                   ) AS intraday_rank
            FROM available
            WHERE snapshot_kind = :intraday_kind
              AND has_chain
              AND (
                  :product = 'BRENT'
                  OR business_date =
                      (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Dubai')::date
              )
        ), intraday_dates AS (
            SELECT * FROM recent_intraday
            WHERE intraday_rank = 1
            ORDER BY business_date DESC
            LIMIT 5
        ), selected AS (
            SELECT snapshot_id, business_date, observed_at, input_fingerprint,
                   option_quote_count, forward_count, metadata, snapshot_kind,
                   0 AS sort_group
            FROM intraday_dates
            UNION ALL
            SELECT snapshot_id, business_date, observed_at, input_fingerprint,
                   option_quote_count, forward_count, metadata, snapshot_kind,
                   1 AS sort_group
            FROM settlements
        )
        SELECT snapshot_id, business_date, observed_at, input_fingerprint,
               option_quote_count, forward_count, metadata, snapshot_kind
        FROM selected
        ORDER BY sort_group, business_date DESC, observed_at DESC
        """
    )
    return pd.read_sql(
        query,
        db_engine,
        params={
            "product": product,
            "limit": int(limit),
            "settlement_kind": "SETTLEMENT",
            "intraday_kind": "INTRADAY",
        },
    )


def load_available_jkm_cobs(engine=None, *, limit: int = SNAPSHOT_LIMIT) -> pd.DataFrame:
    """Return only COBs having both official JKM nodes and ICE_JKM_MO forwards."""

    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        return pd.DataFrame()
    return pd.read_sql(
        text(
            f"""
            SELECT s.cob_date AS business_date,
                   MAX(COALESCE(s.vendor_published_at, s.ingested_at)) AS observed_at,
                   COUNT(*)::integer AS option_quote_count,
                   (
                       SELECT COUNT(*)::integer
                       FROM at_lng.curve AS c
                       WHERE c.code = 'ICE_JKM_MO'
                         AND c.cob = s.cob_date
                         AND c.value IS NOT NULL
                   ) AS forward_count
            FROM {PUBLISHED_TABLE} AS s
            WHERE s.product = 'JKM'
              AND EXISTS (
                  SELECT 1
                  FROM at_lng.curve AS c
                  WHERE c.code = 'ICE_JKM_MO'
                    AND c.cob = s.cob_date
                    AND c.value IS NOT NULL
              )
            GROUP BY s.cob_date
            ORDER BY s.cob_date DESC
            LIMIT :limit
            """
        ),
        db_engine,
        params={"limit": int(limit)},
    )


def load_jkm_official_market(cob_date: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load the exact-COB official ICAP smile and ICE_JKM_MO forward set."""

    from options.vol_calibration.api import load_market_data_with_metadata

    selected = pd.Timestamp(cob_date).date()
    result = load_market_data_with_metadata(
        "JKM",
        selected,
        source="icap",
        allow_synthetic_fallback=False,
    )
    market = result.get("data")
    if not isinstance(market, pd.DataFrame) or market.empty:
        raise ValueError(f"No official JKM inputs exist for exact COB {selected}")
    if result.get("is_synthetic"):
        raise ValueError("Synthetic JKM inputs are prohibited")
    required = {"expiry", "option_expiration_date", "delta", "iv", "strike", "forward"}
    missing = sorted(required.difference(market.columns))
    if missing:
        raise ValueError("Official JKM inputs are missing: " + ", ".join(missing))
    normalized = market.copy()
    for column in ("expiry", "option_expiration_date"):
        normalized[column] = pd.to_datetime(normalized[column], errors="coerce")
    for column in ("delta", "iv", "strike", "forward"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    invalid = (
        normalized[["expiry", "option_expiration_date"]].isna().any(axis=1)
        | ~np.isfinite(
            normalized[["delta", "iv", "strike", "forward"]].to_numpy(dtype=float)
        ).all(axis=1)
        | normalized["delta"].le(0)
        | normalized["delta"].ge(1)
        | normalized["iv"].le(0)
        | normalized["strike"].le(0)
        | normalized["forward"].le(0)
    )
    if invalid.any() or normalized.duplicated(["expiry", "delta"]).any():
        raise ValueError("Official JKM inputs contain invalid or duplicate nodes")
    normalized["business_date"] = pd.Timestamp(selected)
    return normalized.sort_values(["expiry", "delta"]).reset_index(drop=True), result


@lru_cache(maxsize=64)
def _cached_chain_snapshot(
    product: str,
    snapshot_kind: str,
    snapshot_id: str,
) -> pd.DataFrame:
    return _read_chain_snapshot(
        product,
        snapshot_kind,
        snapshot_id,
        get_database_engine(required=False),
    )


def _read_chain_snapshot(
    product: str,
    snapshot_kind: str,
    snapshot_id: str,
    engine,
) -> pd.DataFrame:
    product = _normalize_product(product)
    snapshot_kind = _normalize_snapshot_kind(snapshot_kind)
    if engine is None or not snapshot_id:
        return pd.DataFrame()
    header = pd.read_sql(
        text(
            f"""
            SELECT snapshot_id, input_fingerprint, source_name, source_revision,
                   metadata AS snapshot_metadata
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_id = CAST(:snapshot_id AS uuid)
              AND commodity = :product
              AND status = 'complete'
              AND COALESCE(metadata ->> 'snapshot_kind', 'SETTLEMENT')
                  = :snapshot_kind
            """
        ),
        engine,
        params={
            "snapshot_id": snapshot_id,
            "product": product,
            "snapshot_kind": snapshot_kind,
        },
    )
    if header.empty:
        return pd.DataFrame()
    query = text(
        f"""
        SELECT c.snapshot_id,
               c.product,
               c.business_date,
               c.observed_at,
               c.discovery_method,
               c.bloomberg_description,
               c.underlying_type,
               c.underlying_security,
               COALESCE(
                   c.pricing_underlying_security,
                   c.underlying_security
               ) AS pricing_underlying_security,
               c.underlying_global_id,
               c.underlying_contract_month,
               c.underlying_last_tradeable_date,
               c.underlying_price,
               c.option_security,
               c.option_global_id,
               c.put_call,
               c.strike,
               c.option_expiration_date,
               c.option_last_tradeable_date,
               c.option_style,
               c.premium_style,
               c.exchange_code,
               c.currency,
               c.price_unit,
               c.contract_multiplier,
               c.pricing_discount_rate,
               c.pricing_discount_rate_observed_at,
               c.settlement_price,
               c.last_price,
               c.last_trade_date,
               c.last_update_date,
               c.open_interest_date,
               c.settlement_open_interest,
               c.settlement_open_interest_date,
               c.intraday_open_interest,
               c.intraday_open_interest_date,
               c.volume,
               c.open_interest,
               c.implied_volatility,
               c.pricing_model,
               c.pricing_model_version,
               c.iv_status,
               c.iv_exclusion_reason,
               c.option_bid,
               c.option_ask,
               c.option_mid,
               c.option_spread,
               c.option_spread_pct,
               c.underlying_bid,
               c.underlying_ask,
               c.underlying_mid,
               c.underlying_spread,
               c.quote_batch_id,
               c.quote_request_started_at,
               c.quote_response_at,
               c.quote_capture_skew_ms,
               c.executable_iv_bid,
               c.executable_iv_mid,
               c.executable_iv_ask,
               c.executable_iv_status,
               c.executable_iv_exclusion_reason,
               c.last_trade_price,
               c.last_trade_at,
               c.last_trade_condition_codes,
               c.last_trade_underlying_price,
               c.last_trade_underlying_at,
               c.last_trade_underlying_source,
               c.last_trade_match_lag_ms,
               c.last_trade_iv,
               c.last_trade_iv_status,
               c.last_trade_iv_exclusion_reason,
               c.last_trade_match_source_snapshot_id,
               c.ingested_at
        FROM {CHAIN_TABLE} AS c
        WHERE c.snapshot_id = CAST(:snapshot_id AS uuid)
          AND c.product = :product
        ORDER BY c.underlying_contract_month, c.strike, c.put_call, c.option_security
        """
    )
    frame = pd.read_sql(
        query,
        engine,
        params={"snapshot_id": snapshot_id, "product": product},
    )
    if frame.empty:
        return frame
    header_row = header.iloc[0]
    metadata = _snapshot_metadata(header_row["snapshot_metadata"])
    frame["input_fingerprint"] = str(header_row["input_fingerprint"])
    frame["source_name"] = header_row["source_name"]
    frame["source_revision"] = header_row["source_revision"]
    frame["snapshot_metadata"] = [metadata] * len(frame)
    previous_id = metadata.get("previous_snapshot_id")
    frame["previous_option_security"] = None
    frame["previous_volume"] = np.nan
    frame["previous_snapshot_metadata"] = [{}] * len(frame)
    frame["previous_observed_at"] = pd.NaT
    if previous_id:
        previous_header = pd.read_sql(
            text(
                f"""
                SELECT metadata, observed_at
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_id = CAST(:snapshot_id AS uuid)
                  AND commodity = :product
                  AND status = 'complete'
                  AND COALESCE(metadata ->> 'snapshot_kind', 'SETTLEMENT')
                      = :snapshot_kind
                """
            ),
            engine,
            params={
                "snapshot_id": str(previous_id),
                "product": product,
                "snapshot_kind": snapshot_kind,
            },
        )
        previous = pd.read_sql(
            text(
                f"""
                SELECT previous_chain.option_security AS previous_option_security,
                       previous_chain.volume AS previous_volume,
                       previous_chain.business_date AS previous_business_date,
                       previous_chain.last_trade_date AS previous_last_trade_date
                FROM {CHAIN_TABLE} AS previous_chain
                JOIN {SNAPSHOT_TABLE} AS previous_snapshot
                  ON previous_snapshot.snapshot_id = previous_chain.snapshot_id
                WHERE previous_chain.snapshot_id = CAST(:snapshot_id AS uuid)
                  AND previous_chain.product = :product
                  AND previous_snapshot.commodity = :product
                  AND previous_snapshot.status = 'complete'
                  AND COALESCE(
                      previous_snapshot.metadata ->> 'snapshot_kind',
                      'SETTLEMENT'
                  ) = :snapshot_kind
                """
            ),
            engine,
            params={
                "snapshot_id": str(previous_id),
                "product": product,
                "snapshot_kind": snapshot_kind,
            },
        )
        if not previous.empty:
            frame = frame.drop(
                columns=["previous_option_security", "previous_volume"]
            ).merge(
                previous,
                how="left",
                left_on="option_security",
                right_on="previous_option_security",
                sort=False,
            )
        if not previous_header.empty:
            previous_metadata = _snapshot_metadata(previous_header.iloc[0]["metadata"])
            frame["previous_snapshot_metadata"] = [previous_metadata] * len(frame)
            frame["previous_observed_at"] = previous_header.iloc[0]["observed_at"]
    return _normalize_chain_frame(frame)


def load_chain_snapshot(
    snapshot_id: str,
    engine=None,
    *,
    product: str = PRODUCT,
    snapshot_kind: str = "SETTLEMENT",
    refresh: bool = False,
) -> pd.DataFrame:
    product = _normalize_product(product)
    snapshot_kind = _normalize_snapshot_kind(snapshot_kind)
    if refresh:
        _cached_chain_snapshot.cache_clear()
    if engine is not None:
        return _read_chain_snapshot(product, snapshot_kind, snapshot_id, engine)
    return _cached_chain_snapshot(product, snapshot_kind, str(snapshot_id)).copy()


@lru_cache(maxsize=64)
def _cached_trade_tape(
    product: str,
    snapshot_kind: str,
    snapshot_id: str,
) -> pd.DataFrame:
    return _read_trade_tape(
        product,
        snapshot_kind,
        snapshot_id,
        get_database_engine(required=False),
    )


def _read_trade_tape(
    product: str,
    snapshot_kind: str,
    snapshot_id: str,
    engine,
) -> pd.DataFrame:
    product = _normalize_product(product)
    snapshot_kind = _normalize_snapshot_kind(snapshot_kind)
    if snapshot_kind != "INTRADAY":
        return pd.DataFrame()
    if engine is None or not snapshot_id:
        return pd.DataFrame()
    query = text(
        f"""
        SELECT e.snapshot_id,
               e.product,
               e.business_date,
               e.option_security,
               e.option_global_id,
               e.underlying_security,
               e.underlying_global_id,
               e.underlying_contract_month,
               e.option_expiration_date,
               e.put_call,
               e.strike,
               e.trade_at,
               e.trade_price,
               e.trade_size,
               e.condition_codes,
               e.is_regular,
               e.future_bid,
               e.future_ask,
               e.future_mid,
               e.future_bid_at,
               e.future_ask_at,
               e.future_match_price,
               e.future_match_at,
               e.future_match_source,
               e.future_match_lag_ms,
               e.trade_iv,
               e.trade_iv_status,
               e.trade_iv_exclusion_reason,
               e.pricing_model,
               e.pricing_model_version,
               e.policy_version,
               e.event_fingerprint,
               e.occurrence_ordinal,
               e.first_seen_snapshot_id,
               r.tape_run_id,
               r.window_start,
               r.cutoff_at,
               r.coverage_status,
               r.metadata AS tape_metadata
        FROM {TRADE_EVENT_TABLE} AS e
        JOIN {TRADE_TAPE_RUN_TABLE} AS r
          ON r.tape_run_id = e.tape_run_id
         AND r.snapshot_id = e.snapshot_id
        JOIN {SNAPSHOT_TABLE} AS s
          ON s.snapshot_id = e.snapshot_id
        WHERE e.snapshot_id = CAST(:snapshot_id AS uuid)
          AND e.product = :product
          AND s.commodity = :product
          AND s.status = 'complete'
          AND COALESCE(s.metadata ->> 'snapshot_kind', 'SETTLEMENT')
              = :snapshot_kind
        ORDER BY e.trade_at, e.option_security, e.occurrence_ordinal
        """
    )
    frame = pd.read_sql(
        query,
        engine,
        params={
            "snapshot_id": snapshot_id,
            "product": product,
            "snapshot_kind": snapshot_kind,
        },
    )
    if frame.empty:
        return frame
    for column in (
        "business_date", "underlying_contract_month", "option_expiration_date",
        "trade_at", "future_bid_at", "future_ask_at", "future_match_at",
        "window_start", "cutoff_at",
    ):
        frame[column] = pd.to_datetime(frame[column], errors="coerce", utc=(
            column in {
                "trade_at", "future_bid_at", "future_ask_at", "future_match_at",
                "window_start", "cutoff_at",
            }
        ))
    for column in (
        "strike", "trade_price", "trade_size", "future_bid", "future_ask",
        "future_mid", "future_match_price", "future_match_lag_ms", "trade_iv",
        "occurrence_ordinal",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_trade_tape(
    snapshot_id: str,
    engine=None,
    *,
    product: str = PRODUCT,
    snapshot_kind: str = "INTRADAY",
    refresh: bool = False,
) -> pd.DataFrame:
    product = _normalize_product(product)
    snapshot_kind = _normalize_snapshot_kind(snapshot_kind)
    if refresh:
        _cached_trade_tape.cache_clear()
    if engine is not None:
        return _read_trade_tape(product, snapshot_kind, snapshot_id, engine)
    return _cached_trade_tape(product, snapshot_kind, str(snapshot_id)).copy()


def _normalize_chain_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    normalized = frame.copy()
    defaults = {
        "pricing_underlying_security": None,
        "last_price": np.nan,
        "last_trade_date": pd.NaT,
        "last_update_date": pd.NaT,
        "open_interest_date": pd.NaT,
        "settlement_open_interest": np.nan,
        "settlement_open_interest_date": pd.NaT,
        "intraday_open_interest": np.nan,
        "intraday_open_interest_date": pd.NaT,
        "previous_option_security": None,
        "previous_volume": np.nan,
        "previous_business_date": pd.NaT,
        "previous_last_trade_date": pd.NaT,
        "previous_snapshot_metadata": {},
        "previous_observed_at": pd.NaT,
        "option_bid": np.nan,
        "option_ask": np.nan,
        "option_mid": np.nan,
        "underlying_mid": np.nan,
        "executable_iv_bid": np.nan,
        "executable_iv_mid": np.nan,
        "executable_iv_ask": np.nan,
        "executable_iv_status": None,
        "last_trade_at": pd.NaT,
        "last_trade_underlying_at": pd.NaT,
        "last_trade_iv": np.nan,
        "last_trade_iv_status": None,
        "last_trade_price": np.nan,
        "last_trade_underlying_price": np.nan,
        "last_trade_underlying_source": None,
        "last_trade_match_lag_ms": np.nan,
        "last_trade_condition_codes": None,
        "last_trade_iv_exclusion_reason": None,
        "last_trade_match_source_snapshot_id": None,
        "executable_iv_exclusion_reason": None,
        "quote_capture_skew_ms": np.nan,
        "underlying_bid": np.nan,
        "underlying_ask": np.nan,
        "option_spread": np.nan,
        "option_spread_pct": np.nan,
    }
    for column, default in defaults.items():
        if column not in normalized.columns:
            normalized[column] = [default] * len(normalized)
    normalized["pricing_underlying_security"] = normalized[
        "pricing_underlying_security"
    ].where(
        normalized["pricing_underlying_security"].notna(),
        normalized.get("underlying_security"),
    )
    for column in (
        "business_date",
        "observed_at",
        "underlying_contract_month",
        "underlying_last_tradeable_date",
        "option_expiration_date",
        "option_last_tradeable_date",
        "last_trade_date",
        "last_update_date",
        "open_interest_date",
        "settlement_open_interest_date",
        "intraday_open_interest_date",
        "ingested_at",
        "previous_observed_at",
        "previous_business_date",
        "previous_last_trade_date",
        "quote_request_started_at",
        "quote_response_at",
        "pricing_discount_rate_observed_at",
        "last_trade_at",
        "last_trade_underlying_at",
    ):
        if column in normalized.columns:
            normalized[column] = pd.to_datetime(normalized[column], errors="coerce")
    for column in (
        "underlying_price",
        "strike",
        "contract_multiplier",
        "pricing_discount_rate",
        "settlement_price",
        "last_price",
        "previous_volume",
        "volume",
        "open_interest",
        "settlement_open_interest",
        "intraday_open_interest",
        "implied_volatility",
        "option_bid",
        "option_ask",
        "option_mid",
        "option_spread",
        "option_spread_pct",
        "underlying_bid",
        "underlying_ask",
        "underlying_mid",
        "underlying_spread",
        "quote_batch_id",
        "quote_capture_skew_ms",
        "executable_iv_bid",
        "executable_iv_mid",
        "executable_iv_ask",
        "last_trade_price",
        "last_trade_underlying_price",
        "last_trade_match_lag_ms",
        "last_trade_iv",
    ):
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    metadata = _snapshot_metadata(normalized["snapshot_metadata"].iloc[0])
    normalized["snapshot_kind"] = str(
        metadata.get("snapshot_kind") or "SETTLEMENT"
    ).upper()
    intraday_mask = normalized["snapshot_kind"].eq("INTRADAY")
    normalized["source_volume"] = normalized["volume"]
    legacy_open_interest = normalized["open_interest"].copy()
    legacy_open_interest_date = normalized["open_interest_date"].copy()
    settlement_mask = ~intraday_mask
    normalized.loc[
        settlement_mask & normalized["settlement_open_interest"].isna(),
        "settlement_open_interest",
    ] = legacy_open_interest
    normalized.loc[
        settlement_mask & normalized["settlement_open_interest_date"].isna(),
        "settlement_open_interest_date",
    ] = normalized.loc[settlement_mask, "business_date"]
    normalized.loc[
        intraday_mask & normalized["settlement_open_interest"].isna(),
        "settlement_open_interest",
    ] = legacy_open_interest
    normalized.loc[
        intraday_mask & normalized["settlement_open_interest_date"].isna(),
        "settlement_open_interest_date",
    ] = legacy_open_interest_date
    normalized["source_open_interest"] = normalized[
        "settlement_open_interest"
    ]
    normalized["source_open_interest_date"] = normalized[
        "settlement_open_interest_date"
    ]
    normalized.loc[intraday_mask, "source_open_interest"] = normalized.loc[
        intraday_mask, "intraday_open_interest"
    ]
    normalized.loc[intraday_mask, "source_open_interest_date"] = normalized.loc[
        intraday_mask, "intraday_open_interest_date"
    ]
    normalized["open_interest"] = normalized["source_open_interest"]
    normalized["open_interest_date"] = normalized["source_open_interest_date"]
    normalized["open_interest_source"] = "Bloomberg settlement OPEN_INT"
    normalized.loc[intraday_mask, "open_interest_source"] = (
        "Bloomberg intraday RT_OPEN_INTEREST"
    )
    normalized["volume_scope_status"] = "settlement"
    normalized["open_interest_scope_status"] = "settlement"
    if intraday_mask.any():
        business_dates = pd.to_datetime(
            normalized["business_date"], errors="coerce"
        ).dt.normalize()
        last_trade_dates = pd.to_datetime(
            normalized["last_trade_date"], errors="coerce"
        ).dt.normalize()
        open_interest_dates = pd.to_datetime(
            normalized["source_open_interest_date"], errors="coerce"
        ).dt.normalize()
        same_day_volume = intraday_mask & last_trade_dates.eq(business_dates)
        same_day_open_interest = (
            intraday_mask & open_interest_dates.eq(business_dates)
        )
        normalized.loc[
            intraday_mask & normalized["source_volume"].isna(),
            "volume_scope_status",
        ] = "unavailable"
        normalized.loc[
            intraday_mask
            & normalized["source_volume"].notna()
            & last_trade_dates.isna(),
            "volume_scope_status",
        ] = "trade_date_unavailable_excluded"
        normalized.loc[
            intraday_mask
            & normalized["source_volume"].notna()
            & last_trade_dates.notna()
            & ~same_day_volume,
            "volume_scope_status",
        ] = "prior_session_excluded"
        normalized.loc[same_day_volume, "volume_scope_status"] = "same_day"
        normalized.loc[intraday_mask & ~same_day_volume, "volume"] = np.nan

        normalized.loc[
            intraday_mask & normalized["source_open_interest"].isna(),
            "open_interest_scope_status",
        ] = "unavailable"
        normalized.loc[
            intraday_mask
            & normalized["source_open_interest"].notna()
            & open_interest_dates.isna(),
            "open_interest_scope_status",
        ] = "effective_date_unavailable"
        normalized.loc[
            intraday_mask
            & normalized["source_open_interest"].notna()
            & open_interest_dates.notna()
            & ~same_day_open_interest,
            "open_interest_scope_status",
        ] = "stale"
        normalized.loc[
            same_day_open_interest, "open_interest_scope_status"
        ] = "same_day"

        previous_business_dates = pd.to_datetime(
            normalized["previous_business_date"], errors="coerce"
        ).dt.normalize()
        previous_last_trade_dates = pd.to_datetime(
            normalized["previous_last_trade_date"], errors="coerce"
        ).dt.normalize()
        previous_same_day_volume = (
            intraday_mask
            & previous_business_dates.eq(business_dates)
            & previous_last_trade_dates.eq(business_dates)
        )
        normalized.loc[
            intraday_mask & ~previous_same_day_volume, "previous_volume"
        ] = np.nan
    normalized["effective_price"] = normalized["settlement_price"]
    normalized.loc[intraday_mask, "effective_price"] = normalized.loc[
        intraday_mask, "option_mid"
    ]
    normalized["volume_delta"] = np.nan
    normalized["volume_delta_status"] = "not_applicable"
    if normalized["snapshot_kind"].iloc[0] == "INTRADAY":
        market_manifest = dict(dict(metadata.get("manifest") or {}).get("market_data") or {})
        current_exceptions = dict(market_manifest.get("option_field_exceptions") or {})
        current_errors = dict(market_manifest.get("option_security_errors") or {})
        previous_metadata = _snapshot_metadata(
            normalized["previous_snapshot_metadata"].iloc[0]
        )
        previous_market = dict(
            dict(previous_metadata.get("manifest") or {}).get("market_data") or {}
        )
        previous_exceptions = dict(
            previous_market.get("option_field_exceptions") or {}
        )
        previous_errors = dict(previous_market.get("option_security_errors") or {})
        for row_index, row in normalized.iterrows():
            security = str(row["option_security"])
            status = "available"
            if row.get("volume_scope_status") != "same_day":
                status = str(row.get("volume_scope_status") or "unavailable")
            elif pd.isna(row.get("previous_volume")) and not metadata.get("previous_snapshot_id"):
                status = "no_previous_snapshot"
            elif pd.isna(row.get("previous_option_security")):
                status = "new_or_previous_missing"
            elif security in current_errors or security in previous_errors:
                status = "security_error"
            elif "VOLUME" in current_exceptions.get(security, []):
                status = "current_volume_exception"
            elif "VOLUME" in previous_exceptions.get(security, []):
                status = "previous_volume_exception"
            else:
                current_volume = 0.0 if pd.isna(row["volume"]) else float(row["volume"])
                previous_volume = (
                    0.0 if pd.isna(row["previous_volume"]) else float(row["previous_volume"])
                )
                delta = current_volume - previous_volume
                if delta < 0:
                    status = "cumulative_volume_reset"
                else:
                    normalized.at[row_index, "volume_delta"] = delta
            normalized.at[row_index, "volume_delta_status"] = status
    return normalized


@lru_cache(maxsize=64)
def _cached_published_surface(
    product: str,
    snapshot_kind: str,
    cob_date: str,
) -> pd.DataFrame:
    return _read_published_surface(
        product,
        snapshot_kind,
        cob_date,
        get_database_engine(required=False),
    )


def _read_published_surface(
    product: str,
    snapshot_kind: str,
    cob_date: str,
    engine,
) -> pd.DataFrame:
    product = _normalize_product(product)
    snapshot_kind = _normalize_snapshot_kind(snapshot_kind)
    if snapshot_kind != "SETTLEMENT":
        return pd.DataFrame()
    if engine is None or not cob_date:
        return pd.DataFrame()
    query = text(
        f"""
        SELECT cob_date,
               maturity_date AS contract_date,
               option_expiration_date,
               put_call,
               delta,
               value AS volatility,
               forward_value,
               source_name,
               vendor_published_at,
               ingested_at,
               pricing_model,
               pricing_model_version
        FROM {PUBLISHED_TABLE}
        WHERE product = :product
          AND cob_date = :cob_date
          AND :snapshot_kind = 'SETTLEMENT'
        ORDER BY maturity_date, delta
        """
    )
    frame = pd.read_sql(
        query,
        engine,
        params={
            "product": _product_spec(product)["published_product"],
            "cob_date": pd.Timestamp(cob_date).date(),
            "snapshot_kind": snapshot_kind,
        },
    )
    if frame.empty:
        return frame
    for column in ("cob_date", "contract_date", "option_expiration_date"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in ("delta", "volatility", "forward_value"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_published_surface(
    cob_date: str,
    engine=None,
    *,
    product: str = PRODUCT,
    snapshot_kind: str = "SETTLEMENT",
    refresh: bool = False,
) -> pd.DataFrame:
    product = _normalize_product(product)
    snapshot_kind = _normalize_snapshot_kind(snapshot_kind)
    if refresh:
        _cached_published_surface.cache_clear()
    if engine is not None:
        return _read_published_surface(product, snapshot_kind, cob_date, engine)
    return _cached_published_surface(
        product, snapshot_kind, str(cob_date)
    ).copy()


def load_icap_settlement_surface(
    cob_date: str,
    *,
    refresh: bool = False,
    snapshot_loader=None,
) -> pd.DataFrame:
    """Reuse the governed TTF settlement snapshot contract."""
    if snapshot_loader is None:
        from surface_data import get_operational_surface_snapshot

        snapshot_loader = get_operational_surface_snapshot
    snapshot = snapshot_loader("TTF", cob_date, refresh=refresh)
    surface = snapshot.get("data")
    if not isinstance(surface, pd.DataFrame) or surface.empty:
        return pd.DataFrame()
    actual_cob = pd.to_datetime(snapshot.get("actual_cob"), errors="coerce")
    if pd.isna(actual_cob):
        return pd.DataFrame()
    surface = surface.copy()
    surface["cob_date"] = actual_cob.normalize()
    surface["forward_value"] = np.nan
    surface["source_name"] = f"ICAP settlement · COB {actual_cob:%Y-%m-%d}"
    return surface[
        [
            "cob_date",
            "contract_date",
            "option_expiration_date",
            "put_call",
            "delta",
            "volatility",
            "forward_value",
            "source_name",
        ]
    ]


def _empty_calibrated_surface(
    status: str,
    **metadata: Any,
) -> pd.DataFrame:
    frame = pd.DataFrame()
    frame.attrs["publication_status"] = status
    frame.attrs["publication_metadata"] = metadata
    return frame


def calibrated_publication_metadata(surface: pd.DataFrame | None) -> dict[str, Any]:
    if surface is None:
        return {}
    metadata = dict(surface.attrs.get("publication_metadata") or {})
    if surface.empty:
        return metadata
    aliases = {
        "publication_id": "publication_id",
        "run_id": "run_id",
        "publication_cob_date": "cob_date",
        "published_at": "published_at",
        "published_by": "published_by",
        "commodity": "commodity",
    }
    for column, key in aliases.items():
        if key in metadata or column not in surface.columns:
            continue
        values = surface[column].dropna()
        if not values.empty:
            metadata[key] = values.iloc[0]
    return metadata


def _read_calibrated_surface_points(
    publication_id: str,
    contract_dates: tuple[Any, ...],
    engine,
) -> pd.DataFrame:
    points_query = text(
        f"""
        SELECT s.contract_date,
               s.option_expiration_date,
               s.strike,
               s.delta,
               s.put_call,
               s.volatility,
               s.working_forward AS forward_value,
               s.source_name,
               s.calibration_basis,
               s.surface_region,
               s.blend_classification,
               s.calibration_method,
               s.calibration_policy_version,
               s.input_fingerprint,
               s.created_at
        FROM {CALIBRATED_SURFACE_TABLE} AS s
        WHERE s.publication_id = CAST(:publication_id AS uuid)
          AND s.contract_date IN :contract_dates
        ORDER BY s.contract_date, s.strike
        """
    ).bindparams(bindparam("contract_dates", expanding=True))
    frame = pd.read_sql(
        points_query,
        engine,
        params={
            "publication_id": publication_id,
            "contract_dates": contract_dates,
        },
    )
    if frame.empty:
        return frame
    for column in ("contract_date", "option_expiration_date", "created_at"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in ("strike", "delta", "volatility", "forward_value"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


@lru_cache(maxsize=8)
def _cached_calibrated_surface_points(
    publication_id: str,
    contract_dates: tuple[Any, ...],
) -> pd.DataFrame:
    engine = get_database_engine(required=False)
    if engine is None:
        return pd.DataFrame()
    return _read_calibrated_surface_points(publication_id, contract_dates, engine)


def load_latest_calibrated_surface(
    cob_date: str,
    contract_dates: list[str] | tuple[str, ...],
    engine=None,
    *,
    product: str = PRODUCT,
) -> pd.DataFrame:
    """Load the latest active calibrated publication for the selected product.

    Brent and both HH views use the active revision for the selected exact COB. Other products
    retain their current-publication comparison with a labelled publication COB.
    """
    product = _normalize_product(product)
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        return _empty_calibrated_surface("storage_unavailable")
    selected_cob = pd.to_datetime(cob_date, errors="coerce")
    normalized_contracts = tuple(
        sorted(
            {
                pd.Timestamp(value).date()
                for value in contract_dates or ()
                if not pd.isna(pd.to_datetime(value, errors="coerce"))
            }
        )
    )
    if pd.isna(selected_cob) or not normalized_contracts:
        return _empty_calibrated_surface("invalid_selection")
    commodity = _product_spec(product)["published_product"]
    exact_cob = product in EXACT_COB_SURFACE_PRODUCTS
    cob_filter = "AND p.cob_date = :selected_cob" if exact_cob else ""
    catalog_query = text(
        f"""
        SELECT p.publication_id,
               p.run_id,
               p.commodity,
               p.cob_date AS publication_cob_date,
               p.published_at,
               p.published_by
        FROM {CALIBRATED_PUBLICATION_TABLE} AS p
        WHERE p.commodity = :commodity
          AND p.status = 'published'
          AND p.is_active
          {cob_filter}
        ORDER BY p.cob_date DESC, p.published_at DESC, p.created_at DESC
        LIMIT 1
        """
    )
    catalog = pd.read_sql(
        catalog_query,
        db_engine,
        params={
            "commodity": commodity,
            **({"selected_cob": selected_cob.date()} if exact_cob else {}),
        },
    )
    if catalog.empty:
        return _empty_calibrated_surface(
            "no_publication",
            commodity=commodity,
            selected_cob=selected_cob.date().isoformat(),
        )
    publication = catalog.iloc[0]
    publication_id = str(publication["publication_id"])
    frame = (
        _read_calibrated_surface_points(
            publication_id,
            normalized_contracts,
            db_engine,
        )
        if engine is not None
        else _cached_calibrated_surface_points(
            publication_id,
            normalized_contracts,
        ).copy()
    )
    metadata = {
        "publication_id": publication_id,
        "run_id": str(publication["run_id"]),
        "commodity": str(publication["commodity"]),
        "cob_date": pd.Timestamp(publication["publication_cob_date"]),
        "published_at": pd.Timestamp(publication["published_at"]),
        "published_by": publication["published_by"],
        "selected_cob": selected_cob.date().isoformat(),
    }
    if frame.empty:
        return _empty_calibrated_surface("no_contract_overlap", **metadata)
    frame = frame.loc[
        frame["contract_date"].notna()
        & frame["strike"].gt(0.0)
        & frame["volatility"].gt(0.0)
        & frame["delta"].between(0.0, 1.0, inclusive="both")
    ].copy()
    if frame.empty:
        return _empty_calibrated_surface("invalid_points", **metadata)
    for column, value in (
        ("publication_id", metadata["publication_id"]),
        ("run_id", metadata["run_id"]),
        ("commodity", metadata["commodity"]),
        ("publication_cob_date", metadata["cob_date"]),
        ("published_at", metadata["published_at"]),
        ("published_by", metadata["published_by"]),
    ):
        frame[column] = value
    frame.attrs["publication_status"] = "available"
    frame.attrs["publication_metadata"] = metadata
    return frame


def prepare_market_observations(
    chain: pd.DataFrame,
    *,
    product: str | None = None,
) -> pd.DataFrame:
    if chain is None or chain.empty:
        return pd.DataFrame()
    business_dates = pd.to_datetime(chain["business_date"], errors="coerce").dropna()
    if business_dates.empty:
        return pd.DataFrame()
    resolved_product = _normalize_product(
        product
        or (
            chain["product"].iloc[0]
            if "product" in chain.columns and not chain.empty
            else PRODUCT
        )
    )
    # Non-Brent settlement IV is already governed in the Bloomberg pipeline. The page
    # shows every resolved exchange settlement as a reference and does not apply
    # Brent-specific calibration eligibility rules to it.
    if resolved_product != "BRENT":
        return pd.DataFrame()
    cob_date = business_dates.iloc[0].date()
    effective_price = (
        chain["effective_price"]
        if "effective_price" in chain.columns
        else chain.get("last_price", pd.Series(np.nan, index=chain.index)).where(
            chain.get("last_price", pd.Series(np.nan, index=chain.index)).notna(),
            chain["settlement_price"],
        )
    )
    options = pd.DataFrame(
        {
            "trade_date": chain["business_date"],
            "expiry": chain["underlying_contract_month"],
            "expiration_date": chain["option_expiration_date"],
            "option_type": chain["put_call"],
            "strike": chain["strike"],
            "price": effective_price,
            "open_interest": chain["open_interest"],
            "volume": chain["volume"],
            "option_volatility": chain["implied_volatility"] * 100.0,
            "observed_at": chain["observed_at"],
        }
    )
    forwards = (
        chain[["business_date", "underlying_contract_month", "underlying_price"]]
        .drop_duplicates()
        .rename(
            columns={
                "business_date": "trade_date",
                "underlying_contract_month": "expiry",
                "underlying_price": "forward",
            }
        )
    )
    prepared, _ = prepare_brent_calibration_observations(
        options,
        forwards,
        cob_date,
        use_supplied_governed_iv=True,
    )
    return prepared


def published_strike_nodes(
    surface: pd.DataFrame,
    forward_by_contract: dict[pd.Timestamp, float],
    cob_date: pd.Timestamp,
) -> pd.DataFrame:
    if surface is None or surface.empty:
        return pd.DataFrame()
    rows = []
    for row in surface.itertuples(index=False):
        contract_date = pd.Timestamp(row.contract_date).normalize()
        forward = _numeric_or_none(row.forward_value)
        if forward is None:
            forward = forward_by_contract.get(contract_date)
        volatility = _numeric_or_none(row.volatility)
        delta = _numeric_or_none(row.delta)
        expiration = pd.to_datetime(row.option_expiration_date, errors="coerce")
        if (
            forward is None
            or forward <= 0
            or volatility is None
            or volatility <= 0
            or delta is None
            or pd.isna(expiration)
        ):
            continue
        if volatility > 5:
            volatility /= 100.0
        if abs(delta) > 1:
            delta /= 100.0
        option_type = "put" if str(row.put_call).strip().lower().startswith("p") else "call"
        signed_delta = -abs(delta) if option_type == "put" else abs(delta)
        dte = (expiration.normalize() - cob_date.normalize()).days
        if dte <= 0:
            continue
        try:
            strike = delta_to_strike(
                signed_delta,
                float(forward),
                float(volatility),
                float(dte),
                option_type=option_type,
            )
        except (ArithmeticError, ValueError):
            continue
        rows.append(
            {
                "contract_date": contract_date,
                "strike": strike,
                "volatility": volatility,
                "forward": float(forward),
                "put_call": option_type,
                "delta": signed_delta,
                "source_name": row.source_name,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["contract_date", "strike"])


def _numeric_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _snapshot_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _contract_month_values(frame: pd.DataFrame) -> pd.Series:
    if frame is None or frame.empty or "underlying_contract_month" not in frame:
        return pd.Series(dtype="datetime64[ns]")
    return pd.to_datetime(
        frame["underlying_contract_month"], errors="coerce"
    ).dt.normalize()


def _filter_contract_months(
    frame: pd.DataFrame,
    contract_months: list[str] | tuple[str, ...],
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame.copy() if frame is not None else pd.DataFrame()
    allowed = set(
        pd.to_datetime(pd.Series(list(contract_months)), errors="coerce")
        .dropna()
        .dt.normalize()
    )
    return frame.loc[_contract_month_values(frame).isin(allowed)].copy()


def select_history_universe(
    chain: pd.DataFrame,
    snapshot_kind: str,
    product: str = PRODUCT,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Apply acquisition scope to intraday history; settlements stay complete."""
    if chain is None or chain.empty:
        return pd.DataFrame(), {}
    stored_months = sorted(_contract_month_values(chain).dropna().unique())
    if str(snapshot_kind).upper() != "INTRADAY":
        return chain.copy(), {
            "scope": "ALL_AVAILABLE",
            "selected_contract_months": [
                pd.Timestamp(value).date().isoformat() for value in stored_months
            ],
            "stored_contract_month_count": len(stored_months),
        }

    metadata = _snapshot_metadata(chain["snapshot_metadata"].iloc[0])
    universe = _snapshot_metadata(metadata.get("intraday_universe"))
    if not universe:
        universe = _snapshot_metadata(
            _snapshot_metadata(metadata.get("manifest")).get("intraday_universe")
        )
    requested = universe.get("requested_contract_months")
    policy_version = str(
        universe.get("policy_version")
        or metadata.get("intraday_universe_policy_version")
        or ""
    )
    if isinstance(requested, list) and requested:
        selected = _filter_contract_months(chain, requested)
        selected_months = sorted(_contract_month_values(selected).dropna().unique())
        return selected, {
            **universe,
            "policy_version": policy_version,
            "legacy_fallback": False,
            "selected_contract_months": [
                pd.Timestamp(value).date().isoformat() for value in selected_months
            ],
            "stored_contract_month_count": len(stored_months),
        }

    business_dates = pd.to_datetime(chain["business_date"], errors="coerce").dropna()
    if business_dates.empty:
        raise ValueError("intraday snapshot has no valid business date")
    business_month = business_dates.iloc[0].normalize().replace(day=1)
    spec = _product_spec(product)
    available = [
        pd.Timestamp(value)
        for value in stored_months
        if pd.Timestamp(value) >= business_month
    ]
    front = set(available[: int(spec["front_count"])])
    horizon_year = business_month.year + int(spec["through_year_offset"])
    anchors = {
        value
        for value in available
        if value.month in set(spec["anchor_months"])
        and value.year <= horizon_year
    }
    selected_months = sorted(front | anchors)
    selected_values = [value.date().isoformat() for value in selected_months]
    return _filter_contract_months(chain, selected_values), {
        "policy_version": spec["intraday_policy_version"],
        "scope": "POLICY_FILTERED",
        "legacy_fallback": True,
        "front_count": int(spec["front_count"]),
        "anchor_months": list(spec["anchor_months"]),
        "through_year_offset": int(spec["through_year_offset"]),
        "selected_contract_months": selected_values,
        "selected_underlying_count": len(selected_values),
        "stored_contract_month_count": len(stored_months),
    }


def publication_coverage(
    chain: pd.DataFrame,
    published: pd.DataFrame,
) -> tuple[int, int]:
    chain_contracts = set(
        pd.to_datetime(chain.get("underlying_contract_month"), errors="coerce")
        .dropna()
        .dt.normalize()
    )
    if published is None or published.empty:
        return 0, len(chain_contracts)
    published_contracts = set(
        pd.to_datetime(published.get("contract_date"), errors="coerce")
        .dropna()
        .dt.normalize()
    )
    return len(chain_contracts & published_contracts), len(chain_contracts)



def active_brent_publication(engine, trading_date) -> dict[str, Any]:
    """Read only the active exact-COB revision identity, without its dense grid."""
    with engine.connect() as connection:
        row = connection.execute(
            text("""
                SELECT publication_id, cob_date AS publication_date, published_at
                FROM at_lng.vol_surface_publications
                WHERE commodity = 'BRENT' AND status = 'published' AND is_active
                  AND cob_date = :cob_date
                ORDER BY published_at DESC, created_at DESC
                LIMIT 1
            """),
            {"cob_date": pd.Timestamp(trading_date).date()},
        ).mappings().one_or_none()
    if row is None:
        return {"publication_id": None, "publication_date": None, "published_at": None}
    return {
        "publication_id": str(row["publication_id"]),
        "publication_date": pd.Timestamp(row["publication_date"]).date().isoformat(),
        "published_at": pd.Timestamp(row["published_at"]).isoformat(),
    }


def read_brent_snapshot(engine, snapshot_id: str, kind: str) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text("""
                SELECT snapshot_id, business_date, observed_at, input_fingerprint
                FROM at_lng.vol_market_snapshots
                WHERE snapshot_id = CAST(:snapshot_id AS uuid)
                  AND commodity = 'BRENT' AND status = 'complete'
                  AND COALESCE(metadata ->> 'snapshot_kind', 'SETTLEMENT') = :kind
            """),
            {"snapshot_id": snapshot_id, "kind": kind},
        ).mappings().one_or_none()
    if row is None:
        raise ValueError(f"Pinned Brent {kind.lower()} snapshot is unavailable.")
    return dict(row)


def read_preceding_brent_settlement(engine, trading_date: date, as_of) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text("""
                SELECT snapshot_id, business_date, observed_at, input_fingerprint
                FROM at_lng.vol_market_snapshots
                WHERE commodity = 'BRENT' AND status = 'complete'
                  AND COALESCE(metadata ->> 'snapshot_kind', 'SETTLEMENT') = 'SETTLEMENT'
                  AND business_date <= :trading_date AND observed_at <= :as_of
                ORDER BY business_date DESC, observed_at DESC, created_at DESC,
                         snapshot_id DESC
                LIMIT 1
            """),
            {"trading_date": trading_date, "as_of": as_of},
        ).mappings().one_or_none()
    if row is None:
        raise ValueError("No point-in-time Brent settlement precedes this intraday snapshot.")
    return dict(row)

