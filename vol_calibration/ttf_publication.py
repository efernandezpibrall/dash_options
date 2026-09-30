"""Immutable publication service for governed PCHIP/Wing gas surfaces."""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
from io import StringIO
import json
import logging
import os
from time import perf_counter
from typing import Iterable, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd
from sqlalchemy import inspect, text

from vol_calibration.auth import Identity, Permission, authorize
from vol_calibration.calibration_inputs import TTF_CALL_DELTA_NODES
from vol_calibration.ttf_hybrid_surface import (
    GAS_HYBRID_METHOD,
    GAS_HYBRID_POLICY_VERSIONS,
    TTF_HYBRID_METHOD,
    TTF_HYBRID_POLICY_VERSION,
)
from options.brent_single_surface import BRENT_SINGLE_SURFACE_POLICY_VERSION
from options.hh_single_surface import HH_SINGLE_SURFACE_POLICY_VERSION


PUBLICATION_TABLE = "at_lng.vol_surface_publications"
SURFACE_TABLE = "at_lng.implied_volatility_surface_calibrated"
RUN_TABLE = "at_lng.vol_calibration_runs"
RESULT_TABLE = "at_lng.vol_calibration_expiry_results"
AUDIT_TABLE = "at_lng.vol_calibration_audit_events"
RUN_TRADE_TABLE = "at_lng.vol_calibration_run_trade_inputs"
PUBLICATION_ENGINE_VERSION = "ttf-quote-preserving-core-wing-v2"
JKM_HYBRID_POLICY_VERSION = "jkm_pchip_core_wing_tail_hybrid_v1"
_HYBRID_PUBLICATION_POLICIES = {
    "BRENT": {
        "method": "single_svi_actual_strikes",
        "policy_version": BRENT_SINGLE_SURFACE_POLICY_VERSION,
        "engine_version": BRENT_SINGLE_SURFACE_POLICY_VERSION,
    },
    "TTF": {
        "method": TTF_HYBRID_METHOD,
        "policy_version": TTF_HYBRID_POLICY_VERSION,
        "engine_version": PUBLICATION_ENGINE_VERSION,
    },
    "JKM": {
        "method": GAS_HYBRID_METHOD,
        "policy_version": GAS_HYBRID_POLICY_VERSIONS["JKM"],
        "engine_version": "jkm-pchip-wing-v1",
    },
    "NBP": {
        "method": GAS_HYBRID_METHOD,
        "policy_version": GAS_HYBRID_POLICY_VERSIONS["NBP"],
        "engine_version": "nbp-pchip-wing-v1",
    },
    "HH": {
        "method": "single_svi_seasonal_quotes",
        "policy_version": HH_SINGLE_SURFACE_POLICY_VERSION,
        "engine_version": HH_SINGLE_SURFACE_POLICY_VERSION,
    },
}
_TIMING_LOG = logging.getLogger(__name__)


def _log_publication_timing(enabled, product, stage, seconds, **details):
    if enabled:
        suffix = " ".join(f"{key}={value}" for key, value in details.items())
        _TIMING_LOG.info(
            "calibration_timing product=%s stage=%s seconds=%.6f %s",
            product, stage, seconds, suffix,
        )

_SURFACE_INSERT_COLUMNS = (
    "publication_id",
    "run_id",
    "commodity",
    "cob_date",
    "contract_date",
    "option_expiration_date",
    "strike",
    "delta",
    "put_call",
    "volatility",
    "total_variance",
    "working_forward",
    "surface_region",
    "blend_classification",
    "calibration_basis",
    "source_name",
    "calibration_method",
    "calibration_policy_version",
    "input_fingerprint",
)


class TTFPublicationError(RuntimeError):
    """Raised when a complete governed gas publication cannot be committed."""


HybridPublicationError = TTFPublicationError


def _publication_policy(commodity: str) -> tuple[str, dict]:
    product = str(commodity or "").strip().upper()
    policy = _HYBRID_PUBLICATION_POLICIES.get(product)
    if policy is None:
        raise TTFPublicationError(
            f"Hybrid publication is unsupported for commodity {product!r}."
        )
    return product, policy


def _copy_surface_points(connection, point_rows: list[dict]) -> bool:
    """Use PostgreSQL COPY when the active DBAPI driver exposes it.

    Returning ``False`` means the connection does not support COPY and lets the
    caller retain the portable SQLAlchemy executemany fallback. COPY failures
    themselves propagate so the surrounding publication transaction rolls back.
    """
    sqlalchemy_connection = getattr(connection, "connection", None)
    driver_connection = getattr(sqlalchemy_connection, "driver_connection", None)
    cursor_factory = getattr(driver_connection, "cursor", None)
    if not callable(cursor_factory):
        return False

    cursor = cursor_factory()
    copy_expert = getattr(cursor, "copy_expert", None)
    if not callable(copy_expert):
        cursor.close()
        return False

    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in point_rows:
        writer.writerow(
            [
                r"\N" if row.get(column) is None else row.get(column)
                for column in _SURFACE_INSERT_COLUMNS
            ]
        )
    buffer.seek(0)
    column_sql = ", ".join(_SURFACE_INSERT_COLUMNS)
    copy_sql = (
        f"COPY {SURFACE_TABLE} ({column_sql}) FROM STDIN "
        r"WITH (FORMAT CSV, NULL '\N')"
    )
    try:
        copy_expert(copy_sql, buffer)
    finally:
        cursor.close()
    return True


