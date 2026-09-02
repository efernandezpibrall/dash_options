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
SETTLEMENT_REFRESH_PRODUCTS = ("BRENT", "TFO", "ON", "LNE")
SUPPORTED_PRODUCTS = frozenset(SETTLEMENT_REFRESH_PRODUCTS)
DUBAI = ZoneInfo("Asia/Dubai")
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
INTRADAY_REQUEST_KIND = "user_refresh"
SETTLEMENT_REQUEST_KIND = "settlement_refresh"
SUPPORTED_REQUEST_KINDS = frozenset(
    {INTRADAY_REQUEST_KIND, SETTLEMENT_REQUEST_KIND}
)


class SettlementRefreshBatchError(RuntimeError):
    """Safe, user-actionable rejection of an all-product settlement batch."""


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


def _lock_refresh_products(connection, products: tuple[str, ...]) -> None:
    """Serialize cross-family dashboard submissions in canonical product order."""

    canonical_positions = {
        product: position
        for position, product in enumerate(SETTLEMENT_REFRESH_PRODUCTS)
    }
    ordered_products = sorted(
        dict.fromkeys(products),
        key=lambda product: canonical_positions[product],
    )
    for product in ordered_products:
        connection.execute(
            text(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(:lock_key, 0)
                )
                """
            ),
            {"lock_key": f"bbg_option_chain_refresh:{product}"},
        )


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
    return get_worker_readiness_many(
        (normalized_product,),
        engine=engine,
        freshness_seconds=freshness_seconds,
    )[normalized_product]


def get_worker_readiness_many(
    products: tuple[str, ...] | list[str] = SETTLEMENT_REFRESH_PRODUCTS,
    *,
    engine=None,
    freshness_seconds: int = WORKER_FRESHNESS_SECONDS,
) -> dict[str, WorkerReadiness]:
    """Return worker readiness for several products with one registry query."""

    normalized_products = tuple(
        dict.fromkeys(normalize_product(product) for product in products)
    )
    if not normalized_products:
        return {}
    if freshness_seconds <= 0:
        raise ValueError("Worker freshness must be positive")
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        return {
            product: WorkerReadiness(
                product=product,
                ready=False,
                reason="registry_unavailable",
            )
            for product in normalized_products
        }
    try:
        with db_engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"""
                    WITH requested_products AS (
                        SELECT product, position
                        FROM unnest(CAST(:products AS text[]))
                             WITH ORDINALITY AS requested(product, position)
                    )
                    SELECT requested.product,
                           worker.worker_id,
                           worker.lifecycle_status,
                           worker.bloomberg_session_status,
                           worker.heartbeat_at,
                           worker.lifecycle_is_available,
                           worker.heartbeat_is_fresh
                    FROM requested_products AS requested
                    LEFT JOIN LATERAL (
                        SELECT worker_id,
                               lifecycle_status,
                               bloomberg_session_status,
                               heartbeat_at,
                               lifecycle_status IN (
                                   'starting', 'idle', 'running'
                               ) AS lifecycle_is_available,
                               heartbeat_at >= CURRENT_TIMESTAMP
                                   - CAST(:freshness_seconds AS integer)
                                     * INTERVAL '1 second'
                                   AS heartbeat_is_fresh
                        FROM {WORKER_TABLE}
                        WHERE requested.product = ANY(enabled_products)
                        ORDER BY heartbeat_at DESC NULLS LAST, worker_id
                        LIMIT 1
                    ) AS worker ON TRUE
                    ORDER BY requested.position
                    """
                ),
                {
                    "products": list(normalized_products),
                    "freshness_seconds": int(freshness_seconds),
                },
            ).mappings().all()
    except Exception:
        return {
            product: WorkerReadiness(
                product=product,
                ready=False,
                reason="registry_unavailable",
            )
            for product in normalized_products
        }

    readiness_by_product = {}
    for row in rows:
        value = dict(row)
        product = normalize_product(value["product"])
        if not value.get("worker_id"):
            reason = "no_eligible_worker"
        elif not bool(value.get("lifecycle_is_available")):
            reason = "worker_not_running"
        elif not bool(value.get("heartbeat_is_fresh")):
            reason = "stale_heartbeat"
        else:
            reason = "ready"
        heartbeat_at = value.get("heartbeat_at")
        readiness_by_product[product] = WorkerReadiness(
            product=product,
            ready=reason == "ready",
            reason=reason,
            worker_id=(
                str(value["worker_id"]) if value.get("worker_id") else None
            ),
            lifecycle_status=value.get("lifecycle_status"),
            bloomberg_session_status=value.get("bloomberg_session_status"),
            heartbeat_at=(heartbeat_at.isoformat() if heartbeat_at else None),
        )
    return {
        product: readiness_by_product.get(
            product,
            WorkerReadiness(
                product=product,
                ready=False,
                reason="no_eligible_worker",
            ),
        )
        for product in normalized_products
    }


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
    refresh_name = (
        "settlement"
        if normalized_kind == SETTLEMENT_REQUEST_KIND
        else "intraday"
    )
    if not enabled:
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
        _lock_refresh_products(connection, (normalized_product,))
        conflicting_kind = (
            "intraday"
            if normalized_kind == SETTLEMENT_REQUEST_KIND
            else "settlement"
        )
        conflicting = connection.execute(
            text(
                f"""
                SELECT product, request_kind
                FROM {JOB_TABLE}
                WHERE product = :product
                  AND status IN ('queued', 'running')
                  AND (
                    (
                      :conflicting_kind = 'settlement'
                      AND request_kind = 'settlement_refresh'
                    )
                    OR (
                      :conflicting_kind = 'intraday'
                      AND request_kind IN ('user_refresh', 'daily_tape_seed')
                    )
                  )
                ORDER BY created_at, job_id
                LIMIT 1
                """
            ),
            {**parameters, "conflicting_kind": conflicting_kind},
        ).mappings().first()
        if conflicting is not None:
            active_name = (
                "settlement"
                if conflicting_kind == "settlement"
                else "intraday"
            )
            raise SettlementRefreshBatchError(
                f"A Bloomberg {active_name} refresh is already active for "
                f"{normalized_product}. Wait for it to finish before starting "
                f"a {refresh_name} refresh."
            )
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


def submit_settlement_refresh_jobs(
    requested_by: str,
    *,
    products: tuple[str, ...] | list[str] = SETTLEMENT_REFRESH_PRODUCTS,
    engine=None,
    business_date: date | None = None,
) -> dict[str, tuple[RefreshJobView, bool]]:
    """Atomically insert or join settlement jobs for every requested product."""

    normalized_products = tuple(
        dict.fromkeys(normalize_product(product) for product in products)
    )
    if not normalized_products:
        raise ValueError("At least one settlement product is required.")
    disabled = [
        product
        for product in normalized_products
        if not settlement_refresh_enabled(product)
    ]
    if disabled:
        raise SettlementRefreshBatchError(
            "Bloomberg settlement refresh is disabled for " + ", ".join(disabled) + "."
        )
    if not requested_by.strip():
        raise PermissionError("An authenticated user is required.")

    db_engine = engine or get_database_engine(required=True)
    readiness = get_worker_readiness_many(
        normalized_products,
        engine=db_engine,
    )
    unavailable = [
        product for product in normalized_products if not readiness[product].ready
    ]
    if unavailable:
        reasons = ", ".join(
            f"{product} ({readiness[product].reason})" for product in unavailable
        )
        raise SettlementRefreshBatchError(
            f"Bloomberg workers are unavailable for {reasons}."
        )

    requested_date = business_date or dubai_business_date()
    batch_id = uuid.uuid4()
    parameters = {
        "products": list(normalized_products),
        "business_date": requested_date,
        "requested_by": requested_by.strip(),
        "batch_id": str(batch_id),
    }
    with db_engine.begin() as connection:
        _lock_refresh_products(connection, normalized_products)
        active_intraday = connection.execute(
            text(
                f"""
                SELECT product
                FROM {JOB_TABLE}
                WHERE product = ANY(CAST(:products AS text[]))
                  AND status IN ('queued', 'running')
                  AND request_kind IN ('user_refresh', 'daily_tape_seed')
                ORDER BY array_position(
                    CAST(:products AS text[]), product
                ), created_at, job_id
                """
            ),
            parameters,
        ).mappings().all()
        if active_intraday:
            busy_products = tuple(
                dict.fromkeys(str(row["product"]) for row in active_intraday)
            )
            raise SettlementRefreshBatchError(
                "An intraday refresh is already active for "
                + ", ".join(busy_products)
                + ". Wait for it to finish before refreshing settlements."
            )

        inserted = connection.execute(
            text(
                f"""
                INSERT INTO {JOB_TABLE} (
                    product, business_date, requested_by, idempotency_key,
                    request_kind
                )
                SELECT target.product,
                       :business_date,
                       :requested_by,
                       'dashboard:' || target.product
                           || ':settlement_refresh:'
                           || CAST(:business_date AS text)
                           || ':' || :batch_id,
                       'settlement_refresh'
                FROM unnest(CAST(:products AS text[]))
                     WITH ORDINALITY AS target(product, position)
                ORDER BY target.position
                ON CONFLICT DO NOTHING
                RETURNING *
                """
            ),
            parameters,
        ).mappings().all()
        inserted_ids = {str(row["job_id"]) for row in inserted}
        active = connection.execute(
            text(
                f"""
                SELECT *
                FROM {JOB_TABLE}
                WHERE product = ANY(CAST(:products AS text[]))
                  AND status IN ('queued', 'running')
                  AND request_kind = 'settlement_refresh'
                ORDER BY array_position(
                    CAST(:products AS text[]), product
                ), created_at, job_id
                """
            ),
            parameters,
        ).mappings().all()
        jobs = {
            normalize_product(row["product"]): RefreshJobView.from_mapping(row)
            for row in active
        }
        missing = [product for product in normalized_products if product not in jobs]
        if missing:
            raise RuntimeError(
                "Settlement jobs could not be queued for " + ", ".join(missing) + "."
            )
        return {
            product: (jobs[product], jobs[product].job_id in inserted_ids)
            for product in normalized_products
        }


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


def get_refresh_jobs(
    job_ids_by_product: dict[str, str],
    *,
    engine=None,
) -> dict[str, RefreshJobView]:
    """Read product-scoped refresh jobs with one database query."""

    normalized = {
        normalize_product(product): str(job_id)
        for product, job_id in job_ids_by_product.items()
        if job_id
    }
    if not normalized:
        return {}
    db_engine = engine or get_database_engine(required=True)
    products = list(normalized)
    job_ids = [normalized[product] for product in products]
    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                WITH requested_jobs AS (
                    SELECT product, job_id
                    FROM unnest(
                        CAST(:products AS text[]),
                        CAST(:job_ids AS uuid[])
                    ) AS requested(product, job_id)
                )
                SELECT jobs.*
                FROM {JOB_TABLE} AS jobs
                JOIN requested_jobs AS requested
                  ON requested.job_id = jobs.job_id
                 AND requested.product = jobs.product
                """
            ),
            {"products": products, "job_ids": job_ids},
        ).mappings().all()
    return {
        normalize_product(row["product"]): RefreshJobView.from_mapping(row)
        for row in rows
    }
