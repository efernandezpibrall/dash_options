"""Complete manual snapshots survive cache expiry without crossing actor/revision scope."""
from copy import deepcopy
import json

import pytest

import dash_options.features.live_prices.service as data
from options.market_data.ttf_live_curve import build_ttf_curve, month_add
from options.market_data.live_prices import METHODOLOGY_VERSION, period_rows
from dash_options.core.snapshots import SnapshotStore, SnapshotReferenceError


def complete_payload():
    months = [month_add('2026-11-01', i) for i in range(53)]
    raw = {'months': months[:48], 'captured_at': '2026-10-22T07:00:00+00:00',
           'as_of_date': '2026-10-22', 'contracts': {'TTF': {
               m: {'prior_settle': 30 + i / 10, 'prior_date': '2026-10-21', 'fields': {}}
               for i, m in enumerate(months)}}}
    curve = build_ttf_curve(raw)
    view = {'methodology_version': METHODOLOGY_VERSION, 'captured_at': raw['captured_at'], 'months': raw['months'],
            'products': {'TTF': [{'month': m, **curve['months'][m]} for m in raw['months']]},
            'strips': {'TTF': curve['periods']}, 'ttf_curve': curve,
            'curves':{}, 'fx_quality':{'status':'Verified'}}
    for product in ('TFU','Brent','HH'):
        internal = [{'id':f'{product}-{m}', 'month':m,'mid':30+i/10, 'prior':30+i/10}
                    for i,m in enumerate(months)]
        view['products'][product] = internal[:48]
        view['strips'][product] = period_rows(raw['months'],internal,product)
        if product == 'TFU':
            view['tfu_curve'] = {'calculation_months':internal}
        else:
            view['curves'][product] = {'calculation_months':internal,'max_interval_violation':0}
    return {'raw': raw, 'view': view}


@pytest.fixture
def store(monkeypatch, tmp_path):
    cache = SnapshotStore(tmp_path / 'cache')
    monkeypatch.setattr(data, 'snapshot_store', cache)
    monkeypatch.setattr(data, 'namespace', lambda actor: 'test-live:' + actor)
    monkeypatch.setattr(data, '_retained_path', lambda prefix: tmp_path / 'retained' / (prefix.replace(':', '-') + '.json'))
    yield cache
    cache.close()


def test_exact_reference_survives_cache_expiry_and_preserves_capture_time(store):
    payload = complete_payload()
    ref = store.publish(data.namespace('alice'), 'source-1', payload)
    data._retain_complete(data.namespace('alice'), ref, payload)
    store.cache.clear()
    assert data.resolve(ref, 'alice') == json.loads(json.dumps(payload['view']))
    assert data.resolve(ref, 'alice')['captured_at'] == '2026-10-22T07:00:00+00:00'
    with pytest.raises(SnapshotReferenceError):
        data.resolve(ref, 'bob')
    forged = {**ref, 'snapshot_id': 'different'}
    with pytest.raises(SnapshotReferenceError):
        data.resolve(forged, 'alice')
    assert data._retained_path(data.namespace('alice')).stat().st_mode & 0o777 == 0o600


def test_incomplete_refresh_cannot_replace_last_complete_curve(store):
    payload = complete_payload()
    prefix = data.namespace('alice')
    ref = store.publish(prefix, 'source-1', payload)
    data._retain_complete(prefix, ref, payload)
    before = data._retained_path(prefix).read_bytes()
    incomplete = deepcopy(payload)
    incomplete['view']['products']['TTF'][0]['mid'] = None
    with pytest.raises(ValueError, match='Incomplete TTF curve'):
        data._retain_complete(prefix, ref, incomplete)
    assert data._retained_path(prefix).read_bytes() == before
    assert data._retained_complete(prefix)['reference'] == ref


