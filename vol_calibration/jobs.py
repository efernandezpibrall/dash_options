"""PostgreSQL-backed background-job state transitions for calibration batches."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import text


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobRecord:
    job_id: UUID
    run_id: UUID | None
    status: JobStatus
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    cancellation_requested: bool
    worker_id: str | None = None
    lease_expires_at: datetime | None = None

    @classmethod
    def from_mapping(cls, row):
        return cls(
            job_id=row["job_id"],
            run_id=row.get("run_id"),
            status=JobStatus(row["status"]),
            payload=row.get("payload") or {},
            attempts=int(row.get("attempts") or 0),
            max_attempts=int(row.get("max_attempts") or 0),
            cancellation_requested=bool(row.get("cancellation_requested")),
            worker_id=row.get("worker_id"),
            lease_expires_at=row.get("lease_expires_at"),
        )


SUBMIT_JOB_SQL = text(
    """
    INSERT INTO at_lng.vol_calibration_jobs
        (job_id, run_id, job_type, status, payload, idempotency_key,
         created_by, max_attempts, total_items)
    VALUES
        (gen_random_uuid(), :run_id, :job_type, 'queued', CAST(:payload AS jsonb),
         :idempotency_key, :created_by, :max_attempts, :total_items)
    ON CONFLICT (idempotency_key)
    DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
    RETURNING *
    """
)

CLAIM_JOB_SQL = text(
    """
    WITH candidate AS (
        SELECT job_id
        FROM at_lng.vol_calibration_jobs
        WHERE cancellation_requested = FALSE
          AND attempts < max_attempts
          AND (
              status = 'queued'
              OR (status = 'running' AND lease_expires_at < CURRENT_TIMESTAMP)
          )
        ORDER BY created_at, job_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    ),
    reset_orphaned_items AS (
        UPDATE at_lng.vol_calibration_job_items AS item
        SET status = 'queued',
            updated_at = CURRENT_TIMESTAMP
        FROM candidate
        WHERE item.job_id = candidate.job_id
          AND item.status = 'running'
    )
    UPDATE at_lng.vol_calibration_jobs AS job
    SET status = 'running',
        worker_id = :worker_id,
        lease_expires_at = CURRENT_TIMESTAMP
            + (:lease_seconds * INTERVAL '1 second'),
        attempts = job.attempts + 1,
        started_at = COALESCE(job.started_at, CURRENT_TIMESTAMP),
        updated_at = CURRENT_TIMESTAMP
    FROM candidate
    WHERE job.job_id = candidate.job_id
    RETURNING job.*
    """
)

CLAIM_JOB_ITEM_SQL = text(
    """
    WITH owned_job AS (
        SELECT job_id
        FROM at_lng.vol_calibration_jobs
        WHERE job_id = :job_id
          AND status = 'running'
          AND worker_id = :worker_id
          AND cancellation_requested = FALSE
          AND lease_expires_at >= CURRENT_TIMESTAMP
    ),
    candidate AS (
        SELECT item.item_id
        FROM at_lng.vol_calibration_job_items AS item
        JOIN owned_job ON owned_job.job_id = item.job_id
        WHERE item.status = 'queued'
        ORDER BY item.option_expiration_date, item.item_id
        FOR UPDATE OF item SKIP LOCKED
        LIMIT 1
    )
    UPDATE at_lng.vol_calibration_job_items AS item
    SET status = 'running',
        attempts = item.attempts + 1,
        started_at = COALESCE(item.started_at, CURRENT_TIMESTAMP),
        updated_at = CURRENT_TIMESTAMP
    FROM candidate
    WHERE item.item_id = candidate.item_id
    RETURNING item.*
    """
)

HEARTBEAT_SQL = text(
    """
    UPDATE at_lng.vol_calibration_jobs
    SET lease_expires_at = CURRENT_TIMESTAMP + (:lease_seconds * INTERVAL '1 second'),
        updated_at = CURRENT_TIMESTAMP
    WHERE job_id = :job_id
      AND status = 'running'
      AND worker_id = :worker_id
      AND cancellation_requested = FALSE
    RETURNING *
    """
)

REQUEST_CANCEL_SQL = text(
    """
    UPDATE at_lng.vol_calibration_jobs
    SET cancellation_requested = TRUE,
        status = 'cancelled',
        worker_id = NULL,
        lease_expires_at = NULL,
        finished_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE job_id = :job_id
      AND status IN ('queued', 'running')
    RETURNING *
    """
)

COMPLETE_JOB_SQL = text(
    """
    UPDATE at_lng.vol_calibration_jobs
    SET status = 'succeeded',
        completed_items = total_items,
        worker_id = NULL,
        lease_expires_at = NULL,
        finished_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE job_id = :job_id
      AND status = 'running'
      AND worker_id = :worker_id
      AND cancellation_requested = FALSE
    RETURNING *
    """
)

FAIL_JOB_SQL = text(
    """
    UPDATE at_lng.vol_calibration_jobs
    SET status = CASE
            WHEN cancellation_requested THEN 'cancelled'
            WHEN attempts < max_attempts THEN 'queued'
            ELSE 'failed'
        END,
        worker_id = NULL,
        lease_expires_at = NULL,
        last_error = :last_error,
        finished_at = CASE
            WHEN cancellation_requested OR attempts >= max_attempts
            THEN CURRENT_TIMESTAMP
            ELSE NULL
        END,
        updated_at = CURRENT_TIMESTAMP
    WHERE job_id = :job_id
      AND status = 'running'
      AND worker_id = :worker_id
    RETURNING *
    """
)

UPSERT_JOB_ITEM_SQL = text(
    """
    INSERT INTO at_lng.vol_calibration_job_items
        (item_id, job_id, option_expiration_date, status)
    VALUES
        (gen_random_uuid(), :job_id, :option_expiration_date, 'queued')
    ON CONFLICT (job_id, option_expiration_date)
    DO UPDATE SET job_id = EXCLUDED.job_id
    RETURNING *
    """
)

COMPLETE_JOB_ITEM_SQL = text(
    """
    WITH owned_job AS (
        SELECT job_id
        FROM at_lng.vol_calibration_jobs
        WHERE job_id = :job_id
          AND status = 'running'
          AND worker_id = :worker_id
          AND lease_expires_at >= CURRENT_TIMESTAMP
    ),
    completed AS (
        UPDATE at_lng.vol_calibration_job_items AS item
        SET status = :status,
            result_id = :result_id,
            last_error = :last_error,
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        FROM owned_job
        WHERE item.item_id = :item_id
          AND item.job_id = owned_job.job_id
          AND item.status = 'running'
        RETURNING item.job_id
    )
    UPDATE at_lng.vol_calibration_jobs AS job
    SET completed_items = completed_items + 1,
        updated_at = CURRENT_TIMESTAMP
    FROM completed
    WHERE job.job_id = completed.job_id
    RETURNING job.*
    """
)


class PostgresJobRepository:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def _record(result):
        row = result.mappings().first()
        return JobRecord.from_mapping(row) if row else None

    @staticmethod
    def _mapping(result):
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    def _require_positive(value: int, name: str) -> None:
        if value <= 0:
            raise ValueError(f"{name} must be positive.")

    def submit(
        self,
        *,
        run_id,
        job_type: str,
        payload: Any,
        idempotency_key: str,
        created_by: str,
        max_attempts: int,
        total_items: int,
    ):
        self._require_positive(max_attempts, "max_attempts")
        if total_items < 0:
            raise ValueError("total_items cannot be negative.")
        serialized_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.engine.begin() as connection:
            result = connection.execute(
                SUBMIT_JOB_SQL,
                {
                    "run_id": run_id,
                    "job_type": job_type,
                    "payload": serialized_payload,
                    "idempotency_key": idempotency_key,
                    "created_by": created_by,
                    "max_attempts": max_attempts,
                    "total_items": total_items,
                },
            )
            return self._record(result)

    def claim(self, *, worker_id: str, lease_seconds: int = 60):
        self._require_positive(lease_seconds, "lease_seconds")
        with self.engine.begin() as connection:
            result = connection.execute(
                CLAIM_JOB_SQL,
                {"worker_id": worker_id, "lease_seconds": lease_seconds},
            )
            return self._record(result)

    def heartbeat(self, *, job_id, worker_id: str, lease_seconds: int = 60):
        self._require_positive(lease_seconds, "lease_seconds")
        with self.engine.begin() as connection:
            result = connection.execute(
                HEARTBEAT_SQL,
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds,
                },
            )
            return self._record(result)

    def request_cancel(self, *, job_id):
        with self.engine.begin() as connection:
            return self._record(
                connection.execute(REQUEST_CANCEL_SQL, {"job_id": job_id})
            )

    def complete(self, *, job_id, worker_id: str):
        with self.engine.begin() as connection:
            return self._record(
                connection.execute(
                    COMPLETE_JOB_SQL,
                    {"job_id": job_id, "worker_id": worker_id},
                )
            )

    def fail(self, *, job_id, worker_id: str, last_error: str):
        with self.engine.begin() as connection:
            return self._record(
                connection.execute(
                    FAIL_JOB_SQL,
                    {
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "last_error": last_error,
                    },
                )
            )

    def upsert_item(self, *, job_id, option_expiration_date):
        with self.engine.begin() as connection:
            return self._mapping(
                connection.execute(
                    UPSERT_JOB_ITEM_SQL,
                    {
                        "job_id": job_id,
                        "option_expiration_date": option_expiration_date,
                    },
                )
            )

    def claim_item(self, *, job_id, worker_id: str):
        with self.engine.begin() as connection:
            return self._mapping(
                connection.execute(
                    CLAIM_JOB_ITEM_SQL,
                    {"job_id": job_id, "worker_id": worker_id},
                )
            )

    def complete_item(
        self,
        *,
        job_id,
        item_id,
        worker_id: str,
        status: str,
        result_id=None,
        last_error: str | None = None,
    ):
        if status not in {"succeeded", "failed", "cancelled", "skipped"}:
            raise ValueError("Item completion status is invalid.")
        with self.engine.begin() as connection:
            return self._record(
                connection.execute(
                    COMPLETE_JOB_ITEM_SQL,
                    {
                        "job_id": job_id,
                        "item_id": item_id,
                        "worker_id": worker_id,
                        "status": status,
                        "result_id": result_id,
                        "last_error": last_error,
                    },
                )
            )
