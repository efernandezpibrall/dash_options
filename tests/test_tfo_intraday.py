import numpy as np,pandas as pd
from options.vol_calibration import api
from options.ttf_volatility import STANDARD_CALL_DELTAS,delta_node_to_strike
from options.vol_calibration.intraday_tfo import price,identity,POLICY,VERSION
from uuid import uuid4
from options.option_expiry_engine import calculate_option_expiry

def fixture(month_offset=2, *, base_vol=.55, market_vol=.57, curvature_pp=2.):
    now=pd.Timestamp.now(tz='UTC'); day=now.tz_convert('Asia/Dubai').normalize().tz_localize(None)
    month=(day+pd.offsets.MonthBegin(month_offset)); expiry=pd.Timestamp(calculate_option_expiry('ICE_TTF_TFO_71085679_EXPIRY',month.date())); years=(expiry-day).days/365.25
    deltas=np.array(STANDARD_CALL_DELTAS); ivs=base_vol+.01*curvature_pp*(2*(deltas-.5))**2; forward=80.
    obs=pd.DataFrame({'expiry':month,'option_expiration_date':expiry,'dte':years*365.25,'delta':deltas,'delta_convention':'undiscounted_call_delta','iv':ivs,'strike':[delta_node_to_strike(forward,years,d,v) for d,v in zip(deltas,ivs)],'forward':forward,'source_name':'synthetic','quote_class':'observed','calibration_basis':'observed','weight':1.})
    tail=api.fit_hybrid_candidate(obs,api.get_defaults('TTF'),commodity='TTF',n_starts=1)
    surface,result=api.prepare_gas_publication_expiry(obs,tail['params'],product='TTF',left_blend_width=tail['left_blend_width'],right_blend_width=tail['right_blend_width'])
    surface=api.normalize_ttf_publication_surface(surface,trading_date=day.date())
    surface['source_name']='official'
    rows=[]
    for delta in np.linspace(.15,.85,15):
        vol=market_vol; strike=delta_node_to_strike(forward,years,delta,vol); side='P' if strike<forward else 'C'; premium=float(price(forward,strike,years,vol,side))
        rows.append(dict(underlying_contract_month=month.date(),option_expiration_date=expiry.date(),option_security=str(strike),put_call=side,strike=strike,option_bid=premium-.02,option_ask=premium+.02,underlying_mid=forward,quote_response_at=now,quote_capture_skew_ms=100,executable_iv_status='resolved',executable_iv_mid=vol,currency='EUR',price_unit='EUR/MWH'))
    manifest={'version':VERSION,'implementation':api.implementation_fingerprint(),'policy':POLICY,'snapshot_id':str(uuid4()),'snapshot_fingerprint':'synthetic','business_date':str(day.date()),'cutoff':now.isoformat(),'window_start':(now-pd.Timedelta(hours=1)).isoformat(),'base_publication_id':str(uuid4()),'settlement_cob':str((day-pd.Timedelta(days=1)).date()),'tape':[],'evidence_hash':identity({'chain':rows,'trades':[]}),'expected_intraday_id':None}
    return dict(manifest=manifest,chain=rows,trades=[],base_surface=surface,base_results=[result])

import pytest
from copy import deepcopy
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from options.vol_calibration import intraday_publication as publication
from options.vol_calibration.intraday_tfo import evidence

@pytest.fixture
def inputs():
    return fixture()


def test_price_fit_sparse_modes_and_evidence_exclusions(inputs):
    result=api.fit_tfo_intraday(inputs)
    assert result['summaries'][0]['eligible']
    assert result['summaries'][0]['metrics']['band_fraction'] == 1
    assert len(api.validate_tfo_candidate(result)) == 401
    sparse=deepcopy(inputs)
    sparse['chain']=inputs['chain'][6:9:2]
    held=api.fit_tfo_intraday(sparse)
    assert not held['summaries'][0]['eligible']
    assert 'eight independent strikes' in held['summaries'][0]['reason']
    bad=deepcopy(inputs)
    bad['chain'][0]['quote_response_at']-=pd.Timedelta(minutes=10)
    bad['chain'][1]['option_bid']=bad['chain'][1]['option_ask']+1
    bad['chain'][2]['currency']='USD'
    rows,reasons=evidence(bad,str(inputs['base_surface'].contract_date.iloc[0].date()))
    assert len(rows)==12
    assert {'Stale quote','Invalid market coordinates','Quote units'} <= reasons.keys()
    tampered=deepcopy(result);tampered['surface'][0]['volatility']+=.001
    with pytest.raises(ValueError,match='identity'):
        api.validate_tfo_candidate(tampered)


