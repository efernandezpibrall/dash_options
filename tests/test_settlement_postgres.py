"""Real PostgreSQL regression checks. No configured database is accessed."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import time
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from options.vol_calibration import api
from vol_calibration import settlement_worker as worker
from vol_calibration import settlement_operations as operations
from vol_calibration.jobs import PostgresJobRepository
from integration.verify_settlement_pipeline import fixture_inputs, verify, load, ROOT


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(worker.feature_flags, "settlement_jobs_enabled", lambda: True)
    monkeypatch.setattr(worker.feature_flags, "settlement_auto_publication_enabled", lambda _: True)
    monkeypatch.setattr(worker, "service_identity", lambda: actor(worker.Role.PUBLISHER))


def actor(role):
    return worker.Identity("postgres-regression", frozenset({role}), True, "integration")


def sql(engine, statement, **parameters):
    with engine.begin() as connection:
        return connection.execute(text(statement), parameters)


def source(engine, *, product="BRENT", cob=date(2026, 10, 2)):
    inputs = fixture_inputs()
    inputs.product, inputs.cob, inputs.snapshot_id = product, cob, str(uuid4())
    inputs.manifest.update(commodity=product, cob_date=cob.isoformat(), snapshot_id=inputs.snapshot_id)
    inputs.identity = api.settlement_identity(inputs.manifest)
    sql(engine, """INSERT INTO at_lng.vol_market_snapshots
        (snapshot_id,commodity,business_date,observed_at,source_name,input_fingerprint,status,option_quote_count,forward_count,metadata)
        VALUES (:id,:product,:cob,clock_timestamp(),'synthetic-fixture',:hash,'complete',0,0,'{}')""",
        id=inputs.snapshot_id, product="LNE" if product == "HH" else product, cob=cob, hash=inputs.identity)
    return inputs


def calendar(engine, dates, *, start=None, through=None, ready=None, deadline=None, product="BRENT"):
    now = pd.Timestamp.now(tz="UTC")
    manifest = {"schema": 1, "commodity": product, "version": "synthetic-test-v1", "source": "explicit synthetic fixture", "timezone": "UTC",
                "from_cob": (start or min(dates)).isoformat(), "through_cob": (through or max(dates)).isoformat(),
                "sessions": [{"cob": cob.isoformat(), "ready_at": (ready or now-pd.Timedelta(hours=1)).isoformat(),
                              "deadline_at": (deadline or now+pd.Timedelta(hours=1)).isoformat()} for cob in dates]}
    operations.import_calendar(engine, manifest, identity=actor(worker.Role.APPROVER))
    return manifest


def test_existing_pipeline_and_promotion_failures_are_covered_by_the_official_suite(blank_database):
    originals = (api.load_settlement_inputs, worker.feature_flags.settlement_jobs_enabled, worker.service_identity)
    assert verify(blank_database)["passed"]
    assert originals == (api.load_settlement_inputs, worker.feature_flags.settlement_jobs_enabled, worker.service_identity)
    engine = blank_database
    # A newer failed job has no candidate. It must not disappear from the gate.
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("""INSERT INTO at_lng.vol_calibration_jobs
            (job_type,status,payload,idempotency_key,created_by,max_attempts,total_items)
            SELECT job_type,'failed',payload,:key,created_by,max_attempts,total_items FROM at_lng.vol_calibration_jobs
            WHERE payload->>'mode'='shadow' AND payload->>'cob'='2026-10-02' ORDER BY created_at DESC LIMIT 1"""),
            {"key": str(uuid4())})
        with pytest.raises(ValueError, match="missing, pending or failed"):
            worker._promotion(connection, "BRENT")
        transaction.rollback()

    # Late replay/completion is useful diagnostically, but is not a timely shadow session.
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("""UPDATE at_lng.vol_calibration_jobs j SET finished_at=s.deadline_at+interval '1 second'
            FROM at_lng.vol_settlement_sessions s WHERE s.commodity='BRENT' AND s.cob_date='2026-09-20'
            AND j.payload->>'mode'='shadow' AND j.payload->>'cob'='2026-09-20'"""))
        with pytest.raises(ValueError, match="readiness/deadline"):
            worker._promotion(connection, "BRENT")
        transaction.rollback()
    # An expected day with no job/source/candidate cannot be replaced by an older success.
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("""INSERT INTO at_lng.vol_settlement_sessions
            (commodity,cob_date,ready_at,deadline_at,calendar_identity,calendar_version,calendar_source,imported_by)
            SELECT commodity,'2026-09-21',ready_at,deadline_at,calendar_identity,calendar_version,calendar_source,imported_by
            FROM at_lng.vol_settlement_sessions LIMIT 1"""))
        with pytest.raises(ValueError, match="missing, pending or failed"):
            worker._promotion(connection, "BRENT")
        transaction.rollback()
    # A candidate marked shadow still needs successful job completion.
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("""UPDATE at_lng.vol_calibration_jobs SET status='running'
            WHERE payload->>'mode'='shadow' AND payload->>'cob'='2026-09-20'"""))
        with pytest.raises(ValueError, match="missing, pending or failed"):
            worker._promotion(connection, "BRENT")
        transaction.rollback()


@pytest.mark.parametrize("product", ["BRENT", "HH"])
def test_superseded_queued_sources_are_held_before_fitting(database, enabled, monkeypatch, product):
    old = source(database, product=product)
    monkeypatch.setattr(api, "load_settlement_inputs", lambda *a, **k: deepcopy(old))
    queued = worker.submit_settlement(database, product, old.cob, old.snapshot_id, mode="publish")
    source(database, product=product)
    monkeypatch.setattr(api, "build_settlement_candidate", lambda *a, **k: pytest.fail("Superseded input reached the fitter"))
    result = worker.run_one(database)
    assert result.job_id == queued.job_id and result.status.value == "failed"
    assert "superseded" in result.last_error
    assert sql(database, "SELECT count(*) FROM at_lng.vol_surface_publications").scalar_one() == 0


def test_correction_after_final_preparation_is_rechecked_inside_activation(database, enabled, monkeypatch):
    old = source(database)
    monkeypatch.setattr(api, "load_settlement_inputs", lambda *a, **k: deepcopy(old))
    worker.submit_settlement(database, "BRENT", old.cob, old.snapshot_id, mode="publish")
    def corrected_actor():
        source(database)
        return actor(worker.Role.PUBLISHER)
    monkeypatch.setattr(worker, "service_identity", corrected_actor)
    result = worker.run_one(database)
    assert result.status.value == "failed" and "superseded" in result.last_error
    assert sql(database, "SELECT count(*) FROM at_lng.vol_surface_publications").scalar_one() == 0


def test_snapshot_activation_lock_prevents_a_phantom_correction(database):
    old = source(database)
    with database.connect() as activation:
        transaction = activation.begin()
        api.assert_current_settlement_source(operations.ConnectionEngine(activation), "BRENT", old.cob, old.snapshot_id, lock=True)
        with pytest.raises(OperationalError) as error:
            with database.begin() as ingestion:
                ingestion.execute(text("SET LOCAL lock_timeout='200ms'"))
                ingestion.execute(text("""INSERT INTO at_lng.vol_market_snapshots
                    (snapshot_id,commodity,business_date,observed_at,source_name,input_fingerprint,status,option_quote_count,forward_count,metadata)
                    VALUES (gen_random_uuid(),'BRENT',:cob,clock_timestamp(),'fixture',:hash,'complete',0,0,'{}')"""),
                    {"cob": old.cob, "hash": str(uuid4())})
        assert error.value.orig.pgcode == "55P03"
        transaction.rollback()
    source(database)  # Ingestion can commit as soon as activation releases its lock.


def test_exhausted_retry_preserves_checkpoints_and_audits_authorized_recovery(database, enabled, monkeypatch):
    inputs = source(database)
    monkeypatch.setattr(api, "load_settlement_inputs", lambda *a, **k: deepcopy(inputs))
    worker.submit_settlement(database, "BRENT", inputs.cob, inputs.snapshot_id)
    save = worker._save
    def transient_failure(*args, **kwargs):
        if args[2] == "prepared":
            raise RuntimeError("temporary execution failure after checkpointing")
        return save(*args, **kwargs)
    monkeypatch.setattr(worker, "_save", transient_failure)
    repo = PostgresJobRepository(database)
    for attempt in range(3):
        result = worker.run_one(database)
        assert result.attempts == attempt+1
        assert worker.run_one(database) is None
        sql(database, "UPDATE at_lng.vol_calibration_jobs SET next_attempt_at=clock_timestamp() WHERE job_id=:id", id=result.job_id)
    assert result.status.value == "failed"
    checkpoints = repo.list_items(job_id=result.job_id)
    with pytest.raises(PermissionError):
        operations.retry_settlement(database, result.job_id, identity=actor(worker.Role.VIEWER), reason="outage repaired", worker_identity=worker.worker_identity())
    operations.retry_settlement(database, result.job_id, identity=actor(worker.Role.CALIBRATOR), reason="outage repaired", worker_identity=worker.worker_identity())
    assert repo.list_items(job_id=result.job_id) == checkpoints
    assert sql(database, "SELECT count(*) FROM at_lng.vol_settlement_retry_audit WHERE job_id=:id", id=result.job_id).scalar_one() == 1
    monkeypatch.setattr(worker, "_save", save)
    recovered = worker.run_one(database)
    assert recovered.job_id == result.job_id and recovered.status.value == "succeeded" and recovered.attempts == 4
    with pytest.raises(ValueError, match="Only failed"):
        operations.retry_settlement(database, result.job_id, identity=actor(worker.Role.CALIBRATOR), reason="duplicate", worker_identity=worker.worker_identity())


def test_migration_timeout_applies_to_its_first_locking_statement(database):
    migration = load(next((ROOT / "migrations/versions").glob("20261005_01*.py")))
    with database.connect() as reader:
        transaction = reader.begin()
        reader.execute(text("SELECT * FROM at_lng.ttf_vol_surface_publication_current LIMIT 0"))
        start = time.monotonic()
        with pytest.raises(OperationalError) as error:
            with database.begin() as connection:
                connection.execute(text("SET LOCAL statement_timeout='6500ms'"))
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
        assert error.value.orig.pgcode == "55P03"
        assert 4.5 <= time.monotonic()-start < 6.5
        transaction.rollback()


def test_calendar_waiting_deadline_alert_and_immutable_import(database, enabled, monkeypatch, caplog):
    now = pd.Timestamp.now(tz="UTC")
    cob = date(2026, 10, 2)
    through = (now+pd.Timedelta(days=30)).date()
    missing = calendar(database, [cob], through=through, product="HH", deadline=now-pd.Timedelta(minutes=1))
    future = source(database)
    calendar(database, [cob], through=through, ready=now+pd.Timedelta(hours=1), deadline=now+pd.Timedelta(hours=2))
    monkeypatch.setattr(api, "load_settlement_inputs", lambda *a, **k: pytest.fail("Future session was scheduled early"))
    assert worker.reconcile(database, from_cob=cob) == []
    assert sql(database, "SELECT state FROM at_lng.vol_settlement_source_events WHERE event_identity=:key", key=f"expected:HH:{cob}").scalar_one() == "waiting"
    assert operations.refresh_alerts(database, from_cob=cob, mode="shadow") == 1
    first = sql(database, "SELECT updated_at FROM at_lng.vol_settlement_alerts WHERE is_active").scalar_one()
    operations.refresh_alerts(database, from_cob=cob, mode="shadow")
    assert sql(database, "SELECT updated_at FROM at_lng.vol_settlement_alerts WHERE is_active").scalar_one() == first
    with database.connect() as connection, pytest.raises(ValueError, match="outside"):
        operations.require_session(connection, "HH", cob)
    operations.import_calendar(database, missing, identity=actor(worker.Role.APPROVER))
    changed = deepcopy(missing)
    changed["sessions"][0]["deadline_at"] = (now+pd.Timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="Reviewed session changed"):
        operations.import_calendar(database, changed, identity=actor(worker.Role.APPROVER))
    assert future.snapshot_id


def test_real_brent_source_loader_pins_quotes_forwards_calendar_and_rejects_correction(database, monkeypatch):
    from options import option_expiry_engine, options_library
    from options.vol_calibration.models.svi_numeric import SVIParams
    from options.vol_calibration.config.brent_sources import PRICING_MODEL, PRICING_MODEL_VERSION
    import json
    mapping = option_expiry_engine.get_surface_calendar_mapping("BRENT")
    data = Path(option_expiry_engine.__file__).parent / "data/option_contract_expiries.csv"
    reference = pd.read_csv(data)
    known = reference.loc[reference.expiry_convention_code.eq(mapping.expiry_convention_code)].to_dict("records")
    with database.begin() as connection:
        connection.execute(text("""CREATE TABLE at_lng.option_contract_expiries
            (expiry_convention_code text,expiry_convention_version text,contract_month date,option_expiration_date date,expiry_status text);
            CREATE TABLE at_lng.mapping_volatility_surface_calendar
            (surface_product text,expiry_convention_code text,contract_convention_code text,variance_calendar_code text);
            CREATE TABLE at_lng.bbg_option_chain (snapshot_id uuid)"""))
        connection.execute(text("INSERT INTO at_lng.option_contract_expiries VALUES (:expiry_convention_code,:expiry_convention_version,:contract_month,:option_expiration_date,:expiry_status)"), known)
        connection.execute(text("INSERT INTO at_lng.mapping_volatility_surface_calendar VALUES ('BRENT',:expiry,:contract,:calendar)"),
            {"expiry": mapping.expiry_convention_code, "contract": mapping.contract_convention_code, "calendar": mapping.variance_calendar_code})
        snapshot, cob = str(uuid4()), date(2026,10,2)
        quotes = []
        for month in pd.date_range("2026-12-01", periods=2, freq="MS"):
            expiry = option_expiry_engine.resolve_surface_expiry("BRENT", month, known).option_expiration_date
            t = (expiry-cob).days/365.25
            params = SVIParams(.005,.015,-.2,0.,.2)
            for ratio in np.linspace(.85,1.2,20):
                strike, side = float(100*ratio), "P" if ratio<1 else "C"
                iv = float(np.sqrt(params.total_variance(np.log(ratio))/t))
                price = options_library.american_on_futures_futures_style_price(side,100,strike,t,iv,400)
                quotes.append({"id": snapshot,"month": month.date(),"expiry": expiry,"side": side,"strike": strike,"price": price,"iv": iv})
        months = pd.date_range("2026-10-01","2031-12-01",freq="MS")
        metadata = {"snapshot_kind":"SETTLEMENT","pricing_model":PRICING_MODEL,"pricing_model_version":PRICING_MODEL_VERSION,
                    "settlement_quality":{"price_status":"complete","activity_status":"oi_pending"}}
        connection.execute(text("""INSERT INTO at_lng.vol_market_snapshots
            (snapshot_id,commodity,business_date,observed_at,source_name,input_fingerprint,status,option_quote_count,forward_count,metadata)
            VALUES (:id,'BRENT',:cob,clock_timestamp(),'Bloomberg synthetic source',:hash,'complete',:quotes,:forwards,CAST(:metadata AS jsonb))"""),
            {"id":snapshot,"cob":cob,"hash":snapshot,"quotes":len(quotes),"forwards":len(months),"metadata":json.dumps(metadata)})
        connection.execute(text("""INSERT INTO at_lng.vol_market_option_quotes
            (snapshot_id,contract_date,option_expiration_date,put_call,strike,mark_price,mark_iv,ingested_at)
            VALUES (:id,:month,:expiry,:side,:strike,:price,:iv,clock_timestamp())"""), quotes)
        connection.execute(text("INSERT INTO at_lng.bbg_option_chain VALUES (:id)"), [{"id":snapshot} for _ in quotes])
        connection.execute(text("""INSERT INTO at_lng.vol_market_forwards
            (snapshot_id,contract_date,forward,ingested_at) VALUES (:id,:month,100,clock_timestamp())"""),
            [{"id":snapshot,"month":month.date()} for month in months])
    inputs = api.load_settlement_inputs(database,"BRENT",cob,snapshot)
    assert len(inputs.market) == 40 and len(inputs.targets) == 61
    assert inputs.manifest["source"]["activity_status"] == "oi_pending"
    assert api.load_settlement_inputs(database,"BRENT",cob,snapshot).identity == inputs.identity
    with database.begin() as connection:
        api.assert_current_settlement_source(operations.ConnectionEngine(connection), "BRENT", cob, snapshot, lock=True)
        assert api.load_settlement_inputs(operations.ConnectionEngine(connection),"BRENT",cob,snapshot).identity == inputs.identity
        from options.vol_calibration import settlement
        with monkeypatch.context() as patch:
            patch.setattr(settlement, "prepare_brent_loaded_settlement",
                          lambda *a, **k: pytest.fail("Verification repeated pricing preparation"))
            assert api.verify_settlement_source_inputs(operations.ConnectionEngine(connection), inputs.manifest)
            from sqlalchemy.exc import InternalError
            with pytest.raises(InternalError, match="append-only"), connection.begin_nested():
                connection.execute(text("UPDATE at_lng.vol_market_forwards SET forward=101 WHERE snapshot_id=:id"), {"id":snapshot})
            changed_manifest = {**inputs.manifest, "forwards_identity": "changed"}
            with pytest.raises(ValueError, match="changed"):
                api.verify_settlement_source_inputs(operations.ConnectionEngine(connection), changed_manifest)
    api.assert_current_settlement_source(database,"BRENT",cob,snapshot)
    source(database, cob=cob)
    with pytest.raises(ValueError, match="superseded"):
        api.assert_current_settlement_source(database,"BRENT",cob,snapshot)
