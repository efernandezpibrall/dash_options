"""Dashboard data access for the shared options portfolio analytics."""
from functools import lru_cache
import pandas as pd
from sqlalchemy import text
from dash_options.core.database_reads import DB_SCHEMA, fq_table
from dash_options.core.config import get_database_engine
from options.portfolio import analytics as _implementation
from options.portfolio.analytics import (
    VALUATION_COLUMNS, POSITION_KEY_COLUMNS, GROUPING_LABELS, ValuationDataError,
    _normalize_snapshot, available_currencies, resolve_currency, select_currency,
    add_grouping_columns, validate_kirk_rows, aggregate_scenario, aggregate_pnl_explain,
)
from options.valuation.api import run_portfolio_scenario, calculate_pnl_explain


def __getattr__(name):
    return getattr(_implementation, name)


@lru_cache(maxsize=1)
def available_valuation_dates():
    query = text(
        f'''SELECT DISTINCT cob_date
            FROM {fq_table(DB_SCHEMA, 'trades_options_valuation_current')}
            ORDER BY cob_date DESC'''
    )
    try:
        dates = pd.read_sql(query, get_database_engine())
    except Exception as exc:
        raise ValuationDataError(f'Unable to load valuation dates: {exc}') from exc
    return tuple(pd.to_datetime(dates['cob_date'], errors='coerce').dropna().dt.strftime('%Y-%m-%d'))


@lru_cache(maxsize=8)
def _load_valuation_snapshot_cached(cob_date):
    selected_columns = ', '.join(f'"{column}"' for column in VALUATION_COLUMNS)
    query = text(
        f'''SELECT {selected_columns}
            FROM {fq_table(DB_SCHEMA, 'trades_options_valuation_current')}
            WHERE cob_date = :cob_date
            ORDER BY substrategy, expiration_date, asset_a, asset_b, put_call'''
    )
    try:
        frame = pd.read_sql(
            query,
            get_database_engine(),
            params={'cob_date': pd.Timestamp(cob_date).date()},
        )
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
