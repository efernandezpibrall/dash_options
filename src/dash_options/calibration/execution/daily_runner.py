"""Explicitly requested all-product daily jobs; no scheduler or automatic promotion.

Sources and prior parameters are pinned once. Resume requires the same owner,
code and saved inputs. Publication uses one writer, bounded retries, a stable key,
and an exact persisted readback before the job can succeed.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, copy_context
from functools import partial
import argparse
import json
import os
import time
from threading import Lock
from pathlib import Path
from dash_options.core.code_identity import execution_fingerprint
from uuid import uuid4

import pandas as pd
from sqlalchemy import text
from options.vol_calibration import api

from dash_options.core.config import get_database_engine
from dash_options.calibration.auth import Permission, authorize, resolve_request_identity
from dash_options.calibration import feature_flags
from dash_options.calibration.execution.daily_store import DailyStore
from dash_options.calibration.execution.repository import PostgresJobRepository
from dash_options.calibration.execution.fit_pool import (
    observed_fit_session,
    stream_observed_expiries,
    execute_hybrid_starts,
)
from dash_options.calibration.execution.settlement_operations import ConnectionEngine
from dash_options.calibration.execution.settlement_worker import _guard, _heartbeat, LeaseLost, LEASE_SECONDS
from dash_options.calibration.execution.settings import PublicationSettings
from dash_options.calibration.publication_status import committed_publication as _committed, matching_active_publication
from dash_options.calibration.execution.errors import (
    is_database_error,
    retryable_execution,
    safe_failure_reason,
    failure_details,
)

JOB_TYPE = "daily_settlement_v1"
PRODUCTS = ("BRENT", "HH", "TTF", "JKM", "NBP")
_EXECUTION_SLOTS = ContextVar("daily_execution_slots", default=None)


@contextmanager
def _execution_slot(kind, product, cancellation_check):
    """Overlap two products, with one fitting budget and one publication writer."""
    slots = _EXECUTION_SLOTS.get()
    if slots is None:
        yield
        return
    lock = slots[kind]
    with api.calibration_stage(product, kind + "_queue_wait"):
        while not lock.acquire(timeout=0.25):
            cancellation_check()
    try:
        cancellation_check()
        yield
    finally:
        lock.release()


def worker_identity():
    return execution_fingerprint(('calibration/execution/daily_runner.py', 'calibration/execution/daily_store.py', 'calibration/execution/repository.py', 'calibration/auth.py', 'calibration/feature_flags.py', 'calibration/execution/settlement_worker.py', 'calibration/execution/settlement_operations.py', 'calibration/execution/fit_pool.py', 'calibration/execution/settings.py', 'calibration/execution/metadata.py', 'calibration/publication_status.py', 'calibration/execution/errors.py'))


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


def _pin_inputs(engine, product, cob):
    # Freeze source, exact-date CAS and preceding published parameters in a
    # repeatable-read view. Expensive fitting starts only after it is closed.
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            source = ConnectionEngine(connection)
            inputs = api.load_daily_inputs(source, product, cob)
            current = api.load_publication_header(source, cob, commodity=product, require_exact_cob=True)
            previous = connection.execute(text("""SELECT publication_id,cob_date
                FROM at_lng.vol_surface_publications
                WHERE commodity=:product AND cob_date<:cob AND is_active AND status='published'
                ORDER BY cob_date DESC,published_at DESC LIMIT 1"""),
                {"product": product, "cob": cob}).mappings().one_or_none()
            # Operational warm starts use the preceding active settlement,
            # including one approved after a weekend or holiday cutoff.
            base = (api.load_latest_hybrid_publication(
                source, previous["cob_date"], commodity=product,
                publication_id=str(previous["publication_id"]), require_exact_cob=True,
                include_surface=False,
            ) if previous is not None else {})
            if previous is not None and base.get("publication_id") != str(previous["publication_id"]):
                raise ValueError("The preceding active publication could not be loaded")
            warm_cob = os.environ.get("VOL_CALIBRATION_DAILY_WARM_START_COB")
            if warm_cob and product in {"TTF", "JKM", "NBP"}:
                warm_date = pd.Timestamp(warm_cob).date()
                if not base or warm_date >= pd.Timestamp(base["settlement_cob"]).date():
                    raise ValueError("A recovery warm start must precede the active base settlement")
                warm = api.load_latest_hybrid_publication(
                    source, warm_date, commodity=product, require_exact_cob=True,
                    include_surface=False,
                )
                if not warm.get("publication_id"):
                    raise ValueError("The explicit recovery warm-start publication is unavailable")
                base["warm_start_publication"] = warm
    return inputs, base, current


def submit_daily(engine, product, cob, *, identity, mode="prepare"):
    product = str(product).upper()
    cob = pd.Timestamp(cob).date()
    _authorize(identity, mode, product)
    preparation_error = None
    try:
        inputs, base, current = _pin_inputs(engine, product, cob)
    except api.DailyInputsUnavailable:
        inputs, base, current = None, {}, {}
    except Exception as exc:
        if not isinstance(exc, ValueError) and not is_database_error(exc):
            raise
        inputs, base, current = None, {}, {}
        if not retryable_execution(exc):
            preparation_error = failure_details(exc)
    payload = {
        "schema": 1,
        "job_type": JOB_TYPE,
        "product": product,
        "cob": cob.isoformat(),
        "mode": mode,
        "input_identity": inputs.identity if inputs is not None else None,
        "awaiting_source": inputs is None and preparation_error is None,
        "preparation_error": preparation_error,
        "code_identity": api.implementation_fingerprint(),
        "worker_identity": worker_identity(),
        "base_publication_id": base.get("publication_id"),
        "warm_start_publication_id": base.get("warm_start_publication", base).get("publication_id"),
        "expected_current_publication_id": current.get("publication_id"),
    }
    key = "daily:" + api.settlement_identity({"payload": payload, "owner": identity.subject})
    encoded = (api.encode_daily_artifact({"inputs": api.daily_input_record(inputs), "base": base,
               "expected_current_publication_id": current.get("publication_id")}) if inputs is not None else None)
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
            total_items=len(inputs.targets) if inputs is not None else 0,
        )
        if encoded is not None:
            DailyStore.insert(connection, job.job_id, "inputs", encoded)
        connection.execute(text("""INSERT INTO at_lng.vol_daily_stages(job_id,commodity,cob_date,stage)
            VALUES (:job,:product,:cob,:stage) ON CONFLICT DO NOTHING"""),
            {"job": job.job_id, "product": product, "cob": cob,
             "stage": "prepared" if encoded is not None else "waiting"})
    return job


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
    if payload.get("preparation_error"):
        raise ValueError(payload["preparation_error"]["message"])
    if saved is None and payload.get("awaiting_source"):
        with _heartbeat(repo, job) as lost:
            inputs, base, current = _pin_inputs(engine, payload["product"], pd.Timestamp(payload["cob"]).date())
            saved = {"inputs": api.daily_input_record(inputs), "base": base,
                     "expected_current_publication_id": current.get("publication_id")}
            encoded = api.encode_daily_artifact(saved)
            with engine.begin() as connection:
                if lost.is_set():
                    raise LeaseLost("Daily input preparation lost its lease")
                _guard(connection, job, lock=False)
                DailyStore.insert(connection, job.job_id, "inputs", encoded)
                _guard(connection, job)
                connection.execute(text("UPDATE at_lng.vol_calibration_jobs SET total_items=:count WHERE job_id=:job"),
                                   {"count": len(inputs.targets), "job": job.job_id})
    if saved is None:
        raise ValueError("Daily job has no pinned inputs")
    inputs = api.restore_daily_inputs(saved["inputs"])
    if ((payload["input_identity"] is not None and inputs.identity != payload["input_identity"]) or inputs.product != payload["product"]
            or inputs.cob.isoformat() != payload["cob"]):
        raise ValueError("Saved inputs do not match this job")
    with _heartbeat(repo, job) as lost:

        def guard(connection, *, lock=True):
            _authorize(identity, payload["mode"], inputs.product, owner=job.created_by)
            if lost.is_set():
                raise LeaseLost("Daily heartbeat failed")
            _guard(connection, job, lock=lock)

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
            reused = store.reuse_candidate(job, inputs.identity, saved['base'].get('publication_id'))
            if reused:
                candidate = store.get(job.job_id, 'candidate')
                store.transition(job, 'accepted', details={'candidate_reused_from':reused})
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
            with _execution_slot("fit", inputs.product, cancelled):
                try:
                    candidate = api.build_daily_candidate(
                        inputs,
                        base_publication=saved["base"],
                        **({"warm_start_publication": saved["base"]["warm_start_publication"]}
                           if saved["base"].get("warm_start_publication") else {}),
                        checkpoints=checkpoints,
                        checkpoint_callback=checkpoint,
                        cached_slices=cache,
                        cancellation_check=cancelled,
                        observed_executor=partial(
                            stream_observed_expiries, environment_variable="DAILY_OBSERVED_FIT_WORKERS",
                            cancellation_check=cancelled,
                        ),
                        start_executor=partial(execute_hybrid_starts, cancellation_check=cancelled),
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
            # Full source reads may be slow. Only the final, cached guard locks
            # the lease row immediately before the activation statements.
            guard(connection, lock=checked_connection is connection)
            if checked_connection is not connection:
                api.verify_daily_source(ConnectionEngine(connection), inputs)
                checked_connection = connection
            previous_id = connection.execute(text("""SELECT publication_id FROM at_lng.vol_surface_publications
                WHERE commodity=:product AND cob_date<:cob AND is_active AND status='published'
                ORDER BY cob_date DESC,published_at DESC LIMIT 1 FOR SHARE"""),
                {"product": inputs.product, "cob": inputs.cob}).scalar_one_or_none()
            if (str(previous_id) if previous_id else None) != saved["base"].get("publication_id"):
                raise ValueError("The preceding active publication changed after input pinning")

        with _execution_slot("publication", inputs.product, cancelled):
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
                expected = saved.get('expected_current_publication_id', payload['expected_current_publication_id'])
                if publication_id is None and expected:
                    with engine.connect() as connection:
                        matching = matching_active_publication(connection, expected, certificate,
                                                               product=inputs.product, cob=inputs.cob)
                    if matching:
                        # A repeated explicit command may have a new job/key after
                        # the first activation. Recheck source/lineage and the exact
                        # active certificate before reusing that immutable revision.
                        with engine.begin() as connection:
                            source_guard(connection)
                            publication_id = matching_active_publication(connection, expected, certificate,
                                product=inputs.product, cob=inputs.cob, lock=True)
                            guard(connection)
                        if publication_id:
                            store.put(job, 'publication_reuse', {'schema':1,
                                'publication_id':publication_id, 'certificate':certificate})
                if publication_id is None:
                    store.transition(job, "staging", details={"publication_key": key, "certificate": certificate})
                    receipt = api.publish_hybrid_surface(
                        engine,
                        candidate["surface"],
                        candidate["results"],
                        commodity=inputs.product,
                        trading_date=inputs.cob,
                        settlement_cob=inputs.cob,
                        actor_subject=identity.subject,
                        created_by=identity.subject,
                        base_publication_id=saved["base"].get("publication_id"),
                        expected_current_publication_id=saved.get("expected_current_publication_id", payload["expected_current_publication_id"]),
                        idempotency_key=key,
                        expected_expiries=inputs.targets.contract_date,
                        input_manifest=candidate["manifest"],
                        validation_certificate=candidate["certificate"],
                        transaction_guard=source_guard,
                        idempotency_guard=lambda connection: guard(connection, lock=False),
                        return_surface=False,
                        stage_before_activation=True,
                        staging_guard=lambda connection: guard(connection, lock=False),
                        staging_job_id=job.job_id,
                        publication_stage_callback=lambda state, planned_id: store.transition(
                            job, state, details={"planned_publication_id": planned_id, "certificate": certificate}
                        ),
                        **PublicationSettings.from_environment().arguments(),
                    )
                    publication_id = receipt["publication_id"]
                store.transition(job, "published", publication_id=publication_id)
                store.transition(job, "verification_pending", publication_id=publication_id)
                api.verify_daily_readback(engine, candidate, publication_id)
                api.release_verified_stage(engine, publication_id, actor=identity.subject)
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
        if repo.complete(job_id=job.job_id, worker_id=job.worker_id, attempts=job.attempts) is None:
            raise LeaseLost("Daily completion was fenced")


def run_daily(engine, job_id, *, identity, worker_id=None):
    repo = PostgresJobRepository(engine)
    job = repo.get(job_id=job_id)
    if job is None or job.payload.get("job_type") != JOB_TYPE:
        raise ValueError("Daily job is unavailable")
    _authorize(identity, job.payload["mode"], job.payload["product"], owner=job.created_by)
    # A crash on the final claim must become an explicit retryable failure;
    # otherwise no subsequent claim can recover an exhausted running job.
    with engine.begin() as connection:
        connection.execute(text("""UPDATE at_lng.vol_calibration_jobs SET
            status=CASE WHEN cancellation_requested THEN 'cancelled' ELSE 'failed' END,
            failure_kind=CASE WHEN cancellation_requested THEN failure_kind ELSE 'retryable' END,
            last_error=CASE WHEN cancellation_requested THEN last_error
                ELSE 'Daily worker lease expired at the retry limit' END,
            finished_at=clock_timestamp(),updated_at=clock_timestamp(),worker_id=NULL,lease_expires_at=NULL
            WHERE job_id=:job AND status='running' AND lease_expires_at<clock_timestamp()
              AND (cancellation_requested OR attempts>=max_attempts)"""), {"job":job.job_id})
    job = repo.claim(
        worker_id=worker_id or "daily-" + str(uuid4()), lease_seconds=LEASE_SECONDS, job_id=job_id, job_type=JOB_TYPE
    )
    if job is None:
        return repo.get(job_id=job_id)
    try:
        with observed_fit_session():
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
        repo.fail(job_id=job.job_id, worker_id=job.worker_id, attempts=job.attempts,
                  last_error="Daily execution lost its lease; retry available", failure_kind="retryable")
        raise
    except api.DailyInputsUnavailable as exc:
        DailyStore(engine).transition(job, "waiting", details={"reason": "exact_settlement_not_ready"})
        repo.fail(job_id=job.job_id, worker_id=job.worker_id, attempts=job.attempts,
                  last_error=type(exc).__name__, retry_delay_seconds=min(60, 5 * 2 ** (job.attempts - 1)),
                  failure_kind="retryable")
        raise
    except Exception as exc:
        if retryable_execution(exc):
            repo.fail(job_id=job.job_id, worker_id=job.worker_id, attempts=job.attempts,
                      last_error=safe_failure_reason(exc),
                      retry_delay_seconds=min(60, 5 * 2 ** (job.attempts - 1)), failure_kind="retryable")
        else:
            DailyStore(engine).transition(job, "held", details=failure_details(exc))
            repo.abort(job_id=job.job_id, worker_id=job.worker_id, attempts=job.attempts,
                       last_error=safe_failure_reason(exc))
        raise
    return repo.get(job_id=job_id)


def retry_daily(engine, job_id, *, identity, reason):
    """Grant three more claims only for an owned, compatible execution failure."""
    if not str(reason).strip():
        raise ValueError("An audited retry reason is required")
    with engine.begin() as connection:
        budgets = PublicationSettings.from_environment()
        connection.execute(text("SELECT set_config('lock_timeout',:value,true)"),
                           {"value":f"{budgets.lock_timeout_ms}ms"})
        connection.execute(text("SELECT set_config('statement_timeout',:value,true)"),
                           {"value":f"{budgets.statement_timeout_ms}ms"})
        row = connection.execute(text("SELECT * FROM at_lng.vol_calibration_jobs WHERE job_id=:job FOR UPDATE"),
                                 {"job":job_id}).mappings().one_or_none()
        if row is None or row["job_type"] != JOB_TYPE or row["created_by"] != identity.subject:
            raise PermissionError("Daily job is unavailable for this owner")
        payload = row["payload"]
        _authorize(identity, payload["mode"], payload["product"], owner=row["created_by"])
        if row["status"] != "failed" or row["failure_kind"] != "retryable" or row["cancellation_requested"]:
            raise ValueError("Only failed daily jobs with a retryable execution failure may be retried")
        if (payload.get("worker_identity") != worker_identity()
                or payload.get("code_identity") != api.implementation_fingerprint()):
            raise ValueError("Implementation changed; submit a new daily job")
        source = ConnectionEngine(connection)
        saved = DailyStore(source).get(job_id, "inputs")
        if saved is not None:
            inputs = api.restore_daily_inputs(saved["inputs"])
            api.verify_daily_source(source, inputs)
            prior = connection.execute(text("""SELECT publication_id FROM at_lng.vol_surface_publications
                WHERE commodity=:product AND cob_date<:cob AND is_active AND status='published'
                ORDER BY cob_date DESC,published_at DESC LIMIT 1 FOR SHARE"""),
                {"product":inputs.product, "cob":inputs.cob}).scalar_one_or_none()
            if (str(prior) if prior else None) != saved["base"].get("publication_id"):
                raise ValueError("The preceding active publication changed; submit a new daily job")
        elif not payload.get("awaiting_source"):
            raise ValueError("Daily job has no pinned inputs")
        maximum = row["max_attempts"] + 3
        connection.execute(text("""INSERT INTO at_lng.vol_settlement_retry_audit
            (job_id,actor,reason,prior_attempts,prior_max_attempts,new_max_attempts)
            VALUES (:job,:actor,:reason,:attempts,:prior,:maximum)"""),
            {"job":job_id, "actor":identity.subject, "reason":safe_failure_reason(reason),
             "attempts":row["attempts"], "prior":row["max_attempts"], "maximum":maximum})
        connection.execute(text("""UPDATE at_lng.vol_calibration_jobs SET status='queued',max_attempts=:maximum,
            next_attempt_at=clock_timestamp(),finished_at=NULL,worker_id=NULL,lease_expires_at=NULL,
            updated_at=clock_timestamp() WHERE job_id=:job"""), {"job":job_id, "maximum":maximum})
    return PostgresJobRepository(engine).get(job_id=job_id)


def describe_daily(engine, job_id, *, identity):
    authorize(identity, Permission.VIEW)
    job = PostgresJobRepository(engine).get(job_id=job_id)
    if job is None or job.created_by != identity.subject or job.payload.get("job_type") != JOB_TYPE:
        raise PermissionError("Daily job is unavailable for this owner")
    return {"job_id": str(job.job_id), "product": job.payload["product"], "cob": job.payload["cob"],
            "status": job.status.value, "attempts": job.attempts, "max_attempts": job.max_attempts,
            "failure_kind": job.failure_kind,
            "reason": safe_failure_reason(job.last_error) if job.last_error and job.status.value != "succeeded" else None,
            "completed_items": job.completed_items, "total_items": job.total_items,
            "stage": DailyStore(engine).status(job.job_id)}


def run_daily_batch(engine, job_ids, *, identity, overlap=True):
    """Own one bounded fitting pool across independently recovered products."""
    with observed_fit_session():
        return _run_daily_batch(engine, job_ids, identity=identity, overlap=overlap)


def _run_daily_batch(engine, job_ids, *, identity, overlap=True):
    """Isolate product failures while retaining the pre/post full history scrub."""
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
    def execute(job):
        result = None
        for attempt in range(job.max_attempts):
            try:
                result = run_daily(engine, job.job_id, identity=identity)
                if result.status.value != "queued":
                    break
            except Exception:
                result = PostgresJobRepository(engine).get(job_id=job.job_id)
                if result is None or result.status.value != "queued":
                    break
            if attempt < job.max_attempts - 1:
                time.sleep(min(60, 5 * 2 ** max(0, result.attempts - 1)))
        return result

    try:
        # Duplicate products may intentionally depend on an earlier job's
        # activation. Keep their explicit submission order serial.
        distinct = len({job.payload["product"] for job in jobs}) == len(jobs)
        if overlap and distinct and len(jobs) > 1:
            token = _EXECUTION_SLOTS.set({"fit": Lock(), "publication": Lock()})
            try:
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="daily-product") as executor:
                    futures = [executor.submit(copy_context().run, execute, job) for job in jobs]
                    completed = [future.result() for future in futures]
            finally:
                _EXECUTION_SLOTS.reset(token)
        else:
            completed = [execute(job) for job in jobs]
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
    parser.add_argument("--resume", nargs="+", help="Resume one or more original job IDs")
    parser.add_argument("--status", help="Inspect an owned job without claiming or executing it")
    parser.add_argument("--retry-job", help="Grant three more attempts to an owned failed execution, then resume it")
    parser.add_argument("--reason", help="Required audit reason for --retry-job")
    parser.add_argument("--cleanup-stages", action="store_true", help="Inspect expired abandoned stages owned by this publisher")
    parser.add_argument("--apply-cleanup", action="store_true", help="Delete only the stages selected by --cleanup-stages")
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--timings", type=Path, help="Append credential-free stage events as JSONL")
    parser.add_argument("--serial-products", action="store_true", help="Disable fit/publication overlap for comparison or diagnosis")
    args = parser.parse_args()
    if args.retry_job and (args.resume or args.cob or args.publish or args.status or args.cleanup_stages):
        parser.error("--retry-job cannot be combined with other execution options")
    if bool(args.retry_job) != bool(args.reason and args.reason.strip()):
        parser.error("--retry-job requires a nonempty --reason; --reason is only used with --retry-job")
    if args.resume and (args.cob or args.publish):
        parser.error("--resume retains its saved date and mode; omit --cob and --publish")
    if args.status and (args.resume or args.cob or args.publish):
        parser.error("--status cannot be combined with execution options")
    if args.apply_cleanup and not args.cleanup_stages:
        parser.error("--apply-cleanup requires --cleanup-stages")
    if args.cleanup_stages and (args.resume or args.cob or args.publish or args.status):
        parser.error("--cleanup-stages cannot be combined with execution options")
    if not args.resume and not args.cob and not args.status and not args.cleanup_stages and not args.retry_job:
        parser.error("--cob or --resume is required")
    # A CLI has no trusted proxy headers. Only an explicitly configured local
    # workstation identity can resolve; remote service automation is separate.
    identity = resolve_request_identity({}, remote_addr="127.0.0.1")
    engine = get_database_engine()
    if args.cleanup_stages:
        try:
            authorize(identity, Permission.PUBLISH)
            print(json.dumps(api.prune_abandoned_stages(engine, actor=identity.subject,
                retention_days=args.retention_days, dry_run=not args.apply_cleanup), sort_keys=True))
        finally:
            engine.dispose()
        return
    if args.status:
        try:
            print(json.dumps(describe_daily(engine, args.status, identity=identity), default=str, sort_keys=True))
        finally:
            engine.dispose()
        return
    summaries = []
    command_id = str(uuid4())
    with ExitStack() as contexts:
        contexts.callback(engine.dispose)
        if args.timings:
            args.timings.parent.mkdir(parents=True, exist_ok=True)
            stream = contexts.enter_context(args.timings.open("a"))
            emit_lock = Lock()

            def emit(record):
                with emit_lock:
                    stream.write(json.dumps({"command_id": command_id, **record}, sort_keys=True) + "\n")
                    stream.flush()

            contexts.enter_context(api.calibration_trace(emit))
            from dash_options.calibration.execution.metadata import command_sql_trace, command_usage, execution_manifest
            contexts.enter_context(command_usage(emit))
            contexts.enter_context(command_sql_trace(engine, emit))
        if args.retry_job:
            args.resume = [str(retry_daily(engine, args.retry_job, identity=identity, reason=args.reason).job_id)]
        jobs = (
            [PostgresJobRepository(engine).get(job_id=key) for key in args.resume]
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
        if args.timings:
            args.timings.with_suffix(f".{command_id}.manifest.json").write_text(
                json.dumps(execution_manifest(command_id, jobs, worker_identity()), indent=2) + "\n")
        completed_jobs = run_daily_batch(engine, [job.job_id for job in jobs], identity=identity,
                                        overlap=not args.serial_products)
        for completed in completed_jobs:
            summaries.append(
                {
                    "job_id": str(completed.job_id),
                    "product": completed.payload["product"],
                    "status": completed.status.value,
                    "failure_kind": completed.failure_kind,
                    "reason": safe_failure_reason(completed.last_error) if completed.last_error and completed.status.value != "succeeded" else None,
                    "stage": DailyStore(engine).status(completed.job_id),
                }
            )
        print(json.dumps(summaries, default=str, sort_keys=True))
        if any(job.status.value != "succeeded" for job in completed_jobs):
            raise SystemExit(2)


if __name__ == "__main__":
    main()
