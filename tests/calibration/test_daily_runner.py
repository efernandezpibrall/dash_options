"""Daily orchestration, durability and uncertain commits against owned PostgreSQL."""

from dataclasses import replace
from datetime import date
import shutil

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from options.vol_calibration import api
from scripts.verification.verify_settlement_pipeline import temporary_postgres, apply_migrations, fixture_inputs
from dash_options.calibration.execution import daily_runner as runner
from dash_options.calibration.auth import Identity, Role
from dash_options.calibration.execution.daily_store import DailyStore
from dash_options.calibration.execution.repository import PostgresJobRepository, JobStatus


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
    assert DailyStore(database).status(job.job_id)["stage"] == "prepared"
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


@pytest.mark.parametrize("cob", ["2026-10-05", "2026-10-06"])
def test_pin_inputs_uses_preceding_active_settlement_after_closed_days(
    database, authority, sources, monkeypatch, cob
):
    job = runner.submit_daily(database, "BRENT", sources.cob, identity=authority, mode="publish")
    assert runner.run_daily(database, job.job_id, identity=authority).status == JobStatus.SUCCEEDED
    publication_id = str(DailyStore(database).status(job.job_id)["publication_id"])
    with database.begin() as connection:
        connection.execute(text("""UPDATE at_lng.vol_surface_publications
            SET published_at='2026-10-06 12:00:00+00' WHERE publication_id=:id"""),
            {"id": publication_id})

    pinned, base, current = runner._pin_inputs(database, "BRENT", date.fromisoformat(cob))
    assert pinned is sources
    assert base["publication_id"] == publication_id
    assert base["settlement_cob"] == "2026-10-02"
    assert not current.get("publication_id")

    monkeypatch.setattr(api, "load_latest_hybrid_publication", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="preceding active publication could not be loaded"):
        runner._pin_inputs(database, "BRENT", date.fromisoformat(cob))


def test_prepare_then_publish_reuses_exact_owned_candidate(database, authority, sources, monkeypatch):
    prepared = runner.submit_daily(database, 'BRENT', sources.cob, identity=authority)
    assert runner.run_daily(database, prepared.job_id, identity=authority).status == JobStatus.SUCCEEDED
    publish = runner.submit_daily(database, 'BRENT', sources.cob, identity=authority, mode='publish')
    def must_not_fit(*args, **kwargs):
        pytest.fail('An exact accepted prepare candidate was fitted again')
    monkeypatch.setattr(api, 'build_daily_candidate', must_not_fit)
    result = runner.run_daily(database, publish.job_id, identity=authority)
    assert result.status == JobStatus.SUCCEEDED
    proof = DailyStore(database).get(publish.job_id, 'candidate_reuse')
    assert proof['source_job_id'] == str(prepared.job_id)
    candidate = DailyStore(database).get(publish.job_id, 'candidate')
    status = DailyStore(database).status(publish.job_id)
    assert api.verify_daily_readback(database, candidate, str(status['publication_id']))['point_count'] == len(candidate['surface'])
    repeated = runner.submit_daily(database, 'BRENT', sources.cob, identity=authority, mode='publish')
    assert runner.run_daily(database, repeated.job_id, identity=authority).status == JobStatus.SUCCEEDED
    assert DailyStore(database).get(repeated.job_id, 'publication_reuse')['publication_id'] == str(status['publication_id'])
    with database.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM at_lng.vol_surface_publications')).scalar_one() == 1


@pytest.mark.parametrize('changed', ['owner', 'worker', 'source', 'base', 'warm'])
def test_candidate_reuse_rejects_changed_identity(database, authority, sources, monkeypatch, changed):
    prepared = runner.submit_daily(database, 'BRENT', sources.cob, identity=authority)
    runner.run_daily(database, prepared.job_id, identity=authority)
    identity = authority
    if changed == 'owner':
        identity = replace(authority, subject='different-owner')
    elif changed == 'worker':
        monkeypatch.setattr(runner, 'worker_identity', lambda: 'different-worker')
    elif changed == 'source':
        inputs = replace(sources, identity='0' * 64)
        monkeypatch.setattr(runner, '_pin_inputs', lambda *args: (inputs, {}, {}))
    elif changed == 'warm':
        monkeypatch.setattr(runner, '_pin_inputs', lambda *args: (
            sources, {'warm_start_publication': {'publication_id': 'different-warm'}}, {}
        ))
    else:
        monkeypatch.setattr(runner, '_pin_inputs', lambda *args: (sources, {'publication_id':'different-base'}, {}))
    target = runner.submit_daily(database, 'BRENT', sources.cob, identity=identity, mode='publish')
    repo = PostgresJobRepository(database)
    claimed = repo.claim(job_id=target.job_id, worker_id='reuse-test', job_type=runner.JOB_TYPE)
    saved = DailyStore(database).get(target.job_id, 'inputs')
    assert DailyStore(database).reuse_candidate(claimed, target.payload['input_identity'], saved['base'].get('publication_id')) is None
    assert DailyStore(database).get(target.job_id, 'candidate') is None


