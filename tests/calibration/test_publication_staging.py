"""Publication staging under interruption, source correction and real lease renewal."""

import time
from datetime import date
from uuid import uuid4

import pytest
from options.vol_calibration import api
from options.vol_calibration.publication import staging
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.helpers.publication import publish

from scripts.verification.verify_settlement_pipeline import fixture_inputs
from dash_options.calibration.execution import settlement_worker
from dash_options.calibration.execution.repository import PostgresJobRepository


def test_byte_bounded_chunks_preserve_whole_expiries_and_unicode(monkeypatch):
    from options.vol_calibration.publication.transfer import prepare_copy
    monkeypatch.setattr(staging, "CHUNK_TARGET_BYTES", 170)
    groups = {month: [{"month": month, "strike": strike, "label": "é" * month}
                      for strike in (90., 100., 110.)] for month in range(1, 20)}
    chunks = list(staging._expiry_chunks(groups, ("month", "strike", "label")))
    assert [month for chunk in chunks for month in chunk] == list(groups)
    assert all(len(chunk) <= staging.CHUNK_MAX_EXPIRIES for chunk in chunks)
    assert len(chunks) > 2  # Byte limit binds before the expiry-count limit.
    for chunk in chunks:
        rows = [row for month in chunk for row in groups[month]]
        wire = prepare_copy(rows, ("month", "strike", "label")).byte_count
        assert wire <= staging.CHUNK_TARGET_BYTES or len(chunk) == 1


def test_completed_chunks_survive_failure_and_retry_does_not_reupload(database, monkeypatch):
    candidate = api.build_settlement_candidate(fixture_inputs())
    key = str(uuid4())
    before = api.historical_content_snapshot(database)

    def source_changed(connection):
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_publication_stage_points")).scalar() == len(candidate["surface"])
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 0
        raise ValueError("source corrected before activation")

    with pytest.raises(ValueError, match="source corrected"):
        publish(database, candidate, key, stage_before_activation=True, transaction_guard=source_changed)
    assert api.historical_content_snapshot(database) == before

    def must_not_upload(*args, **kwargs):
        pytest.fail("A committed chunk was uploaded again")

    monkeypatch.setattr(staging, "copy_points", must_not_upload)
    receipt = publish(database, candidate, key, stage_before_activation=True, return_surface=False)
    api.verify_daily_readback(database, candidate, receipt["publication_id"])
    again = publish(database, candidate, key, stage_before_activation=True, return_surface=False)
    assert again["publication_id"] == receipt["publication_id"]
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 1


def test_heartbeat_renews_while_upload_exceeds_original_lease(database, monkeypatch):
    candidate = api.build_settlement_candidate(fixture_inputs())
    repo = PostgresJobRepository(database)
    job = repo.submit(run_id=None, job_type="staging-test", payload={},
                      idempotency_key=str(uuid4()), created_by="integration", max_attempts=3, total_items=1)
    job = repo.claim(worker_id="test-owner", job_id=job.job_id, job_type="staging-test", lease_seconds=2)
    monkeypatch.setattr(settlement_worker, "LEASE_SECONDS", 2)
    original = staging.copy_points

    def slow_upload(*args, **kwargs):
        time.sleep(2.5)
        return original(*args, **kwargs)

    monkeypatch.setattr(staging, "copy_points", slow_upload)
    with settlement_worker._heartbeat(repo, job) as lost:
        receipt = publish(database, candidate, str(uuid4()), stage_before_activation=True,
                          staging_guard=lambda c: settlement_worker._guard(c, job, lock=False),
                          transaction_guard=lambda c: settlement_worker._guard(c, job),
                          staging_job_id=job.job_id, return_surface=False)
        assert not lost.is_set()
    assert receipt["row_count"] == len(candidate["surface"])
    assert repo.heartbeat(job_id=job.job_id, worker_id=job.worker_id,
                          attempts=job.attempts - 1, lease_seconds=2) is None


def test_cancel_during_stage_cannot_activate(database, monkeypatch):
    candidate = api.build_settlement_candidate(fixture_inputs())
    repo = PostgresJobRepository(database)
    job = repo.submit(run_id=None, job_type="staging-test", payload={},
                      idempotency_key=str(uuid4()), created_by="integration", max_attempts=3, total_items=1)
    job = repo.claim(worker_id="test-owner", job_id=job.job_id, job_type="staging-test", lease_seconds=300)
    original = staging.copy_points

    def cancelled_upload(*args, **kwargs):
        result = original(*args, **kwargs)
        repo.request_cancel(job_id=job.job_id, created_by="integration")
        return result

    monkeypatch.setattr(staging, "copy_points", cancelled_upload)
    with pytest.raises(settlement_worker.LeaseLost):
        publish(database, candidate, str(uuid4()), stage_before_activation=True,
                staging_guard=lambda c: settlement_worker._guard(c, job, lock=False),
                transaction_guard=lambda c: settlement_worker._guard(c, job),
                staging_job_id=job.job_id)
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 0
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_publication_stage_points")).scalar() == 0


