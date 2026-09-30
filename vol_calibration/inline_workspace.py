"""Lazy inline calibration workspace for the existing Vol Trades page."""

from __future__ import annotations

from vol_calibration import auth as calibration_auth

from vol_calibration import ttf_publication as publication_data

import copy
from datetime import datetime, timezone
from io import StringIO
import json
import os
from typing import Any

import dash_bootstrap_components as dbc
import pandas as pd
from dash import Input, Output, State, callback, dcc, html, no_update
from dash.exceptions import PreventUpdate

from options.hh_lne_calibration import (
    HH_LNE_CALIBRATION_ENGINE_VERSION,
    HH_LNE_CALIBRATION_METHOD,
    HH_LNE_CALIBRATION_POLICY_VERSION,
    resolve_hh_lne_snapshot_reference,
)
from runtime_config import get_database_engine
from vol_trades_data import active_brent_publication
from vol_calibration.feature_flags import (
    jkm_publication_enabled,
    ttf_publication_enabled,
)
from vol_calibration.model_version import DEFAULT_CALIBRATION_MODEL_VERSION
from vol_calibration.pages import jkm, ttf
from vol_calibration.pages import hh_governed
from vol_calibration import brent_single_workspace
from options.brent_single_surface import BRENT_SINGLE_SURFACE_POLICY_VERSION
from vol_calibration.ttf_hybrid_surface import (
    TTF_HYBRID_METHOD,
    TTF_HYBRID_POLICY_VERSION,
)
from vol_calibration.jkm_hybrid_surface import (
    JKM_HYBRID_METHOD,
    JKM_HYBRID_POLICY_VERSION,
)
from vol_calibration.ttf_publication import (
    input_manifest_fingerprint,
    load_latest_hybrid_publication,
    publish_hybrid_surface,
    publish_ttf_surface,
)


PUBLICATION_COMMODITIES = {
    "BRENT": "BRENT",
    "TFO": "TTF",
    "ON": "HH",
    "LNE": "HH",
    "JKM": "JKM",
}


def _component_children(component):
    children = getattr(component, "children", None)
    if children is None:
        return []
    return list(children) if isinstance(children, (list, tuple)) else [children]


def _walk_components(component):
    yield component
    for child in _component_children(component):
        if hasattr(child, "to_plotly_json"):
            yield from _walk_components(child)


def _prepare_embedded_layout(product: str, context: dict[str, Any]):
    if product == "BRENT":
        return copy.deepcopy(brent_single_workspace.layout)
    elif product == "TFO":
        workspace = copy.deepcopy(ttf.layout)
        date_id = "ttf-date-picker"
        hidden_actions = set()
    elif product == "JKM":
        workspace = copy.deepcopy(jkm.layout)
        date_id = "jkm-date-picker"
        hidden_actions = set()
    else:
        return hh_governed.create_layout(
            context["cob_date"], context["calibration_source_id"]
        )

    for component in _walk_components(workspace):
        component_id = getattr(component, "id", None)
        if component_id == date_id:
            component.date = context["cob_date"]
            component.disabled = True
        if component_id in hidden_actions:
            component.disabled = True
            component.style = {"display": "none"}
        if getattr(component, "href", None) == "/brent_vol_history":
            component.style = {"display": "none"}
    return workspace


def _as_of(snapshot: dict[str, Any]) -> datetime:
    value = pd.to_datetime(snapshot.get("observed_at"), errors="coerce", utc=True)
    if pd.isna(value):
        cob = pd.Timestamp(snapshot["business_date"]).date()
        return datetime.combine(cob, datetime.max.time(), tzinfo=timezone.utc)
    return value.to_pydatetime()