def publish_base(engine,inputs):
    base=api.publish_hybrid_surface(engine,inputs['base_surface'],inputs['base_results'],commodity='TTF',
        trading_date=inputs['manifest']['settlement_cob'],settlement_cob=inputs['manifest']['settlement_cob'],
        actor_subject='alice',created_by='alice',base_publication_id=None,expected_current_publication_id=None,
        idempotency_key='synthetic-base',return_surface=False)
    inputs['manifest']['base_publication_id']=base['publication_id']
    return base


def test_save_publish_idempotency_isolation_and_consumer_readback(database,inputs,monkeypatch):
    engine=database
    base=publish_base(engine,inputs)
    candidate=api.fit_tfo_intraday(inputs)
    months=[candidate['summaries'][0]['month']]
    draft=api.save_tfo_intraday_draft(engine,candidate,months,actor='alice')
    assert draft==api.save_tfo_intraday_draft(engine,candidate,months,actor='alice')
    assert api.load_tfo_intraday_draft(engine,draft['draft_id'],actor='alice')['candidate']==candidate
    with pytest.raises(ValueError,match='unavailable'):
        api.load_tfo_intraday_draft(engine,draft['draft_id'],actor='bob')
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM at_lng.vol_intraday_active')).scalar_one()==0
    with pytest.raises(DBAPIError):
        with engine.begin() as c:
            c.execute(text('UPDATE at_lng.vol_intraday_drafts SET actor=actor'))
    monkeypatch.setattr(publication,'load_tfo_intraday_inputs',lambda *a,**kw:inputs)
    receipt=api.publish_tfo_intraday_draft(engine,draft['draft_id'],actor='alice')
    assert receipt['row_count']==401
    assert api.publish_tfo_intraday_draft(engine,draft['draft_id'],actor='alice')['publication_id']==receipt['publication_id']
    live=api.resolve_tfo_intraday_publication(engine,inputs['manifest']['business_date'])
    assert live['publication_id']==receipt['publication_id']
    assert api.resolve_tfo_intraday_publication(engine,inputs['manifest']['settlement_cob']) is None
    with engine.connect() as c:
        assert str(c.execute(text("SELECT publication_id FROM at_lng.vol_surface_publications WHERE is_active")).scalar_one())==base['publication_id']
    from pricer_surface_reference import load_published_surface_slice
    loaded=load_published_surface_slice('TTF',inputs['manifest']['business_date'],months,engine=engine)
    assert loaded['publication_id']==receipt['publication_id']
    # An old candidate cannot overwrite the new revision.
    other=deepcopy(candidate); other['manifest']['snapshot_fingerprint']='different'; other['candidate_id']=identity({k:v for k,v in other.items() if k!='candidate_id'})
    second=api.save_tfo_intraday_draft(engine,other,months,actor='alice')
    changed=deepcopy(inputs);changed['manifest']=other['manifest']
    monkeypatch.setattr(publication,'load_tfo_intraday_inputs',lambda *a,**kw:changed)
    with pytest.raises(api.HybridPublicationError,match='changed'):
        api.publish_tfo_intraday_draft(engine,second['draft_id'],actor='alice')


