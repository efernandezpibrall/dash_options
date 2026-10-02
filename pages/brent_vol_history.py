"""Bloomberg option-chain history plus the official JKM market view."""

from __future__ import annotations

from vol_trades_workspace import chart_data, grids, history_cards, history_charts, history_layout, overlays, strip_charts, trade_tape as tape_views

import json
import hashlib
from types import SimpleNamespace
from typing import Any

import dash_bootstrap_components as dbc
import pandas as pd
from dash import (
    ALL,
    Input,
    Output,
    Patch,
    State,
    callback,
    clientside_callback,
    ctx,
    html,
    no_update,
)
from flask import has_request_context, request
from dash.exceptions import MissingCallbackContextException

from runtime_config import get_database_engine
from brent_option_chain_refresh import (
    INTRADAY_REQUEST_KIND,
    SETTLEMENT_REFRESH_PRODUCTS,
    SETTLEMENT_REQUEST_KIND,
    SUPPORTED_PRODUCTS,
    WORKER_FRESHNESS_SECONDS,
    SettlementRefreshBatchError,
    get_refresh_jobs,
    get_worker_readiness,
    get_worker_readiness_many,
    intraday_refresh_enabled,
    settlement_refresh_enabled,
    submit_settlement_refresh_jobs,
    submit_refresh_job,
)
from vol_calibration.auth import (
    Permission,
    authorize,
    resolve_request_identity,
)
from vol_calibration.feature_flags import inline_calibration_enabled
from vol_calibration.inline_workspace import (
    create_inline_workspace,
    resolve_inline_context,
)
from pages import ice_chat_quotes
from vol_trades_market_window import (
    MARKET_TIMEZONE, PRESET_SECONDS, filter_event_window, history_identity,
    market_context, select_market_window, utc_timestamp,
)
from vol_trades_ice_quotes import prepare_overlay_events
import vol_trades_data as market_data
import ice_quote_data as quote_data


def _safe_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: option-chain data could not be loaded"






layout = history_layout.build_layout(ice_chat_quotes.layout)


@callback(
    Output("brent-vol-history-calibration-open", "data"),
    Input("brent-vol-history-calibration-toggle", "n_clicks"),
    Input("brent-vol-history-product", "value"),
    Input("brent-vol-history-date", "value"),
    Input("brent-vol-history-snapshot", "data"),
    State("brent-vol-history-calibration-open", "data"),
    prevent_initial_call=True,
)
def update_inline_calibration_state(
    _n_clicks,
    product,
    selected_date,
    snapshot,
    state,
):
    current = state or {"open": False}
    trigger = ctx.triggered_id
    clicks = int(_n_clicks or 0)
    toggle_clicked = (
        "brent-vol-history-calibration-toggle.n_clicks" in ctx.triggered_prop_ids
    )
    if toggle_clicked and clicks:
        is_open = not bool(current.get("open"))
    elif bool(current.get("open")) and trigger in {
        "brent-vol-history-product",
        "brent-vol-history-date",
        "brent-vol-history-snapshot",
    }:
        is_open = True
    else:
        return no_update
    return {
        "open": is_open,
        "product": product,
        "selectedDate": selected_date,
        "snapshotId": (snapshot or {}).get("snapshot_id"),
        "revision": pd.Timestamp.now(tz="UTC").isoformat(),
    }


clientside_callback(
    """
    function(state) {
        const isOpen = Boolean(state && state.open);
        return [isOpen ? 'Close calibration' : 'Calibrate surface', String(isOpen)];
    }
    """,
    Output("brent-vol-history-calibration-toggle", "children"),
    Output("brent-vol-history-calibration-toggle", "aria-expanded"),
    Input("brent-vol-history-calibration-open", "data"),
)


@callback(
    Output("brent-vol-history-calibration-panel", "children"),
    Output("brent-vol-history-calibration-context", "data"),
    Input("brent-vol-history-calibration-open", "data"),
    State("brent-vol-history-snapshot", "data"),
    State("brent-vol-history-product", "value"),
    prevent_initial_call=True,
)
def render_inline_calibration(open_state, snapshot, product):
    if not inline_calibration_enabled() or not (open_state or {}).get("open"):
        return [], None
    try:
        context = resolve_inline_context(
            get_database_engine(required=False),
            snapshot,
            market_data._normalize_product(product),
        )
        return create_inline_workspace(context), context
    except Exception as exc:
        message = str(exc)
        if market_data._normalize_product(product) == "ON" and "LNE" in message:
            message = (
                "Calibration is unavailable because no complete LNE SETTLEMENT "
                "snapshot exists for the selected ON COB. ON observations remain "
                "visible but are never calibration inputs."
            )
        return (
            dbc.Alert(
                [html.Strong("Calibration unavailable: "), message],
                color="warning",
                className="main-section-container mt-3",
            ),
            {
                "market_product": market_data._normalize_product(product),
                "available": False,
                "reason": message,
            },
        )


@callback(
    Output("brent-vol-history-oi-methodology", "children"),
    Input("brent-vol-history-product", "value"),
)
def render_oi_methodology(product):
    return history_cards._oi_methodology_children(market_data._normalize_product(product))


@callback(
    Output("brent-vol-history-trade-source-note", "children"),
    Output("brent-vol-history-chain-source-note", "children"),
    Input("brent-vol-history-product", "value"),
)
def render_market_source_notes(product):
    if market_data._normalize_product(product) != "JKM":
        return None, None
    common = {
        "color": "secondary",
        "className": "brent-vol-history-jkm-unavailable-note",
    }
    return (
        dbc.Alert("No Bloomberg trade tape configured for JKM.", **common),
        dbc.Alert(
            "No Bloomberg option chain or refresh is configured for JKM; "
            "the expiry charts above show official ICAP nodes and ICE_JKM_MO forwards.",
            **common,
        ),
    )


def _request_identity():
    headers = request.headers if has_request_context() else {}
    remote_addr = request.remote_addr if has_request_context() else None
    return resolve_request_identity(headers, remote_addr=remote_addr)


def _authorize_refresh():
    identity = _request_identity()
    authorize(identity, Permission.REFRESH_BLOOMBERG)
    return identity


REFRESH_STAGE_LABELS = {
    "queued": "Waiting for Bloomberg worker",
    "discovery": "Discovering chain",
    "market_data": "Fetching market data",
    "quote_cut": "Capturing synchronized quotes",
    "trade_detection": "Detecting changed trades",
    "trade_ticks": "Confirming exact option ticks",
    "future_match": "Matching trade-time futures",
    "pricing": "Pricing executable implied volatility",
    "persistence": "Persisting snapshot",
    "reconciliation": "Reconciling database readback",
}

SETTLEMENT_REFRESH_STAGE_LABELS = {
    "queued": "Waiting for Bloomberg worker",
    "availability": "Checking Bloomberg settlements",
    "coverage": "Checking settlement coverage",
    "planning": "Checking settlement coverage",
    "settlement_availability": "Checking Bloomberg settlements",
    "settlement_coverage": "Checking settlement coverage",
    "settlement_planning": "Checking settlement coverage",
    "discovery": "Updating option universe",
    "universe": "Updating option universe",
    "universe_reconciliation": "Updating option universe",
    "market_data": "Fetching settlement history",
    "historical_data": "Fetching settlement history",
    "settlement_history": "Fetching settlement history",
    "pricing": "Pricing settlement volatility",
    "persistence": "Saving settlements",
    "settlement_persistence": "Saving settlements",
    "reconciliation": "Reconciling settlement readback",
    "settlement_reconciliation": "Reconciling settlement readback",
}


