# pages/greeks.py
import hashlib
import io
import json
import logging
import threading
from collections import OrderedDict
from functools import lru_cache
from zoneinfo import ZoneInfo

import dash
import dash_ag_grid as dag
from dash import Input, Output, State, callback, dcc, html
import pandas as pd
import requests
from sqlalchemy import text

from dataframe_utils import concat_dataframes
from db_fallback import DB_SCHEMA
from runtime_config import config_bool, config_value, get_database_engine
from snapshot_cache import (
    SnapshotReferenceError,
    bump_snapshot_generation,
    publish_snapshot,
    resolve_snapshot,
    snapshot_generation,
)

logger = logging.getLogger(__name__)

VALUATION_TABLE = f'{DB_SCHEMA}.trades_options_valuation_current'
ASPECT_SOURCE_LABEL = 'Aspect Position Exposure Report'
ASPECT_DEFAULT_URL = (
    'https://at.myaspect.net/webservice/aspectrs/'
    '_MiddleOffice_POSITION_ExposureReport'
)
ASPECT_DEFAULT_BOOK = 'AD-LNG'
ASPECT_REQUIRED_COLUMNS = {
    'instrument',
    'qty',
    'uoM',
    'maturityForwardDate',
    'strategy',
    'entityType',
}
ASPECT_EXPORT_COLUMNS = [
    'instrument',
    'qty',
    'uoM',
    'maturityForwardDate',
    'strategy',
    'dealType',
    'entityType',
    'exchangeTradeOtc',
]
_GREEKS_SERVER_CACHE = OrderedDict()
_GREEKS_SERVER_CACHE_LOCK = threading.Lock()
_GREEKS_SERVER_CACHE_MAX_ENTRIES = 32

GREEK_DEFINITIONS = {
    'delta': {
        'label': 'Delta',
        'type': 'instrument',
        'side_columns': {'a': 'qty_delta_asset_a', 'b': 'qty_delta_asset_b'},
    },
    'gamma': {
        'label': 'Gamma',
        'type': 'instrument',
        'side_columns': {'a': 'qty_gamma_asset_a', 'b': 'qty_gamma_asset_b'},
    },
    'vega': {
        'label': 'Vega',
        'type': 'instrument',
        'side_columns': {'a': 'qty_vega_sigma1', 'b': 'qty_vega_sigma2'},
    },
    'theta': {
        'label': 'Theta',
        'type': 'instrument_or_pair',
        'column': 'qty_theta',
    },
    'correlation': {
        'label': 'Correlation',
        'type': 'pair',
        'column': 'qty_corr_sensitivity',
    },
}

GREEK_KEYS = list(GREEK_DEFINITIONS.keys())

MATURITY_AGGREGATION_OPTIONS = [
    {'label': 'Mixed', 'value': 'mixed'},
    {'label': 'Month', 'value': 'month'},
    {'label': 'Quarter', 'value': 'quarter'},
    {'label': 'Year', 'value': 'year'},
]

UNIT_MODE_OPTIONS = [
    {'label': 'Native', 'value': 'native'},
    {'label': 'Lots', 'value': 'lots'},
]

ASSET_DISPLAY_LABELS = {
    'ICE_BRENT_FUTURES': 'Brent',
    'ICE_HH': 'HH',
    'ICE_JKM': 'JKM',
    'ICE_TTF': 'TTF',
    'TFM': 'TTF',
}

ASSET_DISPLAY_ORDER = ['TTF', 'JKM', 'HH', 'Brent']

DATA_COLUMNS = [
    'cob_date',
    'trade_date',
    'substrategy',
    'type_trade',
    'type_option',
    'put_call',
    'buy_sell',
    'currency',
    'expiration_date',
    'quantity',
    'unit_quantity',
    'asset_a',
    'asset_b',
    'maturity_date_type_a',
    'maturity_date_a',
    'maturity_date_type_b',
    'maturity_date_b',
    'price_a',
    'price_b',
    'adjusted_vol_a',
    'adjusted_vol_b',
    'correlation',
    'qty_delta_asset_a',
    'qty_delta_asset_b',
    'qty_gamma_asset_a',
    'qty_gamma_asset_b',
    'qty_vega_sigma1',
    'qty_vega_sigma2',
    'qty_theta',
    'qty_corr_sensitivity',
    'qty_value',
    'qty_pnl',
]


def _empty_store(message='No data available'):
    return {
        'meta': {
            'message': message,
            'cob_date': None,
            'raw_rows': 0,
            'normalized_rows': 0,
            'strategies': 0,
            'trade_types': 0,
            'units': [],
            'aggregation': 'mixed',
            'month_through': None,
            'quarter_through': None,
            'unit_mode': 'native',
        },
        'rows': [],
    }


def _empty_aspect_store(message='Aspect exposure has not been loaded', status='idle'):
    return {
        'meta': {
            'status': status,
            'message': message,
            'source': ASPECT_SOURCE_LABEL,
            'mode': None,
            'requested_cob_date': None,
            'fetched_at': None,
            'source_rows': 0,
            'accepted_rows': 0,
            'rejected_rows': 0,
            'instruments': 0,
            'strategies': 0,
        },
        'rows': [],
    }


class AspectConfigurationError(RuntimeError):
    """Raised when the Aspect source is not configured for this runtime."""


class AspectSourceError(RuntimeError):
    """Raised when the Aspect source cannot provide a usable exposure snapshot."""


