# pages/greeks.py
import configparser
import hashlib
import io
import json
import os
import threading
from collections import OrderedDict
from functools import lru_cache

import dash
import dash_ag_grid as dag
from dash import Input, Output, State, callback, dcc, html
import pandas as pd
from sqlalchemy import create_engine, text

from dataframe_utils import concat_dataframes


try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_dir = os.path.abspath(os.path.join(script_dir, '..', '..'))
    CONFIG_FILE_PATH = os.path.join(config_dir, 'config.ini')
except Exception:
    CONFIG_FILE_PATH = 'config.ini'

config_reader = configparser.ConfigParser(interpolation=None)
config_reader.read(CONFIG_FILE_PATH)

DB_CONNECTION_STRING = config_reader.get('DATABASE', 'CONNECTION_STRING', fallback=None)
DB_SCHEMA = config_reader.get('DATABASE', 'SCHEMA', fallback='at_lng')

if not DB_CONNECTION_STRING:
    raise ValueError(f"Missing DATABASE CONNECTION_STRING in {CONFIG_FILE_PATH}")

engine = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True)

VALUATION_TABLE = f'{DB_SCHEMA}.trades_options_valuation'
_GREEKS_SERVER_CACHE = OrderedDict()
_GREEKS_SERVER_CACHE_LOCK = threading.Lock()
_GREEKS_SERVER_CACHE_MAX_ENTRIES = 32
_GREEKS_SERVER_CACHE_GENERATION = 0

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
        'type': 'pair',
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


def _greeks_cache_key(namespace, parts):
    payload = json.dumps(
        {
            'namespace': namespace,
            'generation': _GREEKS_SERVER_CACHE_GENERATION,
            'parts': parts,
        },
        sort_keys=True,
        default=str,
        separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _cache_greeks_payload(payload, namespace, parts):
    cache_key = _greeks_cache_key(namespace, parts)
    with _GREEKS_SERVER_CACHE_LOCK:
        _GREEKS_SERVER_CACHE[cache_key] = payload
        _GREEKS_SERVER_CACHE.move_to_end(cache_key)
        while len(_GREEKS_SERVER_CACHE) > _GREEKS_SERVER_CACHE_MAX_ENTRIES:
            _GREEKS_SERVER_CACHE.popitem(last=False)
    return {'cache_key': cache_key, 'meta': payload.get('meta', {})}


def _resolve_greeks_payload(reference):
    if not reference:
        return _empty_store('No data available')
    if 'rows' in reference:
        return reference
    cache_key = reference.get('cache_key')
    with _GREEKS_SERVER_CACHE_LOCK:
        payload = _GREEKS_SERVER_CACHE.get(cache_key)
        if payload is not None:
            _GREEKS_SERVER_CACHE.move_to_end(cache_key)
    return payload if payload is not None else _empty_store('Server snapshot expired; refresh the page')


def _clear_greeks_server_cache():
    global _GREEKS_SERVER_CACHE_GENERATION
    with _GREEKS_SERVER_CACHE_LOCK:
        _GREEKS_SERVER_CACHE_GENERATION += 1
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
    if maturity_type == 'calendar':
        return f'{date_value.year}-CAL'
    return _format_date_bucket(date_value, aggregation, month_through, quarter_through, cob_date)


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


def _read_sql(query, params=None):
    return pd.read_sql(text(query), engine, params=params or {})


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
    pairs = sorted(
        {
            _standard_pair(row['asset_a'], row['asset_b'])
            for _, row in data[['asset_a', 'asset_b']].dropna(how='all').iterrows()
        }
    )

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
            SELECT a.instrument, b."Conv_factor", b."To_unit" AS unit
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
            'unit': _normalize_unit(row.get('unit')),
            'conv_factor': _safe_number(row.get('Conv_factor'), 1.0) or 1.0,
        }
    return unit_map