def _job_request_kind(job, active_job=None) -> str:
    request_kind = getattr(job, "request_kind", None)
    if not request_kind and active_job:
        request_kind = active_job.get("request_kind")
    return str(request_kind or INTRADAY_REQUEST_KIND).strip().lower()


def _short_job_date(value: Any) -> str | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return f"{parsed.day} {parsed.strftime('%b')}"


def _settlement_metric_dates(metrics: dict[str, Any], key: str) -> list[str]:
    values = metrics.get(key) or []
    if isinstance(values, (str, pd.Timestamp)):
        values = [values]
    return [
        formatted
        for formatted in (_short_job_date(value) for value in values)
        if formatted
    ]


def _latest_settlement_label(metrics: dict[str, Any]) -> str | None:
    for key in (
        "latest_settlement_date",
        "latest_available_settlement_date",
        "settlement_cutoff_date",
    ):
        formatted = _short_job_date(metrics.get(key))
        if formatted:
            return formatted
    completed = _settlement_metric_dates(metrics, "completed_dates")
    return completed[-1] if completed else None


def _settlement_success_message(job) -> str:
    metrics = dict(getattr(job, "metrics", None) or {})
    latest = _latest_settlement_label(metrics)
    through = f" through {latest}" if latest else ""
    result_status = str(getattr(job, "result_status", "") or "").lower()
    if result_status in {"noop", "fresh_reuse", "current", "settlements_current"}:
        return f"Settlements already current{through}."

    pending = _settlement_metric_dates(metrics, "oi_pending_dates")
    if not pending:
        pending = _settlement_metric_dates(metrics, "activity_pending_dates")
    failed = _settlement_metric_dates(metrics, "failed_dates")
    prefix = "Settlements partially refreshed" if failed else "Settlements refreshed"
    details = []
    if failed:
        details.append(f"{', '.join(failed)} unavailable")
    if pending:
        details.append(f"OI pending for {', '.join(pending)}")
    suffix = f" · {' · '.join(details)}" if details else ""
    return f"{prefix}{through}{suffix}."


def _refresh_stage_label(job, request_kind: str) -> str:
    stage = str(getattr(job, "stage", "") or "queued")
    labels = (
        SETTLEMENT_REFRESH_STAGE_LABELS
        if request_kind == SETTLEMENT_REQUEST_KIND
        else REFRESH_STAGE_LABELS
    )
    label = labels.get(stage, stage.replace("_", " ").title())
    if request_kind != SETTLEMENT_REQUEST_KIND or "Saving" not in label:
        return label
    metrics = dict(getattr(job, "metrics", None) or {})
    completed = len(metrics.get("completed_dates") or [])
    planned = len(metrics.get("planned_dates") or [])
    if planned:
        return f"{label} ({completed}/{planned})"
    return label


def _worker_readiness_message(readiness, product_label: str) -> str:
    if readiness.ready:
        return f"Bloomberg {product_label} worker ready."
    if readiness.reason == "registry_unavailable":
        return (
            "Bloomberg worker status is unavailable. Apply migration 010 and "
            "start the worker."
        )
    if readiness.reason == "no_eligible_worker":
        return (
            f"No Bloomberg worker is enabled for {product_label}. Enable "
            f"{readiness.product} and start the worker."
        )
    if readiness.reason == "stale_heartbeat":
        return (
            f"Bloomberg {product_label} worker is offline—no heartbeat in "
            f"{WORKER_FRESHNESS_SECONDS} seconds. Start or restart the worker."
        )
    return f"Bloomberg {product_label} worker is offline. Start or restart it."


def _queued_job_wait_exceeded(job, *, now: Any = None) -> bool:
    created_at = pd.to_datetime(
        getattr(job, "updated_at", None) or getattr(job, "created_at", None),
        errors="coerce",
        utc=True,
    )
    if pd.isna(created_at):
        return False
    current = pd.to_datetime(now, errors="coerce", utc=True)
    if pd.isna(current):
        current = pd.Timestamp.now(tz="UTC")
    return (current - created_at).total_seconds() > WORKER_FRESHNESS_SECONDS


def _product_store(value: Any, payload_key: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    if payload_key in value:
        product = market_data._normalize_product(value.get("product"))
        return {product: dict(value)}
    return {
        market_data._normalize_product(product): dict(payload)
        for product, payload in value.items()
        if str(product).upper() in SUPPORTED_PRODUCTS and isinstance(payload, dict)
    }


def _completion_payload(job, product: str, request_kind: str) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "product": product,
        "request_kind": request_kind,
        "result_snapshot_id": job.result_snapshot_id,
        "result_status": job.result_status,
        "updated_at": job.updated_at,
    }


def _job_is_terminal(job: Any) -> bool:
    status = getattr(job, "status", None)
    if status is None and isinstance(job, dict):
        status = job.get("status")
    return str(status or "").lower() in {"succeeded", "failed"}


def _settlement_batch_status(jobs: dict[str, Any]) -> tuple[str, str]:
    terminal_count = 0
    failure_count = 0
    has_warning = False
    summaries = []
    for product in SETTLEMENT_REFRESH_PRODUCTS:
        job = jobs.get(product)
        if job is None:
            continue
        if job.status == "succeeded":
            terminal_count += 1
            failed_dates = bool((getattr(job, "metrics", None) or {}).get("failed_dates"))
            is_partial = (
                str(getattr(job, "result_status", "") or "").lower() == "partial"
                or failed_dates
            )
            has_warning = has_warning or is_partial
            detail = _settlement_success_message(job).removesuffix(".")
            detail = detail.removeprefix("Settlements ")
        elif job.status == "failed":
            terminal_count += 1
            failure_count += 1
            failure_category = str(
                (getattr(job, "metrics", None) or {}).get("failure_category") or ""
            )
            detail = (
                "failed (daily capacity reached)"
                if failure_category == "daily_capacity_reached"
                else "failed"
            )
        else:
            detail = _refresh_stage_label(job, SETTLEMENT_REQUEST_KIND)
        summaries.append(f"{product}: {detail}")
    total = len(jobs)
    message = (
        f"Settlements {terminal_count}/{total} finished · " + " · ".join(summaries)
    )
    if total and failure_count == total:
        suffix = "danger"
    elif failure_count or has_warning:
        suffix = "warning"
    elif terminal_count == total:
        suffix = "success"
    else:
        suffix = "active"
    return message, f"brent-vol-history-refresh-status brent-vol-history-refresh-status-{suffix}"