def _table_available(engine, table_name: str) -> bool:
    try:
        return inspect(engine).has_table(table_name, schema="at_lng")
    except Exception:
        return False


def ttf_publication_storage_available(engine) -> bool:
    required = (
        "vol_calibration_runs",
        "vol_calibration_expiry_results",
        "vol_calibration_audit_events",
        "vol_surface_publications",
        "implied_volatility_surface_calibrated",
    )
    if engine is None:
        return False
    try:
        available = set(inspect(engine).get_table_names(schema="at_lng"))
    except Exception:
        return False
    return all(name in available for name in required)


def _insert_expiry_results(connection, run_id: str, results: list[dict]) -> None:
    """Insert all governed expiry diagnostics in one PostgreSQL round trip."""
    statement = text(
        f"""
        INSERT INTO {RESULT_TABLE}
            (run_id, option_expiration_date, parameters, diagnostics,
             validation, weighted_rmse, unweighted_rmse, max_error,
             optimizer_success)
        VALUES
            (:run_id, :option_expiration_date,
             CAST(:parameters AS jsonb), CAST(:diagnostics AS jsonb),
             CAST(:validation AS jsonb), :weighted_rmse,
             :unweighted_rmse, :max_error, :optimizer_success)
        """
    )
    rows = [
        {
            "run_id": run_id,
            "option_expiration_date": pd.Timestamp(
                item["option_expiration_date"]
            ).date(),
            "parameters": json.dumps(
                _json_ready(item.get("parameters") or {}), sort_keys=True
            ),
            "diagnostics": json.dumps(
                _json_ready(item.get("diagnostics") or {}), sort_keys=True
            ),
            "validation": json.dumps(
                _json_ready(item.get("validation") or {}), sort_keys=True
            ),
            "weighted_rmse": item.get("weighted_rmse"),
            "unweighted_rmse": item.get("unweighted_rmse"),
            "max_error": item.get("max_error"),
            "optimizer_success": bool(item.get("optimizer_success", True)),
        }
        for item in results
    ]
    if not rows:
        return
    driver_connection = getattr(
        getattr(connection, "connection", None), "driver_connection", None
    )
    cursor_factory = getattr(driver_connection, "cursor", None)
    if callable(cursor_factory):
        cursor = cursor_factory()
        try:
            if callable(getattr(cursor, "mogrify", None)):
                from psycopg2.extras import execute_values

                values = [
                    tuple(row[column] for column in (
                        "run_id", "option_expiration_date", "parameters",
                        "diagnostics", "validation", "weighted_rmse",
                        "unweighted_rmse", "max_error", "optimizer_success",
                    ))
                    for row in rows
                ]
                execute_values(
                    cursor,
                    f"INSERT INTO {RESULT_TABLE} "
                    "(run_id, option_expiration_date, parameters, diagnostics, "
                    "validation, weighted_rmse, unweighted_rmse, max_error, "
                    "optimizer_success) VALUES %s",
                    values,
                    template=(
                        "(%s::uuid, %s, %s::jsonb, %s::jsonb, %s::jsonb, "
                        "%s, %s, %s, %s)"
                    ),
                    page_size=len(values),
                )
                return
        finally:
            cursor.close()
    connection.execute(statement, rows)


def _as_of_cutoff(trading_date, now: datetime | None = None) -> datetime:
    selected = pd.Timestamp(trading_date).date()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if selected >= current.date():
        return current
    return datetime.combine(selected, time.max, tzinfo=timezone.utc)


def empty_publication_payload(
    trading_date,
    *,
    error: str | None = None,
    commodity: str = "TTF",
) -> dict:
    product, policy = _publication_policy(commodity)
    selected = pd.to_datetime(trading_date, errors="coerce")
    return {
        "publication_id": None,
        "run_id": None,
        "trading_date": selected.date().isoformat() if not pd.isna(selected) else None,
        "publication_date": None,
        "settlement_cob": None,
        "base_publication_id": None,
        "published_at": None,
        "published_by": None,
        "row_count": 0,
        "expiry_count": 0,
        "source": SURFACE_TABLE,
        "commodity": product,
        "method": policy["method"],
        "policy_version": policy["policy_version"],
        "expiry_results": [],
        "data": pd.DataFrame().to_json(orient="split"),
        "error": error,
    }


