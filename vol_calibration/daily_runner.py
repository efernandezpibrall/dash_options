"""Explicitly requested all-product daily jobs; no scheduler or automatic promotion.

Sources and prior parameters are pinned once. Resume requires the same owner,
code and saved inputs. Publication uses one writer, bounded retries, a stable key,
and an exact persisted readback before the job can succeed.
"""

from contextlib import ExitStack
from datetime import timedelta
from functools import partial
import argparse
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from options.vol_calibration import api

from runtime_config import get_database_engine
from .auth import Permission, authorize, resolve_request_identity
from . import feature_flags
from .daily_store import DailyStore
from .jobs import PostgresJobRepository
from .observed_fit_pool import prefit_observed_expiries
from .settlement_operations import ConnectionEngine
from .settlement_worker import _guard, _heartbeat, LeaseLost, LEASE_SECONDS

JOB_TYPE = "daily_settlement_v1"
PRODUCTS = ("BRENT", "HH", "TTF", "JKM", "NBP")


def worker_identity():
    digest = hashlib.sha256()
    for name in (
        "daily_runner.py",
        "daily_store.py",
        "jobs.py",
        "auth.py",
        "feature_flags.py",
        "settlement_worker.py",
        "settlement_operations.py",
        "observed_fit_pool.py",
    ):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def _authorize(identity, mode, product, *, owner=None):
    if mode not in {"prepare", "publish"} or product not in PRODUCTS:
        raise ValueError("Daily mode or product is unsupported")
    authorize(identity, Permission.PUBLISH if mode == "publish" else Permission.CREATE_DRAFT)
    if owner is not None and identity.subject != owner:
        raise PermissionError("Daily jobs can only be resumed by their owner")
    if not feature_flags.settlement_jobs_enabled():
        raise PermissionError("Settlement jobs are disabled")
    if mode == "publish" and not getattr(feature_flags, product.lower() + "_publication_enabled")():
        raise PermissionError(f"{product} publication is disabled")


def submit_daily(engine, product, cob, *, identity, mode="prepare"):
    product = str(product).upper()
    cob = pd.Timestamp(cob).date()
    _authorize(identity, mode, product)
    # Freeze source, exact-date CAS and preceding published parameters in a
    # repeatable-read view. Expensive fitting starts only after it is closed.
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            source = ConnectionEngine(connection)
            inputs = api.load_daily_inputs(source, product, cob)
            current = api.load_publication_header(source, cob, commodity=product, require_exact_cob=True)
            base = api.load_latest_hybrid_publication(
                source, cob - timedelta(days=1), commodity=product, prefer_exact_cob=True, include_surface=False
            )
    payload = {
        "schema": 1,
        "job_type": JOB_TYPE,
        "product": product,
        "cob": cob.isoformat(),
        "mode": mode,
        "input_identity": inputs.identity,
        "code_identity": api.implementation_fingerprint(),
        "worker_identity": worker_identity(),
        "base_publication_id": base.get("publication_id"),
        "expected_current_publication_id": current.get("publication_id"),
    }
    key = "daily:" + api.settlement_identity({"payload": payload, "owner": identity.subject})
    encoded = api.encode_daily_artifact({"inputs": api.daily_input_record(inputs), "base": base})
    # A single transaction makes a claimable job inseparable from its input artifact.
    with engine.begin() as connection:

        class BoundEngine(ConnectionEngine):
            def begin(self):
                return self.connect()

        job = PostgresJobRepository(BoundEngine(connection)).submit(
            run_id=None,
            job_type=JOB_TYPE,
            payload=payload,
            idempotency_key=key,
            created_by=identity.subject,
            max_attempts=3,
            total_items=len(inputs.targets),
        )
        DailyStore.insert(connection, job.job_id, "inputs", encoded)
    return job


