"""Shared authorization and preview flow for product-specific intraday workspaces."""

import pandas as pd
from dash import no_update
from dash_options.calibration.auth import current_request_identity, authorize, Permission


def identity(permission=Permission.VIEW):
    actor = current_request_identity()
    authorize(actor, permission)
    if not actor.subject:
        raise ValueError("An identified trader is required.")
    return actor


def submit(request, *, jobs, identity, get_database_engine, calibration_enabled, log):
    if not request or not calibration_enabled():
        return None
    try:
        actor = identity()
        return jobs.submit(get_database_engine(required=True), request, actor.subject)
    except Exception:
        log.exception("Intraday preview submission failed")
        return {"request_key": request["key"], "error": "Intraday preview unavailable. Check source access."}


def poll(ticket, request, *, jobs, identity, log):
    if not request or not ticket or ticket["request_key"] != request["key"]:
        return no_update
    if ticket.get("error"):
        return ticket
    try:
        actor = identity()
        state = jobs.poll(ticket, actor.subject)
        return {"request_key": request["key"], **{k: state[k] for k in ("status", "ref", "error") if k in state}}
    except Exception:
        log.exception("Intraday preview polling failed")
        return {"request_key": request["key"], "error": "Preview expired. Reload the market snapshot."}


def candidate(view, request, *, jobs, identity):
    if not request or not view or view.get("request_key") != request["key"] or not view.get("ref"):
        raise ValueError("Wait for the current snapshot's proposal.")
    actor = identity()
    value = jobs.resolve(view["ref"], actor.subject)
    manifest = value["manifest"]
    if (
        manifest["snapshot_id"] != request["snapshot_id"]
        or pd.Timestamp(manifest["cutoff"]) != pd.Timestamp(request["end"])
        or pd.Timestamp(manifest["window_start"]) != pd.Timestamp(request["start"])
    ):
        raise ValueError("Candidate belongs to a different market window.")
    return actor, value


def review_candidate(view, request, draft, *, jobs, identity):
    if draft and request and draft.get("request_key") == request["key"]:
        actor = identity()
        return actor, jobs.resolve(draft["ref"], actor.subject)
    return candidate(view, request, jobs=jobs, identity=identity)
