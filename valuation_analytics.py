"""Shared portfolio revaluation and P&L-attribution calculations.

The functions in this module deliberately stay independent from Dash so the
calculations can be unit-tested and reused by exports.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd
from sqlalchemy import text

from db_fallback import DB_SCHEMA, engine, fq_table
from options.options_library import kirk_model_with_substitution, kirk_spread_greeks


VALUATION_COLUMNS = [
    'cob_date',
    'trade_date',
    'entity',
    'book',
    'strategy',
    'substrategy',
    'type_trade',
    'type_option',
    'model',
    'put_call',
    'buy_sell',
    'premium',
    'expiration_date',
    'quantity',
    'unit_quantity',
    'strike',
    'asset_a',
    'asset_a_multiplier',
    'asset_a_premium',
    'maturity_date_type_a',
    'maturity_date_a',
    'asset_b',
    'asset_b_multiplier',
    'asset_b_premium',
    'maturity_date_type_b',
    'maturity_date_b',
    'adjusted_price_a',
    'adjusted_price_b',
    'adjusted_strike',
    'time_to_expiry',
    'adjusted_vol_a',
    'adjusted_vol_b',
    'correlation',
    'price',
    'delta_S1',
    'delta_S2',
    'gamma_S1',
    'gamma_S2',
    'gamma_S1S2',
    'vega_sigma1',
    'vega_sigma2',
    'corr_sensitivity',
    'theta',
    'qty_value',
    'qty_pnl',
]

DATE_COLUMNS = [
    'cob_date',
    'trade_date',
    'expiration_date',
    'maturity_date_a',
    'maturity_date_b',
]

NUMERIC_COLUMNS = [
    column
    for column in VALUATION_COLUMNS
    if column
    not in {
        *DATE_COLUMNS,
        'entity',
        'book',
        'strategy',
        'substrategy',
        'type_trade',
        'type_option',
        'model',
        'put_call',
        'buy_sell',
        'unit_quantity',
        'asset_a',
        'maturity_date_type_a',
        'asset_b',
        'maturity_date_type_b',
    }
]

POSITION_KEY_COLUMNS = [
    'trade_date',
    'entity',
    'book',
    'strategy',
    'substrategy',
    'type_trade',
    'type_option',
    'model',
    'put_call',
    'buy_sell',
    'premium',
    'expiration_date',
    'unit_quantity',
    'strike',
    'asset_a',
    'asset_a_multiplier',
    'asset_a_premium',
    'maturity_date_type_a',
    'maturity_date_a',
    'asset_b',
    'asset_b_multiplier',
    'asset_b_premium',
    'maturity_date_type_b',
    'maturity_date_b',
]

GROUPING_LABELS = {
    'portfolio': 'Portfolio',
    'substrategy': 'Strategy',
    'expiry_year': 'Expiry year',
    'asset_pair': 'Asset pair',
}


class ValuationDataError(RuntimeError):
    """Raised when valuation data cannot safely support an analytic."""


def _safe_float(value, default=np.nan):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if np.isfinite(number) else default


def _normalize_snapshot(frame):
    if frame is None or frame.empty:
        return pd.DataFrame(columns=VALUATION_COLUMNS)
    data = frame.copy()
    for column in DATE_COLUMNS:
        data[column] = pd.to_datetime(data[column], errors='coerce')
    for column in NUMERIC_COLUMNS:
        data[column] = pd.to_numeric(data[column], errors='coerce')
    return data


@lru_cache(maxsize=1)
def available_valuation_dates():
    query = text(
        f'''SELECT DISTINCT cob_date
            FROM {fq_table(DB_SCHEMA, 'trades_options_valuation')}
            ORDER BY cob_date DESC'''
    )
    try:
        dates = pd.read_sql(query, engine)
    except Exception as exc:
        raise ValuationDataError(f'Unable to load valuation dates: {exc}') from exc
    return tuple(pd.to_datetime(dates['cob_date'], errors='coerce').dropna().dt.strftime('%Y-%m-%d'))


@lru_cache(maxsize=8)
def _load_valuation_snapshot_cached(cob_date):
    selected_columns = ', '.join(f'"{column}"' for column in VALUATION_COLUMNS)
    query = text(
        f'''SELECT {selected_columns}
            FROM {fq_table(DB_SCHEMA, 'trades_options_valuation')}
            WHERE cob_date = :cob_date
            ORDER BY substrategy, expiration_date, asset_a, asset_b, put_call'''
    )
    try:
        frame = pd.read_sql(query, engine, params={'cob_date': pd.Timestamp(cob_date).date()})
    except Exception as exc:
        raise ValuationDataError(f'Unable to load valuation snapshot {cob_date}: {exc}') from exc
    return _normalize_snapshot(frame)


def load_valuation_snapshot(cob_date=None):
    dates = available_valuation_dates()
    resolved_date = cob_date or (dates[0] if dates else None)
    if not resolved_date:
        raise ValuationDataError('No valuation snapshots are available.')
    if resolved_date not in dates:
        raise ValuationDataError(f'Valuation snapshot {resolved_date} is not available.')
    return _load_valuation_snapshot_cached(resolved_date).copy()


def clear_valuation_analytics_cache():
    available_valuation_dates.cache_clear()
    _load_valuation_snapshot_cached.cache_clear()


def add_grouping_columns(frame):
    data = frame.copy()
    data['portfolio'] = 'Portfolio'
    data['expiry_year'] = pd.to_datetime(data['expiration_date'], errors='coerce').dt.year.astype('Int64').astype(str)
    data['expiry_year'] = data['expiry_year'].replace('<NA>', 'Unknown')
    data['asset_pair'] = data['asset_a'].fillna('N/A').astype(str) + ' / ' + data['asset_b'].fillna('N/A').astype(str)
    return data


def validate_kirk_rows(frame):
    data = frame.copy()
    reasons = pd.Series('', index=data.index, dtype=object)

    def mark(mask, message):
        nonlocal reasons
        reasons.loc[mask] = reasons.loc[mask].where(reasons.loc[mask].eq(''), reasons.loc[mask] + '; ') + message

    mark(data['model'].fillna('').str.lower().ne('kirk'), 'unsupported model')
    mark(data['type_option'].fillna('').str.lower().ne('spread'), 'unsupported option type')
    mark(~data['put_call'].fillna('').str.lower().isin(['call', 'put']), 'invalid call/put')
    mark(data['quantity'].isna(), 'missing quantity')
    mark(data['adjusted_price_a'].le(0) | data['adjusted_price_a'].isna(), 'invalid asset A price')
    mark((data['adjusted_price_b'] + data['adjusted_strike']).le(0), 'asset B plus strike is non-positive')
    mark(data['adjusted_vol_a'].le(0) | data['adjusted_vol_a'].isna(), 'invalid asset A volatility')
    mark(data['adjusted_vol_b'].le(0) | data['adjusted_vol_b'].isna(), 'invalid asset B volatility')
    mark(data['correlation'].lt(-1) | data['correlation'].gt(1) | data['correlation'].isna(), 'invalid correlation')
    mark(data['time_to_expiry'].le(0) | data['time_to_expiry'].isna(), 'expired or missing time')
    return reasons


def _kirk_value(row, price_a, price_b, vol_a, vol_b, correlation, time_to_expiry):
    return float(
        kirk_model_with_substitution(
            price_a,
            price_b,
            float(row.adjusted_strike),
            vol_a,
            vol_b,
            correlation,
            max(time_to_expiry, 1 / 365.25),
            str(row.put_call).lower(),
        )
    )


def run_portfolio_scenario(
    frame,
    *,
    price_a_pct=0.0,
    price_b_pct=0.0,
    vol_a_points=0.0,
    vol_b_points=0.0,
    correlation_points=0.0,
    rate_bps=0.0,
    business_days_forward=0,
):
    """Full Kirk revaluation plus a local-Greek approximation.

    ``correlation_points`` is entered as correlation percentage points; for
    example, 10 means rho moves from 0.25 to 0.35. The production Kirk price is
    undiscounted, so ``rate_bps`` is applied as a zero-base discount overlay.
    """

    data = add_grouping_columns(_normalize_snapshot(frame))
    data['validation_error'] = validate_kirk_rows(data)
    valid = data[data['validation_error'].eq('')].copy()
    invalid = data[~data['validation_error'].eq('')].copy()
    if valid.empty:
        return pd.DataFrame(), invalid

    price_a_move = _safe_float(price_a_pct, 0.0) / 100.0
    price_b_move = _safe_float(price_b_pct, 0.0) / 100.0
    vol_a_move = _safe_float(vol_a_points, 0.0) / 100.0
    vol_b_move = _safe_float(vol_b_points, 0.0) / 100.0
    correlation_move = _safe_float(correlation_points, 0.0) / 100.0
    rate_move = _safe_float(rate_bps, 0.0) / 10000.0
    days_forward = max(int(_safe_float(business_days_forward, 0.0)), 0)

    results = []
    for row in valid.itertuples(index=False):
        s1 = float(row.adjusted_price_a)
        s2 = float(row.adjusted_price_b)
        v1 = float(row.adjusted_vol_a)
        v2 = float(row.adjusted_vol_b)
        rho = float(row.correlation)
        time = float(row.time_to_expiry)
        quantity = float(row.quantity)

        shocked_s1 = max(s1 * (1 + price_a_move), 1e-8)
        shocked_s2 = max(s2 * (1 + price_b_move), 1e-8)
        shocked_v1 = max(v1 + vol_a_move, 1e-6)
        shocked_v2 = max(v2 + vol_b_move, 1e-6)
        shocked_rho = float(np.clip(rho + correlation_move, -0.9999, 0.9999))
        shocked_time = max(time - days_forward / 252.0, 1 / 365.25)

        base_price = _kirk_value(row, s1, s2, v1, v2, rho, time)
        undiscounted_shocked = _kirk_value(
            row,
            shocked_s1,
            shocked_s2,
            shocked_v1,
            shocked_v2,
            shocked_rho,
            shocked_time,
        )
        shocked_price = undiscounted_shocked * np.exp(-rate_move * shocked_time)
        exact_pnl = quantity * (shocked_price - base_price)

        greeks = kirk_spread_greeks(
            s1,
            s2,
            float(row.adjusted_strike),
            v1,
            v2,
            rho,
            time,
            str(row.put_call).lower(),
        )
        ds1 = shocked_s1 - s1
        ds2 = shocked_s2 - s2
        delta_pnl = quantity * (greeks['delta_S1'] * ds1 + greeks['delta_S2'] * ds2)
        gamma_pnl = quantity * (
            0.5 * greeks['gamma_S1'] * ds1**2
            + 0.5 * greeks['gamma_S2'] * ds2**2
            + greeks['gamma_S1S2'] * ds1 * ds2
        )
        vega_pnl = quantity * (
            greeks['vega_sigma1'] * _safe_float(vol_a_points, 0.0)
            + greeks['vega_sigma2'] * _safe_float(vol_b_points, 0.0)
        )
        correlation_pnl = quantity * greeks['corr_sensitivity'] * _safe_float(correlation_points, 0.0)
        theta_pnl = quantity * greeks['theta'] * days_forward
        rate_pnl = quantity * undiscounted_shocked * (np.exp(-rate_move * shocked_time) - 1)
        approximation = delta_pnl + gamma_pnl + vega_pnl + correlation_pnl + theta_pnl + rate_pnl

        results.append(
            {
                'substrategy': row.substrategy,
                'expiry_year': row.expiry_year,
                'asset_pair': row.asset_pair,
                'portfolio': 'Portfolio',
                'unit_quantity': row.unit_quantity,
                'expiration_date': row.expiration_date,
                'quantity': quantity,
                'base_price': base_price,
                'stored_price': _safe_float(row.price),
                'shocked_price': shocked_price,
                'base_value': quantity * base_price,
                'shocked_value': quantity * shocked_price,
                'exact_pnl': exact_pnl,
                'delta_pnl': delta_pnl,
                'gamma_pnl': gamma_pnl,
                'vega_pnl': vega_pnl,
                'correlation_pnl': correlation_pnl,
                'theta_pnl': theta_pnl,
                'rate_pnl': rate_pnl,
                'approximation_pnl': approximation,
                'interaction_residual': exact_pnl - approximation,
                'base_reconciliation': quantity * (_safe_float(row.price, base_price) - base_price),
            }
        )

    return pd.DataFrame(results), invalid


def aggregate_scenario(results, grouping):
    if results is None or results.empty:
        return pd.DataFrame()
    grouping = grouping if grouping in GROUPING_LABELS else 'substrategy'
    numeric = [
        'base_value',
        'shocked_value',
        'exact_pnl',
        'delta_pnl',
        'gamma_pnl',
        'vega_pnl',
        'correlation_pnl',
        'theta_pnl',
        'rate_pnl',
        'approximation_pnl',
        'interaction_residual',
        'base_reconciliation',
    ]
    return results.groupby(grouping, dropna=False, as_index=False)[numeric].sum().sort_values('exact_pnl')


def _aggregate_snapshot_positions(frame):
    data = _normalize_snapshot(frame)
    if data.empty:
        return data
    aggregation = {
        'quantity': 'sum',
        'qty_value': 'sum',
        'qty_pnl': 'sum',
        'adjusted_price_a': 'first',
        'adjusted_price_b': 'first',
        'adjusted_strike': 'first',
        'time_to_expiry': 'first',
        'adjusted_vol_a': 'first',
        'adjusted_vol_b': 'first',
        'correlation': 'first',
        'price': 'first',
        'delta_S1': 'first',
        'delta_S2': 'first',
        'gamma_S1': 'first',
        'gamma_S2': 'first',
        'gamma_S1S2': 'first',
        'vega_sigma1': 'first',
        'vega_sigma2': 'first',
        'corr_sensitivity': 'first',
        'theta': 'first',
        'cob_date': 'first',
    }
    return data.groupby(POSITION_KEY_COLUMNS, dropna=False, as_index=False).agg(aggregation)


def calculate_pnl_explain(previous, current):
    previous_positions = _aggregate_snapshot_positions(previous).add_suffix('_previous')
    current_positions = _aggregate_snapshot_positions(current).add_suffix('_current')
    left_keys = [f'{column}_previous' for column in POSITION_KEY_COLUMNS]
    right_keys = [f'{column}_current' for column in POSITION_KEY_COLUMNS]
    merged = previous_positions.merge(
        current_positions,
        how='outer',
        left_on=left_keys,
        right_on=right_keys,
        indicator=True,
    )

    for column in POSITION_KEY_COLUMNS:
        merged[column] = merged[f'{column}_current'].combine_first(merged[f'{column}_previous'])
    merged = add_grouping_columns(merged)
    numeric_pairs = [
        'quantity',
        'qty_value',
        'qty_pnl',
        'adjusted_price_a',
        'adjusted_price_b',
        'adjusted_vol_a',
        'adjusted_vol_b',
        'correlation',
        'price',
        'delta_S1',
        'delta_S2',
        'gamma_S1',
        'gamma_S2',
        'gamma_S1S2',
        'vega_sigma1',
        'vega_sigma2',
        'corr_sensitivity',
        'theta',
    ]
    for column in numeric_pairs:
        for suffix in ('previous', 'current'):
            merged[f'{column}_{suffix}'] = pd.to_numeric(merged[f'{column}_{suffix}'], errors='coerce')

    merged['actual_pnl'] = merged['qty_pnl_current'].fillna(0) - merged['qty_pnl_previous'].fillna(0)
    matched = merged['_merge'].eq('both')
    prior_quantity = merged['quantity_previous'].fillna(0)
    ds1 = merged['adjusted_price_a_current'] - merged['adjusted_price_a_previous']
    ds2 = merged['adjusted_price_b_current'] - merged['adjusted_price_b_previous']
    dv1_points = (merged['adjusted_vol_a_current'] - merged['adjusted_vol_a_previous']) * 100
    dv2_points = (merged['adjusted_vol_b_current'] - merged['adjusted_vol_b_previous']) * 100
    drho_points = (merged['correlation_current'] - merged['correlation_previous']) * 100
    merged['price_mark_changed'] = matched & (ds1.abs().gt(1e-10) | ds2.abs().gt(1e-10))
    merged['vol_mark_changed'] = matched & (dv1_points.abs().gt(1e-10) | dv2_points.abs().gt(1e-10))
    merged['correlation_mark_changed'] = matched & drho_points.abs().gt(1e-10)

    merged['delta_pnl'] = prior_quantity * (
        merged['delta_S1_previous'] * ds1 + merged['delta_S2_previous'] * ds2
    )
    merged['gamma_pnl'] = prior_quantity * (
        0.5 * merged['gamma_S1_previous'] * ds1**2
        + 0.5 * merged['gamma_S2_previous'] * ds2**2
        + merged['gamma_S1S2_previous'] * ds1 * ds2
    )
    merged['vega_pnl'] = prior_quantity * (
        merged['vega_sigma1_previous'] * dv1_points + merged['vega_sigma2_previous'] * dv2_points
    )
    merged['correlation_pnl'] = prior_quantity * merged['corr_sensitivity_previous'] * drho_points

    previous_cob = pd.to_datetime(merged['cob_date_previous'], errors='coerce').dropna()
    current_cob = pd.to_datetime(merged['cob_date_current'], errors='coerce').dropna()
    business_days = 0
    if not previous_cob.empty and not current_cob.empty:
        business_days = int(np.busday_count(previous_cob.iloc[0].date(), current_cob.iloc[0].date()))
    merged['theta_pnl'] = prior_quantity * merged['theta_previous'] * business_days

    for column in ['delta_pnl', 'gamma_pnl', 'vega_pnl', 'correlation_pnl', 'theta_pnl']:
        merged[column] = merged[column].where(matched, 0.0).fillna(0.0)

    quantity_change = merged['quantity_current'].fillna(0) - merged['quantity_previous'].fillna(0)
    merged['trade_pnl'] = np.select(
        [merged['_merge'].eq('right_only'), merged['_merge'].eq('left_only'), matched],
        [
            merged['qty_pnl_current'].fillna(0),
            -merged['qty_pnl_previous'].fillna(0),
            quantity_change * (merged['price_current'].fillna(0) + pd.to_numeric(merged['premium'], errors='coerce').fillna(0)),
        ],
        default=0.0,
    )
    explained_columns = ['delta_pnl', 'gamma_pnl', 'vega_pnl', 'correlation_pnl', 'theta_pnl', 'trade_pnl']
    merged['explained_pnl'] = merged[explained_columns].sum(axis=1)
    merged['unexplained_pnl'] = merged['actual_pnl'] - merged['explained_pnl']
    merged['position_status'] = merged['_merge'].map(
        {'both': 'Matched', 'left_only': 'Closed', 'right_only': 'New'}
    ).astype(str)
    merged['business_days'] = business_days
    return merged


def aggregate_pnl_explain(explain, grouping):
    if explain is None or explain.empty:
        return pd.DataFrame()
    grouping = grouping if grouping in GROUPING_LABELS else 'substrategy'
    numeric = [
        'actual_pnl',
        'delta_pnl',
        'gamma_pnl',
        'vega_pnl',
        'correlation_pnl',
        'theta_pnl',
        'trade_pnl',
        'explained_pnl',
        'unexplained_pnl',
    ]
    return explain.groupby(grouping, dropna=False, as_index=False)[numeric].sum().sort_values('actual_pnl')
