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
    completed_items: int = 0
    total_items: int = 0
    last_error: str | None = None
    created_by: str | None = None
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
            completed_items=int(row.get("completed_items") or 0),
            total_items=int(row.get("total_items") or 0),
            last_error=row.get("last_error"),
            created_by=row.get("created_by"),
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
    WHERE vol_calibration_jobs.payload = EXCLUDED.payload
      AND vol_calibration_jobs.job_type = EXCLUDED.job_type
      AND vol_calibration_jobs.created_by = EXCLUDED.created_by
      AND vol_calibration_jobs.total_items = EXCLUDED.total_items
      AND vol_calibration_jobs.max_attempts = EXCLUDED.max_attempts
      AND vol_calibration_jobs.run_id IS NOT DISTINCT FROM EXCLUDED.run_id
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
          AND (:job_id IS NULL OR job_id = :job_id)
          AND (
              status = 'queued'
              OR (status = 'running' AND lease_expires_at < CURRENT_TIMESTAMP)
          )
        ORDER BY created_at, job_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    ),
    failed_items AS (
        SELECT COUNT(*) AS count
        FROM at_lng.vol_calibration_job_items AS item
        JOIN candidate ON candidate.job_id = item.job_id
        WHERE item.status = 'failed'
    ),
    reset_orphaned_items AS (
        UPDATE at_lng.vol_calibration_job_items AS item
        SET status = 'queued',
            updated_at = CURRENT_TIMESTAMP
        FROM candidate
        WHERE item.job_id = candidate.job_id
          AND item.status IN ('running', 'failed')
    )
    UPDATE at_lng.vol_calibration_jobs AS job
    SET status = 'running',
        worker_id = :worker_id,
        lease_expires_at = CURRENT_TIMESTAMP
            + (:lease_seconds * INTERVAL '1 second'),
        attempts = job.attempts + 1,
        completed_items = job.completed_items - (SELECT count FROM failed_items),
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
      AND lease_expires_at >= CURRENT_TIMESTAMP
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
      AND (:created_by IS NULL OR created_by = :created_by)
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
      AND completed_items = total_items
      AND lease_expires_at >= CURRENT_TIMESTAMP
      AND NOT EXISTS (
          SELECT 1 FROM at_lng.vol_calibration_job_items AS item
          WHERE item.job_id = :job_id
            AND item.status NOT IN ('succeeded', 'skipped')
      )
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

ABORT_JOB_SQL = text(
    """
    UPDATE at_lng.vol_calibration_jobs
    SET status = 'failed',
        worker_id = NULL,
        lease_expires_at = NULL,
        last_error = :last_error,
        finished_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE job_id = :job_id
      AND status = 'running'
      AND worker_id = :worker_id
      AND cancellation_requested = FALSE
      AND lease_expires_at >= CURRENT_TIMESTAMP
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
            result_payload = CAST(:result_payload AS jsonb),
            input_fingerprint = :input_fingerprint,
            dependency_fingerprint = :dependency_fingerprint,
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

GET_JOB_SQL = text(
    "SELECT * FROM at_lng.vol_calibration_jobs WHERE job_id = :job_id"
)

LIST_JOB_ITEMS_SQL = text(
    """
    SELECT * FROM at_lng.vol_calibration_job_items
    WHERE job_id = :job_id
    ORDER BY option_expiration_date, item_id
    """
)

INVALIDATE_FROM_SQL = text(
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
    stale AS (
        UPDATE at_lng.vol_calibration_job_items AS item
        SET status = 'queued',
            result_id = NULL,
            result_payload = NULL,
            input_fingerprint = NULL,
            dependency_fingerprint = NULL,
            last_error = NULL,
            finished_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        FROM owned_job
        WHERE item.job_id = owned_job.job_id
          AND item.option_expiration_date >= :option_expiration_date
          AND item.status IN ('succeeded', 'skipped', 'failed')
        RETURNING item.item_id
    )
    UPDATE at_lng.vol_calibration_jobs AS job
    SET completed_items = job.completed_items - (SELECT COUNT(*) FROM stale),
        updated_at = CURRENT_TIMESTAMP
    FROM owned_job
    WHERE job.job_id = owned_job.job_id
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
        item_dates=(),
    ):
        self._require_positive(max_attempts, "max_attempts")
        if total_items < 0:
            raise ValueError("total_items cannot be negative.")
        item_dates = tuple(item_dates)
        if item_dates and (len(item_dates) != total_items or len(set(item_dates)) != total_items):
            raise ValueError("Job item dates must uniquely match total_items.")
        serialized_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
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
            record = self._record(result)
            if record is None:
                raise ValueError(
                    "The calibration job key belongs to different immutable inputs."
                )
            for option_expiration_date in item_dates:
                connection.execute(
                    UPSERT_JOB_ITEM_SQL,
                    {
                        "job_id": record.job_id,
                        "option_expiration_date": option_expiration_date,
                    },
                )
            return record

    def claim(self, *, worker_id: str, lease_seconds: int = 60, job_id=None):
        self._require_positive(lease_seconds, "lease_seconds")
        with self.engine.begin() as connection:
            result = connection.execute(
                CLAIM_JOB_SQL,
                {
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds,
                    "job_id": job_id,
                },
            )
            return self._record(result)

    def get(self, *, job_id):
        with self.engine.connect() as connection:
            return self._record(connection.execute(GET_JOB_SQL, {"job_id": job_id}))

    def list_items(self, *, job_id):
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    LIST_JOB_ITEMS_SQL, {"job_id": job_id}
                ).mappings().all()
            ]

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

    def request_cancel(self, *, job_id, created_by: str | None = None):
        with self.engine.begin() as connection:
            return self._record(
                connection.execute(
                    REQUEST_CANCEL_SQL,
                    {"job_id": job_id, "created_by": created_by},
                )
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

    def abort(self, *, job_id, worker_id: str, last_error: str):
        """Stop a permanently stale or invalid job without retrying its inputs."""
        with self.engine.begin() as connection:
            return self._record(
                connection.execute(
                    ABORT_JOB_SQL,
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
        result_payload: Any = None,
        input_fingerprint: str | None = None,
        dependency_fingerprint: str | None = None,
        last_error: str | None = None,
    ):
        if status not in {"succeeded", "failed", "cancelled", "skipped"}:
            raise ValueError("Item completion status is invalid.")
        serialized_result = (
            json.dumps(
                result_payload, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            )
            if result_payload is not None else None
        )
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
                        "result_payload": serialized_result,
                        "input_fingerprint": input_fingerprint,
                        "dependency_fingerprint": dependency_fingerprint,
                        "last_error": last_error,
                    },
                )
            )

    def invalidate_from(
        self, *, job_id, worker_id: str, option_expiration_date
    ):
        """Discard one changed expiry and all warm-start dependents atomically."""
        with self.engine.begin() as connection:
            return self._record(
                connection.execute(
                    INVALIDATE_FROM_SQL,
                    {
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "option_expiration_date": option_expiration_date,
                    },
                )
            )
