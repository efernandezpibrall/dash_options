"""Exercise settlement persistence and recovery in an owned temporary PostgreSQL.

No configured database is accessed. Source loading is replaced by a pinned
synthetic fixture; exact historical source/model replay is a separate check.
"""
import importlib.util
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import tempfile
from copy import deepcopy
from datetime import date, timedelta
from uuid import uuid4
from types import SimpleNamespace
from contextlib import contextmanager
from functools import wraps

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from alembic.script import ScriptDirectory
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from options.vol_calibration import api  # noqa: E402 - standalone CLI bootstrap
from dash_options.calibration.execution import settlement_worker as worker  # noqa: E402
from dash_options.calibration.execution.repository import PostgresJobRepository  # noqa: E402


def fixture_inputs():
    values = api.unpack_settlement(json.loads((ROOT / "tests/fixtures/calibration/brent_pinned_source.json").read_text()))
    values["manifest"]["code_identity"] = api.implementation_fingerprint()
    values["identity"] = api.settlement_identity(values["manifest"])
    return SimpleNamespace(**values)


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_external_calibration_fixture(connection):
    """Minimal external curve catalogue needed by the SOFR policy migration.

    This is owned-test setup, not a production migration or a market-data clone.
    Curve observations are supplied by the explicit captured input bundle.
    """
    connection.execute(text("""CREATE TABLE IF NOT EXISTS at_lng.option_discount_curve_definitions (
        curve_code text PRIMARY KEY, currency text NOT NULL, day_count text NOT NULL,
        compounding text NOT NULL, source_name text NOT NULL, active boolean NOT NULL
    )"""))


def apply_migrations(engine):
    """Follow Alembic's graph, excluding only external trade-schema revisions.

    The separate migration upgrade tests exercise every revision through the
    real Alembic environment, including those external-schema prerequisites.
    """
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    excluded = {"20260831_02", "20260831_04", "20260902_01"}
    migrations = [revision for revision in reversed(list(
        script.iterate_revisions("heads", "base", implicit_base=True)
    )) if revision.revision not in excluded]
    with engine.begin() as connection:
        for revision in migrations:
            prefix = revision.revision
            if prefix == "20261005_01":
                connection.execute(text("GRANT SELECT ON at_lng.ttf_vol_surface_publication_current TO PUBLIC"))
                connection.execute(text("COMMENT ON VIEW at_lng.ttf_vol_surface_publication_current IS 'preserved integration comment'"))
            if prefix == "20261028_01":
                prepare_external_calibration_fixture(connection)
            with Operations.context(MigrationContext.configure(connection)):
                revision.module.upgrade()
        assert connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_class c,LATERAL aclexplode(c.relacl) acl WHERE c.oid='at_lng.ttf_vol_surface_publication_current'::regclass AND acl.grantee=0 AND acl.privilege_type='SELECT')")).scalar_one()
        assert connection.execute(text("SELECT obj_description('at_lng.ttf_vol_surface_publication_current'::regclass,'pg_class')")).scalar_one() == "preserved integration comment"
    return [revision.revision for revision in migrations]


@contextmanager
def temporary_postgres():
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        raise RuntimeError("A local PostgreSQL installation is required")
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="settlement-integration-") as directory:
        cluster = Path(directory) / "database"
        subprocess.run([initdb, "-D", str(cluster), "-A", "trust", "-U", "settlement_test"], check=True, capture_output=True)
        subprocess.run([pg_ctl, "-D", str(cluster), "-l", str(Path(directory) / "postgres.log"), "-o",
                        f"-h 127.0.0.1 -p {port} -c unix_socket_directories=", "start"], check=True, capture_output=True)
        engine = create_engine(f"postgresql+psycopg2://settlement_test@127.0.0.1:{port}/postgres")
        try:
            yield engine
        finally:
            engine.dispose()
            subprocess.run([pg_ctl, "-D", str(cluster), "-m", "fast", "stop"], check=True, capture_output=True)


def restore_globals(function):
    @wraps(function)
    def restored(*args, **kwargs):
        targets = ((api, "load_settlement_inputs"), (api, "verify_settlement_source_inputs"), (worker.feature_flags, "settlement_jobs_enabled"),
                   (worker.feature_flags, "settlement_auto_publication_enabled"), (worker, "service_identity"),
                   (worker, "_save"))
        originals = [(owner, name, getattr(owner, name)) for owner, name in targets]
        try:
            return function(*args, **kwargs)
        finally:
            for owner, name, value in originals:
                setattr(owner, name, value)
    return restored


