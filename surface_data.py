"""Shared operational volatility surfaces and immutable snapshot lifecycle.

Owns source fallback, normalization, COB resolution, and the process-local views
of the shared snapshot. Presentation and callback registration belong to pages.
"""

import hashlib
import json
import threading

import numpy as np
import pandas as pd
from sqlalchemy import text

from dataframe_utils import concat_dataframes
from db_fallback import DB_SCHEMA, read_trino_query, safe_exception_message
from runtime_config import get_database_engine
from snapshot_cache import (
    SnapshotReferenceError,
    latest_snapshot,
    publish_snapshot,
    resolve_snapshot,
    snapshot_lock,
)


_DATA_CACHE_LOCK = threading.Lock()

UNIFIED_ATM_COLUMNS = ['cob_date', 'code', 'contract_date', 'year', 'month', 'method', 'volatility']

SURFACE_COLUMNS = [
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

SURFACE_SOURCE_PRODUCTS = {'BRENT', 'HH', 'JKM', 'TTF', 'NBP'}

SURFACE_PRODUCT_DISPLAY_MAP = {'BRENT': 'Brent'}

ICE_SUMMER_MONTHS = {4, 5, 6, 7, 8, 9}

SURFACE_EXPIRY_MONTH = 'month'

SURFACE_EXPIRY_QUARTER = 'quarter'

SURFACE_EXPIRY_SEASON = 'season'

SURFACE_EXPIRY_TYPES = {
    SURFACE_EXPIRY_MONTH,
    SURFACE_EXPIRY_QUARTER,
    SURFACE_EXPIRY_SEASON,
}

SURFACE_SOURCE_COLUMNS = [
    'cob_date',
    'product',
    'maturity_date',
    'option_expiration_date',
    'put_call',
    'delta',
    'value',
]

SURFACE_SOURCE_SELECT = ', '.join(SURFACE_SOURCE_COLUMNS)

SURFACE_POSTGRES_SOURCE_LABEL = f'{DB_SCHEMA}.implied_volatility_surface_from_prices'

SURFACE_SOURCE_LABEL = 'raw.icap.implied_volatility_surface_from_prices'

SURFACE_TRINO_SOURCES = [
    ('raw.icap.implied_volatility_surface_from_prices', 'implied_volatility_surface_from_prices'),
    ('raw.icap.implied_volatility_surface', 'implied_volatility_surface'),
]

SURFACE_POSTGRES_SOURCES = [
    (
        SURFACE_POSTGRES_SOURCE_LABEL,
        f'select {SURFACE_SOURCE_SELECT} from {SURFACE_POSTGRES_SOURCE_LABEL}',
    ),
]

TTF_COMPARISON_TABLE = (
    f'{DB_SCHEMA}.option_volatility_surface_comparison_current'
)

VALUATION_CURRENT_TABLE = f'{DB_SCHEMA}.trades_options_valuation_current'



def _empty_unified_atm_df():
    return pd.DataFrame(columns=UNIFIED_ATM_COLUMNS)


def _empty_surface_df():
    return pd.DataFrame(columns=SURFACE_COLUMNS)


def _source_status_template(source_name):
    return {
        'source': source_name,
        'error': None,
        'rows': 0,
        'latest_cob_date': None,
        'fallback_used': False,
    }

atm_dataset = _empty_unified_atm_df()

surface_dataset = _empty_surface_df()

_SURFACE_SNAPSHOT_CACHE = {}

_SURFACE_PIVOT_CACHE = {}

_SURFACE_SNAPSHOT_GENERATION = 0

_SURFACE_SNAPSHOT_CACHE_ATTR = '_surface_snapshot_cache_key'

VOL_SURFACE_SNAPSHOT_NAMESPACE = 'vol-surface-v1'

_ACTIVE_SURFACE_SNAPSHOT_ID = None

DATA_CACHE_STATE = {
    'initialized': False,
    'last_refresh_token': None,
    'atm': _source_status_template(SURFACE_SOURCE_LABEL),
    'surface': _source_status_template(SURFACE_SOURCE_LABEL),
}


def _surface_quarter_key(value):
    timestamp = pd.to_datetime(value, errors='coerce')
    if pd.isna(timestamp):
        return None
    return f'{timestamp.year}-Q{timestamp.quarter}'


def _surface_season_key(value):
    timestamp = pd.to_datetime(value, errors='coerce')
    if pd.isna(timestamp):
        return None
    if timestamp.month in ICE_SUMMER_MONTHS:
        return f'{timestamp.year}-Summer'
    winter_year = timestamp.year if timestamp.month >= 10 else timestamp.year - 1
    return f'{winter_year}-Winter'


def _surface_expiry_value(expiry_type, key):
    if expiry_type not in SURFACE_EXPIRY_TYPES or key is None:
        return None
    if expiry_type == SURFACE_EXPIRY_MONTH:
        timestamp = pd.to_datetime(key, errors='coerce')
        if pd.isna(timestamp):
            return None
        key = timestamp.strftime('%Y-%m-%d')
    return f'{expiry_type}:{key}'


def _parse_surface_expiry_selection(value):
    if value is None or value == '':
        return None, None

    text_value = str(value)
    if ':' in text_value:
        expiry_type, key = text_value.split(':', 1)
        if expiry_type in SURFACE_EXPIRY_TYPES:
            if expiry_type == SURFACE_EXPIRY_MONTH:
                timestamp = pd.to_datetime(key, errors='coerce')
                if pd.isna(timestamp):
                    return None, None
                return expiry_type, timestamp.strftime('%Y-%m-%d')
            return expiry_type, key

    timestamp = pd.to_datetime(text_value, errors='coerce')
    if pd.isna(timestamp):
        return None, None
    return SURFACE_EXPIRY_MONTH, timestamp.strftime('%Y-%m-%d')


def _normalize_surface_expiry_selection(value):
    expiry_type, key = _parse_surface_expiry_selection(value)
    return _surface_expiry_value(expiry_type, key)


def _filter_surface_by_expiry_selection(surface_df, selected_expiry):
    if surface_df.empty:
        return surface_df.copy()

    expiry_type, key = _parse_surface_expiry_selection(selected_expiry)
    if expiry_type is None:
        return surface_df.iloc[0:0].copy()

    contract_dates = pd.to_datetime(surface_df['contract_date'], errors='coerce')
    if expiry_type == SURFACE_EXPIRY_MONTH:
        selected_date = pd.to_datetime(key).normalize()
        mask = contract_dates.dt.normalize().eq(selected_date)
    elif expiry_type == SURFACE_EXPIRY_QUARTER:
        key_parts = str(key).split('-Q', 1)
        if len(key_parts) != 2 or not all(value.isdigit() for value in key_parts):
            return surface_df.iloc[0:0].copy()
        year, quarter = map(int, key_parts)
        mask = contract_dates.dt.year.eq(year) & contract_dates.dt.quarter.eq(
            quarter
        )
    else:
        key_parts = str(key).rsplit('-', 1)
        if len(key_parts) != 2 or not key_parts[0].isdigit():
            return surface_df.iloc[0:0].copy()
        season_year, season = int(key_parts[0]), key_parts[1]
        months = contract_dates.dt.month
        if season == 'Summer':
            mask = contract_dates.dt.year.eq(season_year) & months.isin(ICE_SUMMER_MONTHS)
        elif season == 'Winter':
            mask = (
                (contract_dates.dt.year.eq(season_year) & months.ge(10))
                | (contract_dates.dt.year.eq(season_year + 1) & months.le(3))
            )
        else:
            return surface_df.iloc[0:0].copy()

    return surface_df.loc[mask].copy()


def _select_existing_column(df, candidates):
    for column in candidates:
        if column in df.columns:
            return column
    return None


def load_surface_atm_data(surface_df=None):
    surface_df = load_surface_data()[0] if surface_df is None else surface_df.copy()
    if surface_df.empty:
        return _empty_unified_atm_df()

    surface_df = surface_df.copy()
    surface_df['delta_distance'] = (surface_df['delta_abs'] - 0.5).abs()
    surface_df = surface_df.sort_values(['code', 'cob_date', 'contract_date', 'delta_distance', 'delta_sort_key'])
    surface_df = surface_df.drop_duplicates(['code', 'cob_date', 'contract_date'], keep='first')
    surface_df['year'] = surface_df['contract_date'].dt.year
    surface_df['month'] = surface_df['contract_date'].dt.month
    surface_df['method'] = 'implied_volatility_surface_atm'

    return surface_df[UNIFIED_ATM_COLUMNS]


def _normalize_surface_data(surface_df):
    if surface_df.empty:
        return _empty_surface_df()

    surface_df = surface_df.copy()

    product_col = _select_existing_column(surface_df, ['product', 'commodity', 'code'])
    contract_col = _select_existing_column(surface_df, ['maturity_date', 'expiry', 'contract_date'])
    option_expiration_col = _select_existing_column(surface_df, ['option_expiration_date', 'expiration_date'])
    vol_col = _select_existing_column(surface_df, ['value', 'implied_vol', 'volatility', 'iv'])
    delta_col = _select_existing_column(surface_df, ['delta'])
    put_call_col = _select_existing_column(surface_df, ['put_call', 'option_type', 'option_side'])

    required_columns = [product_col, contract_col, vol_col, delta_col]
    if any(column is None for column in required_columns) or 'cob_date' not in surface_df.columns:
        return _empty_surface_df()

    rename_map = {
        product_col: 'code',
        contract_col: 'contract_date',
        vol_col: 'volatility',
        delta_col: 'delta',
    }
    if put_call_col is not None:
        rename_map[put_call_col] = 'put_call'
    if option_expiration_col is not None:
        rename_map[option_expiration_col] = 'option_expiration_date'

    surface_df = surface_df.rename(columns=rename_map)
    surface_df['code'] = surface_df['code'].astype(str).str.strip().str.upper()
    surface_df = surface_df[surface_df['code'].isin(SURFACE_SOURCE_PRODUCTS)]
    if surface_df.empty:
        return _empty_surface_df()
    surface_df['code'] = surface_df['code'].replace(SURFACE_PRODUCT_DISPLAY_MAP)

    surface_df['cob_date'] = pd.to_datetime(surface_df['cob_date'], errors='coerce')
    surface_df['contract_date'] = pd.to_datetime(surface_df['contract_date'], errors='coerce')
    if 'option_expiration_date' in surface_df.columns:
        surface_df['option_expiration_date'] = pd.to_datetime(surface_df['option_expiration_date'], errors='coerce')
    else:
        surface_df['option_expiration_date'] = pd.NaT
    surface_df['volatility'] = pd.to_numeric(surface_df['volatility'], errors='coerce')
    surface_df['delta'] = pd.to_numeric(surface_df['delta'], errors='coerce')

    if 'put_call' not in surface_df.columns:
        surface_df['put_call'] = None

    normalized_side = surface_df['put_call'].astype('string').str.strip().str.lower()
    normalized_side = normalized_side.map(
        {'p': 'put', 'put': 'put', 'c': 'call', 'call': 'call'}
    )
    surface_df['put_call'] = normalized_side.astype(object).where(
        normalized_side.notna(), None
    )
    surface_df['delta_abs'] = surface_df['delta'].abs()
    surface_df.loc[surface_df['delta_abs'] > 1, 'delta_abs'] = surface_df.loc[surface_df['delta_abs'] > 1, 'delta_abs'] / 100.0

    has_signed_delta_convention = surface_df['delta'].lt(0).any()
    if has_signed_delta_convention:
        signed_put_mask = surface_df['put_call'].isna() & (surface_df['delta'] < 0)
        signed_call_mask = surface_df['put_call'].isna() & (surface_df['delta'] > 0)
        surface_df.loc[signed_put_mask, 'put_call'] = 'put'
        surface_df.loc[signed_call_mask, 'put_call'] = 'call'

    if not surface_df['volatility'].dropna().empty and surface_df['volatility'].max() > 5:
        surface_df['volatility'] = surface_df['volatility'] / 100.0

    surface_df = surface_df.dropna(subset=['cob_date', 'contract_date', 'volatility', 'delta_abs'])
    if surface_df.empty:
        return _empty_surface_df()

    delta_pct = (surface_df['delta_abs'] * 100).round()
    delta_label = delta_pct.astype('Int64').astype('string')
    is_atm = surface_df['delta_abs'].sub(0.5).abs().lt(1e-8)
    is_put = surface_df['put_call'].eq('put')
    is_call = surface_df['put_call'].eq('call')
    delta_bucket = (delta_label + 'D').mask(is_put, delta_label + 'P')
    delta_bucket = delta_bucket.mask(is_call, delta_label + 'C').mask(is_atm, 'ATM')
    surface_df['delta_bucket'] = delta_bucket.astype(object).where(delta_bucket.notna(), None)
    surface_df['delta_sort_key'] = delta_pct.mask(is_call, 100.0 - delta_pct).mask(is_atm, 50.0)
    surface_df['delta_pct'] = surface_df['delta_abs'] * 100.0
    surface_df = surface_df.dropna(subset=['delta_bucket', 'delta_sort_key'])
    surface_df = surface_df.sort_values(['code', 'cob_date', 'contract_date', 'delta_sort_key']).reset_index(drop=True)

    return surface_df[SURFACE_COLUMNS]


def load_surface_data():
    load_errors = []

    for source_index, (source_label, table_name) in enumerate(
        SURFACE_TRINO_SOURCES
    ):
        try:
            surface_df = read_trino_query(
                f'select {SURFACE_SOURCE_SELECT} from {table_name}',
                catalog='raw',
                schema='icap',
            )
            normalized_surface = _normalize_surface_data(surface_df)
            if normalized_surface.empty:
                load_errors.append(f'{source_label}: no usable rows')
                continue
            return normalized_surface, {
                'source': source_label,
                'error': None,
                'fallback_used': source_index > 0,
            }
        except Exception as exc:
            load_errors.append(f'{source_label}: {safe_exception_message(exc)}')

    for source_label, surface_query in SURFACE_POSTGRES_SOURCES:
        try:
            surface_df = pd.read_sql(sql=surface_query, con=get_database_engine())
            normalized_surface = _normalize_surface_data(surface_df)
            if normalized_surface.empty:
                load_errors.append(f'{source_label}: no usable rows')
                continue
            return normalized_surface, {
                'source': source_label,
                'error': None,
                'fallback_used': True,
            }
        except Exception as exc:
            load_errors.append(f'{source_label}: {safe_exception_message(exc)}')

    attempted_sources = ', '.join(
        [source for source, _ in SURFACE_TRINO_SOURCES] +
        [source for source, _ in SURFACE_POSTGRES_SOURCES]
    )
    return _empty_surface_df(), {
        'source': attempted_sources,
        'error': f'Surface load failed from all sources: {" | ".join(load_errors)}',
        'fallback_used': False,
    }


def _build_source_status(df, source_name, error_message=None, fallback_used=False):
    latest_cob_date = None
    if not df.empty and 'cob_date' in df.columns:
        cob_dates = pd.to_datetime(df['cob_date'], errors='coerce').dropna()
        if not cob_dates.empty:
            latest_cob_date = cob_dates.max()

    return {
        'source': source_name,
        'error': error_message,
        'rows': int(len(df)),
        'latest_cob_date': latest_cob_date,
        'fallback_used': fallback_used,
    }


def _surface_source_revision(surface_df, source_meta):
    if surface_df.empty:
        frame_digest = 'empty'
        latest_cob = None
    else:
        frame_digest = hashlib.sha256(
            pd.util.hash_pandas_object(surface_df, index=True).values.tobytes()
        ).hexdigest()
        latest_cob = pd.to_datetime(
            surface_df['cob_date'], errors='coerce'
        ).max()
    revision = {
        'schema_version': 1,
        'source': source_meta.get('source'),
        'latest_cob': str(latest_cob),
        'rows': int(len(surface_df)),
        'frame_sha256': frame_digest,
    }
    encoded = json.dumps(revision, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _build_surface_snapshot_payload(refresh_token=None):
    loaded_surface_df = _empty_surface_df()
    atm_error = None
    surface_meta = _source_status_template(SURFACE_SOURCE_LABEL)
    try:
        loaded_surface_df, surface_loader_meta = load_surface_data()
        surface_meta.update(surface_loader_meta)
    except Exception as exc:
        surface_meta.update({
            'source': SURFACE_SOURCE_LABEL,
            'error': safe_exception_message(exc),
            'fallback_used': False,
        })

    try:
        loaded_atm_df = load_surface_atm_data(loaded_surface_df)
    except Exception as exc:
        atm_error = f'surface-derived ATM build failed: {safe_exception_message(exc)}'
        loaded_atm_df = _empty_unified_atm_df()

    state = {
        'initialized': True,
        'last_refresh_token': refresh_token,
        'atm': _build_source_status(
            loaded_atm_df,
            surface_meta['source'],
            atm_error,
            fallback_used=surface_meta.get('fallback_used', False),
        ),
        'surface': _build_source_status(
            loaded_surface_df,
            surface_meta['source'],
            surface_meta['error'],
            fallback_used=surface_meta.get('fallback_used', False),
        ),
    }
    payload = {
        'atm_dataset': loaded_atm_df,
        'surface_dataset': loaded_surface_df,
        'data_cache_state': state,
    }
    return payload, _surface_source_revision(loaded_surface_df, surface_meta)


def _activate_surface_snapshot(reference):
    global atm_dataset, surface_dataset, DATA_CACHE_STATE
    global _SURFACE_SNAPSHOT_GENERATION, _ACTIVE_SURFACE_SNAPSHOT_ID

    snapshot_id = reference.get('snapshot_id') if isinstance(reference, dict) else None
    if (
        snapshot_id
        and snapshot_id == _ACTIVE_SURFACE_SNAPSHOT_ID
        and DATA_CACHE_STATE['initialized']
    ):
        return reference

    payload = resolve_snapshot(
        reference,
        expected_namespace=VOL_SURFACE_SNAPSHOT_NAMESPACE,
    )
    loaded_atm = payload.get('atm_dataset')
    loaded_surface = payload.get('surface_dataset')
    loaded_state = payload.get('data_cache_state')
    if not isinstance(loaded_atm, pd.DataFrame) or not isinstance(loaded_surface, pd.DataFrame):
        raise SnapshotReferenceError('Volatility snapshot payload is invalid')
    if not isinstance(loaded_state, dict):
        raise SnapshotReferenceError('Volatility snapshot status is invalid')

    with _DATA_CACHE_LOCK:
        atm_dataset = loaded_atm
        surface_dataset = loaded_surface
        DATA_CACHE_STATE = loaded_state
        _SURFACE_SNAPSHOT_CACHE.clear()
        _SURFACE_PIVOT_CACHE.clear()
        _SURFACE_SNAPSHOT_GENERATION += 1
        _ACTIVE_SURFACE_SNAPSHOT_ID = snapshot_id
    return reference


def prepare_vol_surface_snapshot(*, force=False, refresh_token=None):
    """Resolve or publish the immutable page dataset used by every renderer."""
    if not force:
        reference = latest_snapshot(VOL_SURFACE_SNAPSHOT_NAMESPACE)
        if reference:
            try:
                return _activate_surface_snapshot(reference)
            except SnapshotReferenceError:
                pass

    with snapshot_lock('build-vol-surface-v1', expire=300):
        if not force:
            reference = latest_snapshot(VOL_SURFACE_SNAPSHOT_NAMESPACE)
            if reference:
                try:
                    return _activate_surface_snapshot(reference)
                except SnapshotReferenceError:
                    pass

        payload, source_revision = _build_surface_snapshot_payload(refresh_token)
        state = payload['data_cache_state']
        reference = publish_snapshot(
            VOL_SURFACE_SNAPSHOT_NAMESPACE,
            source_revision,
            payload,
            metadata={
                'source': state['surface']['source'],
                'rows': state['surface']['rows'],
                'latest_cob_date': state['surface']['latest_cob_date'],
                'fallback_used': state['surface']['fallback_used'],
                'error': state['surface']['error'],
            },
            group='vol-surface',
            force=force,
        )
        return _activate_surface_snapshot(reference)


def _refresh_cached_data(refresh_token=None, force=False):
    if isinstance(refresh_token, dict):
        return _activate_surface_snapshot(refresh_token)
    if force:
        return prepare_vol_surface_snapshot(
            force=True,
            refresh_token=refresh_token,
        )
    if DATA_CACHE_STATE['initialized']:
        return latest_snapshot(VOL_SURFACE_SNAPSHOT_NAMESPACE)
    return prepare_vol_surface_snapshot(force=False, refresh_token=refresh_token)


def _ensure_cached_data(snapshot_reference=None):
    return _refresh_cached_data(refresh_token=snapshot_reference, force=False)


def _get_all_available_dates():
    date_series = []
    if not atm_dataset.empty and 'cob_date' in atm_dataset.columns:
        date_series.append(pd.to_datetime(atm_dataset['cob_date'], errors='coerce'))
    if not surface_dataset.empty and 'cob_date' in surface_dataset.columns:
        date_series.append(pd.to_datetime(surface_dataset['cob_date'], errors='coerce'))

    if not date_series:
        return []

    all_dates = concat_dataframes(date_series, ignore_index=True).dropna().drop_duplicates()
    return sorted(all_dates.tolist())


def _get_supported_surface_products(selected_products, selected_date=None):
    if not selected_products:
        return []

    available_products = set(surface_dataset['code'].unique()) if not surface_dataset.empty else set()
    supported_products = [product for product in selected_products if product in available_products]

    if selected_date is None or surface_dataset.empty:
        return supported_products

    selected_date = pd.to_datetime(selected_date).normalize()
    current_products = set(
        surface_dataset.loc[
            surface_dataset['cob_date'].dt.normalize() == selected_date,
            'code',
        ].dropna().unique()
    )
    current_supported = [product for product in supported_products if product in current_products]
    other_supported = [product for product in supported_products if product not in current_products]
    return current_supported + other_supported


def _get_surface_snapshot(code, cob_date):
    if surface_dataset.empty or code is None or cob_date is None:
        return _empty_surface_df()

    cob_date = pd.to_datetime(cob_date)
    cache_key = (_SURFACE_SNAPSHOT_GENERATION, str(code), cob_date.normalize().strftime('%Y-%m-%d'))
    if cache_key in _SURFACE_SNAPSHOT_CACHE:
        snapshot = _SURFACE_SNAPSHOT_CACHE[cache_key].copy()
        snapshot.attrs[_SURFACE_SNAPSHOT_CACHE_ATTR] = cache_key
        return snapshot

    surface_df = surface_dataset

    snapshot = surface_df[
        (surface_df['code'] == code) &
        (surface_df['cob_date'].dt.normalize() == cob_date.normalize())
    ].copy()

    snapshot = snapshot.sort_values(['contract_date', 'delta_sort_key'])
    _SURFACE_SNAPSHOT_CACHE[cache_key] = snapshot
    snapshot = snapshot.copy()
    snapshot.attrs[_SURFACE_SNAPSHOT_CACHE_ATTR] = cache_key
    return snapshot


def get_operational_surface_snapshot(product, requested_cob, refresh=False):
    """Return the governed surface for an exact COB or its nearest prior COB.

    The returned rows use the same normalized schema and source order as the
    ``/vol_surface`` page.  Resolution is product-scoped and can only move
    backwards in time.
    """
    requested_timestamp = pd.to_datetime(requested_cob, errors='coerce')
    normalized_product = str(product or '').strip().upper()
    display_product = SURFACE_PRODUCT_DISPLAY_MAP.get(
        normalized_product,
        normalized_product,
    )

    result = {
        'data': _empty_surface_df(),
        'product': display_product,
        'requested_cob': requested_timestamp.normalize()
        if not pd.isna(requested_timestamp)
        else None,
        'actual_cob': None,
        'date_fallback_used': False,
        'source': DATA_CACHE_STATE['surface']['source'],
        'source_fallback_used': bool(
            DATA_CACHE_STATE['surface'].get('fallback_used', False)
        ),
        'error': None,
    }

    if normalized_product not in SURFACE_SOURCE_PRODUCTS:
        result['error'] = f'Unsupported operational surface product: {product}'
        return result
    if pd.isna(requested_timestamp):
        result['error'] = f'Invalid requested COB: {requested_cob}'
        return result

    if refresh:
        _refresh_cached_data(force=True)
    else:
        _ensure_cached_data()

    surface_status = DATA_CACHE_STATE['surface']
    result['source'] = surface_status['source']
    result['source_fallback_used'] = bool(
        surface_status.get('fallback_used', False)
    )

    if surface_dataset.empty:
        result['error'] = (
            surface_status.get('error')
            or f'No operational surface data is available for {display_product}'
        )
        return result

    requested_timestamp = requested_timestamp.normalize()
    product_rows = surface_dataset.loc[
        surface_dataset['code'] == display_product
    ]
    eligible_dates = (
        pd.to_datetime(product_rows['cob_date'], errors='coerce')
        .dt.normalize()
        .loc[lambda values: values <= requested_timestamp]
        .dropna()
    )
    if eligible_dates.empty:
        result['error'] = (
            f'No operational surface COB exists on or before '
            f'{requested_timestamp:%Y-%m-%d} for {display_product}'
        )
        return result

    actual_cob = eligible_dates.max()
    snapshot = _get_surface_snapshot(display_product, actual_cob)
    if snapshot.empty:
        result['error'] = (
            f'Operational surface resolution returned no rows for '
            f'{display_product} on {actual_cob:%Y-%m-%d}'
        )
        return result

    result['data'] = snapshot
    result['actual_cob'] = actual_cob
    result['date_fallback_used'] = actual_cob < requested_timestamp
    return result


def _get_surface_delta_order(surface_df):
    if surface_df.empty:
        return pd.DataFrame(columns=['delta_bucket', 'delta_sort_key'])

    return (
        surface_df[['delta_bucket', 'delta_sort_key']]
        .drop_duplicates()
        .sort_values(['delta_sort_key', 'delta_bucket'])
    )


def _build_surface_pivot(surface_df):
    if surface_df.empty:
        return pd.DataFrame(), []

    snapshot_cache_key = surface_df.attrs.get(_SURFACE_SNAPSHOT_CACHE_ATTR)
    pivot_cache_key = None
    if snapshot_cache_key is not None:
        pivot_cache_key = (snapshot_cache_key, len(surface_df))
        cached_pivot = _SURFACE_PIVOT_CACHE.get(pivot_cache_key)
        if cached_pivot is not None:
            pivot, delta_columns = cached_pivot
            return pivot.copy(), list(delta_columns)

    delta_order = _get_surface_delta_order(surface_df)
    delta_columns = delta_order['delta_bucket'].tolist()

    pivot = surface_df.pivot_table(
        values='volatility',
        index='contract_date',
        columns='delta_bucket',
        aggfunc='first'
    )
    pivot = pivot.reindex(columns=delta_columns).sort_index()

    if pivot_cache_key is not None:
        _SURFACE_PIVOT_CACHE[pivot_cache_key] = (pivot.copy(), tuple(delta_columns))

    return pivot, delta_columns


def _build_surface_dte_lookup(surface_df):
    if surface_df.empty or 'option_expiration_date' not in surface_df.columns:
        return pd.DataFrame(columns=['contract_date', 'option_expiration_date'])

    lookup = surface_df[['contract_date', 'option_expiration_date']].copy()
    lookup['contract_date'] = pd.to_datetime(lookup['contract_date'], errors='coerce')
    lookup['option_expiration_date'] = pd.to_datetime(lookup['option_expiration_date'], errors='coerce')
    lookup = lookup.dropna(subset=['contract_date', 'option_expiration_date'])
    if lookup.empty:
        return pd.DataFrame(columns=['contract_date', 'option_expiration_date'])

    return (
        lookup
        .drop_duplicates(['contract_date', 'option_expiration_date'])
        .sort_values(['contract_date', 'option_expiration_date'])
        .drop_duplicates('contract_date', keep='first')
        .reset_index(drop=True)
    )


def _load_ttf_source_comparison(selected_date, selected_expiry, engine=None):
    """Load published normalized ICAP/ICE nodes and TFO trade-strike marks."""
    if not selected_date or not selected_expiry:
        return pd.DataFrame(), pd.DataFrame()
    db_engine = engine or get_database_engine(required=False)
    params = {
        'cob_date': pd.Timestamp(selected_date).date(),
        'maturity_date': pd.Timestamp(selected_expiry).date(),
    }
    curve_query = text(f"""
        SELECT
            valuation_run_id,
            valuation_revision,
            valuation_methodology_version,
            'EUR' AS currency,
            cob_date,
            maturity_date,
            option_expiration_date,
            surface_source,
            native_node_type,
            native_node_value,
            strike,
            call_delta,
            volatility,
            forward_value,
            settlement_price,
            contract_type,
            total_volume,
            open_interest,
            vendor_volatility,
            vendor_volatility_difference,
            put_call_parity_difference,
            quality_status,
            valid_for_comparison,
            source_name,
            vendor_published_at,
            ingested_at,
            method,
            day_count,
            delta_convention
        FROM {TTF_COMPARISON_TABLE}
        WHERE cob_date = :cob_date
          AND product = 'TTF'
          AND maturity_date = :maturity_date
        ORDER BY surface_source, strike
    """)
    trade_query = text(f"""
        SELECT
            valuation_run_id,
            valuation_revision,
            currency,
            substrategy,
            buy_sell,
            put_call,
            strike,
            forward_price_used,
            volatility_used,
            comparison_call_delta_used,
            comparison_volatility_used,
            comparison_status,
            price,
            comparison_price,
            qty_pnl,
            comparison_qty_pnl
        FROM {VALUATION_CURRENT_TABLE}
        WHERE cob_date = :cob_date
          AND contract_convention_code = 'ICE_TTF_TFO'
          AND maturity_date_a = :maturity_date
        ORDER BY strike, buy_sell
    """)
    curves = pd.read_sql(curve_query, db_engine, params=params)
    trades = pd.read_sql(trade_query, db_engine, params=params)
    return curves, trades


def _get_selected_tenor_rank(product, selected_expiry, end_date):
    current_surface = _get_surface_snapshot(product, end_date)
    if current_surface.empty:
        return 0

    expiries = sorted(pd.to_datetime(current_surface['contract_date']).drop_duplicates())
    if not expiries:
        return 0

    expiry_type, expiry_key = _parse_surface_expiry_selection(selected_expiry)
    if expiry_type != SURFACE_EXPIRY_MONTH:
        return 0

    selected_expiry = pd.to_datetime(expiry_key)
    return expiries.index(selected_expiry) if selected_expiry in expiries else 0


def _select_rolling_tenor_history(history_df, tenor_rank):
    if history_df.empty:
        return history_df

    selected_rows = []
    for cob_date, cob_slice in history_df.groupby('cob_date'):
        expiries = sorted(pd.to_datetime(cob_slice['contract_date']).drop_duplicates())
        if not expiries:
            continue
        selected_expiry = expiries[min(tenor_rank, len(expiries) - 1)]
        selected_rows.append(cob_slice[cob_slice['contract_date'] == selected_expiry])

    if not selected_rows:
        return history_df.iloc[0:0].copy()
    return concat_dataframes(selected_rows, ignore_index=True)


def group_data_by_period(data, grouping_mode):
    data = data.copy()
    data['contract_date'] = pd.to_datetime(data['contract_date'])

    if grouping_mode == 'monthly':
        data['period'] = data['contract_date'].dt.strftime('%m-%y')
        return data

    if grouping_mode == 'quarterly':
        data['period'] = (
            data['contract_date'].dt.year.astype(str)
            + '-Q'
            + data['contract_date'].dt.quarter.astype(str)
        )
        return data.groupby(['code', 'cob_date', 'period']).agg({'volatility': 'mean'}).reset_index()

    if grouping_mode == 'season':
        years = data['contract_date'].dt.year
        months = data['contract_date'].dt.month
        season_years = years.where(months.isin(ICE_SUMMER_MONTHS) | months.ge(10), years - 1)
        periods = season_years.astype('Int64').astype('string') + np.where(
            months.isin(ICE_SUMMER_MONTHS), '-Summer', '-Winter'
        )
        data['period'] = periods.astype(object).where(periods.notna(), None)
        return data.groupby(['code', 'cob_date', 'period']).agg({'volatility': 'mean'}).reset_index()

    if grouping_mode == 'calendar':
        data['period'] = data['contract_date'].dt.year.astype(str)
        return data.groupby(['code', 'cob_date', 'period']).agg({'volatility': 'mean'}).reset_index()

    data['period'] = data['contract_date'].dt.strftime('%m-%y')
    return data


def _sort_grouped_period_columns(date_cols, grouping_mode):
    if grouping_mode == 'monthly':
        def month_year_to_date(month_year):
            try:
                month, year = month_year.split('-')
                return pd.to_datetime(f'20{year}-{month}-01')
            except Exception:
                return pd.to_datetime('2100-01-01')

        return sorted(date_cols, key=month_year_to_date)

    if grouping_mode == 'quarterly':
        def quarter_key(value):
            try:
                year, quarter = value.split('-')
                return int(year), int(quarter[1])
            except Exception:
                return 9999, 0

        return sorted(date_cols, key=quarter_key)

    if grouping_mode == 'season':
        def season_key(value):
            try:
                year, season = value.split('-')
                return int(year), 0 if season == 'Summer' else 1
            except Exception:
                return 9999, 0

        return sorted(date_cols, key=season_key)

    if grouping_mode == 'calendar':
        return sorted(date_cols, key=lambda value: int(value) if str(value).isdigit() else 9999)

    return list(date_cols)


def _build_atm_table_frames(selected_date, prev_selected_date, selected_products, grouping_mode):
    if selected_date is None or not selected_products or atm_dataset.empty:
        return pd.DataFrame(columns=['product']), pd.DataFrame(columns=['product']), []

    selected_date = pd.to_datetime(selected_date)
    prev_date = pd.to_datetime(prev_selected_date) if prev_selected_date else None

    atm_df = atm_dataset.copy()
    atm_df['cob_date'] = pd.to_datetime(atm_df['cob_date'], errors='coerce')
    atm_df['contract_date'] = pd.to_datetime(atm_df['contract_date'], errors='coerce')

    product_df = atm_df[atm_df['code'].isin(selected_products)].copy()
    if product_df.empty:
        return pd.DataFrame(columns=['product']), pd.DataFrame(columns=['product']), []

    current_data = product_df[product_df['cob_date'].dt.normalize() == selected_date.normalize()].copy()
    current_pivot = pd.DataFrame(columns=['product'])
    sorted_date_cols = []

    if not current_data.empty:
        current_grouped = group_data_by_period(current_data, grouping_mode)
        current_grouped['product'] = current_grouped['code']
        current_pivot = current_grouped.pivot_table(
            values='volatility',
            index='product',
            columns='period',
            aggfunc='first'
        ).reset_index()

        date_cols = [column for column in current_pivot.columns if column != 'product']
        sorted_date_cols = _sort_grouped_period_columns(date_cols, grouping_mode)
        current_pivot = current_pivot[['product'] + sorted_date_cols]

    changes_pivot = pd.DataFrame(columns=['product'])
    if prev_date is not None and not current_data.empty:
        prev_data = product_df[product_df['cob_date'].dt.normalize() == prev_date.normalize()].copy()
        if not prev_data.empty:
            prev_grouped = group_data_by_period(prev_data, grouping_mode)
            prev_grouped['product'] = prev_grouped['code']
            prev_pivot = prev_grouped.pivot_table(
                values='volatility',
                index='product',
                columns='period',
                aggfunc='first'
            )

            current_indexed = current_pivot.set_index('product') if not current_pivot.empty else pd.DataFrame()
            all_products = sorted(set(current_indexed.index.tolist()) | set(prev_pivot.index.tolist()))
            if all_products:
                current_aligned = current_indexed.reindex(all_products)
                prev_aligned = prev_pivot.reindex(all_products).reindex(columns=sorted_date_cols)
                changes_pivot = (current_aligned[sorted_date_cols] - prev_aligned).reset_index().rename(columns={'index': 'product'})

    return current_pivot, changes_pivot, sorted_date_cols