@callback(
    Output("brent-vol-history-refresh-job", "data"),
    Output("brent-vol-history-refresh-completion", "data"),
    Output("brent-vol-history-refresh-poll", "disabled"),
    Output("brent-vol-history-refresh-button", "disabled"),
    Output("brent-vol-history-settlement-refresh-button", "disabled"),
    Output("brent-vol-history-refresh-status", "children"),
    Output("brent-vol-history-refresh-status", "className"),
    Output("brent-vol-history-refresh-button", "style"),
    Output("brent-vol-history-settlement-refresh-button", "style"),
    Input("brent-vol-history-refresh-button", "n_clicks"),
    Input("brent-vol-history-settlement-refresh-button", "n_clicks"),
    Input("brent-vol-history-refresh-poll", "n_intervals"),
    Input("brent-vol-history-worker-poll", "n_intervals"),
    Input("brent-vol-history-product", "value"),
    State("brent-vol-history-refresh-job", "data"),
    State("brent-vol-history-refresh-completion", "data"),
)
def manage_bloomberg_refresh(
    _refresh_clicks,
    _settlement_refresh_clicks,
    _poll_count,
    _worker_poll_count,
    product,
    active_jobs_value,
    completions_value,
):
    product = market_data._normalize_product(product)
    product_label = market_data._product_spec(product)["label"]
    active_jobs = _product_store(active_jobs_value, "job_id")
    completions = _product_store(completions_value, "result_snapshot_id")
    try:
        triggered = ctx.triggered_id
    except Exception:
        triggered = None
    suppress_unchanged_stores = triggered in {
        "brent-vol-history-refresh-poll",
        "brent-vol-history-worker-poll",
    }

    def response(
        updated_jobs,
        updated_completions,
        poll_disabled,
        intraday_disabled,
        settlement_disabled,
        message,
        status_class,
        intraday_style,
        settlement_style,
    ):
        updated_jobs = dict(updated_jobs)
        updated_completions = dict(updated_completions)
        jobs_output = (
            no_update
            if suppress_unchanged_stores and updated_jobs == active_jobs
            else updated_jobs
        )
        completions_output = (
            no_update
            if suppress_unchanged_stores and updated_completions == completions
            else updated_completions
        )
        return (
            jobs_output,
            completions_output,
            poll_disabled,
            intraday_disabled,
            settlement_disabled,
            message,
            status_class,
            intraday_style,
            settlement_style,
        )

    hidden = {"display": "none"}
    visible = {"display": "inline-flex"}
    if product == "JKM":
        return response(
            active_jobs,
            completions,
            True,
            True,
            True,
            "JKM uses official ICAP and ICE_JKM_MO inputs; Bloomberg refresh is unavailable.",
            "brent-vol-history-refresh-status brent-vol-history-refresh-status-neutral",
            hidden,
            hidden,
        )
    intraday_enabled = intraday_refresh_enabled(product)
    settlement_enabled_by_product = {
        settlement_product: settlement_refresh_enabled(settlement_product)
        for settlement_product in SETTLEMENT_REFRESH_PRODUCTS
    }
    settlement_enabled = all(settlement_enabled_by_product.values())
    intraday_style = visible if intraday_enabled else hidden
    settlement_style = (
        visible if any(settlement_enabled_by_product.values()) else hidden
    )
    idle_disabled = (not intraday_enabled, not settlement_enabled)
    if not intraday_enabled and not any(settlement_enabled_by_product.values()):
        return response(
            active_jobs,
            completions,
            True,
            True,
            True,
            f"Bloomberg {product_label} refresh is disabled by configuration.",
            "brent-vol-history-refresh-status brent-vol-history-refresh-status-neutral",
            hidden,
            hidden,
        )
    try:
        identity = _authorize_refresh()
    except Exception:
        return response(
            active_jobs,
            completions,
            not any(not _job_is_terminal(job) for job in active_jobs.values()),
            True,
            True,
            "",
            "brent-vol-history-refresh-status",
            hidden,
            hidden,
        )

    try:
        if triggered == "brent-vol-history-refresh-button":
            if not intraday_enabled:
                raise PermissionError(
                    "Bloomberg intraday refresh is disabled by configuration."
                )
            if any(not _job_is_terminal(job) for job in active_jobs.values()):
                raise SettlementRefreshBatchError(
                    "A Bloomberg refresh is already in progress. Wait for it "
                    "to finish before starting an intraday refresh."
                )
            readiness = get_worker_readiness(product)
            if not readiness.ready:
                job_needs_polling = any(
                    not _job_is_terminal(job) for job in active_jobs.values()
                )
                return response(
                    active_jobs,
                    completions,
                    not job_needs_polling,
                    True,
                    True,
                    _worker_readiness_message(readiness, product_label),
                    (
                        "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-danger"
                    ),
                    intraday_style,
                    settlement_style,
                )
            job, created = submit_refresh_job(
                identity.subject or "",
                product=product,
                request_kind=INTRADAY_REQUEST_KIND,
            )
            updated_jobs = {product: job.as_dict()}
            message = (
                "Bloomberg refresh queued."
                if created
                else "Joined the Bloomberg refresh already in progress."
            )
            return response(
                updated_jobs,
                completions,
                False,
                True,
                True,
                message,
                "brent-vol-history-refresh-status brent-vol-history-refresh-status-active",
                intraday_style,
                settlement_style,
            )

        if triggered == "brent-vol-history-settlement-refresh-button":
            jobs = submit_settlement_refresh_jobs(
                identity.subject or "",
                products=SETTLEMENT_REFRESH_PRODUCTS,
            )
            updated_jobs = dict(active_jobs)
            created_products = []
            joined_products = []
            for settlement_product, (job, created) in jobs.items():
                updated_jobs[settlement_product] = job.as_dict()
                (created_products if created else joined_products).append(
                    settlement_product
                )
            if not joined_products:
                message = "Settlement refresh queued for BRENT, TFO, ON, and LNE."
            elif not created_products:
                message = (
                    "Joined settlement refreshes already in progress for "
                    "BRENT, TFO, ON, and LNE."
                )
            else:
                message = (
                    f"Settlement refresh queued for {', '.join(created_products)}; "
                    f"joined {', '.join(joined_products)}."
                )
            return response(
                updated_jobs,
                completions,
                False,
                True,
                True,
                message,
                "brent-vol-history-refresh-status brent-vol-history-refresh-status-active",
                intraday_style,
                settlement_style,
            )

        if active_jobs:
            job_ids = {
                active_product: payload.get("job_id")
                for active_product, payload in active_jobs.items()
                if payload.get("job_id")
            }
            refreshed_jobs = get_refresh_jobs(job_ids)
            updated_jobs = dict(active_jobs)
            for active_product, job in refreshed_jobs.items():
                updated_jobs[active_product] = job.as_dict()
            missing_products = set(job_ids) - set(refreshed_jobs)
            for missing_product in missing_products:
                missing_payload = dict(updated_jobs[missing_product])
                missing_payload.update(
                    {
                        "status": "failed",
                        "stage": "failed",
                        "metrics": dict(missing_payload.get("metrics") or {}),
                        "result_status": missing_payload.get("result_status"),
                        "last_error": "The queued refresh job no longer exists.",
                    }
                )
                updated_jobs[missing_product] = missing_payload

            updated_completions = dict(completions)
            selected_job = refreshed_jobs.get(product)
            if selected_job is not None and selected_job.status == "succeeded":
                selected_kind = _job_request_kind(
                    selected_job,
                    active_jobs.get(product),
                )
                completion = _completion_payload(
                    selected_job,
                    product,
                    selected_kind,
                )
                if completions.get(product, {}).get("job_id") != selected_job.job_id:
                    updated_completions[product] = completion

            hydrated_jobs = {}
            for active_product, payload in updated_jobs.items():
                job = refreshed_jobs.get(active_product)
                hydrated_jobs[active_product] = job or SimpleNamespace(**payload)
            has_active = any(
                not _job_is_terminal(job) for job in hydrated_jobs.values()
            )
            readiness_by_product = (
                get_worker_readiness_many(SETTLEMENT_REFRESH_PRODUCTS)
                if not has_active
                or any(job.status == "queued" for job in hydrated_jobs.values())
                else {}
            )
            selected_readiness = readiness_by_product.get(product)
            all_ready = bool(readiness_by_product) and all(
                readiness.ready for readiness in readiness_by_product.values()
            )
            intraday_disabled = has_active or not (
                intraday_enabled
                and selected_readiness is not None
                and selected_readiness.ready
            )
            settlement_disabled = has_active or not (
                settlement_enabled and all_ready
            )
            settlement_jobs = {
                active_product: job
                for active_product, job in hydrated_jobs.items()
                if _job_request_kind(job, active_jobs.get(active_product))
                == SETTLEMENT_REQUEST_KIND
            }
            if len(settlement_jobs) > 1:
                message, status_class = _settlement_batch_status(settlement_jobs)
            else:
                selected = hydrated_jobs.get(product) or next(
                    iter(hydrated_jobs.values())
                )
                request_kind = _job_request_kind(selected, active_jobs.get(product))
                if selected.status == "succeeded":
                    if request_kind == SETTLEMENT_REQUEST_KIND:
                        message = _settlement_success_message(selected)
                    elif getattr(selected, "result_status", None) == "fresh_reuse":
                        message = (
                            "Fresh complete Bloomberg snapshot reused—no new "
                            "Bloomberg request was needed."
                        )
                    elif getattr(selected, "result_status", None) == "noop":
                        message = "Checked Bloomberg—no market-data changes."
                    else:
                        message = "Bloomberg intraday snapshot is ready."
                    is_partial = (
                        str(getattr(selected, "result_status", "") or "").lower()
                        == "partial"
                        or bool(
                            (getattr(selected, "metrics", None) or {}).get(
                                "failed_dates"
                            )
                        )
                    )
                    status_class = (
                        "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-warning"
                        if is_partial
                        else "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-success"
                    )
                elif selected.status == "failed":
                    if (
                        (getattr(selected, "metrics", None) or {}).get(
                            "failure_category"
                        )
                        == "daily_capacity_reached"
                    ):
                        message = (
                            "Bloomberg daily request capacity has been reached. "
                            "The displayed snapshot is unchanged; refresh again after "
                            "Bloomberg resets the entitlement."
                        )
                    else:
                        fallback = (
                            "Bloomberg settlement refresh failed."
                            if request_kind == SETTLEMENT_REQUEST_KIND
                            else "Bloomberg refresh failed."
                        )
                        message = (
                            f"{getattr(selected, 'last_error', None) or fallback} "
                            "Check the worker and retry."
                        )
                    status_class = (
                        "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-danger"
                    )
                elif (
                    selected.status == "queued"
                    and selected_readiness is not None
                    and not selected_readiness.ready
                ):
                    message = _worker_readiness_message(
                        selected_readiness,
                        product_label,
                    )
                    status_class = (
                        "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-danger"
                    )
                elif selected.status == "queued" and _queued_job_wait_exceeded(selected):
                    message = (
                        f"Bloomberg {product_label} worker is online—this request "
                        "is queued behind another refresh. The page will keep "
                        "monitoring it."
                    )
                    status_class = (
                        "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-active"
                    )
                else:
                    message = f"{_refresh_stage_label(selected, request_kind)}…"
                    status_class = (
                        "brent-vol-history-refresh-status "
                        "brent-vol-history-refresh-status-active"
                    )
            return response(
                updated_jobs,
                updated_completions,
                not has_active,
                intraday_disabled,
                settlement_disabled,
                message,
                status_class,
                intraday_style,
                settlement_style,
            )

        readiness_by_product = get_worker_readiness_many(
            SETTLEMENT_REFRESH_PRODUCTS
        )
        readiness = readiness_by_product[product]
        all_ready = all(
            candidate.ready for candidate in readiness_by_product.values()
        )
        if not settlement_enabled and any(settlement_enabled_by_product.values()):
            disabled_products = [
                candidate
                for candidate, enabled in settlement_enabled_by_product.items()
                if not enabled
            ]
            message = (
                "Settlement refresh is disabled for "
                + ", ".join(disabled_products)
                + "."
            )
            status_class = (
                "brent-vol-history-refresh-status "
                "brent-vol-history-refresh-status-warning"
            )
        elif readiness.ready and settlement_enabled and not all_ready:
            unavailable = [
                candidate
                for candidate in readiness_by_product.values()
                if not candidate.ready
            ]
            message = "Settlement refresh unavailable · " + " · ".join(
                _worker_readiness_message(candidate, candidate.product)
                for candidate in unavailable
            )
            status_class = (
                "brent-vol-history-refresh-status "
                "brent-vol-history-refresh-status-danger"
            )
        else:
            message = _worker_readiness_message(readiness, product_label)
            status_class = (
                "brent-vol-history-refresh-status "
                + (
                    "brent-vol-history-refresh-status-success"
                    if readiness.ready
                    else "brent-vol-history-refresh-status-danger"
                )
            )
        return response(
            active_jobs,
            completions,
            True,
            not (intraday_enabled and readiness.ready),
            not (settlement_enabled and all_ready),
            message,
            status_class,
            intraday_style,
            settlement_style,
        )
    except Exception as exc:
        has_active = any(
            not _job_is_terminal(job) for job in active_jobs.values()
        )
        error_message = (
            str(exc)
            if isinstance(exc, SettlementRefreshBatchError)
            else f"{_safe_message(exc)}. Check the Bloomberg worker and retry."
        )
        return response(
            active_jobs,
            completions,
            not has_active,
            has_active or idle_disabled[0],
            has_active or idle_disabled[1],
            error_message,
            "brent-vol-history-refresh-status brent-vol-history-refresh-status-danger",
            intraday_style,
            settlement_style,
        )


