"""Read-only selection of Vol Trades publications and owner-scoped saved drafts."""

from __future__ import annotations

import hashlib
import json
import logging

import pandas as pd
from dash import Input, Output, State, callback, ctx, dcc, html

from options.vol_calibration import api
from dash_options.calibration.auth import current_request_identity, authorize, Permission
from dash_options.core.config import get_database_engine
from dash_options.core.snapshots import publish_snapshot, resolve_snapshot, SnapshotReferenceError
from dash_options.core.workspace_cache import WorkspaceLoadCache
from dash_options.shared import surfaces

LOG = logging.getLogger(__name__)
NAMESPACE = "vol-surface-selection-v1"
CATALOG = WorkspaceLoadCache(max_entries=24)


class SelectionError(ValueError):
    """An explicit selection cannot be resolved; never fall back to settlement."""


@callback(Output("surface-heatmap-mode-dropdown", "options"), Input("surface-source-dropdown", "value"))
def heatmap_modes(source):
    return [
        {"label": "Absolute IV", "value": "absolute"},
        {"label": "Smile vs ATM", "value": "vs_atm"},
        {"label": "Change vs Previous" if source == "settlement" else "Change vs comparison", "value": "vs_previous"},
    ]


def layout():
    return html.Div(
        [
            dcc.Store(id="surface-selection-ref", storage_type="memory"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Label("Source", htmlFor="surface-source-dropdown"),
                            dcc.Dropdown(
                                id="surface-source-dropdown",
                                options=[
                                    {"label": "Settlement", "value": "settlement"},
                                    {"label": "Published intraday", "value": "intraday"},
                                    {"label": "My saved draft", "value": "draft"},
                                ],
                                value="settlement",
                                clearable=False,
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.Label("Revision", htmlFor="surface-revision-dropdown"),
                            dcc.Dropdown(
                                id="surface-revision-dropdown",
                                options=[],
                                value=None,
                                clearable=False,
                                placeholder="Select revision…",
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.Label("Compare", htmlFor="surface-comparison-dropdown"),
                            html.Div(dcc.DatePickerSingle(id='table-prev-date-picker', display_format='YYYY-MM-DD',
                                persistence=True, persistence_type='session', with_portal=True), id='surface-settlement-comparison'),
                            dcc.Dropdown(
                                id="surface-comparison-dropdown",
                                options=[],
                                value=None,
                                clearable=False,
                                placeholder="Settlement anchor",
                            ),
                        ]
                    ),
                ],
                className="volatility-surface-source-fields",
            ),
            html.Div(id="surface-selector-status", role="status", **{"aria-live": "polite"}),
        ],
        className="volatility-surface-source-selector",
    )


def _owner(source):
    if source != "draft":
        return None
    try:
        actor = current_request_identity()
        authorize(actor, Permission.VIEW)
        return actor.subject
    except Exception as exc:
        raise SelectionError("Sign in with the Vol Trades identity to view your saved drafts.") from exc


def _enabled(product, source):
    if source not in {"intraday", "draft"}:
        raise SelectionError("Select a supported surface source.")
    if not api.intraday_surface_reads_enabled(product):
        raise SelectionError("Intraday surfaces and drafts are unavailable for this product or disabled.")


def _key(product, source, revision, comparison):
    return {"product": product, "source": source, "revision": revision, "comparison": comparison}


def revision_label(row):
    stamp = pd.Timestamp(row["timestamp"])
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp
    return f"{stamp.tz_convert('Asia/Dubai'):%Y-%m-%d %H:%M} GST · {row['revision_id'][:8]}"


def _catalog_rows(engine, product, source, owner):
    rows = api.list_intraday_surface_revisions(engine, commodity=product, source=source, actor=owner)
    # Legacy draft catalogs omit business date; resolve exact authorized headers once per cache fill.
    return [row if row.get('business_date') else {**row, 'business_date':
                api.load_intraday_surface_revision(engine, row['revision_id'], commodity=product,
                    source=source, actor=owner)['business_date']} for row in rows]


def _catalog(engine, product, source, owner, *, refresh=False):
    return CATALOG.get_or_load(
        (product, source, owner or ""),
        lambda: _catalog_rows(engine, product, source, owner),
        force_refresh=refresh,
        degraded=lambda rows: not rows,
        healthy_ttl_seconds=30,
        degraded_ttl_seconds=5,
    )