def resolve_inline_context(engine, snapshot: dict[str, Any], product: str) -> dict[str, Any]:
    """Resolve the immutable market-to-calibration handoff, failing closed."""

    product = str(product or "").strip().upper()
    if product not in PUBLICATION_COMMODITIES:
        raise ValueError(f"Unsupported Vol Trades product {product!r}")
    if not isinstance(snapshot, dict) or snapshot.get("product") != product:
        raise ValueError("The selected market context is not fully rendered yet.")
    cob_date = pd.Timestamp(snapshot.get("business_date")).date().isoformat()
    market_as_of = _as_of(snapshot)
    commodity = PUBLICATION_COMMODITIES[product]
    market_snapshot_id = snapshot.get("snapshot_id")

    if product == "BRENT":
        if not market_snapshot_id:
            raise ValueError("Brent calibration requires the selected snapshot UUID.")
        source_id = str(market_snapshot_id)
        source_identity = "Bloomberg Brent exact pinned snapshot"
        pricing_model = "American futures-style single SVI"
        policy_version = BRENT_SINGLE_SURFACE_POLICY_VERSION
        engine_version = BRENT_SINGLE_SURFACE_POLICY_VERSION
    elif product == "TFO":
        source_id = cob_date
        source_identity = "Exact-COB ICAP TTF smile + ICE_TTF forward"
        pricing_model = TTF_HYBRID_METHOD
        policy_version = TTF_HYBRID_POLICY_VERSION
        engine_version = DEFAULT_CALIBRATION_MODEL_VERSION
    elif product in {"ON", "LNE"}:
        if product == "LNE" and str(snapshot.get("snapshot_kind")) != "SETTLEMENT":
            raise ValueError(
                "HH · LNE calibration requires the selected market snapshot itself "
                "to be a complete LNE SETTLEMENT snapshot."
            )
        reference = resolve_hh_lne_snapshot_reference(
            engine,
            cob_date,
            snapshot_id=(str(market_snapshot_id) if product == "LNE" else None),
        )
        source_id = reference["snapshot_id"]
        source_identity = "Bloomberg CME LNE exact settlement snapshot"
        pricing_model = HH_LNE_CALIBRATION_METHOD
        policy_version = HH_LNE_CALIBRATION_POLICY_VERSION
        engine_version = HH_LNE_CALIBRATION_ENGINE_VERSION
    else:
        if snapshot.get("snapshot_kind") != "OFFICIAL_COB":
            raise ValueError("JKM calibration requires the exact official COB context.")
        source_id = cob_date
        source_identity = "Exact-COB ICAP JKM smile + ICE_JKM_MO forward"
        pricing_model = JKM_HYBRID_METHOD
        policy_version = JKM_HYBRID_POLICY_VERSION
        engine_version = DEFAULT_CALIBRATION_MODEL_VERSION

    publication = (
        active_brent_publication(engine, cob_date)
        if product == "BRENT"
        else load_latest_hybrid_publication(
            engine,
            cob_date,
            commodity=commodity,
            as_of=market_as_of,
            require_exact_cob=(commodity == "HH"),
        )
    )
    return {
        "market_product": product,
        "publication_commodity": commodity,
        "cob_date": cob_date,
        "market_as_of": market_as_of.isoformat(),
        "market_snapshot_id": market_snapshot_id,
        "market_snapshot_kind": snapshot.get("snapshot_kind"),
        "market_source_revision": snapshot.get("source_revision")
        or snapshot.get("snapshot_id"),
        "calibration_source_identity": source_identity,
        "calibration_source_id": source_id,
        "base_publication_id": publication.get("publication_id"),
        "base_publication_published_at": publication.get("published_at"),
        "pricing_model": pricing_model,
        "model_version": DEFAULT_CALIBRATION_MODEL_VERSION,
        "policy_version": policy_version,
        "engine_version": engine_version,
        "code_revision": os.getenv("APP_CODE_REVISION", "unknown"),
    }