@callback(
    Output("brent-vol-history-date", "options"),
    Output("brent-vol-history-date", "value"),
    Input("refresh-options-data", "n_clicks"),
    Input("brent-vol-history-refresh-completion", "data"),
    Input("brent-vol-history-product", "value"),
    State("brent-vol-history-date", "value"),
)
def update_history_dates(
    _refresh_clicks=None,
    completion=None,
    product=market_data.PRODUCT,
    current_value=None,
):
    product = market_data._normalize_product(product)
    try:
        snapshots = (
            market_data.load_available_jkm_cobs()
            if product == "JKM"
            else market_data.load_available_snapshots(product)
        )
    except Exception:
        return [], None
    if snapshots.empty:
        return [], None
    options = []
    for snapshot in snapshots.itertuples(index=False):
        if product == "JKM":
            business_date = pd.Timestamp(snapshot.business_date).date().isoformat()
            options.append(
                {
                    "label": pd.Timestamp(snapshot.business_date).strftime("%d %b %Y") + " · Settlement",
                    "value": f"jkm:{business_date}",
                }
            )
            continue
        observed = pd.to_datetime(snapshot.observed_at, errors="coerce", utc=True)
        if str(snapshot.snapshot_kind).upper() == "INTRADAY":
            local = observed.tz_convert("Asia/Dubai") if not pd.isna(observed) else observed
            date_label = pd.Timestamp(snapshot.business_date).strftime("%d %b")
            label = (
                f"{date_label} · Intraday {local.strftime('%H:%M')} GST"
                if not pd.isna(local)
                else f"{date_label} · Intraday"
            )
        else:
            label = pd.Timestamp(snapshot.business_date).strftime("%d %b %Y") + " · Settlement"
        options.append({"label": label, "value": str(snapshot.snapshot_id)})
    allowed = {option["value"] for option in options}
    product_completion = (
        {}
        if product == "JKM"
        else _product_store(completion, "result_snapshot_id").get(product, {})
    )
    completed_snapshot = product_completion.get("result_snapshot_id")
    if completed_snapshot in allowed:
        value = completed_snapshot
    else:
        value = current_value if current_value in allowed else options[0]["value"]
    return options, value


