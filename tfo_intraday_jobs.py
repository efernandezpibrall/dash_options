"""Bounded background preview calculation; actor-scoped immutable references."""
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from options.vol_calibration.api import load_tfo_intraday_inputs, fit_tfo_intraday, implementation_fingerprint
from snapshot_cache import snapshot_store
from source_identity import source_config_fingerprint

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tfo-intraday")
_LOCK = threading.RLock()
_JOBS = {}
TTL = 3600
LOG = logging.getLogger(__name__)


def namespace(actor):
    return "tfo-intraday-v1:"+hashlib.sha256((actor+":"+source_config_fingerprint()).encode()).hexdigest()


def retain(candidate, actor):
    return snapshot_store.publish(namespace(actor)+":candidate", candidate["candidate_id"], candidate, ttl_seconds=TTL)


def resolve(ref, actor):
    return snapshot_store.resolve(ref, expected_namespace=namespace(actor)+":candidate")


def submit(engine, request, actor):
    digest = hashlib.sha256(json.dumps([request, implementation_fingerprint()], sort_keys=True).encode()).hexdigest()
    key = namespace(actor)+":job:"+digest
    with _LOCK:
        state = snapshot_store.cache.get(key)
        if state and time.time()-state["started"]<300:
            return {"key": key, "request_key": request["key"]}
        if len(_JOBS)>=4:
            raise ValueError("Intraday queue is busy. Reopen the snapshot shortly.")
        state = {"status": "running", "started": time.time(), "owner": os.getpid()}
        snapshot_store.cache.set(key, state, expire=TTL)
        def run():
            try:
                inputs = load_tfo_intraday_inputs(engine, request["snapshot_id"], start=request["start"], end=request["end"])
                value = fit_tfo_intraday(inputs, cancel_check=lambda: time.time()-state["started"]>240)
                state.update(status="complete", ref=retain(value, actor))
            except Exception as exc:
                LOG.exception("TFO intraday preview failed")
                state.update(status="failed", error=str(exc) if isinstance(exc, ValueError) else "Intraday calculation unavailable; check source readiness.")
            finally:
                snapshot_store.cache.set(key, state, expire=TTL)
                with _LOCK:
                    _JOBS.pop(key, None)
        future = _POOL.submit(run)
        _JOBS[key] = future
    return {"key": key, "request_key": request["key"]}


def poll(ticket, actor):
    if not ticket.get("key", "").startswith(namespace(actor)+":job:"):
        raise ValueError("Intraday job unavailable for this trader.")
    state = snapshot_store.cache.get(ticket["key"])
    if not state or (state["status"]=="running" and time.time()-state["started"]>300):
        raise ValueError("Intraday worker restarted or timed out. Reload the snapshot.")
    return state