def _ledger(context: dict[str, Any]):
    items = (
        ("Market selection", context["market_product"]),
        ("Publication commodity", context["publication_commodity"]),
        ("COB", context["cob_date"]),
        ("Market as-of", context["market_as_of"]),
        ("Market snapshot / revision", context.get("market_snapshot_id") or context.get("market_source_revision")),
        ("Sole calibration source", context["calibration_source_identity"]),
        ("Calibration source ID", context["calibration_source_id"]),
        ("Base publication", context.get("base_publication_id") or "None"),
        ("Pricing / model", context["pricing_model"]),
        ("Policy", context["policy_version"]),
    )
    return html.Dl(
        [
            child
            for label, value in items
            for child in (html.Dt(label), html.Dd(str(value)))
        ],
        className="vol-trades-inline-provenance-list",
    )


def _publication_control(product: str):
    if product in {"ON", "LNE"}:
        return None
    commodity = PUBLICATION_COMMODITIES[product]
    return dbc.Card(
        [
            dbc.CardHeader(html.H3("Controlled publication")),
            dbc.CardBody(
                [
                    dbc.Checklist(
                        id=f"vol-trades-inline-{commodity.lower()}-confirm",
                        options=[
                            {
                                "label": (
                                    f"I confirm the complete validated {commodity} batch "
                                    "will supersede the active revision for this COB."
                                ),
                                "value": "confirmed",
                            }
                        ],
                        value=[],
                        switch=True,
                    ),
                    dbc.Button(
                        f"Validate & Publish {commodity}",
                        id=f"vol-trades-inline-{commodity.lower()}-publish",
                        n_clicks=0,
                        color="danger",
                        disabled=True,
                        className="mt-2",
                    ),
                    html.Div(
                        id=f"vol-trades-inline-{commodity.lower()}-publication-status",
                        className="mt-2",
                    ),
                ]
            ),
        ],
        className="mt-3 vol-trades-inline-publication-card",
    )


def create_inline_workspace(context: dict[str, Any]):
    product = context["market_product"]
    workspace = _prepare_embedded_layout(product, context)
    publication_control = _publication_control(product)
    return html.Section(
        [
            dcc.Store(id="vol-calibration-session-state", storage_type="session"),
            html.Div(
                [
                    html.Div(
                        [
                            html.H2("Surface calibration", className="mb-1"),
                            html.P(
                                "Locked to the selected Vol Trades market context.",
                                className="mb-0 text-muted",
                            ),
                        ]
                    ),
                    dbc.Badge(
                        f"{context['publication_commodity']} · {context['cob_date']}",
                        color="primary",
                        pill=True,
                    ),
                ],
                className="vol-trades-inline-calibration-heading",
            ),
            dbc.Card(
                [
                    dbc.CardHeader(html.H3("Locked input and provenance")),
                    dbc.CardBody(_ledger(context)),
                ],
                className="mb-3",
            ),
            html.Div(workspace, className="vol-trades-inline-product-workspace"),
            publication_control,
        ],
        className="main-section-container vol-trades-inline-calibration",
        **{"aria-label": "Inline volatility surface calibration"},
    )


@callback(
    Output("vol-trades-inline-ttf-publish", "disabled"),
    Input("vol-trades-inline-ttf-confirm", "value"),
)
def enable_inline_ttf_publication(confirmation):
    return not (
        ttf_publication_enabled() and "confirmed" in (confirmation or [])
    )


@callback(
    Output("vol-trades-inline-jkm-publish", "disabled"),
    Input("vol-trades-inline-jkm-confirm", "value"),
)
def enable_inline_jkm_publication(confirmation):
    return not (
        jkm_publication_enabled() and "confirmed" in (confirmation or [])
    )


def _confirmed(values):
    if "confirmed" not in (values or []):
        raise PermissionError("Explicit publication confirmation is required.")


def _manifest_key(commodity: str, cob_date, manifest: dict[str, Any]) -> str:
    return (
        f"inline:{commodity.lower()}:{pd.Timestamp(cob_date).date().isoformat()}:"
        f"{input_manifest_fingerprint(manifest)}"
    )


