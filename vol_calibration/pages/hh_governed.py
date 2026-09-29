"""Inline governed HH calibration sourced exclusively from Bloomberg LNE settlement."""

from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO
import hashlib
import os
from pathlib import Path
import pickle
import sys

import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import scipy
from dash import Input, Output, State, callback, dash_table, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import has_request_context, request

from snapshot_cache import publish_snapshot, resolve_snapshot
from options import hh_lne_calibration as hh_calibration_module
from options.build_brent_vol_surface import (
    exclude_expired_option_targets,
    horizon_from_as_of,
)
from options.surface_expiry_metadata import resolve_surface_expiry_metadata
from options.hh_lne_calibration import (
    HH_LNE_CALIBRATION_ENGINE_VERSION,
    HH_LNE_CALIBRATION_METHOD,
    HH_LNE_CALIBRATION_POLICY_VERSION,
    build_hh_lne_candidate_surface,
    load_hh_lne_calibration_market,
    resolve_hh_lne_snapshot_reference,
)
from runtime_config import get_database_engine
from vol_calibration.auth import Permission, authorize, resolve_request_identity
from vol_calibration.feature_flags import hh_publication_enabled
from vol_calibration.ttf_publication import (
    input_manifest_fingerprint,
    load_latest_hybrid_publication,
    publish_hybrid_surface,
    ttf_publication_frame,
)


COMMODITY = "HH"
PREFIX = "hh-governed"
_CANDIDATE_CACHE_TTL_SECONDS = 2 * 60 * 60


def get_default_date():
    value = date.today() - timedelta(days=1)
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _identity():
    headers = request.headers if has_request_context() else {}
    remote_addr = request.remote_addr if has_request_context() else None
    return resolve_request_identity(headers, remote_addr=remote_addr)


def _same_day_publication_id(publication, cob_date):
    if not publication or not publication.get("publication_id"):
        return None
    publication_date = pd.to_datetime(
        publication.get("publication_date"), errors="coerce"
    )
    if pd.isna(publication_date):
        return None
    return (
        publication.get("publication_id")
        if publication_date.date() == pd.Timestamp(cob_date).date()
        else None
    )


def _candidate_namespace(identity):
    owner = hashlib.sha256(str(identity.subject).encode("utf-8")).hexdigest()
    return f"hh-governed-candidate:{owner}"


def _candidate_code_fingerprint():
    """Invalidate retained candidates after a relevant code/configuration change."""
    options_root = Path(hh_calibration_module.__file__).resolve().parent
    dash_root = Path(__file__).resolve().parents[2]
    paths = (
        options_root / "hh_lne_calibration.py",
        options_root / "hh_single_surface.py",
        options_root / "brent_single_surface.py",
        options_root / "build_hh_vol_surface_from_settlements.py",
        options_root / "build_brent_vol_surface.py",
        options_root / "calibration_engine" / "models" / "wing_model.py",
        options_root / "calibration_engine" / "converters" / "delta.py",
        options_root / "surface_expiry_metadata.py",
        dash_root / "vol_calibration" / "ttf_hybrid_surface.py",
        Path(__file__),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode("utf-8"))
        digest.update(path.read_bytes())
    digest.update(os.getenv("APP_CODE_REVISION", "unknown").encode("utf-8"))
    for version in (sys.version, np.__version__, pd.__version__, scipy.__version__):
        digest.update(version.encode("utf-8"))
    return digest.hexdigest()


def _candidate_output_fingerprint(candidate):
    payload = (candidate["surface"], candidate["expiry_results"])
    return hashlib.sha256(pickle.dumps(payload, protocol=5)).hexdigest()


def _resolve_candidate(browser_candidate, identity):
    if not isinstance(browser_candidate, dict):
        raise ValueError("HH candidate is missing; recalibrate.")
    stored = resolve_snapshot(
        browser_candidate.get("candidate_ref"),
        expected_namespace=_candidate_namespace(identity),
    )
    if (
        not isinstance(stored, dict)
        or stored.get("input_fingerprint") != browser_candidate.get("input_fingerprint")
        or stored.get("code_fingerprint") != _candidate_code_fingerprint()
        or stored.get("cob_date") != browser_candidate.get("cob_date")
        or stored.get("snapshot_id") != browser_candidate.get("snapshot_id")
        or stored.get("idempotency_key") != browser_candidate.get("idempotency_key")
        or stored.get("output_fingerprint") != browser_candidate.get("output_fingerprint")
        or stored.get("output_fingerprint") != _candidate_output_fingerprint(stored)
    ):
        raise ValueError("HH candidate changed or its code version expired; recalibrate.")
    return stored


