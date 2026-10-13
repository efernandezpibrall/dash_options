"""Daily orchestration, durability and uncertain commits against owned PostgreSQL."""

from dataclasses import replace
import shutil

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from options.vol_calibration import api
from integration.verify_settlement_pipeline import temporary_postgres, apply_migrations, fixture_inputs
from vol_calibration import daily_runner as runner
from vol_calibration.auth import Identity, Role
from vol_calibration.daily_store import DailyStore
from vol_calibration.jobs import PostgresJobRepository, JobStatus


@pytest.fixture
def database(forbid_live_postgres_connections):
    if not shutil.which("initdb"):
        pytest.skip("Local PostgreSQL required")
    with temporary_postgres() as engine:
        forbid_live_postgres_connections.add(engine.url.port)
        apply_migrations(engine)
        yield engine


@pytest.fixture
def authority(monkeypatch):
    monkeypatch.setattr(runner.feature_flags, "settlement_jobs_enabled", lambda: True)
    monkeypatch.setattr(runner.feature_flags, "brent_publication_enabled", lambda: True)
    return Identity("daily-owner", frozenset({Role.PUBLISHER, Role.CALIBRATOR}), True, "integration")


@pytest.fixture
def sources(monkeypatch):
    # Controlled immutable numerical fixture; raw-source checks have separate
    # real-table integration coverage in test_settlement_postgres.
    inputs = api.restore_daily_inputs(vars(fixture_inputs()))
    monkeypatch.setattr(api, "load_daily_inputs", lambda *a, **kw: inputs)
    monkeypatch.setattr(api, "verify_daily_source", lambda *a: True)
    return inputs


def test_durable_publish_exact_readback_and_owner_fence(database, authority, sources):
    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority, mode="publish")
    assert DailyStore(database).get(job.job_id, "inputs") is not None
    with pytest.raises(PermissionError, match="owner"):
        runner.run_daily(database, job.job_id, identity=replace(authority, subject="other"))
    completed = runner.run_daily(database, job.job_id, identity=authority)
    assert completed.status == JobStatus.SUCCEEDED
    status = DailyStore(database).status(job.job_id)
    assert status["stage"] == "verified" and status["publication_id"]
    candidate = DailyStore(database).get(job.job_id, "candidate")
    assert api.verify_daily_readback(database, candidate, str(status["publication_id"]))["point_count"] == len(
        candidate["surface"]
    )
    assert runner.run_daily(database, job.job_id, identity=authority).status == JobStatus.SUCCEEDED
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 1


def test_committed_publication_survives_lost_response_and_resume(database, authority, sources, monkeypatch):
    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority, mode="publish")
    original = api.publish_hybrid_surface

    def committed_then_lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OperationalError("test lost commit acknowledgement", {}, Exception("lost"))

    monkeypatch.setattr(api, "publish_hybrid_surface", committed_then_lost)
    with pytest.raises(OperationalError):
        runner.run_daily(database, job.job_id, identity=authority)
    assert DailyStore(database).status(job.job_id)["stage"] == "publishing"

    def must_not_republish(*a, **kw):
        pytest.fail("Uncertain commit was republished")

    monkeypatch.setattr(api, "publish_hybrid_surface", must_not_republish)
    with database.begin() as c:
        c.execute(
            text("UPDATE at_lng.vol_calibration_jobs SET next_attempt_at=clock_timestamp() WHERE job_id=:job"),
            {"job": job.job_id},
        )
    assert runner.run_daily(database, job.job_id, identity=authority).status == JobStatus.SUCCEEDED
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 1


def test_prepare_is_idempotent_and_artifacts_are_immutable(database, authority, sources):
    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority)
    same = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority)
    assert same.job_id == job.job_id
    assert runner.run_daily(database, job.job_id, identity=authority).status == JobStatus.SUCCEEDED
    assert DailyStore(database).status(job.job_id)["stage"] == "accepted"
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 0
    with pytest.raises(Exception, match="immutable"), database.begin() as c:
        c.execute(
            text("UPDATE at_lng.vol_daily_artifacts SET digest=repeat('0',64) WHERE job_id=:job"), {"job": job.job_id}
        )


def test_source_correction_holds_without_publication(database, authority, sources, monkeypatch):
    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority, mode="publish")

    def changed(*a):
        raise ValueError("exact source changed")

    monkeypatch.setattr(api, "verify_daily_source", changed)
    with pytest.raises(ValueError, match="source changed"):
        runner.run_daily(database, job.job_id, identity=authority)
    assert DailyStore(database).status(job.job_id)["stage"] == "held"
    assert PostgresJobRepository(database).get(job_id=job.job_id).status == JobStatus.FAILED
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 0


def test_cancelled_claim_cannot_save_checkpoint(database, authority, sources):
    from vol_calibration.settlement_worker import LeaseLost

    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority)
    repo = PostgresJobRepository(database)
    claimed = repo.claim(worker_id="first", job_id=job.job_id, job_type=runner.JOB_TYPE, lease_seconds=300)
    repo.request_cancel(job_id=job.job_id, created_by=authority.subject)
    with pytest.raises(LeaseLost):
        DailyStore(database).save_checkpoints(claimed, [("2026-12-01", {"result": "not saved"})])
    assert DailyStore(database).checkpoints(job.job_id) == {}


def test_batched_checkpoint_identity_and_stale_lease(database, authority, sources):
    from vol_calibration.settlement_worker import LeaseLost

    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority)
    repo = PostgresJobRepository(database)
    first = repo.claim(worker_id="first", job_id=job.job_id, job_type=runner.JOB_TYPE, lease_seconds=300)
    records = [("2026-12-01", {"exact": float.fromhex("0x1.fffffffffffffp-3")}), ("2027-01-01", {"exact": 0.2})]
    store = DailyStore(database)
    store.save_checkpoints(first, records[:1])
    store.save_checkpoints(first, records[:1])
    assert store.checkpoints(job.job_id) == dict(records[:1])
    with pytest.raises(ValueError, match="different immutable"):
        store.save_checkpoints(first, [("2026-12-01", {"exact": 0.25})])
    with database.begin() as c:
        c.execute(
            text(
                "UPDATE at_lng.vol_calibration_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job"
            ),
            {"job": job.job_id},
        )
    successor = repo.claim(worker_id="successor", job_id=job.job_id, job_type=runner.JOB_TYPE, lease_seconds=300)
    assert successor.attempts == 2
    with pytest.raises(LeaseLost):
        store.save_checkpoints(first, records[1:])
    store.save_checkpoints(successor, records[1:])


def test_batch_retries_uncertain_commit_and_saves_history_evidence(database, authority, sources, monkeypatch):
    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority, mode="publish")
    original = api.publish_hybrid_surface
    calls = []

    def lost(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise OperationalError("lost response", {}, Exception("lost"))

    monkeypatch.setattr(api, "publish_hybrid_surface", lost)

    def allow_retry(_):
        with database.begin() as c:
            c.execute(
                text("UPDATE at_lng.vol_calibration_jobs SET next_attempt_at=clock_timestamp() WHERE job_id=:job"),
                {"job": job.job_id},
            )

    monkeypatch.setattr(runner.time, "sleep", allow_retry)
    result = runner.run_daily_batch(database, [job.job_id], identity=authority)
    assert len(calls) == 1 and result[0].status == JobStatus.SUCCEEDED
    assert DailyStore(database).get(job.job_id, "history_after")["unchanged"] is True