def _normalize(nodes, product, date):
    # Historical draft payloads can retain expiry/iv aliases alongside these
    # validated API fields. Project the canonical nodes before generic renaming.
    frame = nodes.loc[:, ["contract_date", "option_expiration_date", "put_call", "delta", "volatility"]].copy()
    frame["commodity"], frame["cob_date"] = product, date
    return surfaces._normalize_surface_data(frame, volatility_in_percent=False)


def _build_view(engine, product, source, revision, comparison, owner):
    selected = api.load_intraday_surface_revision(
        engine,
        revision,
        commodity=product,
        source=source,
        actor=owner,
    )
    if comparison == "anchor":
        previous = _normalize(selected["base_surface"], product, selected["settlement_cob"])
        comparison_label = f"Settlement {selected['settlement_cob']} · {selected['base_publication_id'][:8]}"
        comparison_id = selected["base_publication_id"]
    else:
        compared = api.load_intraday_surface_revision(
            engine,
            comparison,
            commodity=product,
            source="intraday",
        )
        previous = _normalize(compared["surface"], product, compared["business_date"])
        comparison_label = f"Intraday {revision_label(compared)}"
        comparison_id = compared["revision_id"]
    current = _normalize(selected["surface"], product, selected["business_date"])
    changed = {pd.Timestamp(m).strftime("%Y-%m") for m in selected["changed_months"]}
    months = current.contract_date.dt.strftime("%Y-%m").unique()
    origins = {
        m: ("Draft fit" if source == "draft" else "Intraday fit")
        if m in changed
        else f"Inherited {selected['settlement_cob']}"
        for m in months
    }
    metadata = {
        key: selected[key]
        for key in (
            "business_date",
            "timestamp",
            "cutoff",
            "base_publication_id",
            "settlement_cob",
            "changed_months",
        )
    }
    kind = "Private draft" if source == "draft" else "Published intraday"
    if selected["business_date"] != pd.Timestamp.now(tz="Asia/Dubai").date().isoformat():
        kind += " replay"
    metadata.update(
        revision_id=selected["revision_id"],
        current_label=f"{kind} {revision_label(selected)}",
        comparison_label=comparison_label,
        comparison_id=comparison_id,
        month_origins=origins,
    )
    return {
        "key": _key(product, source, revision, comparison),
        "owner": owner,
        "current": current,
        "previous": previous,
        "metadata": metadata,
    }


def resolve_view(selection, product, source, revision=None, comparison=None):
    """Authorize every consumer, including cached chart and export requests."""
    product = str(product or "").upper()
    if isinstance(selection, dict) and 'products' in selection:
        if selection.get('revision') != revision or selection.get('comparison') != comparison or selection.get('source') != source:
            raise SelectionError("Loading page selection…")
        child = selection['products'].get(product)
        view = resolve_view(child, product, source, (child or {}).get('revision'), (child or {}).get('comparison'))
        view = {**view, 'metadata': {**view['metadata'], 'revision_id': child['revision']}}
        if str(comparison).startswith('settlement:'):
            date = comparison.split(':', 1)[1]
            try:
                payload = resolve_snapshot(selection.get('settlement_reference'),
                                           expected_namespace=surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE)
                frame = payload['surface_dataset']
                previous = frame.loc[frame.code.str.upper().eq(product) &
                                     frame.cob_date.dt.normalize().eq(pd.Timestamp(date).normalize())].copy()
            except (SnapshotReferenceError, KeyError, ValueError, AttributeError) as exc:
                raise SelectionError('Selected settlement comparison is unavailable; refresh the selection.') from exc
            if previous.empty:
                raise SelectionError(f'No settlement surface for {product} on {date}.')
            view = {**view, 'previous': previous, 'metadata': {**view['metadata'],
                    'comparison_label': f'Settlement {date}', 'comparison_id': comparison,
                    'comparison_snapshot_reference': selection['settlement_reference']}}
        return view
    _enabled(product, source)
    owner = _owner(source)
    if not isinstance(selection, dict) or selection.get("error"):
        raise SelectionError("Selected surface unavailable. Refresh the selection or use Settlement.")
    expected = _key(product, source, revision or selection.get("revision"), comparison or selection.get("comparison"))
    if any(selection.get(k) != v for k, v in expected.items()):
        raise SelectionError("Loading the selected surface revision…")
    try:
        view = resolve_snapshot(selection.get("ref"), expected_namespace=NAMESPACE)
    except SnapshotReferenceError as exc:
        raise SelectionError(
            "Selected revision expired from the display cache. Refresh Options Data to reload it."
        ) from exc
    if view.get("owner") != owner or view.get("key") != expected:
        raise SelectionError("Selected surface does not belong to this product, source, or trader.")
    return view