def test_owner_scoped_retention_and_verified_stage_release(database):
    candidate = api.build_settlement_candidate(fixture_inputs())
    def stop(_connection):
        raise ValueError("source changed")
    with pytest.raises(ValueError):
        publish(database, candidate, "abandoned-stage", stage_before_activation=True, transaction_guard=stop)
    with database.begin() as connection:
        connection.execute(text("UPDATE at_lng.vol_publication_stages SET updated_at=clock_timestamp()-interval '8 days'"))
    assert api.prune_abandoned_stages(database, actor="another-owner")["count"] == 0
    assert api.prune_abandoned_stages(database, actor="integration")["count"] == 1
    assert api.prune_abandoned_stages(database, actor="integration", dry_run=False)["count"] == 1
    receipt = publish(database, candidate, "verified-stage", stage_before_activation=True, return_surface=False)
    with database.begin() as connection:
        connection.execute(text("UPDATE at_lng.vol_publication_stages SET updated_at=clock_timestamp()-interval '8 days'"))
    assert api.prune_abandoned_stages(database, actor="integration", dry_run=False)["count"] == 0
    before = api.historical_content_snapshot(database)
    with pytest.raises(PermissionError):
        api.release_verified_stage(database, receipt["publication_id"], actor="another-owner")
    assert api.release_verified_stage(database, receipt["publication_id"], actor="integration")
    assert not api.release_verified_stage(database, receipt["publication_id"], actor="integration")
    assert api.historical_content_snapshot(database) == before


def test_interrupted_second_chunk_resumes_only_remaining_expiries(database, monkeypatch):
    from options.vol_calibration.publication.integrity import point_content_digest
    from options.vol_calibration.publication.service import _SURFACE_INSERT_COLUMNS

    publication, run = str(uuid4()), str(uuid4())
    rows = [{
        "publication_id": publication, "run_id": run, "commodity": "BRENT",
        "cob_date": date(2026, 10, 2), "contract_date": date(2027 + month // 12, month % 12 + 1, 1),
        "option_expiration_date": date(2026, 12, 15), "strike": strike, "delta": .5,
        "put_call": "C", "volatility": .5, "total_variance": .1, "working_forward": 100.,
        "surface_region": "core", "blend_classification": "observed", "calibration_basis": "observed",
        "source_name": "chunk-test", "calibration_method": "single-svi",
        "calibration_policy_version": "chunk-test-v1", "input_fingerprint": "a" * 64,
    } for month in range(17) for strike in (90., 110.)]
    arguments = dict(key="two-chunks", product="BRENT", actor="integration", created_by="integration",
                     configuration={}, results=[], rows=rows, columns=_SURFACE_INSERT_COLUMNS,
                     expected_digest=point_content_digest(rows), expected_current=None,
                     timeouts={"lock_timeout": 5000, "statement_timeout": 120000,
                               "idle_in_transaction_session_timeout": 60000})
    original = staging.copy_points
    copied = []

    def interrupted(connection, payload, **kwargs):
        copied.append(payload.row_count)
        result = original(connection, payload, **kwargs)
        if len(copied) == 2:
            raise OSError("interrupted second chunk")
        return result

    monkeypatch.setattr(staging, "copy_points", interrupted)
    with pytest.raises(OSError, match="second chunk"):
        staging.prepare_stage(database, **arguments)
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_publication_stage_points")).scalar_one() == 32
        assert not connection.execute(text("SELECT ready FROM at_lng.vol_publication_stages")).scalar_one()
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar_one() == 0

    copied.clear()
    def resumed(connection, payload, **kwargs):
        copied.append(payload.row_count)
        return original(connection, payload, **kwargs)
    monkeypatch.setattr(staging, "copy_points", resumed)
    assert staging.prepare_stage(database, **arguments)[:2] == (publication, run)
    assert copied == [2]
    for statement in (
        "UPDATE at_lng.vol_publication_stage_points SET volatility=.6",
        "DELETE FROM at_lng.vol_publication_stage_points",
        "UPDATE at_lng.vol_publication_stages SET ready=FALSE",
    ):
        with pytest.raises(DBAPIError, match="immutable|cannot be reopened"), database.begin() as connection:
            connection.execute(text(statement))
    # Owner retention deletes the parent and is allowed to cascade its points.
    with database.begin() as connection:
        connection.execute(text("UPDATE at_lng.vol_publication_stages SET updated_at=clock_timestamp()-interval '8 days'"))
    assert api.prune_abandoned_stages(database, actor="integration", dry_run=False)["count"] == 1
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_publication_stage_points")).scalar_one() == 0
