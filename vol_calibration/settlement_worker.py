"""Durable settlement execution; all analytics enter through the public API.

Run as a separate process. Default operation is shadow; publication also needs
explicit service authority, a product flag and the database promotion gate.
"""
from contextlib import contextmanager
from datetime import date
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import threading
import time
from uuid import uuid4

import pandas as pd
from sqlalchemy import text
from options.vol_calibration import api

from runtime_config import get_database_engine, config_value
from . import feature_flags
from .auth import Identity, Role, Permission, authorize
from .jobs import PostgresJobRepository
from . import settlement_operations as operations
from .settlement_operations import ConnectionEngine as _ConnectionEngine
from sqlalchemy.exc import SQLAlchemyError

JOB_TYPE = "brent_hh_settlement_v1"
LEASE_SECONDS = 300
LOGGER = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    pass


def worker_identity():
    fingerprint = hashlib.sha256()
    for name in ("settlement_worker.py", "settlement_operations.py", "jobs.py", "auth.py", "feature_flags.py"):
        fingerprint.update(name.encode())
        fingerprint.update(Path(__file__).with_name(name).read_bytes())
    return fingerprint.hexdigest()


def service_identity():
    subject = os.getenv("VOL_CALIBRATION_SETTLEMENT_SERVICE_ACTOR") or config_value(
        "VOL_CALIBRATION", "SETTLEMENT_SERVICE_ACTOR", fallback="")
    role = os.getenv("VOL_CALIBRATION_SETTLEMENT_SERVICE_ROLE") or config_value(
        "VOL_CALIBRATION", "SETTLEMENT_SERVICE_ROLE", fallback="")
    if not str(subject or "").strip() or role != "publisher":
        raise PermissionError("Explicit settlement service actor and publisher role are required")
    identity = Identity(str(subject), frozenset({Role.PUBLISHER}), True, "configured_settlement_service")
    authorize(identity, Permission.PUBLISH)
    return identity


def _guard(connection, job):
    row = connection.execute(text("""SELECT job_id FROM at_lng.vol_calibration_jobs
        WHERE job_id=:job_id AND worker_id=:worker_id AND attempts=:attempts
        AND status='running' AND NOT cancellation_requested
        AND lease_expires_at > clock_timestamp() FOR UPDATE"""),
        {"job_id": job.job_id, "worker_id": job.worker_id, "attempts": job.attempts}).first()
    if row is None:
        raise LeaseLost("Settlement job lost its lease, was cancelled or was reclaimed")


def submit_settlement(engine, product, cob, snapshot_id, *, mode="shadow", source_event_identity=None):
    if not feature_flags.settlement_jobs_enabled():
        raise PermissionError("Settlement jobs are disabled")
    if mode not in {"shadow", "publish"}:
        raise ValueError("Settlement mode must be shadow or publish")
    inputs = api.load_settlement_inputs(engine, product, cob, snapshot_id)
    payload = {"schema": 1, "job_type": JOB_TYPE, "product": inputs.product, "cob": inputs.cob.isoformat(),
        "snapshot_id": inputs.snapshot_id, "input_identity": inputs.identity,
        "worker_identity": worker_identity(), "mode": mode,
        "code_identity": api.implementation_fingerprint(), "source_event_identity": source_event_identity,
        "expected_current_publication_id": None}
    key = api.settlement_identity(payload)
    return PostgresJobRepository(engine).submit(run_id=None, job_type=JOB_TYPE, payload=payload,
        idempotency_key=f"settlement:{key}", created_by="settlement-worker", max_attempts=3,
        total_items=len(inputs.targets), item_dates=tuple(pd.to_datetime(inputs.targets.option_expiration_date).dt.date))


