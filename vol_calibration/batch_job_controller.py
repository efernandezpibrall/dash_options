"""Shared Dash lifecycle for gas batch candidates; publication stays product-owned."""

from __future__ import annotations

from dataclasses import dataclass, field
import getpass
from io import StringIO
from typing import Callable
from uuid import UUID

import dash_bootstrap_components as dbc
from dash import html, no_update
from dash.exceptions import PreventUpdate
from flask import has_request_context, request
import pandas as pd

from options.vol_calibration.api import digest
from vol_calibration import auth, batch_job_runner
from vol_calibration.components.batch_calibration_modal import (
    create_batch_results_table,
    create_batch_summary,
)
from vol_calibration.jobs import JobStatus


def _actor():
    identity = (
        auth.resolve_request_identity(request.headers, remote_addr=request.remote_addr)
        if has_request_context() else None
    )
    return identity.subject if identity and identity.subject else f"local:{getpass.getuser()}"


def _results_display(results):
    return html.Div([create_batch_summary(results), create_batch_results_table(results)])


@dataclass(frozen=True)
class BatchJobController:
    """Product adapters provide calibration and candidate validation, never writes.

    Construct per callback so editable nodes remain scoped to that invocation.
    The tuple order matches the existing gas workspace callback outputs.
    """

    product: str
    calibrate: Callable
    build_state: Callable
    state_ready: Callable
    payload_context: dict = field(default_factory=dict)

    def _badge(self, status, color):
        return dbc.Badge(f"{self.product} calibration {status}", color=color, pill=True)

    def _payload(self, trade_date, market_json, rows, skip_good, publication):
        return batch_job_runner.build_payload(
            product=self.product,
            trade_date=trade_date,
            market_data_json=market_json,
            table_data=rows,
            skip_good=skip_good,
            publication_payload=publication,
            **self.payload_context,
        )

    def run(self, triggered_id, market_json, rows, skip_options, trade_date, publication):
        prefix = self.product.lower()
        if triggered_id == f"{prefix}-batch-progress-close-btn":
            return False, 0, "", [], True, no_update, no_update, no_update, no_update
        if triggered_id != f"{prefix}-batch-confirm-btn":
            raise PreventUpdate
        if not market_json or not rows:
            return (True, 0, "Waiting for market inputs and parameter rows to load.", [], False,
                    None, no_update, no_update, None)
        skip_good = "skip_good" in (skip_options or [])
        if batch_job_runner.background_jobs_enabled():
            try:
                payload = self._payload(trade_date, market_json, rows, skip_good, publication)
                job = batch_job_runner.submit_batch(
                    batch_job_runner.job_repository(), payload, created_by=_actor(),
                )
                if job.status == JobStatus.QUEUED:
                    batch_job_runner.start_worker(job.job_id)
                return (
                    True, 0, "Queued: 0 expiries completed", [], True,
                    None, no_update, self._badge("queued", "info"),
                    {"job_id": str(job.job_id), "payload_fingerprint": digest(payload)},
                )
            except Exception as exc:
                return (
                    True, 0, f"Could not start calibration: {exc}", [], False,
                    None, no_update, self._badge("not started", "danger"), None,
                )

        outcome = self.calibrate(
            pd.read_json(StringIO(market_json), orient="split"), rows, skip_good=skip_good,
        )
        results, updated = outcome["results"], outcome["table_data"]
        success, skipped, failed = (
            outcome["success_count"], outcome["skip_count"], outcome["fail_count"]
        )
        badge = dbc.Badge(
            [html.I(className="fas fa-check me-1"), f"Calibrated {success} expiries"]
            if failed == 0 else [
                html.I(className="fas fa-exclamation-triangle me-1"),
                f"{success} OK, {failed} failed",
            ],
            color="success" if failed == 0 else "warning", pill=True,
        )
        state = self.build_state(trade_date, market_json, updated, publication, results)
        return (
            True, 100, f"Completed: {success} calibrated, {skipped} skipped, {failed} failed",
            _results_display(results), False, state, updated, badge, None,
        )

    def poll(self, reference, market_json, rows, trade_date, publication, skip_options, is_open, batch_state):
        if not batch_job_runner.background_jobs_enabled() or not reference:
            raise PreventUpdate
        try:
            repo = batch_job_runner.job_repository()
            job = repo.get(job_id=UUID(reference["job_id"]))
        except Exception:
            return (is_open, no_update, "Waiting for calibration database connection.",
                    no_update, True, no_update, no_update, no_update, True)
        if job is None:
            return (is_open, 0, "Calibration job is missing.", [], False,
                    None, no_update, no_update, True)
        progress = round(100 * job.completed_items / max(job.total_items, 1))
        if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            if job.status == JobStatus.QUEUED or (
                job.lease_expires_at is not None
                and job.lease_expires_at < pd.Timestamp.now(tz="UTC").to_pydatetime()
            ):
                try:
                    batch_job_runner.start_worker(job.job_id)
                except OSError as exc:
                    return (
                        is_open, progress, f"Could not start calibration worker: {exc}",
                        no_update, True, no_update, no_update, no_update, False,
                    )
            return (
                is_open, progress, f"{job.completed_items} of {job.total_items} expiries completed",
                no_update, True, no_update, no_update, self._badge("running", "info"), False,
            )
        if job.status != JobStatus.SUCCEEDED:
            return (
                is_open, progress, job.last_error or f"Calibration {job.status.value}.",
                [], False, None, no_update, self._badge("failed", "danger"), True,
            )
        try:
            if not market_json or not rows or not trade_date or publication is None:
                return (is_open, 100, "Waiting for page inputs to load.",
                        no_update, True, no_update, no_update, no_update, True)
            if isinstance(batch_state, dict) and batch_state.get("background_job_id") == str(job.job_id):
                ready, _ = self.state_ready(batch_state, trade_date, market_json, rows, publication)
                if ready:
                    return (is_open, 100, "Calibration complete.", no_update, False,
                            no_update, no_update, no_update, True)
            current = self._payload(
                trade_date, market_json, rows, "skip_good" in (skip_options or []), publication,
            )
            if digest(current) != reference["payload_fingerprint"]:
                raise ValueError("Inputs changed while the batch ran; reload and recalibrate.")
            outcome = batch_job_runner.completed_batch(repo, job.job_id)
            results, updated = outcome["results"], outcome["table_data"]
            state = self.build_state(trade_date, market_json, updated, publication, results)
            state["background_job_id"] = str(job.job_id)
            state["background_code_fingerprint"] = job.payload["code_fingerprint"]
            return (
                is_open, 100,
                f"Completed: {outcome['success_count']} calibrated, {outcome['skip_count']} skipped",
                _results_display(results), False, state, updated,
                self._badge("complete", "success"), True,
            )
        except Exception as exc:
            return (
                is_open, progress, f"Cannot apply calibration: {exc}", [], False,
                None, no_update, self._badge("needs review", "warning"), True,
            )

    def cancel(self, reference):
        if not batch_job_runner.background_jobs_enabled() or not reference:
            raise PreventUpdate
        job = batch_job_runner.job_repository().request_cancel(
            job_id=UUID(reference["job_id"]), created_by=_actor(),
        )
        return "Cancellation requested." if job else "Cancellation unavailable."
