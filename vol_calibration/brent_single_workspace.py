"""Trader review and controlled publication of one fitted Brent surface."""

from __future__ import annotations

from datetime import datetime, timezone

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import has_request_context, request

from vol_calibration.auth import resolve_request_identity
from vol_calibration.brent_single_candidate import (
    active_brent_publication,
    build_brent_candidate,
)
from vol_calibration.feature_flags import brent_publication_enabled
from vol_calibration.ttf_publication import input_manifest_fingerprint, publish_hybrid_surface
from runtime_config import get_database_engine


layout = html.Div(
    [
        html.P(
            "Fit the selected immutable snapshot at its observed strikes. "
            "The 11 shared deltas are sampled afterward and do not steer calibration.",
            className="text-muted",
        ),
        dbc.Button("Calibrate selected snapshot", id="brent-single-calibrate", color="primary"),
        html.Div(id="brent-single-calibration-status", className="mt-2", **{"aria-live": "polite"}),
        dcc.Store(id="brent-single-candidate", storage_type="memory"),
        html.Label("Preview contract", htmlFor="brent-single-expiry", className="mt-3"),
        dcc.Dropdown(id="brent-single-expiry", options=[], value=None, clearable=False),
        dcc.Graph(id="brent-single-preview", config={"displaylogo": False}),
        html.Div(id="brent-single-diagnostics", className="mb-2"),
    ],
    className="brent-single-workspace",
)


def _active_publication(engine, trading_date):
    return active_brent_publication(engine, trading_date)


@callback(
    Output("brent-single-candidate", "data"),
    Output("brent-single-expiry", "options"),
    Output("brent-single-expiry", "value"),
    Output("brent-single-calibration-status", "children"),
    Output("brent-single-diagnostics", "children"),
    Input("brent-single-calibrate", "n_clicks"),
    State("brent-vol-history-calibration-context", "data"),
    prevent_initial_call=True,
)
def calibrate_selected_brent(clicks, context):
    if not clicks:
        raise PreventUpdate
    try:
        engine = get_database_engine()
        candidate = build_brent_candidate(engine, context or {})
        active = _active_publication(engine, context["cob_date"])
        expiries = sorted({row["contract_date"] for row in candidate.preview})
        options = [
            {"label": pd.Timestamp(expiry).strftime("%b %Y"), "value": expiry}
            for expiry in expiries
        ]
        diagnostics = candidate.diagnostics
        selected = {
            "snapshot_id": context["market_snapshot_id"],
            "snapshot_kind": context["market_snapshot_kind"],
            "market_as_of": context["market_as_of"],
            "cob_date": context["cob_date"],
            "fingerprint": candidate.fingerprint,
            "base_publication_id": active.get("publication_id"),
            "preview": candidate.preview,
        }
        summary = (
            f"{diagnostics['observed_expiries']} fitted expiries; "
            f"{diagnostics['target_expiries']} target expiries; "
            f"minimum implied density "
            f"{diagnostics['validation']['minimum_implied_density']:.4f}. "
            f"Selected {diagnostics['source_kind'].lower()} snapshot."
        )
        if diagnostics["settlement"]["activity_status"].lower() != "complete":
            summary += (
                " Settlement open interest is pending, so the fit used "
                "equal quote weights."
            )
        if diagnostics["source_kind"] == "INTRADAY":
            summary += (
                f" {diagnostics['intraday']['updated_expiries']} expiries "
                "updated by executable quotes; other expiries carry the settlement model."
            )
        return selected, options, (expiries[0] if expiries else None), dbc.Alert(
            "Candidate calibrated and validated. Review the fit before publication.",
            color="success",
        ), html.P(summary)
    except Exception as exc:
        return None, [], None, dbc.Alert(f"Calibration blocked: {exc}", color="danger"), None