def _verify_candidate_source(engine, stored):
    """Recheck the pinned LNE rows and expiries without rerunning the fit."""
    source = resolve_hh_lne_snapshot_reference(
        engine, stored["cob_date"], snapshot_id=stored["snapshot_id"]
    )
    manifest_source = stored["input_manifest"]["source_snapshots"][0]
    expected = {
        "snapshot_id": manifest_source["snapshot_id"],
        "source_revision": manifest_source["revision"],
        "observed_at": manifest_source["observed_at"],
        "option_quote_count": manifest_source["option_quote_count"],
        "forward_count": manifest_source["forward_count"],
        "chain_row_count": manifest_source["raw_chain_count"],
        "iv_resolved_count": manifest_source["resolved_iv_count"],
    }
    if any(source.get(key) != value for key, value in expected.items()):
        raise ValueError("The pinned HH settlement source changed; recalibrate.")
    surface = stored["surface"]
    expiries = surface[["contract_date", "option_expiration_date"]].drop_duplicates()
    requested = pd.DataFrame(
        {"product": "HH", "maturity_date": expiries["contract_date"]}
    )
    current = resolve_surface_expiry_metadata(engine, requested)
    saved_by_month = {
        pd.Timestamp(row.contract_date).date(): pd.Timestamp(
            row.option_expiration_date
        ).date()
        for row in expiries.itertuples(index=False)
    }
    current_by_month = {
        pd.Timestamp(row.maturity_date).date(): pd.Timestamp(
            row.option_expiration_date
        ).date()
        for row in current.itertuples(index=False)
    }
    if current_by_month != saved_by_month:
        raise ValueError("The HH option expiry reference changed; recalibrate.")

    # The snapshot identifier and counts alone cannot detect a changed quote
    # value under the same identifier. Re-read the raw lineage saved in the
    # candidate, while skipping the expensive SVI, ATM, and projection stages.
    api = hh_calibration_module._builder_api()
    snapshot = api.resolve_snapshot(
        engine, stored["cob_date"], stored["snapshot_id"]
    )
    forwards = api.load_forwards(
        engine, snapshot, horizon_from_as_of(snapshot.business_date)
    )
    options = api.load_options(engine, snapshot)
    api.validate_rows(options, forwards, snapshot)
    expiry_metadata = api.resolve_expiries(
        engine,
        pd.DataFrame({"product": "HH", "maturity_date": forwards["strip"]}),
    )
    api.validate_expiries(options, expiry_metadata)
    forwards, _ = exclude_expired_option_targets(
        forwards, expiry_metadata, snapshot.business_date
    )
    manifest = stored["input_manifest"]
    saved_options = manifest.get("raw_observations") or []
    saved_forwards = manifest.get("forwards") or []
    if not saved_options or not saved_forwards:
        raise ValueError("The locked HH input lineage is incomplete; recalibrate.")
    option_columns = [
        key for key in saved_options[0]
        if key not in {"calibration_eligible", "exclusion_reason"}
    ]
    forward_columns = list(saved_forwards[0])
    current_inputs = {
        "options": hh_calibration_module._manifest_records(options, option_columns),
        "forwards": hh_calibration_module._manifest_records(
            forwards, forward_columns
        ),
    }
    locked_inputs = {
        "options": [
            {key: row.get(key) for key in option_columns} for row in saved_options
        ],
        "forwards": saved_forwards,
    }
    if input_manifest_fingerprint(current_inputs) != input_manifest_fingerprint(
        locked_inputs
    ):
        raise ValueError("The pinned HH settlement rows changed; recalibrate.")