@pytest.mark.parametrize('product',['TFU','Brent','HH'])
def test_other_market_failure_cannot_replace_last_complete_snapshot(store,product):
    payload = complete_payload()
    prefix = data.namespace('alice')
    ref = store.publish(prefix,'source-1',payload)
    data._retain_complete(prefix,ref,payload)
    before = data._retained_path(prefix).read_bytes()
    incomplete = deepcopy(payload)
    incomplete['view']['products'][product][0]['mid'] = None
    with pytest.raises(ValueError,match=f'Incomplete {product} curve'):
        data._retain_complete(prefix,ref,incomplete)
    assert data._retained_path(prefix).read_bytes() == before


def test_stale_fx_does_not_replace_the_last_complete_snapshot(store):
    payload = complete_payload()
    prefix = data.namespace('alice')
    ref = store.publish(prefix,'source-1',payload)
    data._retain_complete(prefix,ref,payload)
    incomplete = deepcopy(payload)
    incomplete['view']['fx_quality']['status']='Stale'
    with pytest.raises(ValueError,match='FX timestamps failed'):
        data._retain_complete(prefix,ref,incomplete)
    assert data._retained_complete(prefix)['reference'] == ref


def test_corrupt_retained_record_is_not_rendered(store):
    payload = complete_payload()
    prefix = data.namespace('alice')
    ref = store.publish(prefix, 'source-1', payload)
    data._retain_complete(prefix, ref, payload)
    path = data._retained_path(prefix)
    envelope = json.loads(path.read_text())
    envelope['record']['payload']['view']['products']['TTF'][0]['mid'] = 999
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match='integrity'):
        data._retained_complete(prefix)


def test_expired_old_reference_never_resolves_to_a_newer_snapshot(store):
    payload = complete_payload()
    prefix = data.namespace('alice')
    old = store.publish(prefix, 'source-1', payload)
    new = store.publish(prefix, 'source-2', payload)
    data._retain_complete(prefix, new, payload)
    store.cache.clear()
    with pytest.raises(SnapshotReferenceError):
        data.resolve(old, 'alice')
    assert data.resolve(new, 'alice')['captured_at'] == payload['view']['captured_at']


def test_failed_manual_refresh_recovers_last_complete_reference(store, monkeypatch, tmp_path):
    import time
    payload = complete_payload()
    prefix = data.namespace('alice')
    previous = store.publish(prefix, 'source-before', payload)
    data._retain_complete(prefix, previous, payload)
    monkeypatch.setattr(data, '_capture_source', lambda progress, **kwargs: payload['raw'])
    incomplete = deepcopy(payload['view'])
    incomplete['products']['TTF'][0]['mid'] = None
    monkeypatch.setattr(data, 'build_snapshot', lambda raw: incomplete)
    ticket = data.submit('alice')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = data.poll(ticket, 'alice')
        if state['status'] == 'failed':
            break
        time.sleep(.01)
    assert state['status'] == 'failed'
    assert state['retained_ref'] == previous
    assert 'original capture time' in state['error']
    assert data.resolve(previous, 'alice')['captured_at'] == payload['view']['captured_at']
    assert store.latest(prefix) == previous
    assert store.cache.get(prefix + ':rebuild-prepared') is True


def test_queued_tabs_join_only_before_acquisition_and_keep_actor_scope(store, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4
    monkeypatch.setattr(data, 'FUTURES', {})
    monkeypatch.setattr(data, 'POOL', SimpleNamespace(submit=lambda run: object()))
    first = data.submit('alice', str(uuid4()))
    second = data.submit('alice', str(uuid4()))
    assert first['key'] == second['key'] and first['tab'] != second['tab']
    other = data.submit('bob', str(uuid4()))
    assert other['key'] != first['key']
    with pytest.raises(ValueError, match='does not belong'):
        data.poll(first, 'bob')
    state = store.cache.get(first['key'])
    state.update(status='running', accepting_join=False)
    store.cache.set(first['key'], state)
    later = data.submit('alice', str(uuid4()))
    assert later['key'] != first['key']
    assert data.submit('alice', first['tab'])['key'] == first['key']
