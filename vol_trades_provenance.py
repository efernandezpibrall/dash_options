"""Vol Trades source alignment and immutable ICAP revision receipts."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd
from dash import html
from sqlalchemy import text


def _date(value):
    timestamp = pd.to_datetime(value, errors='coerce')
    return None if pd.isna(timestamp) else timestamp.date().isoformat()


def prepare_icap_layer(snapshot, cob_date, *, allow_prior=False):
    requested = _date(cob_date)
    metadata = {'requested_cob': requested, 'actual_cob': None, 'status': 'unavailable',
                'source_revision': None, 'snapshot_id': None, 'allow_prior': bool(allow_prior)}
    empty = pd.DataFrame()
    empty.attrs['icap_metadata'] = metadata
    if snapshot is None:
        metadata['status'] = 'refresh_required'
        return empty
    metadata.update(actual_cob=_date(snapshot.get('actual_cob')),
                    source_revision=snapshot.get('source_revision'),
                    snapshot_id=snapshot.get('snapshot_id'), source=snapshot.get('source'))
    surface = snapshot.get('data')
    actual = metadata['actual_cob']
    if snapshot.get('error') or not isinstance(surface, pd.DataFrame) or surface.empty or not actual:
        return empty
    # A future date is never a legitimate prior-date comparison.
    if actual > requested or (actual != requested and not allow_prior):
        metadata['status'] = 'date_unavailable'
        return empty
    metadata['status'] = 'aligned' if actual == requested else 'prior_comparison'
    surface = surface.copy()
    surface['cob_date'] = pd.Timestamp(actual)
    surface['forward_value'] = float('nan')
    surface['source_name'] = f'ICAP settlement · COB {actual}'
    surface = surface[['cob_date', 'contract_date', 'option_expiration_date', 'put_call',
                       'delta', 'volatility', 'forward_value', 'source_name']]
    surface.attrs['icap_metadata'] = metadata
    return surface


def source_revision_event(product, cob_date, refresh_source):
    event = {'product': product, 'cob_date': cob_date}
    try:
        reference = refresh_source('TTF', cob_date)
        event.update(icap_revision=reference['source_revision'], icap_snapshot_id=reference['snapshot_id'])
    except Exception:
        event['error'] = 'ICAP freshness could not be verified; refresh required'
    # Detect publications made in another tab or by the operational publisher.
    from runtime_config import get_database_engine
    try:
        with get_database_engine().connect() as connection:
            row = connection.execute(text("""
                SELECT publication_id::text, cob_date, published_at
                FROM at_lng.vol_surface_publications
                WHERE commodity = 'TTF' AND status = 'published' AND is_active
                ORDER BY cob_date DESC, published_at DESC, created_at DESC LIMIT 1
            """)).mappings().first()
        event['calibration'] = json.loads(json.dumps(dict(row) if row else {}, default=str))
    except Exception:
        event['calibration_error'] = 'Calibration revision could not be verified'
    return event


def render_toolbar_sources(snapshot: dict[str, Any] | None):
    """Show the calibration and forward snapshot used by the selected charts."""
    snapshot = snapshot or {}
    calibration = snapshot.get('calibration') or {}
    calibration_cob = _date(calibration.get('cob_date'))
    publication = pd.to_datetime(calibration.get('published_at'), errors='coerce', utc=True)
    calibration_detail = 'unavailable'
    if calibration_cob and snapshot.get('calibration_status') == 'available':
        published = (publication.tz_convert('Asia/Dubai').strftime('%d %b %Y %H:%M GST')
                     if not pd.isna(publication) else 'publication time unavailable')
        calibration_detail = f'{calibration_cob} · {published}'
    curve_cob = _date(snapshot.get('business_date'))
    curve_kind = {
        'SETTLEMENT': 'Settlement',
        'INTRADAY': 'Intraday',
        'OFFICIAL_COB': 'Settlement',
    }.get(snapshot.get('snapshot_kind'), 'source unavailable')
    curve_detail = f'{curve_cob} · {curve_kind}' if curve_cob else 'unavailable'
    return html.Div(
        [
            html.Div(
                [html.Strong('Calibration: '), calibration_detail],
                className='brent-vol-history-source-card',
                title=f"Publication {calibration.get('publication_id') or 'unavailable'}",
            ),
            html.Div(
                [html.Strong('Curves: '), curve_detail],
                className='brent-vol-history-source-card',
            ),
        ],
        className='brent-vol-history-source-cards',
        role='status',
        **{'aria-live': 'polite'},
    )


def render_icap_date_warning(snapshot: dict[str, Any] | None):
    """Explain the ICAP date actually plotted beside the expiry section title."""
    snapshot = snapshot or {}
    if snapshot.get('product') != 'TFO' or snapshot.get('snapshot_kind') != 'SETTLEMENT':
        return None
    icap = snapshot.get('icap') or {}
    status = icap.get('status')
    selected = _date(snapshot.get('business_date'))
    if status == 'aligned':
        return None
    if status == 'prior_comparison':
        actual = pd.Timestamp(icap['actual_cob']).strftime('%d %b %Y')
        requested = pd.Timestamp(selected).strftime('%d %b %Y')
        return f'⚠ ICAP {actual} · prior date (selected {requested})'
    if status == 'refresh_required':
        return '⚠ ICAP freshness unavailable · marks hidden'
    requested = pd.Timestamp(selected).strftime('%d %b %Y') if selected else 'selected date'
    return f'⚠ ICAP unavailable on or before {requested} · marks hidden'


def render_provenance(snapshot: dict[str, Any] | None):
    if not snapshot:
        return html.Div('Select a market snapshot to verify source dates.'), None
    selected = _date(snapshot.get('business_date'))
    product = snapshot.get('product')
    is_tfo_settlement = product == 'TFO' and snapshot.get('snapshot_kind') == 'SETTLEMENT'
    calibration = snapshot.get('calibration') or {}
    calibration_cob = _date(calibration.get('cob_date'))
    publication = pd.to_datetime(calibration.get('published_at'), errors='coerce', utc=True)
    labels = []
    issues = []
    market_label = 'Bloomberg settlement' if snapshot.get('snapshot_kind') == 'SETTLEMENT' else 'Bloomberg intraday'
    if product == 'JKM':
        market_label = 'Official ICAP / ICE market'
    labels.append(html.Span(f'{market_label}: {selected}', className='vol-trades-source-date'))
    if is_tfo_settlement:
        icap = snapshot.get('icap') or {}
        status = icap.get('status')
        if status in ('aligned', 'prior_comparison'):
            labels.append(html.Span(f"ICAP: {icap.get('actual_cob')}", className='vol-trades-source-date'))
            if status == 'prior_comparison':
                issues.append(f"Prior-date comparison: ICAP {icap.get('actual_cob')} versus Bloomberg {selected}.")
        elif status == 'refresh_required':
            labels.append(html.Span('ICAP: refresh required', className='vol-trades-source-date'))
            issues.append('ICAP freshness could not be verified. Diamonds are hidden; refresh required.')
        else:
            labels.append(html.Span('ICAP: unavailable', className='vol-trades-source-date'))
            issues.append(f'ICAP settlements unavailable for {selected}. Diamonds are hidden.')
    if calibration_cob and snapshot.get("calibration_status") == "available":
        published = (publication.tz_convert('Asia/Dubai').strftime('%d %b %Y %H:%M GST')
                     if not pd.isna(publication) else 'publication time unavailable')
        labels.append(html.Span(f'Calibration: {calibration_cob} · Published {published}',
                                className='vol-trades-source-date',
                                title=f"Publication {calibration.get('publication_id', 'unavailable')}"))
        if calibration_cob != selected:
            issues.append(f'Calibration date {calibration_cob} differs from selected market date {selected}.')
    else:
        labels.append(html.Span('Calibration: unavailable', className='vol-trades-source-date'))
        issues.append('No verified calibrated publication is available for this chart.')
    return html.Div([
        html.Div(labels, className='vol-trades-source-dates'),
        html.Div(' '.join(issues), role='alert', className='vol-trades-source-warning') if issues else
        html.Div('Source dates aligned', className='vol-trades-source-aligned'),
    ], className='vol-trades-source-provenance'), render_icap_date_warning(snapshot)