def _save(engine, job, state, *, candidate=None, diagnostics=None, publication_id=None):
    with engine.begin() as connection:
        _guard(connection, job)
        connection.execute(text("""INSERT INTO at_lng.vol_settlement_candidates
            (job_id,commodity,cob_date,input_identity,state,candidate,diagnostics,publication_id)
            VALUES (:job_id,:product,:cob,:identity,:state,CAST(:candidate AS jsonb),CAST(:diagnostics AS jsonb),:publication_id)
            ON CONFLICT (job_id) DO UPDATE SET state=EXCLUDED.state,
            candidate=COALESCE(EXCLUDED.candidate,vol_settlement_candidates.candidate),
            diagnostics=EXCLUDED.diagnostics, publication_id=EXCLUDED.publication_id, updated_at=clock_timestamp()"""),
            {"job_id": job.job_id, "product": job.payload["product"], "cob": job.payload["cob"],
             "identity": job.payload["input_identity"], "state": state,
             "candidate": json.dumps(api.pack_settlement(candidate), allow_nan=False) if candidate else None,
             "diagnostics": json.dumps(api.pack_settlement(diagnostics or {}), allow_nan=False), "publication_id": publication_id})


@contextmanager
def _heartbeat(repo, job):
    stop = threading.Event()
    lost = threading.Event()
    def beat():
        while not stop.wait(30):
            try:
                if repo.heartbeat(job_id=job.job_id, worker_id=job.worker_id, lease_seconds=LEASE_SECONDS) is None:
                    lost.set()
                    return
            except Exception:
                lost.set()
                return
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        stop.set()
        thread.join(timeout=1)


def _promotion(connection, product):
    gate = connection.execute(text("SELECT * FROM at_lng.vol_settlement_promotion WHERE commodity=:product FOR SHARE"),
                              {"product": product}).mappings().one()
    if not gate["approved"]:
        raise ValueError(f"{product} automatic publication has not been promoted")
    evidence = operations.shadow_evidence(connection, product, start=gate["shadow_start_cob"],
        through=gate["evidence_through_cob"], count=gate["required_sessions"], worker_identity=worker_identity())
    if evidence != gate["evidence_identity"]:
        raise ValueError("Approved shadow evidence changed; review promotion again")


