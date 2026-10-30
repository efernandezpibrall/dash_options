"""Shared dashboard source-freshness and alignment metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

import numpy as np
import pandas as pd
from sqlalchemy import text

from dash_options.core.database_reads import DB_SCHEMA, safe_exception_message, read_curve_with_fallback
from dash_options.core.config import get_database_engine
from dash_options.core.snapshots import SnapshotReferenceError, latest_snapshot, publish_snapshot, resolve_snapshot


SOURCE_STATUS_SNAPSHOT_NAMESPACE = 'dashboard-source-status-v3'
SOURCE_STATUS_CACHE_TTL_SECONDS = 5 * 60


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


def _load_dashboard_source_statuses_uncached(as_of=None):
    sources = {
        'Portfolio': f'{DB_SCHEMA}.trades_options_valuation_current',
        'Vol Surface': (f'{DB_SCHEMA}.implied_volatility_surface_calibrated (Brent/HH); '
                        f'{DB_SCHEMA}.implied_volatility_surface_from_prices (JKM/NBP/TTF)'),
        'Forward Curves': f'{DB_SCHEMA}.curve',
    }
    query = text(
        f"""
        SELECT 'Portfolio' AS label, max(cob_date)::date AS latest_cob
        FROM {DB_SCHEMA}.trades_options_valuation_current
        UNION ALL
        SELECT 'Vol Surface', max(cob_date)::date FROM (
            SELECT cob_date FROM {DB_SCHEMA}.implied_volatility_surface_from_prices
            WHERE product NOT IN ('BRENT', 'HH')
            UNION ALL
            SELECT cob_date FROM {DB_SCHEMA}.vol_surface_publications
            WHERE commodity IN ('BRENT', 'HH') AND is_active AND status = 'published'
        ) surface_dates
        """
    )
    statuses = []
    try:
        engine = get_database_engine()
        with engine.connect() as connection:
            frame = pd.read_sql(query, connection)
        latest_by_label = dict(zip(frame['label'], frame['latest_cob']))
        for label in ('Portfolio', 'Vol Surface'):
            statuses.append(make_source_status(sources[label], latest_by_label.get(label), as_of=as_of).to_dict() | {'label': label})
    except Exception as exc:
        for label in ('Portfolio', 'Vol Surface'):
            statuses.append(make_source_status(sources[label], error=safe_exception_message(exc), as_of=as_of).to_dict() | {'label': label})
    try:
        frame, source = read_curve_with_fallback(
            "SELECT CAST(max(ondate) AS date) AS latest_cob FROM enverus.curve "
            "WHERE category='FINANCIAL' AND version_name='FINAL' AND forward_curve_tenors_value IS NOT NULL",
            text(f'SELECT max(cob)::date AS latest_cob FROM {DB_SCHEMA}.curve'),
        )
        latest = frame['latest_cob'].iloc[0] if not frame.empty else None
        status = make_source_status(source, latest, fallback_used=source != 'transformed.enverus.curve', as_of=as_of)
    except Exception as exc:
        status = make_source_status(sources['Forward Curves'], error=safe_exception_message(exc), as_of=as_of)
    statuses.append(status.to_dict() | {'label': 'Forward Curves'})
    return statuses


def load_dashboard_source_statuses(as_of=None, *, force=False):
    """Load source alignment at most once per five minutes across workers."""
    if as_of is None and not force:
        reference = latest_snapshot(SOURCE_STATUS_SNAPSHOT_NAMESPACE)
        if reference:
            try:
                return resolve_snapshot(
                    reference,
                    expected_namespace=SOURCE_STATUS_SNAPSHOT_NAMESPACE,
                )
            except SnapshotReferenceError:
                pass

    statuses = _load_dashboard_source_statuses_uncached(as_of=as_of)
    if as_of is not None:
        return statuses

    revision_payload = json.dumps(statuses, sort_keys=True, separators=(',', ':'))
    source_revision = hashlib.sha256(revision_payload.encode('utf-8')).hexdigest()
    publish_snapshot(
        SOURCE_STATUS_SNAPSHOT_NAMESPACE,
        source_revision,
        statuses,
        metadata={'sources': len(statuses)},
        group='source-status',
        force=force,
        ttl_seconds=SOURCE_STATUS_CACHE_TTL_SECONDS,
    )
    return statuses


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