def select_revision(product, source, revision, comparison, previous, *, refresh=False, cob=None):
    """Return coherent dropdowns and a small, immutable server reference."""
    product = str(product or "").upper()
    _enabled(product, source)
    owner = _owner(source)
    engine = get_database_engine(required=True)
    rows = _catalog(engine, product, source, owner, refresh=refresh)
    if cob:
        rows = [r for r in rows if str(r['business_date'])[:10] == str(cob)[:10]]
    same_context = previous and previous.get("product") == product and previous.get("source") == source
    valid_ids = {r["revision_id"] for r in rows}
    if revision and revision not in valid_ids and same_context and revision == previous.get("revision"):
        # A pinned revision can age out of the bounded list; resolve it explicitly.
        pinned = api.load_intraday_surface_revision(engine, revision, commodity=product, source=source, actor=owner)
        rows = [pinned, *rows]
        valid_ids.add(revision)
    if not rows:
        raise SelectionError(
            "No published intraday revisions available."
            if source == "intraday"
            else "You have no saved intraday drafts for this product."
        )
    revision = revision if revision in valid_ids else rows[0]["revision_id"]
    comparison_rows = rows if source == "intraday" else _catalog(engine, product, "intraday", None, refresh=refresh)
    same_revision = same_context and previous.get("revision") == revision
    if (
        same_revision
        and comparison
        and comparison != "anchor"
        and comparison == previous.get("comparison")
        and comparison not in {row["revision_id"] for row in comparison_rows}
    ):
        pinned_comparison = api.load_intraday_surface_revision(
            engine,
            comparison,
            commodity=product,
            source="intraday",
        )
        comparison_rows = [pinned_comparison, *comparison_rows]
    comparison_options = [{"label": "Settlement anchor", "value": "anchor"}] + [
        {"label": "Intraday " + revision_label(row), "value": row["revision_id"]}
        for row in comparison_rows
        if row["revision_id"] != revision
    ]
    if not same_revision or comparison not in {r["value"] for r in comparison_options}:
        comparison = "anchor"
    key = _key(product, source, revision, comparison)
    try:
        view = resolve_view(previous, product, source, revision, comparison)
        reference = previous["ref"]
    except SelectionError:
        view = _build_view(engine, product, source, revision, comparison, owner)
        digest = hashlib.sha256(json.dumps({**key, "owner": owner}, sort_keys=True).encode()).hexdigest()
        reference = publish_snapshot(NAMESPACE, digest, view, group=NAMESPACE)
    meta = view["metadata"]
    comparison_options[0]["label"] = f"Settlement {meta['settlement_cob']} · anchor {meta['base_publication_id'][:8]}"
    count = len(meta["changed_months"])
    total = view["current"].contract_date.nunique()
    note = f"{count}/{total} months updated · {total - count} inherited from {meta['settlement_cob']}. "
    note += (
        "Private preview; publishing stays in Vol Trades."
        if source == "draft"
        else "Exact revision pinned; historical selections are replay."
    )
    return (
        [{"label": revision_label(row), "value": row["revision_id"]} for row in rows],
        revision,
        comparison_options,
        comparison,
        {**key, "ref": reference},
        note,
    )


