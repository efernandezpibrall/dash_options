"""Bloomberg Brent/TFO settlement and intraday option-chain history."""

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
from sqlalchemy import text

from options.calibration_engine.converters.delta import delta_to_strike, strike_to_delta
from options.calibration_engine.io.brent_market import (
    AMERICAN_TREE_STEPS,
    _american_implied_vol,
    prepare_brent_calibration_observations,
)
from options.ttf_volatility import (
    TTFVolatilityError,
    implied_volatility_from_settlement as tfo_implied_volatility,
)
from runtime_config import get_database_engine
from brent_option_chain_refresh import (
    INTRADAY_REQUEST_KIND,
    SETTLEMENT_REQUEST_KIND,
    get_refresh_job,
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
SUPPORTED_PRODUCTS = frozenset({"BRENT", "TFO"})
PRODUCT_SPECS = {
    "BRENT": {
        "label": "Brent",
        "price_unit": "USD/bbl",
        "published_product": "BRENT",
        "intraday_policy_version": "brent-front6-jun-dec-y2-v1",
        "front_count": 6,
        "anchor_months": (6, 12),
        "through_year_offset": 2,
    },
    "TFO": {
        "label": "TFO",
        "price_unit": "EUR/MWh",
        "published_product": "TTF",
        "intraday_policy_version": "tfo-front12-quarterly-y2-v1",
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
        "last_trade_at",
        "last_trade_underlying_at",
    ):
        if column in normalized.columns:
            normalized[column] = pd.to_datetime(normalized[column], errors="coerce")
    for column in (
        "underlying_price",
        "strike",
        "contract_multiplier",
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
    # TFO settlement IV is already governed in the Bloomberg pipeline. The page
    # shows every resolved exchange settlement as a reference and does not apply
    # Brent-specific calibration eligibility rules to it.
    if resolved_product == "TFO":
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
) -> float:
    if _normalize_product(product) == "TFO":
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
    return _american_implied_vol(
        put_call,
        price,
        forward,
        strike,
        time_to_expiry,
        steps=AMERICAN_TREE_STEPS,
    )


def _indicative_last_price_smile(raw: pd.DataFrame) -> pd.DataFrame:
    """Build a non-executable OTM smile from Bloomberg last prices.

    Bloomberg can return a valid LAST_PRICE for an illiquid contract without a
    current bid/ask or trade timestamp.  Such a price must not become an
    executable quote, but it is still useful for calculating an indicative IV
    and retaining that strike's activity on the Delta view.
    """
    columns = [
        "strike",
        "put_call",
        "option_security",
        "last_price",
        "reference_iv",
        "forward",
        "display_delta",
        "delta_source",
        "reference_time",
        "volume",
        "open_interest",
        "open_interest_date",
        "open_interest_scope_status",
    ]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=columns)
    work = raw.copy()
    if str(work.get("snapshot_kind", pd.Series([""])).iloc[0]).upper() != "INTRADAY":
        return pd.DataFrame(columns=columns)

    business_dates = pd.to_datetime(work.get("business_date"), errors="coerce").dropna()
    expirations = pd.to_datetime(
        work.get("option_expiration_date"), errors="coerce"
    ).dropna()
    forward_values = pd.to_numeric(
        work.get("underlying_mid", pd.Series(np.nan, index=work.index)),
        errors="coerce",
    ).where(
        pd.to_numeric(
            work.get("underlying_mid", pd.Series(np.nan, index=work.index)),
            errors="coerce",
        ).gt(0.0),
        pd.to_numeric(
            work.get("underlying_price", pd.Series(np.nan, index=work.index)),
            errors="coerce",
        ),
    ).dropna()
    if business_dates.empty or expirations.empty or forward_values.empty:
        return pd.DataFrame(columns=columns)
    dte = (expirations.iloc[0].normalize() - business_dates.iloc[0].normalize()).days
    forward = float(forward_values.median())
    product = _normalize_product(
        work.get("product", pd.Series([PRODUCT])).iloc[0]
    )
    if dte <= 0 or forward <= 0.0:
        return pd.DataFrame(columns=columns)

    work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
    work["last_price"] = pd.to_numeric(work.get("last_price"), errors="coerce")
    work = work.loc[
        work["strike"].gt(0.0) & work["last_price"].gt(0.0)
    ].copy()
    if work.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for strike, strike_rows in work.groupby("strike", sort=True):
        coordinate_side = _option_side_for_strike(strike, forward)
        candidates = strike_rows.assign(
            _preferred_side=strike_rows["put_call"].astype(str).str.upper().eq(
                coordinate_side
            )
        ).sort_values("_preferred_side", ascending=False)
        for candidate in candidates.itertuples(index=False):
            put_call = str(candidate.put_call).strip().upper()
            if put_call not in {"C", "P"}:
                continue
            implied = _page_implied_volatility(
                product,
                put_call,
                float(candidate.last_price),
                forward,
                float(strike),
                dte / 365.25,
            )
            if not math.isfinite(implied) or implied <= 0.0:
                continue
            last_trade_date = pd.to_datetime(
                getattr(candidate, "last_trade_date", None), errors="coerce"
            )
            rows.append(
                {
                    "strike": float(strike),
                    "put_call": put_call,
                    "option_security": getattr(candidate, "option_security", None),
                    "last_price": float(candidate.last_price),
                    "reference_iv": float(implied),
                    "forward": forward,
                    "display_delta": _delta_x_from_market_inputs(
                        strike=strike,
                        forward=forward,
                        volatility=implied,
                        dte=dte,
                        put_call=coordinate_side,
                    ),
                    "delta_source": "Indicative last price · non-executable",
                    "reference_time": (
                        last_trade_date.strftime("%d %b %Y")
                        if pd.notna(last_trade_date)
                        else "Timestamp unavailable"
                    ),
                    "volume": getattr(candidate, "volume", None),
                    "open_interest": getattr(candidate, "open_interest", None),
                    "open_interest_date": getattr(
                        candidate, "open_interest_date", None
                    ),
                    "open_interest_scope_status": getattr(
                        candidate, "open_interest_scope_status", "unavailable"
                    ),
                }
            )
            break
    return pd.DataFrame(rows, columns=columns)


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
                "display_x": float(display_x),
            }
        )
    selected = pd.DataFrame(rows, columns=columns).sort_values("display_x")
    excluded = pd.DataFrame(exclusions, columns=exclusion_columns)
    return selected, excluded