def test_batch_continues_after_one_member_is_held(database, authority, sources, monkeypatch):
    first = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority, mode="prepare")
    second = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority, mode="publish")
    original = api.build_daily_candidate
    calls = []
    def fail_once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise ValueError("hard model rejection")
        return original(*args, **kwargs)
    monkeypatch.setattr(api, "build_daily_candidate", fail_once)
    completed = runner.run_daily_batch(database, [first.job_id, second.job_id], identity=authority)
    assert [job.status for job in completed] == [JobStatus.FAILED, JobStatus.SUCCEEDED]
    assert runner.describe_daily(database, first.job_id, identity=authority)["stage"]["stage"] == "held"


def test_missing_source_is_durable_and_can_pin_when_it_arrives(database, authority, sources, monkeypatch):
    original = api.load_daily_inputs
    def not_ready(*args, **kwargs):
        raise api.DailyInputsUnavailable("exact settlement has not arrived")
    monkeypatch.setattr(api, "load_daily_inputs", not_ready)
    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority, mode="publish")
    assert job.payload["awaiting_source"] and DailyStore(database).get(job.job_id, "inputs") is None
    assert runner.describe_daily(database, job.job_id, identity=authority)["stage"]["stage"] == "waiting"
    monkeypatch.setattr(api, "load_daily_inputs", original)
    completed = runner.run_daily(database, job.job_id, identity=authority)
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.total_items == len(sources.targets)


def test_old_attempt_cannot_finish_or_fail_a_reclaimed_worker(database):
    repo = PostgresJobRepository(database)
    job = repo.submit(run_id=None, job_type="fence-test", payload={}, idempotency_key="fence-test",
                      created_by="owner", max_attempts=3, total_items=0)
    first = repo.claim(worker_id="same-worker-name", job_id=job.job_id, job_type="fence-test", lease_seconds=300)
    with database.begin() as c:
        c.execute(text("UPDATE at_lng.vol_calibration_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:id"),
                  {"id": job.job_id})
    second = repo.claim(worker_id="same-worker-name", job_id=job.job_id, job_type="fence-test", lease_seconds=300)
    assert second.attempts == first.attempts + 1
    assert repo.complete(job_id=job.job_id, worker_id=first.worker_id, attempts=first.attempts) is None
    assert repo.fail(job_id=job.job_id, worker_id=first.worker_id, attempts=first.attempts, last_error="stale") is None
    assert repo.abort(job_id=job.job_id, worker_id=first.worker_id, attempts=first.attempts, last_error="stale") is None
    assert repo.complete(job_id=job.job_id, worker_id=second.worker_id, attempts=second.attempts).status == JobStatus.SUCCEEDED


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


def test_raw_bulk_timeout_exhaustion_audited_retry_reuses_candidate(database, authority, sources, monkeypatch):
    from psycopg2.errors import QueryCanceled
    from options.vol_calibration.publication import staging
    job = runner.submit_daily(database, 'BRENT', sources.cob, identity=authority, mode='publish')
    original = staging.copy_points
    def timeout(connection, *args, **kwargs):
        with connection.connection.driver_connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='20ms'")
            cursor.execute('SELECT pg_sleep(0.05)')
    monkeypatch.setattr(staging, 'copy_points', timeout)
    digest = None
    for attempt in range(3):
        with database.begin() as connection:
            connection.execute(text("UPDATE at_lng.vol_calibration_jobs SET next_attempt_at=clock_timestamp() WHERE job_id=:id"),
                               {'id':job.job_id})
        with pytest.raises(QueryCanceled):
            runner.run_daily(database, job.job_id, identity=authority)
        with database.connect() as connection:
            actual = connection.execute(text("SELECT digest FROM at_lng.vol_daily_artifacts WHERE job_id=:id AND artifact_key='candidate'"),
                                        {'id':job.job_id}).scalar_one()
        if digest is None:
            digest = actual
            monkeypatch.setattr(api, 'build_daily_candidate', lambda *args, **kwargs: pytest.fail('Retained candidate was refitted'))
        assert actual == digest
    status = runner.describe_daily(database, job.job_id, identity=authority)
    assert status['status'] == 'failed' and status['failure_kind'] == 'retryable' and status['attempts'] == 3
    assert '57014' in status['reason'] and status['stage']['stage'] != 'held'
    with pytest.raises(PermissionError, match='owner'):
        runner.retry_daily(database, job.job_id, identity=replace(authority,subject='another'), reason='not owner')
    with pytest.raises(ValueError, match='reason'):
        runner.retry_daily(database, job.job_id, identity=authority, reason=' ')
    resumed = runner.retry_daily(database, job.job_id, identity=authority, reason='Execution budget reviewed')
    assert resumed.status == JobStatus.QUEUED and resumed.max_attempts == 6
    with database.connect() as connection:
        audit = connection.execute(text('SELECT actor,reason,prior_attempts,prior_max_attempts,new_max_attempts FROM at_lng.vol_settlement_retry_audit')).one()
        assert tuple(audit) == (authority.subject, 'Execution budget reviewed', 3, 3, 6)
    monkeypatch.setattr(staging, 'copy_points', original)
    assert runner.run_daily(database, job.job_id, identity=authority).status == JobStatus.SUCCEEDED
    candidate = DailyStore(database).get(job.job_id, 'candidate')
    assert api.verify_daily_readback(database, candidate, str(DailyStore(database).status(job.job_id)['publication_id']))
    with database.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM at_lng.vol_surface_publications')).scalar_one() == 1