def _empty_figure(message: str):
    figure = go.Figure()
    figure.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"color": "#64748b"},
    )
    figure.update_layout(
        template="plotly_white",
        height=360,
        margin={"l": 45, "r": 20, "t": 25, "b": 45},
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return figure


def create_layout(cob_date=None, snapshot_id=None):
    selected = pd.to_datetime(cob_date, errors="coerce")
    selected = get_default_date() if pd.isna(selected) else selected.date()
    return dbc.Container(
        [
            dcc.Store(id=f"{PREFIX}-snapshot-request", data=snapshot_id),
            dcc.Store(id=f"{PREFIX}-market"),
            dcc.Store(id=f"{PREFIX}-provenance"),
            dcc.Store(id=f"{PREFIX}-candidate"),
            dcc.Store(id=f"{PREFIX}-published"),
            html.Div(
                [
                    html.Div(
                        [
                            html.H3("HH governed LNE calibration", className="mb-1"),
                            html.P(
                                "One HH surface for both ON and LNE market views.",
                                className="text-muted mb-0 small",
                            ),
                        ]
                    ),
                    dcc.DatePickerSingle(
                        id=f"{PREFIX}-date",
                        date=selected,
                        display_format="DD-MMM-YYYY",
                        disabled=True,
                    ),
                    dbc.Button(
                        "Reload locked inputs",
                        id=f"{PREFIX}-reload",
                        n_clicks=0,
                        color="secondary",
                        outline=True,
                        size="sm",
                    ),
                    dbc.Button(
                        "Calibrate complete HH surface",
                        id=f"{PREFIX}-calibrate",
                        n_clicks=0,
                        color="primary",
                        size="sm",
                        disabled=True,
                    ),
                    dbc.Button(
                        "Export",
                        id=f"{PREFIX}-export",
                        n_clicks=0,
                        color="info",
                        outline=True,
                        size="sm",
                        disabled=True,
                    ),
                ],
                className="vol-trades-inline-calibration-actions",
            ),
            dbc.Alert(
                [
                    html.Strong("Calibration authority: "),
                    "the exact complete CME LNE settlement snapshot, paired NG forwards, "
                    "captured OPT_FINANCE_RT rates and governed CME LNE expiries. ON, ICE PHE, "
                    "synthetic and alternate-COB rows are excluded.",
                ],
                color="info",
                className="mt-3",
            ),
            html.Div(id=f"{PREFIX}-status", role="status", **{"aria-live": "polite"}),
            dbc.Row(
                [
                    dbc.Col(
                        dbc.Card(
                            [
                                dbc.CardHeader(html.H3("Input and provenance ledger")),
                                dbc.CardBody(html.Div(id=f"{PREFIX}-ledger")),
                            ]
                        ),
                        width=12,
                        lg=5,
                    ),
                    dbc.Col(
                        dbc.Card(
                            [
                                dbc.CardHeader(html.H3("Expiry selection and diagnostics")),
                                dbc.CardBody(
                                    [
                                        dcc.Dropdown(
                                            id=f"{PREFIX}-expiry",
                                            options=[],
                                            value=None,
                                            clearable=False,
                                        ),
                                        dash_table.DataTable(
                                            id=f"{PREFIX}-expiry-table",
                                            columns=[
                                                {"name": "Expiry", "id": "expiry"},
                                                {"name": "Eligible", "id": "eligible"},
                                                {"name": "RMSE", "id": "rmse"},
                                                {"name": "Arbitrage", "id": "arbitrage"},
                                                {"name": "Source class", "id": "source_class"},
                                            ],
                                            data=[],
                                            page_size=10,
                                            style_table={"overflowX": "auto"},
                                            style_cell={"fontSize": 12, "padding": "6px"},
                                        ),
                                    ]
                                ),
                            ]
                        ),
                        width=12,
                        lg=7,
                    ),
                ],
                className="g-3 mt-0",
            ),
            dbc.Card(
                [
                    dbc.CardHeader(html.H3("Candidate versus active HH surface")),
                    dbc.CardBody(
                        dcc.Graph(
                            id=f"{PREFIX}-comparison",
                            figure=_empty_figure("Run complete calibration to build a candidate."),
                            config={"displaylogo": False, "responsive": True},
                        )
                    ),
                ],
                className="mt-3",
            ),
            dbc.Card(
                [
                    dbc.CardHeader(html.H3("Controlled publication")),
                    dbc.CardBody(
                        [
                            dbc.Checklist(
                                id=f"{PREFIX}-publish-confirm",
                                options=[
                                    {
                                        "label": (
                                            "I confirm this complete validated batch will "
                                            "supersede the active HH revision for this COB."
                                        ),
                                        "value": "confirmed",
                                    }
                                ],
                                value=[],
                                switch=True,
                            ),
                            dbc.Button(
                                "Validate & Publish HH",
                                id=f"{PREFIX}-publish",
                                n_clicks=0,
                                color="danger",
                                disabled=True,
                                className="mt-2",
                            ),
                            html.Div(id=f"{PREFIX}-publication-status", className="mt-2"),
                        ]
                    ),
                ],
                className="mt-3",
            ),
            dcc.Download(id=f"{PREFIX}-download"),
        ],
        fluid=True,
        className="vol-trades-hh-governed",
    )


layout = create_layout()


@callback(
    Output(f"{PREFIX}-market", "data"),
    Output(f"{PREFIX}-provenance", "data"),
    Output(f"{PREFIX}-published", "data"),
    Output(f"{PREFIX}-ledger", "children"),
    Output(f"{PREFIX}-expiry", "options"),
    Output(f"{PREFIX}-expiry", "value"),
    Output(f"{PREFIX}-expiry-table", "data"),
    Output(f"{PREFIX}-status", "children"),
    Output(f"{PREFIX}-calibrate", "disabled"),
    Input(f"{PREFIX}-date", "date"),
    Input(f"{PREFIX}-reload", "n_clicks"),
    State(f"{PREFIX}-snapshot-request", "data"),
)
def load_hh_governed_inputs(trade_date, _reload_clicks, requested_snapshot_id):
    if not trade_date or not requested_snapshot_id:
        reason = "A complete exact-COB LNE settlement snapshot must be pinned."
        return None, None, None, [], [], None, [], dbc.Alert(reason, color="danger"), True
    try:
        engine = get_database_engine()
        reference = resolve_hh_lne_snapshot_reference(
            engine, trade_date, snapshot_id=requested_snapshot_id
        )
        market = load_hh_lne_calibration_market(
            engine, trade_date, snapshot_id=requested_snapshot_id
        )
        metadata = market.attrs.get("calibration_metadata") or {}
        manifest = metadata.get("input_manifest") or {}
        published = load_latest_hybrid_publication(
            engine,
            trade_date,
            commodity="HH",
            as_of=pd.to_datetime(reference["observed_at"], utc=True),
            require_exact_cob=True,
        )
        expiry_rows = []
        for expiry, group in market.groupby("expiry", sort=True):
            expiry_rows.append(
                {
                    "expiry": pd.Timestamp(expiry).strftime("%Y-%m"),
                    "eligible": int(group["strike"].nunique()),
                    "rmse": "Pending",
                    "arbitrage": "Pending",
                    "source_class": "Raw LNE settlement",
                }
            )
        options = [
            {"label": row["expiry"], "value": row["expiry"]}
            for row in expiry_rows
        ]
        ledger_items = {
            "Publication commodity": "HH",
            "Sole source": "Bloomberg CME LNE settlement",
            "Pinned snapshot UUID": reference["snapshot_id"],
            "Observed at": reference["observed_at"],
            "Pricing": "Premium-upfront Black-76",
            "Forward": "NG paired to LNE option",
            "Rate": "Captured OPT_FINANCE_RT by contract month",
            "Expiry": "CME LNE Chapter 560",
            "Policy": HH_LNE_CALIBRATION_POLICY_VERSION,
            "Rows": len(market),
        }
        ledger = html.Dl(
            [
                child
                for key, value in ledger_items.items()
                for child in (html.Dt(key), html.Dd(str(value)))
            ],
            className="vol-trades-inline-provenance-list",
        )
        provenance = {
            "reference": reference,
            "manifest": manifest,
            "market_fingerprint": input_manifest_fingerprint(manifest),
        }
        publication_note = (
            f"Active HH publication for {trade_date}: {published.get('publication_id')}"
            if published.get("publication_id")
            else "No HH surface published for this settlement date."
        )
        return (
            market.to_json(date_format="iso", orient="split"),
            provenance,
            published,
            ledger,
            options,
            options[0]["value"],
            expiry_rows,
            dbc.Alert(publication_note, color="secondary"),
            False,
        )
    except Exception as exc:
        reason = f"HH calibration unavailable: {exc}"
        return None, None, None, [], [], None, [], dbc.Alert(reason, color="danger"), True


@callback(
    Output(f"{PREFIX}-candidate", "data"),
    Output(f"{PREFIX}-expiry-table", "data", allow_duplicate=True),
    Output(f"{PREFIX}-status", "children", allow_duplicate=True),
    Output(f"{PREFIX}-export", "disabled"),
    Input(f"{PREFIX}-calibrate", "n_clicks"),
    State(f"{PREFIX}-date", "date"),
    State(f"{PREFIX}-snapshot-request", "data"),
    State(f"{PREFIX}-published", "data"),
    prevent_initial_call=True,
)
def calibrate_hh_governed(_clicks, trade_date, requested_snapshot_id, published):
    if not _clicks:
        raise PreventUpdate
    try:
        candidate = build_hh_lne_candidate_surface(
            get_database_engine(),
            trade_date,
            snapshot_id=requested_snapshot_id,
            code_revision=os.getenv("APP_CODE_REVISION", "unknown"),
            base_publication_id=(published or {}).get("publication_id"),
        )
        candidate["input_fingerprint"] = input_manifest_fingerprint(
            candidate["input_manifest"]
        )
        candidate["code_fingerprint"] = _candidate_code_fingerprint()
        candidate["output_fingerprint"] = _candidate_output_fingerprint(candidate)
        candidate["idempotency_key"] = (
            f"hh:{candidate['cob_date']}:{candidate['input_fingerprint']}"
        )
        rows = []
        for result in candidate["expiry_results"]:
            validation = result.get("validation") or {}
            rmse = result.get("weighted_rmse")
            rows.append(
                {
                    "expiry": pd.Timestamp(
                        result["option_expiration_date"]
                    ).strftime("%Y-%m"),
                    "eligible": (result.get("diagnostics") or {}).get("quote_count"),
                    "rmse": "No quotes" if rmse is None else f"{100 * float(rmse):.3f}%",
                    "arbitrage": "Pass" if validation.get("is_valid") else "Fail",
                    "source_class": (result.get("diagnostics") or {}).get(
                        "term_basis", "unknown"
                    ),
                }
            )
        identity = _identity()
        reference = publish_snapshot(
            _candidate_namespace(identity),
            f"{candidate['input_fingerprint']}:{candidate['code_fingerprint']}",
            candidate,
            metadata={
                "commodity": COMMODITY,
                "cob_date": candidate["cob_date"],
                "snapshot_id": candidate["snapshot_id"],
            },
            force=True,
            ttl_seconds=_CANDIDATE_CACHE_TTL_SECONDS,
        )
        browser_candidate = {
            "candidate_ref": reference,
            "commodity": COMMODITY,
            "cob_date": candidate["cob_date"],
            "snapshot_id": candidate["snapshot_id"],
            "input_fingerprint": candidate["input_fingerprint"],
            "output_fingerprint": candidate["output_fingerprint"],
            "idempotency_key": candidate["idempotency_key"],
            "expiry_count": candidate["expiry_count"],
            "point_count": candidate["point_count"],
        }
        return (
            browser_candidate,
            rows,
            dbc.Alert(
                f"Complete HH batch validated: {candidate['expiry_count']} expiries, "
                f"{candidate['point_count']} points.",
                color="success",
            ),
            False,
        )
    except Exception as exc:
        return None, no_update, dbc.Alert(f"Calibration blocked: {exc}", color="danger"), True


@callback(
    Output(f"{PREFIX}-comparison", "figure"),
    Input(f"{PREFIX}-candidate", "data"),
    Input(f"{PREFIX}-published", "data"),
    Input(f"{PREFIX}-expiry", "value"),
)
def render_hh_candidate_comparison(candidate, published, selected_expiry):
    if not candidate or not selected_expiry:
        return _empty_figure("Run complete calibration to build a candidate.")
    try:
        frame = _resolve_candidate(candidate, _identity())["surface"]
    except Exception:
        return _empty_figure("HH candidate unavailable or expired; recalibrate.")
    month = pd.Period(selected_expiry, freq="M")
    candidate_months = pd.to_datetime(frame["contract_date"]).dt.to_period("M")
    selected = frame.loc[candidate_months.eq(month)].sort_values("delta")
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=selected["delta"],
            y=100 * selected["volatility"],
            mode="lines+markers",
            name="Candidate",
            marker={"symbol": "circle", "color": "#0b5cab"},
        )
    )
    active = ttf_publication_frame(published)
    if not active.empty:
        active_months = pd.to_datetime(active["contract_date"]).dt.to_period("M")
        active = active.loc[active_months.eq(month)].sort_values("delta")
        if not active.empty:
            figure.add_trace(
                go.Scatter(
                    x=active["delta"],
                    y=100 * active["volatility"],
                    mode="lines",
                    name="Active publication",
                    line={"color": "#7c3aed", "dash": "dash"},
                )
            )
    figure.update_layout(
        template="plotly_white",
        height=360,
        margin={"l": 45, "r": 20, "t": 25, "b": 45},
        xaxis_title="Undiscounted call delta",
        yaxis_title="Implied volatility (%)",
        legend={"orientation": "h", "y": 1.12},
    )
    return figure