def _render_jkm_history(
    selected_value,
    *,
    x_axis,
    current_detail_expiry,
    current_legend_options,
    current_legend_value,
):
    token = str(selected_value or "")
    if not token.startswith("jkm:"):
        raise ValueError("JKM requires an exact official COB selection")
    selected_date = pd.Timestamp(token.removeprefix("jkm:")).date().isoformat()
    market, metadata = market_data.load_jkm_official_market(selected_date)
    display_expiries = sorted(
        pd.to_datetime(market["expiry"], errors="coerce")
        .dropna()
        .dt.date.astype(str)
        .unique()
        .tolist()
    )
    try:
        calibrated = market_data.load_latest_calibrated_surface(
            selected_date,
            display_expiries,
            product="JKM",
        )
    except Exception:
        calibrated = market_data._empty_calibrated_surface(
            "query_error", commodity="JKM", selected_cob=selected_date
        )
    cards = history_cards.build_jkm_official_cards(
        market,
        calibrated,
        x_axis=x_axis,
    )
    history_charts._stamp_plot_generation(cards, snapshot_id=selected_value, product="JKM", x_axis=x_axis,
                           publication_id=market_data.calibrated_publication_metadata(calibrated).get("publication_id"))
    expiry_options = [
        {
            "label": pd.Timestamp(value).strftime("%b-%y"),
            "value": pd.Timestamp(value).date().isoformat(),
        }
        for value in sorted(pd.to_datetime(market["expiry"]).unique())
    ]
    allowed = {item["value"] for item in expiry_options}
    detail_value = (
        current_detail_expiry
        if current_detail_expiry in allowed
        else expiry_options[0]["value"]
    )
    legend_contract = history_charts._expiry_legend_contract(cards)
    legend_options = history_charts._expiry_legend_options(
        legend_contract["available_layers"],
        new_volume_layers=legend_contract["new_volume_layers"],
    )
    selected_layers = history_charts._selected_expiry_layers(
        legend_contract["available_layers"],
        current_legend_options,
        current_legend_value,
    )
    history_charts._apply_expiry_layer_selection(cards, legend_contract, selected_layers)
    observed_at = pd.to_datetime(metadata.get("last_update"), errors="coerce", utc=True)
    if pd.isna(observed_at):
        observed_at = pd.Timestamp(selected_date, tz="UTC") + pd.Timedelta(
            hours=23, minutes=59, seconds=59
        )
    return (
        {
            "snapshot_id": None,
            "source_revision": selected_date,
            "source_identity": "ICAP_JKM+ICE_JKM_MO",
            "calibration": market_data.calibrated_publication_metadata(calibrated),
            "calibration_status": calibrated.attrs.get("publication_status"),
            "product": "JKM",
            "business_date": selected_date,
            "observed_at": observed_at.isoformat(),
            "snapshot_kind": "OFFICIAL_COB",
            "display_expiries": display_expiries,
            "intraday_universe_policy_version": (
                market_data.PRODUCT_SPECS["JKM"]["intraday_policy_version"]
            ),
        },
        cards,
        expiry_options,
        detail_value,
        0,
        1,
        0,
        {0: "00:00", 1: "Latest"},
        True,
        True,
        True,
        True,
        True,
        history_charts._expiry_legend_controls(legend_options, selected_layers),
        legend_contract,
    )


