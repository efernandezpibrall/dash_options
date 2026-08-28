"""Bloomberg Brent, TTF, and Henry Hub option-chain history."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Any

import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Input, Output, Patch, State, callback, ctx, dcc, html, no_update
from flask import has_request_context, request
from plotly.subplots import make_subplots
from sqlalchemy import bindparam, text

from options.calibration_engine.converters.delta import delta_to_strike, strike_to_delta
from options.calibration_engine.io.brent_market import (
    AMERICAN_TREE_STEPS,
    CALIBRATION_MONEYNESS_BAND,
    DISPLAY_MONEYNESS_BAND,
    MIN_OPEN_INTEREST,
    _american_implied_vol,
    prepare_brent_calibration_observations,
)
from options.option_contract_conventions import FlatDiscountCurve
from options.options_library import (
    american_futures_equity_style_implied_volatility,
    black_76_equity_style_implied_volatility,
)
from options.ttf_volatility import (
    TTFVolatilityError,
    implied_volatility_from_settlement as tfo_implied_volatility,
)
from runtime_config import get_database_engine
from brent_option_chain_refresh import (
    INTRADAY_REQUEST_KIND,
    SETTLEMENT_REQUEST_KIND,
    WORKER_FRESHNESS_SECONDS,
    get_refresh_job,
    get_worker_readiness,
    intraday_refresh_enabled,
    settlement_refresh_enabled,
    submit_refresh_job,
)
from vol_calibration.auth import (
    Permission,
    authorize,
    resolve_request_identity,
)


PRODUCT = "BRENT"
SUPPORTED_PRODUCTS = frozenset({"BRENT", "TFO", "ON", "LNE"})
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
}
SNAPSHOT_LIMIT = 20
CHAIN_TABLE = "at_lng.bbg_option_chain"
TRADE_EVENT_TABLE = "at_lng.bbg_option_trade_events"
TRADE_TAPE_RUN_TABLE = "at_lng.bbg_option_trade_tape_runs"
SNAPSHOT_TABLE = "at_lng.vol_market_snapshots"
PUBLISHED_TABLE = "at_lng.implied_volatility_surface_from_prices"
CALIBRATED_PUBLICATION_TABLE = "at_lng.vol_surface_publications"
CALIBRATED_SURFACE_TABLE = "at_lng.implied_volatility_surface_calibrated"
X_AXIS_STRIKE = "strike"
X_AXIS_DELTA = "delta"
SETTLEMENT_IV_LOWER_BOUND = 0.005
BRENT_INTRADAY_UNIVERSE_POLICY_VERSION = "brent-front6-jun-dec-y2-v1"
BRENT_INTRADAY_FRONT_COUNT = 6
BRENT_INTRADAY_ANCHOR_MONTHS = (6, 12)
BRENT_INTRADAY_THROUGH_YEAR_OFFSET = 2


def _normalize_product(product: Any) -> str:
    normalized = str(product or PRODUCT).strip().upper()
    return normalized if normalized in SUPPORTED_PRODUCTS else PRODUCT


def _product_spec(product: Any) -> dict[str, Any]:
    return PRODUCT_SPECS[_normalize_product(product)]


def _normalize_snapshot_kind(snapshot_kind: Any) -> str:
    normalized = str(snapshot_kind or "").strip().upper()
    if normalized not in {"INTRADAY", "SETTLEMENT"}:
        raise ValueError(f"Unsupported snapshot kind {snapshot_kind!r}")
    return normalized


def _safe_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: option-chain data could not be loaded"


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
        ), today_intraday AS (
            SELECT *,
                   row_number() OVER (
                       ORDER BY observed_at DESC, created_at DESC, snapshot_id DESC
                   ) AS intraday_rank
            FROM available
            WHERE snapshot_kind = :intraday_kind
              AND has_chain
              AND business_date =
                  (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Dubai')::date
        ), selected AS (
            SELECT snapshot_id, business_date, observed_at, input_fingerprint,
                   option_quote_count, forward_count, metadata, snapshot_kind,
                   0 AS sort_group
            FROM today_intraday
            WHERE intraday_rank = 1
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


def load_latest_calibrated_surface(
    cob_date: str,
    contract_dates: list[str] | tuple[str, ...],
    engine=None,
    *,
    product: str = PRODUCT,
) -> pd.DataFrame:
    """Load the latest active calibrated publication for the selected product.

    This is a current-publication comparison, not a point-in-time reconstruction:
    the latest active revision remains visible alongside an older selected market
    snapshot, with both the publication COB and timestamp carried into the chart.
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
        ORDER BY p.cob_date DESC, p.published_at DESC, p.created_at DESC
        LIMIT 1
        """
    )
    catalog = pd.read_sql(
        catalog_query,
        db_engine,
        params={"commodity": commodity},
    )
    if catalog.empty:
        return _empty_calibrated_surface(
            "no_publication",
            commodity=commodity,
            selected_cob=selected_cob.date().isoformat(),
        )
    publication = catalog.iloc[0]
    publication_id = str(publication["publication_id"])
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
        db_engine,
        params={
            "publication_id": publication_id,
            "contract_dates": normalized_contracts,
        },
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
    for column in ("contract_date", "option_expiration_date", "created_at"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in ("strike", "delta", "volatility", "forward_value"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
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


def _hover_count_strings(values: pd.Series) -> pd.Series:
    """Format activity counts once so missing values render as an em dash."""
    return pd.to_numeric(values, errors="coerce").map(
        lambda value: f"{value:,.0f}" if pd.notna(value) else "—"
    )


def _hover_price_strings(values: pd.Series) -> pd.Series:
    """Format option premiums without exposing Plotly's ``nan`` placeholder."""
    return pd.to_numeric(values, errors="coerce").map(
        lambda value: f"{value:,.4f}" if pd.notna(value) else "—"
    )


def _hover_date_strings(values: pd.Series) -> pd.Series:
    """Keep effective dates compact; the selected business date carries the year."""
    return pd.to_datetime(values, errors="coerce").map(
        lambda value: value.strftime("%d %b") if pd.notna(value) else "—"
    )


def _hover_oi_status_strings(values: pd.Series) -> pd.Series:
    return values.fillna("unavailable").astype(str).map(
        {
            "same_day": "same day",
            "stale": "stale",
            "effective_date_unavailable": "date unavailable",
            "settlement": "official close",
            "unavailable": "unavailable",
        }
    ).fillna("reported")


def _expiry_mask(frame: pd.DataFrame, expiry: pd.Timestamp, column: str) -> pd.Series:
    return pd.to_datetime(frame[column], errors="coerce").dt.normalize().eq(expiry)


def _normalize_x_axis(value: Any) -> str:
    return X_AXIS_DELTA if str(value).strip().lower() == X_AXIS_DELTA else X_AXIS_STRIKE


def _option_side_for_strike(strike: Any, forward: Any) -> str:
    strike_value = _numeric_or_none(strike)
    forward_value = _numeric_or_none(forward)
    return (
        "P"
        if strike_value is not None
        and forward_value is not None
        and strike_value < forward_value
        else "C"
    )


def _display_delta(signed_delta: Any, put_call: Any = None) -> float:
    """Project signed Black-76 delta onto put wing -> ATM -> call wing."""
    delta = _numeric_or_none(signed_delta)
    if delta is None or not -1.0 <= delta <= 1.0:
        return np.nan
    is_put = (
        str(put_call).strip().lower().startswith("p")
        if put_call is not None
        else delta < 0.0
    )
    display = abs(delta) if is_put else 1.0 - delta
    return float(np.clip(display, 0.0, 1.0))


def _delta_x_from_market_inputs(
    *,
    strike: Any,
    forward: Any,
    volatility: Any,
    dte: Any,
    put_call: Any,
) -> float:
    strike_value = _numeric_or_none(strike)
    forward_value = _numeric_or_none(forward)
    volatility_value = _numeric_or_none(volatility)
    dte_value = _numeric_or_none(dte)
    if (
        strike_value is None
        or strike_value <= 0.0
        or forward_value is None
        or forward_value <= 0.0
        or volatility_value is None
        or volatility_value <= 0.0
        or dte_value is None
        or dte_value <= 0.0
    ):
        return np.nan
    option_type = (
        "put" if str(put_call).strip().lower().startswith("p") else "call"
    )
    try:
        signed_delta = strike_to_delta(
            strike_value,
            forward_value,
            volatility_value,
            dte_value,
            option_type=option_type,
        )
    except (ArithmeticError, ValueError):
        return np.nan
    return _display_delta(signed_delta, option_type)


def _row_delta_x(
    row: pd.Series,
    *,
    volatility_column: str,
    forward_column: str,
) -> float:
    expiration = pd.to_datetime(row.get("option_expiration_date"), errors="coerce")
    business_date = pd.to_datetime(row.get("business_date"), errors="coerce")
    if pd.isna(expiration) or pd.isna(business_date):
        return np.nan
    dte = (expiration.normalize() - business_date.normalize()).days
    return _delta_x_from_market_inputs(
        strike=row.get("strike"),
        forward=row.get(forward_column),
        volatility=row.get(volatility_column),
        dte=dte,
        put_call=row.get("put_call"),
    )


def _page_implied_volatility(
    product: str,
    put_call: str,
    price: float,
    forward: float,
    strike: float,
    time_to_expiry: float,
    discount_rate: float | None = None,
) -> float:
    resolved_product = _normalize_product(product)
    if resolved_product == "TFO":
        try:
            return float(
                tfo_implied_volatility(
                    put_call,
                    forward,
                    strike,
                    time_to_expiry,
                    price,
                )
            )
        except (ArithmeticError, TTFVolatilityError, ValueError):
            return float("nan")
    if resolved_product in {"ON", "LNE"}:
        if discount_rate is None or not math.isfinite(float(discount_rate)):
            return float("nan")
        curve = FlatDiscountCurve(
            float(discount_rate), "BLOOMBERG_OPT_FINANCE_RT"
        )
        inverter = (
            american_futures_equity_style_implied_volatility
            if resolved_product == "ON"
            else black_76_equity_style_implied_volatility
        )
        return float(
            inverter(
                put_call,
                price,
                forward,
                strike,
                time_to_expiry,
                curve,
                **({"steps": AMERICAN_TREE_STEPS} if resolved_product == "ON" else {}),
            )
        )
    return _american_implied_vol(
        put_call,
        price,
        forward,
        strike,
        time_to_expiry,
        steps=AMERICAN_TREE_STEPS,
    )


def _last_price_parity_quality(raw: pd.DataFrame) -> dict[str, Any]:
    """Audit TFO LAST_PRICE timing without treating it as a current smile."""
    result = {
        "status": "not_applicable",
        "pair_count": 0,
        "parity_forward": None,
        "parity_mad": None,
        "live_forward": None,
        "live_spread": None,
        "gap": None,
        "tolerance": None,
        "timestamp_coverage": 0.0,
    }
    if raw is None or raw.empty:
        return result
    work = raw.copy()
    if (
        _normalize_product(work.get("product", pd.Series([PRODUCT])).iloc[0]) != "TFO"
        or str(work.get("snapshot_kind", pd.Series([""])).iloc[0]).upper()
        != "INTRADAY"
    ):
        return result

    work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
    work["last_price"] = pd.to_numeric(work.get("last_price"), errors="coerce")
    work["put_call"] = work.get(
        "put_call", pd.Series("", index=work.index)
    ).astype(str).str.upper()
    valid = work.loc[
        work["strike"].gt(0.0)
        & work["last_price"].gt(0.0)
        & work["put_call"].isin(["C", "P"])
    ].copy()
    if valid.empty:
        result["status"] = "insufficient"
        return result

    paired = valid.pivot_table(
        index="strike", columns="put_call", values="last_price", aggfunc="median"
    )
    if "C" not in paired or "P" not in paired:
        result["status"] = "insufficient"
        return result
    paired = paired.dropna(subset=["C", "P"])
    parity_forwards = (
        pd.Series(paired.index.to_numpy(dtype=float), index=paired.index)
        + paired["C"]
        - paired["P"]
    )
    parity_forwards = parity_forwards.loc[
        np.isfinite(parity_forwards) & parity_forwards.gt(0.0)
    ]
    result["pair_count"] = int(len(parity_forwards))
    if parity_forwards.empty:
        result["status"] = "insufficient"
        return result

    parity_forward = float(parity_forwards.median())
    parity_mad = float((parity_forwards - parity_forward).abs().median())
    live_values = pd.to_numeric(
        work.get("underlying_mid", pd.Series(np.nan, index=work.index)),
        errors="coerce",
    )
    live_values = live_values.loc[live_values.gt(0.0)]
    live_forward = float(live_values.median()) if not live_values.empty else None
    bid = pd.to_numeric(
        work.get("underlying_bid", pd.Series(np.nan, index=work.index)),
        errors="coerce",
    )
    ask = pd.to_numeric(
        work.get("underlying_ask", pd.Series(np.nan, index=work.index)),
        errors="coerce",
    )
    spreads = (ask - bid).loc[ask.gt(bid) & bid.gt(0.0)]
    live_spread = float(spreads.median()) if not spreads.empty else 0.0
    timestamp_coverage = float(
        pd.to_datetime(
            valid.get("last_trade_date", pd.Series(pd.NaT, index=valid.index)),
            errors="coerce",
        ).notna().mean()
    )
    result.update(
        {
            "parity_forward": parity_forward,
            "parity_mad": parity_mad,
            "live_forward": live_forward,
            "live_spread": live_spread,
            "timestamp_coverage": timestamp_coverage,
        }
    )
    if len(parity_forwards) < 3:
        result["status"] = "insufficient"
        return result
    if live_forward is None:
        result["status"] = "unverifiable"
        return result

    tolerance = max(2.0 * live_spread, 0.25, 0.005 * live_forward)
    gap = live_forward - parity_forward
    result.update({"gap": gap, "tolerance": tolerance})
    if parity_mad > tolerance:
        result["status"] = "incoherent"
    elif abs(gap) <= tolerance:
        result["status"] = "current_compatible"
    else:
        result["status"] = "coherent_historical"
    return result