def load_latest_ttf_publication(
    engine,
    trading_date,
    *,
    as_of: datetime | None = None,
    publication_id: str | None = None,
    prefer_exact_cob: bool = False,
    require_exact_cob: bool = False,
    commodity: str = "TTF",
) -> dict:
    """Load one complete hybrid publication, normally without look-ahead.

    Calibration review may opt into the active revision for the exact selected
    COB even when that revision was approved later.  Older fallback revisions
    remain subject to the normal point-in-time cutoff. require_exact_cob selects
    only that date's active publication, with no older-date fallback.
    """
    product, policy = _publication_policy(commodity)
    if not ttf_publication_storage_available(engine):
        return empty_publication_payload(
            trading_date,
            error=f"{product} publication storage is not migrated.",
            commodity=product,
        )
    cutoff = _as_of_cutoff(trading_date, as_of)
    if publication_id is None:
        if require_exact_cob:
            publication_filter = "p.cob_date = :trading_date"
            publication_order = "p.published_at DESC, p.created_at DESC"
        elif prefer_exact_cob:
            publication_filter = (
                "(p.cob_date = :trading_date OR "
                "(p.cob_date < :trading_date AND p.published_at <= :as_of))"
            )
            publication_order = (
                "CASE WHEN p.cob_date = :trading_date THEN 0 ELSE 1 END, "
                "p.published_at DESC, p.created_at DESC"
            )
        else:
            publication_filter = (
                "p.cob_date <= :trading_date AND p.published_at <= :as_of"
            )
            publication_order = "p.published_at DESC, p.created_at DESC"
        publication_params = {
            "trading_date": pd.Timestamp(trading_date).date(),
            "as_of": cutoff,
        }
    else:
        # Publication writes are verified by immutable ID.  A historical COB
        # may legitimately be published later, so applying the normal
        # point-in-time cutoff here would hide the row that was just committed.
        publication_filter = "p.publication_id = CAST(:publication_id AS uuid)"
        publication_params = {"publication_id": str(publication_id)}
        publication_order = "p.published_at DESC, p.created_at DESC"
    publication_query = text(
        f"""
        SELECT p.publication_id, p.run_id, p.cob_date, p.published_at,
               p.published_by, r.configuration
        FROM {PUBLICATION_TABLE} p
        JOIN {RUN_TABLE} r ON r.run_id = p.run_id
        WHERE p.commodity = '{product}' AND p.status = 'published' AND p.is_active
          AND {publication_filter}
        ORDER BY {publication_order}
        LIMIT 1
        """
    )
    with engine.connect() as connection:
        publication = connection.execute(
            publication_query,
            publication_params,
        ).mappings().first()
        if publication is None:
            return empty_publication_payload(trading_date, commodity=product)
        points = pd.read_sql(
            text(
                f"""
                SELECT surface_point_id, publication_id, run_id, commodity,
                       cob_date, contract_date, option_expiration_date, strike,
                       delta, put_call, volatility, total_variance, working_forward,
                       surface_region, blend_classification,
                       calibration_basis, source_name, calibration_method,
                       calibration_policy_version, input_fingerprint, created_at
                FROM {SURFACE_TABLE}
                WHERE publication_id = :publication_id
                ORDER BY contract_date, strike
                """
            ),
            connection,
            params={"publication_id": publication["publication_id"]},
        )
        expiry_results = connection.execute(
            text(
                f"""
                SELECT option_expiration_date, parameters, diagnostics,
                       validation, weighted_rmse, unweighted_rmse, max_error,
                       optimizer_success
                FROM {RESULT_TABLE}
                WHERE run_id = :run_id
                ORDER BY option_expiration_date
                """
            ),
            {"run_id": publication["run_id"]},
        ).mappings().all()
    configuration = publication.get("configuration") or {}
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except json.JSONDecodeError:
            configuration = {}
    for column in (
        "cob_date",
        "contract_date",
        "option_expiration_date",
        "created_at",
    ):
        if column in points.columns:
            points[column] = pd.to_datetime(points[column], errors="coerce")
    for column in ("surface_point_id", "publication_id", "run_id"):
        if column in points.columns:
            points[column] = points[column].astype(str)
    return {
        "publication_id": str(publication["publication_id"]),
        "run_id": str(publication["run_id"]),
        "trading_date": pd.Timestamp(trading_date).date().isoformat(),
        "publication_date": pd.Timestamp(publication["cob_date"]).date().isoformat(),
        "settlement_cob": configuration.get("settlement_cob"),
        "base_publication_id": configuration.get("base_publication_id"),
        "input_manifest_fingerprint": configuration.get(
            "input_manifest_fingerprint"
        ),
        "published_at": pd.Timestamp(publication["published_at"]).isoformat(),
        "published_by": publication["published_by"],
        "row_count": int(len(points)),
        "expiry_count": int(points["contract_date"].nunique()) if not points.empty else 0,
        "source": SURFACE_TABLE,
        "commodity": product,
        "method": configuration.get("method") or policy["method"],
        "policy_version": configuration.get("policy_version") or policy["policy_version"],
        "expiry_results": [_json_ready(dict(item)) for item in expiry_results],
        "data": points.to_json(date_format="iso", orient="split"),
        "error": None,
    }


