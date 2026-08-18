"""Exact-COB raw ICE traded-option overlays for TTF calibration."""

from __future__ import annotations

from io import StringIO
from typing import Callable

import dash_bootstrap_components as dbc
import pandas as pd
from dash import Input, Output, callback, dcc, html
from sqlalchemy import text

from db_fallback import DB_SCHEMA, safe_exception_message
from options.ttf_volatility import (
    TTFVolatilityError,
    black76_call_delta,
    year_fraction,
)
from runtime_config import get_database_engine


TTF_TRADED_OPTIONS_TABLE = f'{DB_SCHEMA}.gas_options_activity'
TTF_ICE_HUB = 'TTF (Futures-style)'
TTF_ICE_CONTRACT = 'TFO'
TTF_TRADED_OPTION_COLUMNS = [
    'trade_date',
    'cob_date',
    'hub',
    'product',
    'raw_product',
    'strip',
    'maturity_date',
    'contract',
    'contract_type',
    'strike',
    'settlement_price',
    'total_volume',
    'open_interest',
    'expiration_date',
    'option_expiration_date',
    'option_volatility',
    'volatility',
    'forward_value',
    'call_delta',
    'dte',
    'surface_source',
    'source_name',
    'vendor_published_at',
    'ingested_at',
    'quality_status',
    'method',
    'day_count',
    'delta_convention',
]


def empty_ttf_traded_options() -> pd.DataFrame:
    return pd.DataFrame(columns=TTF_TRADED_OPTION_COLUMNS)


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


def _normalize_vendor_volatility(values: pd.Series) -> pd.Series:
    """Normalize raw ICE percentage volatility to decimal volatility."""
    numeric = pd.to_numeric(values, errors='coerce')
    normalized = numeric.where(numeric <= 5.0, numeric / 100.0)
    return normalized.where(normalized.gt(0) & normalized.lt(2.0))


def _normalize_ttf_traded_options(
    data: pd.DataFrame,
    requested_cob,
) -> pd.DataFrame:
    """Validate exact-COB positive-volume rows from gas_options_activity."""
    requested = pd.to_datetime(requested_cob, errors='coerce')
    if pd.isna(requested):
        raise ValueError('A valid COB date is required for traded options.')
    requested = requested.normalize()

    if data is None or data.empty:
        return empty_ttf_traded_options()

    prepared = data.copy()
    raw_columns = [
        'trade_date',
        'hub',
        'raw_product',
        'strip',
        'contract',
        'contract_type',
        'strike',
        'settlement_price',
        'total_volume',
        'open_interest',
        'expiration_date',
        'option_volatility',
        'source_name',
        'vendor_published_at',
        'ingested_at',
    ]
    for column in raw_columns:
        if column not in prepared.columns:
            prepared[column] = pd.NA

    for column in ('trade_date', 'strip', 'expiration_date'):
        prepared[column] = pd.to_datetime(
            prepared[column],
            errors='coerce',
        ).dt.normalize()
    for column in ('vendor_published_at', 'ingested_at'):
        prepared[column] = pd.to_datetime(
            prepared[column],
            errors='coerce',
        )
    for column in (
        'strike',
        'settlement_price',
        'total_volume',
        'open_interest',
        'option_volatility',
    ):
        prepared[column] = pd.to_numeric(prepared[column], errors='coerce')

    prepared['contract_type'] = (
        prepared['contract_type'].astype(str).str.strip().str.upper()
    )
    prepared = prepared[
        prepared['trade_date'].eq(requested)
        & prepared['hub'].astype(str).eq(TTF_ICE_HUB)
        & prepared['contract'].astype(str).str.upper().eq(TTF_ICE_CONTRACT)
        & prepared['contract_type'].isin({'C', 'P'})
        & prepared['total_volume'].gt(0)
        & prepared['strike'].gt(0)
        & prepared['settlement_price'].notna()
        & prepared['strip'].notna()
        & prepared['expiration_date'].notna()
    ].copy()

    duplicate_mask = prepared.duplicated(
        subset=['strip', 'strike', 'contract_type'],
        keep=False,
    )
    if duplicate_mask.any():
        raise ValueError(
            'Duplicate raw ICE traded-option rows exist for the same '
            'strip, strike, and contract type.'
        )

    prepared['volatility'] = _normalize_vendor_volatility(
        prepared['option_volatility']
    )
    prepared = prepared[prepared['volatility'].notna()].copy()
    prepared['cob_date'] = prepared['trade_date']
    prepared['product'] = 'TTF'
    prepared['maturity_date'] = prepared['strip']
    prepared['option_expiration_date'] = prepared['expiration_date']
    prepared['forward_value'] = pd.NA
    prepared['call_delta'] = pd.NA
    prepared['dte'] = (
        prepared['option_expiration_date'] - prepared['cob_date']
    ).dt.days.astype(float)
    prepared['surface_source'] = 'ICE'
    prepared['quality_status'] = 'raw_market_activity'
    prepared['method'] = 'gas_options_activity.option_volatility'
    prepared['day_count'] = 'ACT/365.25'
    prepared['delta_convention'] = 'undiscounted_forward_call_delta'

    for column in TTF_TRADED_OPTION_COLUMNS:
        if column not in prepared.columns:
            prepared[column] = pd.NA
    return prepared[TTF_TRADED_OPTION_COLUMNS].sort_values(
        ['maturity_date', 'strike', 'contract_type']
    ).reset_index(drop=True)


def load_ttf_traded_options_payload(requested_cob, *, engine=None) -> dict:
    """Load exact-COB raw TTF TFO rows whose reported volume is positive."""
    requested = _date_string(requested_cob)
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


def _attach_chart_coordinates(
    data: pd.DataFrame,
    market_data: pd.DataFrame | None,
) -> pd.DataFrame:
    """Use the calibration forward only to position raw ICE vols by delta."""
    if data.empty or market_data is None or market_data.empty:
        return data
    if not {'expiry', 'forward'}.issubset(market_data.columns):
        return data

    forwards = market_data[['expiry', 'forward']].copy()
    forwards['expiry'] = pd.to_datetime(
        forwards['expiry'],
        errors='coerce',
    ).dt.normalize()
    forwards['forward'] = pd.to_numeric(forwards['forward'], errors='coerce')
    forwards = forwards.dropna().drop_duplicates()
    inconsistent = forwards.groupby('expiry')['forward'].nunique().gt(1)
    if inconsistent.any():
        return data.assign(quality_status='inconsistent_calibration_forward')
    forward_map = (
        forwards.drop_duplicates('expiry').set_index('expiry')['forward']
    )

    positioned = data.copy()
    positioned['forward_value'] = positioned['maturity_date'].map(forward_map)
    call_deltas = []
    quality = []
    for row in positioned.itertuples(index=False):
        if pd.isna(row.forward_value):
            call_deltas.append(float('nan'))
            quality.append('missing_calibration_forward')
            continue
        try:
            time_to_expiry = year_fraction(
                row.cob_date,
                row.option_expiration_date,
            )
            call_deltas.append(
                black76_call_delta(
                    float(row.forward_value),
                    float(row.strike),
                    time_to_expiry,
                    float(row.volatility),
                )
            )
            quality.append('raw_market_activity')
        except (TTFVolatilityError, TypeError, ValueError):
            call_deltas.append(float('nan'))
            quality.append('delta_coordinate_unavailable')
    positioned['call_delta'] = call_deltas
    positioned['quality_status'] = quality
    return positioned


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
    requested = _display_date(payload.get('requested_cob'))
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
