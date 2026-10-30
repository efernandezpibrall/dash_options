"""Exercise the four-table lifecycle against owned PostgreSQL, without Aspect access."""
from types import SimpleNamespace
import json
import pandas as pd
import pytest
from sqlalchemy import text
from options.aspect import service, repository, storage
from options.aspect.errors import AspectSourceError, AspectPersistenceError


def settings():
    return service.AspectSettings('test','secret',service.ASPECT_DEFAULT_URL,'Test','',None,True,10,True)


def raw_option(**changes):
    return dict(instrument='ICE_TTF',qty=-100.,uoM='MWh',strategy='Test',dealType='ETO',entityType='Paper',
                exchangeTradeOtc='Exchange Trade',maturityForwardDate='2027-01-01',buySell='Sell',**changes)


def price(rows,**kwargs):
    return pd.DataFrame([dict(aspect_source_row_id=f'aspect-option-{i}',asset_a='ICE_TTF',model='black76',
        qty_delta_asset_a=-50.,qty_gamma_asset_a=-2.,qty_vega_sigma1=-30.,qty_theta=5.,qty_value=-100.,qty_pnl=50.) for i in rows.index])


def response(payload,status=200):
    return SimpleNamespace(status_code=status,ok=status==200,text=json.dumps(payload),json=lambda:payload)


def test_failed_valuation_retains_source_then_revalues_without_http(database,monkeypatch):
    payload=[dict(raw_option(),dealType='Future',qty='broken'),dict(raw_option(),dealType='Future'),raw_option()]
    calls=[]
    def fetch(*a,**k):
        calls.append(1);return response(payload)
    def fail(*a,**k):
        with database.connect() as c:
            assert c.execute(text('SELECT count(*) FROM at_lng.aspect_position')).scalar_one()==3
        raise service.backend.AspectOptionValuationError('Missing governed surface')
    monkeypatch.setattr(service.backend,'value_aspect_option_positions',fail)
    with pytest.raises(AspectSourceError,match='Source positions retained'):
        service.refresh_snapshot('2026-10-29',engine=database,schema='at_lng',settings=settings(),request_get=fetch)
    assert repository.load_snapshot(database,'2026-10-29') is None
    with database.connect() as c:
        source=dict(c.execute(text('SELECT * FROM at_lng.aspect_position_snapshot')).mappings().one())
        assert source['raw_response']==payload and source['validation_status']=='partial'
        assert source['request_parameters']['cobDate']=='2026-10-29'
        assert source['request_attempts']==1 and 'secret' not in str(source)
        assert c.execute(text('SELECT status FROM at_lng.aspect_valuation_run')).scalar_one()=='failed'
    source_id=str(source['snapshot_id'])
    monkeypatch.setattr(service.backend,'value_aspect_option_positions',price)
    first=service.revalue_snapshot(source_id,engine=database,schema='at_lng',writes_enabled=True)
    assert len(first['rows'])==2 and len(first['option_valuations'])==1
    assert first['option_valuations'][0]['aspect_source_row_id']=='aspect-option-2'
    second=service.revalue_snapshot(source_id,engine=database,schema='at_lng',writes_enabled=True)
    assert second['meta']['snapshot_id']!=first['meta']['snapshot_id']
    assert second['meta']['source_snapshot_id']==source_id
    assert repository.load_snapshot(database,snapshot_id=first['meta']['snapshot_id'])['option_valuations']==first['option_valuations']
    monkeypatch.setattr(service.backend,'value_aspect_option_positions',fail)
    with pytest.raises(AspectSourceError):
        service.revalue_snapshot(source_id,engine=database,schema='at_lng',writes_enabled=True)
    assert repository.load_snapshot(database,'2026-10-29')['meta']['snapshot_id']==second['meta']['snapshot_id']
    assert calls==[1]
    with database.connect() as c:
        assert c.execute(text('SELECT count(*) FROM at_lng.aspect_position_snapshot')).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM at_lng.aspect_position')).scalar_one()==3
        assert c.execute(text('SELECT count(*) FROM at_lng.aspect_valuation_run')).scalar_one()==4
        assert c.execute(text('SELECT count(*) FROM at_lng.aspect_option_valuation')).scalar_one()==2
        run=c.execute(text('SELECT input_manifest,engine_revision FROM at_lng.aspect_valuation_run WHERE is_current')).mappings().one()
        assert run['engine_revision'] and run['input_manifest'][0]['source_row_id']==2
        rows=list(c.execute(text('SELECT source_row_id,original_row_index,validation_status FROM at_lng.aspect_position ORDER BY source_row_id')))
        assert rows==[(0,0,'rejected'),(1,1,'accepted'),(2,2,'accepted')]