def _load_persisted_publication_receipt(
    engine, publication_id: str, *, commodity: str
) -> dict:
    """Verify a committed revision without transferring its entire dense grid."""
    product, policy = _publication_policy(commodity)
    query = text(
        f"""
        SELECT p.publication_id, p.run_id, p.cob_date, p.published_at,
               p.published_by,
               r.configuration->>'input_manifest_fingerprint' AS input_manifest_fingerprint,
               r.configuration->>'method' AS calibration_method,
               r.configuration->>'policy_version' AS calibration_policy_version,
               points.row_count, points.expiry_count, points.bad_point_count,
               points.min_fingerprint, points.max_fingerprint,
               month_sizes.min_points_per_month, month_sizes.max_points_per_month,
               results.result_count, results.all_valid
        FROM {PUBLICATION_TABLE} p
        JOIN {RUN_TABLE} r ON r.run_id = p.run_id
        CROSS JOIN LATERAL (
            SELECT COUNT(*) AS row_count,
                   COUNT(DISTINCT contract_date) AS expiry_count,
                   COUNT(*) FILTER (WHERE strike <= 0 OR volatility <= 0
                       OR total_variance <= 0 OR working_forward <= 0
                       OR delta <= 0 OR delta >= 1
                       OR strike IS NULL OR volatility IS NULL
                       OR total_variance IS NULL OR working_forward IS NULL
                       OR delta IS NULL OR input_fingerprint IS NULL)
                       AS bad_point_count,
                   MIN(input_fingerprint) AS min_fingerprint,
                   MAX(input_fingerprint) AS max_fingerprint
            FROM {SURFACE_TABLE} s
            WHERE s.publication_id = p.publication_id
        ) points
        CROSS JOIN LATERAL (
            SELECT MIN(month_count) AS min_points_per_month,
                   MAX(month_count) AS max_points_per_month
            FROM (
                SELECT COUNT(*) AS month_count
                FROM {SURFACE_TABLE} s
                WHERE s.publication_id = p.publication_id
                GROUP BY contract_date
            ) monthly
        ) month_sizes
        CROSS JOIN LATERAL (
            SELECT COUNT(*) AS result_count,
                   BOOL_AND(COALESCE((validation->>'is_valid')::boolean, FALSE))
                       AS all_valid
            FROM {RESULT_TABLE} x
            WHERE x.run_id = p.run_id
        ) results
        WHERE p.publication_id = CAST(:publication_id AS uuid)
          AND p.commodity = :commodity AND p.status = 'published' AND p.is_active
        """
    )
    with engine.connect() as connection:
        row = connection.execute(
            query, {"publication_id": publication_id, "commodity": product}
        ).mappings().one_or_none()
    if row is None:
        raise TTFPublicationError(f"Published {product} revision is unavailable.")
    if (
        int(row["bad_point_count"]) != 0
        or int(row["min_points_per_month"] or 0) != 401
        or int(row["max_points_per_month"] or 0) != 401
        or not row["all_valid"]
    ):
        raise TTFPublicationError(
            f"Published {product} revision failed persisted quality checks."
        )
    return {
        "publication_id": str(row["publication_id"]),
        "run_id": str(row["run_id"]),
        "publication_date": pd.Timestamp(row["cob_date"]).date().isoformat(),
        "published_at": pd.Timestamp(row["published_at"]).isoformat(),
        "published_by": row["published_by"],
        "row_count": int(row["row_count"]),
        "expiry_count": int(row["expiry_count"]),
        "result_count": int(row["result_count"]),
        "input_manifest_fingerprint": row["input_manifest_fingerprint"],
        "point_fingerprint": row["min_fingerprint"]
        if row["min_fingerprint"] == row["max_fingerprint"]
        else None,
        "source": SURFACE_TABLE,
        "commodity": product,
        "method": row["calibration_method"] or policy["method"],
        "policy_version": row["calibration_policy_version"] or policy["policy_version"],
        "surface_loaded": False,
        "data": None,
        "error": None,
    }


def ttf_publication_frame(payload: Mapping | None) -> pd.DataFrame:
    if not payload or not payload.get("data"):
        return pd.DataFrame()
    try:
        return pd.read_json(StringIO(payload["data"]), orient="split")
    except (TypeError, ValueError):
        return pd.DataFrame()


def _json_ready(value):
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if value is pd.NA or (isinstance(value, float) and np.isnan(value)):
        return None
    return value


