"""Detached, resumable TTF/JKM settlement calibration.

Only a completed job produces a Dash batch candidate.  Publication remains a
separate, explicitly authorized page action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
from functools import lru_cache
from io import StringIO
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import scipy

from options.vol_calibration.api import implementation_fingerprint
from options.vol_calibration.api import numeric_verified_checkpoint
from options.vol_calibration.api import load_market_data_with_metadata
from options.vol_calibration.api import get_database_engine
from options.vol_calibration.api import (
    CalibrationCancelled,
    StaleCalibrationCheckpoint,
    canonical,
    digest,
)
from vol_calibration.jobs import JobStatus, PostgresJobRepository
from source_identity import source_config_fingerprint
from vol_calibration.feature_flags import gas_batch_jobs_enabled
from vol_calibration.ttf_market_context import load_ttf_trading_context


JOB_TYPE = "gas_settlement_batch_v3"
JOB_PAYLOAD_SCHEMA_VERSION = 3
LEASE_SECONDS = 300
_DASH_ROOT = Path(__file__).resolve().parents[1]
_CODE_FILES = (
    _DASH_ROOT / "vol_calibration/batch_job_runner.py",
    _DASH_ROOT / "source_identity.py",
    _DASH_ROOT / "surface_data.py",
    _DASH_ROOT / "snapshot_cache.py",
    _DASH_ROOT / "dataframe_utils.py",
    _DASH_ROOT / "runtime_config.py",
    _DASH_ROOT / "db_fallback.py",
    _DASH_ROOT / "vol_calibration/jobs.py",
    _DASH_ROOT / "vol_calibration/feature_flags.py",
    _DASH_ROOT / "vol_calibration/batch_results.py",
    _DASH_ROOT / "vol_calibration/batch_adapter.py",
    _DASH_ROOT / "vol_calibration/jkm_batch.py",
    _DASH_ROOT / "vol_calibration/ttf_batch.py",
    _DASH_ROOT / "vol_calibration/observed_fit_pool.py",
    _DASH_ROOT / "vol_calibration/ttf_market_context.py",
)


def background_jobs_enabled() -> bool:
    return gas_batch_jobs_enabled()


@lru_cache(maxsize=4)
def _repository_for_source(_source_fingerprint: str) -> PostgresJobRepository:
    engine = get_database_engine(strict=True)
    if engine is None:
        raise RuntimeError("Calibration database is unavailable.")
    return PostgresJobRepository(engine)


def job_repository() -> PostgresJobRepository:
    return _repository_for_source(source_config_fingerprint())


def code_fingerprint() -> str:
    state = hashlib.sha256()
    state.update(implementation_fingerprint().encode("utf-8"))
    for path in _CODE_FILES:
        label = "dashboard/" + path.relative_to(_DASH_ROOT).as_posix()
        state.update(label.encode())
        state.update(path.read_bytes())
    state.update(json.dumps({
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scipy": scipy.__version__,
    }, sort_keys=True).encode())
    return state.hexdigest()


def _market(market_data_json: str) -> pd.DataFrame:
    return pd.read_json(StringIO(market_data_json), orient="split")


def _expiries(market_data: pd.DataFrame) -> list:
    expiries = sorted(market_data["expiry"].dropna().unique())
    if not expiries:
        raise ValueError("No expiry inputs were provided.")
    dates = []
    for expiry in expiries:
        frame = market_data.loc[market_data["expiry"] == expiry]
        values = pd.to_datetime(frame["option_expiration_date"]).dt.date.unique()
        if len(values) != 1:
            raise ValueError(f"{expiry} lacks one actual option expiration date.")
        dates.append(values[0])
    if len(set(dates)) != len(dates):
        raise ValueError("Each contract must have a distinct option expiration date.")
    return list(zip(expiries, dates))


def build_payload(
    *, product: str, trade_date: str, market_data_json: str,
    table_data: list[dict], skip_good: bool, node_store=None,
    publication_payload=None,
) -> dict:
    product = product.upper()
    if product not in {"TTF", "JKM"}:
        raise ValueError("Background batch is supported for TTF and JKM.")
    market = _market(market_data_json)
    _expiries(market)
    publication_reference = (
        {
            key: publication_payload.get(key)
            for key in (
                "publication_id", "input_manifest_fingerprint",
                "published_at", "publication_date", "policy_version",
            )
        }
        if isinstance(publication_payload, dict) else None
    )
    payload = {
        "schema_version": JOB_PAYLOAD_SCHEMA_VERSION,
        "job_type": JOB_TYPE,
        "product": product,
        "trade_date": pd.Timestamp(trade_date).date().isoformat(),
        "market_data_json": market_data_json,
        "table_data": table_data,
        "skip_good": bool(skip_good),
        "node_store": node_store if product == "TTF" else None,
        "publication_payload": publication_reference,
        "code_fingerprint": code_fingerprint(),
    }
    return canonical(payload)


def submit_batch(repo: PostgresJobRepository, payload: dict, *, created_by: str):
    dates = [date for _, date in _expiries(_market(payload["market_data_json"]))]
    key = digest({"payload": payload, "created_by": created_by})
    args = dict(
        run_id=None,
        job_type=JOB_TYPE,
        payload=payload,
        created_by=created_by,
        max_attempts=3,
        total_items=len(dates),
        item_dates=dates,
    )
    job = repo.submit(idempotency_key=key, **args)
    if job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
        # A later explicit confirmation may retry unchanged inputs after a
        # transient source outage or a user cancellation. Keep the old job.
        job = repo.submit(
            idempotency_key=digest({"original_key": key, "retry": str(uuid4())}),
            **args,
        )
    return job


def start_worker(job_id: UUID) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "vol_calibration.batch_job_runner", str(job_id)],
        cwd=_DASH_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
    )


def _fresh_source(product: str, trade_date: str) -> pd.DataFrame:
    if product == "TTF":
        context = load_ttf_trading_context(trade_date, refresh=True)
        if context.get("error"):
            raise RuntimeError(str(context["error"]))
        return context["market_data"]
    result = load_market_data_with_metadata(
        product, trade_date, allow_synthetic_fallback=False,
    )
    if result.get("error") or result.get("is_synthetic"):
        raise RuntimeError(str(result.get("error") or "Synthetic source refused."))
    return result["data"]


def _expiry_records(frame: pd.DataFrame, expiry) -> str:
    selected = frame.loc[pd.to_datetime(frame["expiry"]) == pd.Timestamp(expiry)]
    records = selected.to_json(orient="records", date_format="iso", double_precision=10)
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return value

    return digest(sorted(
        [normalize(row) for row in json.loads(records)],
        key=lambda row: json.dumps(row, sort_keys=True),
    ))


def changed_source_expiry(snapshot: pd.DataFrame, fresh: pd.DataFrame):
    if fresh is None or fresh.empty:
        raise RuntimeError("Fresh calibration source is unavailable.")
    snapshot_expiries = sorted(pd.to_datetime(snapshot["expiry"]).dropna().unique())
    fresh_expiries = sorted(pd.to_datetime(fresh["expiry"]).dropna().unique())
    for expiry in sorted(set(snapshot_expiries) | set(fresh_expiries)):
        if _expiry_records(snapshot, expiry) != _expiry_records(fresh, expiry):
            return expiry
    return None


def _checkpoint_items(repo, job_id):
    items = repo.list_items(job_id=job_id)
    checkpoints = {}
    invalid_date = None
    for item in items:
        if item["status"] not in {"succeeded", "skipped"}:
            continue
        saved = item.get("result_payload")
        if not isinstance(saved, dict) or not saved.get("expiry"):
            invalid_date = item["option_expiration_date"]
            break
        checkpoints[saved["expiry"]] = saved
    return items, checkpoints, invalid_date


def _run_claimed(repo, job, *, source_loader=_fresh_source):
    payload = job.payload
    if payload.get("schema_version") != JOB_PAYLOAD_SCHEMA_VERSION or payload.get("job_type") != JOB_TYPE:
        raise ValueError("Unsupported calibration job payload.")
    if payload.get("code_fingerprint") != code_fingerprint():
        raise ValueError("Calibration code or numerical runtime changed; resubmit the batch.")
    market = _market(payload["market_data_json"])
    expiry_dates = {
        pd.Timestamp(expiry).strftime("%Y-%m-%d"): date
        for expiry, date in _expiries(market)
    }
    worker_id = job.worker_id
    lost_lease = threading.Event()
    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.wait(30):
            try:
                if repo.heartbeat(
                    job_id=job.job_id, worker_id=worker_id,
                    lease_seconds=LEASE_SECONDS,
                ) is None:
                    lost_lease.set()
                    return
            except Exception:
                lost_lease.set()
                return

    heart = threading.Thread(target=heartbeat, daemon=True)
    heart.start()
    try:
        def check_cancel():
            current = repo.get(job_id=job.job_id)
            if (
                lost_lease.is_set() or current is None
                or current.status != JobStatus.RUNNING
                or current.worker_id != worker_id
                or current.cancellation_requested
            ):
                raise CalibrationCancelled("Calibration job was cancelled or lost its lease.")

        def check_source():
            check_cancel()
            changed = changed_source_expiry(
                market, source_loader(payload["product"], payload["trade_date"])
            )
            if changed is not None:
                expiry = pd.Timestamp(changed).strftime("%Y-%m-%d")
                date = expiry_dates.get(expiry)
                if date is not None:
                    repo.invalidate_from(
                        job_id=job.job_id, worker_id=worker_id,
                        option_expiration_date=date,
                    )
                raise ValueError(
                    f"The source changed from {expiry}; reload inputs and submit a new batch."
                )

        check_source()
        for _ in range(len(expiry_dates) + 1):
            items, checkpoints, invalid_date = _checkpoint_items(repo, job.job_id)
            if invalid_date is not None:
                if repo.invalidate_from(
                    job_id=job.job_id, worker_id=worker_id,
                    option_expiration_date=invalid_date,
                ) is None:
                    raise CalibrationCancelled("Could not invalidate an incomplete checkpoint.")
                continue

            def save_checkpoint(saved):
                check_cancel()
                item = repo.claim_item(job_id=job.job_id, worker_id=worker_id)
                expected_date = expiry_dates[saved["expiry"]]
                if item is None or item["option_expiration_date"] != expected_date:
                    raise RuntimeError("The next persisted expiry is out of chronological order.")
                status = saved["result_row"]["status"]
                recorded = repo.complete_item(
                    job_id=job.job_id,
                    item_id=item["item_id"],
                    worker_id=worker_id,
                    status={"Success": "succeeded", "Skipped": "skipped", "Failed": "failed"}[status],
                    result_payload=saved,
                    input_fingerprint=saved["input_fingerprint"],
                    dependency_fingerprint=saved["dependency_fingerprint"],
                    last_error=saved["result_row"].get("error"),
                )
                if recorded is None:
                    raise CalibrationCancelled("Checkpoint could not be committed by this worker.")

            try:
                if payload["product"] == "TTF":
                    from vol_calibration.ttf_batch import calibrate_ttf_batch
                    outcome = calibrate_ttf_batch(
                        market, [dict(row) for row in payload["table_data"]],
                        skip_good=payload["skip_good"],
                        node_store=payload["node_store"],
                        checkpoints=checkpoints,
                        checkpoint_callback=save_checkpoint,
                        cancellation_check=check_cancel,
                    )
                else:
                    from vol_calibration.jkm_batch import calibrate_jkm_batch
                    outcome = calibrate_jkm_batch(
                        market, [dict(row) for row in payload["table_data"]],
                        skip_good=payload["skip_good"],
                        checkpoints=checkpoints,
                        checkpoint_callback=save_checkpoint,
                        cancellation_check=check_cancel,
                    )
            except StaleCalibrationCheckpoint as exc:
                date = expiry_dates[exc.expiry]
                if repo.invalidate_from(
                    job_id=job.job_id, worker_id=worker_id,
                    option_expiration_date=date,
                ) is None:
                    raise CalibrationCancelled("Could not invalidate stale checkpoints.")
                continue
            check_cancel()
            if outcome["fail_count"]:
                repo.fail(
                    job_id=job.job_id, worker_id=worker_id,
                    last_error=f"{outcome['fail_count']} expiries failed calibration.",
                )
                return
            check_source()
            if repo.complete(job_id=job.job_id, worker_id=worker_id) is None:
                raise CalibrationCancelled("Complete batch could not be committed.")
            return
        raise RuntimeError("Checkpoint dependencies did not converge.")
    finally:
        stop_heartbeat.set()
        heart.join(timeout=1)


def run_one(repo: PostgresJobRepository, job_id: UUID, *, source_loader=_fresh_source):
    worker_id = f"{platform.node()}:{os.getpid()}:{uuid4().hex[:8]}"
    job = repo.claim(job_id=job_id, worker_id=worker_id, lease_seconds=LEASE_SECONDS)
    if job is None:
        return repo.get(job_id=job_id)
    try:
        _run_claimed(repo, job, source_loader=source_loader)
    except CalibrationCancelled:
        pass
    except ValueError as exc:
        repo.abort(job_id=job_id, worker_id=worker_id, last_error=str(exc))
    except Exception as exc:
        repo.fail(job_id=job_id, worker_id=worker_id, last_error=str(exc))
    return repo.get(job_id=job_id)


def completed_batch(repo: PostgresJobRepository, job_id: UUID) -> dict:
    job = repo.get(job_id=job_id)
    if job is None or job.status != JobStatus.SUCCEEDED:
        raise ValueError("The batch job has not completed successfully.")
    if (
        job.payload.get("schema_version") != JOB_PAYLOAD_SCHEMA_VERSION
        or job.payload.get("job_type") != JOB_TYPE
        or job.payload.get("code_fingerprint") != code_fingerprint()
    ):
        raise ValueError("Calibration code or numerical runtime changed; resubmit the batch.")
    items, _, invalid_date = _checkpoint_items(repo, job_id)
    if invalid_date is not None:
        raise ValueError("The saved batch has an incomplete checkpoint.")
    if len(items) != job.total_items or any(
        item["status"] not in {"succeeded", "skipped"} for item in items
    ):
        raise ValueError("The saved batch is incomplete.")
    base = [dict(row) for row in job.payload["table_data"]]
    from vol_calibration.batch_adapter import numeric_parameter_rows, format_numeric_outcome
    parameters = numeric_parameter_rows(base)
    from options.vol_calibration.api import expiry_month
    row_by_period = {expiry_month(row["expiry"]): index for index, row in enumerate(base)}
    results = []
    for item in items:
        saved = item["result_payload"]
        numeric_verified_checkpoint(
            saved,
            expiry=saved["expiry"],
            input_fingerprint=item["input_fingerprint"],
            dependency_fingerprint=item["dependency_fingerprint"],
        )
        parameters[row_by_period[expiry_month(saved["expiry"])]] = saved["updated_row"]
        results.append(saved["result_row"])
    numeric = {
        "results": results,
        "parameter_rows": parameters,
        "success_count": sum(row["status"] == "Success" for row in results),
        "skip_count": sum(row["status"] == "Skipped" for row in results),
        "fail_count": 0,
    }
    return {**format_numeric_outcome(numeric, base), "payload": job.payload}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one durable gas calibration batch")
    parser.add_argument("job_id", type=UUID)
    args = parser.parse_args(argv)
    engine = get_database_engine(strict=True)
    if engine is None:
        raise RuntimeError("Calibration database is unavailable.")
    repo = PostgresJobRepository(engine)
    for _ in range(3):
        job = run_one(repo, args.job_id)
        if job is None or job.status != JobStatus.QUEUED:
            break
    return 0 if job is not None and job.status == JobStatus.SUCCEEDED else 1


if __name__ == "__main__":
    raise SystemExit(main())
