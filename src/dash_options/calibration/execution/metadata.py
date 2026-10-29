"""Credential-free provenance for comparable cold and resumed executions."""

import hashlib
import logging
import os
import platform
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import version
from time import perf_counter

from options.vol_calibration import api
from sqlalchemy import event

from dash_options.calibration.execution.settings import PublicationSettings
from dash_options.calibration.execution.fit_pool import worker_budget


@contextmanager
def command_sql_trace(engine, emit):
    """Count this command's engine executions, including heartbeat operations.

    Query identities are hashes; statements and parameters are never emitted.
    Time is DBAPI execution/wait, not isolated server CPU. COPY payloads retain
    their separate analytics transfer events. Listener lifetime is command-scoped.
    """
    def before(connection, cursor, statement, parameters, context, executemany):
        context._daily_sql_observation = (
            perf_counter(), hashlib.sha256(statement.encode()).hexdigest()[:16], bool(executemany),
            datetime.now(timezone.utc).isoformat(), api.calibration_timing_context(),
        )

    def record(context, cursor, outcome):
        observation = getattr(context, '_daily_sql_observation', None)
        if observation is None:
            return
        started, query, many, at, scope = observation
        try:
            emit({'stage':'command_sql', 'seconds':perf_counter()-started,
                  'query_identity':query, 'executemany':many, 'outcome':outcome,
                  'rows':cursor.rowcount if cursor is not None else None,
                  'started_at':at, **scope})
        except Exception as exc:
            logging.getLogger(__name__).warning('SQL telemetry unavailable: %s', type(exc).__name__)

    def after(connection, cursor, statement, parameters, context, executemany):
        record(context, cursor, 'succeeded')

    def failed(error):
        record(error.execution_context, None, 'failed')

    listeners = [('before_cursor_execute',before), ('after_cursor_execute',after), ('handle_error',failed)]
    for name, listener in listeners:
        event.listen(engine, name, listener)
    try:
        yield
    finally:
        for name, listener in listeners:
            event.remove(engine, name, listener)


def _usage():
    times = os.times()
    value = {"process_cpu_seconds": times.user + times.system,
             "reaped_children_cpu_seconds": times.children_user + times.children_system,
             "process_peak_rss_bytes": None, "reported_children_peak_rss_bytes": None}
    try:
        import resource
    except ImportError:
        value["reaped_children_cpu_seconds"] = None
        return value
    scale = 1 if platform.system() == "Darwin" else 1024
    value["process_peak_rss_bytes"] = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale)
    value["reported_children_peak_rss_bytes"] = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale)
    return value


def _safe_usage():
    try:
        return _usage()
    except Exception as exc:
        logging.getLogger(__name__).warning("Resource counters unavailable: %s", type(exc).__name__)
        return dict.fromkeys(("process_cpu_seconds", "reaped_children_cpu_seconds",
                              "process_peak_rss_bytes", "reported_children_peak_rss_bytes"))


@contextmanager
def command_usage(emit):
    """Dedicated CLI usage; child CPU becomes visible after batch pool shutdown.

    RSS fields are kernel-reported lifetime peaks, never a sum of concurrent
    worker memory. Unsupported platforms retain explicit unavailable values.
    """
    before, started, outcome = _safe_usage(), perf_counter(), "failed"
    try:
        yield
        outcome = "succeeded"
    finally:
        after, elapsed = _safe_usage(), perf_counter() - started
        record = {"stage": "command_resource_usage", "seconds": elapsed, "outcome": outcome,
                  "memory_scope": "kernel-reported lifetime peaks; not aggregate simultaneous RSS"}
        for name in ("process_cpu_seconds", "reaped_children_cpu_seconds"):
            record[name] = (max(0., after[name] - before[name])
                            if after[name] is not None and before[name] is not None else None)
        record.update({name: after[name] for name in ("process_peak_rss_bytes", "reported_children_peak_rss_bytes")})
        record.update({"baseline_" + name: before[name]
                       for name in ("process_peak_rss_bytes", "reported_children_peak_rss_bytes")})
        record["average_cpu_cores"] = (sum(record[name] for name in (
            "process_cpu_seconds", "reaped_children_cpu_seconds")) / max(elapsed, 1e-9)
            if record["reaped_children_cpu_seconds"] is not None and record["process_cpu_seconds"] is not None else None)
        try:
            emit(record)
        except Exception as exc:
            logging.getLogger(__name__).warning("Resource telemetry unavailable: %s", type(exc).__name__)


def execution_manifest(command_id, jobs, worker_identity):
    return {
        "schema": 1, "command_id": command_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
        "cpu_count": os.cpu_count(), "workers": worker_budget("DAILY_OBSERVED_FIT_WORKERS"),
        "dependencies": {name: version(name) for name in ("at-options-analytics", "numpy", "pandas", "scipy", "SQLAlchemy", "dash")},
        "analytics_identity": api.implementation_fingerprint(), "worker_identity": worker_identity,
        "publication_budgets": PublicationSettings.from_environment().arguments(),
        "jobs": [{"job_id": str(job.job_id), "product": job.payload["product"], "cob": job.payload["cob"],
                  "input_identity": job.payload["input_identity"], "attempts_before_run": job.attempts,
                  "status_before_run": job.status.value} for job in jobs],
    }