def test_stale_source_and_numerical_corruption_never_activate(database,inputs,monkeypatch):
    engine=database;publish_base(engine,inputs);candidate=api.fit_tfo_intraday(inputs)
    draft=api.save_tfo_intraday_draft(engine,candidate,[candidate['summaries'][0]['month']],actor='alice')
    with pytest.raises(ValueError,match='stale'):
        api.publish_tfo_intraday_draft(engine,draft['draft_id'],actor='alice',now=pd.Timestamp(inputs['manifest']['cutoff'])+pd.Timedelta(minutes=31))
    changed=deepcopy(inputs); changed['manifest']['evidence_hash']='changed'
    monkeypatch.setattr(publication,'load_tfo_intraday_inputs',lambda *a,**kw:changed)
    with pytest.raises(ValueError,match='Source evidence'):
        api.publish_tfo_intraday_draft(engine,draft['draft_id'],actor='alice')
    monkeypatch.setattr(publication,'load_tfo_intraday_inputs',lambda *a,**kw:inputs)
    with engine.begin() as c:
        c.execute(text("""CREATE FUNCTION at_lng.corrupt_intraday_point() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.volatility=NEW.volatility+0.001; RETURN NEW; END $$"""))
        c.execute(text("CREATE TRIGGER corrupt_intraday BEFORE INSERT ON at_lng.implied_volatility_surface_calibrated FOR EACH ROW EXECUTE FUNCTION at_lng.corrupt_intraday_point()"))
    with pytest.raises(api.HybridPublicationError,match='readback differs'):
        api.publish_tfo_intraday_draft(engine,draft['draft_id'],actor='alice')
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM at_lng.vol_intraday_active')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM at_lng.vol_surface_publications')).scalar_one()==1


def persist_source(engine, inputs):
    """Minimal real source contracts; all writes stay in the owned test cluster."""
    manifest=inputs['manifest']
    with engine.begin() as c:
        c.execute(text("UPDATE at_lng.vol_surface_publications SET published_at=:at WHERE publication_id=CAST(:id AS uuid)"),
                  {'at':pd.Timestamp(manifest['cutoff'])-pd.Timedelta(minutes=1),'id':manifest['base_publication_id']})
        c.execute(text("""INSERT INTO at_lng.vol_market_snapshots
          (snapshot_id,commodity,business_date,observed_at,source_name,input_fingerprint,status,option_quote_count,forward_count,metadata)
          VALUES (:id,'TFO',:day,:at,'synthetic-fixture','synthetic','complete',15,1,'{"snapshot_kind":"INTRADAY"}')"""),
          {'id':manifest['snapshot_id'],'day':manifest['business_date'],'at':manifest['cutoff']})
        c.execute(text("""CREATE TABLE at_lng.bbg_option_chain (
          snapshot_id uuid,product text,underlying_contract_month date,option_expiration_date date,
          option_security text,put_call text,strike double precision,option_bid double precision,option_ask double precision,
          underlying_mid double precision,quote_response_at timestamptz,quote_capture_skew_ms integer,
          executable_iv_status text,executable_iv_mid double precision,executable_iv_bid double precision,
          executable_iv_ask double precision,currency text,price_unit text)"""))
        c.execute(text("""CREATE TABLE at_lng.bbg_option_trade_events (
          snapshot_id uuid,product text,underlying_contract_month date,option_expiration_date date,option_security text,
          put_call text,strike double precision,execution_at timestamptz,trade_at timestamptz,trade_price double precision,
          trade_size double precision,trade_iv double precision,trade_iv_status text,iv_eligible boolean,trade_classification text,
          future_match_price double precision,future_match_lag_ms integer,future_match_source text,event_fingerprint text,
          source_event jsonb,occurrence_ordinal integer)"""))
        c.execute(text("""CREATE TABLE at_lng.bbg_option_trade_tape_runs (
          tape_run_id uuid,snapshot_id uuid,coverage_status text,cutoff_at timestamptz,metadata jsonb)"""))
        for r in inputs['chain']:
            row={**r,'snapshot_id':manifest['snapshot_id'],'product':'TFO'}
            names=','.join(row);binds=','.join(':'+k for k in row)
            c.execute(text(f'INSERT INTO at_lng.bbg_option_chain ({names}) VALUES ({binds})'),row)
    return api.load_tfo_intraday_inputs(engine,manifest['snapshot_id'])