@callback(
    Output("brent-vol-history-snapshot", "data"),
    Output("brent-vol-history-plots", "children"),
    Output("brent-vol-history-detail-expiry", "options"),
    Output("brent-vol-history-detail-expiry", "value"),
    Output("brent-vol-history-trade-start", "min"),
    Output("brent-vol-history-trade-start", "max"),
    Output("brent-vol-history-trade-start", "value"),
    Output("brent-vol-history-trade-start", "marks"),
    Output("brent-vol-history-trade-start", "disabled"),
    Output("brent-vol-history-trade-all", "disabled"),
    Output("brent-vol-history-trade-4h", "disabled"),
    Output("brent-vol-history-trade-1h", "disabled"),
    Output("brent-vol-history-trade-15m", "disabled"),
    Output("brent-vol-history-expiry-legend", "children"),
    Output("brent-vol-history-expiry-layer-manifest", "data"),
    Input("brent-vol-history-date", "value"),
    Input("brent-vol-history-x-axis", "value"),
    Input("brent-vol-history-product", "value"),
    Input("vol-trades-publication-revision", "data"),
    Input("vol-trades-source-revision", "data"),
    Input("vol-trades-icap-prior", "value"),
    State("brent-vol-history-detail-expiry", "value"),
    State("brent-vol-history-trade-window-state", "data"),
    State("brent-vol-history-expiry-layers", "options"),
    State("brent-vol-history-expiry-layers", "value"),
)
def render_history(
    selected_snapshot_id,
    x_axis=chart_data.X_AXIS_STRIKE,
    product=market_data.PRODUCT,
    _published_revision=None,
    _source_revision=None,
    allow_prior_icap=None,
    current_detail_expiry=None,
    current_trade_window=None,
    current_legend_options=None,
    current_legend_value=None,
):
    x_axis = chart_data._normalize_x_axis(x_axis)
    product = market_data._normalize_product(product)
    try:
        publication_trigger = ctx.triggered_id == "vol-trades-publication-revision"
    except MissingCallbackContextException:
        publication_trigger = False
    if publication_trigger and (
        (_published_revision or {}).get("commodity") != market_data._product_spec(product)["published_product"]
    ):
        return (no_update,) * 15
    if not selected_snapshot_id:
        cards = history_cards.build_plot_cards(
            pd.DataFrame(), pd.DataFrame(), x_axis=x_axis, product=product
        )
        legend_contract = history_charts._expiry_legend_contract(cards)
        return (
            None,
            cards,
            [],
            None,
            0,
            1,
            0,
            {0: "00:00", 1: "Latest"},
            True,
            True,
            True,
            True,
            True,
            history_charts._expiry_legend_controls([], []),
            legend_contract,
        )
    if product == "JKM":
        try:
            return _render_jkm_history(
                selected_snapshot_id,
                x_axis=x_axis,
                current_detail_expiry=current_detail_expiry,
                current_legend_options=current_legend_options,
                current_legend_value=current_legend_value,
            )
        except Exception:
            legend_contract = history_charts._expiry_legend_contract([])
            return (
                None,
                [
                    dbc.Alert(
                        "The exact-COB official JKM inputs could not be rendered. "
                        "No alternate date or synthetic source was selected.",
                        color="danger",
                    )
                ],
                [],
                None,
                0,
                1,
                0,
                {0: "00:00", 1: "Latest"},
                True,
                True,
                True,
                True,
                True,
                history_charts._expiry_legend_controls([], []),
                legend_contract,
            )
    try:
        snapshots = market_data.load_available_snapshots(product)
        selected = snapshots[snapshots["snapshot_id"].astype(str).eq(selected_snapshot_id)]
        if selected.empty:
            raise ValueError("selected snapshot is no longer available")
        snapshot = selected.iloc[0]
        if publication_trigger and (
            (_published_revision or {}).get("cob_date") != str(pd.Timestamp(snapshot["business_date"]).date())
        ):
            return (no_update,) * 15
        snapshot_id = str(snapshot["snapshot_id"])
        snapshot_kind = market_data._normalize_snapshot_kind(
            snapshot.get("snapshot_kind") or "SETTLEMENT"
        )
        raw_chain = market_data.load_chain_snapshot(
            snapshot_id,
            product=product,
            snapshot_kind=snapshot_kind,
        )
        if raw_chain.empty:
            raise ValueError("complete snapshot has no option-chain rows")
        selected_date = pd.Timestamp(snapshot["business_date"]).date().isoformat()
        chain, universe = market_data.select_history_universe(
            raw_chain, snapshot_kind, product=product
        )
        if chain.empty:
            raise ValueError("snapshot has no rows in the governed display universe")
        display_expiries = universe.get("selected_contract_months") or []
        trade_tape = (
            market_data._filter_contract_months(
                market_data.load_trade_tape(
                    snapshot_id,
                    product=product,
                    snapshot_kind=snapshot_kind,
                ),
                display_expiries,
            )
            if snapshot_kind == "INTRADAY"
            else pd.DataFrame()
        )
        prior_settlement_chain = pd.DataFrame()
        if snapshot_kind == "INTRADAY":
            snapshot_dates = pd.to_datetime(
                snapshots["business_date"], errors="coerce"
            ).dt.normalize()
            prior_candidates = snapshots.loc[
                snapshots["snapshot_kind"].astype(str).str.upper().eq("SETTLEMENT")
                & snapshot_dates.lt(pd.Timestamp(selected_date))
            ].copy()
            if not prior_candidates.empty:
                prior_candidates["_business_date"] = pd.to_datetime(
                    prior_candidates["business_date"], errors="coerce"
                )
                prior_candidates["_observed_at"] = pd.to_datetime(
                    prior_candidates["observed_at"], errors="coerce", utc=True
                )
                prior_snapshot = prior_candidates.sort_values(
                    ["_business_date", "_observed_at"], ascending=False
                ).iloc[0]
                try:
                    prior_settlement_chain = market_data._filter_contract_months(
                        market_data.load_chain_snapshot(
                            str(prior_snapshot["snapshot_id"]),
                            product=product,
                            snapshot_kind="SETTLEMENT",
                        ),
                        display_expiries,
                    )
                except Exception:
                    prior_settlement_chain = pd.DataFrame()
        if product in market_data.EXACT_COB_SURFACE_PRODUCTS or snapshot_kind == "INTRADAY":
            published = pd.DataFrame()
        elif product == "TFO":
            published = market_data.load_icap_settlement_surface(selected_date, allow_prior="allow" in (allow_prior_icap or []))
            if (_source_revision or {}).get("product") == product and (_source_revision or {}).get("cob_date") == selected_date and (_source_revision or {}).get("error"):
                from vol_trades_provenance import prepare_icap_layer
                published = prepare_icap_layer(None, selected_date)
        else:
            published = market_data.load_published_surface(
                selected_date,
                product=product,
                snapshot_kind=snapshot_kind,
            )
        try:
            calibrated = market_data.load_latest_calibrated_surface(
                selected_date,
                display_expiries,
                product=product,
            )
        except Exception:
            calibrated = market_data._empty_calibrated_surface(
                "query_error",
                commodity=market_data._product_spec(product)["published_product"],
                selected_cob=selected_date,
            )
        expiries = sorted(
            pd.to_datetime(chain["underlying_contract_month"], errors="coerce")
            .dropna()
            .dt.normalize()
            .unique()
        )
        expiry_options = [
            {"label": pd.Timestamp(value).strftime("%b-%y"), "value": pd.Timestamp(value).date().isoformat()}
            for value in expiries
        ]
        allowed = {option["value"] for option in expiry_options}
        detail_value = (
            current_detail_expiry
            if current_detail_expiry in allowed
            else expiry_options[0]["value"]
        )
        cards = history_cards.build_plot_cards(
            chain,
            published,
            x_axis=x_axis,
            trade_tape=(trade_tape if snapshot_kind == "INTRADAY" else None),
            prior_settlement_chain=(
                prior_settlement_chain
                if snapshot_kind == "INTRADAY"
                else None
            ),
            product=product,
            calibrated=calibrated,
        )
        publication_id = market_data.calibrated_publication_metadata(calibrated).get("publication_id")
        history_charts._stamp_plot_generation(cards, snapshot_id=snapshot_id, product=product,
                               x_axis=x_axis, publication_id=publication_id,
                               icap_revision=published.attrs.get("icap_metadata"))
        legend_contract = history_charts._expiry_legend_contract(cards)
        legend_options = history_charts._expiry_legend_options(
            legend_contract["available_layers"],
            new_volume_layers=legend_contract["new_volume_layers"],
            product=product,
            surface_label=chart_data._calibrated_surface_label(product, calibrated),
        )
        selected_layers = history_charts._selected_expiry_layers(
            legend_contract["available_layers"],
            current_legend_options,
            current_legend_value,
        )
        history_charts._apply_expiry_layer_selection(cards, legend_contract, selected_layers)
        return (
            {
                "snapshot_id": snapshot_id,
                "product": product,
                "business_date": selected_date,
                "snapshot_kind": snapshot_kind,
                "observed_at": pd.Timestamp(snapshot["observed_at"]).isoformat(),
                "is_latest": bool(
                    str(snapshots.loc[snapshots["snapshot_kind"].eq(snapshot_kind)]
                        .sort_values(["business_date", "observed_at"], ascending=False)
                        .iloc[0]["snapshot_id"]) == snapshot_id
                    and (snapshot_kind == "SETTLEMENT" or selected_date == pd.Timestamp.now(tz=MARKET_TIMEZONE).date().isoformat())
                ),
                "trade_coverage": {
                    "market_date": selected_date,
                    "event_count": len(trade_tape),
                    "first_event_at": (pd.to_datetime(trade_tape["trade_at"], utc=True).min().isoformat() if not trade_tape.empty else None),
                    "last_event_at": (pd.to_datetime(trade_tape["trade_at"], utc=True).max().isoformat() if not trade_tape.empty else None),
                    "cutoff_at": (pd.to_datetime(trade_tape["cutoff_at"], errors="coerce", utc=True).max().isoformat()
                                  if not trade_tape.empty and "cutoff_at" in trade_tape and pd.to_datetime(trade_tape["cutoff_at"], errors="coerce", utc=True).notna().any() else None),
                },
                "display_expiries": display_expiries,
                "intraday_universe_policy_version": universe.get("policy_version"),
                "publication_id": publication_id,
                "calibration": market_data.calibrated_publication_metadata(calibrated),
                "calibration_status": calibrated.attrs.get("publication_status"),
                "icap": published.attrs.get("icap_metadata"),
            },
            cards,
            expiry_options,
            detail_value,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            history_charts._expiry_legend_controls(legend_options, selected_layers),
            legend_contract,
        )
    except Exception:
        legend_contract = history_charts._expiry_legend_contract([])
        return (
            None,
            [dbc.Alert("The selected option-chain snapshot could not be rendered.", color="danger")],
            [],
            None,
            0,
            1,
            0,
            {0: "00:00", 1: "Latest"},
            True,
            True,
            True,
            True,
            True,
            history_charts._expiry_legend_controls([], []),
            legend_contract,
        )


@callback(
    Output("brent-vol-history-expiry-layers", "value", allow_duplicate=True),
    Input("brent-vol-history-expiry-layers-reset", "n_clicks"),
    State("brent-vol-history-expiry-layers", "options"),
    prevent_initial_call=True,
)
def reset_expiry_layers(_n_clicks, options):
    return history_charts._default_expiry_layers(
        [
            option["value"]
            for option in (options or [])
            if option.get("value")
        ]
    )


