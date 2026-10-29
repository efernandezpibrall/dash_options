"""Shared preview execution; each product retains its own pool and actor namespace."""

import hashlib
import json
import logging
import os
import threading
import time
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor
from diskcache import Lock

from options.vol_calibration import api
from options.vol_calibration.api import implementation_fingerprint
from dash_options.core.snapshots import snapshot_store, SnapshotReferenceError
from dash_options.core.source_identity import source_config_fingerprint

TTL = 3600


class IntradayPreviewJobs:
    def __init__(self, prefix, *, load_inputs, fit, fit_identity, rebind_candidate):
        self.prefix = prefix
        self.load_inputs = load_inputs
        self.fit = fit
        self.fit_identity = fit_identity
        self.rebind_candidate = rebind_candidate
        self.snapshot_store = snapshot_store
        self.source_config_fingerprint = source_config_fingerprint
        self._POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{prefix}-intraday")
        self._LOCK = threading.RLock()
        self._JOBS = {}
        self.log = logging.getLogger(f"{__name__}.{prefix}")

    def namespace(self, actor):
        return (
            self.prefix
            + "-intraday-v1:"
            + hashlib.sha256((actor + ":" + self.source_config_fingerprint()).encode()).hexdigest()
        )

    def retain(self, candidate, actor):
        return self.snapshot_store.publish(
            self.namespace(actor) + ":candidate", candidate["candidate_id"], candidate, ttl_seconds=TTL
        )

    def resolve(self, ref, actor):
        return self.snapshot_store.resolve(ref, expected_namespace=self.namespace(actor) + ":candidate")

    def submit(self, engine, request, actor):
        digest = hashlib.sha256(
            json.dumps([request, implementation_fingerprint()], sort_keys=True).encode()
        ).hexdigest()
        key = self.namespace(actor) + ":job:" + digest
        scope = self.namespace(actor) + ":scope:" + str(UUID(request["scope"]))
        with self._LOCK, self.snapshot_store.cache.transact():
            self.snapshot_store.cache.set(scope, key, expire=TTL)
            state = self.snapshot_store.cache.get(key)
            if state and state["status"] == "complete":
                try:
                    self.resolve(state["ref"], actor)
                except SnapshotReferenceError:
                    state = None
            if state and (
                state["status"] == "complete" or state["status"] == "running" and time.time() - state["started"] < 300
            ):
                return {"key": key, "request_key": request["key"]}
            if len(self._JOBS) >= 4:
                raise ValueError("Intraday queue is busy. Reopen the snapshot shortly.")
            state = {"status": "running", "started": time.time(), "owner": os.getpid()}
            self.snapshot_store.cache.set(key, state, expire=TTL)

            def run():
                # Submission must commit its scope/claim before the worker checks
                # supersession; SQLite readers can otherwise see the prior scope.
                with self._LOCK:
                    pass
                execution_started = time.monotonic()

                def cancelled():
                    return time.monotonic() - execution_started > 240 or self.snapshot_store.cache.get(scope) != key

                try:
                    if cancelled():
                        raise InterruptedError("Intraday fit superseded")
                    inputs = self.load_inputs(
                        engine, request["snapshot_id"], start=request["start"], end=request["end"]
                    )
                    fit_key = self.namespace(actor) + ":fit:" + self.fit_identity(inputs["manifest"])
                    with Lock(self.snapshot_store.cache, fit_key + ":lock", expire=300):
                        if cancelled():
                            raise InterruptedError("Intraday fit superseded")
                        ref = self.snapshot_store.cache.get(fit_key)
                        cached = None
                        if ref:
                            try:
                                cached = self.resolve(ref, actor)
                            except SnapshotReferenceError:
                                pass
                        if cached is None:
                            value = self.fit(inputs, cancel_check=cancelled)
                            self.snapshot_store.cache.set(fit_key, self.retain(value, actor), expire=TTL)
                        else:
                            value = self.rebind_candidate(cached, inputs["manifest"])
                    state.update(status="complete", ref=self.retain(value, actor))
                    state["execution_seconds"] = time.monotonic() - execution_started
                    state["fit_reused"] = cached is not None
                except InterruptedError:
                    state.update(status="failed", error="Proposal superseded by a newer market selection.")
                except Exception as exc:
                    self.log.exception("%s intraday preview failed", self.prefix)
                    state.update(
                        status="failed",
                        error=str(exc)
                        if isinstance(exc, ValueError)
                        else "Intraday calculation unavailable; check source readiness.",
                    )
                finally:
                    self.snapshot_store.cache.set(key, state, expire=TTL)
                    with self._LOCK:
                        self._JOBS.pop(key, None)

            future = self._POOL.submit(run)
            self._JOBS[key] = future
        return {"key": key, "request_key": request["key"]}

    def poll(self, ticket, actor):
        if not ticket.get("key", "").startswith(self.namespace(actor) + ":job:"):
            raise ValueError("Intraday job unavailable for this trader.")
        state = self.snapshot_store.cache.get(ticket["key"])
        if not state or (state["status"] == "running" and time.time() - state["started"] > 300):
            raise ValueError("Intraday worker restarted or timed out. Reload the snapshot.")
        return state


brent_jobs = IntradayPreviewJobs(
    "brent",
    load_inputs=api.load_brent_intraday_inputs,
    fit=api.fit_brent_intraday,
    fit_identity=api.brent_intraday_fit_identity,
    rebind_candidate=api.rebind_brent_intraday_candidate,
)
ttf_jobs = IntradayPreviewJobs(
    "tfo",
    load_inputs=api.load_tfo_intraday_inputs,
    fit=api.fit_tfo_intraday,
    fit_identity=api.tfo_intraday_fit_identity,
    rebind_candidate=api.rebind_tfo_intraday_candidate,
)