@callback(
    Output("ttf-published-surface-store", "data", allow_duplicate=True),
    Output("vol-trades-inline-ttf-publication-status", "children"),
    Input("vol-trades-inline-ttf-publish", "n_clicks"),
    State("vol-trades-inline-ttf-confirm", "value"),
    State("ttf-date-picker", "date"),
    State("ttf-trading-context-store", "data"),
    State("ttf-published-surface-store", "data"),
    State("ttf-market-data-store", "data"),
    State("ttf-param-table", "data"),
    State("ttf-final-nodes-store", "data"),
    State("ttf-adjustment-store", "data"),
    State("ttf-intraday-trades-store", "data"),
    State("ttf-batch-results-store", "data"),
    prevent_initial_call=True,
)
def publish_inline_ttf(
    clicks,
    confirmation,
    trading_date,
    trading_context,
    current_publication,
    market_data_json,
    table_data,
    node_store,
    adjustment_store,
    trade_store,
    batch_state,
):
    if not clicks:
        raise PreventUpdate
    try:
        _confirmed(confirmation)
        if not ttf_publication_enabled():
            raise PermissionError("TTF publication is disabled.")
        ready, reason = ttf._batch_state_ready(
            batch_state,
            trading_date,
            market_data_json,
            table_data,
            node_store,
            current_publication,
        )
        if not ready:
            raise ValueError(reason)
        market = pd.read_json(StringIO(market_data_json), orient="split")
        rows_by_expiry = {
            ttf.expiry_month(row.get("expiry")): row for row in table_data
        }
        surfaces = []
        results = []
        for expiry in sorted(market["expiry"].dropna().unique()):
            row = rows_by_expiry.get(ttf.expiry_month(expiry))
            if row is None:
                raise ValueError(f"Missing TTF parameter row for {expiry}.")
            surface, result = ttf._publication_candidate_for_expiry(
                market,
                row,
                expiry,
                node_store,
                adjustment_store,
                current_publication,
                calibration_target=ttf.TTF_BATCH_CALIBRATION_TARGET,
            )
            surfaces.append(surface)
            results.append(result)
        complete = pd.concat(surfaces, ignore_index=True)
        manual_trade_ids = [
            str(item["trade_id"])
            for item in (trade_store or {}).get("trades", [])
            if item.get("trade_id")
        ]
        manifest = {
            "commodity": "TTF",
            "cob_date": pd.Timestamp(trading_date).date().isoformat(),
            "source_snapshots": [{
                "source": "ICAP official smile plus ICE_TTF forward",
                "revision": (trading_context or {}).get("settlement_cob"),
                "observed_at": (trading_context or {}).get("forward_observed_at"),
            }],
            "raw_observations": json.loads(
                market.to_json(orient="records", date_format="iso")
            ),
            "weights_and_parameters": table_data,
            "forward_context": trading_context or {},
            "manual_trade_ids": manual_trade_ids,
            "base_publication_id": (current_publication or {}).get("publication_id"),
            "model_version": DEFAULT_CALIBRATION_MODEL_VERSION,
            "policy_version": TTF_HYBRID_POLICY_VERSION,
            "code_revision": os.getenv("APP_CODE_REVISION", "unknown"),
        }
        identity = calibration_auth.current_request_identity()
        payload = publish_ttf_surface(
            get_database_engine(),
            complete,
            results,
            trading_date=trading_date,
            settlement_cob=(trading_context or {}).get("settlement_cob"),
            identity=identity,
            created_by=ttf._publication_created_by(
                identity,
                adjustment_store,
                ttf._batch_state_results(batch_state),
                rows_by_expiry.keys(),
            ),
            base_publication_id=(current_publication or {}).get("publication_id"),
            expected_current_publication_id=publication_data.same_day_publication_id(
                current_publication, trading_date
            ),
            idempotency_key=_manifest_key("TTF", trading_date, manifest),
            manual_trade_ids=manual_trade_ids,
            expected_expiries=rows_by_expiry.keys(),
            notes="Controlled inline TTF self-publication after complete batch validation.",
            input_manifest=manifest,
        )
        return payload, dbc.Alert(
            f"Published TTF revision {payload['publication_id']} with "
            f"{payload['row_count']} freshly read-back points.",
            color="success",
        )
    except Exception as exc:
        return no_update, dbc.Alert(f"Publication blocked: {exc}", color="danger")