@callback(
    Output(
        {"type": "brent-vol-history-expiry-graph", "expiry": ALL, "generation": ALL},
        "figure",
        allow_duplicate=True,
    ),
    Input("brent-vol-history-expiry-layers", "value"),
    State("brent-vol-history-expiry-layer-manifest", "data"),
    State(
        {"type": "brent-vol-history-expiry-graph", "expiry": ALL, "generation": ALL},
        "id",
    ),
    prevent_initial_call=True,
)
def update_expiry_layer_visibility(selected_layers, manifest, graph_ids):
    if not graph_ids:
        return []
    manifest = manifest or {}
    selected = set(
        manifest.get("available_layers")
        if selected_layers is None
        else selected_layers
    )
    graph_contracts = manifest.get("graphs") or {}
    updates = []
    for graph_id in graph_ids:
        expiry = str(dict(graph_id or {}).get("expiry") or "")
        graph_contract = graph_contracts.get(expiry) or {}
        if graph_id.get("generation") and graph_id["generation"] != graph_contract.get("generation"):
            updates.append(no_update)
            continue
        patch = Patch()
        for entry in graph_contract.get("traces") or []:
            patch["data"][int(entry["index"])]["visible"] = (
                entry["layer"] in selected
            )
        for entry in graph_contract.get("shapes") or []:
            patch["layout"]["shapes"][int(entry["index"])]["visible"] = (
                entry["layer"] in selected
            )
        updates.append(patch)
    return updates


