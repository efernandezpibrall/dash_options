"""Shared dashboard source-freshness and alignment metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from sqlalchemy import text

from db_fallback import DB_SCHEMA, safe_exception_message
from runtime_config import get_database_engine


@dataclass(frozen=True)
class SourceStatus:
    source: str
    latest_cob: str | None
    business_day_age: int | None
    fallback_used: bool
    error: str | None

    def to_dict(self):
        return asdict(self)


def business_day_age(latest_cob, as_of=None):
    latest = pd.to_datetime(latest_cob, errors='coerce')
    current = pd.to_datetime(as_of or pd.Timestamp.now(), errors='coerce')
    if pd.isna(latest) or pd.isna(current):
        return None
    latest_day = latest.normalize().date()
    current_day = current.normalize().date()
    if latest_day >= current_day:
        return 0
    return int(np.busday_count(latest_day, current_day))


def make_source_status(source, latest_cob=None, fallback_used=False, error=None, as_of=None):
    latest = pd.to_datetime(latest_cob, errors='coerce')
    latest_text = None if pd.isna(latest) else latest.strftime('%Y-%m-%d')
    return SourceStatus(
        source=source,
        latest_cob=latest_text,
        business_day_age=business_day_age(latest, as_of=as_of),
        fallback_used=bool(fallback_used),
        error=error,
    )


def load_dashboard_source_statuses(as_of=None):
    sources = {
        'Portfolio': f'{DB_SCHEMA}.trades_options_valuation_current',
        'Vol Surface': f'{DB_SCHEMA}.implied_volatility_surface_from_prices',
        'Forward Curves': f'{DB_SCHEMA}.curve',
    }
    query = text(
        f"""
        SELECT 'Portfolio' AS label, max(cob_date)::date AS latest_cob
        FROM {DB_SCHEMA}.trades_options_valuation_current
        UNION ALL
        SELECT 'Vol Surface', max(cob_date)::date
        FROM {DB_SCHEMA}.implied_volatility_surface_from_prices
        UNION ALL
        SELECT 'Forward Curves', max(cob)::date
        FROM {DB_SCHEMA}.curve
        """
    )
    try:
        engine = get_database_engine()
        with engine.connect() as connection:
            frame = pd.read_sql(query, connection)
    except Exception as exc:
        message = safe_exception_message(exc)
        return [
            make_source_status(source, error=message, as_of=as_of).to_dict()
            | {'label': label}
            for label, source in sources.items()
        ]

    latest_by_label = dict(zip(frame['label'], frame['latest_cob']))
    return [
        make_source_status(source, latest_by_label.get(label), as_of=as_of).to_dict()
        | {'label': label}
        for label, source in sources.items()
    ]


def summarize_alignment(statuses, stale_after_business_days=2):
    valid_dates = [status['latest_cob'] for status in statuses if status.get('latest_cob')]
    errors = [status for status in statuses if status.get('error')]
    stale = [
        status
        for status in statuses
        if status.get('business_day_age') is not None
        and status['business_day_age'] > stale_after_business_days
    ]
    misaligned = len(set(valid_dates)) > 1
    tone = 'danger' if errors else ('warning' if stale or misaligned else 'success')
    return {
        'tone': tone,
        'misaligned': misaligned,
        'stale_labels': [status['label'] for status in stale],
        'error_labels': [status['label'] for status in errors],
    }