@callback(
    Output("brent-single-preview", "figure"),
    Input("brent-single-expiry", "value"),
    Input("brent-single-candidate", "data"),
)
def render_brent_preview(expiry, candidate):
    figure = go.Figure()
    rows = [
        row for row in (candidate or {}).get("preview", [])
        if row["contract_date"] == expiry
    ]
    if rows:
        frame = pd.DataFrame(rows).sort_values("strike")
        figure.add_trace(go.Scatter(
            x=frame["strike"], y=100 * frame["volatility"],
            mode="lines+markers", name="Single fitted SVI surface",
            line={"color": "#E69525", "width": 2.5},
            marker={"size": 4},
        ))
    figure.update_layout(
        title="Continuous surface preview (sampled after fitting)",
        xaxis_title="Strike (USD/bbl)", yaxis_title="Implied volatility (%)",
        template="plotly_white", margin={"l": 55, "r": 20, "t": 55, "b": 45},
        height=360,
    )
    return figure


@callback(
    Output("vol-trades-inline-brent-publish", "disabled"),
    Input("vol-trades-inline-brent-confirm", "value"),
    Input("brent-single-candidate", "data"),
    State("brent-vol-history-calibration-context", "data"),
)
def enable_brent_publication(confirmation, candidate, context):
    return not (
        brent_publication_enabled()
        and "confirmed" in (confirmation or [])
        and candidate
        and context
        and candidate.get("snapshot_id") == context.get("market_snapshot_id")
        and candidate.get("market_as_of") == context.get("market_as_of")
    )


@callback(
    Output("vol-trades-inline-brent-publication-status", "children"),
    Output("brent-single-publication-revision", "data"),
    Input("vol-trades-inline-brent-publish", "n_clicks"),
    State("vol-trades-inline-brent-confirm", "value"),
    State("brent-single-candidate", "data"),
    State("brent-vol-history-calibration-context", "data"),
    prevent_initial_call=True,
)
def publish_brent_candidate(clicks, confirmation, preview, context):
    if not clicks:
        raise PreventUpdate
    try:
        if "confirmed" not in (confirmation or []):
            raise PermissionError("Explicit publication confirmation is required.")
        if not brent_publication_enabled():
            raise PermissionError("Brent publication is disabled.")
        if not preview or not context:
            raise ValueError("Calibrate the selected Brent snapshot first.")
        for key in ("snapshot_id", "snapshot_kind", "market_as_of", "cob_date"):
            context_key = "market_" + key if key in {"snapshot_id", "snapshot_kind"} else key
            if preview.get(key) != context.get(context_key):
                raise ValueError("Market selection changed; recalibrate before publishing.")
        engine = get_database_engine()
        candidate = build_brent_candidate(engine, context)
        if preview["fingerprint"] != candidate.fingerprint:
            raise ValueError("Candidate changed since review; recalibrate before publishing.")
        active = _active_publication(engine, context["cob_date"])
        if active.get("publication_id") != preview.get("base_publication_id"):
            raise ValueError("Active Brent publication changed; recalibrate before publishing.")
        manifest = {**candidate.manifest, "base_publication_id": active.get("publication_id")}
        identity = resolve_request_identity(
            request.headers if has_request_context() else {},
            remote_addr=request.remote_addr if has_request_context() else None,
        )
        payload = publish_hybrid_surface(
            engine, candidate.surface, candidate.results,
            commodity="BRENT", trading_date=context["cob_date"],
            settlement_cob=candidate.settlement_cob,
            identity=identity, created_by=str(identity.subject),
            base_publication_id=active.get("publication_id"),
            expected_current_publication_id=active.get("publication_id"),
            idempotency_key=(
                f"inline:brent:{context['cob_date']}:"
                f"{input_manifest_fingerprint(manifest)}"
            ),
            expected_expiries=candidate.surface["contract_date"].unique(),
            notes="Single-SVI Brent surface fitted at observed option strikes.",
            input_manifest=manifest,
            return_surface=False,
        )
        publication_id = payload["publication_id"]
        return dbc.Alert(
            f"Published Brent revision {publication_id} with "
            f"{payload['row_count']} persisted points.", color="success"
        ), {"publication_id": publication_id, "at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        return dbc.Alert(f"Publication blocked: {exc}", color="danger"), no_update
