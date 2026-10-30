import pandas as pd
import pytest
from sqlalchemy import text

from options.aspect import repository
from options.aspect.errors import AspectPersistenceError
from options.aspect.import_snapshot import import_records


def inputs():
    raw = pd.DataFrame([
        {'instrument': 'ICE_TTF', 'qty': 1000., 'uoM': 'MWh', 'strategy': 'Test',
         'dealType': 'Future', 'maturityForwardDate': '2027-01-01'},
        {'instrument': 'ICE_TTF', 'qty': -100., 'uoM': 'MWh', 'strategy': 'Options',
         'dealType': 'ETO', 'maturityForwardDate': '2027-01-01', 'buySell': 'Sell'},
    ])
    valued = pd.DataFrame([{
        'aspect_source_row_id': 'aspect-option-1', 'asset_a': 'ICE_TTF', 'model': 'black76',
        'qty_delta_asset_a': -50., 'qty_gamma_asset_a': -2., 'qty_vega_sigma1': -30.,
        'qty_theta': 5., 'qty_value': -100., 'qty_pnl': 50.,
    }])
    meta = dict(requested_cob_date='2026-10-29', mode='settlement', fetched_at='2026-10-29T18:00:00+04:00',
                source_rows=2, accepted_rows=2, rejected_rows=0)
    return raw, valued, meta


def test_real_postgres_replacement_readback_and_rollback(database):
    raw, valued, meta = inputs()
    repository.replace_snapshot(database, raw, valued, meta=meta)
    snapshot = repository.load_snapshot(database, '2026-10-29')
    assert snapshot['meta']['valued_option_rows'] == 1
    assert snapshot['rows'][1]['is_option'] is True
    assert snapshot['option_valuations'][0]['qty_delta_asset_a'] == -50.
    assert snapshot['option_valuations'][0]['quantity'] == 100.
    assert repository.available_snapshot_dates(database) == ['2026-10-29']
    broken = raw.copy()
    broken.loc[0, 'qty'] = None  # NOT NULL failure must roll back the version and current pointer.
    with pytest.raises(AspectPersistenceError, match='persistence failed'):
        repository.replace_snapshot(database, broken, valued, meta=meta)
    assert repository.load_snapshot(database, '2026-10-29') == snapshot
    with database.begin() as connection:
        connection.execute(text('DELETE FROM at_lng.aspect_position WHERE source_row_id=0'))
    with pytest.raises(AspectPersistenceError, match='incomplete'):
        repository.load_snapshot(database, '2026-10-29')


def test_import_is_atomic_does_not_overwrite_and_keeps_fetch_time_unknown(database):
    raw, valued, meta = inputs()
    records = repository._records_for_insert(raw, valued, meta=meta)
    import_records(database, records, source_file_sha256='a' * 64)
    snapshot = repository.load_snapshot(database, '2026-10-29')
    assert snapshot['meta']['mode'] == 'imported'
    assert snapshot['meta']['fetched_at'] is None
    assert snapshot['meta']['ingested_at']
    assert snapshot['meta']['source_file_sha256'] == 'a' * 64
    with pytest.raises(AspectPersistenceError, match='Refusing to replace'):
        import_records(database, records, source_file_sha256='a' * 64)
    assert repository.load_snapshot(database, '2026-10-29') == snapshot


def test_versions_preserve_modes_scopes_and_reject_late_promotion(database):
    raw, valued, meta = inputs()
    first = repository.replace_snapshot(database, raw, valued, meta=meta)
    newer = repository.replace_snapshot(database, raw, valued, meta={**meta, 'request_started_at':'2026-10-29T20:00:00+04:00'})
    late = repository.replace_snapshot(database, raw, valued, meta={**meta, 'request_started_at':'2026-10-29T19:00:00+04:00'})
    live = repository.replace_snapshot(database, raw, valued, meta={**meta, 'mode':'live'})
    other_scope = repository.replace_snapshot(database, raw, valued, meta={**meta, 'book':'Other'})
    scope = repository.scope_key('', '')
    assert repository.load_snapshot(database, '2026-10-29', mode='settlement', scope=scope)['meta']['snapshot_id'] == newer
    assert repository.load_snapshot(database, snapshot_id=first)['meta']['is_current'] is False
    assert repository.load_snapshot(database, snapshot_id=late)['meta']['is_current'] is False
    assert repository.load_snapshot(database, snapshot_id=live)['meta']['is_current'] is True
    assert repository.load_snapshot(database, snapshot_id=other_scope)['meta']['is_current'] is True
    with pytest.raises(AspectPersistenceError, match='Multiple Aspect'):
        repository.load_snapshot(database, '2026-10-29')
    with database.connect() as c:
        assert c.execute(text('SELECT count(*) FROM at_lng.aspect_options_valuation_snapshot')).scalar_one() == 5
        assert c.execute(text('SELECT count(*) FROM at_lng.aspect_options_valuation')).scalar_one() == 10


def test_confirmed_empty_snapshot_supersedes_positions_without_losing_history(database):
    raw, valued, meta = inputs()
    first = repository.replace_snapshot(database, raw, valued, meta=meta)
    empty_meta = {**meta, 'source_rows':0, 'accepted_rows':0, 'rejected_rows':0, 'confirmed_empty':True,
                  'request_started_at':'2026-10-30T00:00:00+04:00', 'raw_response':[]}
    empty_id = repository.replace_snapshot(database, pd.DataFrame(), pd.DataFrame(), meta=empty_meta)
    assert repository.load_snapshot(database, '2026-10-29')['meta']['snapshot_id'] == empty_id
    assert repository.load_snapshot(database, '2026-10-29')['rows'] == []
    assert len(repository.load_snapshot(database, snapshot_id=first)['rows']) == 2
    assert repository.load_snapshot_audit(database, empty_id)['raw_response'] == []
    with pytest.raises(AspectPersistenceError, match='confirmed empty'):
        repository.replace_snapshot(database, pd.DataFrame(), pd.DataFrame(), meta={**empty_meta, 'confirmed_empty':False})