@callback(
    Output(f"{PREFIX}-publish", "disabled"),
    Input(f"{PREFIX}-candidate", "data"),
    Input(f"{PREFIX}-publish-confirm", "value"),
)
def enable_hh_publish(candidate, confirmation):
    return not (
        hh_publication_enabled()
        and candidate
        and candidate.get("expiry_count")
        and "confirmed" in (confirmation or [])
    )


@callback(
    Output(f"{PREFIX}-published", "data", allow_duplicate=True),
    Output(f"{PREFIX}-publication-status", "children"),
    Output("vol-trades-publication-revision", "data", allow_duplicate=True),
    Input(f"{PREFIX}-publish", "n_clicks"),
    State(f"{PREFIX}-candidate", "data"),
    State(f"{PREFIX}-published", "data"),
    State(f"{PREFIX}-publish-confirm", "value"),
    prevent_initial_call=True,
)
def publish_hh_governed(_clicks, candidate, current, confirmation):
    if not _clicks or not candidate:
        raise PreventUpdate
    try:
        if not hh_publication_enabled():
            raise PermissionError("HH governed publication is disabled.")
        if "confirmed" not in (confirmation or []):
            raise PermissionError("Explicit publication confirmation is required.")
        identity = _identity()
        authorize(identity, Permission.PUBLISH)
        rebuilt = _resolve_candidate(candidate, identity)
        if rebuilt["input_manifest"].get("base_publication_id") != (
            current or {}
        ).get("publication_id"):
            raise ValueError("The HH publication base changed; reload and recalibrate.")
        if input_manifest_fingerprint(rebuilt["input_manifest"]) != candidate.get("input_fingerprint"):
            raise ValueError("The locked HH input manifest changed; recalibrate.")
        engine = get_database_engine()
        _verify_candidate_source(engine, rebuilt)
        payload = publish_hybrid_surface(
            engine,
            rebuilt["surface"],
            rebuilt["expiry_results"],
            commodity="HH",
            trading_date=rebuilt["cob_date"],
            settlement_cob=rebuilt["cob_date"],
            identity=identity,
            created_by=str(identity.subject),
            base_publication_id=(current or {}).get("publication_id"),
            expected_current_publication_id=_same_day_publication_id(
                current, rebuilt["cob_date"]
            ),
            idempotency_key=candidate["idempotency_key"],
            expected_expiries=rebuilt["surface"]["contract_date"].unique(),
            notes="Controlled self-publication from exact Bloomberg LNE settlement.",
            input_manifest=rebuilt["input_manifest"],
        )
        return payload, dbc.Alert(
            f"Published HH revision {payload['publication_id']} with "
            f"{payload['row_count']} freshly read-back points.",
            color="success",
        ), {
            "commodity": "HH",
            "cob_date": rebuilt["cob_date"],
            "publication_id": payload["publication_id"],
        }
    except Exception as exc:
        return no_update, dbc.Alert(f"Publication blocked: {exc}", color="danger"), no_update


