"""Reference-only operational surface helpers for calibration pages."""

from __future__ import annotations

from io import StringIO
from typing import Callable

import dash_bootstrap_components as dbc
import pandas as pd
from dash import Input, Output, callback, ctx, dcc, html


OPERATIONAL_SURFACE_COLUMNS = [
    'cob_date',
    'code',
    'contract_date',
    'option_expiration_date',
    'delta',
    'delta_abs',
    'put_call',
    'volatility',
    'delta_bucket',
    'delta_sort_key',
    'delta_pct',
]


def empty_operational_surface() -> pd.DataFrame:
    return pd.DataFrame(columns=OPERATIONAL_SURFACE_COLUMNS)


def create_operational_surface_store(product: str):
    return dcc.Store(id=f'{product.lower()}-operational-surface-store')


def create_operational_surface_status(product: str):
    return html.Div(
        id=f'{product.lower()}-operational-surface-status',
        children=dbc.Alert(
            'Operational surface loading...',
            color='secondary',
            className='py-2 px-3 mb-3 small',
        ),
    )


def load_operational_surface_payload(
    product: str,
    requested_cob,
    *,
    refresh: bool = False,
) -> dict:
    """Resolve and serialize the shared ``/vol_surface`` snapshot contract."""
    from pages.vol_surface import get_operational_surface_snapshot

    snapshot = get_operational_surface_snapshot(
        product,
        requested_cob,
        refresh=refresh,
    )
    data = snapshot.get('data')
    if not isinstance(data, pd.DataFrame):
        data = empty_operational_surface()

    return {
        'data': data.to_json(date_format='iso', orient='split'),
        'product': snapshot.get('product', product),
        'requested_cob': _date_string(snapshot.get('requested_cob')),
        'actual_cob': _date_string(snapshot.get('actual_cob')),
        'date_fallback_used': bool(snapshot.get('date_fallback_used', False)),
        'source': snapshot.get('source') or 'unknown',
        'source_fallback_used': bool(
            snapshot.get('source_fallback_used', False)
        ),
        'error': snapshot.get('error'),
        'row_count': int(len(data)),
    }


def operational_surface_frame(payload: dict | None) -> pd.DataFrame:
    if not payload or not payload.get('data'):
        return empty_operational_surface()
    try:
        data = pd.read_json(StringIO(payload['data']), orient='split')
    except (TypeError, ValueError):
        return empty_operational_surface()

    for column in ('cob_date', 'contract_date', 'option_expiration_date'):
        if column in data.columns:
            data[column] = pd.to_datetime(data[column], errors='coerce')
    return data


def operational_surface_status_text(
    payload: dict | None,
    x_axis: str = 'delta',
) -> tuple[str, str]:
    """Return status copy and Bootstrap color for the surface reference."""
    payload = payload or {}
    requested = _display_date(payload.get('requested_cob'))
    actual = _display_date(payload.get('actual_cob'))
    source = payload.get('source') or 'unknown'
    error = payload.get('error')
    row_count = int(payload.get('row_count') or 0)

    if error or not actual or row_count == 0:
        detail = error or 'No operational surface exists on or before this COB.'
        parts = [
            'Operational Surface unavailable',
            f'Requested COB {requested}',
            f'Source {source}',
        ]
        if payload.get('source_fallback_used'):
            parts.append(f'Source fallback: {source}')
        parts.append(detail)
        text = ' · '.join(parts)
        color = 'danger'
    else:
        parts = [
            'Operational Surface',
            f'Requested COB {requested}',
            f'Surface COB {actual}',
            f'Source {source}',
        ]
        color = 'success'
        if payload.get('date_fallback_used'):
            parts.append('Date fallback: nearest prior product COB')
            color = 'warning'
        if payload.get('source_fallback_used'):
            parts.append(f'Source fallback: {source}')
            if color == 'success':
                color = 'info'
        text = ' · '.join(parts)

    if x_axis != 'delta' and row_count:
        text += ' · Select Delta to display the operational surface.'
        if color == 'success':
            color = 'info'
    return text, color


def render_operational_surface_status(
    payload: dict | None,
    x_axis: str = 'delta',
):
    text, color = operational_surface_status_text(payload, x_axis)
    return dbc.Alert(text, color=color, className='py-2 px-3 mb-3 small')


def register_operational_surface_callback(
    product: str,
    default_date_factory: Callable,
):
    """Register the product-scoped reference loader without touching session state."""
    product_lower = product.lower()

    @callback(
        Output(f'{product_lower}-operational-surface-store', 'data'),
        Input(f'{product_lower}-date-picker', 'date'),
        Input(f'{product_lower}-reload-btn', 'n_clicks'),
        Input('refresh-options-data', 'n_clicks'),
        prevent_initial_call=False,
    )
    def update_operational_surface(requested_cob, reload_clicks, refresh_clicks):
        del reload_clicks, refresh_clicks
        if requested_cob is None:
            requested_cob = default_date_factory()
        force_refresh = ctx.triggered_id in {
            f'{product_lower}-reload-btn',
            'refresh-options-data',
        }
        return load_operational_surface_payload(
            product,
            requested_cob,
            refresh=force_refresh,
        )

    @callback(
        Output(f'{product_lower}-operational-surface-status', 'children'),
        Input(f'{product_lower}-operational-surface-store', 'data'),
        Input(f'{product_lower}-x-axis-selector', 'value'),
        prevent_initial_call=False,
    )
    def update_operational_surface_status(payload, x_axis):
        return render_operational_surface_status(payload, x_axis or 'delta')

    return update_operational_surface, update_operational_surface_status


def _date_string(value):
    parsed = pd.to_datetime(value, errors='coerce')
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _display_date(value):
    parsed = pd.to_datetime(value, errors='coerce')
    if pd.isna(parsed):
        return 'unknown'
    return parsed.strftime('%d-%b-%Y')