@callback(
    Output({"type": "brent-vol-history-expiry-graph", "expiry": ALL, "generation": ALL}, "figure", allow_duplicate=True),
    Output("ice-chat-overlay-status", "children"),
    Output("brent-vol-history-ice-overlay-revisions", "data"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("ice-chat-contract", "value"),
    Input("ice-chat-option-type", "value"),
    Input("ice-chat-strike", "value"),
    Input("ice-chat-sender", "value"),
    Input("ice-chat-source-channel", "value"),
    Input("ice-chat-status-filter", "value"),
    Input("ice-chat-positive-only", "value"),
    Input("brent-vol-history-snapshot", "data"),
    Input("brent-vol-history-expiry-layer-manifest", "data"),
    Input("brent-vol-history-trade-window-state", "data"),
    Input("brent-vol-history-expiry-layers", "value"),
    Input({"type": "brent-vol-history-expiry-graph", "expiry": ALL, "generation": ALL}, "id"),
    State("brent-vol-history-x-axis", "value"),
    State({"type": "brent-vol-history-expiry-graph", "expiry": ALL, "generation": ALL}, "relayoutData"),
    State("brent-vol-history-ice-overlay-revisions", "data"),
    prevent_initial_call=True,
)
def render_ice_quote_overlays(
    quote_snapshot, contract, option_type, strike, sender, source_channel,
    status, positive_only, history_snapshot, manifest, window, selected_layers,
    graph_ids, x_axis, relayout_data, previous_revisions,
):
    if not window:
        return [no_update for _ in (graph_ids or [])], "Loading the market window", no_update
    patches, message = overlays.update_ice_quote_overlays(
        quote_snapshot, contract, option_type, strike, sender, source_channel,
        status, positive_only, history_snapshot, manifest, x_axis, selected_layers,
        graph_ids, window, relayout_data,
    )
    previous_revisions, revisions, updates = previous_revisions or {}, {}, []
    for graph_id, patch in zip(graph_ids or [], patches):
        key = json.dumps(graph_id, sort_keys=True)
        if patch is no_update:
            if key in previous_revisions:
                revisions[key] = previous_revisions[key]
            updates.append(no_update)
            continue
        revision = hashlib.sha256(json.dumps(patch.to_plotly_json(), sort_keys=True).encode()).hexdigest()
        updates.append(no_update if previous_revisions.get(key) == revision else patch)
        revisions[key] = revision
    return updates, message, revisions if revisions != previous_revisions else no_update


@callback(
    Output("brent-vol-history-strip-plots", "children"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("brent-vol-history-snapshot", "data"),
    Input("brent-vol-history-trade-window-state", "data"),
    Input("brent-vol-history-x-axis", "value"),
    Input("brent-vol-history-expiry-layers", "value"),
    Input("ice-chat-contract", "value"),
    Input("ice-chat-option-type", "value"),
    Input("ice-chat-strike", "value"),
    Input("ice-chat-sender", "value"),
    Input("ice-chat-source-channel", "value"),
    Input("ice-chat-status-filter", "value"),
    Input("ice-chat-positive-only", "value"),
)
def render_quarter_season_charts(quote_snapshot, history, window, x_axis, layers,
                                contract, option_type, strike, sender, source_channel, status, positive_only):
    return strip_charts.render_strip_charts(
        quote_snapshot, history, window, x_axis, layers,
        contract=contract, option_type=option_type, strike=strike, sender=sender,
        source_channel=source_channel, status=status, positive_only="positive" in (positive_only or []),
    )


clientside_callback(
    """
    function(windowState, start, disabled) {
        const presets = ['all', '4h', '1h', '15m'];
        const value = Number(start);
        const maximum = Number(windowState && windowState.maximum);
        const valid = windowState && Number.isFinite(value)
            && Number.isFinite(maximum) && maximum > 0;
        const matching = valid && !disabled
            && Math.abs(value - Number(windowState.start)) < 0.001;
        const selected = matching ? windowState.preset : null;
        const fraction = valid ? Math.min(1, Math.max(0, value / maximum)) : 0;
        return presets.map(preset => String(preset === selected)).concat([
            {'--brent-market-start': (fraction * 100) + '%'}
        ]);
    }
    """,
    Output("brent-vol-history-trade-all", "aria-pressed"),
    Output("brent-vol-history-trade-4h", "aria-pressed"),
    Output("brent-vol-history-trade-1h", "aria-pressed"),
    Output("brent-vol-history-trade-15m", "aria-pressed"),
    Output("brent-vol-history-trade-slider-track", "style"),
    Input("brent-vol-history-trade-window-state", "data"),
    Input("brent-vol-history-trade-start", "value"),
    Input("brent-vol-history-trade-start", "disabled"),
)


@callback(
    Output("brent-vol-history-trade-window-state", "data"),
    Output("brent-vol-history-trade-start", "min", allow_duplicate=True),
    Output("brent-vol-history-trade-start", "max", allow_duplicate=True),
    Output("brent-vol-history-trade-start", "value", allow_duplicate=True),
    Output("brent-vol-history-trade-start", "marks", allow_duplicate=True),
    Output("brent-vol-history-trade-start", "disabled", allow_duplicate=True),
    Output("brent-vol-history-trade-all", "disabled", allow_duplicate=True),
    Output("brent-vol-history-trade-4h", "disabled", allow_duplicate=True),
    Output("brent-vol-history-trade-1h", "disabled", allow_duplicate=True),
    Output("brent-vol-history-trade-15m", "disabled", allow_duplicate=True),
    Output("brent-vol-history-market-window-status", "children"),
    Input("brent-vol-history-snapshot", "data"),
    Input("ice-chat-quote-snapshot", "data"),
    Input("brent-vol-history-expiry-layer-manifest", "data"),
    Input("brent-vol-history-trade-start", "value"),
    Input("brent-vol-history-trade-all", "n_clicks"),
    Input("brent-vol-history-trade-4h", "n_clicks"),
    Input("brent-vol-history-trade-1h", "n_clicks"),
    Input("brent-vol-history-trade-15m", "n_clicks"),
    State("brent-vol-history-trade-window-state", "data"),
    prevent_initial_call=True,
)
def update_market_window_controls(history, quotes, manifest, start_second, _all, _4h, _1h, _15m, previous):
    quotes, previous = quotes or {}, previous or {}
    if not history:
        return None, 0, 1, 0, {0: "00:00"}, True, True, True, True, True, "No market context"
    key = history_identity(history)
    context = quotes.get("market_context")
    if history.get("product") in quote_data.QUOTE_FEED_PRODUCTS:
        if not context or context.get("history_key") != key:
            return (no_update,) * 10 + ("Loading market observations",)
        if (previous.get("history_key") == key and context.get("mode") == "live"
                and utc_timestamp(context.get("cutoff_at")) < utc_timestamp(previous.get("cutoff_at"))):
            return (no_update,) * 11
        if quotes.get("error") and previous.get("history_key") == key:
            context = {name: previous.get(name) for name in (
                "history_key", "product", "market_date", "day_start_at", "cutoff_at", "mode", "reference_date",
            )}
    else:
        context = market_context(history, now=history.get("observed_at"))
    if not context:
        return None, 0, 1, 0, {0: "00:00"}, True, True, True, True, True, "Market timing unavailable"
    try:
        trigger = ctx.triggered_id
    except MissingCallbackContextException:
        trigger = None
    preset_ids = {f"brent-vol-history-trade-{name}": name for name in PRESET_SECONDS}
    manual = None
    if trigger == "brent-vol-history-trade-start" and previous.get("history_key") == key and start_second is not None:
        if abs(float(start_second) - float(previous.get("start", 0))) > 0.001:
            manual = start_second
    window = select_market_window(context, previous=previous, preset=preset_ids.get(trigger), manual_start=manual)
    day_window = select_market_window(context, preset="all")
    groups, _ = prepare_overlay_events(quotes.get("rows") or [], history.get("product"), day_window, (manifest or {}).get("graphs") or {})
    has_quotes = any(
        market_data._numeric_or_none(row.get(iv)) is not None and market_data._numeric_or_none(row.get(iv)) > 0
        for rows in groups.values() for row in rows for _, iv in overlays.ICE_QUOTE_LAYERS.values()
    ) and not quotes.get("error")
    coverage = history.get("trade_coverage") or {}
    first_trade, last_trade = utc_timestamp(coverage.get("first_event_at")), utc_timestamp(coverage.get("last_event_at"))
    has_trades = pd.notna(first_trade) and pd.notna(last_trade) and last_trade >= utc_timestamp(context["day_start_at"]) and first_trade <= utc_timestamp(context["cutoff_at"])
    disabled = not (has_quotes or has_trades)
    maximum = max(1.0, window["maximum"])
    mark_values = sorted({0, maximum, *(v for v in (21600, 43200, 64800) if maximum - v >= 1800)})
    marks = {value: f"{int(value) // 3600:02d}:{int(value) % 3600 // 60:02d}" for value in mark_values}
    start, cutoff = utc_timestamp(window["start_at"]).tz_convert(MARKET_TIMEZONE), utc_timestamp(window["cutoff_at"]).tz_convert(MARKET_TIMEZONE)
    label = f"{cutoff:%d %b} {start:%H:%M}–{cutoff:%H:%M} GST · {window['mode']}"
    if quotes.get("error"):
        label += " · ICE unavailable"
    elif disabled:
        label += " · no observations"
    changed = window != previous
    slider_value = window["start"] if start_second is None or abs(float(start_second) - window["start"]) > 0.001 else no_update
    return window if changed else no_update, 0, maximum, slider_value, marks, disabled, disabled, disabled, disabled, disabled, label


@callback(
    Output({"type": "brent-vol-history-expiry-graph", "expiry": ALL, "generation": ALL}, "figure"),
    Output("brent-vol-history-trade-grid", "rowData"),
    Input("brent-vol-history-trade-window-state", "data"),
    Input("brent-vol-history-detail-expiry", "value"),
    Input("brent-vol-history-snapshot", "data"),
    State({"type": "brent-vol-history-expiry-graph", "expiry": ALL, "generation": ALL}, "figure"),
    State("brent-vol-history-x-axis", "value"),
    State("brent-vol-history-trade-start", "max"),
)
def update_trade_window(market_window, detail_expiry, snapshot_reference, figures, x_axis, maximum):
    if not figures:
        return [], []
    if not snapshot_reference or snapshot_reference.get("snapshot_kind") != "INTRADAY":
        return (
            [no_update for _ in figures],
            [],
        )
    if not market_window or market_window.get("history_key") != history_identity(snapshot_reference):
        return [no_update for _ in figures], no_update
    try:
        tape = market_data._filter_contract_months(
            market_data.load_trade_tape(
                snapshot_reference["snapshot_id"],
                product=market_data._normalize_product(snapshot_reference.get("product")),
                snapshot_kind=snapshot_reference.get("snapshot_kind"),
            ),
            snapshot_reference.get("display_expiries") or [],
        )
        filtered = filter_event_window(tape, "trade_at", market_window)
        figure_updates = []
        for figure in figures:
            expiry_value = dict(dict(figure.get("layout") or {}).get("meta") or {}).get(
                "expiry"
            )
            if not expiry_value:
                figure_updates.append(no_update)
                continue
            payloads = tape_views.trade_trace_payloads(
                filtered, pd.Timestamp(expiry_value), chart_data._normalize_x_axis(x_axis),
                volume_reference=tape,
            )
            patch = Patch()
            for index, trace in enumerate(figure.get("data") or []):
                meta = trace.get("meta") if isinstance(trace, dict) else None
                if not isinstance(meta, dict) or meta.get("role") != "trade-tape":
                    continue
                payload = payloads[str(meta["put_call"])]
                patch["data"][index]["x"] = payload["x"]
                patch["data"][index]["y"] = payload["y"]
                patch["data"][index]["customdata"] = payload["customdata"]
                patch["data"][index]["marker"]["size"] = payload["size"]
                patch["data"][index]["marker"]["symbol"] = payload["symbol"]
                patch["data"][index]["marker"]["opacity"] = payload["opacity"]
                patch["data"][index]["marker"]["line"]["color"] = payload["line_color"]
                patch["data"][index]["marker"]["line"]["width"] = payload["line_width"]
            figure_updates.append(patch)
        rows = tape_views._trade_tape_rows(filtered, detail_expiry, 0)
        return (
            figure_updates,
            rows,
        )
    except Exception:
        return (
            [no_update for _ in figures],
            [],
        )


@callback(
    Output("brent-vol-history-grid", "rowData"),
    Input("brent-vol-history-snapshot", "data"),
    Input("brent-vol-history-detail-expiry", "value"),
)
def update_detail_grid(snapshot_reference, expiry_value):
    if not snapshot_reference or not expiry_value:
        return []
    try:
        chain = market_data.load_chain_snapshot(
            snapshot_reference["snapshot_id"],
            product=market_data._normalize_product(snapshot_reference.get("product")),
            snapshot_kind=snapshot_reference.get("snapshot_kind"),
        )
        return grids._detail_rows(chain, expiry_value)
    except Exception:
        return []


__all__ = [
    "layout",
    "render_history",
    "update_detail_grid",
    "update_history_dates",
]


@callback(
    Output("vol-trades-source-revision", "data"),
    Input("vol-trades-source-poll", "n_intervals"),
    Input("brent-vol-history-product", "value"),
    State("brent-vol-history-snapshot", "data"),
    State("vol-trades-source-revision", "data"),
)
def poll_market_source_revision(_tick, product, snapshot, previous):
    """Notify only after a complete source revision is available, or on failure."""
    if not snapshot or snapshot.get('product') != product or product != 'TFO':
        return no_update
    from surface_data import refresh_operational_surface_if_changed
    from vol_trades_provenance import source_revision_event
    event = source_revision_event(product, snapshot['business_date'],
                                  refresh_operational_surface_if_changed)
    return no_update if event == previous else event


@callback(
    Output("vol-trades-provenance", "children"),
    Output("vol-trades-icap-prior-control", "style"),
    Input("brent-vol-history-snapshot", "data"),
)
def render_market_provenance(snapshot):
    from vol_trades_provenance import render_provenance
    return render_provenance(snapshot)
