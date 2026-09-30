"""Shared curve sources for history analytics and recent underlying-price monitoring.

The history loader excludes spot and nonpositive prices. The monitoring loader
retains spot and signed prices, its TFM/TFU labels, and the latest five source COBs.
Their data contracts and refresh caches deliberately remain distinct.
"""

from __future__ import annotations

import datetime
import threading
from functools import lru_cache

import numpy as np
import pandas as pd
from sqlalchemy import text

from db_fallback import DB_SCHEMA, fq_table, read_with_fallback, sql_literal


FORWARD_CURVE_PRODUCTS = {
    'JKM': {'code': 'ICE_JKM_MO', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'TTF': {'code': 'ICE_TTF', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'HH': {'code': 'ICE_HH', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'Brent': {'code': 'ICE_BRENT_FUTURES', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'NBP': {'code': 'ICE_UKD', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
}
CODE_TO_PRODUCT = {settings['code']: product for product, settings in FORWARD_CURVE_PRODUCTS.items()}


def _sql_in_literal(values):
    return ', '.join(sql_literal(value) for value in values)


def normalize_forward_curves(frame):
    columns = [
        'trade_date',
        'product',
        'maturity_date',
        'expiration_date',
        'price',
        'code',
        'currency',
        'units',
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)

    normalized = frame.copy()
    normalized['trade_date'] = pd.to_datetime(normalized['COB'], errors='coerce')
    normalized['maturity_date'] = pd.to_datetime(
        normalized['contract'].astype(str).str.strip(),
        format='%YM%m',
        errors='coerce',
    )
    normalized['expiration_date'] = pd.to_datetime(normalized['expiry'], errors='coerce')
    normalized['price'] = pd.to_numeric(normalized['value'], errors='coerce')
    normalized['product'] = normalized['code'].map(CODE_TO_PRODUCT)
    normalized = normalized.dropna(subset=['trade_date', 'maturity_date', 'price', 'product'])
    normalized = normalized[normalized['price'] > 0]
    return normalized[columns].sort_values(
        ['trade_date', 'product', 'maturity_date']
    ).reset_index(drop=True)


@lru_cache(maxsize=32)
def _load_forward_curves_cached(start_date, end_date, products):
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    selected_products = tuple(product for product in products if product in FORWARD_CURVE_PRODUCTS)
    if not selected_products:
        return normalize_forward_curves(pd.DataFrame())

    codes = [FORWARD_CURVE_PRODUCTS[product]['code'] for product in selected_products]
    categories = sorted({FORWARD_CURVE_PRODUCTS[product]['category'] for product in selected_products})
    versions = sorted({FORWARD_CURVE_PRODUCTS[product]['version_name'] for product in selected_products})
    trino_query = f'''
        SELECT code,
               ondate AS COB,
               currency,
               units,
               forward_curve_tenors_expiry AS expiry,
               forward_curve_tenors_absolute AS contract,
               forward_curve_tenors_value AS value
        FROM enverus.curve
        WHERE code IN ({_sql_in_literal(codes)})
          AND category IN ({_sql_in_literal(categories)})
          AND version_name IN ({_sql_in_literal(versions)})
          AND ondate_index >= {int(start.strftime('%Y%m%d'))}
          AND ondate_index <= {int(end.strftime('%Y%m%d'))}
          AND forward_curve_tenors_absolute NOT IN ('M-1','M-2','M-3','SPOT')
          AND forward_curve_tenors_value IS NOT NULL
        ORDER BY ondate, code, forward_curve_tenors_tenor
    '''
    postgres_query = text(
        f'''
        SELECT code,
               cob AS "COB",
               currency,
               units,
               expiry,
               contract,
               value::double precision AS value
        FROM {fq_table(DB_SCHEMA, 'curve')}
        WHERE code = ANY(:codes)
          AND cob >= :start_date
          AND cob <= :end_date
          AND contract NOT IN ('M-1','M-2','M-3','SPOT')
          AND value IS NOT NULL
        ORDER BY cob, code, expiry
        '''
    )
    raw = read_with_fallback(
        trino_query,
        postgres_query,
        catalog='transformed',
        schema='enverus',
        postgres_params={'codes': codes, 'start_date': start, 'end_date': end},
        context_label='Shared forward curve load',
    )
    return normalize_forward_curves(raw)


def load_forward_curves(start_date, end_date, products=None, force=False):
    selected_products = tuple(sorted(products or FORWARD_CURVE_PRODUCTS))
    if force:
        _load_forward_curves_cached.cache_clear()
    return _load_forward_curves_cached(
        pd.Timestamp(start_date).strftime('%Y-%m-%d'),
        pd.Timestamp(end_date).strftime('%Y-%m-%d'),
        selected_products,
    ).copy()


def clear_forward_curve_cache():
    _load_forward_curves_cached.cache_clear()


# The monitoring view retains its TFM label and separate TFU source.
# Copy settings so changing one view's mapping cannot mutate the other.
ENVERUS_UNDERLYING_SOURCES = {
    'HH': dict(FORWARD_CURVE_PRODUCTS['HH']),
    'NBP': dict(FORWARD_CURVE_PRODUCTS['NBP']),
    'TFM': dict(FORWARD_CURVE_PRODUCTS['TTF']),
    'TFU': {'code': 'ICE_TFU_MO', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'Brent': dict(FORWARD_CURVE_PRODUCTS['Brent']),
    'JKM': dict(FORWARD_CURVE_PRODUCTS['JKM']),
}
ENVERUS_CODE_TO_PRODUCT = {
    source['code']: product
    for product, source in ENVERUS_UNDERLYING_SOURCES.items()
}


def normalize_underlying_prices(df):
    if df.empty:
        return pd.DataFrame(
            columns=[
                'trade_date',
                'hub',
                'product',
                'maturity_date',
                'expiration_date',
                'contract',
                'contract_type',
                'settlement_price',
                'code',
            ]
        )

    normalized = df.copy()
    normalized['trade_date'] = pd.to_datetime(normalized['COB'], errors='coerce')
    normalized['maturity_date'] = np.where(
        normalized['contract'].eq('SPOT'),
        normalized['trade_date'],
        pd.to_datetime(normalized['contract'], format='%YM%m', errors='coerce'),
    )
    normalized['expiration_date'] = pd.to_datetime(normalized['expiry'], errors='coerce')
    normalized['settlement_price'] = pd.to_numeric(normalized['value'], errors='coerce')
    normalized['product'] = normalized['code'].map(ENVERUS_CODE_TO_PRODUCT).fillna(normalized['code'])
    normalized['contract_type'] = None
    normalized['hub'] = None
    normalized['contract'] = normalized['product']

    normalized = normalized.dropna(subset=['trade_date', 'maturity_date', 'settlement_price'])
    return normalized[
        [
            'trade_date',
            'hub',
            'product',
            'maturity_date',
            'expiration_date',
            'contract',
            'contract_type',
            'settlement_price',
            'code',
        ]
    ].reset_index(drop=True)


def load_recent_underlying_prices(from_COB, to_COB):
    """Load only the five most recent curve COBs in the requested window."""
    postgres_from_cob = datetime.datetime.strptime(str(from_COB), "%Y%m%d").date()
    postgres_to_cob = datetime.datetime.strptime(str(to_COB), "%Y%m%d").date()
    codes = [source['code'] for source in ENVERUS_UNDERLYING_SOURCES.values()]
    categories = sorted({source['category'] for source in ENVERUS_UNDERLYING_SOURCES.values()})
    versions = sorted({source['version_name'] for source in ENVERUS_UNDERLYING_SOURCES.values()})

    trino_query = '''WITH selected_dates AS (
                        SELECT DISTINCT ondate_index
                        FROM enverus.curve
                        WHERE code IN ({})
                            AND category IN ({})
                            AND version_name IN ({})
                            AND ondate_index >= {}
                            AND ondate_index <= {}
                        ORDER BY ondate_index DESC
                        LIMIT 5
                    )
                    SELECT   code,
                        ondate AS COB,
                        currency,
                        units,
                        forward_curve_tenors_expiry AS expiry,
                        forward_curve_tenors_absolute AS contract,
                        forward_curve_tenors_value AS value
                        FROM enverus.curve
                        WHERE code IN ({})
                            AND category IN ({})
                            AND version_name IN ({})
                            AND ondate_index >= {}
                            AND ondate_index <= {}
                            AND ondate_index IN (SELECT ondate_index FROM selected_dates)
                            AND forward_curve_tenors_absolute NOT IN ('M-1','M-2','M-3')
                            AND forward_curve_tenors_value is not null
                        ORDER BY ondate, forward_curve_tenors_tenor
                            '''.format(
                                _sql_in_literal(codes),
                                _sql_in_literal(categories),
                                _sql_in_literal(versions),
                                int(from_COB),
                                int(to_COB),
                                _sql_in_literal(codes),
                                _sql_in_literal(categories),
                                _sql_in_literal(versions),
                                int(from_COB),
                                int(to_COB),
                            )
    postgres_query = text(
        f'''
        WITH selected_dates AS (
            SELECT DISTINCT cob
            FROM {fq_table(DB_SCHEMA, 'curve')}
            WHERE code = ANY(:codes)
              AND cob >= :from_cob
              AND cob <= :to_cob
            ORDER BY cob DESC
            LIMIT 5
        )
        SELECT  code,
                cob AS "COB",
                currency,
                units,
                expiry,
                contract,
                value::double precision AS value
        FROM {fq_table(DB_SCHEMA, 'curve')}
        WHERE code = ANY(:codes)
          AND cob >= :from_cob
          AND cob <= :to_cob
          AND cob IN (SELECT cob FROM selected_dates)
          AND contract NOT IN ('M-1','M-2','M-3')
          AND value IS NOT NULL
        ORDER BY cob, expiry
        '''
    )
    df_enverus = read_with_fallback(
        trino_query,
        postgres_query,
        catalog='transformed',
        schema='enverus',
        postgres_params={
            'codes': codes,
            'from_cob': postgres_from_cob,
            'to_cob': postgres_to_cob,
        },
        context_label='Underlying prices Enverus load',
    )

    return normalize_underlying_prices(df_enverus)


_underlying_prices = normalize_underlying_prices(pd.DataFrame())
_prices_data_loaded = False
_prices_data_refresh_key = None
_prices_data_lock = threading.Lock()


def ensure_underlying_prices(force=False, refresh_key=None):
    """Reuse the monitoring dataset; deduplicate reloads for one refresh event."""
    global _underlying_prices, _prices_data_loaded, _prices_data_refresh_key
    with _prices_data_lock:
        should_reload = not _prices_data_loaded
        if force:
            should_reload = refresh_key is None or refresh_key != _prices_data_refresh_key

        if should_reload:
            try:
                # Get recent dates for current underlying price monitoring.
                end_date = datetime.datetime.now()
                start_date = end_date - datetime.timedelta(days=30)
                _underlying_prices = load_recent_underlying_prices(
                    from_COB=start_date.strftime("%Y%m%d"),
                    to_COB=end_date.strftime("%Y%m%d"),
                )
            except Exception:
                _underlying_prices = normalize_underlying_prices(pd.DataFrame())
            _prices_data_loaded = True
            if force:
                _prices_data_refresh_key = refresh_key
    return _underlying_prices