def _settlement_reference_smile(
    raw: pd.DataFrame,
    x_axis: str,
) -> pd.DataFrame:
    """Return only strict-OTM, price-valid Bloomberg settlement IVs."""
    selected, _ = _settlement_reference_selection(raw, x_axis)
    return selected


def _activity_delta_projection(
    raw: pd.DataFrame,
    indicative_smile: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Map every activity strike to the governed primary smile's delta axis."""
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
    reference_kind = "primary"
    if reference.empty and is_intraday:
        indicative = (
            _indicative_last_price_smile(work)
            if indicative_smile is None
            else indicative_smile.copy()
        )
        if not indicative.empty:
            reference = (
                indicative[["strike", "reference_iv"]]
                .rename(columns={"reference_iv": "_primary_iv"})
                .dropna()
                .sort_values("strike")
            )
            reference_kind = "indicative_last_price"
    if reference.empty:
        return pd.DataFrame(columns=["strike", "display_delta", "delta_source"])

    reference_strikes = reference["strike"].to_numpy(dtype=float)
    reference_vols = reference["_primary_iv"].to_numpy(dtype=float)
    direct_strikes = set(reference_strikes.tolist())
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

    rows = []
    for strike in sorted(pd.to_numeric(work["strike"], errors="coerce").dropna().unique()):
        strike_value = float(strike)
        reference_iv = float(
            np.interp(strike_value, reference_strikes, reference_vols)
        )
        if reference_kind == "indicative_last_price":
            if strike_value in direct_strikes:
                source = "Indicative last price · non-executable"
            elif reference_strikes[0] < strike_value < reference_strikes[-1]:
                source = "Interpolated indicative last-price smile"
            else:
                source = "Nearest indicative last-price smile wing"
        elif strike_value in direct_strikes:
            source = "Primary smile at strike"
        elif reference_strikes[0] < strike_value < reference_strikes[-1]:
            source = "Interpolated primary smile"
        else:
            source = "Nearest primary smile wing"
        option_type = _option_side_for_strike(strike_value, forward)
        rows.append(
            {
                "strike": strike_value,
                "display_delta": _delta_x_from_market_inputs(
                    strike=strike_value,
                    forward=forward,
                    volatility=reference_iv,
                    dte=dte,
                    put_call=option_type,
                ),
                "delta_source": source,
            }
        )
    return pd.DataFrame(rows)


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
        source = side["future_match_source"].fillna("").astype(str)
        event_times = pd.to_datetime(side["trade_at"], errors="coerce", utc=True)
        most_recent = event_times.eq(latest_at)
        result[put_call] = {
            "x": side["_axis_x"].astype(float).tolist(),
            "y": (100.0 * pd.to_numeric(side["trade_iv"], errors="coerce")).tolist(),
            "customdata": np.column_stack(
                [
                    side["strike"], side["trade_price"], side["trade_size"],
                    side["trade_time_gst"], side["future_match_price"],
                    side["future_match_source"], side["future_match_lag_ms"],
                    side["condition_codes"].fillna("regular"),
                ]
            ).tolist(),
            "size": marker_sizes.astype(float).tolist(),
            "symbol": np.where(source.eq("QUOTE_MID"), "circle", "circle-open").tolist(),
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
                "future_match_source": row.future_match_source,
                "future_match_lag_ms": _numeric_or_none(row.future_match_lag_ms),
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
    product: str | None = None,
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
    underlying_hover_label = "TZT" if resolved_product == "TFO" else "Underlying"
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
    indicative_smile = (
        _indicative_last_price_smile(raw)
        if is_intraday and not executable_mask.any()
        else pd.DataFrame()
    )
    market = (
        prepared.loc[_expiry_mask(prepared, expiry, "expiry")].copy()
        if prepared is not None and not prepared.empty
        else pd.DataFrame()
    )
    published = (
        published_nodes.loc[_expiry_mask(published_nodes, expiry, "contract_date")].copy()
        if published_nodes is not None and not published_nodes.empty
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
    reference_activity = raw.assign(
        _reference_volume=pd.to_numeric(
            raw.get("source_volume", raw.get("volume")), errors="coerce"
        ),
        _reference_open_interest=pd.to_numeric(
            raw.get("source_open_interest", raw.get("open_interest")),
            errors="coerce",
        ),
    )
    indicative_reference_strikes = set(
        pd.to_numeric(
            reference_activity.loc[
                reference_activity[
                    ["_reference_volume", "_reference_open_interest"]
                ].notna().any(axis=1),
                "strike",
            ],
            errors="coerce",
        ).dropna()
    )
    indicative_plot = (
        indicative_smile.loc[
            pd.to_numeric(indicative_smile["strike"], errors="coerce").isin(
                indicative_reference_strikes
            )
        ].copy()
        if not indicative_smile.empty
        else pd.DataFrame()
    )
    if x_axis == X_AXIS_DELTA:
        activity = activity.merge(
            _activity_delta_projection(raw, indicative_smile),
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
        if executable.empty and not indicative_plot.empty:
            indicative_plot["_axis_x"] = (
                indicative_plot["display_delta"]
                if x_axis == X_AXIS_DELTA
                else indicative_plot["strike"]
            )
            indicative_plot = indicative_plot.loc[
                pd.to_numeric(indicative_plot["_axis_x"], errors="coerce").notna()
            ].sort_values("_axis_x")
            figure.add_trace(
                go.Scatter(
                    x=indicative_plot["_axis_x"],
                    y=100.0 * indicative_plot["reference_iv"],
                    mode="markers+lines",
                    name="Indicative last-price IV",
                    legendrank=25,
                    legendgroup="indicative-last-price",
                    line={"color": "#A16207", "width": 1.4, "dash": "dot"},
                    marker={
                        "color": "#FEF3C7",
                        "line": {"color": "#A16207", "width": 1.2},
                        "size": 6,
                        "symbol": "circle",
                    },
                    customdata=np.column_stack(
                        [
                            indicative_plot["strike"],
                            indicative_plot["option_security"],
                            indicative_plot["last_price"],
                            indicative_plot["forward"],
                            indicative_plot["reference_time"],
                            _hover_count_strings(indicative_plot["volume"]),
                            _hover_count_strings(indicative_plot["open_interest"]),
                            indicative_plot["put_call"].map(
                                {"C": "Call", "P": "Put"}
                            ),
                            _hover_date_strings(
                                indicative_plot["open_interest_date"]
                            ),
                            _hover_oi_status_strings(
                                indicative_plot["open_interest_scope_status"]
                            ),
                        ]
                    ),
                    hovertemplate=(
                        "<b>Indicative last · %{customdata[7]}</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[1]} · Last %{customdata[2]:.4f}"
                        f" · {underlying_hover_label} %{{customdata[3]:.3f}}"
                        "<br>Volume / OI <b>%{customdata[5]} / %{customdata[6]}</b>"
                        " · OI %{customdata[8]} (%{customdata[9]})"
                        "<br>%{customdata[4]} · Non-executable<extra></extra>"
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
                        meta={"role": "trade-tape", "put_call": put_call},
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
                            "<br>Condition %{customdata[7]}<extra></extra>"
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
                        marker={"color": "#F97316", "size": 9, "symbol": "diamond"},
                        customdata=np.column_stack(
                            [
                                matched_trades["strike"],
                                matched_trades["last_trade_price"],
                                matched_trades["trade_time_gst"],
                                matched_trades["last_trade_underlying_price"],
                                matched_trades["last_trade_underlying_source"],
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
        if not market.empty and not settlement_reference.empty:
            premium_lookup = settlement_reference[
                ["strike", "option_security", "settlement_price"]
            ].rename(
                columns={
                    "option_security": "premium_option_security",
                    "settlement_price": "option_premium",
                }
            )
            market["strike"] = pd.to_numeric(market["strike"], errors="coerce")
            premium_lookup["strike"] = pd.to_numeric(
                premium_lookup["strike"], errors="coerce"
            )
            market = market.merge(
                premium_lookup,
                how="left",
                on="strike",
                validate="many_to_one",
            )
        if not settlement_reference.empty:
            figure.add_trace(
                go.Scatter(
                    x=settlement_reference["display_x"],
                    y=100.0 * settlement_reference["reference_iv"],
                    mode="markers+lines",
                    name="Bloomberg settlement IV",
                    legendrank=5,
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
                        ]
                    ),
                    hovertemplate=(
                        "<b>Bloomberg settlement IV</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[1]} · Premium <b>%{customdata[2]:.4f} "
                        + spec["price_unit"]
                        + "</b>"
                        f" · {underlying_hover_label} %{{customdata[5]:.3f}}"
                        "<br>Volume / OI <b>%{customdata[3]} / %{customdata[4]}</b>"
                        "<extra></extra>"
                    ),
                ),
                secondary_y=False,
            )
        for column, default in {
            "strike": np.nan,
            "delta": np.nan,
            "forward": np.nan,
            "calibration_eligible": False,
            "iv": np.nan,
            "volume": np.nan,
            "open_interest": np.nan,
            "exclusion_reason": "",
            "premium_option_security": "Option",
            "option_premium": np.nan,
        }.items():
            if column not in market.columns:
                market[column] = default
        if x_axis == X_AXIS_DELTA:
            market["_axis_x"] = market.apply(
                lambda row: _display_delta(
                    row.get("delta"),
                    _option_side_for_strike(row.get("strike"), row.get("forward")),
                ),
                axis=1,
            )
        else:
            market["_axis_x"] = pd.to_numeric(market["strike"], errors="coerce")
        market = market.loc[market["_axis_x"].notna()].sort_values("_axis_x")
        eligible = market[market["calibration_eligible"].fillna(False)]
        excluded = market[~market["calibration_eligible"].fillna(False)]
        if not eligible.empty:
            figure.add_trace(
                go.Scatter(
                    x=eligible["_axis_x"],
                    y=100.0 * eligible["iv"],
                    mode="markers+lines",
                    name="Settlement eligible",
                    legendrank=10,
                    marker={"color": "#2563EB", "size": 7, "symbol": "circle"},
                    line={"color": "#93C5FD", "width": 1},
                    customdata=np.column_stack(
                        [
                            eligible["strike"],
                            _hover_count_strings(eligible["volume"]),
                            _hover_count_strings(eligible["open_interest"]),
                            eligible["forward"],
                            eligible["premium_option_security"].fillna("Option"),
                            _hover_price_strings(eligible["option_premium"]),
                        ]
                    ),
                    hovertemplate=(
                        "<b>Calibration eligible</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[4]} · Premium <b>%{customdata[5]} "
                        + spec["price_unit"]
                        + "</b>"
                        f"<br>{underlying_hover_label} %{{customdata[3]:.3f}}"
                        " · Volume / OI <b>%{customdata[1]} / %{customdata[2]}</b>"
                        "<extra></extra>"
                    ),
                ),
                secondary_y=False,
            )
        if not excluded.empty:
            figure.add_trace(
                go.Scatter(
                    x=excluded["_axis_x"],
                    y=100.0 * excluded["iv"],
                    mode="markers",
                    name="Settlement excluded",
                    legendrank=20,
                    marker={"color": "#6B7280", "size": 7, "symbol": "x"},
                    customdata=np.column_stack(
                        [
                            excluded["strike"],
                            excluded["exclusion_reason"],
                            excluded["forward"],
                            _hover_count_strings(excluded["volume"]),
                            _hover_count_strings(excluded["open_interest"]),
                            excluded["premium_option_security"].fillna("Option"),
                            _hover_price_strings(excluded["option_premium"]),
                        ]
                    ),
                    hovertemplate=(
                        "<b>Calibration excluded</b>"
                        "<br>Strike %{customdata[0]:.2f}"
                        + axis_hover
                        + " · IV <b>%{y:.2f}%</b>"
                        "<br>%{customdata[5]} · Premium <b>%{customdata[6]} "
                        + spec["price_unit"]
                        + "</b>"
                        f"<br>{underlying_hover_label} %{{customdata[2]:.3f}}"
                        " · Volume / OI <b>%{customdata[3]} / %{customdata[4]}</b>"
                        "<br>%{customdata[1]}<extra></extra>"
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
                name=(
                    "Published TTF exact COB"
                    if resolved_product == "TFO"
                    else "Published Brent exact COB"
                ),
                legendrank=30,
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
                    (
                        "<b>Published TTF exact COB</b>"
                        if resolved_product == "TFO"
                        else "<b>Published Brent exact COB</b>"
                    )
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
        )

    focus_strikes = []
    if not executable.empty:
        focus_strikes.extend(
            pd.to_numeric(executable["strike"], errors="coerce").dropna()
        )
    if not indicative_plot.empty:
        focus_strikes.extend(
            pd.to_numeric(indicative_plot["strike"], errors="coerce").dropna()
        )
    if not matched_trades.empty:
        focus_strikes.extend(
            pd.to_numeric(matched_trades["strike"], errors="coerce").dropna()
        )
    if not market.empty:
        focus_strikes.extend(pd.to_numeric(market["strike"], errors="coerce").dropna())
    if not published.empty:
        focus_strikes.extend(
            pd.to_numeric(published["strike"], errors="coerce").dropna()
        )
    if resolved_product == "TFO" and not settlement_reference.empty:
        # Bloomberg publishes a materially wider FJS settlement chain than the
        # liquid smile. Settlement panels are an exchange-record view, so their
        # initial range must include every price-valid published strike rather
        # than applying a trader-facing moneyness window.
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
    figure.update_layout(
        template="plotly_white",
        height=440,
        margin={"l": 58, "r": 62, "t": 18, "b": 48},
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
        meta={"expiry": expiry.date().isoformat()},
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
    pricing_future_label = (
        "TZT" if _normalize_product(product) == "TFO" else "underlying"
    )
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
    product: str | None = None,
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
    prepared = prepare_market_observations(chain, product=resolved_product)
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
            product=resolved_product,
        )
        label = expiry.strftime("%b-%y")
        cards.append(
            html.Section(
                [
                    html.H3(label, className="brent-vol-history-card-title"),
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
                "last_trade_underlying_source": getattr(
                    row, "last_trade_underlying_source", None
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


DETAIL_COLUMN_DEFS = [
    {"headerName": "Bloomberg security", "field": "option_security", "pinned": "left", "minWidth": 190},
    {"headerName": "Global ID", "field": "option_global_id", "minWidth": 145},
    {"headerName": "Option underlier", "field": "native_option_underlier", "minWidth": 150},
    {"headerName": "Pricing future", "field": "pricing_future", "minWidth": 142},
    {"headerName": "P/C", "field": "put_call", "width": 68},
    {"headerName": "Strike", "field": "strike", "type": "rightAligned", "width": 92,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(3)"}},
    {"headerName": "Settlement", "field": "settlement_price", "type": "rightAligned", "width": 104,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Latest price", "field": "last_price", "type": "rightAligned", "width": 104,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Option bid", "field": "option_bid", "type": "rightAligned", "width": 96,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Option mid", "field": "option_mid", "type": "rightAligned", "width": 96,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Option ask", "field": "option_ask", "type": "rightAligned", "width": 96,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Spread", "field": "option_spread", "type": "rightAligned", "width": 90,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Spread %", "field": "option_spread_pct", "type": "rightAligned", "width": 92,
     "valueFormatter": {"function": "params.value == null ? '—' : (100 * Number(params.value)).toFixed(2)"}},
    {"headerName": "Future bid", "field": "underlying_bid", "type": "rightAligned", "width": 96,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Future mid", "field": "underlying_mid", "type": "rightAligned", "width": 96,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Future ask", "field": "underlying_ask", "type": "rightAligned", "width": 96,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Future spread", "field": "underlying_spread", "type": "rightAligned", "width": 112,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Batch", "field": "quote_batch_id", "type": "rightAligned", "width": 78},
    {"headerName": "Capture ms", "field": "quote_capture_skew_ms", "type": "rightAligned", "width": 104},
    {"headerName": "Request start", "field": "quote_request_started_at", "minWidth": 190},
    {"headerName": "Response", "field": "quote_response_at", "minWidth": 190},
    {"headerName": "Exec bid IV %", "field": "executable_iv_bid_pct", "type": "rightAligned", "width": 112,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Exec mid IV %", "field": "executable_iv_mid_pct", "type": "rightAligned", "width": 112,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Exec ask IV %", "field": "executable_iv_ask_pct", "type": "rightAligned", "width": 112,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Exec status", "field": "executable_iv_status", "minWidth": 116},
    {"headerName": "Exec exclusion", "field": "executable_iv_exclusion_reason", "minWidth": 250},
    {"headerName": "IV (%)", "field": "implied_volatility_pct", "type": "rightAligned", "width": 90,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Volume", "field": "volume", "type": "rightAligned", "width": 96,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toLocaleString()"}},
    {"headerName": "Volume session", "field": "volume_scope_status", "minWidth": 176},
    {"headerName": "New volume", "field": "volume_delta", "type": "rightAligned", "width": 104,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toLocaleString()"}},
    {"headerName": "Delta status", "field": "volume_delta_status", "minWidth": 150},
    {"headerName": "OI used", "field": "open_interest", "type": "rightAligned", "width": 104,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toLocaleString()"}},
    {"headerName": "OI source", "field": "open_interest_source", "minWidth": 210},
    {"headerName": "OI session", "field": "open_interest_scope_status", "minWidth": 176},
    {"headerName": "OI used date", "field": "open_interest_date", "width": 112},
    {"headerName": "Intraday OI", "field": "intraday_open_interest", "type": "rightAligned", "width": 112,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toLocaleString()"}},
    {"headerName": "Intraday OI date", "field": "intraday_open_interest_date", "width": 136},
    {"headerName": "Settlement OI", "field": "settlement_open_interest", "type": "rightAligned", "width": 120,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toLocaleString()"}},
    {"headerName": "Settlement OI date", "field": "settlement_open_interest_date", "width": 144},
    {"headerName": "Last trade", "field": "last_trade_date", "width": 104},
    {"headerName": "Exact trade px", "field": "last_trade_price_exact", "type": "rightAligned", "width": 116,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Exact trade GST", "field": "last_trade_at_gst", "minWidth": 190},
    {"headerName": "Matched future", "field": "last_trade_underlying_price", "type": "rightAligned", "width": 120,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Future event GST", "field": "last_trade_underlying_at_gst", "minWidth": 190},
    {"headerName": "Match source", "field": "last_trade_underlying_source", "width": 118},
    {"headerName": "Match lag ms", "field": "last_trade_match_lag_ms", "type": "rightAligned", "width": 112},
    {"headerName": "Trade condition", "field": "last_trade_condition_codes", "minWidth": 132},
    {"headerName": "Trade IV %", "field": "last_trade_iv_pct", "type": "rightAligned", "width": 104,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Trade IV status", "field": "last_trade_iv_status", "minWidth": 126},
    {"headerName": "Trade IV exclusion", "field": "last_trade_iv_exclusion_reason", "minWidth": 250},
    {"headerName": "Trade reused", "field": "last_trade_reused", "width": 108},
    {"headerName": "Underlying", "field": "underlying_price", "type": "rightAligned", "width": 104,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(3)"}},
    {"headerName": "Option expiry", "field": "option_expiration_date", "width": 112},
    {"headerName": "Discovery", "field": "discovery_method", "minWidth": 132},
    {"headerName": "IV status", "field": "iv_status", "minWidth": 118},
    {"headerName": "Smile eligible", "field": "smile_eligible", "width": 116},
    {"headerName": "Exclusion reason", "field": "exclusion_reason", "minWidth": 260, "flex": 1},
]

TRADE_TAPE_COLUMN_DEFS = [
    {"headerName": "Trade time GST", "field": "trade_time_gst", "pinned": "left", "width": 132},
    {"headerName": "Bloomberg security", "field": "option_security", "pinned": "left", "minWidth": 190},
    {"headerName": "P/C", "field": "put_call", "width": 68},
    {"headerName": "Strike", "field": "strike", "type": "rightAligned", "width": 92,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(3)"}},
    {"headerName": "Trade price", "field": "trade_price", "type": "rightAligned", "width": 104,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Size", "field": "trade_size", "type": "rightAligned", "width": 84,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toLocaleString()"}},
    {"headerName": "Condition", "field": "condition_codes", "minWidth": 112},
    {"headerName": "Matched future", "field": "future_match_price", "type": "rightAligned", "width": 118,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "Match source", "field": "future_match_source", "width": 112},
    {"headerName": "Lag ms", "field": "future_match_lag_ms", "type": "rightAligned", "width": 86},
    {"headerName": "Trade IV %", "field": "trade_iv_pct", "type": "rightAligned", "width": 104,
     "valueFormatter": {"function": "params.value == null ? '—' : Number(params.value).toFixed(4)"}},
    {"headerName": "IV status", "field": "trade_iv_status", "width": 104},
    {"headerName": "Exclusion reason", "field": "trade_iv_exclusion_reason", "minWidth": 250, "flex": 1},
]


def _expiry_legend_item(label: str, swatch_class: str):
    return html.Span(
        [
            html.Span(
                className=f"brent-vol-history-legend-swatch {swatch_class}",
                **{"aria-hidden": "true"},
            ),
            html.Span(label),
        ],
        className="brent-vol-history-legend-item",
        role="listitem",
    )


def _expiry_legend_items(
    snapshot_kind: str | None = None,
    product: str = PRODUCT,
):
    normalized_kind = str(snapshot_kind or "").upper()
    resolved_product = _normalize_product(product)
    open_interest_label = (
        "Intraday OI · Bloomberg effective date"
        if normalized_kind == "INTRADAY"
        else (
            "Settlement OI · official close"
            if normalized_kind == "SETTLEMENT"
            else "Open interest · follows selected snapshot"
        )
    )
    items = [
        _expiry_legend_item("Calls IV", "brent-vol-history-legend-calls"),
        _expiry_legend_item("Puts IV", "brent-vol-history-legend-puts"),
        _expiry_legend_item(
            "Executable band", "brent-vol-history-legend-executable-band"
        ),
        _expiry_legend_item(
            "Indicative last IV", "brent-vol-history-legend-indicative-last"
        ),
        _expiry_legend_item(
            "Trade-time IV", "brent-vol-history-legend-trade"
        ),
        _expiry_legend_item(
            "Bloomberg settlement IV",
            "brent-vol-history-legend-settlement-reference",
        ),
        _expiry_legend_item(
            (
                "Published TTF exact COB"
                if resolved_product == "TFO"
                else "Published Brent exact COB"
            ),
            "brent-vol-history-legend-published",
        ),
        _expiry_legend_item("Volume", "brent-vol-history-legend-volume"),
        _expiry_legend_item(
            open_interest_label, "brent-vol-history-legend-open-interest"
        ),
        _expiry_legend_item(
            "New volume", "brent-vol-history-legend-new-volume"
        ),
    ]
    if resolved_product == "BRENT":
        items[6:6] = [
            _expiry_legend_item(
                "Settlement eligible", "brent-vol-history-legend-eligible"
            ),
            _expiry_legend_item(
                "Settlement excluded", "brent-vol-history-legend-excluded"
            ),
        ]
    return items


def build_expiry_legend(
    snapshot_kind: str | None = None,
    product: str = PRODUCT,
):
    """Return the shared visual key for every expiry chart in the section."""
    return html.Div(
        _expiry_legend_items(snapshot_kind, product),
        id="brent-vol-history-expiry-legend",
        className="brent-vol-history-common-legend",
        role="list",
        **{"aria-label": "Common legend for all expiry charts"},
    )


def _oi_methodology_children(product: str):
    if _normalize_product(product) == "TFO":
        title = "ICE Endex open-interest timing"
        copy = (
            "Official TFO open interest can be published after the option settlement. "
            "This page keeps the Bloomberg effective date, marks earlier observations "
            "as stale, and never carries settlement OI into the intraday view. Delayed "
            "OI publication does not change the recorded option or TZT futures settlement."
        )
        link_label = "ICE Dutch TTF Natural Gas options"
        href = "https://www.ice.com/products/71085679/Dutch-TTF-Natural-Gas-Options"
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
                                        ),
                                        html.Button(
                                            "4h",
                                            id="brent-vol-history-trade-4h",
                                            n_clicks=0,
                                        ),
                                        html.Button(
                                            "1h",
                                            id="brent-vol-history-trade-1h",
                                            n_clicks=0,
                                        ),
                                        html.Button(
                                            "15m",
                                            id="brent-vol-history-trade-15m",
                                            n_clicks=0,
                                        ),
                                        html.Button(
                                            "Latest print",
                                            id="brent-vol-history-trade-latest",
                                            n_clicks=0,
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
        dcc.Store(id="brent-vol-history-refresh-job", storage_type="session"),
        dcc.Store(id="brent-vol-history-refresh-completion", storage_type="session"),
        dcc.Interval(
            id="brent-vol-history-refresh-poll",
            interval=1000,
            n_intervals=0,
            disabled=True,
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
                html.H2("Exact trade tape"),
                dag.AgGrid(
                    id="brent-vol-history-trade-grid",
                    rowData=[],
                    columnDefs=TRADE_TAPE_COLUMN_DEFS,
                    defaultColDef={"sortable": True, "filter": True, "resizable": True},
                    dashGridOptions={
                        "rowHeight": 28,
                        "headerHeight": 34,
                        "pagination": True,
                        "paginationPageSize": 50,
                        "enableCellTextSelection": True,
                        "animateRows": False,
                        "getRowId": {"function": "params.data.event_id"},
                        "ariaLabel": "Bloomberg exact option trade tape",
                    },
                    className="ag-theme-alpine mckinsey-ag-grid brent-vol-history-grid",
                    style={"width": "100%", "height": "360px"},
                    dangerously_allow_code=True,
                ),
            ],
            className="brent-vol-history-section",
        ),
        html.Section(
            [
                html.Div(
                    [
                        html.H2("Option-chain detail"),
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
                    className="brent-vol-history-detail-header",
                ),
                dag.AgGrid(
                    id="brent-vol-history-grid",
                    rowData=[],
                    columnDefs=DETAIL_COLUMN_DEFS,
                    defaultColDef={
                        "sortable": True,
                        "filter": True,
                        "resizable": True,
                        "suppressHeaderMenuButton": False,
                    },
                    dashGridOptions={
                        "rowHeight": 28,
                        "headerHeight": 34,
                        "pagination": True,
                        "paginationPageSize": 50,
                        "enableCellTextSelection": True,
                        "animateRows": False,
                        "getRowId": {"function": "params.data.option_security"},
                        "ariaLabel": "Bloomberg option-chain detail",
                    },
                    className="ag-theme-alpine mckinsey-ag-grid brent-vol-history-grid",
                    style={"width": "100%", "height": "560px"},
                    dangerously_allow_code=True,
                ),
            ],
            className="brent-vol-history-section",
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
    Input("brent-vol-history-product", "value"),
    State("brent-vol-history-refresh-job", "data"),
    State("brent-vol-history-refresh-completion", "data"),
)
def manage_bloomberg_refresh(
    _refresh_clicks,
    _settlement_refresh_clicks,
    _poll_count,
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
        return (
            updated_jobs,
            updated_completions,
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
        triggered = ctx.triggered_id
    except Exception:
        triggered = None
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
                return response(
                    job.as_dict(),
                    completion,
                    True,
                    idle_disabled[0],
                    idle_disabled[1],
                    message,
                    status_class,
                    intraday_style,
                    settlement_style,
                )
            if job.status == "failed":
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
                    idle_disabled[0],
                    idle_disabled[1],
                    failure_message,
                    "brent-vol-history-refresh-status brent-vol-history-refresh-status-danger",
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
        return response(
            None,
            current_completion,
            True,
            idle_disabled[0],
            idle_disabled[1],
            "",
            "brent-vol-history-refresh-status",
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
    Output("brent-vol-history-expiry-legend", "children"),
    Input("brent-vol-history-date", "value"),
    Input("brent-vol-history-x-axis", "value"),
    Input("brent-vol-history-product", "value"),
    State("brent-vol-history-detail-expiry", "value"),
    State("brent-vol-history-trade-window-state", "data"),
)
def render_history(
    selected_snapshot_id,
    x_axis=X_AXIS_STRIKE,
    product=PRODUCT,
    current_detail_expiry=None,
    current_trade_window=None,
):
    x_axis = _normalize_x_axis(x_axis)
    product = _normalize_product(product)
    if not selected_snapshot_id:
        return (
            None,
            build_plot_cards(
                pd.DataFrame(), pd.DataFrame(), x_axis=x_axis, product=product
            ),
            [],
            None,
            0,
            1,
            0,
            {0: "00:00", 1: "Latest"},
            True,
            _expiry_legend_items(product=product),
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
        published = (
            pd.DataFrame()
            if snapshot_kind == "INTRADAY"
            else load_published_surface(
                selected_date,
                product=product,
                snapshot_kind=snapshot_kind,
            )
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
        return (
            {
                "snapshot_id": snapshot_id,
                "product": product,
                "business_date": selected_date,
                "snapshot_kind": snapshot_kind,
                "display_expiries": display_expiries,
                "intraday_universe_policy_version": universe.get("policy_version"),
            },
            build_plot_cards(
                chain, published, x_axis=x_axis,
                trade_tape=(trade_tape if snapshot_kind == "INTRADAY" else None),
                product=product,
            ),
            expiry_options,
            detail_value,
            slider_min,
            slider_max,
            slider_value,
            slider_marks,
            slider_disabled,
            _expiry_legend_items(snapshot_kind, product),
        )
    except Exception:
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
            _expiry_legend_items(product=product),
        )


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
    "load_published_surface",
    "prepare_market_observations",
    "publication_coverage",
    "published_strike_nodes",
    "render_history",
    "select_history_universe",
    "update_detail_grid",
    "update_history_dates",
]