@pytest.mark.parametrize('payload,status', [([dict(raw_option(),qty='broken')],200),([None],200),({'unexpected':[]},200),({'error':'denied'},401)])
def test_invalid_source_is_retained_without_publishing(database,payload,status):
    with pytest.raises((AspectSourceError,AspectPersistenceError)):
        service.refresh_snapshot('2026-10-29',engine=database,schema='at_lng',settings=settings(),request_get=lambda *a,**k:response(payload,status))
    with database.connect() as c:
        row=c.execute(text('SELECT raw_response,response_body,validation_status FROM at_lng.aspect_position_snapshot')).mappings().one()
        assert row['raw_response']==payload and row['response_body']==json.dumps(payload)
        assert row['validation_status']=='invalid'
        assert c.execute(text('SELECT count(*) FROM at_lng.aspect_valuation_run WHERE is_current')).scalar_one()==0


def test_empty_source_publishes_explicit_zero_run(database):
    result=service.refresh_snapshot('2026-10-29',engine=database,schema='at_lng',settings=settings(),request_get=lambda *a,**k:response([]))
    assert result['rows']==[] and result['meta']['accepted_rows']==0
    assert result['meta']['source_snapshot_id']!=result['meta']['valuation_run_id']


def test_database_rejects_valuation_linked_to_different_source(database):
    meta=dict(requested_cob_date='2026-10-29',mode='settlement',strategy_filter='Test')
    first=storage.start_source(database,meta)
    second=storage.start_source(database,meta)
    storage.capture_source(database,first,[],{})
    storage.capture_source(database,second,[],{})
    run=storage.start_run(database,first)
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError),database.begin() as c:
        c.execute(text('INSERT INTO at_lng.aspect_option_valuation (run_id,source_snapshot_id,source_row_id,model,asset_a,position_value,position_pnl,position_delta_asset_a,position_gamma_asset_a,position_vega_sigma1,position_theta) VALUES (:run,:source,0,\'black76\',\'ICE_TTF\',0,0,0,0,0,0)'),dict(run=run,source=second))


def test_late_run_and_repricing_older_source_cannot_replace_newer_positions(database):
    sources=[]
    for time in ('2026-10-29T10:00:00Z','2026-10-29T11:00:00Z'):
        sid=storage.start_source(database,dict(requested_cob_date='2026-10-29',mode='settlement',request_started_at=time))
        storage.capture_source(database,sid,[raw_option()],{})
        _,frame=storage.load_source_frame(database,sid)
        prepared,rejected=service.prepare_positions(frame)
        storage.validate_source(database,sid,prepared,rejected)
        sources.append((sid,prepared))
    old=storage.start_run(database,sources[0][0])
    new=storage.start_run(database,sources[1][0])
    storage.publish_run(database,new,price(sources[1][1]))
    storage.publish_run(database,old,price(sources[0][1]))
    assert repository.load_snapshot(database,'2026-10-29')['meta']['snapshot_id']==new
    later_reprice=storage.start_run(database,sources[0][0])
    storage.publish_run(database,later_reprice,price(sources[0][1]))
    assert repository.load_snapshot(database,'2026-10-29')['meta']['snapshot_id']==new


def test_network_failure_keeps_attempt_metadata(database,monkeypatch):
    import requests
    def timeout(*a,**k):
        raise requests.Timeout('test')
    original=service.fetch_report
    monkeypatch.setattr(service,'fetch_report',lambda *a,**k:original(*a,**k,retry_sleep=lambda _:None,jitter=lambda:0))
    with pytest.raises(AspectSourceError,match='could not be reached'):
        service.refresh_snapshot('2026-10-29',engine=database,schema='at_lng',settings=settings(),request_get=timeout)
    with database.connect() as c:
        row=c.execute(text('SELECT ingestion_status,request_attempts,fetched_at FROM at_lng.aspect_position_snapshot')).one()
        assert row==('failed',3,None)