def test_raw_schema_failure_is_held_and_cannot_be_retried(database, authority, sources, monkeypatch):
    from psycopg2.errors import UndefinedTable
    job = runner.submit_daily(database, 'BRENT', sources.cob, identity=authority)
    def invalid(*args, **kwargs):
        with database.begin() as connection, connection.connection.driver_connection.cursor() as cursor:
            cursor.execute('SELECT * FROM deliberately_absent_private_table')
    monkeypatch.setattr(api, 'build_daily_candidate', invalid)
    with pytest.raises(UndefinedTable):
        runner.run_daily(database, job.job_id, identity=authority)
    status = runner.describe_daily(database, job.job_id, identity=authority)
    assert status['failure_kind'] == 'permanent' and status['stage']['stage'] == 'held'
    assert '42P01' in status['reason'] and 'deliberately_absent' not in str(status)
    with pytest.raises(ValueError, match='retryable execution'):
        runner.retry_daily(database, job.job_id, identity=authority, reason='attempt')


@pytest.mark.parametrize('changed', ['source','worker','base'])
def test_explicit_retry_rejects_changed_inputs_or_implementation(database, authority, sources, monkeypatch, changed):
    job = runner.submit_daily(database, 'BRENT', sources.cob, identity=authority)
    repo = PostgresJobRepository(database)
    claim = repo.claim(job_id=job.job_id, worker_id='crashed', job_type=runner.JOB_TYPE)
    with database.begin() as connection:
        connection.execute(text("UPDATE at_lng.vol_calibration_jobs SET attempts=max_attempts,lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:id"),
                           {'id':job.job_id})
    assert runner.run_daily(database, job.job_id, identity=authority).status == JobStatus.FAILED
    if changed == 'worker':
        monkeypatch.setattr(runner,'worker_identity',lambda:'changed')
    elif changed == 'source':
        def corrected(*args):
            raise ValueError('Source changed')
        monkeypatch.setattr(api,'verify_daily_source',corrected)
    else:
        artifact = DailyStore(database).get(job.job_id,'inputs')
        artifact['base']['publication_id'] = 'different-prior'
        original = DailyStore.get
        monkeypatch.setattr(DailyStore,'get',lambda self,key,name:artifact if name=='inputs' else original(self,key,name))
    with pytest.raises(ValueError, match='changed'):
        runner.retry_daily(database, job.job_id, identity=authority, reason='explicit')
    assert repo.get(job_id=claim.job_id).max_attempts == 3
    with database.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM at_lng.vol_settlement_retry_audit')).scalar_one() == 0


def test_cancelled_claim_cannot_save_checkpoint(database, authority, sources):
    from dash_options.calibration.execution.settlement_worker import LeaseLost

    job = runner.submit_daily(database, "BRENT", "2026-10-02", identity=authority)
    repo = PostgresJobRepository(database)
    claimed = repo.claim(worker_id="first", job_id=job.job_id, job_type=runner.JOB_TYPE, lease_seconds=300)
    repo.request_cancel(job_id=job.job_id, created_by=authority.subject)
    with pytest.raises(LeaseLost):
        DailyStore(database).save_checkpoints(claimed, [("2026-12-01", {"result": "not saved"})])
    assert DailyStore(database).checkpoints(job.job_id) == {}


def test_batched_checkpoint_identity_and_stale_lease(database, authority, sources):
    from dash_options.calibration.execution.settlement_worker import LeaseLost

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
