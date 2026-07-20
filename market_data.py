"""Shared normalized forward-curve access for dashboard analytics."""

from __future__ import annotations

from functools import lru_cache

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