def _run_claimed(engine, repo, job):
    payload = job.payload
    if payload.get("schema") != 1 or payload.get("job_type") != JOB_TYPE or payload.get("worker_identity") != worker_identity():
        raise ValueError("Unsupported or stale settlement job; submit with the current implementation")
    with _heartbeat(repo, job) as lost:
        api.assert_current_settlement_source(engine, payload["product"], payload["cob"], payload["snapshot_id"])
        inputs = api.load_settlement_inputs(engine, payload["product"], payload["cob"], payload["snapshot_id"])
        if inputs.identity != payload["input_identity"]:
            raise ValueError("Pinned settlement source, calendar, policy or code changed")
        cache = {}
        items = repo.list_items(job_id=job.job_id)
        expiry_for_month = dict(zip(pd.to_datetime(inputs.targets.contract_date), pd.to_datetime(inputs.targets.option_expiration_date).dt.date))
        for item in items:
            if item["status"] == "succeeded" and item["result_payload"]:
                saved = item["result_payload"]
                if item["input_fingerprint"] != inputs.identity:
                    raise ValueError("Checkpoint belongs to different settlement inputs")
                slice_ = api.restore_slice(saved["slice"])
                if saved["identity"] != api.settlement_identity(saved["slice"]):
                    raise ValueError("Checkpoint failed exact numerical identity")
                cache[pd.Timestamp(slice_.fit.maturity_date)] = slice_

        def checkpoint(slice_):
            if lost.is_set():
                raise LeaseLost("Settlement heartbeat failed")
            record = api.slice_record(slice_)
            with engine.begin() as connection:
                _guard(connection, job)
                connection.execute(text("""UPDATE at_lng.vol_calibration_job_items SET status='succeeded',
                    result_payload=CAST(:result AS jsonb),input_fingerprint=:identity,updated_at=clock_timestamp()
                    WHERE job_id=:job_id AND option_expiration_date=:expiry"""),
                    {"job_id": job.job_id, "expiry": expiry_for_month[pd.Timestamp(slice_.fit.maturity_date)],
                     "identity": inputs.identity, "result": json.dumps({"slice": record, "identity": api.settlement_identity(record)}, allow_nan=False)})
                connection.execute(text("""UPDATE at_lng.vol_calibration_jobs SET completed_items=(
                    SELECT count(*) FROM at_lng.vol_calibration_job_items WHERE job_id=:job_id AND status IN ('succeeded','skipped'))
                    WHERE job_id=:job_id"""), {"job_id": job.job_id})

        with engine.connect() as connection:
            saved_candidate = connection.execute(text("SELECT candidate FROM at_lng.vol_settlement_candidates WHERE job_id=:job_id"),
                {"job_id": job.job_id}).scalar_one_or_none()
        if saved_candidate:
            candidate = api.unpack_settlement(saved_candidate)
            api.verify_settlement_certificate(candidate["surface"], candidate["results"], candidate["manifest"], candidate["certificate"])
        else:
            candidate = api.build_settlement_candidate(inputs, checkpoint=checkpoint, cached_slices=cache)
            _save(engine, job, "prepared", candidate=candidate)
        # Remaining targets are explicit interpolation/extrapolation, validated above.
        with engine.begin() as connection:
            _guard(connection, job)
            connection.execute(text("UPDATE at_lng.vol_calibration_job_items SET status='skipped',updated_at=clock_timestamp() WHERE job_id=:job_id AND status='queued'"), {"job_id": job.job_id})
            connection.execute(text("UPDATE at_lng.vol_calibration_jobs SET completed_items=total_items WHERE job_id=:job_id"), {"job_id": job.job_id})
        api.assert_current_settlement_source(engine, inputs.product, inputs.cob, inputs.snapshot_id)
        fresh = api.load_settlement_inputs(engine, inputs.product, inputs.cob, inputs.snapshot_id)
        if fresh.identity != inputs.identity:
            raise ValueError("Source changed while the settlement was calibrating")
        if payload["mode"] == "shadow":
            _save(engine, job, "shadow", candidate=candidate, diagnostics=candidate["certificate"]["metrics"])
        else:
            actor = service_identity()
            checked_connection = None
            def publication_guard(connection):
                nonlocal checked_connection
                _guard(connection, job)
                if lost.is_set() or not feature_flags.settlement_auto_publication_enabled(inputs.product):
                    raise PermissionError("Automatic settlement publication is disabled or the heartbeat failed")
                api.assert_current_settlement_source(_ConnectionEngine(connection), inputs.product,
                    inputs.cob, inputs.snapshot_id, lock=True)
                _promotion(connection, inputs.product)
                operations.require_session(connection, inputs.product, inputs.cob)
                if checked_connection is not connection:
                    api.verify_settlement_source_inputs(_ConnectionEngine(connection), inputs.manifest)
                    checked_connection = connection
            receipt = api.publish_hybrid_surface(engine, candidate["surface"], candidate["results"],
                commodity=inputs.product, trading_date=inputs.cob, settlement_cob=inputs.cob,
                actor_subject=actor.subject, created_by=actor.subject, base_publication_id=None,
                expected_current_publication_id=payload["expected_current_publication_id"],
                idempotency_key=f"auto-settlement:{candidate['certificate']['certificate_identity']}",
                expected_expiries=inputs.targets.contract_date, input_manifest=candidate["manifest"],
                validation_certificate=candidate["certificate"], transaction_guard=publication_guard,
                idempotency_guard=lambda connection: _guard(connection, job), return_surface=False)
            _save(engine, job, "published", candidate=candidate, publication_id=receipt["publication_id"])
        if repo.complete(job_id=job.job_id, worker_id=job.worker_id) is None:
            raise LeaseLost("Settlement completion was fenced after candidate processing")


