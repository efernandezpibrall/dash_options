"""Server-side gateway for queued Bloomberg option-chain refresh jobs."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from runtime_config import config_bool, config_value, get_database_engine


JOB_TABLE = "at_lng.bbg_option_chain_refresh_jobs"
WORKER_TABLE = "at_lng.bbg_option_chain_workers"
WORKER_FRESHNESS_SECONDS = 30
DEFAULT_PRODUCT = "BRENT"
SUPPORTED_PRODUCTS = frozenset({"BRENT", "TFO", "ON", "LNE"})
DUBAI = ZoneInfo("Asia/Dubai")
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
INTRADAY_REQUEST_KIND = "user_refresh"
SETTLEMENT_REQUEST_KIND = "settlement_refresh"
SUPPORTED_REQUEST_KINDS = frozenset(
    {INTRADAY_REQUEST_KIND, SETTLEMENT_REQUEST_KIND}
)


def enabled_products() -> frozenset[str]:
    raw = os.getenv("BBG_OPTION_CHAIN_ENABLED_PRODUCTS")
    if raw is None:
        raw = config_value(
            "BLOOMBERG_OPTIONS",
            "ENABLED_PRODUCTS",
            fallback=DEFAULT_PRODUCT,
        )
    products = frozenset(
        value.strip().upper() for value in str(raw).split(",") if value.strip()
    )
    unsupported = sorted(products - SUPPORTED_PRODUCTS)
    if unsupported:
        raise ValueError(
            "Unsupported BLOOMBERG_OPTIONS.ENABLED_PRODUCTS: "
            + ", ".join(unsupported)
        )
    if not products:
        raise ValueError("BLOOMBERG_OPTIONS.ENABLED_PRODUCTS cannot be empty")
    return products


def normalize_product(product: str) -> str:
    normalized = str(product or "").strip().upper()
    if normalized not in SUPPORTED_PRODUCTS:
        raise ValueError(f"Unsupported option product {product!r}")
    return normalized


def intraday_refresh_enabled(product: str = DEFAULT_PRODUCT) -> bool:
    raw = os.getenv("BBG_OPTION_CHAIN_INTRADAY_REFRESH_ENABLED")
    if raw is not None:
        feature_enabled = raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        feature_enabled = config_bool(
            "BLOOMBERG_OPTIONS",
            "INTRADAY_REFRESH_ENABLED",
            fallback=False,
        )
    return feature_enabled and normalize_product(product) in enabled_products()


def settlement_refresh_enabled(product: str = DEFAULT_PRODUCT) -> bool:
    raw = os.getenv("BBG_OPTION_CHAIN_SETTLEMENT_REFRESH_ENABLED")
    if raw is not None:
        feature_enabled = raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        feature_enabled = config_bool(
            "BLOOMBERG_OPTIONS",
            "SETTLEMENT_REFRESH_ENABLED",
            fallback=False,
        )
    return feature_enabled and normalize_product(product) in enabled_products()


def dubai_business_date() -> date:
    return datetime.now(DUBAI).date()


@dataclass(frozen=True)
class RefreshJobView:
    job_id: str
    product: str
    business_date: str
    status: str
    stage: str
    requested_by: str
    request_kind: str
    result_snapshot_id: str | None
    result_status: str | None
    previous_snapshot_id: str | None
    metrics: dict[str, Any]
    last_error: str | None
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_mapping(cls, row) -> "RefreshJobView":
        value = dict(row)
        return cls(
            job_id=str(value["job_id"]),
            product=str(value["product"]),
            business_date=value["business_date"].isoformat(),
            status=str(value["status"]),
            stage=str(value["stage"]),
            requested_by=str(value["requested_by"]),
            request_kind=str(
                value.get("request_kind") or INTRADAY_REQUEST_KIND
            ),
            result_snapshot_id=(
                str(value["result_snapshot_id"])
                if value.get("result_snapshot_id")
                else None
            ),
            result_status=value.get("result_status"),
            previous_snapshot_id=(
                str(value["previous_snapshot_id"])
                if value.get("previous_snapshot_id")
                else None
            ),
            metrics=dict(value.get("metrics") or {}),
            last_error=value.get("last_error"),
            created_at=(
                value["created_at"].isoformat() if value.get("created_at") else None
            ),
            updated_at=(
                value["updated_at"].isoformat() if value.get("updated_at") else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class WorkerReadiness:
    product: str
    ready: bool
    reason: str
    worker_id: str | None = None
    lifecycle_status: str | None = None
    bloomberg_session_status: str | None = None
    heartbeat_at: str | None = None


def get_worker_readiness(
    product: str,
    *,
    engine=None,
    freshness_seconds: int = WORKER_FRESHNESS_SECONDS,
) -> WorkerReadiness:
    """Return whether a fresh, running worker can serve ``product``.

    Registry failures intentionally fail closed so the dashboard cannot queue a
    request that no observable worker is available to claim.
    """

    normalized_product = normalize_product(product)
    if freshness_seconds <= 0:
        raise ValueError("Worker freshness must be positive")
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        return WorkerReadiness(
            product=normalized_product,
            ready=False,
            reason="registry_unavailable",
        )
    try:
        with db_engine.connect() as connection:
            row = connection.execute(
                text(
                    f"""
                    SELECT worker_id,
                           lifecycle_status,
                           bloomberg_session_status,
                           heartbeat_at,
                           lifecycle_status IN ('starting', 'idle', 'running')
                               AS lifecycle_is_available,
                           heartbeat_at >= CURRENT_TIMESTAMP
                               - CAST(:freshness_seconds AS integer)
                                 * INTERVAL '1 second'
                               AS heartbeat_is_fresh
                    FROM {WORKER_TABLE}
                    WHERE :product = ANY(enabled_products)
                    ORDER BY heartbeat_at DESC NULLS LAST, worker_id
                    LIMIT 1
                    """
                ),
                {
                    "product": normalized_product,
                    "freshness_seconds": int(freshness_seconds),
                },
            ).mappings().first()
    except Exception:
        return WorkerReadiness(
            product=normalized_product,
            ready=False,
            reason="registry_unavailable",
        )

    if row is None:
        return WorkerReadiness(
            product=normalized_product,
            ready=False,
            reason="no_eligible_worker",
        )
    value = dict(row)
    lifecycle_available = bool(value.get("lifecycle_is_available"))
    heartbeat_is_fresh = bool(value.get("heartbeat_is_fresh"))
    if not lifecycle_available:
        reason = "worker_not_running"
    elif not heartbeat_is_fresh:
        reason = "stale_heartbeat"
    else:
        reason = "ready"
    heartbeat_at = value.get("heartbeat_at")
    return WorkerReadiness(
        product=normalized_product,
        ready=reason == "ready",
        reason=reason,
        worker_id=(str(value["worker_id"]) if value.get("worker_id") else None),
        lifecycle_status=value.get("lifecycle_status"),
        bloomberg_session_status=value.get("bloomberg_session_status"),
        heartbeat_at=(heartbeat_at.isoformat() if heartbeat_at else None),
    )


def submit_refresh_job(
    requested_by: str,
    *,
    product: str,
    engine=None,
    business_date: date | None = None,
    request_kind: str = INTRADAY_REQUEST_KIND,
) -> tuple[RefreshJobView, bool]:
    normalized_product = normalize_product(product)
    normalized_kind = str(request_kind).strip().lower()
    if normalized_kind not in SUPPORTED_REQUEST_KINDS:
        raise ValueError(f"Unsupported refresh request kind {request_kind!r}")
    enabled = (
        settlement_refresh_enabled(normalized_product)
        if normalized_kind == SETTLEMENT_REQUEST_KIND
        else intraday_refresh_enabled(normalized_product)
    )
    if not enabled:
        refresh_name = (
            "settlement"
            if normalized_kind == SETTLEMENT_REQUEST_KIND
            else "intraday"
        )
        raise PermissionError(
            f"Bloomberg {refresh_name} refresh is disabled by configuration."
        )
    if not requested_by.strip():
        raise PermissionError("An authenticated user is required.")
    db_engine = engine or get_database_engine(required=True)
    requested_date = business_date or dubai_business_date()
    parameters = {
        "product": normalized_product,
        "business_date": requested_date,
        "requested_by": requested_by.strip(),
        "request_kind": normalized_kind,
        "request_family": (
            "settlement"
            if normalized_kind == SETTLEMENT_REQUEST_KIND
            else "intraday"
        ),
        "idempotency_key": (
            f"dashboard:{normalized_product}:{normalized_kind}:{requested_date}:{uuid.uuid4()}"
        ),
    }
    with db_engine.begin() as connection:
        inserted = connection.execute(
            text(
                f"""
                INSERT INTO {JOB_TABLE} (
                    product, business_date, requested_by, idempotency_key,
                    request_kind
                ) VALUES (
                    :product, :business_date, :requested_by, :idempotency_key,
                    :request_kind
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """
            ),
            parameters,
        ).mappings().first()
        if inserted is not None:
            return RefreshJobView.from_mapping(inserted), True
        active = connection.execute(
            text(
                f"""
                SELECT *
                FROM {JOB_TABLE}
                WHERE product = :product
                  AND status IN ('queued', 'running')
                  AND (
                    (
                      :request_family = 'settlement'
                      AND request_kind = 'settlement_refresh'
                    )
                    OR (
                      :request_family = 'intraday'
                      AND request_kind IN ('user_refresh', 'daily_tape_seed')
                      AND business_date = :business_date
                    )
                  )
                ORDER BY created_at, job_id
                LIMIT 1
                """
            ),
            parameters,
        ).mappings().first()
    if active is None:
        raise RuntimeError("Refresh job could not be queued; retry the request.")
    return RefreshJobView.from_mapping(active), False


def get_refresh_job(
    job_id: str,
    *,
    product: str,
    engine=None,
) -> RefreshJobView | None:
    normalized_product = normalize_product(product)
    db_engine = engine or get_database_engine(required=True)
    with db_engine.connect() as connection:
        row = connection.execute(
            text(
                f"""
                SELECT *
                FROM {JOB_TABLE}
                WHERE job_id = CAST(:job_id AS uuid) AND product = :product
                """
            ),
            {"job_id": job_id, "product": normalized_product},
        ).mappings().first()
    return RefreshJobView.from_mapping(row) if row is not None else None