@callback(
    Output("surface-revision-dropdown", "options"),
    Output("surface-revision-dropdown", "value"),
    Output("surface-comparison-dropdown", "options"),
    Output("surface-comparison-dropdown", "value"),
    Output("surface-selection-ref", "data"),
    Output("surface-selector-status", "children"),
    Input("table-product-dropdown", "value"),
    Input("surface-source-dropdown", "value"),
    Input("surface-revision-dropdown", "value"),
    Input("surface-comparison-dropdown", "value"),
    Input("table-date-picker", "date"),
    Input("table-prev-date-picker", "date"),
    Input("refresh-options-data", "n_clicks"),
    State("surface-selection-ref", "data"),
    Input("vol-surface-snapshot-ref", "data"),
)
def update_selector(products, source, revision, comparison, date, previous_date, _refresh, previous, snapshot_reference=None):
    if source == 'settlement':
        return ([{'label': f'{date or "Select COB"} · settlement', 'value':'settlement'}],
                'settlement', [{'label':f'{previous_date or "Select date"}', 'value':'previous'}],
                'previous', None, '')
    products = products if isinstance(products, list) else [products] if products else []
    selections, options, comparisons, errors = {}, [{'label':'Latest on COB', 'value':'latest'}], [{'label':'Settlement anchor', 'value':'anchor'}], []
    if snapshot_reference:
        try:
            frame = resolve_snapshot(snapshot_reference, expected_namespace=surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE)['surface_dataset']
            dates = frame.loc[frame.code.str.upper().isin([p.upper() for p in products]), 'cob_date'].dropna().dt.strftime('%Y-%m-%d').unique()
            comparisons.extend({'label':'Settlement · '+d, 'value':'settlement:'+d} for d in sorted(dates, reverse=True))
        except (SnapshotReferenceError, KeyError, AttributeError):
            pass  # Anchor and exact intraday comparisons remain available.
    valid_context = isinstance(previous, dict) and previous.get('source') == source and previous.get('cob') == date
    revision = revision if revision not in (None,'settlement') and valid_context else 'latest'
    comparison = comparison if comparison not in (None,'previous') and valid_context else 'anchor'
    for product in products:
        code = product.upper()
        prior = previous.get('products', {}).get(code) if valid_context else None
        selected = revision.split('|',1)[1] if revision.startswith(code+'|') else (prior or {}).get('revision')
        compared = comparison.split('|',1)[1] if comparison.startswith(code+'|') else ((prior or {}).get('comparison','anchor') if comparison != 'anchor' else 'anchor')
        if comparison.startswith('settlement:'):
            compared = 'anchor'
        if ctx.triggered_id == 'refresh-options-data' or not valid_context or (revision == 'latest' and revision != (previous or {}).get('revision')):
            if not revision.startswith(code+'|'):
                selected = None
        try:
            result = select_revision(code, source, selected, compared, prior,
                refresh=ctx.triggered_id == 'refresh-options-data', cob=date)
            ro, _, co, _, child, _ = result
            selections[code] = child
            options.extend({'label':product+' · '+o['label'], 'value':code+'|'+o['value']} for o in ro)
            comparisons.extend({'label':product+' · '+o['label'], 'value':code+'|'+o['value']} for o in co if o['value'] != 'anchor')
        except Exception as exc:
            LOG.warning('Page surface selection unavailable for %s (%s)', code, type(exc).__name__)
            selections[code] = {'error':True}
            errors.append(product)
    values = {o['value'] for o in options}
    if revision not in values:
        revision = 'latest'
    if comparison not in {o['value'] for o in comparisons}:
        comparison = 'anchor'
    context = {'products':selections, 'source':source, 'revision':revision, 'comparison':comparison, 'cob':date, 'settlement_reference':snapshot_reference}
    return options, revision, comparisons, comparison, context, ('Unavailable: '+', '.join(errors) if errors else '')


@callback(Output('surface-settlement-comparison','style'), Output('surface-comparison-dropdown','style'),
          Output('surface-revision-dropdown','disabled'), Input('surface-source-dropdown','value'))
def selector_visibility(source):
    settlement = source == 'settlement'
    return ({'display':'block' if settlement else 'none'}, {'display':'none' if settlement else 'block'}, settlement)


@callback(Output('surface-toolbar-source-cards','children'), Input('dashboard-source-status-store','data'))
def toolbar_source_cards(statuses):
    return [html.Span([html.Strong(item['label']), html.Span(item.get('latest_cob') or 'Unavailable')],
                className='surface-toolbar-source-card',
                title=f"{item.get('source', '')} · {item.get('error') or item.get('latest_cob') or 'Unavailable'}")
            for item in statuses or [] if item.get('label') in {'Vol Surface','Forward Curves'}]


@callback(Output('surface-selector-status', 'title'), Input('surface-selector-status', 'children'))
def selection_status_tooltip(message):
    return message or ''