@callback(
    Output(f"{PREFIX}-download", "data"),
    Input(f"{PREFIX}-export", "n_clicks"),
    State(f"{PREFIX}-candidate", "data"),
    State(f"{PREFIX}-published", "data"),
    State(f"{PREFIX}-date", "date"),
    prevent_initial_call=True,
)
def export_hh_governed(_clicks, candidate, published, trade_date):
    if not _clicks or not candidate:
        raise PreventUpdate
    output = BytesIO()
    candidate = _resolve_candidate(candidate, _identity())
    surface = candidate["surface"]
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        surface.to_excel(writer, sheet_name="Candidate Surface", index=False)
        pd.json_normalize(candidate["expiry_results"], sep=".").to_excel(
            writer, sheet_name="Diagnostics", index=False
        )
        pd.DataFrame([{
            "commodity": "HH",
            "cob_date": trade_date,
            "snapshot_id": candidate["snapshot_id"],
            "input_fingerprint": candidate["input_fingerprint"],
            "model": HH_LNE_CALIBRATION_METHOD,
            "policy": HH_LNE_CALIBRATION_POLICY_VERSION,
            "engine": HH_LNE_CALIBRATION_ENGINE_VERSION,
            "active_publication_id": (published or {}).get("publication_id"),
        }]).to_excel(writer, sheet_name="Provenance", index=False)
        pd.DataFrame(
            candidate["input_manifest"].get("raw_observations") or []
        ).to_excel(writer, sheet_name="Raw Inputs", index=False)
    return dcc.send_bytes(
        output.getvalue(),
        f"HH_LNE_calibration_{pd.Timestamp(trade_date):%Y%m%d}.xlsx",
    )


__all__ = ["create_layout", "layout"]