def test_real_source_loader_and_ice_reader_share_the_published_revision(database,inputs):
    engine=database
    extra=fixture(month_offset=3)
    inputs['base_surface']=pd.concat([inputs['base_surface'],extra['base_surface']],ignore_index=True)
    inputs['base_results']+=extra['base_results']
    inputs['chain']+=extra['chain']
    publish_base(engine,inputs);loaded=persist_source(engine,inputs)
    assert len(loaded['chain'])==30
    candidate=api.fit_tfo_intraday(loaded)
    draft=api.save_tfo_intraday_draft(engine,candidate,[candidate['summaries'][0]['month']],actor='alice')
    receipt=api.publish_tfo_intraday_draft(engine,draft['draft_id'],actor='alice')
    from options.ttf_quote_valuation import load_ttf_market_snapshot,TTFOptionSpec
    market=load_ttf_market_snapshot(engine,TTFOptionSpec(pd.Timestamp(candidate['summaries'][0]['month']).date(),'C',80.,pd.Timestamp.now(tz='UTC').to_pydatetime()))
    assert market.surface_publication_id==receipt['publication_id']
    assert market.forward==80.
    assert receipt['row_count']==802
    with engine.connect() as c:
        inherited=pd.read_sql(text('SELECT strike,volatility FROM at_lng.implied_volatility_surface_calibrated WHERE publication_id=CAST(:id AS uuid) AND contract_date=:month ORDER BY strike'),c,
            params={'id':receipt['publication_id'],'month':extra['base_surface'].contract_date.iloc[0].date()})
    np.testing.assert_allclose(inherited.volatility,extra['base_surface'].sort_values('strike').volatility,rtol=0,atol=1e-14)
    # Preview/saving does not affect either live consumer until explicit publication.
    second=api.load_tfo_intraday_inputs(engine,inputs['manifest']['snapshot_id'])
    assert second['manifest']['expected_intraday_id']==receipt['publication_id']


def test_request_generation_cache_ownership_and_write_flags(inputs,monkeypatch):
    import tfo_intraday_workspace as ui
    import tfo_intraday_jobs as jobs
    from vol_trades_market_window import history_identity
    from vol_calibration.auth import Identity,Role
    from dash import no_update
    history={'product':'TFO','snapshot_kind':'INTRADAY','snapshot_id':inputs['manifest']['snapshot_id'],
             'observed_at':inputs['manifest']['cutoff'],'business_date':inputs['manifest']['business_date']}
    window={'history_key':history_identity(history),'cutoff_at':inputs['manifest']['cutoff'],'start_at':inputs['manifest']['window_start']}
    request=ui.market_request(history,window,'TFO')
    assert request and ui.market_request(history,window,'TFO',request) is no_update
    assert ui.market_request(history,{**window,'history_key':'old'},'TFO') is None
    assert ui.market_request(history,window,'BRENT') is None
    result=api.fit_tfo_intraday(inputs);ref=jobs.retain(result,'alice')
    assert jobs.resolve(ref,'alice')['candidate_id']==result['candidate_id']
    with pytest.raises(Exception):jobs.resolve(ref,'bob')
    actor=Identity('alice',frozenset({Role.CALIBRATOR,Role.PUBLISHER}),True,'test')
    monkeypatch.setattr(ui,'identity',lambda *a:actor)
    view={'request_key':request['key'],'ref':ref}
    assert ui.candidate(view,request)[1]['candidate_id']==result['candidate_id']
    with pytest.raises(ValueError,match='current snapshot'):
        ui.candidate({**view,'request_key':'old'},request)
    monkeypatch.setattr(ui,'ttf_intraday_writes_enabled',lambda:False)
    monkeypatch.setattr(ui,'ctx',type('Context',(),{'triggered_id':'tfo-intraday-save'})())
    saved,message,activation=ui.save_or_publish(1,0,view,request,[result['summaries'][0]['month']],None)
    assert saved is no_update and activation is no_update and 'disabled' in message
    rendered=ui.render(view,request,None)
    assert len(rendered)==12 and rendered[-1] and rendered[-2]