def _greeks_cache_key(namespace, parts):
    payload = json.dumps(
        {
            'namespace': namespace,
            'generation': snapshot_generation('greeks'),
            'parts': parts,
        },
        sort_keys=True,
        default=str,
        separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _cache_greeks_payload(payload, namespace, parts):
    cache_key = _greeks_cache_key(namespace, parts)
    reference = publish_snapshot(
        f'greeks-{namespace}-v1',
        cache_key,
        payload,
        metadata=payload.get('meta', {}),
        group='greeks',
    )
    snapshot_id = reference['snapshot_id']
    with _GREEKS_SERVER_CACHE_LOCK:
        _GREEKS_SERVER_CACHE[snapshot_id] = payload
        _GREEKS_SERVER_CACHE.move_to_end(snapshot_id)
        while len(_GREEKS_SERVER_CACHE) > _GREEKS_SERVER_CACHE_MAX_ENTRIES:
            _GREEKS_SERVER_CACHE.popitem(last=False)
    return reference


def _resolve_greeks_payload(reference):
    if not reference:
        return _empty_store('No data available')
    if 'rows' in reference:
        return reference
    snapshot_id = reference.get('snapshot_id')
    namespace = reference.get('namespace')
    if not isinstance(namespace, str) or not namespace.startswith('greeks-'):
        return _empty_store('Server snapshot expired; refresh the page')
    with _GREEKS_SERVER_CACHE_LOCK:
        payload = _GREEKS_SERVER_CACHE.get(snapshot_id)
        if payload is not None:
            _GREEKS_SERVER_CACHE.move_to_end(snapshot_id)
    if payload is not None:
        return payload
    try:
        payload = resolve_snapshot(
            reference,
            expected_namespace=namespace,
        )
    except SnapshotReferenceError:
        return _empty_store('Server snapshot expired; refresh the page')
    with _GREEKS_SERVER_CACHE_LOCK:
        _GREEKS_SERVER_CACHE[snapshot_id] = payload
        _GREEKS_SERVER_CACHE.move_to_end(snapshot_id)
        while len(_GREEKS_SERVER_CACHE) > _GREEKS_SERVER_CACHE_MAX_ENTRIES:
            _GREEKS_SERVER_CACHE.popitem(last=False)
    return payload


def _resolve_aspect_payload(reference):
    if not reference:
        return _empty_aspect_store()

    payload = _resolve_greeks_payload(reference)
    if payload.get('meta', {}).get('source') == ASPECT_SOURCE_LABEL:
        return payload
    return _empty_aspect_store('Aspect snapshot expired; reload Aspect exposure', status='error')


def _clear_greeks_server_cache():
    bump_snapshot_generation('greeks')
    with _GREEKS_SERVER_CACHE_LOCK:
        _GREEKS_SERVER_CACHE.clear()


def _safe_number(value, default=0.0):
    if value is None or pd.isna(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_unit(unit):
    if unit is None or pd.isna(unit):
        return 'N/A'

    normalized = str(unit).strip()
    normalized_key = normalized.lower().replace(' ', '')
    unit_map = {
        'mmbtu': 'MMBtu',
        'mmbtus': 'MMBtu',
        'mmbtu/day': 'MMBtu',
        'mmbtu/d': 'MMBtu',
        'mmbtuperday': 'MMBtu',
        'bbl': 'BBL',
        'bbls': 'BBL',
        'barrel': 'BBL',
        'barrels': 'BBL',
        'day': 'Day',
        'days': 'Day',
    }
    return unit_map.get(normalized_key, normalized)


def _lot_divisor(unit):
    normalized = _normalize_unit(unit)
    if normalized == 'BBL':
        return 1000.0
    if normalized == 'MMBtu':
        return 10000.0
    return 1.0


def _standard_pair(asset_a, asset_b):
    assets = [str(asset).strip() for asset in [asset_a, asset_b] if asset is not None and not pd.isna(asset)]
    if not assets:
        return 'N/A'
    if len(assets) == 1:
        return assets[0]
    return ' / '.join(sorted(assets))


def _underlying_sides(row):
    sides = []
    for side in ('a', 'b'):
        asset = row.get(f'asset_{side}')
        if asset is None or pd.isna(asset) or not str(asset).strip():
            continue
        sides.append((side, str(asset).strip()))
    return sides


def _display_asset_label(value):
    if value is None or pd.isna(value):
        return 'N/A'

    label = str(value).strip()
    if not label:
        return 'N/A'

    if label in ASSET_DISPLAY_LABELS:
        return ASSET_DISPLAY_LABELS[label]

    upper_label = label.upper()
    if upper_label in ASSET_DISPLAY_LABELS:
        return ASSET_DISPLAY_LABELS[upper_label]

    if upper_label.startswith('ICE_'):
        cleaned = label[4:].replace('_FUTURES', '').replace('_', ' ').strip()
        return cleaned.title() if cleaned else label

    return label


def _compact_pair_label(value):
    parts = [_display_asset_label(part) for part in str(value or '').split(' / ') if part.strip()]
    if not parts:
        return 'N/A'

    unique_parts = []
    for part in parts:
        if part not in unique_parts:
            unique_parts.append(part)

    preferred_orders = {
        frozenset(['TTF', 'JKM']): ['TTF', 'JKM'],
        frozenset(['Brent', 'HH']): ['Brent', 'HH'],
    }
    preferred_order = preferred_orders.get(frozenset(unique_parts))
    if preferred_order:
        unique_parts = [part for part in preferred_order if part in unique_parts]

    if len(unique_parts) == 1:
        return unique_parts[0]
    return '-'.join(unique_parts)


def _asset_sort_position(label):
    if label in ASSET_DISPLAY_ORDER:
        return ASSET_DISPLAY_ORDER.index(label)
    return len(ASSET_DISPLAY_ORDER) + 1


def _ladder_column_sort_key(column):
    parts = str(column).split('-')
    return (
        1 if len(parts) > 1 else 0,
        [_asset_sort_position(part) for part in parts],
        str(column),
    )


def _to_timestamp(value):
    date_value = pd.to_datetime(value, errors='coerce')
    if pd.isna(date_value):
        return None
    return pd.Timestamp(date_value).normalize()


def _format_cutoff_value(value):
    date_value = _to_timestamp(value)
    if date_value is None:
        return None
    return date_value.strftime('%Y-%m-%d')


def _quarter_end(value):
    date_value = _to_timestamp(value)
    if date_value is None:
        return None
    return (date_value + pd.offsets.QuarterEnd(0)).normalize()


def _year_end(value):
    date_value = _to_timestamp(value)
    if date_value is None:
        return None
    return (date_value + pd.offsets.YearEnd(0)).normalize()


def _quarter_label(value):
    date_value = _to_timestamp(value)
    if date_value is None:
        return 'Unknown'
    return f'{date_value.year}-Q{((date_value.month - 1) // 3) + 1}'


def _month_through_label(value):
    date_value = _to_timestamp(value)
    if date_value is None:
        return 'Unknown'
    return f'{_quarter_label(date_value)} / {date_value.strftime("%Y-%m")}'


def _quarter_through_label(value):
    date_value = _to_timestamp(value)
    if date_value is None:
        return 'Unknown'
    return str(date_value.year)


def _default_month_cutoff(cob_date):
    date_value = _to_timestamp(cob_date)
    if date_value is None:
        date_value = pd.Timestamp.today().normalize()
    return _quarter_end(date_value + pd.DateOffset(months=6))


def _default_quarter_cutoff(cob_date):
    date_value = _to_timestamp(cob_date)
    if date_value is None:
        date_value = pd.Timestamp.today().normalize()
    return _year_end(date_value + pd.DateOffset(months=12))


def _resolve_maturity_cutoffs(cob_date, month_through=None, quarter_through=None):
    month_cutoff = _to_timestamp(month_through) or _default_month_cutoff(cob_date)
    quarter_cutoff = _to_timestamp(quarter_through) or _default_quarter_cutoff(cob_date)
    if quarter_cutoff < month_cutoff:
        quarter_cutoff = _year_end(month_cutoff)
    return month_cutoff, quarter_cutoff


def _format_date_bucket(value, aggregation='mixed', month_through=None, quarter_through=None, cob_date=None):
    date_value = pd.to_datetime(value, errors='coerce')
    if pd.isna(date_value):
        return 'Unknown'

    if aggregation == 'mixed':
        month_cutoff, quarter_cutoff = _resolve_maturity_cutoffs(cob_date, month_through, quarter_through)
        date_value = pd.Timestamp(date_value).normalize()
        if date_value <= month_cutoff:
            return date_value.strftime('%Y-%m')
        if date_value <= quarter_cutoff:
            return _quarter_label(date_value)
        return str(date_value.year)
    if aggregation == 'year':
        return str(date_value.year)
    if aggregation == 'quarter':
        return f'{date_value.year}-Q{((date_value.month - 1) // 3) + 1}'
    return date_value.strftime('%Y-%m')


def _maturity_sort_key(value):
    value = str(value)
    if value == 'Unknown':
        return (9999, 12, 9, value)

    first_component = value.split(' / ', 1)[0]
    if first_component.endswith('-CAL') and len(first_component) >= 8:
        year_part = first_component[:4]
        if year_part.isdigit():
            return (int(year_part), 12, 3, value)
    if '-Q' in first_component:
        year_part, quarter_part = first_component.split('-Q', 1)
        if year_part.isdigit() and quarter_part[:1].isdigit():
            return (int(year_part), int(quarter_part[:1]) * 3, 2, value)
    if len(first_component) == 7 and first_component[4] == '-':
        year_part, month_part = first_component.split('-', 1)
        if year_part.isdigit() and month_part.isdigit():
            return (int(year_part), int(month_part), 1, value)
    if len(first_component) == 4 and first_component.isdigit():
        return (int(first_component), 12, 4, value)
    return (9998, 12, 8, value)


def _calendar_month_starts(value):
    date_value = pd.to_datetime(value, errors='coerce')
    if pd.isna(date_value):
        return []
    return [pd.Timestamp(year=date_value.year, month=month, day=1) for month in range(1, 13)]


def _expand_instrument_maturity(
    value,
    maturity_type,
    greek_value,
    aggregation,
    month_through=None,
    quarter_through=None,
    cob_date=None,
):
    maturity_type = str(maturity_type or '').strip().lower()
    if maturity_type == 'calendar':
        months = _calendar_month_starts(value)
        if not months:
            return [('Unknown', greek_value)]
        allocated_value = greek_value / len(months)
        return [
            (
                _format_date_bucket(month, aggregation, month_through, quarter_through, cob_date),
                allocated_value,
            )
            for month in months
        ]
    return [(_format_date_bucket(value, aggregation, month_through, quarter_through, cob_date), greek_value)]


def _format_maturity_component(
    value,
    maturity_type,
    aggregation,
    month_through=None,
    quarter_through=None,
    cob_date=None,
):
    maturity_type = str(maturity_type or '').strip().lower()
    date_value = pd.to_datetime(value, errors='coerce')
    if pd.isna(date_value):
        return 'Unknown'
    maturity_bucket = _format_date_bucket(
        date_value,
        aggregation,
        month_through,
        quarter_through,
        cob_date,
    )
    if (
        maturity_type == 'calendar'
        and not (len(maturity_bucket) == 4 and maturity_bucket.isdigit())
    ):
        return f'{date_value.year}-CAL'
    return maturity_bucket


def _format_maturity_pair(row, aggregation, month_through=None, quarter_through=None, cob_date=None):
    label_a = _format_maturity_component(
        row.get('maturity_date_a'),
        row.get('maturity_date_type_a'),
        aggregation,
        month_through,
        quarter_through,
        cob_date,
    )
    label_b = _format_maturity_component(
        row.get('maturity_date_b'),
        row.get('maturity_date_type_b'),
        aggregation,
        month_through,
        quarter_through,
        cob_date,
    )
    if label_a == label_b:
        return label_a
    return f'{label_a} / {label_b}'


def _format_cob_option(value):
    date_value = pd.to_datetime(value, errors='coerce')
    if pd.isna(date_value):
        return None
    return date_value.strftime('%Y-%m-%d')


def _dubai_today():
    return pd.Timestamp.now(tz=ZoneInfo('Asia/Dubai')).date()


def _resolve_aspect_request_date(mode, selected_date):
    if mode == 'live':
        return _dubai_today()
    if mode != 'settlement':
        raise ValueError(f'Unsupported Aspect pricing mode: {mode}')

    date_value = _to_timestamp(selected_date)
    if date_value is None:
        raise AspectSourceError('Select a COB date before loading Aspect settlement exposure.')
    return date_value.date()


def _aspect_request_timeout():
    raw_value = config_value('ASPECT', 'REQUEST_TIMEOUT_SECONDS', fallback='90')
    try:
        timeout = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise AspectConfigurationError(
            'ASPECT.REQUEST_TIMEOUT_SECONDS must be a number.'
        ) from exc
    if not 1 <= timeout <= 300:
        raise AspectConfigurationError(
            'ASPECT.REQUEST_TIMEOUT_SECONDS must be between 1 and 300 seconds.'
        )
    return timeout


def _extract_aspect_response_records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ('data', 'rows', 'result'):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return candidate
        if ASPECT_REQUIRED_COLUMNS.issubset(payload):
            return [payload]
    raise AspectSourceError('Aspect returned an unsupported response shape.')


def fetch_aspect_exposure_report(request_date):
    username = config_value('ASPECT', 'USERNAME')
    password = config_value('ASPECT', 'PASSWORD')
    if not username or not password:
        raise AspectConfigurationError(
            'Aspect credentials are unavailable. Configure ASPECT_USERNAME and '
            'ASPECT_PASSWORD for the dashboard runtime.'
        )

    endpoint = config_value('ASPECT', 'BASE_URL', fallback=ASPECT_DEFAULT_URL) or ASPECT_DEFAULT_URL
    book = config_value('ASPECT', 'BOOK', fallback=ASPECT_DEFAULT_BOOK) or ASPECT_DEFAULT_BOOK
    try:
        verify_ssl = config_bool('ASPECT', 'VERIFY_SSL', fallback=True)
    except ValueError as exc:
        raise AspectConfigurationError(str(exc)) from exc
    proxy_url = config_value('NETWORK', 'PROXY_URL')
    timeout = _aspect_request_timeout()
    today_pricing = 0 if request_date == _dubai_today() else 1
    request_payload = {
        'cobDate': request_date.strftime('%Y-%m-%d'),
        'todayPricing': today_pricing,
        'book': book,
        'displayExchangeTradeOtc': True,
    }
    request_options = {
        'json': request_payload,
        'auth': (username, password),
        'timeout': (min(10.0, timeout), timeout),
        'verify': verify_ssl,
    }
    if proxy_url:
        request_options['proxies'] = {'http': proxy_url, 'https': proxy_url}

    try:
        response = requests.post(endpoint, **request_options)
    except requests.RequestException as exc:
        raise AspectSourceError('Aspect service could not be reached.') from exc

    if response.status_code in {401, 403}:
        raise AspectSourceError('Aspect authentication was rejected.')
    if not response.ok:
        raise AspectSourceError(
            f'Aspect service request failed with HTTP {response.status_code}.'
        )

    try:
        response_payload = response.json()
    except ValueError as exc:
        raise AspectSourceError('Aspect returned a non-JSON response.') from exc

    records = _extract_aspect_response_records(response_payload)
    return pd.DataFrame.from_records(records)


def _prepare_aspect_records(data):
    missing_columns = sorted(ASPECT_REQUIRED_COLUMNS - set(data.columns))
    if missing_columns:
        raise AspectSourceError(
            'Aspect response is missing required fields: ' + ', '.join(missing_columns)
        )

    prepared = data.copy()
    for column in ASPECT_EXPORT_COLUMNS:
        if column not in prepared.columns:
            prepared[column] = None

    prepared['instrument'] = prepared['instrument'].astype('string').str.strip()
    prepared['qty'] = pd.to_numeric(prepared['qty'], errors='coerce')
    prepared['maturityForwardDate'] = pd.to_datetime(
        prepared['maturityForwardDate'],
        errors='coerce',
    )
    valid_rows = (
        prepared['instrument'].notna()
        & prepared['instrument'].ne('')
        & prepared['qty'].notna()
        & prepared['maturityForwardDate'].notna()
    )
    rejected_rows = int((~valid_rows).sum())
    prepared = prepared.loc[valid_rows, ASPECT_EXPORT_COLUMNS].copy()
    if prepared.empty and not data.empty:
        raise AspectSourceError(
            'Aspect returned rows, but none had a valid instrument, quantity, and maturity.'
        )

    for column in ('uoM', 'strategy', 'dealType', 'entityType', 'exchangeTradeOtc'):
        prepared[column] = prepared[column].fillna('N/A').astype(str)
    prepared['qty'] = prepared['qty'].astype(float)
    prepared['maturityForwardDate'] = prepared['maturityForwardDate'].dt.strftime('%Y-%m-%d')
    return prepared, rejected_rows


def _load_aspect_payload(mode, selected_date):
    request_date = _resolve_aspect_request_date(mode, selected_date)
    source_data = fetch_aspect_exposure_report(request_date)
    prepared, rejected_rows = _prepare_aspect_records(source_data)
    fetched_at = pd.Timestamp.now(tz=ZoneInfo('Asia/Dubai')).isoformat()
    meta = {
        'status': 'ok',
        'message': 'Aspect exposure loaded',
        'source': ASPECT_SOURCE_LABEL,
        'mode': mode,
        'requested_cob_date': request_date.strftime('%Y-%m-%d'),
        'fetched_at': fetched_at,
        'source_rows': int(source_data.shape[0]),
        'accepted_rows': int(prepared.shape[0]),
        'rejected_rows': rejected_rows,
        'instruments': int(prepared['instrument'].nunique()),
        'strategies': int(prepared['strategy'].nunique()),
        'book': config_value('ASPECT', 'BOOK', fallback=ASPECT_DEFAULT_BOOK) or ASPECT_DEFAULT_BOOK,
    }
    return {'meta': meta, 'rows': prepared.to_dict('records')}


def _read_sql(query, params=None):
    return pd.read_sql(text(query), get_database_engine(), params=params or {})


def get_available_dates():
    try:
        query = f'''
            SELECT DISTINCT cob_date
            FROM {VALUATION_TABLE}
            ORDER BY cob_date DESC
        '''
        dates = _read_sql(query)
        return [_format_cob_option(value) for value in dates['cob_date'].dropna()]
    except Exception:
        return []


def fetch_filter_values(cob_date):
    if not cob_date:
        return [], [], []

    data = fetch_options_data(cob_date)
    if data.empty:
        return [], [], []

    strategies = sorted(data['substrategy'].dropna().astype(str).unique().tolist())
    trade_types = sorted(data['type_trade'].dropna().astype(str).unique().tolist())

    assets = sorted(
        set(data['asset_a'].dropna().astype(str).tolist())
        | set(data['asset_b'].dropna().astype(str).tolist())
    )
    pairs = sorted({
        _standard_pair(row['asset_a'], row['asset_b'])
        for _, row in data[['asset_a', 'asset_b']].dropna(how='all').iterrows()
        if len(_underlying_sides(row)) == 2
    })

    bucket_options = (
        [{'label': f'Instrument | {asset}', 'value': f'instrument::{asset}'} for asset in assets]
        + [{'label': f'Pair | {pair}', 'value': f'pair::{pair}'} for pair in pairs]
    )
    return strategies, trade_types, bucket_options


def fetch_options_data(cob_date):
    return _fetch_options_data_cached(cob_date).copy()


@lru_cache(maxsize=8)
def _fetch_options_data_cached(cob_date):
    if not cob_date:
        return pd.DataFrame(columns=DATA_COLUMNS)

    query = f'''
        SELECT {', '.join(DATA_COLUMNS)}
        FROM {VALUATION_TABLE}
        WHERE cob_date = :cob_date
    '''
    try:
        data = _read_sql(query, {'cob_date': cob_date})
    except Exception:
        return pd.DataFrame(columns=DATA_COLUMNS)

    for column in ['cob_date', 'trade_date', 'expiration_date', 'maturity_date_a', 'maturity_date_b']:
        if column in data.columns:
            data[column] = pd.to_datetime(data[column], errors='coerce')

    return data


def fetch_max_maturity_date(cob_date):
    if not cob_date:
        return None

    try:
        data = fetch_options_data(cob_date)
    except Exception:
        return None

    if data.empty:
        return None

    maturities = concat_dataframes([
        data[['maturity_date_a']].rename(columns={'maturity_date_a': 'maturity_date'}),
        data[['maturity_date_b']].rename(columns={'maturity_date_b': 'maturity_date'}),
    ], ignore_index=True)
    maturity_dates = pd.to_datetime(maturities['maturity_date'], errors='coerce').dropna()
    if maturity_dates.empty:
        return None
    return pd.Timestamp(maturity_dates.max()).normalize()


def _iter_quarter_ends(start_date, end_date):
    current = _quarter_end(start_date)
    end = _quarter_end(end_date)
    if current is None or end is None:
        return []

    values = []
    while current <= end:
        values.append(current)
        current = (current + pd.offsets.QuarterEnd(1)).normalize()
    return values


def _iter_year_ends(start_date, end_date):
    current = _year_end(start_date)
    end = _year_end(end_date)
    if current is None or end is None:
        return []

    values = []
    while current <= end:
        values.append(current)
        current = (current + pd.offsets.YearEnd(1)).normalize()
    return values


def build_maturity_cutoff_options(cob_date):
    cob_ts = _to_timestamp(cob_date)
    if cob_ts is None:
        return [], None, [], None

    max_maturity = fetch_max_maturity_date(cob_date) or cob_ts
    default_month = _default_month_cutoff(cob_ts)
    default_quarter = _default_quarter_cutoff(cob_ts)
    option_end = max(max_maturity, default_month, default_quarter)

    month_options = [
        {'label': _month_through_label(value), 'value': _format_cutoff_value(value)}
        for value in _iter_quarter_ends(cob_ts, option_end)
    ]
    quarter_options = [
        {'label': _quarter_through_label(value), 'value': _format_cutoff_value(value)}
        for value in _iter_year_ends(cob_ts, option_end)
    ]

    return (
        month_options,
        _format_cutoff_value(default_month),
        quarter_options,
        _format_cutoff_value(default_quarter),
    )


@lru_cache(maxsize=1)
def fetch_instrument_unit_map():
    try:
        query = f'''
            SELECT
                a.instrument,
                b."Conv_factor",
                b."From_unit" AS native_unit,
                b."To_unit" AS unit
            FROM {DB_SCHEMA}.mapping_instrument_marker a
            LEFT JOIN {DB_SCHEMA}.mapping_instrument_curve_enverus b
                ON a."Marker" = b."Instrument"
        '''
        mapping = _read_sql(query)
    except Exception:
        return {}

    unit_map = {}
    for _, row in mapping.iterrows():
        instrument = row.get('instrument')
        if instrument is None or pd.isna(instrument):
            continue
        unit_map[str(instrument)] = {
            'native_unit': _normalize_unit(row.get('native_unit')),
            'unit': _normalize_unit(row.get('unit')),
            'conv_factor': _safe_number(row.get('Conv_factor'), 1.0) or 1.0,
        }
    return unit_map


def _instrument_unit_info(asset, unit_map, fallback_unit):
    info = unit_map.get(str(asset), {})
    return {
        'native_unit': _normalize_unit(
            info.get('native_unit') or fallback_unit
        ),
        'unit': _normalize_unit(info.get('unit') or fallback_unit),
        'conv_factor': _safe_number(info.get('conv_factor'), 1.0) or 1.0,
    }


def normalize_greek_contributions(
    data,
    aggregation='mixed',
    unit_mode='native',
    month_through=None,
    quarter_through=None,
    cob_date=None,
):
    if data.empty:
        return pd.DataFrame()

    unit_map = fetch_instrument_unit_map()
    normalized_rows = []
    data = data.reset_index(drop=True)

    for source_row_id, row in data.iterrows():
        pair = _standard_pair(row.get('asset_a'), row.get('asset_b'))
        underlying_sides = _underlying_sides(row)
        base = {
            'source_row_id': int(source_row_id),
            'source': 'Options valuation',
            'cob_date': _format_cob_option(row.get('cob_date')),
            'strategy': row.get('substrategy') or 'N/A',
            'trade_type': row.get('type_trade') or 'N/A',
            'option_type': row.get('type_option') or 'N/A',
            'put_call': row.get('put_call') or 'N/A',
            'currency': row.get('currency'),
            'asset_pair': pair,
            'quantity': _safe_number(row.get('quantity')),
            'value': _safe_number(row.get('qty_value')),
            'pnl': _safe_number(row.get('qty_pnl')),
        }

        for greek_key in ['delta', 'gamma', 'vega']:
            definition = GREEK_DEFINITIONS[greek_key]
            for side, source_column in definition['side_columns'].items():
                asset = row.get(f'asset_{side}')
                if asset is None or pd.isna(asset):
                    continue

                raw_value = _safe_number(row.get(source_column))
                unit_info = _instrument_unit_info(asset, unit_map, row.get('unit_quantity'))
                display_unit = unit_info['native_unit']
                display_value = raw_value

                if greek_key in ['delta', 'gamma'] and unit_mode == 'lots':
                    display_value = display_value * unit_info['conv_factor']
                    display_value = display_value / _lot_divisor(unit_info['unit'])
                    display_unit = 'Lots'

                maturity_column = f'maturity_date_{side}'
                maturity_type_column = f'maturity_date_type_{side}'
                maturity_entries = _expand_instrument_maturity(
                    row.get(maturity_column),
                    row.get(maturity_type_column),
                    display_value,
                    aggregation,
                    month_through,
                    quarter_through,
                    cob_date,
                )

                for maturity_bucket, allocated_value in maturity_entries:
                    normalized_rows.append({
                        **base,
                        'greek': greek_key,
                        'greek_label': definition['label'],
                        'bucket_type': 'Instrument',
                        'risk_bucket': str(asset),
                        'instrument': str(asset),
                        'unit': display_unit,
                        'maturity_bucket': maturity_bucket,
                        'maturity_pair': maturity_bucket,
                        'exposure': allocated_value,
                    })

        theta_value = _safe_number(row.get('qty_theta'))
        if len(underlying_sides) == 1:
            theta_side, theta_asset = underlying_sides[0]
            theta_unit_info = _instrument_unit_info(
                theta_asset,
                unit_map,
                row.get('unit_quantity'),
            )
            theta_entries = _expand_instrument_maturity(
                row.get(f'maturity_date_{theta_side}'),
                row.get(f'maturity_date_type_{theta_side}'),
                theta_value,
                aggregation,
                month_through,
                quarter_through,
                cob_date,
            )
            for theta_maturity, allocated_theta in theta_entries:
                normalized_rows.append({
                    **base,
                    'greek': 'theta',
                    'greek_label': 'Theta',
                    'bucket_type': 'Instrument',
                    'risk_bucket': theta_asset,
                    'instrument': theta_asset,
                    'unit': theta_unit_info['native_unit'],
                    'maturity_bucket': theta_maturity,
                    'maturity_pair': theta_maturity,
                    'exposure': allocated_theta,
                })
        else:
            theta_maturity = _format_maturity_pair(
                row,
                aggregation,
                month_through,
                quarter_through,
                cob_date,
            )
            normalized_rows.append({
                **base,
                'greek': 'theta',
                'greek_label': 'Theta',
                'bucket_type': 'Pair',
                'risk_bucket': pair,
                'instrument': 'N/A',
                'unit': _normalize_unit(row.get('unit_quantity')),
                'maturity_bucket': theta_maturity,
                'maturity_pair': theta_maturity,
                'exposure': theta_value,
            })

        if len(underlying_sides) == 2:
            corr_value = _safe_number(row.get('qty_corr_sensitivity'))
            corr_maturity = _format_maturity_pair(
                row,
                aggregation,
                month_through,
                quarter_through,
                cob_date,
            )
            normalized_rows.append({
                **base,
                'greek': 'correlation',
                'greek_label': 'Correlation',
                'bucket_type': 'Pair',
                'risk_bucket': pair,
                'instrument': 'N/A',
                'unit': _normalize_unit(row.get('unit_quantity')),
                'maturity_bucket': corr_maturity,
                'maturity_pair': corr_maturity,
                'exposure': corr_value,
            })

    return pd.DataFrame(normalized_rows)


def normalize_aspect_contributions(
    data,
    aggregation='mixed',
    unit_mode='native',
    month_through=None,
    quarter_through=None,
    cob_date=None,
):
    if data.empty:
        return pd.DataFrame()

    unit_map = fetch_instrument_unit_map()
    normalized_rows = []
    data = data.reset_index(drop=True)

    for source_row_id, row in data.iterrows():
        instrument = str(row.get('instrument') or '').strip()
        if not instrument:
            continue

        unit_info = _instrument_unit_info(instrument, unit_map, row.get('uoM'))
        display_unit = unit_info['unit']
        display_value = _safe_number(row.get('qty')) * unit_info['conv_factor']
        if unit_mode == 'lots':
            display_value = display_value / _lot_divisor(display_unit)

        maturity_entries = _expand_instrument_maturity(
            row.get('maturityForwardDate'),
            'month',
            display_value,
            aggregation,
            month_through,
            quarter_through,
            cob_date,
        )
        base = {
            'source_row_id': f'aspect-{source_row_id}',
            'source': ASPECT_SOURCE_LABEL,
            'cob_date': _format_cob_option(cob_date),
            'strategy': row.get('strategy') or 'N/A',
            'trade_type': row.get('entityType') or 'N/A',
            'option_type': row.get('dealType') or 'Aspect exposure',
            'put_call': 'N/A',
            'currency': None,
            'asset_pair': instrument,
            'quantity': _safe_number(row.get('qty')),
            'value': 0.0,
            'pnl': 0.0,
        }
        for maturity_bucket, allocated_value in maturity_entries:
            normalized_rows.append({
                **base,
                'greek': 'delta',
                'greek_label': 'Delta',
                'bucket_type': 'Instrument',
                'risk_bucket': instrument,
                'instrument': instrument,
                'unit': display_unit,
                'maturity_bucket': maturity_bucket,
                'maturity_pair': maturity_bucket,
                'exposure': allocated_value,
            })

    return pd.DataFrame(normalized_rows)


def _split_bucket_filter(values):
    instruments = set()
    pairs = set()
    for value in values or []:
        if isinstance(value, str) and value.startswith('instrument::'):
            instruments.add(value.split('::', 1)[1])
        elif isinstance(value, str) and value.startswith('pair::'):
            pairs.add(value.split('::', 1)[1])
    return instruments, pairs


def filter_normalized_rows(rows, bucket_values):
    if rows.empty or not bucket_values:
        return rows

    instruments, pairs = _split_bucket_filter(bucket_values)
    mask = pd.Series(False, index=rows.index)
    if instruments:
        mask = mask | ((rows['bucket_type'] == 'Instrument') & rows['risk_bucket'].isin(instruments))
    if pairs:
        mask = mask | ((rows['bucket_type'] == 'Pair') & rows['risk_bucket'].isin(pairs))
    return rows[mask].copy()


def _add_greek_columns(grouped, include_zero_columns=True):
    for greek_key in GREEK_KEYS:
        greek_col = GREEK_DEFINITIONS[greek_key]["label"]
        if greek_col not in grouped.columns:
            grouped[greek_col] = 0.0

    ordered_columns = []
    for greek_key in GREEK_KEYS:
        greek_col = GREEK_DEFINITIONS[greek_key]['label']
        if include_zero_columns or grouped[greek_col].abs().sum():
            ordered_columns.append(greek_col)
    return grouped, ordered_columns


def create_summary_df(rows):
    if rows.empty:
        return pd.DataFrame()

    bucket_grouped = (
        rows.groupby(['bucket_type', 'risk_bucket', 'unit', 'greek'], dropna=False)
        .agg(net=('exposure', 'sum'))
        .reset_index()
    )
    bucket_pivot = bucket_grouped.pivot_table(
        index=['bucket_type', 'risk_bucket', 'unit'],
        columns='greek',
        values='net',
        aggfunc='sum',
        fill_value=0,
    )
    bucket_pivot.columns = [GREEK_DEFINITIONS[greek]["label"] for greek in bucket_pivot.columns]
    bucket_pivot = bucket_pivot.reset_index().rename(
        columns={'bucket_type': 'Bucket Type', 'risk_bucket': 'Risk Bucket', 'unit': 'Unit'}
    )
    bucket_pivot, greek_columns = _add_greek_columns(bucket_pivot)
    bucket_pivot['_sort_type'] = bucket_pivot['Bucket Type'].map({'Instrument': 0, 'Pair': 1}).fillna(9)
    bucket_pivot['_sort_abs'] = bucket_pivot[greek_columns].abs().sum(axis=1)
    bucket_pivot = bucket_pivot.sort_values(['_sort_type', '_sort_abs', 'Risk Bucket'], ascending=[True, False, True])
    bucket_pivot = bucket_pivot.drop(columns=['_sort_type', '_sort_abs'])
    bucket_pivot = bucket_pivot[['Bucket Type', 'Risk Bucket', 'Unit'] + greek_columns]

    unit_rows = create_unit_aggregate_df(rows)
    if not unit_rows.empty:
        unit_rows.insert(0, 'Risk Bucket', '')
        unit_rows.insert(0, 'Bucket Type', 'Unit')
        unit_rows = unit_rows[['Bucket Type', 'Risk Bucket', 'Unit'] + greek_columns]
        bucket_pivot = concat_dataframes([bucket_pivot, unit_rows], ignore_index=True)

    return bucket_pivot


def create_unit_aggregate_df(rows):
    if rows.empty:
        return pd.DataFrame()

    grouped = (
        rows.groupby(['unit', 'greek'], dropna=False)
        .agg(net=('exposure', 'sum'))
        .reset_index()
    )
    pivot = grouped.pivot_table(
        index=['unit'],
        columns='greek',
        values='net',
        aggfunc='sum',
        fill_value=0,
    )
    pivot.columns = [GREEK_DEFINITIONS[greek]["label"] for greek in pivot.columns]
    pivot = pivot.reset_index().rename(columns={'unit': 'Unit'})
    pivot, greek_columns = _add_greek_columns(pivot)
    pivot['_sort_abs'] = pivot[greek_columns].abs().sum(axis=1)
    pivot = pivot.sort_values(['_sort_abs', 'Unit'], ascending=[False, True]).drop(columns=['_sort_abs'])
    return pivot[['Unit'] + greek_columns]


def create_ladder_df(rows, greek_key):
    if rows.empty:
        return pd.DataFrame()

    greek_rows = rows[rows['greek'] == greek_key].copy()
    if greek_rows.empty:
        return pd.DataFrame()

    greek_rows['ladder_bucket'] = greek_rows['risk_bucket'].map(_compact_pair_label)
    column_name = 'ladder_bucket'

    index_name = 'Maturity'
    net = greek_rows.pivot_table(
        index='maturity_bucket',
        columns=column_name,
        values='exposure',
        aggfunc='sum',
        fill_value=0,
    )

    net = net.reset_index().rename(columns={'maturity_bucket': index_name})
    net = net.loc[sorted(net.index, key=lambda index: _maturity_sort_key(net.at[index, index_name]))].reset_index(drop=True)

    numeric_columns = [column for column in net.columns if column != index_name]
    if _should_add_theta_total(greek_rows, greek_key):
        net['Total'] = net[numeric_columns].sum(axis=1)
        numeric_columns.append('Total')

    total_row = {index_name: 'Total', '_row_type': 'total'}
    for column in numeric_columns:
        total_row[column] = net[column].sum()

    net['_row_type'] = 'normal'
    net = concat_dataframes([net, pd.DataFrame([total_row])], ignore_index=True)

    total_columns = [column for column in numeric_columns if column == 'Total']
    exposure_columns = [column for column in numeric_columns if column != 'Total']
    ordered_columns = [index_name] + sorted(exposure_columns, key=_ladder_column_sort_key) + total_columns + ['_row_type']
    return net[ordered_columns]


def _single_unit_label(rows):
    units = sorted({str(value) for value in rows['unit'] if value is not None and not pd.isna(value) and str(value).strip()})
    if len(units) == 1:
        return units[0]
    return None


def _should_add_theta_total(greek_rows, greek_key):
    return greek_key == 'theta' and _single_unit_label(greek_rows) is not None


def _format_unit_header(values):
    units = sorted({str(value) for value in values if value is not None and not pd.isna(value) and str(value).strip()})
    if not units:
        return ''
    if len(units) == 1:
        return units[0]
    return 'Mixed'


def create_ladder_unit_headers(rows, greek_key):
    if rows.empty:
        return {}

    greek_rows = rows[rows['greek'] == greek_key].copy()
    if greek_rows.empty:
        return {}

    greek_rows['ladder_bucket'] = greek_rows['risk_bucket'].map(_compact_pair_label)
    unit_headers = {
        bucket: _format_unit_header(group['unit'])
        for bucket, group in greek_rows.groupby('ladder_bucket', dropna=False)
    }
    unit_headers['Maturity'] = ''
    theta_unit = _single_unit_label(greek_rows)
    if greek_key == 'theta' and theta_unit is not None:
        unit_headers['Total'] = theta_unit
    return unit_headers


def create_unit_ladder_df(rows, greek_key):
    if rows.empty:
        return pd.DataFrame()

    greek_rows = rows[rows['greek'] == greek_key].copy()
    if greek_rows.empty:
        return pd.DataFrame()

    index_name = 'Maturity'
    net = greek_rows.pivot_table(
        index='maturity_bucket',
        columns='unit',
        values='exposure',
        aggfunc='sum',
        fill_value=0,
    )

    net = net.reset_index().rename(columns={'maturity_bucket': index_name})
    net = net.loc[sorted(net.index, key=lambda index: _maturity_sort_key(net.at[index, index_name]))].reset_index(drop=True)

    numeric_columns = [column for column in net.columns if column != index_name]
    if _should_add_theta_total(greek_rows, greek_key):
        net['Total'] = net[numeric_columns].sum(axis=1)
        numeric_columns.append('Total')

    total_row = {index_name: 'Total', '_row_type': 'total'}
    for column in numeric_columns:
        total_row[column] = net[column].sum()

    net['_row_type'] = 'normal'
    net = concat_dataframes([net, pd.DataFrame([total_row])], ignore_index=True)

    total_columns = [column for column in numeric_columns if column == 'Total']
    exposure_columns = [column for column in numeric_columns if column != 'Total']
    ordered_columns = [index_name] + sorted(exposure_columns) + total_columns + ['_row_type']
    return net[ordered_columns]


def _bucket_greek_column_labels(bucket_type, available_greeks=None):
    greek_keys = (
        ['theta', 'correlation']
        if bucket_type == 'Pair'
        else ['delta', 'gamma', 'vega', 'theta']
    )
    if available_greeks is not None:
        available_greeks = set(available_greeks)
        greek_keys = [greek_key for greek_key in greek_keys if greek_key in available_greeks]
    return [GREEK_DEFINITIONS[greek_key]['label'] for greek_key in greek_keys]


def create_bucket_greek_tables(rows):
    if rows.empty:
        return []

    working = rows.copy()
    working['display_bucket'] = working['risk_bucket'].map(_compact_pair_label)

    grouped = (
        working.groupby(['bucket_type', 'display_bucket', 'unit', 'maturity_bucket', 'greek'], dropna=False)
        .agg(net=('exposure', 'sum'))
        .reset_index()
    )
    sort_source = (
        grouped.groupby(['bucket_type', 'display_bucket', 'unit'], dropna=False)['net']
        .apply(lambda values: values.abs().sum())
        .reset_index(name='abs_total')
    )
    sort_source['_sort_type'] = sort_source['bucket_type'].map({'Instrument': 0, 'Pair': 1}).fillna(9)
    sort_source = sort_source.sort_values(
        ['_sort_type', 'abs_total', 'display_bucket', 'unit'],
        ascending=[True, False, True, True],
    )

    bucket_tables = []
    for index, bucket in sort_source.reset_index(drop=True).iterrows():
        bucket_filter = (
            (grouped['bucket_type'] == bucket['bucket_type'])
            & (grouped['display_bucket'] == bucket['display_bucket'])
        )
        if pd.isna(bucket['unit']):
            bucket_filter = bucket_filter & grouped['unit'].isna()
        else:
            bucket_filter = bucket_filter & (grouped['unit'] == bucket['unit'])
        bucket_rows = grouped[bucket_filter]
        if bucket_rows.empty:
            continue

        table = bucket_rows.pivot_table(
            index='maturity_bucket',
            columns='greek',
            values='net',
            aggfunc='sum',
            fill_value=0,
        )
        table.columns = [GREEK_DEFINITIONS[greek]['label'] for greek in table.columns]
        table = table.reset_index().rename(columns={'maturity_bucket': 'Maturity'})
        table = table.loc[
            sorted(table.index, key=lambda row_index: _maturity_sort_key(table.at[row_index, 'Maturity']))
        ].reset_index(drop=True)
        greek_columns = _bucket_greek_column_labels(
            bucket['bucket_type'],
            bucket_rows['greek'],
        )
        for column in greek_columns:
            if column not in table.columns:
                table[column] = 0.0

        total_row = {'Maturity': 'Total', '_row_type': 'total'}
        for column in greek_columns:
            total_row[column] = table[column].sum()

        table['_row_type'] = 'normal'
        table = concat_dataframes([table, pd.DataFrame([total_row])], ignore_index=True)
        unit_label = str(bucket['unit']) if bucket['unit'] is not None and not pd.isna(bucket['unit']) else ''
        bucket_type_label = (
            'ASSET' if bucket['bucket_type'] == 'Instrument' else 'PAIR'
        )
        bucket_label = f"{bucket_type_label}: {bucket['display_bucket']}"
        title = f'{bucket_label} ({unit_label})' if unit_label else bucket_label
        bucket_tables.append({
            'id': f'bucket-greeks-{index}',
            'title': title,
            'bucket_type': bucket['bucket_type'],
            'risk_bucket': str(bucket['display_bucket']),
            'unit': unit_label,
            'table': table[['Maturity'] + greek_columns + ['_row_type']],
        })

    return bucket_tables


def create_bucket_greek_export_df(bucket_tables):
    if not bucket_tables:
        return pd.DataFrame()

    frames = []
    for bucket_table in bucket_tables:
        table = bucket_table['table'].drop(columns=['_row_type'], errors='ignore').copy()
        table.insert(0, 'Unit', bucket_table.get('unit', ''))
        table.insert(0, 'Risk Bucket', bucket_table.get('risk_bucket', ''))
        table.insert(0, 'Bucket Type', bucket_table.get('bucket_type', ''))
        frames.append(table)

    return concat_dataframes(frames, ignore_index=True)


def create_raw_rows_df(rows):
    if rows.empty:
        return pd.DataFrame()

    columns = [
        'cob_date',
        'source_row_id',
        'strategy',
        'trade_type',
        'option_type',
        'currency',
        'greek_label',
        'bucket_type',
        'risk_bucket',
        'unit',
        'maturity_bucket',
        'asset_pair',
        'exposure',
        'quantity',
        'value',
        'pnl',
    ]
    return rows[columns].rename(
        columns={
            'cob_date': 'COB Date',
            'source_row_id': 'Source Row',
            'strategy': 'Strategy',
            'trade_type': 'Trade Type',
            'option_type': 'Option Type',
            'currency': 'Currency',
            'greek_label': 'Greek',
            'bucket_type': 'Bucket Type',
            'risk_bucket': 'Risk Bucket',
            'unit': 'Unit',
            'maturity_bucket': 'Maturity',
            'asset_pair': 'Asset Pair',
            'exposure': 'Exposure',
            'quantity': 'Quantity',
            'value': 'Value',
            'pnl': 'P&L',
        }
    )


def build_display_tables(rows):
    bucket_greek_tables = create_bucket_greek_tables(rows)
    return {
        'summary': create_summary_df(rows),
        'bucket_greek_tables': bucket_greek_tables,
        **{f'{greek_key}_ladder': create_ladder_df(rows, greek_key) for greek_key in GREEK_KEYS},
        **{f'{greek_key}_ladder_units': create_ladder_unit_headers(rows, greek_key) for greek_key in GREEK_KEYS},
        **{f'{greek_key}_unit_ladder': create_unit_ladder_df(rows, greek_key) for greek_key in GREEK_KEYS},
    }


def build_output_tables(rows):
    tables = build_display_tables(rows)
    return {
        **tables,
        'unit_aggregate': create_unit_aggregate_df(rows),
        'bucket_greek_export': create_bucket_greek_export_df(tables['bucket_greek_tables']),
        'raw': create_raw_rows_df(rows),
    }


def create_aspect_summary_df(rows):
    if rows.empty:
        return pd.DataFrame()

    working = rows.copy()
    working['Instrument'] = working['risk_bucket'].map(_display_asset_label)
    summary = (
        working.groupby(
            ['strategy', 'trade_type', 'Instrument', 'unit'],
            dropna=False,
        )
        .agg(Delta=('exposure', 'sum'))
        .reset_index()
        .rename(
            columns={
                'strategy': 'Strategy',
                'trade_type': 'Trade Type',
                'unit': 'Unit',
            }
        )
    )
    summary['_sort_abs'] = summary['Delta'].abs()
    summary = summary.sort_values(
        ['_sort_abs', 'Strategy', 'Instrument'],
        ascending=[False, True, True],
    ).drop(columns=['_sort_abs'])
    return summary[['Strategy', 'Trade Type', 'Instrument', 'Unit', 'Delta']]


def create_aspect_raw_export_df(rows):
    if rows.empty:
        return pd.DataFrame()
    return rows[ASPECT_EXPORT_COLUMNS].rename(
        columns={
            'instrument': 'Instrument',
            'qty': 'Quantity',
            'uoM': 'Source Unit',
            'maturityForwardDate': 'Maturity',
            'strategy': 'Strategy',
            'dealType': 'Deal Type',
            'entityType': 'Entity Type',
            'exchangeTradeOtc': 'Exchange/OTC',
        }
    )


def _sanitize_excel_text(frame):
    safe = frame.copy()
    for column in safe.select_dtypes(include=['object', 'string']).columns:
        safe[column] = safe[column].map(
            lambda value: (
                f"'{value}"
                if isinstance(value, str) and value.startswith(('=', '+', '-', '@'))
                else value
            )
        )
    return safe


def _aspect_status_text(meta):
    status = meta.get('status')
    if status != 'ok':
        return meta.get('message') or 'Aspect exposure is unavailable.'

    mode_label = 'Live' if meta.get('mode') == 'live' else 'Settlement'
    message = (
        f"{mode_label} Aspect exposure | COB {meta.get('requested_cob_date')} | "
        f"{meta.get('accepted_rows', 0):,} accepted rows | "
        f"{meta.get('instruments', 0):,} instruments | "
        f"book {meta.get('book') or ASPECT_DEFAULT_BOOK}"
    )
    rejected_rows = int(meta.get('rejected_rows') or 0)
    if rejected_rows:
        message += f' | {rejected_rows:,} invalid rows excluded'
    return message


def build_aspect_overlay_tables(rows):
    if rows.empty:
        return html.Div(
            'Load an Aspect settlement or live snapshot to view the delta overlay.',
            className='greeks-empty-state',
        )

    summary = create_aspect_summary_df(rows)
    delta_ladder = create_ladder_df(rows, 'delta')
    delta_unit_ladder = create_unit_ladder_df(rows, 'delta')
    unit_headers = create_ladder_unit_headers(rows, 'delta')

    return html.Div([
        html.Div([
            html.Div('By Strategy and Instrument', className='greeks-ladder-subtitle'),
            build_compact_table(
                'greeks-aspect-summary-table',
                summary,
                fit_to_content=True,
                grid_class_suffix=' greeks-aspect-grid',
            ),
        ], className='greeks-aspect-table-panel'),
        html.Div([
            html.Div('By Maturity and Instrument', className='greeks-ladder-subtitle'),
            build_compact_table(
                'greeks-aspect-delta-ladder-table',
                delta_ladder,
                fit_to_content=True,
                grid_class_suffix=' greeks-aspect-grid',
                unit_headers=unit_headers,
            ),
        ], className='greeks-aspect-table-panel'),
        html.Div([
            html.Div('By Maturity and Unit', className='greeks-ladder-subtitle'),
            build_compact_table(
                'greeks-aspect-unit-ladder-table',
                delta_unit_ladder,
                fit_to_content=True,
                grid_class_suffix=' greeks-aspect-grid',
            ),
        ], className='greeks-aspect-table-panel'),
    ], className='greeks-aspect-table-grid')


QUANTITY_GREEK_DECIMAL_PLACES = 6
QUANTITY_GREEK_NUMBER_FORMAT = '#,##0.000000'
MONETARY_NUMBER_FORMAT = '#,##0.00'
MONETARY_EXPORT_COLUMNS = {'Value', 'P&L'}
INTEGER_EXPORT_COLUMNS = {'Source Row'}


def _round_numeric(df):
    if df.empty:
        return df
    rounded = df.copy()
    for column in rounded.select_dtypes(include='number').columns:
        rounded[column] = rounded[column].round(
            QUANTITY_GREEK_DECIMAL_PLACES
        )
    return rounded


def _format_grid_number(value):
    numeric_value = _safe_number(value)
    if numeric_value == 0:
        return None
    return f'{numeric_value:,.{QUANTITY_GREEK_DECIMAL_PLACES}f}'


def _format_grid_records(df, numeric_columns):
    formatted = []
    formatted = []
    for record in df.to_dict('records'):
        clean = {}
        for key, value in record.items():
            if value is None or pd.isna(value):
                clean[key] = None
            elif key in numeric_columns:
                raw_key = f'__raw_{key}'
                clean[raw_key] = _safe_number(value)
                clean[key] = _format_grid_number(value)
            else:
                clean[key] = value
        formatted.append(clean)
    return formatted


def _clamp_width(value, minimum, maximum):
    return int(max(minimum, min(maximum, value)))


def _estimate_content_width(df, column, numeric_columns):
    header_length = len(str(column))
    values = df[column].dropna().tolist() if column in df.columns else []

    if column in numeric_columns:
        display_lengths = [len(_format_grid_number(value) or '') for value in values]
        content_length = max([header_length, *display_lengths], default=header_length)
        return _clamp_width(content_length * 8 + 24, 72, 230)

    display_lengths = [len(str(value)) for value in values]
    content_length = max([header_length, *display_lengths], default=header_length)
    if column == 'Maturity':
        return _clamp_width(content_length * 8 + 28, 88, 122)
    if column == 'Risk Bucket':
        return _clamp_width(content_length * 7 + 36, 140, 260)
    if column == 'Bucket Type':
        return _clamp_width(content_length * 7 + 28, 96, 124)
    if column == 'Unit':
        return _clamp_width(content_length * 7 + 28, 72, 90)
    return _clamp_width(content_length * 7 + 28, 84, 220)


def _with_unit_header(column_def, unit_headers, column):
    if unit_headers is None or column not in unit_headers:
        return column_def

    return {
        'headerName': unit_headers.get(column) or '',
        'headerClass': 'mckinsey-ag-grid-header greeks-unit-group-header',
        'children': [column_def],
    }


def _ag_grid_column_defs(df, fit_to_content=False, unit_headers=None):
    column_defs = []
    numeric_columns = set(df.select_dtypes(include='number').columns.tolist())
    text_columns = {'Bucket Type', 'Risk Bucket', 'Unit', 'Maturity'}
    text_widths = {
        'Bucket Type': {'minWidth': 92, 'flex': 1.0},
        'Risk Bucket': {'minWidth': 170, 'flex': 1.8},
        'Unit': {'minWidth': 74, 'flex': 0.7},
        'Maturity': {'minWidth': 112, 'flex': 1.0},
    }

    for column in df.columns:
        if column == '_row_type':
            continue

        if column in numeric_columns:
            raw_field = f'__raw_{column}'
            column_def = {
                'headerName': column,
                'field': column,
                'type': 'rightAligned',
                'sortable': True,
                'filter': False,
                'resizable': True,
                'minWidth': 64,
                'cellClass': 'mckinsey-ag-grid-cell mckinsey-ag-grid-number-cell greeks-number-cell',
                'headerClass': 'mckinsey-ag-grid-header greeks-number-header',
                'headerTooltip': column,
                'cellClassRules': {
                    'greeks-positive-cell': f"Number(params.data['{raw_field}']) > 0",
                    'greeks-negative-cell': f"Number(params.data['{raw_field}']) < 0",
                },
            }
            if fit_to_content:
                column_def['width'] = _estimate_content_width(df, column, numeric_columns)
            else:
                column_def['flex'] = 1
            column_defs.append(_with_unit_header(column_def, unit_headers, column))
        else:
            column_def = {
                'headerName': column,
                'field': column,
                'sortable': True,
                'filter': False,
                'resizable': True,
                'minWidth': text_widths.get(column, {}).get('minWidth', 84 if column in text_columns else 72),
                'cellClass': 'mckinsey-ag-grid-cell mckinsey-ag-grid-text-cell greeks-text-cell',
                'headerClass': 'mckinsey-ag-grid-header greeks-text-header',
                'headerTooltip': column,
                'tooltipField': column,
            }
            if fit_to_content:
                column_def['width'] = _estimate_content_width(df, column, numeric_columns)
            else:
                column_def['flex'] = text_widths.get(column, {}).get('flex', 1.3 if column in text_columns else 1)
            column_defs.append(_with_unit_header(column_def, unit_headers, column))

    return column_defs


def _column_width_sum(column_defs):
    total = 0
    for column in column_defs:
        if 'children' in column:
            total += _column_width_sum(column['children'])
        else:
            total += int(column.get('width') or column.get('minWidth') or 0)
    return total


def build_compact_table(table_id, df, max_height=None, fit_to_content=False, grid_class_suffix='', unit_headers=None):
    del max_height
    if df.empty:
        return html.Div('No data for the current selection.', className='greeks-empty-state')

    display_df = _round_numeric(df)
    numeric_columns = display_df.select_dtypes(include='number').columns.tolist()
    records = _format_grid_records(display_df, numeric_columns)
    column_defs = _ag_grid_column_defs(display_df, fit_to_content=fit_to_content, unit_headers=unit_headers)
    content_width = _column_width_sum(column_defs) + 28
    grid_style = (
        {'width': f'{content_width}px', 'maxWidth': '100%', 'height': 'auto'}
        if fit_to_content
        else {'width': '100%', 'height': 'auto'}
    )

    return dag.AgGrid(
        id=table_id,
        rowData=records,
        columnDefs=column_defs,
        defaultColDef={
            'wrapHeaderText': False,
            'autoHeaderHeight': False,
            'suppressHeaderMenuButton': True,
            'suppressHeaderFilterButton': True,
            'resizable': True,
        },
        dashGridOptions={
            'domLayout': 'autoHeight',
            'rowHeight': 30,
            'headerHeight': 32,
            'groupHeaderHeight': 28,
            'pagination': False,
            'suppressPaginationPanel': True,
            'enableCellTextSelection': True,
            'ensureDomOrder': True,
            'animateRows': False,
            'alwaysShowHorizontalScroll': False,
            'alwaysShowVerticalScroll': False,
            'suppressHorizontalScroll': True,
        },
        rowClassRules={
            'greeks-total-row': "params.data && params.data._row_type === 'total'",
            'greeks-unit-row': "params.data && params.data['Bucket Type'] === 'Unit'",
        },
        className=(
            'ag-theme-alpine mckinsey-ag-grid supply-dest-summary-grid greeks-ag-grid'
            + (' greeks-content-fit-grid' if fit_to_content else '')
            + grid_class_suffix
        ),
        style=grid_style,
        columnSize=None if fit_to_content else 'responsiveSizeToFit',
        columnSizeOptions={
            'defaultMinWidth': 58,
            'columnLimits': [
                {'key': 'Risk Bucket', 'minWidth': 170},
                {'key': 'Maturity', 'minWidth': 112},
                {'key': 'Bucket Type', 'minWidth': 92},
                {'key': 'Unit', 'minWidth': 74},
            ],
        },
        dangerously_allow_code=True,
    )


def build_bucket_greek_sections(tables):
    bucket_tables = tables.get('bucket_greek_tables', [])
    if not bucket_tables:
        return html.Div('No data for the current selection.', className='greeks-empty-state')

    return html.Div([
        html.Div([
            html.Div(bucket_table['title'], className='greeks-bucket-greek-title'),
            build_compact_table(
                f"greeks-{bucket_table['id']}-table",
                bucket_table['table'],
                fit_to_content=True,
                grid_class_suffix=' greeks-bucket-greek-grid',
            ),
        ], className='greeks-bucket-greek-panel')
        for bucket_table in bucket_tables
    ], className='greeks-bucket-greek-grid-wrap')


def build_ladder_sections(tables):
    sections = []
    for greek_key in GREEK_KEYS:
        greek_label = GREEK_DEFINITIONS[greek_key]['label']
        exposure_table = build_compact_table(
            f'greeks-{greek_key}-ladder-table',
            tables.get(f'{greek_key}_ladder', pd.DataFrame()),
            fit_to_content=True,
            grid_class_suffix=' greeks-ladder-grid',
            unit_headers=tables.get(f'{greek_key}_ladder_units', {}),
        )
        unit_exposure_table = build_compact_table(
            f'greeks-{greek_key}-unit-ladder-table',
            tables.get(f'{greek_key}_unit_ladder', pd.DataFrame()),
            fit_to_content=True,
            grid_class_suffix=' greeks-ladder-grid greeks-unit-exposure-grid',
        )
        sections.append(
            html.Div([
                html.Div([
                    html.H3(f'{greek_label} Ladder', className='section-title-inline greeks-monitor-title'),
                ], className='inline-section-header supply-dest-section-header greeks-monitor-section-header'),
                html.Div([
                    html.Div([
                        html.Div('By Bucket', className='greeks-ladder-subtitle'),
                        exposure_table,
                    ], className='supply-dest-table-container greeks-monitor-table-wrap greeks-ladder-table-panel'),
                    html.Div([
                        html.Div('By Unit', className='greeks-ladder-subtitle'),
                        unit_exposure_table,
                    ], className='supply-dest-table-container greeks-monitor-table-wrap greeks-unit-ladder-table-panel'),
                ], className='greeks-ladder-table-pair'),
            ], className='main-section-container supply-dest-section greeks-monitor-section')
        )
    return sections


def _format_number(value):
    value = _safe_number(value)
    return f'{value:,.0f}'


def _format_currency(value):
    value = _safe_number(value)
    sign = '-' if value < 0 else ''
    return (
        f'{sign}${abs(value):,.{QUANTITY_GREEK_DECIMAL_PLACES}f}'
    )


def _apply_export_number_formats(worksheet, frame):
    numeric_columns = set(
        frame.select_dtypes(include='number').columns.tolist()
    )
    for column_index, column in enumerate(frame.columns, start=1):
        if column not in numeric_columns:
            continue
        if column in MONETARY_EXPORT_COLUMNS:
            number_format = MONETARY_NUMBER_FORMAT
        elif column in INTEGER_EXPORT_COLUMNS:
            number_format = '#,##0'
        else:
            number_format = QUANTITY_GREEK_NUMBER_FORMAT
        for row_index in range(2, len(frame) + 2):
            worksheet.cell(
                row=row_index,
                column=column_index,
            ).number_format = number_format


def _theta_kpi_class(value):
    value = _safe_number(value)
    if value < 0:
        return 'greeks-kpi-value greeks-kpi-value-negative'
    if value > 0:
        return 'greeks-kpi-value greeks-kpi-value-positive'
    return 'greeks-kpi-value greeks-kpi-value-neutral'


def build_kpi_strip(rows, meta):
    if rows.empty:
        return html.Div([
            html.Div([
                html.Div('COB Date', className='greeks-kpi-label'),
                html.Div(meta.get('cob_date') or '-', className='greeks-kpi-value'),
            ], className='greeks-kpi-card'),
        ], className='greeks-kpi-strip')

    theta_per_day = rows.loc[rows['greek'] == 'theta', 'exposure'].sum()

    return html.Div([
        html.Div([
            html.Div('COB Date', className='greeks-kpi-label'),
            html.Div(meta.get('cob_date') or '-', className='greeks-kpi-value'),
        ], className='greeks-kpi-card'),
        html.Div([
            html.Div('Strategies', className='greeks-kpi-label'),
            html.Div(_format_number(meta.get('strategies', 0)), className='greeks-kpi-value'),
        ], className='greeks-kpi-card'),
        html.Div([
            html.Div('Daily Theta P&L', className='greeks-kpi-label'),
            html.Div(_format_currency(theta_per_day), className=_theta_kpi_class(theta_per_day)),
        ], className='greeks-kpi-card greeks-theta-kpi-card'),
    ], className='greeks-kpi-strip')


def _preserve_or_default(current_values, available_values):
    available_set = set(available_values)
    preserved = [value for value in (current_values or []) if value in available_set]
    return preserved if preserved else available_values


def _option_values(options):
    return {option['value'] for option in options}


layout = html.Div([
    dcc.Store(id='refresh-trigger-store', storage_type='memory'),
    dcc.Store(id='greeks-unfiltered-normalized-store', storage_type='memory'),
    dcc.Store(id='greeks-normalized-store', storage_type='memory'),
    dcc.Store(id='greeks-aspect-store', storage_type='memory'),
    dcc.Download(id='download-greeks-monitor-workbook'),

    html.Div([
        html.Div([
            html.Div([
                html.Label('COB Date', className='inline-filter-label'),
                dcc.Dropdown(
                    id='date-selector',
                    options=[],
                    value=None,
                    clearable=False,
                    className='inline-dropdown-date',
                ),
            ], className='greeks-monitor-control-group greeks-control-date'),
            html.Div([
                html.Label('Aggregation', className='inline-filter-label'),
                dcc.RadioItems(
                    id='maturity-aggregation-mode-selector',
                    options=MATURITY_AGGREGATION_OPTIONS,
                    value='mixed',
                    inline=True,
                    className='supply-dest-view-selector exporters-sticky-selector greeks-aggregation-mode-selector',
                    inputClassName='greeks-aggregation-mode-input',
                    labelClassName='greeks-aggregation-mode-option',
                    inputStyle={'display': 'none'},
                    labelStyle={'marginRight': '0'},
                ),
            ], className='greeks-monitor-control-group greeks-control-aggregation'),
            html.Div([
                html.Label('Monthly Through', className='inline-filter-label'),
                dcc.Dropdown(
                    id='month-through-selector',
                    options=[],
                    value=None,
                    clearable=False,
                    className='greeks-inline-dropdown-cutoff',
                ),
            ], className='greeks-monitor-control-group greeks-control-month-through'),
            html.Div([
                html.Label('Quarterly Through', className='inline-filter-label'),
                dcc.Dropdown(
                    id='quarter-through-selector',
                    options=[],
                    value=None,
                    clearable=False,
                    className='greeks-inline-dropdown-cutoff',
                ),
            ], className='greeks-monitor-control-group greeks-control-quarter-through'),
            html.Div([
                html.Label('Unit Mode', className='inline-filter-label'),
                dcc.RadioItems(
                    id='unit-mode-selector',
                    options=UNIT_MODE_OPTIONS,
                    value='native',
                    inline=True,
                    className='supply-dest-view-selector exporters-sticky-selector greeks-unit-mode-selector',
                    inputClassName='greeks-unit-mode-input',
                    labelClassName='greeks-unit-mode-option',
                    inputStyle={'display': 'none'},
                    labelStyle={'marginRight': '0'},
                ),
            ], className='greeks-monitor-control-group greeks-control-unit-mode'),
            html.Div([
                html.Button(
                    'Load Aspect COB',
                    id='greeks-aspect-settlement-btn',
                    className='greeks-aspect-button greeks-aspect-button-secondary',
                    title='Load the selected COB settlement exposure from Aspect',
                ),
                html.Button(
                    'Load Aspect Live',
                    id='greeks-aspect-live-btn',
                    className='greeks-aspect-button greeks-aspect-button-live',
                    title='Load today’s live exposure from Aspect',
                ),
                html.Button('Export Workbook', id='export-greeks-workbook-btn', className='inline-button-primary'),
            ], className='greeks-monitor-actions'),
        ], className='greeks-monitor-control-row'),
        html.Div([
            html.Div([
                html.Label('Strategies', className='inline-filter-label'),
                dcc.Dropdown(
                    id='strategy-selector',
                    options=[],
                    value=[],
                    multi=True,
                    placeholder='Select strategies',
                    className='greeks-inline-dropdown-multi',
                ),
            ], className='greeks-monitor-control-group greeks-control-strategies'),
            html.Div([
                html.Label('Trade Types', className='inline-filter-label'),
                dcc.Dropdown(
                    id='trade-type-selector',
                    options=[],
                    value=[],
                    multi=True,
                    placeholder='Select trade types',
                    className='greeks-inline-dropdown-trade-type',
                ),
            ], className='greeks-monitor-control-group greeks-control-trade-types'),
            html.Div([
                html.Label('Asset / Pair', className='inline-filter-label'),
                dcc.Dropdown(
                    id='risk-bucket-selector',
                    options=[],
                    value=[],
                    multi=True,
                    placeholder='Select instruments and pairs',
                    className='greeks-inline-dropdown-multi',
                ),
            ], className='greeks-monitor-control-group greeks-control-risk-buckets'),
        ], className='greeks-monitor-control-row'),
    ], className='professional-section-header greeks-sticky-filter-bar greeks-monitor-controls'),

    dcc.Loading(
        id='greeks-monitor-loading',
        type='circle',
        children=[
            html.Div(id='greeks-kpi-strip'),
            html.Div([
                dcc.Loading(
                    id='greeks-aspect-loading',
                    type='circle',
                    children=html.Div([
                        html.Div([
                            html.H3('Aspect Delta Overlay', className='section-title-inline greeks-monitor-title'),
                        ], className='inline-section-header supply-dest-section-header greeks-monitor-section-header'),
                        html.Div(
                            id='greeks-aspect-status',
                            className='greeks-aspect-status greeks-aspect-status-idle',
                            role='status',
                            **{'aria-live': 'polite'},
                        ),
                        html.Div(
                            id='greeks-aspect-table-container',
                            className='supply-dest-table-container greeks-monitor-table-wrap',
                        ),
                    ], className='main-section-container supply-dest-section greeks-monitor-section greeks-aspect-section'),
                ),
                html.Div([
                    html.Div([
                        html.H3('Maturity by ASSET/PAIR', className='section-title-inline greeks-monitor-title'),
                    ], className='inline-section-header supply-dest-section-header greeks-monitor-section-header'),
                    html.Div(id='greeks-bucket-greek-tables-container', className='supply-dest-table-container greeks-monitor-table-wrap'),
                ], className='main-section-container supply-dest-section greeks-monitor-section'),
                html.Div(id='greeks-ladder-sections', className='greeks-monitor-grid'),
            ], className='greeks-monitor-grid'),
        ],
    ),
], className='options-dashboard-container greeks-page greeks-monitor-page')


@callback(
    Output('refresh-trigger-store', 'data'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=True,
)
def trigger_data_refresh(n_clicks):
    if n_clicks:
        _fetch_options_data_cached.cache_clear()
        fetch_instrument_unit_map.cache_clear()
        _clear_greeks_server_cache()
        return {'timestamp': pd.Timestamp.now().isoformat(), 'refresh_count': n_clicks}
    return dash.no_update


@callback(
    Output('greeks-aspect-store', 'data'),
    Input('greeks-aspect-settlement-btn', 'n_clicks'),
    Input('greeks-aspect-live-btn', 'n_clicks'),
    State('date-selector', 'value'),
    prevent_initial_call=True,
    running=[
        (Output('greeks-aspect-settlement-btn', 'disabled'), True, False),
        (Output('greeks-aspect-live-btn', 'disabled'), True, False),
    ],
)
def load_aspect_exposure(settlement_clicks, live_clicks, selected_date):
    del settlement_clicks, live_clicks
    triggered_id = dash.ctx.triggered_id
    mode = 'live' if triggered_id == 'greeks-aspect-live-btn' else 'settlement'
    try:
        payload = _load_aspect_payload(mode, selected_date)
    except (AspectConfigurationError, AspectSourceError) as exc:
        logger.warning('Aspect exposure load failed: %s', exc)
        return _empty_aspect_store(str(exc), status='error')
    except Exception:
        logger.exception('Unexpected Aspect exposure load failure')
        return _empty_aspect_store(
            'Aspect exposure could not be loaded because of an unexpected source error.',
            status='error',
        )

    meta = payload.get('meta', {})
    return _cache_greeks_payload(
        payload,
        'aspect',
        [
            meta.get('mode'),
            meta.get('requested_cob_date'),
            meta.get('fetched_at'),
        ],
    )


@callback(
    Output('date-selector', 'options'),
    Output('date-selector', 'value'),
    Input('refresh-trigger-store', 'data'),
    State('date-selector', 'value'),
)
def update_date_options(refresh_trigger, selected_date):
    del refresh_trigger
    dates = get_available_dates()
    options = [{'label': date, 'value': date} for date in dates]
    resolved_date = selected_date if selected_date in dates else (dates[0] if dates else None)
    return options, resolved_date


@callback(
    Output('strategy-selector', 'options'),
    Output('strategy-selector', 'value'),
    Output('trade-type-selector', 'options'),
    Output('trade-type-selector', 'value'),
    Output('risk-bucket-selector', 'options'),
    Output('risk-bucket-selector', 'value'),
    Input('date-selector', 'value'),
    Input('refresh-trigger-store', 'data'),
    State('strategy-selector', 'value'),
    State('trade-type-selector', 'value'),
    State('risk-bucket-selector', 'value'),
)
def update_filter_options(selected_date, refresh_trigger, selected_strategies, selected_trade_types, selected_buckets):
    del refresh_trigger
    strategies, trade_types, bucket_options = fetch_filter_values(selected_date)
    bucket_values = [option['value'] for option in bucket_options]

    return (
        [{'label': strategy, 'value': strategy} for strategy in strategies],
        _preserve_or_default(selected_strategies, strategies),
        [{'label': trade_type, 'value': trade_type} for trade_type in trade_types],
        _preserve_or_default(selected_trade_types, trade_types),
        bucket_options,
        _preserve_or_default(selected_buckets, bucket_values),
    )


@callback(
    Output('month-through-selector', 'options'),
    Output('month-through-selector', 'value'),
    Output('quarter-through-selector', 'options'),
    Output('quarter-through-selector', 'value'),
    Output('month-through-selector', 'disabled'),
    Output('quarter-through-selector', 'disabled'),
    Input('date-selector', 'value'),
    Input('maturity-aggregation-mode-selector', 'value'),
    Input('refresh-trigger-store', 'data'),
    State('month-through-selector', 'value'),
    State('quarter-through-selector', 'value'),
)
def update_maturity_cutoff_controls(
    selected_date,
    aggregation_mode,
    refresh_trigger,
    selected_month_through,
    selected_quarter_through,
):
    del refresh_trigger
    month_options, default_month, quarter_options, default_quarter = build_maturity_cutoff_options(selected_date)
    if not month_options or not quarter_options:
        return month_options, default_month, quarter_options, default_quarter, True, True

    triggered_id = dash.callback_context.triggered[0]['prop_id'].split('.')[0] if dash.callback_context.triggered else None
    reset_to_default = triggered_id in {None, 'date-selector', 'refresh-trigger-store'}

    month_values = _option_values(month_options)
    quarter_values = _option_values(quarter_options)
    month_through = (
        default_month
        if reset_to_default or selected_month_through not in month_values
        else selected_month_through
    )
    quarter_through = (
        default_quarter
        if reset_to_default or selected_quarter_through not in quarter_values
        else selected_quarter_through
    )

    month_cutoff, quarter_cutoff = _resolve_maturity_cutoffs(selected_date, month_through, quarter_through)
    resolved_quarter_through = _format_cutoff_value(quarter_cutoff)
    if resolved_quarter_through != quarter_through:
        quarter_through = resolved_quarter_through
        if quarter_through not in quarter_values:
            quarter_options.append({
                'label': _quarter_through_label(quarter_through),
                'value': quarter_through,
            })

    controls_disabled = aggregation_mode != 'mixed'
    return month_options, month_through, quarter_options, quarter_through, controls_disabled, controls_disabled


def _build_unfiltered_greeks_store(
    selected_date,
    aggregation,
    month_through,
    quarter_through,
    unit_mode,
):
    if not selected_date:
        return _empty_store('No COB date selected')

    data = fetch_options_data(selected_date)
    if data.empty:
        return _empty_store('No option Greeks found')

    month_cutoff, quarter_cutoff = _resolve_maturity_cutoffs(selected_date, month_through, quarter_through)
    normalized = normalize_greek_contributions(
        data,
        aggregation or 'mixed',
        unit_mode,
        _format_cutoff_value(month_cutoff),
        _format_cutoff_value(quarter_cutoff),
        selected_date,
    )

    if normalized.empty:
        return _empty_store('No option Greeks found')

    meta = {
        'message': 'OK',
        'cob_date': selected_date,
        'raw_rows': int(data.shape[0]),
        'normalized_rows': int(normalized.shape[0]),
        'strategies': int(data['substrategy'].nunique()),
        'trade_types': int(data['type_trade'].nunique()),
        'units': sorted(normalized['unit'].dropna().unique().tolist()),
        'aggregation': aggregation or 'mixed',
        'month_through': _format_cutoff_value(month_cutoff),
        'quarter_through': _format_cutoff_value(quarter_cutoff),
        'unit_mode': unit_mode,
    }

    return {'meta': meta, 'rows': normalized.to_dict('records')}


def _filter_greeks_store(base_store, selected_strategies, selected_trade_types, selected_buckets):
    if not base_store:
        return _empty_store('No COB date selected')

    base_message = (base_store or {}).get('meta', {}).get('message')
    if base_message == 'No COB date selected':
        return base_store
    if selected_strategies == []:
        return _empty_store('No strategies selected')
    if selected_trade_types == []:
        return _empty_store('No trade types selected')
    if selected_buckets == []:
        return _empty_store('No instruments or pairs selected')

    if not base_store or not base_store.get('rows'):
        return _empty_store(base_message or 'No data available')

    rows = pd.DataFrame(base_store['rows'])
    if rows.empty:
        return _empty_store(base_message or 'No data available')

    if selected_strategies:
        rows = rows[rows['strategy'].astype(str).isin(selected_strategies)]
    if selected_trade_types:
        rows = rows[rows['trade_type'].astype(str).isin(selected_trade_types)]

    if rows.empty:
        return _empty_store('No rows match selected strategy/trade-type filters')

    source_order = rows['source_row_id'].drop_duplicates().tolist()
    source_id_map = {source_row_id: dense_id for dense_id, source_row_id in enumerate(source_order)}
    rows = rows.copy()
    rows['source_row_id'] = rows['source_row_id'].map(source_id_map).astype(int)
    strategy_trade_rows = rows

    normalized = filter_normalized_rows(strategy_trade_rows, selected_buckets)
    if normalized.empty:
        return _empty_store('No Greek rows match selected risk buckets')

    normalized = normalized.sort_values(['greek', 'bucket_type', 'risk_bucket', 'maturity_bucket']).reset_index(drop=True)
    base_meta = base_store.get('meta', {})

    meta = {
        'message': 'OK',
        'cob_date': base_meta.get('cob_date'),
        'raw_rows': int(len(source_order)),
        'normalized_rows': int(normalized.shape[0]),
        'strategies': int(strategy_trade_rows['strategy'].replace('N/A', pd.NA).nunique()),
        'trade_types': int(strategy_trade_rows['trade_type'].replace('N/A', pd.NA).nunique()),
        'units': sorted(normalized['unit'].dropna().unique().tolist()),
        'aggregation': base_meta.get('aggregation') or 'mixed',
        'month_through': base_meta.get('month_through'),
        'quarter_through': base_meta.get('quarter_through'),
        'unit_mode': base_meta.get('unit_mode'),
    }

    return {'meta': meta, 'rows': normalized.to_dict('records')}


@callback(
    Output('greeks-unfiltered-normalized-store', 'data'),
    Input('date-selector', 'value'),
    Input('maturity-aggregation-mode-selector', 'value'),
    Input('month-through-selector', 'value'),
    Input('quarter-through-selector', 'value'),
    Input('unit-mode-selector', 'value'),
    Input('refresh-trigger-store', 'data'),
)
def update_unfiltered_greeks_store(
    selected_date,
    aggregation,
    month_through,
    quarter_through,
    unit_mode,
    refresh_trigger,
):
    del refresh_trigger
    payload = _build_unfiltered_greeks_store(
        selected_date,
        aggregation,
        month_through,
        quarter_through,
        unit_mode,
    )
    return _cache_greeks_payload(
        payload,
        'unfiltered',
        [selected_date, aggregation, month_through, quarter_through, unit_mode],
    )


@callback(
    Output('greeks-normalized-store', 'data'),
    Input('greeks-unfiltered-normalized-store', 'data'),
    Input('strategy-selector', 'value'),
    Input('trade-type-selector', 'value'),
    Input('risk-bucket-selector', 'value'),
)
def update_filtered_greeks_store(base_store, selected_strategies, selected_trade_types, selected_buckets):
    base_payload = _resolve_greeks_payload(base_store)
    filtered_payload = _filter_greeks_store(
        base_payload,
        selected_strategies,
        selected_trade_types,
        selected_buckets,
    )
    return _cache_greeks_payload(
        filtered_payload,
        'filtered',
        [
            (base_store or {}).get('snapshot_id'),
            sorted(selected_strategies or []),
            sorted(selected_trade_types or []),
            sorted(selected_buckets or []),
        ],
    )


@callback(
    Output('greeks-aspect-status', 'children'),
    Output('greeks-aspect-status', 'className'),
    Output('greeks-aspect-table-container', 'children'),
    Input('greeks-aspect-store', 'data'),
    Input('maturity-aggregation-mode-selector', 'value'),
    Input('month-through-selector', 'value'),
    Input('quarter-through-selector', 'value'),
    Input('unit-mode-selector', 'value'),
)
def update_aspect_overlay(
    aspect_store,
    aggregation,
    month_through,
    quarter_through,
    unit_mode,
):
    payload = _resolve_aspect_payload(aspect_store)
    meta = payload.get('meta', {})
    status = meta.get('status') or 'idle'
    status_class = f'greeks-aspect-status greeks-aspect-status-{status}'

    if status != 'ok':
        empty = html.Div(
            'The option Greeks tables below remain available and are not changed.',
            className='greeks-empty-state',
        )
        return _aspect_status_text(meta), status_class, empty

    raw_rows = pd.DataFrame(payload.get('rows', []))
    if raw_rows.empty:
        empty = html.Div(
            'Aspect returned no exposure rows for this snapshot.',
            className='greeks-empty-state',
        )
        return _aspect_status_text(meta), status_class, empty

    normalized = normalize_aspect_contributions(
        raw_rows,
        aggregation or 'mixed',
        unit_mode or 'native',
        month_through,
        quarter_through,
        meta.get('requested_cob_date'),
    )
    return (
        _aspect_status_text(meta),
        status_class,
        build_aspect_overlay_tables(normalized),
    )


@callback(
    Output('greeks-kpi-strip', 'children'),
    Output('greeks-bucket-greek-tables-container', 'children'),
    Output('greeks-ladder-sections', 'children'),
    Input('greeks-normalized-store', 'data'),
)
def update_monitor_tables(store_data):
    payload = _resolve_greeks_payload(store_data)
    if not payload.get('rows'):
        empty = html.Div('No data for the current selection.', className='greeks-empty-state')
        return empty, empty, []

    rows = pd.DataFrame(payload['rows'])
    meta = payload.get('meta', {})
    tables = build_display_tables(rows)

    return (
        build_kpi_strip(rows, meta),
        build_bucket_greek_sections(tables),
        build_ladder_sections(tables),
    )


@callback(
    Output('download-greeks-monitor-workbook', 'data'),
    Input('export-greeks-workbook-btn', 'n_clicks'),
    State('greeks-normalized-store', 'data'),
    State('greeks-aspect-store', 'data'),
    State('maturity-aggregation-mode-selector', 'value'),
    State('month-through-selector', 'value'),
    State('quarter-through-selector', 'value'),
    State('unit-mode-selector', 'value'),
    prevent_initial_call=True,
)
def export_greeks_workbook(
    n_clicks,
    store_data,
    aspect_store,
    aggregation,
    month_through,
    quarter_through,
    unit_mode,
):
    payload = _resolve_greeks_payload(store_data)
    aspect_payload = _resolve_aspect_payload(aspect_store)
    if not n_clicks or (not payload.get('rows') and not aspect_payload.get('rows')):
        return dash.no_update

    rows = pd.DataFrame(payload.get('rows', []))
    tables = build_output_tables(rows) if not rows.empty else {}
    cob_date = (
        payload.get('meta', {}).get('cob_date')
        or aspect_payload.get('meta', {}).get('requested_cob_date')
        or pd.Timestamp.now().strftime('%Y-%m-%d')
    )

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        sheet_map = {}
        if tables:
            sheet_map.update({
                'Summary': tables['summary'],
                'Bucket Greeks': tables['bucket_greek_export'],
                'Delta Ladder': tables['delta_ladder'],
                'Delta Unit Ladder': tables['delta_unit_ladder'],
                'Gamma Ladder': tables['gamma_ladder'],
                'Gamma Unit Ladder': tables['gamma_unit_ladder'],
                'Vega Ladder': tables['vega_ladder'],
                'Vega Unit Ladder': tables['vega_unit_ladder'],
                'Theta Ladder': tables['theta_ladder'],
                'Theta Unit Ladder': tables['theta_unit_ladder'],
                'Correlation Ladder': tables['correlation_ladder'],
                'Corr Unit Ladder': tables['correlation_unit_ladder'],
                'Raw Normalized': tables['raw'],
            })

        aspect_meta = aspect_payload.get('meta', {})
        aspect_raw = pd.DataFrame(aspect_payload.get('rows', []))
        if aspect_meta.get('status') == 'ok' and not aspect_raw.empty:
            aspect_normalized = normalize_aspect_contributions(
                aspect_raw,
                aggregation or 'mixed',
                unit_mode or 'native',
                month_through,
                quarter_through,
                aspect_meta.get('requested_cob_date'),
            )
            aspect_metadata = pd.DataFrame([
                {'Field': 'Source', 'Value': aspect_meta.get('source')},
                {'Field': 'Mode', 'Value': aspect_meta.get('mode')},
                {'Field': 'COB Date', 'Value': aspect_meta.get('requested_cob_date')},
                {'Field': 'Fetched At', 'Value': aspect_meta.get('fetched_at')},
                {'Field': 'Book', 'Value': aspect_meta.get('book')},
                {'Field': 'Source Rows', 'Value': aspect_meta.get('source_rows')},
                {'Field': 'Accepted Rows', 'Value': aspect_meta.get('accepted_rows')},
                {'Field': 'Rejected Rows', 'Value': aspect_meta.get('rejected_rows')},
            ])
            sheet_map.update({
                'Aspect Metadata': _sanitize_excel_text(aspect_metadata),
                'Aspect Summary': _sanitize_excel_text(create_aspect_summary_df(aspect_normalized)),
                'Aspect Delta': create_ladder_df(aspect_normalized, 'delta'),
                'Aspect Unit Delta': create_unit_ladder_df(aspect_normalized, 'delta'),
                'Aspect Raw': _sanitize_excel_text(create_aspect_raw_export_df(aspect_raw)),
            })

        for sheet_name, table in sheet_map.items():
            export_df = table.drop(columns=['_row_type'], errors='ignore')
            export_sheet_name = sheet_name[:31]
            export_df.to_excel(
                writer,
                sheet_name=export_sheet_name,
                index=False,
            )
            if sheet_name != 'Aspect Metadata':
                _apply_export_number_formats(
                    writer.sheets[export_sheet_name],
                    export_df,
                )

    output.seek(0)
    return dcc.send_bytes(output.getvalue(), f'current_greeks_monitor_{cob_date}.xlsx')