def _instrument_unit_info(asset, unit_map, fallback_unit):
    info = unit_map.get(str(asset), {})
    return {
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
        base = {
            'source_row_id': int(source_row_id),
            'cob_date': _format_cob_option(row.get('cob_date')),
            'strategy': row.get('substrategy') or 'N/A',
            'trade_type': row.get('type_trade') or 'N/A',
            'option_type': row.get('type_option') or 'N/A',
            'put_call': row.get('put_call') or 'N/A',
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
                display_unit = unit_info['unit']
                display_value = raw_value

                if greek_key in ['delta', 'gamma']:
                    display_value = display_value * unit_info['conv_factor']
                    if unit_mode == 'lots':
                        display_value = display_value / _lot_divisor(display_unit)

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
        theta_maturity = _format_maturity_pair(row, aggregation, month_through, quarter_through, cob_date)
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

        corr_value = _safe_number(row.get('qty_corr_sensitivity'))
        corr_maturity = _format_maturity_pair(row, aggregation, month_through, quarter_through, cob_date)
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


def _bucket_greek_column_labels(bucket_type):
    greek_keys = ['theta', 'correlation'] if bucket_type == 'Pair' else ['delta', 'gamma', 'vega']
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
        greek_columns = _bucket_greek_column_labels(bucket['bucket_type'])
        for column in greek_columns:
            if column not in table.columns:
                table[column] = 0.0

        total_row = {'Maturity': 'Total', '_row_type': 'total'}
        for column in greek_columns:
            total_row[column] = table[column].sum()

        table['_row_type'] = 'normal'
        table = concat_dataframes([table, pd.DataFrame([total_row])], ignore_index=True)
        unit_label = str(bucket['unit']) if bucket['unit'] is not None and not pd.isna(bucket['unit']) else ''
        title = f"{bucket['display_bucket']} ({unit_label})" if unit_label else str(bucket['display_bucket'])
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


def _round_numeric(df):
    if df.empty:
        return df
    rounded = df.copy()
    for column in rounded.select_dtypes(include='number').columns:
        rounded[column] = rounded[column].round(0)
    return rounded


def _format_grid_number(value):
    numeric_value = _safe_number(value)
    if numeric_value == 0:
        return None
    return f'{numeric_value:,.0f}'


def _format_grid_records(df, numeric_columns):
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
    return f'{sign}${abs(value):,.0f}'


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
                html.Div([
                    html.Div([
                        html.H3('Current Greeks Summary', className='section-title-inline greeks-monitor-title'),
                    ], className='inline-section-header supply-dest-section-header greeks-monitor-section-header'),
                    html.Div(id='greeks-summary-table-container', className='supply-dest-table-container greeks-monitor-table-wrap'),
                ], className='main-section-container supply-dest-section greeks-monitor-section'),
                html.Div([
                    html.Div([
                        html.H3('Maturity by Risk Bucket', className='section-title-inline greeks-monitor-title'),
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
            (base_store or {}).get('cache_key'),
            sorted(selected_strategies or []),
            sorted(selected_trade_types or []),
            sorted(selected_buckets or []),
        ],
    )


@callback(
    Output('greeks-kpi-strip', 'children'),
    Output('greeks-summary-table-container', 'children'),
    Output('greeks-bucket-greek-tables-container', 'children'),
    Output('greeks-ladder-sections', 'children'),
    Input('greeks-normalized-store', 'data'),
)
def update_monitor_tables(store_data):
    payload = _resolve_greeks_payload(store_data)
    if not payload.get('rows'):
        empty = html.Div('No data for the current selection.', className='greeks-empty-state')
        return empty, empty, empty, []

    rows = pd.DataFrame(payload['rows'])
    meta = payload.get('meta', {})
    tables = build_display_tables(rows)

    return (
        build_kpi_strip(rows, meta),
        build_compact_table('greeks-summary-table', tables['summary'], fit_to_content=True),
        build_bucket_greek_sections(tables),
        build_ladder_sections(tables),
    )


@callback(
    Output('download-greeks-monitor-workbook', 'data'),
    Input('export-greeks-workbook-btn', 'n_clicks'),
    State('greeks-normalized-store', 'data'),
    prevent_initial_call=True,
)
def export_greeks_workbook(n_clicks, store_data):
    payload = _resolve_greeks_payload(store_data)
    if not n_clicks or not payload.get('rows'):
        return dash.no_update

    rows = pd.DataFrame(payload['rows'])
    tables = build_output_tables(rows)
    cob_date = payload.get('meta', {}).get('cob_date') or pd.Timestamp.now().strftime('%Y-%m-%d')

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        sheet_map = {
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
        }
        for sheet_name, table in sheet_map.items():
            export_df = table.drop(columns=['_row_type'], errors='ignore')
            export_df.to_excel(writer, sheet_name=sheet_name[:31], index=False)

    output.seek(0)
    return dcc.send_bytes(output.getvalue(), f'current_greeks_monitor_{cob_date}.xlsx')