def normalize_ttf_publication_surface(
    surface: pd.DataFrame,
    *,
    trading_date,
    input_fingerprint: str | None = None,
    commodity: str = "TTF",
) -> pd.DataFrame:
    """Normalize and fail closed on a complete operational-surface frame."""
    product, policy = _publication_policy(commodity)
    if surface is None or surface.empty:
        raise TTFPublicationError(f"A {product} publication cannot be empty.")
    normalized = surface.copy()
    aliases = {
        "expiry": "contract_date",
        "iv": "volatility",
        "core_tail_classification": "surface_region",
    }
    for source, target in aliases.items():
        if target not in normalized.columns and source in normalized.columns:
            normalized[target] = normalized[source]
    required = {
        "contract_date",
        "option_expiration_date",
        "strike",
        "delta",
        "volatility",
        "total_variance",
        "working_forward",
        "surface_region",
        "blend_classification",
        "calibration_basis",
        "source_name",
    }
    missing = sorted(required - set(normalized.columns))
    if missing:
        raise TTFPublicationError(
            f"{product} publication surface is missing: " + ", ".join(missing)
        )
    normalized["contract_date"] = pd.to_datetime(
        normalized["contract_date"], errors="coerce"
    ).dt.normalize()
    normalized["option_expiration_date"] = pd.to_datetime(
        normalized["option_expiration_date"], errors="coerce"
    ).dt.normalize()
    for column in (
        "strike",
        "delta",
        "volatility",
        "total_variance",
        "working_forward",
    ):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if normalized[["contract_date", "option_expiration_date"]].isna().any().any():
        raise TTFPublicationError(
            f"{product} publication dates must be complete and valid."
        )
    numeric = normalized[
        ["strike", "delta", "volatility", "total_variance", "working_forward"]
    ]
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise TTFPublicationError(f"{product} publication coordinates must be finite.")
    if (
        normalized[["strike", "volatility", "total_variance", "working_forward"]]
        <= 0
    ).any().any():
        raise TTFPublicationError(
            f"{product} publication strike, IV, variance, and forward must be positive."
        )
    if not normalized["delta"].between(0, 1, inclusive="neither").all():
        raise TTFPublicationError(
            f"{product} publication deltas must be inside (0, 1)."
        )
    if normalized.duplicated(["contract_date", "strike"]).any():
        raise TTFPublicationError(
            f"{product} publication contains duplicate expiry/strike points."
        )
    if normalized["source_name"].astype(str).str.strip().eq("").any():
        raise TTFPublicationError(
            f"{product} publication source provenance is required."
        )
    if not normalized["calibration_basis"].astype(str).str.lower().isin(
        {"observed", "extrapolated"}
    ).all():
        raise TTFPublicationError(f"{product} publication basis is unsupported.")

    normalized["commodity"] = product
    normalized["cob_date"] = pd.Timestamp(trading_date).normalize()
    normalized["put_call"] = "C"
    normalized["calibration_method"] = policy["method"]
    normalized["calibration_policy_version"] = policy["policy_version"]
    if input_fingerprint is not None:
        normalized["input_fingerprint"] = input_fingerprint
    return normalized.sort_values(["contract_date", "strike"]).reset_index(drop=True)