@callback(
    Output("jkm-published-surface-store", "data", allow_duplicate=True),
    Output("vol-trades-inline-jkm-publication-status", "children"),
    Input("vol-trades-inline-jkm-publish", "n_clicks"),
    State("vol-trades-inline-jkm-confirm", "value"),
    State("jkm-date-picker", "date"),
    State("jkm-published-surface-store", "data"),
    State("jkm-market-data-store", "data"),
    State("jkm-param-table", "data"),
    State("jkm-batch-results-store", "data"),
    prevent_initial_call=True,
)
def publish_inline_jkm(
    clicks,
    confirmation,
    trading_date,
    current_publication,
    market_data_json,
    table_data,
    batch_state,
):
    if not clicks:
        raise PreventUpdate
    try:
        _confirmed(confirmation)
        if not jkm_publication_enabled():
            raise PermissionError("JKM publication is disabled.")
        ready, reason = jkm._batch_state_ready(
            batch_state,
            trading_date,
            market_data_json,
            table_data,
            current_publication,
        )
        if not ready:
            raise ValueError(reason)
        market = pd.read_json(StringIO(market_data_json), orient="split")
        rows_by_expiry = {
            jkm.expiry_month(row.get("expiry")): row for row in table_data
        }
        surfaces = []
        results = []
        for expiry in sorted(market["expiry"].dropna().unique()):
            row = rows_by_expiry.get(jkm.expiry_month(expiry))
            if row is None:
                raise ValueError(f"Missing JKM parameter row for {expiry}.")
            surface, result = jkm._publication_candidate_for_expiry(
                market, row, expiry
            )
            surfaces.append(surface)
            results.append(result)
        complete = pd.concat(surfaces, ignore_index=True)
        manifest = {
            "commodity": "JKM",
            "cob_date": pd.Timestamp(trading_date).date().isoformat(),
            "source_snapshots": [{
                "source": "ICAP official smile plus ICE_JKM_MO forward",
                "revision": pd.Timestamp(trading_date).date().isoformat(),
            }],
            "raw_observations": json.loads(
                market.to_json(orient="records", date_format="iso")
            ),
            "weights_and_parameters": table_data,
            "manual_trade_ids": [],
            "base_publication_id": (current_publication or {}).get("publication_id"),
            "model_version": DEFAULT_CALIBRATION_MODEL_VERSION,
            "policy_version": JKM_HYBRID_POLICY_VERSION,
            "code_revision": os.getenv("APP_CODE_REVISION", "unknown"),
        }
        identity = calibration_auth.current_request_identity()
        payload = publish_hybrid_surface(
            get_database_engine(),
            complete,
            results,
            commodity="JKM",
            trading_date=trading_date,
            settlement_cob=trading_date,
            identity=identity,
            created_by=str(identity.subject),
            base_publication_id=(current_publication or {}).get("publication_id"),
            expected_current_publication_id=publication_data.same_day_publication_id(
                current_publication, trading_date
            ),
            idempotency_key=_manifest_key("JKM", trading_date, manifest),
            expected_expiries=rows_by_expiry.keys(),
            notes="Controlled inline JKM self-publication after complete batch validation.",
            input_manifest=manifest,
        )
        return payload, dbc.Alert(
            f"Published JKM revision {payload['publication_id']} with "
            f"{payload['row_count']} freshly read-back points.",
            color="success",
        )
    except Exception as exc:
        return no_update, dbc.Alert(f"Publication blocked: {exc}", color="danger")


__all__ = [
    "create_inline_workspace",
    "resolve_inline_context",
]
