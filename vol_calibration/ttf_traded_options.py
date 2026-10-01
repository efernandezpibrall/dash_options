"""Exact-COB raw ICE traded-option overlays for TTF calibration."""

from __future__ import annotations

from dataframe_utils import date_string, display_date

from io import StringIO
from typing import Callable

import dash_bootstrap_components as dbc
import pandas as pd
from dash import Input, Output, callback, dcc, html
from sqlalchemy import text

from db_fallback import DB_SCHEMA, safe_exception_message
from options.vol_calibration.api import (
    TTF_ICE_HUB as TTF_ICE_HUB, TTF_ICE_CONTRACT as TTF_ICE_CONTRACT,
    empty_ttf_traded_options as empty_ttf_traded_options,
    normalize_ttf_traded_options as _normalize_ttf_traded_options,
    attach_ttf_traded_option_coordinates as _attach_chart_coordinates,
)
from runtime_config import get_database_engine


TTF_TRADED_OPTIONS_TABLE = f'{DB_SCHEMA}.gas_options_activity'


def create_ttf_traded_options_store():
    return dcc.Store(id='ttf-traded-options-store')


def create_ttf_traded_options_status():
    return html.Div(
        id='ttf-traded-options-status',
        children=dbc.Alert(
            'ICE traded options loading...',
            color='secondary',
            className='py-2 px-3 mb-3 small',
        ),
    )






def load_ttf_traded_options_payload(requested_cob, *, engine=None) -> dict:
    """Load exact-COB raw TTF TFO rows whose reported volume is positive."""
    requested = date_string(requested_cob)
    base_payload = {
        'data': empty_ttf_traded_options().to_json(
            date_format='iso',
            orient='split',
        ),
        'product': 'TTF',
        'requested_cob': requested,
        'actual_cob': None,
        'surface_source': 'ICE',
        'source': TTF_TRADED_OPTIONS_TABLE,
        'filter': (
            f"hub = '{TTF_ICE_HUB}', contract = '{TTF_ICE_CONTRACT}', "
            'total_volume > 0'
        ),
        'row_count': 0,
        'expiry_count': 0,
        'total_volume': 0,
        'error': None,
    }
    if requested is None:
        return {
            **base_payload,
            'error': 'A valid COB date is required for traded options.',
        }

    query = text(f"""
        SELECT
            trade_date,
            hub,
            product AS raw_product,
            strip,
            contract,
            contract_type,
            strike,
            settlement_price,
            total_volume,
            open_interest,
            expiration_date,
            option_volatility,
            source_name,
            vendor_published_at,
            ingested_at
        FROM {TTF_TRADED_OPTIONS_TABLE}
        WHERE trade_date = :trade_date
          AND hub = :hub
          AND UPPER(contract) = :contract
          AND COALESCE(total_volume, 0) > 0
          AND strike IS NOT NULL
          AND settlement_price IS NOT NULL
          AND option_volatility IS NOT NULL
        ORDER BY strip, strike, contract_type
    """)

    try:
        db_engine = engine or get_database_engine(required=False)
        if db_engine is None:
            raise RuntimeError('Database configuration is unavailable.')
        raw = pd.read_sql(
            query,
            db_engine,
            params={
                'trade_date': pd.Timestamp(requested).date(),
                'hub': TTF_ICE_HUB,
                'contract': TTF_ICE_CONTRACT,
            },
        )
        data = _normalize_ttf_traded_options(raw, requested)
    except Exception as exc:
        return {
            **base_payload,
            'error': safe_exception_message(exc),
        }

    if data.empty:
        return base_payload

    return {
        **base_payload,
        'data': data.to_json(date_format='iso', orient='split'),
        'actual_cob': requested,
        'row_count': int(len(data)),
        'expiry_count': int(data['maturity_date'].nunique()),
        'total_volume': int(data['total_volume'].sum()),
    }




def ttf_traded_options_frame(
    payload: dict | None,
    market_data: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if not payload or not payload.get('data'):
        return empty_ttf_traded_options()
    try:
        data = pd.read_json(StringIO(payload['data']), orient='split')
    except (TypeError, ValueError):
        return empty_ttf_traded_options()

    for column in (
        'trade_date',
        'cob_date',
        'strip',
        'maturity_date',
        'expiration_date',
        'option_expiration_date',
    ):
        if column in data.columns:
            data[column] = pd.to_datetime(data[column], errors='coerce')
    for column in ('vendor_published_at', 'ingested_at'):
        if column in data.columns:
            data[column] = pd.to_datetime(data[column], errors='coerce')
    return _attach_chart_coordinates(data, market_data)


def ttf_traded_options_status_text(payload: dict | None) -> tuple[str, str]:
    payload = payload or {}
    requested = display_date(payload.get('requested_cob'))
    error = payload.get('error')
    row_count = int(payload.get('row_count') or 0)

    if error:
        return (
            'ICE traded options unavailable'
            f' · Requested COB {requested}'
            f' · Source {payload.get("source") or TTF_TRADED_OPTIONS_TABLE}'
            f' · {error}',
            'danger',
        )
    if row_count == 0:
        return (
            'ICE traded options'
            f' · COB {requested}'
            ' · No raw TTF TFO rows with positive reported volume.',
            'secondary',
        )

    expiry_count = int(payload.get('expiry_count') or 0)
    total_volume = int(payload.get('total_volume') or 0)
    parts = [
        'ICE traded options',
        f'COB {requested}',
        f'{row_count:,} traded option rows',
        f'{total_volume:,} lots',
        f'{expiry_count:,} {"expiry" if expiry_count == 1 else "expiries"}',
        f'Source {payload.get("source") or TTF_TRADED_OPTIONS_TABLE}',
    ]
    return ' · '.join(parts), 'info'


def render_ttf_traded_options_status(payload: dict | None):
    text_value, color = ttf_traded_options_status_text(payload)
    return dbc.Alert(
        text_value,
        color=color,
        className='py-2 px-3 mb-3 small',
    )


def register_ttf_traded_options_callback(default_date_factory: Callable):
    @callback(
        Output('ttf-traded-options-store', 'data'),
        Input('ttf-date-picker', 'date'),
        Input('ttf-reload-btn', 'n_clicks'),
        Input('refresh-options-data', 'n_clicks'),
        prevent_initial_call=False,
    )
    def update_ttf_traded_options(requested_cob, reload_clicks, refresh_clicks):
        del reload_clicks, refresh_clicks
        if requested_cob is None:
            requested_cob = default_date_factory()
        return load_ttf_traded_options_payload(requested_cob)

    @callback(
        Output('ttf-traded-options-status', 'children'),
        Input('ttf-traded-options-store', 'data'),
        prevent_initial_call=False,
    )
    def update_ttf_traded_options_status(payload):
        return render_ttf_traded_options_status(payload)

    return update_ttf_traded_options, update_ttf_traded_options_status