@restore_globals
def verify(engine):
    migrations = apply_migrations(engine)
    fixture = SimpleNamespace(inputs=fixture_inputs)
    inputs = fixture.inputs()
    inputs.snapshot_id = str(uuid4())
    inputs.manifest["snapshot_id"] = inputs.snapshot_id
    inputs.identity = api.settlement_identity(inputs.manifest)
    original_loader = api.load_settlement_inputs
    api.load_settlement_inputs = lambda *a, **k: deepcopy(inputs)
    # This verifier's source is synthetic; raw source SQL is independently
    # exercised by test_real_brent_source_loader. Preserve content-change checks.
    def verify_fixture_source(engine, manifest, **kwargs):
        api.assert_current_settlement_source(engine, inputs.product, inputs.cob, inputs.snapshot_id, lock=True)
        if api.settlement_identity(manifest) != inputs.identity:
            raise ValueError("Source changed before publication activation")
        return True
    api.verify_settlement_source_inputs = verify_fixture_source
    worker.feature_flags.settlement_jobs_enabled = lambda: True
    worker.feature_flags.settlement_auto_publication_enabled = lambda _: True
    worker.service_identity = lambda: worker.Identity("integration-service", frozenset({worker.Role.PUBLISHER}), True, "integration")
    repo = PostgresJobRepository(engine)
    checks = []
    def check(condition, name):
        assert condition, name
        checks.append(name)
        print("passed:", name, flush=True)
    def sql(statement, **parameters):
        with engine.begin() as connection:
            return connection.execute(text(statement), parameters)
    def scalar(statement, **parameters):
        with engine.connect() as connection:
            return connection.execute(text(statement), parameters).scalar_one()
    def new_source():
        inputs.snapshot_id = str(uuid4())
        inputs.manifest["snapshot_id"] = inputs.snapshot_id
        inputs.manifest["market_identity"] = api.settlement_identity(inputs.market)
        inputs.manifest["expected_targets"] = api.pack_settlement(inputs.targets)
        inputs.identity = api.settlement_identity(inputs.manifest)
        sql("""INSERT INTO at_lng.vol_market_snapshots (snapshot_id,commodity,business_date,observed_at,
            source_name,input_fingerprint,status,option_quote_count,forward_count,metadata)
            VALUES (:snapshot,'BRENT',:cob,clock_timestamp(),'fixture',:identity,'complete',0,0,'{}')""",
            snapshot=inputs.snapshot_id, cob=inputs.cob, identity=inputs.identity)
    new_source()
    submitted = worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id)
    duplicate = worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id)
    check(submitted.job_id == duplicate.job_id, "duplicate source submission reuses one durable job")
    job = worker.run_one(engine)
    check(job.status.value == "succeeded", "shadow job completes without publication")
    check(scalar("SELECT count(*) FROM at_lng.vol_surface_publications") == 0, "shadow has no publication side effects")
    check(len(repo.list_items(job_id=job.job_id)) == 2, "per-maturity checkpoints persist")
    new_source()
    pending = worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id)
    old = repo.claim(worker_id="old-owner", job_id=pending.job_id, job_type=worker.JOB_TYPE)
    sql("UPDATE at_lng.vol_calibration_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job", job=old.job_id)
    reclaimed = repo.claim(worker_id="new-owner", job_id=old.job_id, job_type=worker.JOB_TYPE)
    try:
        with engine.begin() as connection:
            worker._guard(connection, old)
        raise AssertionError("old owner was not fenced")
    except worker.LeaseLost:
        check(reclaimed.attempts == 2, "expired lease is reclaimed and the previous owner is fenced")
    worker._run_claimed(engine, repo, reclaimed)
    check(repo.get(job_id=reclaimed.job_id).status.value == "succeeded", "reclaimed worker finishes exact checkpoints")
    new_source()
    worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id)
    inputs.manifest["source_mutation"] = True
    inputs.identity = api.settlement_identity(inputs.manifest)
    result = worker.run_one(engine)
    check(result.status.value == "failed" and "changed" in result.last_error, "changed pinned inputs are held without numerical retry")
    new_source()
    cancelled = worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id)
    repo.request_cancel(job_id=cancelled.job_id)
    check(worker.run_one(engine) is None, "cancelled jobs cannot be claimed")
    new_source()
    cancelled = worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id)
    claimed = repo.claim(job_id=cancelled.job_id,worker_id="cancelled-owner",job_type=worker.JOB_TYPE)
    repo.request_cancel(job_id=cancelled.job_id)
    sql("UPDATE at_lng.vol_calibration_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job", job=claimed.job_id)
    worker.run_one(engine)
    check(repo.get(job_id=claimed.job_id).status.value == "cancelled", "cancelled running jobs terminate after a crashed worker")
    new_source()
    worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id, mode="publish")
    result = worker.run_one(engine)
    check(result.status.value == "failed" and "promoted" in result.last_error, "publication is held before explicit promotion")
    # Twenty independently certified sessions, under this exact implementation.
    sql("UPDATE at_lng.vol_settlement_promotion SET shadow_start_cob='2026-09-01' WHERE commodity='BRENT'")
    now = pd.Timestamp.now(tz="UTC")
    calendar = {"schema": 1, "commodity": "BRENT", "version": "synthetic-test-v1", "source": "synthetic integration fixture", "timezone": "UTC",
        "from_cob": "2026-09-01", "through_cob": "2026-10-02", "sessions": [
            {"cob": cob.isoformat(), "ready_at": (now-pd.Timedelta(hours=1)).isoformat(),
             "deadline_at": (now+pd.Timedelta(hours=1)).isoformat()}
            for cob in [date(2026,9,1)+timedelta(days=n) for n in range(20)] + [date(2026,10,2)]]}
    approver = worker.Identity("integration-approver", frozenset({worker.Role.APPROVER}), True, "integration")
    worker.operations.import_calendar(engine, calendar, identity=approver)
    for offset in range(20):
        inputs = fixture.inputs()
        inputs.cob = date(2026, 9, 1) + timedelta(days=offset)
        inputs.manifest["cob_date"] = inputs.cob.isoformat()
        current_t = (inputs.targets.option_expiration_date - pd.Timestamp(inputs.cob)).dt.days / 365.25
        times = dict(zip(inputs.targets.contract_date, current_t))
        inputs.market["iv"] *= np.sqrt(inputs.market.t / inputs.market.strip.map(times))
        inputs.market["t"] = inputs.market.strip.map(times)
        new_source()
        worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id)
        result = worker.run_one(engine)
        assert result.status.value == "succeeded", result.last_error
    inputs = fixture.inputs()
    new_source()
    worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id)
    assert worker.run_one(engine).status.value == "succeeded"
    worker.operations.approve_promotion(engine, "BRENT", inputs.cob,
        identity=approver, worker_identity=worker.worker_identity())
    worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id, mode="publish")
    # Simulate process failure immediately after the database publication commit.
    save = worker._save
    def crash_after_commit(engine, job, state, **kwargs):
        if state == "published":
            assert kwargs.get("candidate") is None, "Already retained candidate was uploaded again"
            raise RuntimeError("injected process failure after commit")
        return save(engine, job, state, **kwargs)
    worker._save = crash_after_commit
    result = worker.run_one(engine)
    print("after injected failure:", result.status.value, result.last_error, flush=True)
    check(result.status.value == "queued" and scalar("SELECT count(*) FROM at_lng.vol_surface_publications WHERE is_active") == 1,
          "failure after publication commit leaves a recoverable queued job")
    worker._save = save
    check(worker.run_one(engine) is None, "runtime retry waits for persisted backoff")
    sql("UPDATE at_lng.vol_calibration_jobs SET next_attempt_at=clock_timestamp() WHERE job_id=:job", job=result.job_id)
    publisher = api.publish_hybrid_surface
    def no_publication_replay(*args, **kwargs):
        raise AssertionError("Committed publication reached the writer after verified stage cleanup")
    api.publish_hybrid_surface = no_publication_replay
    try:
        result = worker.run_one(engine)
    finally:
        api.publish_hybrid_surface = publisher
    check(result.status.value == "succeeded" and scalar("SELECT count(*) FROM at_lng.vol_surface_publications") == 1,
          "restart recovers the committed publication without a duplicate")
    receipt = api.load_latest_hybrid_publication(engine, inputs.cob, commodity="BRENT", require_exact_cob=True, force_refresh=True)
    actual = api.hybrid_publication_frame(receipt)
    with engine.connect() as connection:
        encoded = connection.execute(text("SELECT candidate FROM at_lng.vol_settlement_candidates WHERE publication_id=:publication"), {"publication": receipt["publication_id"]}).scalar_one()
    candidate = api.unpack_settlement(encoded)
    for name in ("strike", "delta", "volatility", "total_variance", "working_forward"):
        np.testing.assert_array_equal(actual[name].to_numpy(float), candidate["surface"][name].to_numpy(float))
    check(True, "fresh publication consumer readback preserves all float bits")
    new_source()
    worker.submit_settlement(engine, "BRENT", inputs.cob, inputs.snapshot_id, mode="publish")
    result = worker.run_one(engine)
    check(result.status.value == "failed" and ("changed" in result.last_error or "superseded" in result.last_error),
          "a competing publication keeps the active revision and holds the stale job")
    check(scalar("SELECT count(*) FROM at_lng.vol_surface_publications") == 1,
          "rejected competing publication rolls back all draft writes")
    # Inject altered persisted values: all inserted rows must roll back before visibility.
    candidate = deepcopy(candidate)
    candidate["manifest"]["test_case"] = "readback-corruption"
    candidate["certificate"]["input_identity"] = api.settlement_identity(candidate["manifest"])
    binding = {k: v for k, v in candidate["certificate"].items() if k != "certificate_identity"}
    candidate["certificate"]["certificate_identity"] = api.settlement_identity(binding)
    sql("""CREATE FUNCTION at_lng.inject_point_corruption() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.volatility := NEW.volatility + 0.001; RETURN NEW; END $$;
        CREATE TRIGGER inject_point_corruption BEFORE INSERT ON at_lng.implied_volatility_surface_calibrated
        FOR EACH ROW EXECUTE FUNCTION at_lng.inject_point_corruption()""")
    def publish(key, expected=receipt["publication_id"]):
        return api.publish_hybrid_surface(engine, candidate["surface"], candidate["results"],
            commodity="BRENT", trading_date=inputs.cob, settlement_cob=inputs.cob,
            actor_subject="integration-manual", created_by="integration-manual", base_publication_id=expected,
            expected_current_publication_id=expected, idempotency_key=key,
            input_manifest=candidate["manifest"], validation_certificate=candidate["certificate"])
    try:
        publish("injected-corruption")
        raise AssertionError("altered points activated")
    except api.HybridPublicationError as exc:
        check("readback differs" in str(exc), "altered persisted values are rejected before activation")
    sql("DROP TRIGGER inject_point_corruption ON at_lng.implied_volatility_surface_calibrated")
    check(scalar("SELECT count(*) FROM at_lng.vol_surface_publications") == 1,
          "numerical corruption rolls back the complete publication transaction")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, f"concurrent:{index}") for index in range(2)]
        outcomes = []
        for future in futures:
            try:
                future.result()
                outcomes.append("published")
            except api.HybridPublicationError:
                outcomes.append("held")
    check(sorted(outcomes) == ["held", "published"], "concurrent publishers admit exactly one revision")
    check(scalar("SELECT count(*) FROM at_lng.vol_surface_publications WHERE is_active") == 1,
          "concurrent publication preserves one active revision")
    api.load_settlement_inputs = original_loader
    return {"passed": True, "checks": checks, "migrations": list(migrations)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, action="append", default=[],
                        help="Optional exact candidate from the installed artifact's read-only replay")
    args = parser.parse_args()
    with temporary_postgres() as engine:
        result = verify(engine)
        result["saved_candidates"] = []
        for path in args.candidate:
            candidate = api.unpack_settlement(json.loads(path.read_text()))
            api.verify_settlement_certificate(candidate["surface"], candidate["results"], candidate["manifest"], candidate["certificate"])
            product, cob = candidate["manifest"]["commodity"], candidate["manifest"]["cob_date"]
            with engine.connect() as connection:
                current = connection.execute(text("SELECT publication_id FROM at_lng.vol_surface_publications WHERE commodity=:product AND cob_date=:cob AND is_active"),
                    {"product": product, "cob": cob}).scalar_one_or_none()
            receipt = api.publish_hybrid_surface(engine, candidate["surface"], candidate["results"],
            commodity=product, trading_date=cob, settlement_cob=cob, actor_subject="integration-source-replay",
            created_by="integration-source-replay", base_publication_id=str(current) if current else None,
            expected_current_publication_id=str(current) if current else None,
            idempotency_key=f"saved:{candidate['certificate']['certificate_identity']}", input_manifest=candidate["manifest"],
            validation_certificate=candidate["certificate"])
            actual = api.hybrid_publication_frame(receipt)
            for name in ("strike", "delta", "volatility", "total_variance", "working_forward"):
                np.testing.assert_array_equal(actual[name].to_numpy(float), candidate["surface"][name].to_numpy(float))
            result["saved_candidates"].append({"product": product, "cob": cob, "points": len(actual),
            "expiries": len(receipt["expiry_results"]), "exact_readback": True})
        print(json.dumps(result, indent=2))



if __name__ == "__main__":
    main()