def run_one(engine, *, worker_id=None):
    if not feature_flags.settlement_jobs_enabled():
        return None
    repo = PostgresJobRepository(engine)
    with engine.begin() as connection:
        connection.execute(text("""UPDATE at_lng.vol_calibration_jobs SET status='cancelled',
            finished_at=clock_timestamp(),updated_at=clock_timestamp()
            WHERE job_type=:type AND status='running' AND cancellation_requested
            AND lease_expires_at < clock_timestamp()"""), {"type": JOB_TYPE})
        connection.execute(text("""UPDATE at_lng.vol_calibration_jobs SET status='failed',
            last_error='Settlement worker lease expired at the retry limit',failure_kind='retryable',finished_at=clock_timestamp()
            WHERE job_type=:type AND status='running' AND lease_expires_at < clock_timestamp()
            AND attempts >= max_attempts"""), {"type": JOB_TYPE})
    job = repo.claim(worker_id=worker_id or f"{platform.node()}:{os.getpid()}:{uuid4()}",
                     job_type=JOB_TYPE, lease_seconds=LEASE_SECONDS)
    if job is None:
        return None
    try:
        _run_claimed(engine, repo, job)
    except LeaseLost:
        with engine.begin() as connection:
            connection.execute(text("""UPDATE at_lng.vol_calibration_jobs SET status='cancelled',
                finished_at=clock_timestamp(),updated_at=clock_timestamp()
                WHERE job_id=:job_id AND worker_id=:worker_id AND attempts=:attempts
                AND status='running' AND cancellation_requested"""),
                {"job_id": job.job_id, "worker_id": job.worker_id, "attempts": job.attempts})
        LOGGER.warning("Settlement job %s lost ownership", job.job_id)
    except (ValueError, PermissionError, api.HybridPublicationError) as exc:
        try:
            _save(engine, job, "held", diagnostics={"error": str(exc), "details": getattr(exc, "diagnostics", {})})
            repo.abort(job_id=job.job_id, worker_id=job.worker_id, last_error=str(exc))
        except LeaseLost:
            LOGGER.warning("Settlement job %s was fenced while recording its hold", job.job_id)
    except Exception as exc:
        LOGGER.exception("Settlement job %s encountered a retryable execution failure", job.job_id)
        repo.fail(job_id=job.job_id, worker_id=job.worker_id, last_error=f"{type(exc).__name__}: {str(exc)[:1000]}",
                  retry_delay_seconds=min(300, 30 * 2 ** (job.attempts - 1)), failure_kind="retryable")
    return repo.get(job_id=job.job_id)


