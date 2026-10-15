"""Bounded background preview calculation; actor-scoped immutable references."""
import hashlib
import json
import logging
import os
import threading
import time
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor
from diskcache import Lock

from options.vol_calibration.api import (load_tfo_intraday_inputs, fit_tfo_intraday, implementation_fingerprint,
    tfo_intraday_fit_identity, rebind_tfo_intraday_candidate)
from snapshot_cache import snapshot_store, SnapshotReferenceError
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
    scope = namespace(actor)+":scope:"+str(UUID(request['scope']))
    with _LOCK, snapshot_store.cache.transact():
        snapshot_store.cache.set(scope, key, expire=TTL)
        state = snapshot_store.cache.get(key)
        if state and state['status']=='complete':
            try:
                resolve(state['ref'], actor)
            except SnapshotReferenceError:
                state = None
        if state and (state['status']=='complete' or
                      state['status']=='running' and time.time()-state['started']<300):
            return {"key": key, "request_key": request["key"]}
        if len(_JOBS)>=4:
            raise ValueError("Intraday queue is busy. Reopen the snapshot shortly.")
        state = {"status": "running", "started": time.time(), "owner": os.getpid()}
        snapshot_store.cache.set(key, state, expire=TTL)
        def run():
            # Submission must commit its scope/claim before the worker checks
            # supersession; SQLite readers can otherwise see the prior scope.
            with _LOCK:
                pass
            execution_started = time.monotonic()
            def cancelled():
                return time.monotonic()-execution_started>240 or snapshot_store.cache.get(scope)!=key
            try:
                if cancelled():
                    raise InterruptedError('Intraday fit superseded')
                inputs = load_tfo_intraday_inputs(engine, request["snapshot_id"], start=request["start"], end=request["end"])
                fit_key = namespace(actor)+':fit:'+tfo_intraday_fit_identity(inputs['manifest'])
                with Lock(snapshot_store.cache, fit_key+':lock', expire=300):
                    if cancelled():
                        raise InterruptedError('Intraday fit superseded')
                    ref = snapshot_store.cache.get(fit_key)
                    cached = None
                    if ref:
                        try:
                            cached = resolve(ref, actor)
                        except SnapshotReferenceError:
                            pass
                    if cached is None:
                        value = fit_tfo_intraday(inputs, cancel_check=cancelled)
                        snapshot_store.cache.set(fit_key, retain(value, actor), expire=TTL)
                    else:
                        value = rebind_tfo_intraday_candidate(cached, inputs['manifest'])
                state.update(status="complete", ref=retain(value, actor))
                state['execution_seconds'] = time.monotonic()-execution_started
                state['fit_reused'] = cached is not None
            except InterruptedError:
                state.update(status='failed', error='Proposal superseded by a newer market selection.')
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