def test_concurrent_publication_and_new_settlement_base_hold_stale_proposals(database,inputs,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    engine=database;base=publish_base(engine,inputs)
    alternative=deepcopy(inputs);alternative['manifest']['snapshot_id']=str(uuid4())
    sources={item['manifest']['snapshot_id']:item for item in (inputs,alternative)}
    monkeypatch.setattr(publication,'load_tfo_intraday_inputs',lambda engine,snapshot_id,**kw:sources[snapshot_id])
    drafts=[]
    for item in (inputs,alternative):
        candidate=api.fit_tfo_intraday(item)
        drafts.append(api.save_tfo_intraday_draft(engine,candidate,[candidate['summaries'][0]['month']],actor='alice'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(api.publish_tfo_intraday_draft,engine,d['draft_id'],actor='alice') for d in drafts]
        outcomes=[]
        for future in futures:
            try:outcomes.append(future.result()['publication_id'])
            except api.HybridPublicationError:outcomes.append('held')
    assert outcomes.count('held')==1
    day=inputs['manifest']['business_date']
    assert api.resolve_tfo_intraday_publication(engine,day)['publication_id'] in outcomes
    api.publish_hybrid_surface(engine,inputs['base_surface'],inputs['base_results'],commodity='TTF',
        trading_date=inputs['manifest']['settlement_cob'],settlement_cob=inputs['manifest']['settlement_cob'],
        actor_subject='alice',created_by='alice',base_publication_id=base['publication_id'],
        expected_current_publication_id=base['publication_id'],idempotency_key='corrected-base',return_surface=False)
    assert api.resolve_tfo_intraday_publication(engine,day) is None
    with pytest.raises(ValueError,match='base|current'):
        api.publish_tfo_intraday_draft(engine,drafts[0]['draft_id'],actor='alice')


def test_intraday_read_rollback_flag_never_accesses_storage(monkeypatch):
    monkeypatch.setenv('OPTIONS_TFO_INTRADAY_READS_ENABLED','0')
    assert api.resolve_tfo_intraday_publication(object(),pd.Timestamp.now(tz='Asia/Dubai').date()) is None


def test_review_separates_trade_age_and_plots_comparable_moneyness(inputs,monkeypatch):
    import tfo_intraday_workspace as ui
    import vol_trades_data
    from plotly.utils import PlotlyJSONEncoder
    import json
    market=deepcopy(inputs);q=market['chain'][7];historical_forward=q['underlying_mid']*.99
    at=pd.Timestamp(market['manifest']['cutoff'])-pd.Timedelta(minutes=20)
    market['trades']=[dict(q,lifecycle='active',iv_eligible=True,trade_iv_status='resolved',
        trade_classification='regular',execution_at=at,trade_at=at,
        future_match_lag_ms=0,future_match_price=historical_forward,
        trade_price=float(price(historical_forward,q['strike'],(pd.Timestamp(q['option_expiration_date']).date()-pd.Timestamp(market['manifest']['business_date']).date()).days/365.25,.57,q['put_call'])),
        trade_iv=.57,trade_size=10)]
    value=api.fit_tfo_intraday(market);assert value['summaries'][0]['eligible']
    monkeypatch.setattr(ui,'review_candidate',lambda *a:(None,value))
    monkeypatch.setattr(ui,'get_database_engine',lambda **kw:object())
    monkeypatch.setattr(vol_trades_data,'_read_calibrated_surface_points',lambda *a:market['base_surface'])
    figure,detail=ui.chart(value['summaries'][0]['month'],{'ref':'test'},None,{})
    trade=next(t for t in figure.data if t.name=='Bloomberg trades')
    assert abs(trade.x[0]-q['strike']/historical_forward*q['underlying_mid'])<1e-9
    assert trade.customdata[0][1]==20
    assert trade.customdata[0][0]==f"{q['strike']:g}{q['put_call']}"
    text=json.dumps(detail,cls=PlotlyJSONEncoder,ensure_ascii=False)
    assert 'Age · min' in text and 'Older Bloomberg trades carry less weight' in text
    assert 'Full wings checked' in text and 'Tails rise' in text
    surface=pd.DataFrame(value['surface'])
    np.testing.assert_allclose(figure.layout.xaxis.range,[surface.strike.min(),surface.strike.max()])
    assert len(figure.layout.shapes)==2
    assert all(a.text=='Extrapolated' for a in figure.layout.annotations)