def reconcile(engine, *, from_cob, mode="shadow", expected_cob=None):
    """Reconcile persisted source events on startup and every poll; duplicates reuse jobs."""
    if not feature_flags.settlement_jobs_enabled():
        return []
    with engine.connect() as connection:
        sources = connection.execute(text("""SELECT DISTINCT ON (commodity,business_date)
            CAST(snapshot_id AS text) AS snapshot_id,commodity,business_date,input_fingerprint,
            source_revision,metadata,option_quote_count,forward_count FROM at_lng.vol_market_snapshots
            WHERE commodity IN ('BRENT','LNE') AND business_date >= :start AND status='complete'
            AND COALESCE(metadata->>'snapshot_kind','SETTLEMENT')='SETTLEMENT'
            ORDER BY commodity,business_date,observed_at DESC,created_at DESC,snapshot_id DESC"""),
            {"start": pd.Timestamp(from_cob).date()}).mappings().all()
        scheduled = connection.execute(text("""SELECT * FROM at_lng.vol_settlement_sessions
            WHERE cob_date>=:start ORDER BY cob_date,commodity"""),
            {"start": pd.Timestamp(from_cob).date()}).mappings().all()
        now = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
        existing = connection.execute(text("SELECT payload FROM at_lng.vol_calibration_jobs WHERE job_type=:type"), {"type": JOB_TYPE}).scalars().all()
    # Cheap event deduplication avoids re-inverting unchanged chains every poll.
    known = {(x.get("source_event_identity"), x["mode"], x.get("worker_identity"), x.get("code_identity")) for x in existing}
    jobs = []
    expected = {(row["commodity"], row["cob_date"]) for row in scheduled if row["ready_at"] <= now}
    if expected_cob is not None:
        expected.update((product, pd.Timestamp(expected_cob).date()) for product in ("BRENT", "HH"))
    for product, expected_date in sorted(expected):
        source_product = "LNE" if product == "HH" else product
        present = next((source for source in sources if source["commodity"] == source_product and source["business_date"] == expected_date), None)
        with engine.begin() as connection:
            connection.execute(text("""INSERT INTO at_lng.vol_settlement_source_events
                (event_identity,commodity,cob_date,state,reason) VALUES (:event,:product,:cob,:state,:reason)
                ON CONFLICT (event_identity) DO UPDATE SET state=EXCLUDED.state,reason=EXCLUDED.reason,updated_at=clock_timestamp()"""),
                {"event": f"expected:{product}:{expected_date}", "product": product, "cob": expected_date,
                 "state": "queued" if present else "waiting",
                 "reason": None if present else "No complete settlement snapshot for the expected exchange session"})
    scheduled_by_key = {(row["commodity"], row["cob_date"]): row for row in scheduled}
    worker_hash, code_hash = worker_identity(), api.implementation_fingerprint()
    for source in sources:
        product = "HH" if source["commodity"] == "LNE" else "BRENT"
        session = scheduled_by_key.get((product, source["business_date"]))
        if session and session["ready_at"] > now:
            continue
        event_identity = api.settlement_identity(dict(source))
        if (event_identity, mode, worker_hash, code_hash) in known:
            continue
        product = "HH" if source["commodity"] == "LNE" else "BRENT"
        job = None
        state, reason = "queued", None
        try:
            job = submit_settlement(engine, product, source["business_date"], source["snapshot_id"],
                                    mode=mode, source_event_identity=event_identity)
            jobs.append(job)
        except ValueError as exc:
            state, reason = "waiting", str(exc)
            LOGGER.warning("Settlement source %s is waiting: %s", source["snapshot_id"], exc)
        with engine.begin() as connection:
            connection.execute(text("""INSERT INTO at_lng.vol_settlement_source_events
                (event_identity,commodity,cob_date,snapshot_id,state,reason,job_id)
                VALUES (:event,:product,:cob,:snapshot,:state,:reason,:job)
                ON CONFLICT (event_identity) DO UPDATE SET state=EXCLUDED.state,reason=EXCLUDED.reason,
                job_id=EXCLUDED.job_id,updated_at=clock_timestamp()"""),
                {"event": event_identity, "product": product, "cob": source["business_date"],
                 "snapshot": source["snapshot_id"], "state": state, "reason": reason, "job": job.job_id if job else None})
    return jobs


def settlement_status(engine, *, from_cob):
    """Read persisted readiness, holds and job progress without publication effects."""
    with engine.connect() as connection:
        jobs = connection.execute(text("""SELECT j.job_id,j.status,j.attempts,j.worker_id,j.lease_expires_at,
            j.completed_items,j.total_items,j.last_error,j.next_attempt_at,j.failure_kind,
            j.payload->>'product' AS product,j.payload->>'cob' AS cob,
            c.state,c.publication_id FROM at_lng.vol_calibration_jobs j
            LEFT JOIN at_lng.vol_settlement_candidates c USING(job_id)
            WHERE j.job_type=:type AND (j.payload->>'cob')::date >= :start ORDER BY j.created_at DESC"""),
            {"type": JOB_TYPE, "start": pd.Timestamp(from_cob).date()}).mappings().all()
        waiting = connection.execute(text("SELECT * FROM at_lng.vol_settlement_source_events WHERE state IN ('waiting','held') AND cob_date >= :start ORDER BY cob_date DESC"),
            {"start": pd.Timestamp(from_cob).date()}).mappings().all()
        alerts = connection.execute(text("SELECT * FROM at_lng.vol_settlement_alerts WHERE is_active AND cob_date>=:start ORDER BY cob_date,commodity"),
            {"start": pd.Timestamp(from_cob).date()}).mappings().all()
        sessions = connection.execute(text("""SELECT s.commodity,s.cob_date,s.ready_at,s.deadline_at,
            j.status,j.next_attempt_at,j.failure_kind,extract(epoch FROM (j.finished_at-s.ready_at)) AS completion_seconds
            FROM at_lng.vol_settlement_sessions s LEFT JOIN LATERAL
            (SELECT * FROM at_lng.vol_calibration_jobs WHERE job_type=:type AND payload->>'product'=s.commodity
             AND (payload->>'cob')::date=s.cob_date ORDER BY created_at DESC,job_id DESC LIMIT 1) j ON true
            WHERE s.cob_date>=:start ORDER BY s.cob_date DESC,s.commodity"""),
            {"start": pd.Timestamp(from_cob).date(), "type": JOB_TYPE}).mappings().all()
        promotion = connection.execute(text("SELECT * FROM at_lng.vol_settlement_promotion ORDER BY commodity")).mappings().all()
    return {"jobs_enabled": feature_flags.settlement_jobs_enabled(),
        "auto_publication": {p: feature_flags.settlement_auto_publication_enabled(p) for p in ("BRENT", "HH")},
        "alerts": [dict(row) for row in alerts], "sessions": [dict(row) for row in sessions],
        "jobs": [dict(row) for row in jobs], "waiting": [dict(row) for row in waiting], "promotion": [dict(row) for row in promotion]}