def _settlement_reference_selection(
    raw: pd.DataFrame,
    x_axis: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return strict-OTM settlement IVs and auditable chart exclusions.

    Liquidity governs calibration eligibility, not whether a published
    settlement is visible. Select the OTM option before testing IV availability;
    an intrinsic-value ITM option must never replace an unresolved OTM wing.
    """
    columns = [
        "strike",
        "put_call",
        "option_security",
        "settlement_price",
        "reference_iv",
        "forward",
        "volume",
        "open_interest",
        "calibration_status",
        "display_x",
    ]
    exclusion_columns = [
        "underlying_contract_month",
        "strike",
        "put_call",
        "option_security",
        "settlement_price",
        "reason_code",
        "reason",
    ]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=exclusion_columns)
    work = raw.copy()
    if str(work.get("snapshot_kind", pd.Series([""])).iloc[0]).upper() == "INTRADAY":
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=exclusion_columns)
    for column in (
        "strike",
        "underlying_price",
        "settlement_price",
        "implied_volatility",
        "volume",
        "open_interest",
    ):
        work[column] = pd.to_numeric(work.get(column), errors="coerce")
    work = work.loc[
        work["strike"].gt(0.0)
    ].copy()
    if work.empty:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=exclusion_columns)

    rows = []
    exclusions = []
    for strike, strike_rows in work.groupby("strike", sort=True):
        forward_values = pd.to_numeric(
            strike_rows["underlying_price"], errors="coerce"
        ).dropna()
        forward_values = forward_values.loc[forward_values.gt(0.0)]
        if forward_values.empty:
            exclusions.append(
                {
                    "underlying_contract_month": strike_rows.get(
                        "underlying_contract_month", pd.Series([pd.NaT])
                    ).iloc[0],
                    "strike": float(strike),
                    "put_call": None,
                    "option_security": None,
                    "settlement_price": None,
                    "reason_code": "pricing_future_unavailable",
                    "reason": "Pricing-future settlement is unavailable",
                }
            )
            continue
        forward = float(forward_values.median())
        preferred_side = _option_side_for_strike(strike, forward)
        preferred_rows = strike_rows.loc[
            strike_rows["put_call"].astype(str).str.upper().eq(preferred_side)
        ].copy()
        if preferred_rows.empty:
            exclusions.append(
                {
                    "underlying_contract_month": strike_rows.get(
                        "underlying_contract_month", pd.Series([pd.NaT])
                    ).iloc[0],
                    "strike": float(strike),
                    "put_call": preferred_side,
                    "option_security": None,
                    "settlement_price": None,
                    "reason_code": "otm_contract_unavailable",
                    "reason": "OTM option contract is unavailable",
                }
            )
            continue
        candidate = (
            preferred_rows.assign(
                _has_settlement=preferred_rows["settlement_price"].gt(0.0),
                _has_iv=preferred_rows["implied_volatility"].gt(0.0),
            )
            .sort_values(
                ["_has_settlement", "_has_iv", "option_security"],
                ascending=[False, False, True],
            )
            .iloc[0]
        )
        settlement_price = _numeric_or_none(candidate.get("settlement_price"))
        reference_iv = _numeric_or_none(candidate.get("implied_volatility"))
        reason_code = None
        reason = None
        if settlement_price is None or settlement_price <= 0.0:
            reason_code = "otm_settlement_unavailable"
            reason = "OTM settlement premium is unavailable"
        elif reference_iv is None or reference_iv <= 0.0:
            raw_reason = str(candidate.get("iv_exclusion_reason") or "").strip()
            if "(0.5%, 200%)" in raw_reason:
                reason_code = "otm_iv_outside_supported_range"
                reason = "OTM premium does not imply IV within 0.5%–200%"
            else:
                reason_code = "otm_iv_unavailable"
                reason = raw_reason or "OTM implied volatility is unavailable"
        elif reference_iv <= SETTLEMENT_IV_LOWER_BOUND + 1e-12:
            reason_code = "otm_iv_lower_boundary"
            reason = "OTM IV is pinned to the 0.5% numerical boundary"
        if reason_code is not None:
            exclusions.append(
                {
                    "underlying_contract_month": candidate.get(
                        "underlying_contract_month"
                    ),
                    "strike": float(strike),
                    "put_call": preferred_side,
                    "option_security": candidate.get("option_security"),
                    "settlement_price": settlement_price,
                    "reason_code": reason_code,
                    "reason": reason,
                }
            )
            continue
        display_x = (
            _row_delta_x(
                candidate,
                volatility_column="implied_volatility",
                forward_column="underlying_price",
            )
            if _normalize_x_axis(x_axis) == X_AXIS_DELTA
            else float(strike)
        )
        if not math.isfinite(display_x):
            exclusions.append(
                {
                    "underlying_contract_month": candidate.get(
                        "underlying_contract_month"
                    ),
                    "strike": float(strike),
                    "put_call": preferred_side,
                    "option_security": candidate.get("option_security"),
                    "settlement_price": settlement_price,
                    "reason_code": "delta_unavailable",
                    "reason": "Delta coordinate is unavailable",
                }
            )
            continue
        rows.append(
            {
                "strike": float(strike),
                "put_call": str(candidate["put_call"]).upper(),
                "option_security": candidate.get("option_security"),
                "settlement_price": float(candidate["settlement_price"]),
                "reference_iv": reference_iv,
                "forward": forward,
                "volume": candidate.get("volume"),
                "open_interest": candidate.get("open_interest"),
                "calibration_status": _settlement_calibration_status(
                    candidate,
                    forward,
                ),
                "display_x": float(display_x),
            }
        )
    selected = pd.DataFrame(rows, columns=columns).sort_values("display_x")
    excluded = pd.DataFrame(exclusions, columns=exclusion_columns)
    return selected, excluded


def _settlement_calibration_status(candidate: pd.Series, forward: float) -> str:
    """Summarize Brent calibration suitability without creating another trace."""
    if _normalize_product(candidate.get("product")) != "BRENT":
        return ""
    open_interest = _numeric_or_none(candidate.get("open_interest"))
    if open_interest is None:
        return "Not assessed · OI unavailable"
    if open_interest < MIN_OPEN_INTEREST:
        return (
            f"Excluded · OI {open_interest:,.0f} < "
            f"{MIN_OPEN_INTEREST:,.0f}"
        )
    strike = _numeric_or_none(candidate.get("strike"))
    if strike is None or forward <= 0.0:
        return "Not assessed · moneyness unavailable"
    moneyness = strike / forward
    display_low, display_high = DISPLAY_MONEYNESS_BAND
    if not display_low < moneyness < display_high:
        return "Excluded · outside supported moneyness"
    body_low, body_high = CALIBRATION_MONEYNESS_BAND
    if not body_low < moneyness < body_high:
        return "Excluded · outside calibration body"
    return "Eligible · OI and moneyness gates passed"


def _settlement_reference_smile(
    raw: pd.DataFrame,
    x_axis: str,
) -> pd.DataFrame:
    """Return only strict-OTM, price-valid Bloomberg settlement IVs."""
    selected, _ = _settlement_reference_selection(raw, x_axis)
    return selected


def _activity_delta_projection(
    raw: pd.DataFrame,
    trade_tape: pd.DataFrame | None = None,
    prior_settlement: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Map activity to current quotes, exact trades, then prior settlement."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["strike", "display_delta", "delta_source"])
    work = raw.copy()
    is_intraday = str(work["snapshot_kind"].iloc[0]).upper() == "INTRADAY"
    if is_intraday:
        primary_iv = pd.to_numeric(work["executable_iv_mid"], errors="coerce").where(
            work["executable_iv_status"].astype(str).eq("resolved")
        )
        forward_values = pd.to_numeric(work["underlying_mid"], errors="coerce").where(
            pd.to_numeric(work["underlying_mid"], errors="coerce").gt(0.0),
            pd.to_numeric(work["underlying_price"], errors="coerce"),
        )
    else:
        primary_iv = pd.to_numeric(work["implied_volatility"], errors="coerce")
        forward_values = pd.to_numeric(work["underlying_price"], errors="coerce")
    work["_primary_iv"] = primary_iv.where(primary_iv.gt(0.0))
    work["_forward"] = forward_values.where(forward_values.gt(0.0))
    reference = (
        work.loc[work["_primary_iv"].notna(), ["strike", "_primary_iv"]]
        .assign(strike=lambda frame: pd.to_numeric(frame["strike"], errors="coerce"))
        .dropna()
        .groupby("strike", as_index=False)["_primary_iv"]
        .mean()
        .sort_values("strike")
    )
    business_date = pd.to_datetime(work["business_date"], errors="coerce").dropna()
    expiration = pd.to_datetime(
        work["option_expiration_date"], errors="coerce"
    ).dropna()
    forwards = work["_forward"].dropna()
    if business_date.empty or expiration.empty or forwards.empty:
        return pd.DataFrame(columns=["strike", "display_delta", "delta_source"])
    dte = (expiration.iloc[0].normalize() - business_date.iloc[0].normalize()).days
    forward = float(forwards.median())
    if dte <= 0 or forward <= 0.0:
        return pd.DataFrame(columns=["strike", "display_delta", "delta_source"])

    activity_strikes = sorted(
        pd.to_numeric(work["strike"], errors="coerce").dropna().unique()
    )
    rows: list[dict[str, Any]] = []
    projected: set[float] = set()

    def add_curve_projection(
        curve: pd.DataFrame,
        *,
        curve_forward: float,
        curve_dte: int,
        label: str,
    ) -> None:
        if curve.empty or curve_forward <= 0.0 or curve_dte <= 0:
            return
        curve = curve.sort_values("strike")
        reference_strikes = curve["strike"].to_numpy(dtype=float)
        reference_vols = curve["_primary_iv"].to_numpy(dtype=float)
        direct_strikes = set(reference_strikes.tolist())
        for strike in activity_strikes:
            strike_value = float(strike)
            if strike_value in projected:
                continue
            reference_iv = float(
                np.interp(strike_value, reference_strikes, reference_vols)
            )
            if strike_value in direct_strikes:
                source = f"{label} at strike"
            elif reference_strikes[0] < strike_value < reference_strikes[-1]:
                source = f"Interpolated {label.lower()}"
            else:
                source = f"Nearest {label.lower()} wing"
            rows.append(
                {
                    "strike": strike_value,
                    "display_delta": _delta_x_from_market_inputs(
                        strike=strike_value,
                        forward=curve_forward,
                        volatility=reference_iv,
                        dte=curve_dte,
                        put_call=_option_side_for_strike(
                            strike_value, curve_forward
                        ),
                    ),
                    "delta_source": source,
                }
            )
            projected.add(strike_value)

    if not reference.empty:
        add_curve_projection(
            reference,
            curve_forward=forward,
            curve_dte=dte,
            label="Current executable smile",
        )

    if is_intraday and trade_tape is not None and not trade_tape.empty:
        trades = trade_tape.copy()
        contract_months = pd.to_datetime(
            trades.get(
                "underlying_contract_month",
                pd.Series(pd.NaT, index=trades.index),
            ),
            errors="coerce",
        ).dt.normalize()
        raw_month = pd.to_datetime(
            work["underlying_contract_month"], errors="coerce"
        ).dropna()
        if not raw_month.empty:
            trades = trades.loc[contract_months.eq(raw_month.iloc[0].normalize())]
        trades = trades.loc[
            trades.get(
                "trade_iv_status", pd.Series("", index=trades.index)
            ).astype(str).eq("resolved")
            & pd.to_numeric(
                trades.get("trade_iv", pd.Series(np.nan, index=trades.index)),
                errors="coerce",
            ).gt(0.0)
            & pd.to_numeric(
                trades.get(
                    "future_match_price", pd.Series(np.nan, index=trades.index)
                ),
                errors="coerce",
            ).gt(0.0)
        ].copy()
        if not trades.empty:
            trades["strike"] = pd.to_numeric(trades["strike"], errors="coerce")
            trades = (
                trades.sort_values("trade_at")
                .dropna(subset=["strike"])
                .drop_duplicates("strike", keep="last")
            )
            for trade in trades.itertuples(index=False):
                strike_value = float(trade.strike)
                if strike_value in projected or strike_value not in activity_strikes:
                    continue
                trade_expiration = pd.to_datetime(
                    trade.option_expiration_date, errors="coerce"
                )
                trade_business_date = pd.to_datetime(
                    trade.business_date, errors="coerce"
                )
                trade_dte = (
                    trade_expiration.normalize() - trade_business_date.normalize()
                ).days
                rows.append(
                    {
                        "strike": strike_value,
                        "display_delta": _delta_x_from_market_inputs(
                            strike=strike_value,
                            forward=float(trade.future_match_price),
                            volatility=float(trade.trade_iv),
                            dte=trade_dte,
                            put_call=_option_side_for_strike(
                                strike_value, float(trade.future_match_price)
                            ),
                        ),
                        "delta_source": (
                            "Trade-time IV · "
                            + _trade_match_source_label(
                                trade.future_match_source
                            )
                        ),
                    }
                )
                projected.add(strike_value)

    if is_intraday and prior_settlement is not None and not prior_settlement.empty:
        prior = prior_settlement.copy()
        prior["strike"] = pd.to_numeric(prior["strike"], errors="coerce")
        prior["_primary_iv"] = pd.to_numeric(
            prior["reference_iv"], errors="coerce"
        )
        prior = prior.dropna(subset=["strike", "_primary_iv"])
        prior_dates = pd.to_datetime(
            prior.get("business_date", pd.Series(dtype=object)), errors="coerce"
        ).dropna()
        prior_expirations = pd.to_datetime(
            prior.get("option_expiration_date", pd.Series(dtype=object)),
            errors="coerce",
        ).dropna()
        prior_forwards = pd.to_numeric(prior["forward"], errors="coerce").dropna()
        if not prior_dates.empty and not prior_expirations.empty and not prior_forwards.empty:
            prior_date = prior_dates.iloc[0].normalize()
            add_curve_projection(
                prior[["strike", "_primary_iv"]],
                curve_forward=float(prior_forwards.median()),
                curve_dte=(prior_expirations.iloc[0].normalize() - prior_date).days,
                label=f"Prior settlement {prior_date.strftime('%d %b %Y')}",
            )

    return pd.DataFrame(rows, columns=["strike", "display_delta", "delta_source"])


def _intraday_expiry_quality(
    raw: pd.DataFrame,
    trade_tape: pd.DataFrame | None,
    prior_settlement: pd.DataFrame | None,
) -> dict[str, Any]:
    """Return one compact, trader-facing quality state for an expiry."""
    if raw is None or raw.empty or str(raw["snapshot_kind"].iloc[0]).upper() != "INTRADAY":
        return {}
    executable_count = int(
        (
            raw.get("executable_iv_status", pd.Series(index=raw.index, dtype=object))
            .astype(str)
            .eq("resolved")
            & pd.to_numeric(
                raw.get("executable_iv_mid", pd.Series(np.nan, index=raw.index)),
                errors="coerce",
            ).gt(0.0)
        ).sum()
    )
    trade_count = 0
    if trade_tape is not None and not trade_tape.empty:
        trade_count = int(
            (
                trade_tape.get(
                    "trade_iv_status",
                    pd.Series(index=trade_tape.index, dtype=object),
                )
                .astype(str)
                .eq("resolved")
                & pd.to_numeric(
                    trade_tape.get(
                        "trade_iv", pd.Series(np.nan, index=trade_tape.index)
                    ),
                    errors="coerce",
                ).gt(0.0)
            ).sum()
        )
    elif "last_trade_iv_status" in raw:
        trade_count = int(
            (
                raw["last_trade_iv_status"].astype(str).eq("resolved")
                & pd.to_numeric(raw["last_trade_iv"], errors="coerce").gt(0.0)
            ).sum()
        )

    prior_count = 0 if prior_settlement is None else int(len(prior_settlement))
    prior_dates = (
        pd.Series(dtype="datetime64[ns]")
        if prior_settlement is None or prior_settlement.empty
        else pd.to_datetime(prior_settlement.get("business_date"), errors="coerce").dropna()
    )
    prior_label = (
        prior_dates.iloc[0].strftime("%d %b %Y") if not prior_dates.empty else None
    )
    parity = _last_price_parity_quality(raw)

    if executable_count:
        label = "Live executable"
        color = "success"
        detail = f"{executable_count:,} synchronized two-sided IV observations"
    elif trade_count:
        label = "Trades only"
        color = "info"
        detail = f"No executable smile · {trade_count:,} resolved trade-time IV observations"
    elif prior_count:
        label = f"Prior settle · {prior_label}" if prior_label else "Prior settle reference"
        color = "secondary"
        detail = "No reliable current IV"
    else:
        label = "No reliable IV"
        color = "danger"
        detail = "No executable quotes, matched trades, or prior settlement reference"

    if not executable_count and parity["status"] == "coherent_historical":
        direction = "higher" if float(parity["gap"]) > 0.0 else "lower"
        detail += (
            f" · FJS last-price parity implies TZT {parity['parity_forward']:.4f}; "
            f"live TZT {parity['live_forward']:.4f} is {abs(parity['gap']):.4f} "
            f"EUR/MWh {direction}"
        )
    elif not executable_count and parity["status"] == "incoherent":
        detail += " · FJS last-price pairs are internally inconsistent"
    elif not executable_count and parity["status"] in {"insufficient", "unverifiable"}:
        detail += " · Bloomberg LAST_PRICE timing cannot be verified"

    return {
        "status": label,
        "color": color,
        "detail": detail,
        "executable_count": executable_count,
        "trade_count": trade_count,
        "prior_settlement_count": prior_count,
        "prior_settlement_date": prior_label,
        "last_price_parity": parity,
    }


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
    if _normalize_x_axis(x_axis) == X_AXIS_STRIKE:
        return float(row["strike"])
    business_date = pd.Timestamp(row["business_date"]).date()
    expiration = pd.Timestamp(row["option_expiration_date"]).date()
    dte = (expiration - business_date).days
    return _delta_x_from_market_inputs(
        strike=row.get("strike"),
        forward=row.get("future_match_price"),
        volatility=row.get("trade_iv"),
        dte=dte,
        put_call=row.get("put_call"),
    )


_TRADE_MATCH_SOURCE_LABELS = {
    "QUOTE_MID": "Exact mid",
    "PREVAILING_MID": "Prevailing mid",
    "TRADE": "Future trade",
}


def _trade_match_source_label(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return _TRADE_MATCH_SOURCE_LABELS.get(normalized, normalized or "—")


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
) -> dict[str, dict[str, Any]]:
    empty = {
        side: {
            "x": [], "y": [], "customdata": [], "size": [], "symbol": [],
            "line_color": [], "line_width": [],
        }
        for side in ("C", "P")
    }
    if trade_tape is None or trade_tape.empty:
        return empty
    selected = trade_tape.loc[
        _expiry_mask(trade_tape, expiry, "underlying_contract_month")
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
    latest_at = pd.to_datetime(selected["trade_at"], errors="coerce", utc=True).max()
    selected["trade_time_gst"] = pd.to_datetime(
        selected["trade_at"], errors="coerce", utc=True
    ).dt.tz_convert("Asia/Dubai").dt.strftime("%H:%M:%S GST")
    result = dict(empty)
    for put_call in ("C", "P"):
        side = selected.loc[selected["put_call"].eq(put_call)].copy()
        if side.empty:
            continue
        sizes = pd.to_numeric(side["trade_size"], errors="coerce")
        marker_sizes = (6.0 + 1.8 * np.sqrt(sizes.fillna(0.0).clip(lower=0.0))).clip(
            upper=14.0
        )
        source = side["future_match_source"].fillna("").astype(str).str.upper()
        source_labels = source.map(_trade_match_source_label)
        event_times = pd.to_datetime(side["trade_at"], errors="coerce", utc=True)
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
        most_recent = event_times.eq(latest_at)
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
                ]
            ).tolist(),
            "size": marker_sizes.astype(float).tolist(),
            "symbol": source.map(
                {
                    "QUOTE_MID": "circle",
                    "PREVAILING_MID": "circle-open",
                    "TRADE": "diamond-open",
                }
            ).fillna("circle-open").tolist(),
            "line_color": np.where(most_recent, "#F97316", "#FFFFFF").tolist(),
            "line_width": np.where(most_recent, 2.4, 0.7).astype(float).tolist(),
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
        _expiry_mask(selected, expiry, "underlying_contract_month")
    ].sort_values("trade_at", ascending=False)
    rows = []
    for row in selected.itertuples(index=False):
        trade_at = pd.Timestamp(row.trade_at).tz_convert("Asia/Dubai")
        quote_age_label = _trade_quote_age_label(
            row.trade_at,
            getattr(row, "future_bid_at", None),
            getattr(row, "future_ask_at", None),
        )
        rows.append(
            {
                "event_id": f"{row.event_fingerprint}:{int(row.occurrence_ordinal)}",
                "trade_time_gst": trade_at.strftime("%H:%M:%S.%f")[:-3],
                "option_security": row.option_security,
                "put_call": row.put_call,
                "strike": _numeric_or_none(row.strike),
                "trade_price": _numeric_or_none(row.trade_price),
                "trade_size": _numeric_or_none(row.trade_size),
                "condition_codes": row.condition_codes or "regular",
                "future_match_price": _numeric_or_none(row.future_match_price),
                "future_match_source": _trade_match_source_label(
                    row.future_match_source
                ),
                "future_match_lag_ms": _numeric_or_none(row.future_match_lag_ms),
                "future_quote_ages": quote_age_label,
                "trade_iv_pct": (
                    None if _numeric_or_none(row.trade_iv) is None
                    else 100.0 * float(row.trade_iv)
                ),
                "trade_iv_status": row.trade_iv_status,
                "trade_iv_exclusion_reason": row.trade_iv_exclusion_reason,
            }
        )
    return rows


def build_expiry_figure(
    chain: pd.DataFrame,
    prepared: pd.DataFrame,
    published_nodes: pd.DataFrame,
    expiry: pd.Timestamp,
    x_axis: str = X_AXIS_STRIKE,
    trade_tape: pd.DataFrame | None = None,
    prior_settlement_chain: pd.DataFrame | None = None,
    product: str | None = None,
    calibrated_nodes: pd.DataFrame | None = None,
) -> go.Figure:
    x_axis = _normalize_x_axis(x_axis)
    raw = chain.loc[_expiry_mask(chain, expiry, "underlying_contract_month")].copy()
    resolved_product = _normalize_product(
        product
        or (
            raw["product"].iloc[0]
            if "product" in raw.columns and not raw.empty
            else PRODUCT
        )
    )
    spec = _product_spec(resolved_product)
    underlying_hover_label = spec["underlying_label"]
    if "volume_delta" not in raw.columns:
        raw["volume_delta"] = np.nan
    if "snapshot_kind" not in raw.columns:
        raw["snapshot_kind"] = "SETTLEMENT"
    if "open_interest_date" not in raw.columns:
        raw["open_interest_date"] = pd.NaT
    if "open_interest_scope_status" not in raw.columns:
        raw["open_interest_scope_status"] = np.where(
            raw["snapshot_kind"].eq("INTRADAY"),
            "effective_date_unavailable",
            "settlement",
        )
    is_intraday = raw["snapshot_kind"].iloc[0] == "INTRADAY"
    executable_mask = (
        raw.get("executable_iv_status", pd.Series(index=raw.index, dtype=object))
        .astype(str)
        .eq("resolved")
        & pd.to_numeric(
            raw.get("executable_iv_mid", pd.Series(np.nan, index=raw.index)),
            errors="coerce",
        ).notna()
    )
    prior_raw = (
        prior_settlement_chain.loc[
            _expiry_mask(
                prior_settlement_chain,
                expiry,
                "underlying_contract_month",
            )
        ].copy()
        if is_intraday
        and prior_settlement_chain is not None
        and not prior_settlement_chain.empty
        else pd.DataFrame()
    )
    prior_reference = (
        _settlement_reference_smile(prior_raw, x_axis)
        if not prior_raw.empty
        else pd.DataFrame()
    )
    if not prior_reference.empty:
        current_strikes = set(
            pd.to_numeric(raw["strike"], errors="coerce").dropna().astype(float)
        )
        prior_reference = prior_reference.loc[
            pd.to_numeric(prior_reference["strike"], errors="coerce").isin(
                current_strikes
            )
        ].copy()
        prior_dates = pd.to_datetime(
            prior_raw["business_date"], errors="coerce"
        ).dropna()
        prior_expirations = pd.to_datetime(
            prior_raw["option_expiration_date"], errors="coerce"
        ).dropna()
        if prior_dates.empty:
            prior_reference = pd.DataFrame()
        else:
            prior_reference["business_date"] = prior_dates.iloc[0]
            prior_reference["option_expiration_date"] = (
                prior_expirations.iloc[0]
                if not prior_expirations.empty
                else pd.NaT
            )
    published = (
        published_nodes.loc[_expiry_mask(published_nodes, expiry, "contract_date")].copy()
        if published_nodes is not None and not published_nodes.empty
        else pd.DataFrame()
    )
    calibrated = (
        calibrated_nodes.loc[
            _expiry_mask(calibrated_nodes, expiry, "contract_date")
        ].copy()
        if calibrated_nodes is not None and not calibrated_nodes.empty
        else pd.DataFrame()
    )
    figure = make_subplots(
        rows=1,
        cols=1,
        specs=[[{"secondary_y": True}]],
    )
    activity = (
        raw.groupby(["strike", "put_call"], as_index=False)
        .agg(
            volume=("volume", lambda values: values.sum(min_count=1)),
            open_interest=("open_interest", lambda values: values.sum(min_count=1)),
            volume_delta=("volume_delta", lambda values: values.sum(min_count=1)),
            underlying_price=("underlying_price", "median"),
            open_interest_date=("open_interest_date", "max"),
            open_interest_scope_status=("open_interest_scope_status", "first"),
        )
        .sort_values("strike")
    )
    activity_data_mask = activity[["volume", "open_interest"]].notna().any(axis=1)
    activity_strikes_with_data = set(
        pd.to_numeric(
            activity.loc[activity_data_mask, "strike"], errors="coerce"
        ).dropna()
    )
    if x_axis == X_AXIS_DELTA:
        activity = activity.merge(
            _activity_delta_projection(
                raw,
                trade_tape=trade_tape,
                prior_settlement=prior_reference,
            ),
            how="left",
            on="strike",
            validate="many_to_one",
        )
        activity = activity.loc[activity["display_delta"].notna()].copy()
    else:
        activity["display_delta"] = pd.to_numeric(activity["strike"], errors="coerce")
        activity["delta_source"] = "Strike"
    projected_activity_strikes = set(
        pd.to_numeric(activity["strike"], errors="coerce").dropna()
    )
    missing_activity_strikes = len(
        activity_strikes_with_data - projected_activity_strikes
    )
    activity_pivot = activity.pivot(
        index="strike",
        columns="put_call",
        values=["volume", "open_interest", "volume_delta"],
    ).sort_index()
    strikes = activity_pivot.index.to_numpy(dtype=float)
    activity_by_strike = activity.drop_duplicates("strike").set_index("strike")
    axis_values = pd.to_numeric(
        activity_by_strike["display_delta"], errors="coerce"
    ).reindex(activity_pivot.index)
    delta_sources = activity_by_strike["delta_source"].reindex(activity_pivot.index)
    underlying_prices = pd.to_numeric(
        activity_by_strike["underlying_price"], errors="coerce"
    ).reindex(activity_pivot.index)
    axis_differences = np.diff(np.unique(axis_values.dropna().to_numpy(dtype=float)))
    positive_differences = axis_differences[axis_differences > 0]
    axis_spacing = (
        float(np.median(positive_differences))
        if positive_differences.size
        else (
            0.02
            if x_axis == X_AXIS_DELTA or not strikes.size
            else max(float(strikes[0]) * 0.01, 0.5)
        )
    )
    if x_axis == X_AXIS_DELTA:
        open_interest_width = float(np.clip(0.82 * axis_spacing, 0.006, 0.035))
        volume_width = float(np.clip(0.40 * axis_spacing, 0.003, 0.018))
    else:
        open_interest_width = 0.82 * axis_spacing
        volume_width = 0.40 * axis_spacing
    activity_colors = {"C": "#2563EB", "P": "#0F766E"}
    activity_patterns = {"C": "", "P": "/"}

    def activity_values(metric: str, put_call: str) -> pd.Series:
        key = (metric, put_call)
        if key not in activity_pivot.columns:
            return pd.Series(index=activity_pivot.index, dtype=float)
        return pd.to_numeric(activity_pivot[key], errors="coerce")

    call_open_interest = activity_values("open_interest", "C")
    put_open_interest = activity_values("open_interest", "P")
    call_volume = activity_values("volume", "C")
    put_volume = activity_values("volume", "P")
    call_volume_delta = activity_values("volume_delta", "C")
    put_volume_delta = activity_values("volume_delta", "P")

    def add_activity_bar(
        values: pd.Series,
        *,
        put_call: str,
        metric_label: str,
        width: float,
        opacity: float,
        base: pd.Series,
        legendrank: int,
        deltas: pd.Series | None = None,
    ) -> None:
        if not values.notna().any():
            return
        option_label = "calls" if put_call == "C" else "puts"
        aligned_volume_deltas = (
            pd.to_numeric(deltas, errors="coerce").reindex(activity_pivot.index)
            if deltas is not None
            else pd.Series(np.nan, index=activity_pivot.index)
        )
        side_activity = activity.loc[activity["put_call"].eq(put_call)].set_index(
            "strike"
        )
        side_volume = pd.to_numeric(
            side_activity["volume"], errors="coerce"
        ).reindex(activity_pivot.index)
        side_open_interest = pd.to_numeric(
            side_activity["open_interest"], errors="coerce"
        ).reindex(activity_pivot.index)
        open_interest_as_of = _hover_date_strings(
            side_activity["open_interest_date"].reindex(activity_pivot.index)
        )
        open_interest_status = _hover_oi_status_strings(
            side_activity["open_interest_scope_status"].reindex(
                activity_pivot.index
            )
        )
        positive_delta = aligned_volume_deltas.fillna(0.0).gt(0.0)
        line_colors = [
            "#F97316" if is_positive else activity_colors[put_call]
            for is_positive in positive_delta
        ]
        line_widths = [2.2 if is_positive else 0.7 for is_positive in positive_delta]
        axis_hover = (
            " · Δ %{x:.3f}<br>%{customdata[2]}"
            if x_axis == X_AXIS_DELTA
            else ""
        )
        new_volume = (
            " · New <b>%{customdata[3]:,.0f}</b>"
            if metric_label == "Volume" and deltas is not None
            else ""
        )
        hovertemplate = (
            f"<b>{option_label.title()} activity</b>"
            "<br>Strike %{customdata[0]:.2f}"
            + axis_hover
            + f"<br>{underlying_hover_label} %{{customdata[4]:.3f}}"
            + "<br>Volume / OI <b>%{customdata[7]} / %{customdata[8]}</b>"
            + new_volume
            + "<br>OI %{customdata[5]} · %{customdata[6]}"
            + "<extra></extra>"
        )
        figure.add_trace(
            go.Bar(
                x=axis_values,
                y=values,
                base=base,
                width=width,
                name=f"{metric_label} · {option_label}",
                legendgroup=f"{metric_label.lower().replace(' ', '-')}-{put_call}",
                legendrank=legendrank,
                meta={
                    "legend_layer": (
                        "open-interest" if metric_label == "Open interest" else "volume"
                    )
                    + ("-calls" if put_call == "C" else "-puts")
                },
                opacity=opacity,
                marker={
                    "color": activity_colors[put_call],
                    "line": {"color": line_colors, "width": line_widths},
                    "pattern": {"shape": activity_patterns[put_call]},
                },
                customdata=np.column_stack(
                    [
                        strikes,
                        axis_values,
                        delta_sources,
                        aligned_volume_deltas,
                        underlying_prices,
                        open_interest_as_of,
                        open_interest_status,
                        _hover_count_strings(side_volume),
                        _hover_count_strings(side_open_interest),
                    ]
                ),
                hovertemplate=hovertemplate,
            ),
            secondary_y=True,
        )

    zero_base = pd.Series(0.0, index=activity_pivot.index)
    add_activity_bar(
        call_open_interest,
        put_call="C",
        metric_label="Open interest",
        width=open_interest_width,
        opacity=0.24,
        base=zero_base,
        legendrank=60,
    )
    add_activity_bar(
        put_open_interest,
        put_call="P",
        metric_label="Open interest",
        width=open_interest_width,
        opacity=0.24,
        base=call_open_interest.fillna(0.0),
        legendrank=70,
    )
    add_activity_bar(
        call_volume,
        put_call="C",
        metric_label="Volume",
        width=volume_width,
        opacity=0.88,
        base=zero_base,
        legendrank=40,
        deltas=(
            call_volume_delta
            if raw["snapshot_kind"].iloc[0] == "INTRADAY"
            else None
        ),
    )
    add_activity_bar(
        put_volume,
        put_call="P",
        metric_label="Volume",
        width=volume_width,
        opacity=0.88,
        base=call_volume.fillna(0.0),
        legendrank=50,
        deltas=(
            put_volume_delta
            if raw["snapshot_kind"].iloc[0] == "INTRADAY"
            else None
        ),
    )
    if x_axis == X_AXIS_DELTA and missing_activity_strikes:
        figure.add_annotation(
            x=0.5,
            y=0.01,
            xref="paper",
            yref="paper",
            text=(
                f"{missing_activity_strikes} activity strike"
                f"{'s' if missing_activity_strikes != 1 else ''} unavailable on Delta: "
                "no quality-approved IV reference"
            ),
            showarrow=False,
            bgcolor="rgba(255,247,237,0.94)",
            bordercolor="#FDBA74",
            borderpad=4,
            font={"color": "#9A3412", "size": 10},
        )

    executable = pd.DataFrame()
    settlement_reference = pd.DataFrame()
    matched_trades = pd.DataFrame()
    axis_hover = " · Δ %{x:.3f}" if x_axis == X_AXIS_DELTA else ""
    if is_intraday:
        executable = raw.loc[executable_mask].copy()
        if not prior_reference.empty:
            prior_date = pd.to_datetime(
                prior_reference["business_date"], errors="coerce"
            ).dropna().iloc[0]
            figure.add_trace(
                go.Scatter(
                    x=prior_reference["display_x"],
                    y=100.0 * prior_reference["reference_iv"],
                    mode="lines",
                    name=f"Prior settlement IV · {prior_date.strftime('%d %b %Y')}",
                    legendrank=25,
                    legendgroup="prior-settlement",
                    meta={"legend_layer": "prior-settlement"},
                    line={"color": "#94A3B8", "width": 1.2, "dash": "dash"},
                    customdata=np.column_stack(
                        [
                            prior_reference["strike"],
                            prior_reference["option_security"],
                            prior_reference["settlement_price"],
                            prior_reference["forward"],
                        ]
                    ),
                    hovertemplate=(
                        f"<b>Prior official settlement · {prior_date.strftime('%d %b %Y')}</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[1]} · Premium <b>%{customdata[2]:.4f} "
                        + spec["price_unit"]
                        + "</b>"
                        f" · {underlying_hover_label} settle %{{customdata[3]:.3f}}"
                        "<extra></extra>"
                    ),
                ),
                secondary_y=False,
            )
        side_colors = {"C": "#2563EB", "P": "#0F766E"}
        for put_call, side_label in (("C", "Calls"), ("P", "Puts")):
            side = executable.loc[executable["put_call"].eq(put_call)].sort_values("strike")
            side["_axis_x"] = (
                side.apply(
                    _row_delta_x,
                    axis=1,
                    volatility_column="executable_iv_mid",
                    forward_column="underlying_mid",
                )
                if x_axis == X_AXIS_DELTA
                else pd.to_numeric(side["strike"], errors="coerce")
            )
            side = side.loc[side["_axis_x"].notna()].sort_values("_axis_x")
            if side.empty:
                continue
            figure.add_trace(
                go.Scatter(
                    x=side["_axis_x"],
                    y=100.0 * side["executable_iv_bid"],
                    mode="lines",
                    line={"color": side_colors[put_call], "width": 0},
                    showlegend=False,
                    hoverinfo="skip",
                    legendgroup=f"exec-{put_call}",
                    meta={"legend_layer": "executable-band"},
                ),
                secondary_y=False,
            )
            figure.add_trace(
                go.Scatter(
                    x=side["_axis_x"],
                    y=100.0 * side["executable_iv_ask"],
                    mode="lines",
                    name=f"Executable IV band · {side_label}",
                    legendrank=12,
                    legendgroup=f"exec-{put_call}",
                    meta={"legend_layer": "executable-band"},
                    fill="tonexty",
                    fillcolor=(
                        "rgba(37,99,235,0.14)"
                        if put_call == "C"
                        else "rgba(15,118,110,0.14)"
                    ),
                    line={"color": side_colors[put_call], "width": 0.7},
                    hoverinfo="skip",
                ),
                secondary_y=False,
            )
            customdata = np.column_stack(
                [
                    side["strike"],
                    side["option_bid"],
                    side["option_mid"],
                    side["option_ask"],
                    side["underlying_bid"],
                    side["underlying_mid"],
                    side["underlying_ask"],
                    side["quote_capture_skew_ms"],
                    side.get(
                        "option_security",
                        pd.Series(
                            f"{side_label[:-1]} option", index=side.index
                        ),
                    ),
                    _hover_count_strings(side["volume"]),
                    _hover_count_strings(side["open_interest"]),
                    _hover_date_strings(side["open_interest_date"]),
                    _hover_oi_status_strings(side["open_interest_scope_status"]),
                ]
            )
            figure.add_trace(
                go.Scatter(
                    x=side["_axis_x"],
                    y=100.0 * side["executable_iv_mid"],
                    mode="lines",
                    name=f"Executable mid IV · {side_label}",
                    legendrank=10,
                    legendgroup=f"exec-{put_call}",
                    meta={
                        "legend_layer": (
                            "call-mid" if put_call == "C" else "put-mid"
                        )
                    },
                    line={"color": side_colors[put_call], "width": 1.6},
                    customdata=customdata,
                    hovertemplate=(
                        f"<b>Executable {side_label[:-1].lower()}</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[8]} · Option B/M/A "
                        "%{customdata[1]:.4f} / %{customdata[2]:.4f} / %{customdata[3]:.4f}"
                        f"<br>{underlying_hover_label} B/M/A "
                        "%{customdata[4]:.3f} / %{customdata[5]:.3f} / %{customdata[6]:.3f}"
                        "<br>Volume / OI <b>%{customdata[9]} / %{customdata[10]}</b>"
                        " · OI %{customdata[11]} (%{customdata[12]})"
                        "<br>Quote skew %{customdata[7]:.0f} ms<extra></extra>"
                    ),
                ),
                secondary_y=False,
            )
        if trade_tape is not None:
            payloads = trade_trace_payloads(trade_tape, expiry, x_axis)
            side_colors = {"C": "#2563EB", "P": "#0F766E"}
            for put_call, side_label in (("C", "Calls"), ("P", "Puts")):
                payload = payloads[put_call]
                figure.add_trace(
                    go.Scatter(
                        x=payload["x"],
                        y=payload["y"],
                        mode="markers",
                        name=f"Trade-time IV · {side_label}",
                        legendrank=20 if put_call == "C" else 21,
                        legendgroup=f"trade-tape-{put_call}",
                        meta={
                            "role": "trade-tape",
                            "put_call": put_call,
                            "legend_layer": "trades",
                        },
                        marker={
                            "color": side_colors[put_call],
                            "size": payload["size"],
                            "symbol": payload["symbol"],
                            "line": {
                                "color": payload["line_color"],
                                "width": payload["line_width"],
                            },
                        },
                        customdata=payload["customdata"],
                        hovertemplate=(
                            f"<b>Trade-time IV · {side_label[:-1]}</b>"
                            "<br>Strike %{customdata[0]:.2f}"
                            + axis_hover
                            + " · IV <b>%{y:.2f}%</b>"
                            "<br>Trade <b>%{customdata[1]:.4f} × %{customdata[2]:,.0f}</b>"
                            " · %{customdata[3]}"
                            f"<br>{underlying_hover_label} %{{customdata[4]:.3f}}"
                            " · %{customdata[5]} · %{customdata[6]:.0f} ms"
                            "<br>Quote ages %{customdata[7]}"
                            "<br>Condition %{customdata[8]}<extra></extra>"
                        ),
                    ),
                    secondary_y=False,
                )
            matched_trades = trade_tape.loc[
                _expiry_mask(
                    trade_tape, expiry, "underlying_contract_month"
                )
                & trade_tape["trade_iv_status"].astype(str).eq("resolved")
            ].copy()
        else:
            matched_trades = raw.loc[
                raw.get("last_trade_iv_status", pd.Series(index=raw.index, dtype=object))
                .astype(str)
                .eq("resolved")
                & pd.to_numeric(
                    raw.get("last_trade_iv", pd.Series(np.nan, index=raw.index)),
                    errors="coerce",
                ).notna()
            ].copy()
            if not matched_trades.empty:
                matched_trades["_axis_x"] = (
                    matched_trades.apply(
                        _row_delta_x,
                        axis=1,
                        volatility_column="last_trade_iv",
                        forward_column="last_trade_underlying_price",
                    )
                    if x_axis == X_AXIS_DELTA
                    else pd.to_numeric(matched_trades["strike"], errors="coerce")
                )
                matched_trades = matched_trades.loc[
                    matched_trades["_axis_x"].notna()
                ].copy()
                matched_trades["trade_time_gst"] = pd.to_datetime(
                    matched_trades["last_trade_at"], errors="coerce", utc=True
                ).dt.tz_convert("Asia/Dubai").dt.strftime("%H:%M:%S GST")
                figure.add_trace(
                    go.Scatter(
                        x=matched_trades["_axis_x"],
                        y=100.0 * matched_trades["last_trade_iv"],
                        mode="markers",
                        name="New matched trade IV",
                        legendrank=20,
                        meta={"legend_layer": "trades"},
                        marker={"color": "#F97316", "size": 9, "symbol": "diamond"},
                        customdata=np.column_stack(
                            [
                                matched_trades["strike"],
                                matched_trades["last_trade_price"],
                                matched_trades["trade_time_gst"],
                                matched_trades["last_trade_underlying_price"],
                                matched_trades["last_trade_underlying_source"].map(
                                    _trade_match_source_label
                                ),
                                matched_trades["last_trade_match_lag_ms"],
                                matched_trades["last_trade_condition_codes"].fillna("regular"),
                                matched_trades.get(
                                    "option_security",
                                    pd.Series("Option", index=matched_trades.index),
                                ),
                                _hover_count_strings(matched_trades["volume"]),
                                _hover_count_strings(matched_trades["open_interest"]),
                                _hover_date_strings(
                                    matched_trades["open_interest_date"]
                                ),
                                _hover_oi_status_strings(
                                    matched_trades["open_interest_scope_status"]
                                ),
                            ]
                        ),
                        hovertemplate=(
                            "<b>New matched trade</b>"
                            "<br>Strike %{customdata[0]:.2f}" + axis_hover
                            + " · IV <b>%{y:.2f}%</b>"
                            "<br>%{customdata[7]} · Trade %{customdata[1]:.4f}"
                            " · %{customdata[2]}"
                            f"<br>{underlying_hover_label} %{{customdata[3]:.3f}}"
                            " · %{customdata[4]} · %{customdata[5]:.0f} ms"
                            "<br>Volume / OI <b>%{customdata[8]} / %{customdata[9]}</b>"
                            " · OI %{customdata[10]} (%{customdata[11]})"
                            "<br>Condition %{customdata[6]}<extra></extra>"
                        ),
                    ),
                    secondary_y=False,
                )
    else:
        settlement_reference = _settlement_reference_smile(raw, x_axis)
        if not settlement_reference.empty:
            calibration_hover = (
                "<br>Calibration: <b>%{customdata[6]}</b>"
                if resolved_product == "BRENT"
                else ""
            )
            figure.add_trace(
                go.Scatter(
                    x=settlement_reference["display_x"],
                    y=100.0 * settlement_reference["reference_iv"],
                    mode="markers+lines",
                    name="Bloomberg settlement IV",
                    legendrank=5,
                    meta={"legend_layer": "bloomberg-settlement"},
                    line={"color": "#475569", "width": 1.4},
                    marker={
                        "color": "#FFFFFF",
                        "line": {"color": "#475569", "width": 1.2},
                        "size": 6,
                        "symbol": "circle",
                    },
                    customdata=np.column_stack(
                        [
                            settlement_reference["strike"],
                            settlement_reference["option_security"],
                            settlement_reference["settlement_price"],
                            settlement_reference["volume"].map(
                                lambda value: (
                                    f"{value:,.0f}" if pd.notna(value) else "—"
                                )
                            ),
                            settlement_reference["open_interest"].map(
                                lambda value: (
                                    f"{value:,.0f}" if pd.notna(value) else "—"
                                )
                            ),
                            settlement_reference["forward"],
                            settlement_reference["calibration_status"],
                        ]
                    ),
                    hovertemplate=(
                        "<b>Settlement from Bloomberg</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[1]} · Premium <b>%{customdata[2]:.4f} "
                        + spec["price_unit"]
                        + "</b>"
                        f" · {underlying_hover_label} %{{customdata[5]:.3f}}"
                        "<br>Volume / OI <b>%{customdata[3]} / %{customdata[4]}</b>"
                        + calibration_hover
                        + "<extra></extra>"
                    ),
                ),
                secondary_y=False,
            )
    if not published.empty:
        if x_axis == X_AXIS_DELTA:
            published["_axis_x"] = published.apply(
                lambda row: _display_delta(row.get("delta"), row.get("put_call")),
                axis=1,
            )
        else:
            published["_axis_x"] = pd.to_numeric(
                published["strike"], errors="coerce"
            )
        published = published.loc[published["_axis_x"].notna()].sort_values("_axis_x")
    if not published.empty:
        figure.add_trace(
            go.Scatter(
                x=published["_axis_x"],
                y=100.0 * published["volatility"],
                mode="lines",
                name=f"Published {spec['published_label']} exact COB",
                legendrank=30,
                meta={"legend_layer": "published"},
                line={"color": "#EA580C", "width": 2.2},
                customdata=np.column_stack(
                    [
                        published["strike"],
                        published["put_call"],
                        published["delta"],
                        published["source_name"],
                        published["forward"],
                    ]
                ),
                hovertemplate=(
                    f"<b>Published {spec['published_label']} exact COB</b>"
                    + "<br>Strike %{customdata[0]:.2f}"
                    + axis_hover
                    + " · IV <b>%{y:.2f}%</b>"
                    f"<br>{underlying_hover_label} %{{customdata[4]:.3f}}"
                    " · %{customdata[1]} Δ %{customdata[2]:.3f}"
                    "<br>%{customdata[3]}<extra></extra>"
                ),
            ),
            secondary_y=False,
        )
    if not calibrated.empty:
        if x_axis == X_AXIS_DELTA:
            calibrated["_axis_x"] = calibrated.apply(
                lambda row: _display_delta(row.get("delta"), row.get("put_call")),
                axis=1,
            )
        else:
            calibrated["_axis_x"] = pd.to_numeric(
                calibrated["strike"], errors="coerce"
            )
        calibrated = calibrated.loc[calibrated["_axis_x"].notna()].sort_values(
            "_axis_x"
        )
    if not calibrated.empty:
        metadata = calibrated_publication_metadata(calibrated)
        publication_cob = pd.to_datetime(metadata.get("cob_date"), errors="coerce")
        published_at = pd.to_datetime(
            metadata.get("published_at"), errors="coerce", utc=True
        )
        cob_label = (
            publication_cob.strftime("%d %b %Y")
            if not pd.isna(publication_cob)
            else "unknown"
        )
        published_label = (
            published_at.tz_convert("Asia/Dubai").strftime("%d %b %Y %H:%M GST")
            if not pd.isna(published_at)
            else "unavailable"
        )
        commodity = str(metadata.get("commodity") or spec["published_product"])
        publication_id = str(metadata.get("publication_id") or "unavailable")
        figure.add_trace(
            go.Scatter(
                x=calibrated["_axis_x"],
                y=100.0 * calibrated["volatility"],
                mode="lines",
                name=f"Calibrated {commodity} · COB {cob_label}",
                legendrank=35,
                meta={"legend_layer": "calibrated"},
                line={"color": "#7C3AED", "width": 2.4, "dash": "dash"},
                customdata=np.column_stack(
                    [
                        calibrated["strike"],
                        calibrated["delta"],
                        calibrated["forward_value"],
                        calibrated["source_name"],
                        calibrated["calibration_basis"],
                    ]
                ),
                hovertemplate=(
                    f"<b>Calibrated {commodity} publication</b>"
                    "<br>Strike %{customdata[0]:.3f}"
                    + axis_hover
                    + " · IV <b>%{y:.2f}%</b>"
                    f"<br>{underlying_hover_label} %{{customdata[2]:.3f}}"
                    " · call Δ %{customdata[1]:.3f}"
                    "<br>%{customdata[4]} · %{customdata[3]}"
                    f"<br>COB {cob_label} · Published {published_label}"
                    f"<br>Revision {publication_id}<extra></extra>"
                ),
            ),
            secondary_y=False,
        )

    forward_values = pd.to_numeric(raw["underlying_price"], errors="coerce").dropna()
    if not forward_values.empty:
        figure.add_vline(
            x=(
                0.5
                if x_axis == X_AXIS_DELTA
                else float(forward_values.iloc[0])
            ),
            line_width=1,
            line_dash="dot",
            line_color="#111827",
            name="pricing-reference",
        )

    focus_strikes = []
    if not executable.empty:
        focus_strikes.extend(
            pd.to_numeric(executable["strike"], errors="coerce").dropna()
        )
    if not prior_reference.empty:
        focus_strikes.extend(
            pd.to_numeric(prior_reference["strike"], errors="coerce").dropna()
        )
    if not matched_trades.empty:
        focus_strikes.extend(
            pd.to_numeric(matched_trades["strike"], errors="coerce").dropna()
        )
    if not published.empty:
        focus_strikes.extend(
            pd.to_numeric(published["strike"], errors="coerce").dropna()
        )
    if not settlement_reference.empty:
        # Settlement panels are an exchange-record view, so their initial range
        # includes every price-valid Bloomberg strike.
        focus_strikes.extend(
            pd.to_numeric(settlement_reference["strike"], errors="coerce").dropna()
        )
    if not forward_values.empty:
        focus_strikes.append(float(forward_values.iloc[0]))
    if focus_strikes and x_axis == X_AXIS_STRIKE:
        focus_low = float(min(focus_strikes))
        focus_high = float(max(focus_strikes))
        focus_padding = max(0.04 * (focus_high - focus_low), axis_spacing)
        figure.update_xaxes(
            range=[max(0.0, focus_low - focus_padding), focus_high + focus_padding]
        )

    figure.update_yaxes(title_text="IV (%)", secondary_y=False)
    if is_intraday and trade_tape is not None:
        # Keep the executable smile visually fixed while the trade window moves.
        # Plotly otherwise autoranges the primary axis after every trade-trace
        # patch, which makes unchanged executable IVs appear to move.
        iv_values = []
        for trace in figure.data:
            if trace.type == "bar" or getattr(trace, "yaxis", "y") == "y2":
                continue
            iv_values.extend(
                value
                for value in pd.to_numeric(
                    pd.Series(trace.y, dtype=object), errors="coerce"
                ).dropna()
                if np.isfinite(value)
            )
        if iv_values:
            iv_low = float(min(iv_values))
            iv_high = float(max(iv_values))
            iv_padding = max(0.5, 0.05 * max(iv_high - iv_low, 1.0))
            figure.update_yaxes(
                range=[max(0.0, iv_low - iv_padding), iv_high + iv_padding],
                secondary_y=False,
            )
    figure.update_yaxes(
        title_text="Activity (contracts)",
        rangemode="tozero",
        showgrid=False,
        secondary_y=True,
    )
    if x_axis == X_AXIS_DELTA:
        figure.update_xaxes(
            title_text="Delta (put wing → call wing)",
            range=[0.0, 1.0],
            tickmode="array",
            tickvals=[0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0],
            ticktext=[
                "0Δ put",
                "10Δ put",
                "25Δ put",
                "ATM",
                "25Δ call",
                "10Δ call",
                "0Δ call",
            ],
        )
    else:
        figure.update_xaxes(title_text=f"Strike ({spec['price_unit']})")
    expiry_trade_tape = (
        trade_tape.loc[
            _expiry_mask(trade_tape, expiry, "underlying_contract_month")
        ].copy()
        if trade_tape is not None and not trade_tape.empty
        else pd.DataFrame()
    )
    quality = _intraday_expiry_quality(raw, expiry_trade_tape, prior_reference)
    figure.update_layout(
        template="plotly_white",
        height=440,
        margin={"l": 58, "r": 44, "t": 18, "b": 48},
        barmode="overlay",
        hovermode="closest",
        hoverdistance=36,
        hoverlabel={
            "bgcolor": "#0F172A",
            "bordercolor": "#334155",
            "font": {
                "family": "Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif",
                "size": 11,
                "color": "#F8FAFC",
            },
            "align": "left",
            "namelength": 0,
        },
        showlegend=False,
        font={"family": "Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif", "size": 11},
        uirevision=(
            f"vol-trades-{resolved_product.lower()}-"
            f"{expiry.date().isoformat()}-{x_axis}"
        ),
        meta={"expiry": expiry.date().isoformat(), "quality": quality},
    )
    return figure


def _settlement_chart_exclusions(
    chain: pd.DataFrame,
    x_axis: str,
) -> pd.DataFrame:
    columns = [
        "underlying_contract_month",
        "strike",
        "put_call",
        "option_security",
        "settlement_price",
        "reason_code",
        "reason",
    ]
    if chain is None or chain.empty:
        return pd.DataFrame(columns=columns)
    exclusions = []
    contract_months = pd.to_datetime(
        chain.get("underlying_contract_month"), errors="coerce"
    ).dt.normalize()
    for contract_month in sorted(contract_months.dropna().unique()):
        expiry_rows = chain.loc[contract_months.eq(contract_month)].copy()
        _, expiry_exclusions = _settlement_reference_selection(expiry_rows, x_axis)
        if not expiry_exclusions.empty:
            expiry_exclusions["underlying_contract_month"] = pd.Timestamp(
                contract_month
            )
            exclusions.append(expiry_exclusions)
    if not exclusions:
        return pd.DataFrame(columns=columns)
    return pd.concat(exclusions, ignore_index=True)[columns]


def _settlement_exclusion_note(
    exclusions: pd.DataFrame,
    *,
    product: str,
):
    total = len(exclusions)
    expiry_values = pd.to_datetime(
        exclusions["underlying_contract_month"], errors="coerce"
    ).dt.normalize()
    expiry_count = int(expiry_values.nunique())
    reason_items = []
    for (_, reason), rows in exclusions.groupby(
        ["reason_code", "reason"], sort=False, dropna=False
    ):
        count = len(rows)
        reason_items.append(
            html.Div(
                [
                    html.Span(
                        className="brent-vol-history-exclusion-reason-marker",
                        **{"aria-hidden": "true"},
                    ),
                    html.Span(
                        str(reason),
                        className="brent-vol-history-exclusion-reason-label",
                    ),
                    html.Span(
                        f"{count:,}",
                        className="brent-vol-history-exclusion-reason-count",
                        title=(
                            f"{count:,} excluded "
                            f"observation{'s' if count != 1 else ''}"
                        ),
                    ),
                ],
                className="brent-vol-history-exclusion-reason-row",
            )
        )
    expiry_groups = []
    working = exclusions.assign(_contract_month=expiry_values)
    for contract_month, rows in working.groupby("_contract_month", sort=True):
        side_groups = []
        for side, side_name in (("P", "Puts"), ("C", "Calls")):
            side_rows = rows.loc[
                rows["put_call"].astype(str).str.strip().str.upper().eq(side)
            ].sort_values("strike")
            if side_rows.empty:
                continue
            strike_chips = []
            for row in side_rows.itertuples():
                strike_value = _numeric_or_none(row.strike)
                strike_label = f"{strike_value:g}" if strike_value is not None else "—"
                strike_chips.append(
                    html.Span(
                        strike_label,
                        className="brent-vol-history-exclusion-strike-chip",
                        title=f"{strike_label}{side}",
                        **{"aria-label": f"{strike_label} {side_name.lower()}"},
                    )
                )
            side_groups.append(
                html.Div(
                    [
                        html.Div(
                            [
                                html.Span(
                                    side,
                                    className=(
                                        "brent-vol-history-exclusion-side-code "
                                        f"brent-vol-history-exclusion-side-{side.lower()}"
                                    ),
                                    **{"aria-hidden": "true"},
                                ),
                                html.Span(side_name),
                            ],
                            className="brent-vol-history-exclusion-side-label",
                        ),
                        html.Div(
                            strike_chips,
                            className="brent-vol-history-exclusion-strike-list",
                        ),
                    ],
                    className="brent-vol-history-exclusion-side-row",
                )
            )
        row_count = len(rows)
        expiry_groups.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Strong(
                                pd.Timestamp(contract_month).strftime("%b-%y")
                            ),
                            html.Span(
                                f"{row_count:,} strike{'s' if row_count != 1 else ''}"
                            ),
                        ],
                        className="brent-vol-history-exclusion-expiry-heading",
                    ),
                    html.Div(
                        side_groups,
                        className="brent-vol-history-exclusion-expiry-sides",
                    ),
                ],
                className="brent-vol-history-exclusion-expiry-group",
            )
        )
    pricing_future_label = _product_spec(product)["underlying_label"]
    observation_word = "observation" if total == 1 else "observations"
    expiry_word = "expiry" if expiry_count == 1 else "expiries"
    return html.Aside(
        [
            html.Div(
                [
                    html.Span(
                        "IV",
                        className="brent-vol-history-exclusion-badge",
                        **{"aria-hidden": "true"},
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H3(
                                        "Excluded from settlement IV charts",
                                        id=(
                                            "brent-vol-history-settlement-"
                                            "exclusion-title"
                                        ),
                                    ),
                                    html.Div(
                                        [
                                            html.Span(
                                                f"{total:,} excluded",
                                                className=(
                                                    "brent-vol-history-exclusion-"
                                                    "metric brent-vol-history-"
                                                    "exclusion-metric-primary"
                                                ),
                                            ),
                                            html.Span(
                                                f"{expiry_count:,} {expiry_word}",
                                                className=(
                                                    "brent-vol-history-exclusion-"
                                                    "metric"
                                                ),
                                            ),
                                        ],
                                        className=(
                                            "brent-vol-history-exclusion-metrics"
                                        ),
                                        **{"aria-label": (
                                            f"{total:,} excluded OTM strike "
                                            f"{observation_word} across "
                                            f"{expiry_count:,} {expiry_word}"
                                        )},
                                    ),
                                ],
                                className="brent-vol-history-exclusion-title-row",
                            ),
                            html.P(
                                "A strict OTM quality gate keeps non-resolvable "
                                "observations out of the smile. Official premiums remain "
                                "available in Option-chain detail.",
                                className="brent-vol-history-exclusion-summary",
                            ),
                        ],
                        className="brent-vol-history-exclusion-intro",
                    ),
                ],
                className="brent-vol-history-exclusion-header",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H4("Quality gate"),
                            html.Div(
                                reason_items,
                                className="brent-vol-history-exclusion-reasons",
                            ),
                        ],
                        className=(
                            "brent-vol-history-exclusion-panel "
                            "brent-vol-history-exclusion-quality"
                        ),
                    ),
                    html.Div(
                        [
                            html.H4("Affected strikes"),
                            html.Div(
                                expiry_groups,
                                className="brent-vol-history-exclusion-expiries",
                            ),
                        ],
                        className=(
                            "brent-vol-history-exclusion-panel "
                            "brent-vol-history-exclusion-strikes"
                        ),
                    ),
                ],
                className="brent-vol-history-exclusion-body",
            ),
            html.P(
                [
                    html.Span(
                        className="brent-vol-history-exclusion-footnote-marker",
                        **{"aria-hidden": "true"},
                    ),
                    f"OTM convention: puts below {pricing_future_label}; calls at or "
                    "above it. An unresolved OTM option is never replaced by an "
                    "intrinsic-value ITM option.",
                ],
                className="brent-vol-history-exclusion-footnote",
            ),
        ],
        className="brent-vol-history-settlement-exclusion-note",
        **{
            "aria-labelledby": "brent-vol-history-settlement-exclusion-title",
            "role": "note",
        },
    )


def build_plot_cards(
    chain: pd.DataFrame,
    published: pd.DataFrame,
    x_axis: str = X_AXIS_STRIKE,
    trade_tape: pd.DataFrame | None = None,
    prior_settlement_chain: pd.DataFrame | None = None,
    product: str | None = None,
    calibrated: pd.DataFrame | None = None,
):
    x_axis = _normalize_x_axis(x_axis)
    resolved_product = _normalize_product(
        product
        or (
            chain["product"].iloc[0]
            if chain is not None and not chain.empty and "product" in chain.columns
            else PRODUCT
        )
    )
    product_label = _product_spec(resolved_product)["label"]
    if chain is None or chain.empty:
        return [
            dbc.Alert(
                f"No complete Bloomberg {product_label} option-chain snapshot is available.",
                color="secondary",
            )
        ]
    # Settlement calibration suitability is carried in the authoritative
    # Bloomberg-point hover; it is not rendered as a second smile.
    prepared = pd.DataFrame()
    expiries = sorted(
        pd.to_datetime(chain["underlying_contract_month"], errors="coerce")
        .dropna()
        .dt.normalize()
        .unique()
    )
    cob_date = pd.to_datetime(chain["business_date"], errors="coerce").dropna().iloc[0]
    forward_by_contract = {
        pd.Timestamp(row.underlying_contract_month).normalize(): float(row.underlying_price)
        for row in chain[["underlying_contract_month", "underlying_price"]]
        .dropna()
        .drop_duplicates()
        .itertuples(index=False)
    }
    published_nodes = published_strike_nodes(published, forward_by_contract, cob_date)
    calibrated_nodes = calibrated if calibrated is not None else pd.DataFrame()
    cards = []
    for expiry_value in expiries:
        expiry = pd.Timestamp(expiry_value).normalize()
        figure = build_expiry_figure(
            chain,
            prepared,
            published_nodes,
            expiry,
            x_axis=x_axis,
            trade_tape=trade_tape,
            prior_settlement_chain=prior_settlement_chain,
            product=resolved_product,
            calibrated_nodes=calibrated_nodes,
        )
        label = expiry.strftime("%b-%y")
        quality = dict(figure.layout.meta or {}).get("quality") or {}
        quality_summary = (
            html.Div(
                [
                    dbc.Badge(
                        quality["status"],
                        color=quality["color"],
                        pill=True,
                        className="brent-vol-history-quality-badge",
                    ),
                    html.Span(
                        quality["detail"],
                        className="brent-vol-history-quality-detail",
                    ),
                ],
                className="brent-vol-history-card-quality",
                role="status",
                title=quality["detail"],
            )
            if quality
            else None
        )
        cards.append(
            html.Section(
                [
                    html.Header(
                        [
                            html.H3(
                                label,
                                className="brent-vol-history-card-title",
                            ),
                            quality_summary,
                        ],
                        className="brent-vol-history-card-header",
                    ),
                    dcc.Graph(
                        id={
                            "type": "brent-vol-history-expiry-graph",
                            "expiry": expiry.date().isoformat(),
                        },
                        figure=figure,
                        config={
                            "displaylogo": False,
                            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                            "responsive": True,
                        },
                        className="brent-vol-history-graph",
                    ),
                ],
                className="brent-vol-history-expiry-card",
                role="region",
                **{
                    "aria-label": (
                        f"{label} {product_label} smile, volume, and open interest"
                    )
                },
            )
        )
    exclusions = _settlement_chart_exclusions(chain, x_axis)
    if not exclusions.empty:
        cards.append(
            _settlement_exclusion_note(
                exclusions,
                product=resolved_product,
            )
        )
    return cards


def _plot_card_graphs(cards) -> list[Any]:
    graphs = []
    for card in cards or []:
        children = getattr(card, "children", None)
        if children is None:
            continue
        if not isinstance(children, (list, tuple)):
            children = [children]
        for child in children:
            component_id = getattr(child, "id", None)
            if (
                isinstance(component_id, dict)
                and component_id.get("type")
                == "brent-vol-history-expiry-graph"
            ):
                graphs.append(child)
                break
    return graphs


def _trace_has_points(trace: Any) -> bool:
    values = getattr(trace, "x", None)
    if values is None:
        return False
    try:
        return len(values) > 0
    except TypeError:
        return True


def _trace_has_new_volume_edge(trace: Any) -> bool:
    meta = getattr(trace, "meta", None)
    if not isinstance(meta, dict) or meta.get("legend_layer") not in {
        "volume-calls",
        "volume-puts",
    }:
        return False
    marker = getattr(trace, "marker", None)
    marker_line = getattr(marker, "line", None) if marker is not None else None
    colors = getattr(marker_line, "color", None) if marker_line is not None else None
    if colors is None:
        return False
    if isinstance(colors, str):
        return colors.upper() == "#F97316"
    try:
        return any(str(color).upper() == "#F97316" for color in colors)
    except TypeError:
        return False


def _expiry_legend_contract(cards) -> dict[str, Any]:
    available = set()
    graphs = {}
    new_volume_layers = set()
    for graph in _plot_card_graphs(cards):
        figure = graph.figure
        graph_id = dict(graph.id)
        expiry = str(graph_id.get("expiry") or "")
        trace_entries = []
        for index, trace in enumerate(figure.data):
            meta = getattr(trace, "meta", None)
            layer = meta.get("legend_layer") if isinstance(meta, dict) else None
            if not layer:
                continue
            trace_entries.append({"index": index, "layer": layer})
            if _trace_has_points(trace):
                available.add(layer)
            if _trace_has_new_volume_edge(trace):
                new_volume_layers.add(layer)
        shape_entries = []
        for index, shape in enumerate(figure.layout.shapes or ()):
            layer = getattr(shape, "name", None)
            if layer not in EXPIRY_LEGEND_LAYER_SPECS:
                continue
            shape_entries.append({"index": index, "layer": layer})
            available.add(layer)
        graphs[expiry] = {"traces": trace_entries, "shapes": shape_entries}
    available_layers = [
        layer for layer in EXPIRY_LEGEND_LAYER_ORDER if layer in available
    ]
    return {
        "available_layers": available_layers,
        "new_volume_layers": [
            layer for layer in EXPIRY_LEGEND_LAYER_ORDER if layer in new_volume_layers
        ],
        "graphs": graphs,
    }


DEFAULT_HIDDEN_EXPIRY_LAYERS = frozenset({"call-mid", "put-mid"})


def _default_expiry_layers(available_layers: list[str]) -> list[str]:
    return [
        layer
        for layer in available_layers
        if layer not in DEFAULT_HIDDEN_EXPIRY_LAYERS
    ]


def _selected_expiry_layers(
    available_layers: list[str],
    current_options: list[dict[str, Any]] | None,
    current_value: list[str] | None,
) -> list[str]:
    previous_available = {
        str(option.get("value"))
        for option in (current_options or [])
        if option.get("value")
    }
    if not previous_available or current_value is None:
        return _default_expiry_layers(available_layers)
    selected = {str(value) for value in current_value}
    selected.update(
        (set(available_layers) - previous_available)
        - DEFAULT_HIDDEN_EXPIRY_LAYERS
    )
    return [layer for layer in available_layers if layer in selected]


def _apply_expiry_layer_selection(
    cards,
    contract: dict[str, Any],
    selected_layers: list[str] | None,
) -> None:
    selected = set(selected_layers or [])
    graph_contracts = contract.get("graphs") or {}
    for graph in _plot_card_graphs(cards):
        expiry = str(dict(graph.id).get("expiry") or "")
        graph_contract = graph_contracts.get(expiry) or {}
        for entry in graph_contract.get("traces") or []:
            graph.figure.data[int(entry["index"])].visible = (
                entry["layer"] in selected
            )
        for entry in graph_contract.get("shapes") or []:
            graph.figure.layout.shapes[int(entry["index"])].visible = (
                entry["layer"] in selected
            )


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


def _detail_rows(chain: pd.DataFrame, expiry_value: str | None) -> list[dict[str, Any]]:
    if chain is None or chain.empty or not expiry_value:
        return []
    expiry = pd.Timestamp(expiry_value).normalize()
    selected = chain.loc[_expiry_mask(chain, expiry, "underlying_contract_month")].copy()
    if selected.empty:
        return []
    prepared = prepare_market_observations(selected)
    eligibility: dict[float, tuple[bool, str]] = {}
    if not prepared.empty:
        for strike, group in prepared.groupby("strike"):
            is_eligible = bool(group["calibration_eligible"].fillna(False).any())
            reasons = ";".join(
                dict.fromkeys(
                    reason
                    for reason in group["exclusion_reason"].fillna("").astype(str)
                    if reason
                )
            )
            eligibility[float(strike)] = (is_eligible, reasons)
    rows = []
    for row in selected.itertuples(index=False):
        eligible, smile_reason = eligibility.get(float(row.strike), (False, ""))
        rows.append(
            {
                "option_security": row.option_security,
                "option_global_id": row.option_global_id,
                "native_option_underlier": getattr(
                    row, "underlying_security", None
                ),
                "pricing_future": getattr(
                    row, "pricing_underlying_security", None
                ),
                "finance_rate_pct": (
                    None
                    if _numeric_or_none(
                        getattr(row, "pricing_discount_rate", None)
                    ) is None
                    else 100.0 * float(row.pricing_discount_rate)
                ),
                "finance_rate_observed_at": (
                    None
                    if pd.isna(
                        getattr(row, "pricing_discount_rate_observed_at", None)
                    )
                    else pd.Timestamp(
                        getattr(row, "pricing_discount_rate_observed_at")
                    ).isoformat()
                ),
                "put_call": row.put_call,
                "strike": _numeric_or_none(row.strike),
                "settlement_price": _numeric_or_none(row.settlement_price),
                "last_price": _numeric_or_none(getattr(row, "last_price", None)),
                "option_bid": _numeric_or_none(getattr(row, "option_bid", None)),
                "option_mid": _numeric_or_none(getattr(row, "option_mid", None)),
                "option_ask": _numeric_or_none(getattr(row, "option_ask", None)),
                "option_spread": _numeric_or_none(getattr(row, "option_spread", None)),
                "option_spread_pct": _numeric_or_none(getattr(row, "option_spread_pct", None)),
                "underlying_bid": _numeric_or_none(getattr(row, "underlying_bid", None)),
                "underlying_mid": _numeric_or_none(getattr(row, "underlying_mid", None)),
                "underlying_ask": _numeric_or_none(getattr(row, "underlying_ask", None)),
                "underlying_spread": _numeric_or_none(getattr(row, "underlying_spread", None)),
                "quote_batch_id": _numeric_or_none(getattr(row, "quote_batch_id", None)),
                "quote_capture_skew_ms": _numeric_or_none(getattr(row, "quote_capture_skew_ms", None)),
                "quote_request_started_at": (
                    None if pd.isna(getattr(row, "quote_request_started_at", None))
                    else pd.Timestamp(getattr(row, "quote_request_started_at")).isoformat()
                ),
                "quote_response_at": (
                    None if pd.isna(getattr(row, "quote_response_at", None))
                    else pd.Timestamp(getattr(row, "quote_response_at")).isoformat()
                ),
                "executable_iv_bid_pct": (
                    None if _numeric_or_none(getattr(row, "executable_iv_bid", None)) is None
                    else 100.0 * float(row.executable_iv_bid)
                ),
                "executable_iv_mid_pct": (
                    None if _numeric_or_none(getattr(row, "executable_iv_mid", None)) is None
                    else 100.0 * float(row.executable_iv_mid)
                ),
                "executable_iv_ask_pct": (
                    None if _numeric_or_none(getattr(row, "executable_iv_ask", None)) is None
                    else 100.0 * float(row.executable_iv_ask)
                ),
                "executable_iv_status": getattr(row, "executable_iv_status", None),
                "executable_iv_exclusion_reason": getattr(
                    row, "executable_iv_exclusion_reason", None
                ),
                "last_trade_date": (
                    None
                    if pd.isna(getattr(row, "last_trade_date", None))
                    else pd.Timestamp(getattr(row, "last_trade_date")).date().isoformat()
                ),
                "open_interest_date": (
                    None
                    if pd.isna(getattr(row, "open_interest_date", None))
                    else pd.Timestamp(getattr(row, "open_interest_date")).date().isoformat()
                ),
                "settlement_open_interest": _numeric_or_none(
                    getattr(row, "settlement_open_interest", None)
                ),
                "settlement_open_interest_date": (
                    None
                    if pd.isna(
                        getattr(row, "settlement_open_interest_date", None)
                    )
                    else pd.Timestamp(
                        getattr(row, "settlement_open_interest_date")
                    ).date().isoformat()
                ),
                "intraday_open_interest": _numeric_or_none(
                    getattr(row, "intraday_open_interest", None)
                ),
                "intraday_open_interest_date": (
                    None
                    if pd.isna(
                        getattr(row, "intraday_open_interest_date", None)
                    )
                    else pd.Timestamp(
                        getattr(row, "intraday_open_interest_date")
                    ).date().isoformat()
                ),
                "implied_volatility_pct": (
                    None
                    if _numeric_or_none(row.implied_volatility) is None
                    else 100.0 * float(row.implied_volatility)
                ),
                "volume": _numeric_or_none(row.volume),
                "volume_scope_status": getattr(
                    row, "volume_scope_status", "settlement"
                ),
                "volume_delta": _numeric_or_none(getattr(row, "volume_delta", None)),
                "volume_delta_status": getattr(
                    row, "volume_delta_status", "not_applicable"
                ),
                "open_interest": _numeric_or_none(row.open_interest),
                "open_interest_scope_status": getattr(
                    row, "open_interest_scope_status", "settlement"
                ),
                "open_interest_source": getattr(
                    row, "open_interest_source", None
                ),
                "underlying_price": _numeric_or_none(row.underlying_price),
                "last_trade_price_exact": _numeric_or_none(getattr(row, "last_trade_price", None)),
                "last_trade_at_gst": (
                    None if pd.isna(getattr(row, "last_trade_at", None))
                    else pd.Timestamp(getattr(row, "last_trade_at")).tz_convert("Asia/Dubai").isoformat()
                ),
                "last_trade_underlying_price": _numeric_or_none(
                    getattr(row, "last_trade_underlying_price", None)
                ),
                "last_trade_underlying_at_gst": (
                    None if pd.isna(getattr(row, "last_trade_underlying_at", None))
                    else pd.Timestamp(getattr(row, "last_trade_underlying_at")).tz_convert("Asia/Dubai").isoformat()
                ),
                "last_trade_underlying_source": _trade_match_source_label(
                    getattr(row, "last_trade_underlying_source", None)
                ),
                "last_trade_match_lag_ms": _numeric_or_none(
                    getattr(row, "last_trade_match_lag_ms", None)
                ),
                "last_trade_condition_codes": getattr(
                    row, "last_trade_condition_codes", None
                ),
                "last_trade_iv_pct": (
                    None if _numeric_or_none(getattr(row, "last_trade_iv", None)) is None
                    else 100.0 * float(row.last_trade_iv)
                ),
                "last_trade_iv_status": getattr(row, "last_trade_iv_status", None),
                "last_trade_iv_exclusion_reason": getattr(
                    row, "last_trade_iv_exclusion_reason", None
                ),
                "last_trade_reused": (
                    "Yes"
                    if getattr(row, "last_trade_match_source_snapshot_id", None)
                    else "No"
                ),
                "option_expiration_date": (
                    None
                    if pd.isna(row.option_expiration_date)
                    else pd.Timestamp(row.option_expiration_date).date().isoformat()
                ),
                "discovery_method": row.discovery_method,
                "iv_status": row.iv_status,
                "smile_eligible": "Yes" if eligible else "No",
                "exclusion_reason": row.iv_exclusion_reason or smile_reason or None,
            }
        )
    return rows


_GRID_PRICE_4DP = {
    "function": "params.value == null ? '—' : Number(params.value).toFixed(4)"
}
_GRID_PRICE_3DP = {
    "function": "params.value == null ? '—' : Number(params.value).toFixed(3)"
}
_GRID_IV_2DP = {
    "function": "params.value == null ? '—' : Number(params.value).toFixed(2)"
}
_GRID_PERCENT_2DP = {
    "function": (
        "params.value == null ? '—' : "
        "(100 * Number(params.value)).toFixed(2)"
    )
}
_GRID_INTEGER = {
    "function": (
        "params.value == null ? '—' : "
        "Number(params.value).toLocaleString('en-GB', {maximumFractionDigits: 0})"
    )
}
_GRID_DATE = {
    "function": (
        "params.value == null ? '—' : "
        "new Intl.DateTimeFormat('en-GB', {day: '2-digit', month: 'short', "
        "year: '2-digit', timeZone: 'UTC'}).format("
        "new Date(String(params.value).slice(0, 10) + 'T00:00:00Z'))"
    )
}
_GRID_DATETIME_GST = {
    "function": (
        "params.value == null ? '—' : "
        "new Intl.DateTimeFormat('en-GB', {day: '2-digit', month: 'short', "
        "hour: '2-digit', minute: '2-digit', second: '2-digit', "
        "hour12: false, timeZone: 'Asia/Dubai'}).format(new Date(params.value))"
    )
}
_GRID_SIDE_RULES = {
    "vol-trades-call-cell": "params.value === 'C'",
    "vol-trades-put-cell": "params.value === 'P'",
}
_GRID_STATUS_RULES = {
    "vol-trades-status-ok": (
        "['resolved', 'eligible', 'same_day', 'settlement'].includes("
        "String(params.value || '').toLowerCase())"
    ),
    "vol-trades-status-warning": (
        "!['', 'resolved', 'eligible', 'same_day', 'settlement'].includes("
        "String(params.value || '').toLowerCase())"
    ),
}


def _grid_column(
    header: str,
    field: str,
    *,
    width: int | None = None,
    min_width: int | None = None,
    flex: int | None = None,
    pinned: str | None = None,
    formatter: dict[str, str] | None = None,
    numeric: bool = False,
    group_show: str | None = None,
    cell_class: str | None = None,
    cell_rules: dict[str, str] | None = None,
) -> dict[str, Any]:
    column: dict[str, Any] = {
        "headerName": header,
        "field": field,
        "tooltipField": field,
    }
    if width is not None:
        column["width"] = width
    if min_width is not None:
        column["minWidth"] = min_width
    if flex is not None:
        column["flex"] = flex
    if pinned is not None:
        column["pinned"] = pinned
        column["lockPinned"] = True
    if formatter is not None:
        column["valueFormatter"] = formatter
    if numeric:
        column["type"] = "rightAligned"
        column["cellClass"] = "vol-trades-number-cell"
        column["headerClass"] = "vol-trades-number-header"
    elif cell_class is not None:
        column["cellClass"] = cell_class
    if group_show is not None:
        column["columnGroupShow"] = group_show
    if cell_rules is not None:
        column["cellClassRules"] = cell_rules
    return column


def _grid_group(
    header: str,
    css_class: str,
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "headerName": header,
        "headerClass": css_class,
        "marryChildren": True,
        "children": children,
    }


DETAIL_COLUMN_DEFS = [
    _grid_group(
        "Contract",
        "vol-trades-group-contract",
        [
            _grid_column(
                "Bloomberg security",
                "option_security",
                pinned="left",
                min_width=176,
                cell_class="vol-trades-security-cell",
            ),
            _grid_column(
                "P/C",
                "put_call",
                pinned="left",
                width=58,
                cell_class="vol-trades-side-cell",
                cell_rules=_GRID_SIDE_RULES,
            ),
            _grid_column(
                "Strike",
                "strike",
                pinned="left",
                width=86,
                formatter=_GRID_PRICE_3DP,
                numeric=True,
            ),
        ],
    ),
    _grid_group(
        "Option premium",
        "vol-trades-group-premium",
        [
            _grid_column(
                "Settlement", "settlement_price", width=98,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Latest", "last_price", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Mid", "option_mid", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Bid", "option_bid", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Ask", "option_ask", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Spread", "option_spread", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Spread %", "option_spread_pct", width=92,
                formatter=_GRID_PERCENT_2DP, numeric=True, group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Volatility (%)",
        "vol-trades-group-volatility",
        [
            _grid_column(
                "Settlement IV", "implied_volatility_pct", width=102,
                formatter=_GRID_IV_2DP, numeric=True,
            ),
            _grid_column(
                "Exec mid", "executable_iv_mid_pct", width=92,
                formatter=_GRID_IV_2DP, numeric=True,
            ),
            _grid_column(
                "Exec bid", "executable_iv_bid_pct", width=92,
                formatter=_GRID_IV_2DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Exec ask", "executable_iv_ask_pct", width=92,
                formatter=_GRID_IV_2DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Exec status",
                "executable_iv_status",
                min_width=116,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Exec exclusion",
                "executable_iv_exclusion_reason",
                min_width=250,
                group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Activity (contracts)",
        "vol-trades-group-activity",
        [
            _grid_column(
                "Volume", "volume", width=94,
                formatter=_GRID_INTEGER, numeric=True,
            ),
            _grid_column(
                "New volume", "volume_delta", width=102,
                formatter=_GRID_INTEGER, numeric=True,
            ),
            _grid_column(
                "Open interest", "open_interest", width=108,
                formatter=_GRID_INTEGER, numeric=True,
            ),
            _grid_column(
                "OI date", "open_interest_date", width=104,
                formatter=_GRID_DATE,
            ),
            _grid_column(
                "Volume session",
                "volume_scope_status",
                min_width=176,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Volume delta status",
                "volume_delta_status",
                min_width=154,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "OI source", "open_interest_source",
                min_width=210, group_show="open",
            ),
            _grid_column(
                "OI session",
                "open_interest_scope_status",
                min_width=176,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Intraday OI", "intraday_open_interest", width=112,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Intraday OI date", "intraday_open_interest_date", width=132,
                formatter=_GRID_DATE, group_show="open",
            ),
            _grid_column(
                "Settlement OI", "settlement_open_interest", width=120,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Settlement OI date", "settlement_open_interest_date", width=142,
                formatter=_GRID_DATE, group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Pricing future",
        "vol-trades-group-future",
        [
            _grid_column("Contract", "pricing_future", min_width=136),
            _grid_column(
                "Reference", "underlying_price", width=96,
                formatter=_GRID_PRICE_3DP, numeric=True,
            ),
            _grid_column(
                "Bid", "underlying_bid", width=88,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Mid", "underlying_mid", width=88,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Ask", "underlying_ask", width=88,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Spread", "underlying_spread", width=90,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Finance rate %", "finance_rate_pct", width=112,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Rate observed",
                "finance_rate_observed_at",
                min_width=152,
                formatter=_GRID_DATETIME_GST,
                group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Last exact trade",
        "vol-trades-group-trade",
        [
            _grid_column(
                "Price", "last_trade_price_exact", width=92,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Time GST", "last_trade_at_gst", min_width=142,
                formatter=_GRID_DATETIME_GST,
            ),
            _grid_column(
                "Matched future", "last_trade_underlying_price", width=112,
                formatter=_GRID_PRICE_3DP, numeric=True,
            ),
            _grid_column(
                "Trade IV", "last_trade_iv_pct", width=92,
                formatter=_GRID_IV_2DP, numeric=True,
            ),
            _grid_column(
                "Trade date", "last_trade_date", width=104,
                formatter=_GRID_DATE, group_show="open",
            ),
            _grid_column(
                "Future event GST",
                "last_trade_underlying_at_gst",
                min_width=152,
                formatter=_GRID_DATETIME_GST,
                group_show="open",
            ),
            _grid_column(
                "Match method", "last_trade_underlying_source",
                width=118, group_show="open",
            ),
            _grid_column(
                "Lag ms", "last_trade_match_lag_ms", width=88,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Condition", "last_trade_condition_codes",
                min_width=124, group_show="open",
            ),
            _grid_column(
                "IV status",
                "last_trade_iv_status",
                min_width=118,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "IV exclusion",
                "last_trade_iv_exclusion_reason",
                min_width=250,
                group_show="open",
            ),
            _grid_column(
                "Reused", "last_trade_reused", width=86, group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Quality & source",
        "vol-trades-group-quality",
        [
            _grid_column(
                "Smile eligible",
                "smile_eligible",
                width=128,
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Option expiry",
                "option_expiration_date",
                width=108,
                formatter=_GRID_DATE,
                group_show="open",
            ),
            _grid_column(
                "Global ID", "option_global_id", min_width=145, group_show="open",
            ),
            _grid_column(
                "Option underlier",
                "native_option_underlier",
                min_width=148,
                group_show="open",
            ),
            _grid_column(
                "IV status",
                "iv_status",
                min_width=112,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Exclusion reason",
                "exclusion_reason",
                min_width=260,
                flex=1,
                group_show="open",
            ),
            _grid_column(
                "Discovery", "discovery_method", min_width=128, group_show="open",
            ),
            _grid_column(
                "Batch", "quote_batch_id", width=76,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Capture ms", "quote_capture_skew_ms", width=98,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Request start",
                "quote_request_started_at",
                min_width=152,
                formatter=_GRID_DATETIME_GST,
                group_show="open",
            ),
            _grid_column(
                "Response",
                "quote_response_at",
                min_width=152,
                formatter=_GRID_DATETIME_GST,
                group_show="open",
            ),
        ],
    ),
]

TRADE_TAPE_COLUMN_DEFS = [
    _grid_group(
        "Trade",
        "vol-trades-group-contract",
        [
            _grid_column(
                "Time GST", "trade_time_gst", pinned="left", width=116,
                cell_class="vol-trades-time-cell",
            ),
            _grid_column(
                "Bloomberg security",
                "option_security",
                pinned="left",
                min_width=176,
                cell_class="vol-trades-security-cell",
            ),
            _grid_column(
                "P/C",
                "put_call",
                pinned="left",
                width=58,
                cell_class="vol-trades-side-cell",
                cell_rules=_GRID_SIDE_RULES,
            ),
            _grid_column(
                "Strike",
                "strike",
                pinned="left",
                width=86,
                formatter=_GRID_PRICE_3DP,
                numeric=True,
            ),
        ],
    ),
    _grid_group(
        "Print",
        "vol-trades-group-premium",
        [
            _grid_column(
                "Price", "trade_price", width=94,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Size", "trade_size", width=82,
                formatter=_GRID_INTEGER, numeric=True,
            ),
            _grid_column("Condition", "condition_codes", min_width=112),
        ],
    ),
    _grid_group(
        "Matched future",
        "vol-trades-group-future",
        [
            _grid_column(
                "Price", "future_match_price", width=96,
                formatter=_GRID_PRICE_3DP, numeric=True,
            ),
            _grid_column("Method", "future_match_source", width=122),
            _grid_column(
                "Bid / ask age", "future_quote_ages", width=136,
            ),
            _grid_column(
                "Lag ms", "future_match_lag_ms", width=86,
                formatter=_GRID_INTEGER, numeric=True,
            ),
        ],
    ),
    _grid_group(
        "Trade volatility",
        "vol-trades-group-volatility",
        [
            _grid_column(
                "IV %", "trade_iv_pct", width=88,
                formatter=_GRID_IV_2DP, numeric=True,
            ),
            _grid_column(
                "Status",
                "trade_iv_status",
                width=104,
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Exclusion reason",
                "trade_iv_exclusion_reason",
                min_width=250,
                flex=1,
            ),
        ],
    ),
]


EXPIRY_LEGEND_LAYER_ORDER = (
    "call-mid",
    "put-mid",
    "executable-band",
    "trades",
    "prior-settlement",
    "bloomberg-settlement",
    "published",
    "calibrated",
    "pricing-reference",
    "volume-calls",
    "volume-puts",
    "open-interest-calls",
    "open-interest-puts",
)

EXPIRY_LEGEND_LAYER_SPECS = {
    "call-mid": {
        "label": "Call mid",
        "group": "iv",
        "swatch": "brent-vol-history-legend-calls",
        "description": "Show or hide executable call mid volatility",
    },
    "put-mid": {
        "label": "Put mid",
        "group": "iv",
        "swatch": "brent-vol-history-legend-puts",
        "description": "Show or hide executable put mid volatility",
    },
    "executable-band": {
        "label": "Band",
        "group": "iv",
        "swatch": "brent-vol-history-legend-executable-band",
        "description": "Show or hide executable bid-ask volatility bands",
    },
    "trades": {
        "label": "Trades",
        "group": "iv",
        "swatch": "brent-vol-history-legend-trade",
        "description": (
            "Show or hide trade-time volatility; filled markers are exact "
            "midpoints and hollow markers are prevailing midpoints"
        ),
    },
    "prior-settlement": {
        "label": "Prior settle",
        "group": "reference",
        "swatch": "brent-vol-history-legend-prior-settlement",
        "description": "Show or hide the prior official settlement smile",
    },
    "bloomberg-settlement": {
        "label": "Settlement",
        "group": "reference",
        "swatch": "brent-vol-history-legend-bloomberg-settlement",
        "description": "Settlement from Bloomberg",
    },
    "published": {
        "label": "Published",
        "group": "reference",
        "swatch": "brent-vol-history-legend-published",
        "description": "Show or hide the exact-COB published surface",
    },
    "calibrated": {
        "label": "Calibrated",
        "group": "reference",
        "swatch": "brent-vol-history-legend-calibrated",
        "description": "Show or hide the latest governed calibrated surface",
    },
    "pricing-reference": {
        "label": "ATM / future",
        "group": "reference",
        "swatch": "brent-vol-history-legend-pricing-reference",
        "description": "Show or hide the ATM or pricing-future reference line",
    },
    "volume-calls": {
        "label": "Volume calls",
        "group": "activity",
        "swatch": "brent-vol-history-legend-volume-calls",
        "new_swatch": "brent-vol-history-legend-volume-calls-new",
        "description": "Show or hide call volume",
    },
    "volume-puts": {
        "label": "Volume puts",
        "group": "activity",
        "swatch": "brent-vol-history-legend-volume-puts",
        "new_swatch": "brent-vol-history-legend-volume-puts-new",
        "description": "Show or hide put volume",
    },
    "open-interest-calls": {
        "label": "OI calls",
        "group": "activity",
        "swatch": "brent-vol-history-legend-open-interest-calls",
        "description": "Show or hide call open interest",
    },
    "open-interest-puts": {
        "label": "OI puts",
        "group": "activity",
        "swatch": "brent-vol-history-legend-open-interest-puts",
        "description": "Show or hide put open interest",
    },
}


def _expiry_legend_options(
    available_layers: list[str] | tuple[str, ...],
    *,
    new_volume_layers: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    available = set(available_layers)
    new_volume = set(new_volume_layers)
    options = []
    previous_group = None
    for layer in EXPIRY_LEGEND_LAYER_ORDER:
        if layer not in available:
            continue
        spec = EXPIRY_LEGEND_LAYER_SPECS[layer]
        group_start = previous_group is not None and spec["group"] != previous_group
        label = spec["label"]
        description = spec["description"]
        swatch = spec["swatch"]
        if layer in new_volume:
            label = f"{label} · new edge"
            description = (
                f"{description}; an orange edge marks "
                "same-day volume added since the previous snapshot"
            )
            swatch = spec["new_swatch"]
        options.append(
            {
                "label": html.Span(
                    [
                        html.Span(
                            className=(
                                "brent-vol-history-legend-swatch " + swatch
                            ),
                            **{"aria-hidden": "true"},
                        ),
                        html.Span(label),
                    ],
                    className=(
                        "brent-vol-history-layer-label"
                        + (
                            " brent-vol-history-layer-group-start"
                            if group_start
                            else ""
                        )
                    ),
                    title=description,
                ),
                "value": layer,
            }
        )
        previous_group = spec["group"]
    return options


def _expiry_legend_controls(options, value):
    return [
        dcc.Checklist(
            id="brent-vol-history-expiry-layers",
            options=options,
            value=value,
            className="brent-vol-history-layer-options",
            inputClassName="brent-vol-history-layer-input",
            inline=True,
        ),
        html.Button(
            "Reset",
            id="brent-vol-history-expiry-layers-reset",
            n_clicks=0,
            className="brent-vol-history-layer-reset",
            title="Restore default chart layers",
        ),
    ]


def build_expiry_legend():
    """Return the shared, selectable layer key for every expiry chart."""
    return html.Div(
        _expiry_legend_controls([], []),
        id="brent-vol-history-expiry-legend",
        className="brent-vol-history-common-legend",
        role="group",
        **{"aria-label": "Common chart layers for all expiry panels"},
    )


def _oi_methodology_children(product: str):
    resolved_product = _normalize_product(product)
    if resolved_product == "TFO":
        title = "ICE Endex open-interest timing"
        copy = (
            "Official TFO open interest can be published after the option settlement. "
            "This page keeps the Bloomberg effective date, marks earlier observations "
            "as stale, and never carries settlement OI into the intraday view. Delayed "
            "OI publication does not change the recorded option or TZT futures settlement."
        )
        link_label = "ICE Dutch TTF Natural Gas options"
        href = "https://www.ice.com/products/71085679/Dutch-TTF-Natural-Gas-Options"
    elif resolved_product in {"ON", "LNE"}:
        title = "CME Henry Hub open-interest timing"
        copy = (
            "Bloomberg reports CME Henry Hub open interest with its effective date. "
            "This page marks earlier observations as stale and never carries settlement "
            "OI into the intraday view; delayed OI does not revise the recorded option "
            "premium or NG futures settlement."
        )
        link_label = "CME Henry Hub options"
        href = (
            "https://www.cmegroup.com/markets/energy/natural-gas/"
            "natural-gas.contractSpecs.options.html"
        )
    else:
        title = "ICE open-interest timing"
        copy = (
            "ICE Futures Europe calculates official open interest from positions held "
            "at the previous trading day’s close after the 10:00 UK position-maintenance "
            "cutoff on the next trading day. Until Bloomberg publishes a value for the "
            "selected business date, this page shows OI as pending and does not carry "
            "forward a prior-day figure. This routine OI process does not revise settlement "
            "prices or reported volume."
        )
        link_label = "ICE Futures Europe position-maintenance guidance"
        href = (
            "https://www.ice.com/publicdocs/futures/"
            "ICE_Futures_Europe_Position_Maintenance_Cut_Off_times.pdf"
        )
    return [
        html.Span(
            "OI",
            className="brent-vol-history-methodology-badge",
            **{"aria-hidden": "true"},
        ),
        html.Div(
            [
                html.H2(title, id="brent-vol-history-oi-methodology-title"),
                html.P(copy),
                html.A(
                    link_label,
                    href=href,
                    target="_blank",
                    rel="noopener noreferrer",
                ),
            ],
            className="brent-vol-history-methodology-copy",
        ),
    ]


layout = html.Main(
    [
        html.H1(
            "Vol trades",
            className="brent-vol-history-visually-hidden-heading",
        ),
        html.Header(
            [
                html.Div(
                    [
                        html.Fieldset(
                            [
                                html.Legend("Product"),
                                dcc.RadioItems(
                                    id="brent-vol-history-product",
                                    options=[
                                        {"label": "Brent", "value": "BRENT"},
                                        {"label": "TFO", "value": "TFO"},
                                        {"label": "HH · ON", "value": "ON"},
                                        {"label": "HH · LNE", "value": "LNE"},
                                    ],
                                    value=PRODUCT,
                                    inline=True,
                                    persistence=True,
                                    persistence_type="session",
                                    className="brent-vol-history-product-options",
                                ),
                            ],
                            className=(
                                "brent-vol-history-toolbar-control "
                                "brent-vol-history-product-control"
                            ),
                        ),
                        html.Fieldset(
                            [
                                html.Legend("X axis"),
                                dcc.RadioItems(
                                    id="brent-vol-history-x-axis",
                                    options=[
                                        {"label": "Strike", "value": X_AXIS_STRIKE},
                                        {"label": "Delta", "value": X_AXIS_DELTA},
                                    ],
                                    value=X_AXIS_STRIKE,
                                    inline=True,
                                    persistence=True,
                                    persistence_type="session",
                                    className="brent-vol-history-axis-options",
                                ),
                            ],
                            className=(
                                "brent-vol-history-toolbar-control "
                                "brent-vol-history-axis-control"
                            ),
                        ),
                        html.Div(
                            [
                                html.Span(
                                    "Bloomberg",
                                    className="brent-vol-history-toolbar-label",
                                ),
                                html.Div(
                                    [
                                        html.Button(
                                            "Refresh Bloomberg",
                                            id="brent-vol-history-refresh-button",
                                            n_clicks=0,
                                            disabled=True,
                                            className=(
                                                "brent-vol-history-refresh-button "
                                                "brent-vol-history-refresh-button-primary"
                                            ),
                                            title="Refresh today's Bloomberg snapshot",
                                            style={"display": "none"},
                                        ),
                                        html.Button(
                                            "Refresh settlements",
                                            id=(
                                                "brent-vol-history-settlement-"
                                                "refresh-button"
                                            ),
                                            n_clicks=0,
                                            disabled=True,
                                            className=(
                                                "brent-vol-history-refresh-button "
                                                "brent-vol-history-refresh-button-secondary"
                                            ),
                                            title=(
                                                "Load missing or incomplete Bloomberg "
                                                "settlements"
                                            ),
                                            style={"display": "none"},
                                        ),
                                        html.Div(
                                            id="brent-vol-history-refresh-status",
                                            className="brent-vol-history-refresh-status",
                                            role="status",
                                            **{"aria-live": "polite"},
                                        ),
                                    ],
                                    className="brent-vol-history-refresh-row",
                                ),
                            ],
                            className=(
                                "brent-vol-history-toolbar-control "
                                "brent-vol-history-refresh-control"
                            ),
                        ),
                        html.Div(
                            [
                                html.Label("Settlement", htmlFor="brent-vol-history-date"),
                                dcc.Dropdown(
                                    id="brent-vol-history-date",
                                    options=[],
                                    value=None,
                                    clearable=False,
                                    placeholder="No complete snapshots",
                                ),
                            ],
                            className=(
                                "brent-vol-history-toolbar-control "
                                "brent-vol-history-date-control"
                            ),
                        ),
                        html.Div(
                            [
                                html.Label(
                                    "Trade window",
                                    htmlFor="brent-vol-history-trade-start",
                                ),
                                html.Div(
                                    dcc.Slider(
                                        id="brent-vol-history-trade-start",
                                        min=0,
                                        max=1,
                                        value=0,
                                        marks={0: "00:00", 1: "Latest"},
                                        step=60,
                                        disabled=True,
                                        updatemode="mouseup",
                                        allow_direct_input=False,
                                        persistence=True,
                                        persistence_type="session",
                                    ),
                                    className="brent-vol-history-trade-slider-wrap",
                                ),
                            ],
                            className=(
                                "brent-vol-history-toolbar-control "
                                "brent-vol-history-trade-slider-control"
                            ),
                        ),
                        html.Div(
                            [
                                html.Span(
                                    "Presets",
                                    className="brent-vol-history-toolbar-label",
                                ),
                                html.Div(
                                    [
                                        html.Button(
                                            "All day",
                                            id="brent-vol-history-trade-all",
                                            n_clicks=0,
                                            disabled=True,
                                        ),
                                        html.Button(
                                            "4h",
                                            id="brent-vol-history-trade-4h",
                                            n_clicks=0,
                                            disabled=True,
                                        ),
                                        html.Button(
                                            "1h",
                                            id="brent-vol-history-trade-1h",
                                            n_clicks=0,
                                            disabled=True,
                                        ),
                                        html.Button(
                                            "15m",
                                            id="brent-vol-history-trade-15m",
                                            n_clicks=0,
                                            disabled=True,
                                        ),
                                        html.Button(
                                            "Latest print",
                                            id="brent-vol-history-trade-latest",
                                            n_clicks=0,
                                            disabled=True,
                                        ),
                                        html.Div(
                                            id="brent-vol-history-market-data-status",
                                            className=(
                                                "brent-vol-history-market-data-status"
                                            ),
                                        ),
                                    ],
                                    className="brent-vol-history-trade-presets",
                                    role="group",
                                    **{"aria-label": "Trade window presets"},
                                ),
                            ],
                            className=(
                                "brent-vol-history-toolbar-control "
                                "brent-vol-history-trade-preset-control"
                            ),
                        ),
                    ],
                    className="brent-vol-history-toolbar",
                ),
            ],
            className=(
                "professional-section-header "
                "brent-vol-history-sticky-filter-bar"
            ),
        ),
        dcc.Store(id="brent-vol-history-snapshot"),
        dcc.Store(id="brent-vol-history-source-status-mount", data=True),
        dcc.Store(id="brent-vol-history-trade-window-state", storage_type="session"),
        dcc.Store(id="brent-vol-history-expiry-layer-manifest"),
        dcc.Store(id="brent-vol-history-refresh-job", storage_type="session"),
        dcc.Store(id="brent-vol-history-refresh-completion", storage_type="session"),
        dcc.Interval(
            id="brent-vol-history-refresh-poll",
            interval=1000,
            n_intervals=0,
            disabled=True,
        ),
        dcc.Interval(
            id="brent-vol-history-worker-poll",
            interval=10000,
            n_intervals=0,
            disabled=False,
        ),
        html.Section(
            [
                html.Div(
                    [
                        html.H2(
                            "Expiry panels",
                            className=(
                                "section-title-inline greeks-monitor-title "
                                "brent-vol-history-section-title"
                            ),
                        ),
                        build_expiry_legend(),
                    ],
                    className=(
                        "inline-section-header supply-dest-section-header "
                        "greeks-monitor-section-header "
                        "brent-vol-history-expiry-section-header"
                    ),
                ),
                dcc.Loading(
                    type="circle",
                    children=html.Div(
                        id="brent-vol-history-plots",
                        className="brent-vol-history-plot-grid",
                    ),
                ),
            ],
            className=(
                "main-section-container supply-dest-section greeks-monitor-section "
                "brent-vol-history-section brent-vol-history-expiry-section"
            ),
        ),
        html.Section(
            [
                html.Div(
                    [
                        html.Div(
                            [
                                html.H2(
                                    "Exact trade tape",
                                    className="brent-vol-history-table-title",
                                ),
                                html.P(
                                    (
                                        "Time-ordered prints for the selected expiry "
                                        "and trade window."
                                    ),
                                    className="brent-vol-history-table-subtitle",
                                ),
                            ],
                            className="brent-vol-history-table-heading",
                        ),
                        html.Span(
                            [
                                html.Span(
                                    className=(
                                        "brent-vol-history-table-context-dot"
                                    ),
                                    **{"aria-hidden": "true"},
                                ),
                                "Latest print only",
                            ],
                            className="brent-vol-history-table-context",
                        ),
                    ],
                    className="brent-vol-history-table-header",
                ),
                dag.AgGrid(
                    id="brent-vol-history-trade-grid",
                    rowData=[],
                    columnDefs=TRADE_TAPE_COLUMN_DEFS,
                    eventListeners={
                        "modelUpdated": [
                            (
                                "params.api.setGridAriaProperty('label', "
                                "'Bloomberg exact option trade tape')"
                            )
                        ],
                    },
                    defaultColDef={
                        "sortable": True,
                        "filter": True,
                        "resizable": True,
                        "suppressHeaderFilterButton": True,
                    },
                    dashGridOptions={
                        "rowHeight": 30,
                        "headerHeight": 34,
                        "groupHeaderHeight": 28,
                        "pagination": True,
                        "paginationPageSize": 50,
                        "enableCellTextSelection": True,
                        "animateRows": False,
                        "ensureDomOrder": True,
                        "tooltipShowDelay": 250,
                        "overlayNoRowsTemplate": (
                            "<span>No exact trades in this expiry and trade "
                            "window.</span>"
                        ),
                        "getRowId": {"function": "params.data.event_id"},
                        "ariaLabel": "Bloomberg exact option trade tape",
                    },
                    className=(
                        "ag-theme-alpine mckinsey-ag-grid brent-vol-history-grid "
                        "brent-vol-history-table-grid vol-trades-trade-grid"
                    ),
                    style={"width": "100%", "height": "560px"},
                    dangerously_allow_code=True,
                ),
            ],
            className=(
                "brent-vol-history-section brent-vol-history-table-section "
                "brent-vol-history-trade-table-section"
            ),
        ),
        html.Section(
            [
                html.Div(
                    [
                        html.Div(
                            [
                                html.H2(
                                    "Option-chain detail",
                                    className="brent-vol-history-table-title",
                                ),
                                html.P(
                                    (
                                        "Premium, volatility and activity by strike. "
                                        "Expand grouped headers for diagnostics."
                                    ),
                                    className="brent-vol-history-table-subtitle",
                                ),
                            ],
                            className="brent-vol-history-table-heading",
                        ),
                        html.Div(
                            [
                                html.Label("Detail expiry", htmlFor="brent-vol-history-detail-expiry"),
                                dcc.Dropdown(
                                    id="brent-vol-history-detail-expiry",
                                    options=[],
                                    value=None,
                                    clearable=False,
                                ),
                            ],
                            className="brent-vol-history-detail-control",
                        ),
                    ],
                    className=(
                        "brent-vol-history-detail-header "
                        "brent-vol-history-table-header"
                    ),
                ),
                dag.AgGrid(
                    id="brent-vol-history-grid",
                    rowData=[],
                    columnDefs=DETAIL_COLUMN_DEFS,
                    eventListeners={
                        "firstDataRendered": [
                            (
                                "params.api.setGridAriaProperty('label', "
                                "'Bloomberg option-chain detail')"
                            )
                        ]
                    },
                    defaultColDef={
                        "sortable": True,
                        "filter": True,
                        "resizable": True,
                        "suppressHeaderMenuButton": False,
                        "suppressHeaderFilterButton": True,
                    },
                    dashGridOptions={
                        "rowHeight": 30,
                        "headerHeight": 34,
                        "groupHeaderHeight": 28,
                        "pagination": True,
                        "paginationPageSize": 50,
                        "enableCellTextSelection": True,
                        "animateRows": False,
                        "ensureDomOrder": True,
                        "tooltipShowDelay": 250,
                        "overlayNoRowsTemplate": (
                            "<span>No options are available for this expiry.</span>"
                        ),
                        "getRowId": {"function": "params.data.option_security"},
                        "ariaLabel": "Bloomberg option-chain detail",
                    },
                    className=(
                        "ag-theme-alpine mckinsey-ag-grid brent-vol-history-grid "
                        "brent-vol-history-table-grid vol-trades-chain-grid"
                    ),
                    style={"width": "100%", "height": "560px"},
                    dangerously_allow_code=True,
                ),
            ],
            className=(
                "brent-vol-history-section brent-vol-history-table-section "
                "brent-vol-history-chain-table-section"
            ),
        ),
        html.Aside(
            _oi_methodology_children(PRODUCT),
            id="brent-vol-history-oi-methodology",
            className="brent-vol-history-methodology-note",
            **{
                "aria-labelledby": "brent-vol-history-oi-methodology-title",
                "role": "note",
            },
        ),
    ],
    className="options-dashboard-container brent-vol-history-page",
)


@callback(
    Output("brent-vol-history-oi-methodology", "children"),
    Input("brent-vol-history-product", "value"),
)
def render_oi_methodology(product):
    return _oi_methodology_children(_normalize_product(product))


def _request_identity():
    headers = request.headers if has_request_context() else {}
    remote_addr = request.remote_addr if has_request_context() else None
    return resolve_request_identity(headers, remote_addr=remote_addr)


def _authorize_refresh():
    identity = _request_identity()
    authorize(identity, Permission.REFRESH_BLOOMBERG)
    return identity


def _trade_slider_config(
    trade_tape: pd.DataFrame,
    observed_at: Any,
    *,
    preserve_lookback: bool = False,
    previous_start: Any = None,
    previous_max: Any = None,
) -> tuple[int, int, int, dict[int, str], bool]:
    cutoff = pd.to_datetime(observed_at, errors="coerce", utc=True)
    if trade_tape is not None and not trade_tape.empty:
        tape_cutoff = pd.to_datetime(
            trade_tape["cutoff_at"], errors="coerce", utc=True
        ).dropna()
        if not tape_cutoff.empty:
            cutoff = tape_cutoff.max()
    if pd.isna(cutoff):
        return 0, 1, 0, {0: "00:00", 1: "Latest"}, True
    local = cutoff.tz_convert("Asia/Dubai")
    maximum = max(1, int(local.hour * 3600 + local.minute * 60 + local.second))
    value = 0
    if preserve_lookback and previous_start is not None and previous_max is not None:
        lookback = max(0, int(float(previous_max) - float(previous_start)))
        value = max(0, maximum - lookback)
    mark_values = sorted({0, maximum, *(value for value in (21600, 43200, 64800) if value < maximum)})
    marks = {
        value: (
            "Latest"
            if value == maximum
            else f"{value // 3600:02d}:{(value % 3600) // 60:02d}"
        )
        for value in mark_values
    }
    return 0, maximum, value, marks, trade_tape is None or trade_tape.empty


REFRESH_STAGE_LABELS = {
    "queued": "Waiting for Bloomberg worker",
    "discovery": "Discovering chain",
    "market_data": "Fetching market data",
    "quote_cut": "Capturing synchronized quotes",
    "trade_detection": "Detecting changed trades",
    "trade_ticks": "Confirming exact option ticks",
    "future_match": "Matching trade-time futures",
    "pricing": "Pricing executable implied volatility",
    "persistence": "Persisting snapshot",
    "reconciliation": "Reconciling database readback",
}

SETTLEMENT_REFRESH_STAGE_LABELS = {
    "queued": "Waiting for Bloomberg worker",
    "availability": "Checking Bloomberg settlements",
    "coverage": "Checking settlement coverage",
    "planning": "Checking settlement coverage",
    "settlement_availability": "Checking Bloomberg settlements",
    "settlement_coverage": "Checking settlement coverage",
    "settlement_planning": "Checking settlement coverage",
    "discovery": "Updating option universe",
    "universe": "Updating option universe",
    "universe_reconciliation": "Updating option universe",
    "market_data": "Fetching settlement history",
    "historical_data": "Fetching settlement history",
    "settlement_history": "Fetching settlement history",
    "pricing": "Pricing settlement volatility",
    "persistence": "Saving settlements",
    "settlement_persistence": "Saving settlements",
    "reconciliation": "Reconciling settlement readback",
    "settlement_reconciliation": "Reconciling settlement readback",
}


def _job_request_kind(job, active_job=None) -> str:
    request_kind = getattr(job, "request_kind", None)
    if not request_kind and active_job:
        request_kind = active_job.get("request_kind")
    return str(request_kind or INTRADAY_REQUEST_KIND).strip().lower()


def _short_job_date(value: Any) -> str | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return f"{parsed.day} {parsed.strftime('%b')}"


def _settlement_metric_dates(metrics: dict[str, Any], key: str) -> list[str]:
    values = metrics.get(key) or []
    if isinstance(values, (str, pd.Timestamp)):
        values = [values]
    return [
        formatted
        for formatted in (_short_job_date(value) for value in values)
        if formatted
    ]


def _latest_settlement_label(metrics: dict[str, Any]) -> str | None:
    for key in (
        "latest_settlement_date",
        "latest_available_settlement_date",
        "settlement_cutoff_date",
    ):
        formatted = _short_job_date(metrics.get(key))
        if formatted:
            return formatted
    completed = _settlement_metric_dates(metrics, "completed_dates")
    return completed[-1] if completed else None


def _settlement_success_message(job) -> str:
    metrics = dict(getattr(job, "metrics", None) or {})
    latest = _latest_settlement_label(metrics)
    through = f" through {latest}" if latest else ""
    result_status = str(getattr(job, "result_status", "") or "").lower()
    if result_status in {"noop", "fresh_reuse", "current", "settlements_current"}:
        return f"Settlements already current{through}."

    pending = _settlement_metric_dates(metrics, "oi_pending_dates")
    if not pending:
        pending = _settlement_metric_dates(metrics, "activity_pending_dates")
    failed = _settlement_metric_dates(metrics, "failed_dates")
    prefix = "Settlements partially refreshed" if failed else "Settlements refreshed"
    details = []
    if failed:
        details.append(f"{', '.join(failed)} unavailable")
    if pending:
        details.append(f"OI pending for {', '.join(pending)}")
    suffix = f" · {' · '.join(details)}" if details else ""
    return f"{prefix}{through}{suffix}."


def _refresh_stage_label(job, request_kind: str) -> str:
    stage = str(getattr(job, "stage", "") or "queued")
    labels = (
        SETTLEMENT_REFRESH_STAGE_LABELS
        if request_kind == SETTLEMENT_REQUEST_KIND
        else REFRESH_STAGE_LABELS
    )
    label = labels.get(stage, stage.replace("_", " ").title())
    if request_kind != SETTLEMENT_REQUEST_KIND or "Saving" not in label:
        return label
    metrics = dict(getattr(job, "metrics", None) or {})
    completed = len(metrics.get("completed_dates") or [])
    planned = len(metrics.get("planned_dates") or [])
    if planned:
        return f"{label} ({completed}/{planned})"
    return label


def _worker_readiness_message(readiness, product_label: str) -> str:
    if readiness.ready:
        return f"Bloomberg {product_label} worker ready."
    if readiness.reason == "registry_unavailable":
        return (
            "Bloomberg worker status is unavailable. Apply migration 010 and "
            "start the worker."
        )
    if readiness.reason == "no_eligible_worker":
        return (
            f"No Bloomberg worker is enabled for {product_label}. Enable "
            f"{readiness.product} and start the worker."
        )
    if readiness.reason == "stale_heartbeat":
        return (
            f"Bloomberg {product_label} worker is offline—no heartbeat in "
            f"{WORKER_FRESHNESS_SECONDS} seconds. Start or restart the worker."
        )
    return f"Bloomberg {product_label} worker is offline. Start or restart it."


def _queued_job_wait_exceeded(job, *, now: Any = None) -> bool:
    created_at = pd.to_datetime(
        getattr(job, "updated_at", None) or getattr(job, "created_at", None),
        errors="coerce",
        utc=True,
    )
    if pd.isna(created_at):
        return False
    current = pd.to_datetime(now, errors="coerce", utc=True)
    if pd.isna(current):
        current = pd.Timestamp.now(tz="UTC")
    return (current - created_at).total_seconds() > WORKER_FRESHNESS_SECONDS


def _product_store(value: Any, payload_key: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    if payload_key in value:
        product = _normalize_product(value.get("product"))
        return {product: dict(value)}
    return {
        _normalize_product(product): dict(payload)
        for product, payload in value.items()
        if str(product).upper() in SUPPORTED_PRODUCTS and isinstance(payload, dict)
    }


@callback(
    Output("brent-vol-history-refresh-job", "data"),
    Output("brent-vol-history-refresh-completion", "data"),
    Output("brent-vol-history-refresh-poll", "disabled"),
    Output("brent-vol-history-refresh-button", "disabled"),
    Output("brent-vol-history-settlement-refresh-button", "disabled"),
    Output("brent-vol-history-refresh-status", "children"),
    Output("brent-vol-history-refresh-status", "className"),
    Output("brent-vol-history-refresh-button", "style"),
    Output("brent-vol-history-settlement-refresh-button", "style"),
    Input("brent-vol-history-refresh-button", "n_clicks"),
    Input("brent-vol-history-settlement-refresh-button", "n_clicks"),
    Input("brent-vol-history-refresh-poll", "n_intervals"),
    Input("brent-vol-history-worker-poll", "n_intervals"),
    Input("brent-vol-history-product", "value"),
    State("brent-vol-history-refresh-job", "data"),
    State("brent-vol-history-refresh-completion", "data"),
)
def manage_bloomberg_refresh(
    _refresh_clicks,
    _settlement_refresh_clicks,
    _poll_count,
    _worker_poll_count,
    product,
    active_jobs_value,
    completions_value,
):
    product = _normalize_product(product)
    product_label = _product_spec(product)["label"]
    active_jobs = _product_store(active_jobs_value, "job_id")
    completions = _product_store(completions_value, "result_snapshot_id")
    active_job = active_jobs.get(product)
    current_completion = completions.get(product)
    try:
        triggered = ctx.triggered_id
    except Exception:
        triggered = None
    suppress_unchanged_stores = triggered in {
        "brent-vol-history-refresh-poll",
        "brent-vol-history-worker-poll",
    }

    def response(
        job_payload,
        completion_payload,
        poll_disabled,
        intraday_disabled,
        settlement_disabled,
        message,
        status_class,
        intraday_style,
        settlement_style,
    ):
        updated_jobs = dict(active_jobs)
        updated_completions = dict(completions)
        if job_payload is None:
            updated_jobs.pop(product, None)
        else:
            updated_jobs[product] = dict(job_payload)
        if completion_payload is not None:
            updated_completions[product] = dict(completion_payload)
        jobs_output = (
            no_update
            if suppress_unchanged_stores and updated_jobs == active_jobs
            else updated_jobs
        )
        completions_output = (
            no_update
            if suppress_unchanged_stores and updated_completions == completions
            else updated_completions
        )
        return (
            jobs_output,
            completions_output,
            poll_disabled,
            intraday_disabled,
            settlement_disabled,
            message,
            status_class,
            intraday_style,
            settlement_style,
        )

    hidden = {"display": "none"}
    visible = {"display": "inline-flex"}
    intraday_enabled = intraday_refresh_enabled(product)
    settlement_enabled = settlement_refresh_enabled(product)
    intraday_style = visible if intraday_enabled else hidden
    settlement_style = visible if settlement_enabled else hidden
    idle_disabled = (not intraday_enabled, not settlement_enabled)
    if not intraday_enabled and not settlement_enabled:
        return response(
            None,
            current_completion,
            True,
            True,
            True,
            f"Bloomberg {product_label} refresh is disabled by configuration.",
            "brent-vol-history-refresh-status brent-vol-history-refresh-status-neutral",
            hidden,
            hidden,
        )
    try:
        identity = _authorize_refresh()
    except Exception:
        return response(
            active_job,
            current_completion,
            True,
            True,
            True,
            "",
            "brent-vol-history-refresh-status",
            hidden,
            hidden,
        )

    try:
        triggered_kind = None
        if triggered == "brent-vol-history-refresh-button":
            triggered_kind = INTRADAY_REQUEST_KIND
            if not intraday_enabled:
                raise PermissionError(
                    "Bloomberg intraday refresh is disabled by configuration."
                )
        elif triggered == "brent-vol-history-settlement-refresh-button":
            triggered_kind = SETTLEMENT_REQUEST_KIND
            if not settlement_enabled:
                raise PermissionError(
                    "Bloomberg settlement refresh is disabled by configuration."
                )

        if triggered_kind:
            readiness = get_worker_readiness(product)
            if not readiness.ready:
                job_needs_polling = bool(
                    active_job
                    and active_job.get("status") in {"queued", "running"}
                )
                return response(
                    active_job,
                    current_completion,
                    not job_needs_polling,
                    True,
                    True,
                    _worker_readiness_message(readiness, product_label),
                    (
                        "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-danger"
                    ),
                    intraday_style,
                    settlement_style,
                )
            job, created = submit_refresh_job(
                identity.subject or "",
                product=product,
                request_kind=triggered_kind,
            )
            if triggered_kind == SETTLEMENT_REQUEST_KIND:
                message = (
                    "Settlement refresh queued."
                    if created
                    else "Joined the settlement refresh already in progress."
                )
            else:
                message = (
                    "Bloomberg refresh queued."
                    if created
                    else "Joined the Bloomberg refresh already in progress."
                )
            return response(
                job.as_dict(),
                current_completion,
                False,
                True,
                True,
                message,
                "brent-vol-history-refresh-status brent-vol-history-refresh-status-active",
                intraday_style,
                settlement_style,
            )

        if active_job and active_job.get("job_id"):
            job = get_refresh_job(active_job["job_id"], product=product)
            if job is None:
                raise RuntimeError("The queued refresh job no longer exists.")
            request_kind = _job_request_kind(job, active_job)
            if job.status == "succeeded":
                readiness = get_worker_readiness(product)
                completion = {
                    "job_id": job.job_id,
                    "product": product,
                    "request_kind": request_kind,
                    "result_snapshot_id": job.result_snapshot_id,
                    "result_status": job.result_status,
                    "updated_at": job.updated_at,
                }
                if request_kind == SETTLEMENT_REQUEST_KIND:
                    message = _settlement_success_message(job)
                elif job.result_status == "fresh_reuse":
                    message = (
                        "Fresh complete Bloomberg snapshot reused—no new "
                        "Bloomberg request was needed."
                    )
                elif job.result_status == "noop":
                    message = "Checked Bloomberg—no market-data changes."
                else:
                    message = "Bloomberg intraday snapshot is ready."
                result_is_partial = (
                    str(job.result_status or "").lower() == "partial"
                    or bool((getattr(job, "metrics", None) or {}).get("failed_dates"))
                )
                status_class = (
                    "brent-vol-history-refresh-status "
                    "brent-vol-history-refresh-status-warning"
                    if result_is_partial
                    else "brent-vol-history-refresh-status "
                    "brent-vol-history-refresh-status-success"
                )
                if not readiness.ready:
                    message = (
                        f"{message} "
                        f"{_worker_readiness_message(readiness, product_label)}"
                    )
                    status_class = (
                        "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-danger"
                    )
                return response(
                    job.as_dict(),
                    completion,
                    True,
                    not (intraday_enabled and readiness.ready),
                    not (settlement_enabled and readiness.ready),
                    message,
                    status_class,
                    intraday_style,
                    settlement_style,
                )
            if job.status == "failed":
                readiness = get_worker_readiness(product)
                if job.metrics.get("failure_category") == "daily_capacity_reached":
                    failure_message = (
                        "Bloomberg daily request capacity has been reached. "
                        "The displayed snapshot is unchanged; refresh again after "
                        "Bloomberg resets the entitlement."
                    )
                else:
                    fallback = (
                        "Bloomberg settlement refresh failed."
                        if request_kind == SETTLEMENT_REQUEST_KIND
                        else "Bloomberg refresh failed."
                    )
                    failure_message = (
                        f"{job.last_error or fallback} "
                        "Check the worker and retry."
                    )
                return response(
                    job.as_dict(),
                    current_completion,
                    True,
                    not (intraday_enabled and readiness.ready),
                    not (settlement_enabled and readiness.ready),
                    failure_message,
                    "brent-vol-history-refresh-status brent-vol-history-refresh-status-danger",
                    intraday_style,
                    settlement_style,
                )
            readiness = None
            if job.status == "queued":
                readiness = get_worker_readiness(product)
                if not readiness.ready:
                    return response(
                        job.as_dict(),
                        current_completion,
                        False,
                        True,
                        True,
                        _worker_readiness_message(readiness, product_label),
                        (
                            "brent-vol-history-refresh-status "
                            "brent-vol-history-refresh-status-danger"
                        ),
                        intraday_style,
                        settlement_style,
                    )
                if _queued_job_wait_exceeded(job):
                    return response(
                        job.as_dict(),
                        current_completion,
                        False,
                        True,
                        True,
                        (
                            f"Bloomberg {product_label} worker is online—this "
                            "request is queued behind another refresh. The page "
                            "will keep monitoring it."
                        ),
                        (
                            "brent-vol-history-refresh-status "
                            "brent-vol-history-refresh-status-active"
                        ),
                        intraday_style,
                        settlement_style,
                    )
            stage = _refresh_stage_label(job, request_kind)
            return response(
                job.as_dict(),
                current_completion,
                False,
                True,
                True,
                f"{stage}…",
                "brent-vol-history-refresh-status brent-vol-history-refresh-status-active",
                intraday_style,
                settlement_style,
            )
        readiness = get_worker_readiness(product)
        return response(
            None,
            current_completion,
            True,
            not (intraday_enabled and readiness.ready),
            not (settlement_enabled and readiness.ready),
            _worker_readiness_message(readiness, product_label),
            (
                "brent-vol-history-refresh-status "
                + (
                    "brent-vol-history-refresh-status-success"
                    if readiness.ready
                    else "brent-vol-history-refresh-status-danger"
                )
            ),
            intraday_style,
            settlement_style,
        )
    except Exception as exc:
        return response(
            active_job,
            current_completion,
            True,
            idle_disabled[0],
            idle_disabled[1],
            f"{_safe_message(exc)}. Check the Bloomberg worker and retry.",
            "brent-vol-history-refresh-status brent-vol-history-refresh-status-danger",
            intraday_style,
            settlement_style,
        )


@callback(
    Output("brent-vol-history-date", "options"),
    Output("brent-vol-history-date", "value"),
    Input("refresh-options-data", "n_clicks"),
    Input("brent-vol-history-refresh-completion", "data"),
    Input("brent-vol-history-product", "value"),
    State("brent-vol-history-date", "value"),
)
def update_history_dates(
    _refresh_clicks=None,
    completion=None,
    product=PRODUCT,
    current_value=None,
):
    product = _normalize_product(product)
    try:
        snapshots = load_available_snapshots(product)
    except Exception:
        return [], None
    if snapshots.empty:
        return [], None
    options = []
    for snapshot in snapshots.itertuples(index=False):
        observed = pd.to_datetime(snapshot.observed_at, errors="coerce", utc=True)
        if str(snapshot.snapshot_kind).upper() == "INTRADAY":
            local = observed.tz_convert("Asia/Dubai") if not pd.isna(observed) else observed
            label = (
                f"Intraday {local.strftime('%H:%M')} GST"
                if not pd.isna(local)
                else "Intraday"
            )
        else:
            label = pd.Timestamp(snapshot.business_date).strftime("%d %b %Y")
        options.append({"label": label, "value": str(snapshot.snapshot_id)})
    allowed = {option["value"] for option in options}
    product_completion = _product_store(
        completion, "result_snapshot_id"
    ).get(product, {})
    completed_snapshot = product_completion.get("result_snapshot_id")
    if completed_snapshot in allowed:
        value = completed_snapshot
    else:
        value = current_value if current_value in allowed else options[0]["value"]
    return options, value


@callback(
    Output("brent-vol-history-snapshot", "data"),
    Output("brent-vol-history-plots", "children"),
    Output("brent-vol-history-detail-expiry", "options"),
    Output("brent-vol-history-detail-expiry", "value"),
    Output("brent-vol-history-trade-start", "min"),
    Output("brent-vol-history-trade-start", "max"),
    Output("brent-vol-history-trade-start", "value"),
    Output("brent-vol-history-trade-start", "marks"),
    Output("brent-vol-history-trade-start", "disabled"),
    Output("brent-vol-history-trade-all", "disabled"),
    Output("brent-vol-history-trade-4h", "disabled"),
    Output("brent-vol-history-trade-1h", "disabled"),
    Output("brent-vol-history-trade-15m", "disabled"),
    Output("brent-vol-history-trade-latest", "disabled"),
    Output("brent-vol-history-expiry-legend", "children"),
    Output("brent-vol-history-expiry-layer-manifest", "data"),
    Input("brent-vol-history-date", "value"),
    Input("brent-vol-history-x-axis", "value"),
    Input("brent-vol-history-product", "value"),
    State("brent-vol-history-detail-expiry", "value"),
    State("brent-vol-history-trade-window-state", "data"),
    State("brent-vol-history-expiry-layers", "options"),
    State("brent-vol-history-expiry-layers", "value"),
)
def render_history(
    selected_snapshot_id,
    x_axis=X_AXIS_STRIKE,
    product=PRODUCT,
    current_detail_expiry=None,
    current_trade_window=None,
    current_legend_options=None,
    current_legend_value=None,
):
    x_axis = _normalize_x_axis(x_axis)
    product = _normalize_product(product)
    if not selected_snapshot_id:
        cards = build_plot_cards(
            pd.DataFrame(), pd.DataFrame(), x_axis=x_axis, product=product
        )
        legend_contract = _expiry_legend_contract(cards)
        return (
            None,
            cards,
            [],
            None,
            0,
            1,
            0,
            {0: "00:00", 1: "Latest"},
            True,
            True,
            True,
            True,
            True,
            True,
            _expiry_legend_controls([], []),
            legend_contract,
        )
    try:
        snapshots = load_available_snapshots(product)
        selected = snapshots[snapshots["snapshot_id"].astype(str).eq(selected_snapshot_id)]
        if selected.empty:
            raise ValueError("selected snapshot is no longer available")
        snapshot = selected.iloc[0]
        snapshot_id = str(snapshot["snapshot_id"])
        snapshot_kind = _normalize_snapshot_kind(
            snapshot.get("snapshot_kind") or "SETTLEMENT"
        )
        raw_chain = load_chain_snapshot(
            snapshot_id,
            product=product,
            snapshot_kind=snapshot_kind,
        )
        if raw_chain.empty:
            raise ValueError("complete snapshot has no option-chain rows")
        selected_date = pd.Timestamp(snapshot["business_date"]).date().isoformat()
        chain, universe = select_history_universe(
            raw_chain, snapshot_kind, product=product
        )
        if chain.empty:
            raise ValueError("snapshot has no rows in the governed display universe")
        display_expiries = universe.get("selected_contract_months") or []
        trade_tape = (
            _filter_contract_months(
                load_trade_tape(
                    snapshot_id,
                    product=product,
                    snapshot_kind=snapshot_kind,
                ),
                display_expiries,
            )
            if snapshot_kind == "INTRADAY"
            else pd.DataFrame()
        )
        prior_settlement_chain = pd.DataFrame()
        if snapshot_kind == "INTRADAY":
            snapshot_dates = pd.to_datetime(
                snapshots["business_date"], errors="coerce"
            ).dt.normalize()
            prior_candidates = snapshots.loc[
                snapshots["snapshot_kind"].astype(str).str.upper().eq("SETTLEMENT")
                & snapshot_dates.lt(pd.Timestamp(selected_date))
            ].copy()
            if not prior_candidates.empty:
                prior_candidates["_business_date"] = pd.to_datetime(
                    prior_candidates["business_date"], errors="coerce"
                )
                prior_candidates["_observed_at"] = pd.to_datetime(
                    prior_candidates["observed_at"], errors="coerce", utc=True
                )
                prior_snapshot = prior_candidates.sort_values(
                    ["_business_date", "_observed_at"], ascending=False
                ).iloc[0]
                try:
                    prior_settlement_chain = _filter_contract_months(
                        load_chain_snapshot(
                            str(prior_snapshot["snapshot_id"]),
                            product=product,
                            snapshot_kind="SETTLEMENT",
                        ),
                        display_expiries,
                    )
                except Exception:
                    prior_settlement_chain = pd.DataFrame()
        published = (
            pd.DataFrame()
            if snapshot_kind == "INTRADAY"
            else load_published_surface(
                selected_date,
                product=product,
                snapshot_kind=snapshot_kind,
            )
        )
        try:
            calibrated = load_latest_calibrated_surface(
                selected_date,
                display_expiries,
                product=product,
            )
        except Exception:
            calibrated = _empty_calibrated_surface(
                "query_error",
                commodity=_product_spec(product)["published_product"],
                selected_cob=selected_date,
            )
        expiries = sorted(
            pd.to_datetime(chain["underlying_contract_month"], errors="coerce")
            .dropna()
            .dt.normalize()
            .unique()
        )
        expiry_options = [
            {"label": pd.Timestamp(value).strftime("%b-%y"), "value": pd.Timestamp(value).date().isoformat()}
            for value in expiries
        ]
        allowed = {option["value"] for option in expiry_options}
        detail_value = (
            current_detail_expiry
            if current_detail_expiry in allowed
            else expiry_options[0]["value"]
        )
        preserve_lookback = bool(
            snapshot_kind == "INTRADAY"
            and current_trade_window
            and current_trade_window.get("product") == product
            and current_trade_window.get("business_date") == selected_date
            and current_trade_window.get("maximum") is not None
            and float(current_trade_window["maximum"]) > 1
        )
        slider_min, slider_max, slider_value, slider_marks, slider_disabled = (
            _trade_slider_config(
                trade_tape,
                snapshot["observed_at"],
                preserve_lookback=preserve_lookback,
                previous_start=(current_trade_window or {}).get("start"),
                previous_max=(current_trade_window or {}).get("maximum"),
            )
            if snapshot_kind == "INTRADAY"
            else (0, 1, 0, {0: "00:00", 1: "Latest"}, True)
        )
        cards = build_plot_cards(
            chain,
            published,
            x_axis=x_axis,
            trade_tape=(trade_tape if snapshot_kind == "INTRADAY" else None),
            prior_settlement_chain=(
                prior_settlement_chain
                if snapshot_kind == "INTRADAY"
                else None
            ),
            product=product,
            calibrated=calibrated,
        )
        legend_contract = _expiry_legend_contract(cards)
        legend_options = _expiry_legend_options(
            legend_contract["available_layers"],
            new_volume_layers=legend_contract["new_volume_layers"],
        )
        selected_layers = _selected_expiry_layers(
            legend_contract["available_layers"],
            current_legend_options,
            current_legend_value,
        )
        _apply_expiry_layer_selection(cards, legend_contract, selected_layers)
        return (
            {
                "snapshot_id": snapshot_id,
                "product": product,
                "business_date": selected_date,
                "snapshot_kind": snapshot_kind,
                "display_expiries": display_expiries,
                "intraday_universe_policy_version": universe.get("policy_version"),
            },
            cards,
            expiry_options,
            detail_value,
            slider_min,
            slider_max,
            slider_value,
            slider_marks,
            slider_disabled,
            slider_disabled,
            slider_disabled,
            slider_disabled,
            slider_disabled,
            slider_disabled,
            _expiry_legend_controls(legend_options, selected_layers),
            legend_contract,
        )
    except Exception:
        legend_contract = _expiry_legend_contract([])
        return (
            None,
            [dbc.Alert("The selected option-chain snapshot could not be rendered.", color="danger")],
            [],
            None,
            0,
            1,
            0,
            {0: "00:00", 1: "Latest"},
            True,
            True,
            True,
            True,
            True,
            True,
            _expiry_legend_controls([], []),
            legend_contract,
        )


@callback(
    Output("brent-vol-history-expiry-layers", "value", allow_duplicate=True),
    Input("brent-vol-history-expiry-layers-reset", "n_clicks"),
    State("brent-vol-history-expiry-layers", "options"),
    prevent_initial_call=True,
)
def reset_expiry_layers(_n_clicks, options):
    return _default_expiry_layers(
        [
            option["value"]
            for option in (options or [])
            if option.get("value")
        ]
    )


@callback(
    Output(
        {"type": "brent-vol-history-expiry-graph", "expiry": ALL},
        "figure",
        allow_duplicate=True,
    ),
    Input("brent-vol-history-expiry-layers", "value"),
    State("brent-vol-history-expiry-layer-manifest", "data"),
    State(
        {"type": "brent-vol-history-expiry-graph", "expiry": ALL},
        "id",
    ),
    prevent_initial_call=True,
)
def update_expiry_layer_visibility(selected_layers, manifest, graph_ids):
    if not graph_ids:
        return []
    manifest = manifest or {}
    selected = set(
        manifest.get("available_layers")
        if selected_layers is None
        else selected_layers
    )
    graph_contracts = manifest.get("graphs") or {}
    updates = []
    for graph_id in graph_ids:
        expiry = str(dict(graph_id or {}).get("expiry") or "")
        graph_contract = graph_contracts.get(expiry) or {}
        patch = Patch()
        for entry in graph_contract.get("traces") or []:
            patch["data"][int(entry["index"])]["visible"] = (
                entry["layer"] in selected
            )
        for entry in graph_contract.get("shapes") or []:
            patch["layout"]["shapes"][int(entry["index"])]["visible"] = (
                entry["layer"] in selected
            )
        updates.append(patch)
    return updates


@callback(
    Output("brent-vol-history-trade-start", "value", allow_duplicate=True),
    Input("brent-vol-history-trade-all", "n_clicks"),
    Input("brent-vol-history-trade-4h", "n_clicks"),
    Input("brent-vol-history-trade-1h", "n_clicks"),
    Input("brent-vol-history-trade-15m", "n_clicks"),
    Input("brent-vol-history-trade-latest", "n_clicks"),
    State("brent-vol-history-trade-start", "max"),
    State("brent-vol-history-snapshot", "data"),
    prevent_initial_call=True,
)
def apply_trade_window_preset(_all, _four_hours, _one_hour, _fifteen_minutes, _latest, maximum, snapshot):
    del _all, _four_hours, _one_hour, _fifteen_minutes, _latest
    if maximum is None or not snapshot or snapshot.get("snapshot_kind") != "INTRADAY":
        return no_update
    preset = ctx.triggered_id
    lookbacks = {
        "brent-vol-history-trade-all": None,
        "brent-vol-history-trade-4h": 4 * 3600,
        "brent-vol-history-trade-1h": 3600,
        "brent-vol-history-trade-15m": 15 * 60,
    }
    if preset in lookbacks:
        lookback = lookbacks[preset]
        return 0 if lookback is None else max(0, int(maximum) - lookback)
    if preset == "brent-vol-history-trade-latest":
        tape = _filter_contract_months(
            load_trade_tape(
                snapshot["snapshot_id"],
                product=_normalize_product(snapshot.get("product")),
                snapshot_kind=snapshot.get("snapshot_kind"),
            ),
            snapshot.get("display_expiries") or [],
        )
        if tape.empty:
            return no_update
        return int(_seconds_since_midnight_gst(tape["trade_at"]).max())
    return no_update


@callback(
    Output({"type": "brent-vol-history-expiry-graph", "expiry": ALL}, "figure"),
    Output("brent-vol-history-trade-grid", "rowData"),
    Output("brent-vol-history-trade-window-state", "data"),
    Input("brent-vol-history-trade-start", "value"),
    Input("brent-vol-history-detail-expiry", "value"),
    Input("brent-vol-history-snapshot", "data"),
    State({"type": "brent-vol-history-expiry-graph", "expiry": ALL}, "figure"),
    State("brent-vol-history-x-axis", "value"),
    State("brent-vol-history-trade-start", "max"),
)
def update_trade_window(start_second, detail_expiry, snapshot_reference, figures, x_axis, maximum):
    if not figures:
        return [], [], no_update
    if not snapshot_reference or snapshot_reference.get("snapshot_kind") != "INTRADAY":
        return (
            [no_update for _ in figures],
            [],
            no_update,
        )
    try:
        tape = _filter_contract_months(
            load_trade_tape(
                snapshot_reference["snapshot_id"],
                product=_normalize_product(snapshot_reference.get("product")),
                snapshot_kind=snapshot_reference.get("snapshot_kind"),
            ),
            snapshot_reference.get("display_expiries") or [],
        )
        filtered = filter_trade_window(tape, start_second)
        figure_updates = []
        for figure in figures:
            expiry_value = dict(dict(figure.get("layout") or {}).get("meta") or {}).get(
                "expiry"
            )
            if not expiry_value:
                figure_updates.append(no_update)
                continue
            payloads = trade_trace_payloads(
                filtered, pd.Timestamp(expiry_value), _normalize_x_axis(x_axis)
            )
            patch = Patch()
            for index, trace in enumerate(figure.get("data") or []):
                meta = trace.get("meta") if isinstance(trace, dict) else None
                if not isinstance(meta, dict) or meta.get("role") != "trade-tape":
                    continue
                payload = payloads[str(meta["put_call"])]
                patch["data"][index]["x"] = payload["x"]
                patch["data"][index]["y"] = payload["y"]
                patch["data"][index]["customdata"] = payload["customdata"]
                patch["data"][index]["marker"]["size"] = payload["size"]
                patch["data"][index]["marker"]["symbol"] = payload["symbol"]
                patch["data"][index]["marker"]["line"]["color"] = payload["line_color"]
                patch["data"][index]["marker"]["line"]["width"] = payload["line_width"]
            figure_updates.append(patch)
        rows = _trade_tape_rows(tape, detail_expiry, start_second)
        return (
            figure_updates,
            rows,
            {
                "business_date": snapshot_reference.get("business_date"),
                "product": _normalize_product(snapshot_reference.get("product")),
                "start": float(start_second or 0),
                "maximum": float(maximum or 0),
            },
        )
    except Exception:
        return (
            [no_update for _ in figures],
            [],
            no_update,
        )


@callback(
    Output("brent-vol-history-grid", "rowData"),
    Input("brent-vol-history-snapshot", "data"),
    Input("brent-vol-history-detail-expiry", "value"),
)
def update_detail_grid(snapshot_reference, expiry_value):
    if not snapshot_reference or not expiry_value:
        return []
    try:
        chain = load_chain_snapshot(
            snapshot_reference["snapshot_id"],
            product=_normalize_product(snapshot_reference.get("product")),
            snapshot_kind=snapshot_reference.get("snapshot_kind"),
        )
        return _detail_rows(chain, expiry_value)
    except Exception:
        return []


__all__ = [
    "build_expiry_figure",
    "build_plot_cards",
    "layout",
    "load_available_snapshots",
    "load_chain_snapshot",
    "load_latest_calibrated_surface",
    "load_published_surface",
    "prepare_market_observations",
    "publication_coverage",
    "published_strike_nodes",
    "render_history",
    "select_history_universe",
    "update_detail_grid",
    "update_history_dates",
]
