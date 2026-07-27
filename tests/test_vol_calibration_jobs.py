import json
from datetime import date
from uuid import uuid4

import pytest

from vol_calibration.jobs import (
    CLAIM_JOB_ITEM_SQL,
    CLAIM_JOB_SQL,
    COMPLETE_JOB_ITEM_SQL,
    COMPLETE_JOB_SQL,
    FAIL_JOB_SQL,
    REQUEST_CANCEL_SQL,
    SUBMIT_JOB_SQL,
    PostgresJobRepository,
)


class _Mappings:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return _Mappings(self.row)


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        return _Result(self.rows.pop(0) if self.rows else None)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *args):
        return False


class _Engine:
    def __init__(self, rows):
        self.connection = _Connection(rows)

    def begin(self):
        return _Transaction(self.connection)


def _job_row(**overrides):
    row = {
        "job_id": uuid4(),
        "run_id": uuid4(),
        "status": "queued",
        "payload": {"product": "ttf"},
        "attempts": 0,
        "max_attempts": 3,
        "cancellation_requested": False,
        "worker_id": None,
        "lease_expires_at": None,
    }
    row.update(overrides)
    return row


def test_submit_is_idempotent_and_serializes_payload_deterministically():
    row = _job_row()
    engine = _Engine([row])
    repository = PostgresJobRepository(engine)

    submitted = repository.submit(
        run_id=row["run_id"],
        job_type="batch_calibration",
        payload={"product": "ttf", "expiries": ["Sep-26"]},
        idempotency_key="request-123",
        created_by="calibrator@example.com",
        max_attempts=3,
        total_items=1,
    )

    statement, parameters = engine.connection.calls[0]
    assert statement is SUBMIT_JOB_SQL
    assert "ON CONFLICT (idempotency_key)" in str(statement)
    assert json.loads(parameters["payload"]) == {
        "expiries": ["Sep-26"],
        "product": "ttf",
    }
    assert submitted.job_id == row["job_id"]


def test_claim_is_atomic_and_recovers_expired_work():
    row = _job_row(status="running", attempts=2, worker_id="worker-2")
    engine = _Engine([row])

    claimed = PostgresJobRepository(engine).claim(
        worker_id="worker-2",
        lease_seconds=90,
    )

    statement, parameters = engine.connection.calls[0]
    sql = str(statement)
    assert statement is CLAIM_JOB_SQL
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "lease_expires_at < CURRENT_TIMESTAMP" in sql
    assert "reset_orphaned_items" in sql
    assert "attempts < max_attempts" in sql
    assert parameters == {"worker_id": "worker-2", "lease_seconds": 90}
    assert claimed.attempts == 2


def test_job_item_claim_checks_owner_lease_and_cancellation_before_next_expiry():
    item = {
        "item_id": uuid4(),
        "job_id": uuid4(),
        "option_expiration_date": date(2026, 9, 23),
        "status": "running",
    }
    engine = _Engine([item])

    claimed = PostgresJobRepository(engine).claim_item(
        job_id=item["job_id"],
        worker_id="worker-1",
    )

    statement, _ = engine.connection.calls[0]
    sql = str(statement)
    assert statement is CLAIM_JOB_ITEM_SQL
    assert "cancellation_requested = FALSE" in sql
    assert "lease_expires_at >= CURRENT_TIMESTAMP" in sql
    assert "FOR UPDATE OF item SKIP LOCKED" in sql
    assert claimed["item_id"] == item["item_id"]


def test_complete_item_is_idempotent_and_bound_to_the_current_worker():
    job = _job_row(status="running", attempts=1, worker_id="worker-1")
    engine = _Engine([job])
    repository = PostgresJobRepository(engine)

    completed = repository.complete_item(
        job_id=job["job_id"],
        item_id=uuid4(),
        worker_id="worker-1",
        status="succeeded",
        result_id=uuid4(),
    )

    statement, parameters = engine.connection.calls[0]
    sql = str(statement)
    assert statement is COMPLETE_JOB_ITEM_SQL
    assert "worker_id = :worker_id" in sql
    assert "item.status = 'running'" in sql
    assert "completed_items = completed_items + 1" in sql
    assert parameters["status"] == "succeeded"
    assert completed.job_id == job["job_id"]

    with pytest.raises(ValueError, match="completion status"):
        repository.complete_item(
            job_id=job["job_id"],
            item_id=uuid4(),
            worker_id="worker-1",
            status="running",
        )


def test_cancel_complete_and_retry_sql_fail_closed():
    cancel_sql = str(REQUEST_CANCEL_SQL)
    complete_sql = str(COMPLETE_JOB_SQL)
    fail_sql = str(FAIL_JOB_SQL)

    assert "cancellation_requested = TRUE" in cancel_sql
    assert "status = 'cancelled'" in cancel_sql
    assert "lease_expires_at = NULL" in cancel_sql
    assert "worker_id = :worker_id" in complete_sql
    assert "cancellation_requested = FALSE" in complete_sql
    assert "attempts < max_attempts THEN 'queued'" in fail_sql
    assert "attempts >= max_attempts" in fail_sql


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"max_attempts": 0, "total_items": 1}, "max_attempts"),
        ({"max_attempts": 1, "total_items": -1}, "total_items"),
    ],
)
def test_submit_rejects_invalid_retry_or_progress_limits(arguments, message):
    repository = PostgresJobRepository(_Engine([]))
    with pytest.raises(ValueError, match=message):
        repository.submit(
            run_id=None,
            job_type="batch_calibration",
            payload={},
            idempotency_key="request-123",
            created_by="calibrator@example.com",
            **arguments,
        )