def ttf_surface_fingerprint(
    surface: pd.DataFrame,
    *,
    commodity: str = "TTF",
) -> str:
    normalized = normalize_ttf_publication_surface(
        surface,
        trading_date=surface.get("cob_date", pd.Series([date.today()])).iloc[0],
        commodity=commodity,
    )
    columns = [
        "contract_date",
        "option_expiration_date",
        "strike",
        "delta",
        "volatility",
        "total_variance",
        "working_forward",
        "surface_region",
        "blend_classification",
        "calibration_basis",
        "source_name",
    ]
    payload = normalized[columns].to_csv(index=False, float_format="%.12g")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def input_manifest_fingerprint(input_manifest: Mapping) -> str:
    """Fingerprint normalized raw inputs, never the generated surface."""

    if not isinstance(input_manifest, Mapping) or not input_manifest:
        raise TTFPublicationError("A complete canonical input manifest is required.")
    payload = json.dumps(
        _json_ready(dict(input_manifest)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit(connection, run_id, event_type, actor, details, from_status=None, to_status=None):
    connection.execute(
        text(
            f"""
            INSERT INTO {AUDIT_TABLE}
                (run_id, event_type, from_status, to_status, actor, details)
            VALUES
                (:run_id, :event_type, :from_status, :to_status, :actor,
                 CAST(:details AS jsonb))
            """
        ),
        {
            "run_id": run_id,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "actor": actor,
            "details": json.dumps(_json_ready(details), sort_keys=True),
        },
    )


def publish_ttf_surface(
    engine,
    surface: pd.DataFrame,
    expiry_results: Iterable[Mapping],
    *,
    trading_date,
    settlement_cob,
    identity: Identity,
    created_by: str,
    base_publication_id: str | None,
    expected_current_publication_id: str | None,
    idempotency_key: str,
    manual_trade_ids: Iterable[str] = (),
    expected_expiries: Iterable | None = None,
    notes: str | None = None,
    commodity: str = "TTF",
    input_manifest: Mapping | None = None,
    return_surface: bool = True,
) -> dict:
    """Atomically supersede and publish one complete hybrid surface revision."""
    timing_enabled = os.getenv("CALIBRATION_TIMING", "").lower() in {
        "1", "true", "yes", "on"
    }
    publication_started = perf_counter() if timing_enabled else 0.0
    product, policy = _publication_policy(commodity)
    # This workflow is explicitly controlled self-publication: the server still
    # requires a publish-capable authenticated identity, but the candidate
    # creator and publisher may intentionally be the same operator.
    authorize(identity, Permission.PUBLISH)
    if not ttf_publication_storage_available(engine):
        raise TTFPublicationError(f"{product} publication storage is not migrated.")
    if not str(idempotency_key or "").strip():
        raise TTFPublicationError("A publication idempotency key is required.")
    trading = pd.Timestamp(trading_date).date()
    settlement = pd.Timestamp(settlement_cob).date()
    if settlement > trading:
        raise TTFPublicationError("Settlement COB cannot be after the trading date.")

    prepared = normalize_ttf_publication_surface(
        surface,
        trading_date=trading,
        commodity=product,
    )
    maturity_counts = prepared.groupby("contract_date").size()
    if not maturity_counts.eq(401).all():
        raise TTFPublicationError(
            f"Every governed {product} maturity must contain exactly 401 dense points."
        )
    missing_anchor_months = []
    for contract_date, group in prepared.groupby("contract_date", sort=True):
        published_delta = group["delta"].to_numpy(dtype=float)
        if any(
            not np.isclose(
                published_delta,
                expected_delta,
                rtol=0.0,
                atol=1e-10,
            ).any()
            for expected_delta in TTF_CALL_DELTA_NODES
        ):
            missing_anchor_months.append(pd.Timestamp(contract_date).date().isoformat())
    if missing_anchor_months:
        raise TTFPublicationError(
            f"Every governed {product} maturity must retain the complete 11-node "
            "delta grid; missing=" + ", ".join(missing_anchor_months[:10])
        )
    fingerprint = (
        input_manifest_fingerprint(input_manifest)
        if input_manifest is not None
        else ttf_surface_fingerprint(prepared, commodity=product)
    )
    prepared["input_fingerprint"] = fingerprint
    results = [dict(item) for item in expiry_results]
    trade_ids = [str(trade_id) for trade_id in manual_trade_ids]
    result_expiries = {
        pd.Timestamp(item.get("option_expiration_date")).date()
        for item in results
        if item.get("option_expiration_date") is not None
    }
    surface_expiries = set(prepared["option_expiration_date"].dt.date.unique())
    if len(results) != len(result_expiries):
        raise TTFPublicationError(
            f"A complete {product} publication requires one result per expiry."
        )
    if result_expiries != surface_expiries:
        raise TTFPublicationError(
            f"Every published {product} expiry requires exactly one validated result."
        )
    if any(not (item.get("validation") or {}).get("is_valid", False) for item in results):
        raise TTFPublicationError(
            f"Every published {product} expiry must pass validation."
        )
    if expected_expiries is not None:
        expected_months = {
            value if isinstance(value, pd.Period) else pd.Timestamp(value).to_period("M")
            for value in expected_expiries
        }
        surface_months = set(prepared["contract_date"].dt.to_period("M").unique())
        if expected_months != surface_months:
            raise TTFPublicationError(
                f"The {product} publication does not contain the complete governed expiry set."
            )

    run_id = str(uuid4())
    publication_id = str(uuid4())
    configuration = {
        "settlement_cob": settlement.isoformat(),
        "base_publication_id": base_publication_id,
        "commodity": product,
        "method": policy["method"],
        "policy_version": policy["policy_version"],
        "manual_trade_ids": trade_ids,
        "expiry_count": len(surface_expiries),
        "point_count": len(prepared),
        "input_manifest": (
            _json_ready(dict(input_manifest))
            if input_manifest is not None
            else None
        ),
        "input_manifest_fingerprint": fingerprint,
    }
    _log_publication_timing(
        timing_enabled, product, "publication_preparation",
        perf_counter() - publication_started,
        points=len(prepared), expiries=len(surface_expiries),
    )
    transaction_started = perf_counter() if timing_enabled else 0.0
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"{product}:{trading.isoformat()}"},
        )
        existing = connection.execute(
            text(
                f"SELECT publication_id FROM {PUBLICATION_TABLE} "
                "WHERE idempotency_key = :idempotency_key"
            ),
            {"idempotency_key": idempotency_key},
        ).scalar_one_or_none()
        if existing is not None:
            if return_surface:
                return load_latest_ttf_publication(
                    engine, trading, publication_id=str(existing), commodity=product
                )
            return _load_persisted_publication_receipt(
                engine, str(existing), commodity=product
            )

        current = connection.execute(
            text(
                f"""
                SELECT publication_id FROM {PUBLICATION_TABLE}
                WHERE commodity = '{product}' AND cob_date = :cob_date AND is_active
                FOR UPDATE
                """
            ),
            {"cob_date": trading},
        ).scalar_one_or_none()
        current_id = str(current) if current is not None else None
        expected_current_id = (
            str(expected_current_publication_id)
            if expected_current_publication_id
            else None
        )
        if current_id != expected_current_id:
            raise TTFPublicationError(
                f"The active {product} publication for this trading date changed; "
                "reload before publishing."
            )

        connection.execute(
            text(
                f"""
                INSERT INTO {RUN_TABLE}
                    (run_id, commodity, cob_date, input_fingerprint, engine_version,
                     status, configuration, notes, created_by, idempotency_key)
                VALUES
                    (:run_id, :commodity, :cob_date, :fingerprint, :engine_version,
                     'draft', CAST(:configuration AS jsonb), :notes, :created_by,
                     :run_idempotency)
                """
            ),
            {
                "run_id": run_id,
                "commodity": product,
                "cob_date": trading,
                "fingerprint": fingerprint,
                "engine_version": policy["engine_version"],
                "configuration": json.dumps(configuration, sort_keys=True),
                "notes": notes,
                "created_by": created_by,
                "run_idempotency": f"{idempotency_key}:run",
            },
        )
        _audit(connection, run_id, "created", created_by, configuration, None, "draft")
        if trade_ids and _table_available(engine, "vol_calibration_run_trade_inputs"):
            connection.execute(
                text(
                    f"""
                    INSERT INTO {RUN_TRADE_TABLE} (run_id, trade_id)
                    VALUES (:run_id, :trade_id)
                    """
                ),
                [
                    {"run_id": run_id, "trade_id": trade_id}
                    for trade_id in trade_ids
                ],
            )
        connection.execute(
            text(
                f"UPDATE {RUN_TABLE} SET status='submitted', submitted_at=CURRENT_TIMESTAMP "
                "WHERE run_id=:run_id"
            ),
            {"run_id": run_id},
        )
        _audit(connection, run_id, "submitted", created_by, {}, "draft", "submitted")
        connection.execute(
            text(f"UPDATE {RUN_TABLE} SET status='approved' WHERE run_id=:run_id"),
            {"run_id": run_id},
        )
        _audit(
            connection,
            run_id,
            "approved",
            identity.subject,
            {},
            "submitted",
            "approved",
        )

        expiry_write_started = perf_counter() if timing_enabled else 0.0
        _insert_expiry_results(connection, run_id, results)
        _log_publication_timing(
            timing_enabled, product, "expiry_result_write",
            perf_counter() - expiry_write_started, results=len(results),
        )

        connection.execute(
            text(
                f"""
                INSERT INTO {PUBLICATION_TABLE}
                    (publication_id, run_id, commodity, cob_date, status, is_active,
                     approved_by, approved_at, supersedes_publication_id,
                     idempotency_key)
                VALUES
                    (:publication_id, :run_id, :commodity, :cob_date, 'approved', FALSE,
                     :approved_by, CURRENT_TIMESTAMP, :supersedes, :idempotency_key)
                """
            ),
            {
                "publication_id": publication_id,
                "run_id": run_id,
                "commodity": product,
                "cob_date": trading,
                "approved_by": identity.subject,
                "supersedes": current,
                "idempotency_key": idempotency_key,
            },
        )
        point_write_started = perf_counter() if timing_enabled else 0.0
        point_rows = []
        for row in prepared.to_dict("records"):
            point_rows.append(
                {
                    "publication_id": publication_id,
                    "run_id": run_id,
                    "commodity": product,
                    "cob_date": trading,
                    "contract_date": pd.Timestamp(row["contract_date"]).date(),
                    "option_expiration_date": pd.Timestamp(
                        row["option_expiration_date"]
                    ).date(),
                    "strike": float(row["strike"]),
                    "delta": float(row["delta"]),
                    "put_call": "C",
                    "volatility": float(row["volatility"]),
                    "total_variance": float(row["total_variance"]),
                    "working_forward": float(row["working_forward"]),
                    "surface_region": str(row["surface_region"]),
                    "blend_classification": str(row["blend_classification"]),
                    "calibration_basis": str(row["calibration_basis"]).lower(),
                    "source_name": str(row["source_name"]),
                    "calibration_method": policy["method"],
                    "calibration_policy_version": policy["policy_version"],
                    "input_fingerprint": fingerprint,
                }
            )
        copied = _copy_surface_points(connection, point_rows)
        if not copied:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {SURFACE_TABLE}
                        (publication_id, run_id, commodity, cob_date, contract_date,
                         option_expiration_date, strike, delta, put_call, volatility,
                         total_variance, working_forward, surface_region, blend_classification,
                         calibration_basis, source_name, calibration_method,
                         calibration_policy_version, input_fingerprint)
                    VALUES
                        (:publication_id, :run_id, :commodity, :cob_date, :contract_date,
                         :option_expiration_date, :strike, :delta, :put_call, :volatility,
                         :total_variance, :working_forward, :surface_region, :blend_classification,
                         :calibration_basis, :source_name, :calibration_method,
                         :calibration_policy_version, :input_fingerprint)
                    """
                ),
                point_rows,
            )
        inserted = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {SURFACE_TABLE} "
                "WHERE publication_id=:publication_id"
            ),
            {"publication_id": publication_id},
        ).scalar_one()
        if int(inserted) != len(prepared):
            raise TTFPublicationError(
                f"{product} publication point readback did not reconcile."
            )
        _log_publication_timing(
            timing_enabled, product, "dense_point_write",
            perf_counter() - point_write_started, points=len(point_rows),
            copy=bool(copied),
        )
        if current is not None:
            connection.execute(
                text(
                    f"""
                    UPDATE {PUBLICATION_TABLE}
                    SET status='superseded', is_active=FALSE
                    WHERE publication_id=:publication_id
                    """
                ),
                {"publication_id": current},
            )
        connection.execute(
            text(
                f"""
                UPDATE {PUBLICATION_TABLE}
                SET status='published', is_active=TRUE,
                    published_by=:published_by, published_at=CURRENT_TIMESTAMP
                WHERE publication_id=:publication_id
                """
            ),
            {
                "publication_id": publication_id,
                "published_by": identity.subject,
            },
        )
        connection.execute(
            text(f"UPDATE {RUN_TABLE} SET status='published' WHERE run_id=:run_id"),
            {"run_id": run_id},
        )
        _audit(
            connection,
            run_id,
            "published",
            identity.subject,
            {"publication_id": publication_id, "point_count": len(prepared)},
            "approved",
            "published",
        )
        commit_started = perf_counter() if timing_enabled else 0.0

    _log_publication_timing(
        timing_enabled, product, "transaction_commit",
        perf_counter() - commit_started, points=len(prepared),
    )
    _log_publication_timing(
        timing_enabled, product, "transaction_total",
        perf_counter() - transaction_started, points=len(prepared),
    )
    readback_started = perf_counter() if timing_enabled else 0.0
    if return_surface:
        readback = load_latest_ttf_publication(
            engine, trading, publication_id=publication_id, commodity=product
        )
        result_count = len(readback.get("expiry_results") or [])
        all_valid = all(
            (item.get("validation") or {}).get("is_valid", False)
            for item in (readback.get("expiry_results") or [])
        )
    else:
        readback = _load_persisted_publication_receipt(
            engine, publication_id, commodity=product
        )
        result_count = readback["result_count"]
        all_valid = True  # The receipt checked the persisted aggregate.
    if (
        readback.get("publication_id") != publication_id
        or int(readback.get("row_count") or 0) != len(prepared)
        or int(readback.get("expiry_count") or 0) != len(surface_expiries)
        or result_count != len(results)
        or not all_valid
        or readback.get("input_manifest_fingerprint") != fingerprint
        or (not return_surface and readback.get("point_fingerprint") != fingerprint)
    ):
        raise TTFPublicationError(
            f"Published {product} surface failed post-commit readback."
        )
    _log_publication_timing(
        timing_enabled, product, "publication_readback",
        perf_counter() - readback_started, full_surface=bool(return_surface),
        rows=readback.get("row_count"), results=result_count,
    )
    _log_publication_timing(
        timing_enabled, product, "publication_total",
        perf_counter() - publication_started,
    )
    return readback


def load_latest_hybrid_publication(
    engine,
    trading_date,
    *,
    commodity: str,
    as_of: datetime | None = None,
    publication_id: str | None = None,
    prefer_exact_cob: bool = False,
    require_exact_cob: bool = False,
) -> dict:
    return load_latest_ttf_publication(
        engine,
        trading_date,
        as_of=as_of,
        publication_id=publication_id,
        prefer_exact_cob=prefer_exact_cob,
        require_exact_cob=require_exact_cob,
        commodity=commodity,
    )


def hybrid_publication_frame(payload: Mapping | None) -> pd.DataFrame:
    return ttf_publication_frame(payload)


def publish_hybrid_surface(
    engine,
    surface: pd.DataFrame,
    expiry_results: Iterable[Mapping],
    *,
    commodity: str,
    **kwargs,
) -> dict:
    return publish_ttf_surface(
        engine,
        surface,
        expiry_results,
        commodity=commodity,
        **kwargs,
    )


def published_expiry_result(publication_payload, observations):
    """Return the saved tail parameters for one governed option expiry."""
    target = pd.to_datetime(
        observations['option_expiration_date'].iloc[0], errors='coerce'
    )
    if pd.isna(target):
        return None
    for item in (publication_payload or {}).get('expiry_results') or []:
        item_expiry = pd.to_datetime(
            item.get('option_expiration_date'), errors='coerce'
        )
        if not pd.isna(item_expiry) and item_expiry.date() == target.date():
            return item
    return None


def same_day_publication_id(publication_payload, trading_date):
    """Return the optimistic-concurrency ID only for the selected working date."""
    publication_id = (publication_payload or {}).get('publication_id')
    publication_date = pd.to_datetime(
        (publication_payload or {}).get('publication_date'), errors='coerce'
    )
    selected = pd.to_datetime(trading_date, errors='coerce')
    if (
        publication_id
        and not pd.isna(publication_date)
        and not pd.isna(selected)
        and publication_date.date() == selected.date()
    ):
        return str(publication_id)
    return None


def same_day_publication_reference(publication, cob_date):
    """Match a valid working COB while retaining the publication ID type.

    HH callers validate the selected COB before this lookup. Unlike the
    tolerant text-ID helper, an invalid working date remains an error.
    """
    if not publication or not publication.get("publication_id"):
        return None
    publication_date = pd.to_datetime(
        publication.get("publication_date"), errors="coerce"
    )
    if pd.isna(publication_date):
        return None
    return (
        publication.get("publication_id")
        if publication_date.date() == pd.Timestamp(cob_date).date()
        else None
    )