def operator_identity():
    subject = os.getenv("VOL_CALIBRATION_SETTLEMENT_OPERATOR_ACTOR", "").strip()
    roles = os.getenv("VOL_CALIBRATION_SETTLEMENT_OPERATOR_ROLES", "")
    if not subject or not roles:
        raise PermissionError("Explicit settlement operator actor and roles are required")
    return Identity(subject, frozenset(Role(role.strip()) for role in roles.split(",")), True,
                    "configured_settlement_operator")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-cob", required=True, type=date.fromisoformat)
    parser.add_argument("--mode", choices=("shadow", "publish"), default="shadow")
    parser.add_argument("--once", action="store_true")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--status", action="store_true")
    commands.add_argument("--retry-job")
    commands.add_argument("--import-calendar", type=Path)
    commands.add_argument("--approve-product", choices=("BRENT", "HH"))
    parser.add_argument("--reason", help="Required audit reason for --retry-job")
    parser.add_argument("--evidence-through", type=date.fromisoformat)
    parser.add_argument("--expected-cob", type=date.fromisoformat)
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if args.poll_seconds < 10:
        parser.error("Polling interval must be at least ten seconds")
    if args.retry_job and not args.reason:
        parser.error("--retry-job requires --reason")
    if args.approve_product and not args.evidence_through:
        parser.error("--approve-product requires --evidence-through")
    operational = args.status or args.retry_job or args.import_calendar or args.approve_product
    if not operational and not feature_flags.settlement_jobs_enabled():
        parser.error("Settlement jobs are disabled; enable shadow jobs explicitly")
    logging.basicConfig(level=logging.INFO)
    engine = get_database_engine()
    if args.status:
        print(json.dumps(settlement_status(engine, from_cob=args.from_cob), default=str, indent=2))
        return
    if args.retry_job:
        print(operations.retry_settlement(engine, args.retry_job, identity=operator_identity(),
            reason=args.reason, worker_identity=worker_identity()))
        return
    if args.import_calendar:
        print(operations.import_calendar(engine, json.loads(args.import_calendar.read_text()), identity=operator_identity()))
        return
    if args.approve_product:
        print(operations.approve_promotion(engine, args.approve_product, args.evidence_through,
            identity=operator_identity(), worker_identity=worker_identity()))
        return
    previous_error = None
    while True:
        try:
            reconcile(engine, from_cob=args.from_cob, mode=args.mode, expected_cob=args.expected_cob)
            operations.refresh_alerts(engine, from_cob=args.from_cob, mode=args.mode)
            while run_one(engine) is not None:
                operations.refresh_alerts(engine, from_cob=args.from_cob, mode=args.mode)
            operations.refresh_alerts(engine, from_cob=args.from_cob, mode=args.mode)
            if previous_error is not None:
                LOGGER.info("Settlement database connectivity recovered")
            previous_error = None
        except SQLAlchemyError as exc:
            error = type(exc).__name__
            if previous_error != error:
                LOGGER.error("Settlement database unavailable (%s); retrying on next poll", error)
            previous_error = error
            if args.once:
                raise
        if args.once:
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