def _committed(engine, key, certificate):
    """Resolve an uncertain commit before attempting any publication replay."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("""SELECT p.publication_id,p.status,p.is_active,
            r.configuration->'validation_certificate'->>'certificate_identity' AS certificate
            FROM at_lng.vol_surface_publications p JOIN at_lng.vol_calibration_runs r USING(run_id)
            WHERE p.idempotency_key=:key"""),
                {"key": key},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    if row["certificate"] != certificate or (row["status"] != "published" or not row["is_active"]):
        raise ValueError("Committed publication is different or superseded; review required")
    return str(row["publication_id"])


def _run_claimed(engine, repo, job, identity):
    payload = job.payload
    store = DailyStore(engine)
    _authorize(identity, payload["mode"], payload["product"], owner=job.created_by)
    if (
        payload.get("schema") != 1
        or payload.get("job_type") != JOB_TYPE
        or payload.get("worker_identity") != worker_identity()
        or payload.get("code_identity") != api.implementation_fingerprint()
    ):
        raise ValueError("Saved daily job requires its original implementation")
    saved = store.get(job.job_id, "inputs")
    if saved is None:
        raise ValueError("Daily job has no pinned inputs")
    inputs = api.restore_daily_inputs(saved["inputs"])
    if inputs.identity != payload["input_identity"]:
        raise ValueError("Saved inputs do not match this job")
    with _heartbeat(repo, job) as lost:

        def guard(connection):
            _authorize(identity, payload["mode"], inputs.product, owner=job.created_by)
            if lost.is_set():
                raise LeaseLost("Daily heartbeat failed")
            _guard(connection, job)

        last_cancel_check = 0.0

        def cancelled(force=False):
            nonlocal last_cancel_check
            if lost.is_set():
                raise LeaseLost("Daily heartbeat failed")
            if force or time.monotonic() - last_cancel_check >= 5:
                with engine.begin() as connection:
                    guard(connection)
                last_cancel_check = time.monotonic()

        candidate = store.get(job.job_id, "candidate")
        if candidate is None:
            store.transition(job, "calibrating")
            checkpoints = store.checkpoints(job.job_id)
            pending = []

            def checkpoint(value):
                if inputs.product in {"BRENT", "HH"}:
                    record = api.slice_record(value)
                    key = pd.Timestamp(value.fit.maturity_date).date().isoformat()
                else:
                    record = value
                    key = value["expiry"]
                pending.append((key, record))
                if len(pending) >= 8:
                    store.save_checkpoints(job, pending)
                    pending.clear()

            cache = (
                {pd.Timestamp(key): api.restore_slice(value) for key, value in checkpoints.items()}
                if inputs.product in {"BRENT", "HH"}
                else None
            )
            try:
                candidate = api.build_daily_candidate(
                    inputs,
                    base_publication=saved["base"],
                    checkpoints=checkpoints,
                    checkpoint_callback=checkpoint,
                    cached_slices=cache,
                    cancellation_check=cancelled,
                    observed_executor=partial(
                        prefit_observed_expiries, environment_variable="DAILY_OBSERVED_FIT_WORKERS"
                    ),
                )
            finally:
                if pending:
                    store.save_checkpoints(job, pending)
            store.put(job, "candidate", candidate)
        api.verify_daily_candidate(candidate)
        if candidate["source_identity"] != inputs.identity:
            raise ValueError("Candidate belongs to different daily inputs")
        # A prepare-only run still checks for a corrected source before success.
        checked_connection = None

        def source_guard(connection):
            nonlocal checked_connection
            guard(connection)
            if checked_connection is not connection:
                api.verify_daily_source(ConnectionEngine(connection), inputs)
                checked_connection = connection

        if payload["mode"] == "prepare":
            with engine.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout='5s'"))
                connection.execute(text("SET LOCAL statement_timeout='120s'"))
                source_guard(connection)
            store.transition(job, "accepted")
        else:
            certificate = candidate["certificate"]["certificate_identity"]
            key = "daily-publication:" + str(job.job_id) + ":" + certificate
            publication_id = _committed(engine, key, certificate)
            if publication_id is None:
                store.transition(job, "publishing", details={"publication_key": key, "certificate": certificate})
                receipt = api.publish_hybrid_surface(
                    engine,
                    candidate["surface"],
                    candidate["results"],
                    commodity=inputs.product,
                    trading_date=inputs.cob,
                    settlement_cob=inputs.cob,
                    actor_subject=identity.subject,
                    created_by=identity.subject,
                    base_publication_id=payload["base_publication_id"],
                    expected_current_publication_id=payload["expected_current_publication_id"],
                    idempotency_key=key,
                    expected_expiries=inputs.targets.contract_date,
                    input_manifest=candidate["manifest"],
                    validation_certificate=candidate["certificate"],
                    transaction_guard=source_guard,
                    idempotency_guard=guard,
                    return_surface=False,
                )
                publication_id = receipt["publication_id"]
            store.transition(job, "published", publication_id=publication_id)
            api.verify_daily_readback(engine, candidate, publication_id)
            cancelled(force=True)
            store.transition(
                job,
                "verified",
                publication_id=publication_id,
                details={"certificate": certificate, "point_count": len(candidate["surface"])},
            )
        with engine.begin() as connection:
            guard(connection)
            connection.execute(
                text("UPDATE at_lng.vol_calibration_jobs SET completed_items=total_items WHERE job_id=:job"),
                {"job": job.job_id},
            )
        if repo.complete(job_id=job.job_id, worker_id=job.worker_id) is None:
            raise LeaseLost("Daily completion was fenced")


def run_daily(engine, job_id, *, identity, worker_id=None):
    repo = PostgresJobRepository(engine)
    job = repo.get(job_id=job_id)
    if job is None or job.payload.get("job_type") != JOB_TYPE:
        raise ValueError("Daily job is unavailable")
    _authorize(identity, job.payload["mode"], job.payload["product"], owner=job.created_by)
    job = repo.claim(
        worker_id=worker_id or "daily-" + str(uuid4()), lease_seconds=LEASE_SECONDS, job_id=job_id, job_type=JOB_TYPE
    )
    if job is None:
        return repo.get(job_id=job_id)
    try:
        _run_claimed(engine, repo, job, identity)
    except LeaseLost:
        # A cancelled owner may finish cancellation, but cannot change a
        # successor's state after lease reclaim.
        with engine.begin() as connection:
            connection.execute(
                text("""UPDATE at_lng.vol_calibration_jobs SET status='cancelled',
                finished_at=clock_timestamp(),updated_at=clock_timestamp()
                WHERE job_id=:job AND worker_id=:worker AND attempts=:attempts
                  AND status='running' AND cancellation_requested"""),
                {"job": job.job_id, "worker": job.worker_id, "attempts": job.attempts},
            )
        raise
    except SQLAlchemyError as exc:
        repo.fail(
            job_id=job.job_id,
            worker_id=job.worker_id,
            last_error=type(exc).__name__,
            retry_delay_seconds=min(60, 5 * 2 ** (job.attempts - 1)),
            failure_kind="retryable",
        )
        raise
    except Exception as exc:
        DailyStore(engine).transition(job, "held", details={"reason": type(exc).__name__})
        repo.abort(job_id=job.job_id, worker_id=job.worker_id, last_error=str(exc))
        raise
    return repo.get(job_id=job_id)


def run_daily_batch(engine, job_ids, *, identity):
    """One serialized daily command, retaining the pre/post full history scrub."""
    jobs = [PostgresJobRepository(engine).get(job_id=key) for key in job_ids]
    if not jobs or any(job is None for job in jobs):
        raise ValueError("Daily batch has unavailable jobs")
    for job in jobs:
        _authorize(identity, job.payload["mode"], job.payload["product"], owner=job.created_by)
    store = DailyStore(engine)
    before = store.get(jobs[0].job_id, "history_before")
    if before is None:
        with api.calibration_stage("ALL", "full_historical_scrub_before"):
            before = api.historical_content_snapshot(engine)
        with engine.begin() as connection:
            DailyStore.insert(connection, jobs[0].job_id, "history_before", api.encode_daily_artifact(before))
    completed = []
    try:
        for job in jobs:
            for attempt in range(3):
                try:
                    completed.append(run_daily(engine, job.job_id, identity=identity))
                    break
                except SQLAlchemyError:
                    current = PostgresJobRepository(engine).get(job_id=job.job_id)
                    if current.status.value != "queued" or attempt == 2:
                        raise
                    time.sleep(min(60, 5 * 2 ** (current.attempts - 1)))
    finally:
        with api.calibration_stage("ALL", "full_historical_scrub_after"):
            after = api.historical_content_snapshot(engine, list(before))
        with engine.begin() as connection:
            DailyStore.insert(
                connection,
                jobs[0].job_id,
                "history_after",
                api.encode_daily_artifact({"unchanged": before == after, "contents": after}),
            )
        if before != after:
            raise ValueError("Historical publication content changed; review required")
    return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cob")
    parser.add_argument("--products", nargs="+", choices=PRODUCTS, default=list(PRODUCTS))
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--resume")
    parser.add_argument("--timings", type=Path, help="Append credential-free stage events as JSONL")
    args = parser.parse_args()
    if args.resume and (args.cob or args.publish):
        parser.error("--resume retains its saved date and mode; omit --cob and --publish")
    if not args.resume and not args.cob:
        parser.error("--cob or --resume is required")
    # A CLI has no trusted proxy headers. Only an explicitly configured local
    # workstation identity can resolve; remote service automation is separate.
    identity = resolve_request_identity({}, remote_addr="127.0.0.1")
    engine = get_database_engine()
    summaries = []
    with ExitStack() as contexts:
        if args.timings:
            stream = contexts.enter_context(args.timings.open("a"))

            def emit(record):
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()

            contexts.enter_context(api.calibration_trace(emit))
        jobs = (
            [PostgresJobRepository(engine).get(job_id=args.resume)]
            if args.resume
            else [
                submit_daily(
                    engine, product, args.cob, identity=identity, mode="publish" if args.publish else "prepare"
                )
                for product in args.products
            ]
        )
        if any(job is None for job in jobs):
            raise ValueError("Daily job is unavailable")
        completed_jobs = run_daily_batch(engine, [job.job_id for job in jobs], identity=identity)
        for completed in completed_jobs:
            summaries.append(
                {
                    "job_id": str(completed.job_id),
                    "product": completed.payload["product"],
                    "status": completed.status.value,
                    "stage": DailyStore(engine).status(completed.job_id),
                }
            )
    print(json.dumps(summaries, default=str, sort_keys=True))
    if any(job.status.value != "succeeded" for job in completed_jobs):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
