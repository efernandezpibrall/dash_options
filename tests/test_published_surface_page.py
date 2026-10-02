import numpy as np
import pandas as pd
import pytest

import surface_data as data
from snapshot_cache import SnapshotReferenceError


def publication_rows(product='BRENT', date='2026-10-01', value=0.42):
    return pd.DataFrame({
        'product': product, 'cob_date': date, 'maturity_date': '2026-12-01',
        'option_expiration_date': '2026-10-28', 'put_call': 'C',
        'delta': data.PUBLISHED_SURFACE_DELTAS, 'value': value,
        'dense_count': 401, 'anchor_count': 11,
        'publication_id': f'{product}-{date}', 'published_at': '2026-10-02T06:17:31Z',
    })


def read_publication(monkeypatch, rows):
    monkeypatch.setattr(data, 'get_database_engine', lambda: None)
    monkeypatch.setattr(data.pd, 'read_sql', lambda *a, **k: rows.copy())
    return data._load_published_surface_data()


def test_exact_published_values_and_decimal_units(monkeypatch):
    rows = publication_rows(value=6.123456789)
    surface, provenance = read_publication(monkeypatch, rows)
    assert len(surface) == 11
    assert surface.volatility.eq(6.123456789).all()
    assert surface.loc[surface.delta_bucket.eq('ATM'), 'delta'].item() == 0.5
    assert provenance['BRENT:2026-10-01']['publication_id'] == 'BRENT-2026-10-01'


@pytest.mark.parametrize('fault', ['missing_anchor', 'duplicate', 'bad_vol', 'missing_expiry', 'partial_dense'])
def test_bad_publications_fail_closed(monkeypatch, fault):
    rows = publication_rows()
    if fault == 'missing_anchor':
        rows = rows.iloc[:-1]
    elif fault == 'duplicate':
        rows = pd.concat([rows, rows.iloc[:1]])
    elif fault == 'bad_vol':
        rows.loc[0, 'value'] = np.nan
    elif fault == 'missing_expiry':
        rows.loc[0, 'option_expiration_date'] = None
    else:
        rows['dense_count'] = 400
    with pytest.raises(SnapshotReferenceError):
        read_publication(monkeypatch, rows)


def test_publication_precedence_history_gap_and_other_products(monkeypatch):
    raw = pd.concat([
        publication_rows('BRENT', '2026-08-28', 0.20),
        publication_rows('BRENT', '2026-09-30', 0.21),
        publication_rows('BRENT', '2026-10-01', 0.22),
        publication_rows('BRENT', '2026-10-02', 0.23),
        publication_rows('TTF', '2026-10-01', 0.80),
    ])
    legacy = data._normalize_surface_data(raw)
    published, provenance = read_publication(monkeypatch, publication_rows())
    metadata = {'source': data.SURFACE_POSTGRES_SOURCE_LABEL, 'error': None, 'fallback_used': True}
    monkeypatch.setattr(data, '_load_legacy_surface_data', lambda: (legacy, metadata))
    monkeypatch.setattr(data, '_load_published_surface_data', lambda: (published, provenance))
    combined, meta = data.load_surface_data()
    brent = combined.loc[combined.code.eq('Brent')]
    assert brent.loc[brent.cob_date.eq('2026-10-01'), 'volatility'].eq(0.42).all()
    assert not brent.cob_date.eq('2026-10-02').any()
    assert brent.cob_date.eq('2026-08-28').any()
    pd.testing.assert_frame_equal(combined.loc[combined.code.eq('TTF')].reset_index(drop=True),
                                  legacy.loc[legacy.code.eq('TTF')].reset_index(drop=True))
    assert data.surface_source_for('Brent', '2026-10-01', meta).startswith(data.PUBLISHED_SURFACE_SOURCE_LABEL)
    assert data.surface_source_for('TTF', '2026-10-01', meta) == metadata['source']


def test_identical_value_republication_changes_snapshot_revision():
    frame = data._normalize_surface_data(publication_rows())
    first = {'source': 'published', 'publications': {'BRENT:2026-10-01': {'publication_id': 'first'}}}
    second = {'source': 'published', 'publications': {'BRENT:2026-10-01': {'publication_id': 'second'}}}
    assert data._surface_source_revision(frame, first) != data._surface_source_revision(frame, second)
