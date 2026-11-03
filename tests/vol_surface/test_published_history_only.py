"""Historical IV never falls back to legacy or packaged reconstructions."""
import base64
import io

import pandas as pd
import pytest

from dash_options.core.snapshots import SnapshotReferenceError, SnapshotStore
from dash_options.features.vol_surface import overview, page
from dash_options.shared import surfaces
from tests.vol_surface.test_overview import nodes
from tests.vol_surface.test_published_surface_page import publication_rows


def test_history_requires_publication_provenance_even_before_first_publication(monkeypatch,tmp_path):
    store=SnapshotStore(tmp_path/'history')
    frame=pd.concat([nodes('Brent','2026-07-20'),nodes('Brent','2026-10-29'),
                     nodes('Brent','2026-10-30'),nodes('TTF','2026-10-29')],ignore_index=True)
    ref=store.publish(surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE,'first',{
        'surface_dataset':frame,'history_surface_dataset':frame,'data_cache_state':{'history':{'publications':{
            'BRENT:2026-10-29':{'publication_id':'published-29'},
            'BRENT:2026-10-30':{},
        }}}})
    monkeypatch.setattr(overview,'resolve_snapshot',store.resolve)
    selected=overview.history_frame(ref)
    assert set(selected.code)=={'Brent'}
    assert selected.cob_date.eq(pd.Timestamp('2026-10-29')).all()
    assert selected.attrs['publication_ids']=={'BRENT:2026-10-29':'published-29'}
    # The old reference remains immutable after a newer publication replaces it.
    store.publish(surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE,'second',{'surface_dataset':nodes('Brent')})
    pd.testing.assert_frame_equal(overview.history_frame(ref),selected)
    with pytest.raises(SnapshotReferenceError):overview.history_frame({**ref,'namespace':'wrong'})
    store.close()


@pytest.mark.parametrize('product',['TTF','JKM','NBP'])
def test_published_history_reader_accepts_all_products_without_legacy_fallback(monkeypatch,product):
    rows=publication_rows(product)
    rows['dense_count']=11
    seen=[]
    def read(query,*a,**k):
        seen.append(str(query));return rows
    monkeypatch.setattr(surfaces,'get_database_engine',lambda:None)
    monkeypatch.setattr(surfaces.pd,'read_sql',read)
    frame,publications=surfaces._load_published_history_data()
    assert frame.code.eq(product).all()
    assert publications[f'{product}:2026-10-01']['publication_id']==f'{product}-2026-10-01'
    assert 'vol_surface_publications' in seen[0] and 'implied_volatility_surface_calibrated' in seen[0]
    assert "'TTF', 'JKM', 'NBP'" in seen[0]


def test_missing_publications_give_empty_history_and_do_not_read_another_source(monkeypatch):
    frame=nodes()
    monkeypatch.setattr(overview,'resolve_snapshot',lambda *a,**k:{'surface_dataset':frame,'history_surface_dataset':frame})
    assert overview.history_frame({'namespace':'vol-surface-v2'}).empty
    assert overview.history_frame(None).empty
    monkeypatch.setattr(overview,'resolve_snapshot',lambda *a,**k:{'surface_dataset':frame})
    with pytest.raises(SnapshotReferenceError,match='refresh Options Data'):
        overview.history_frame({'namespace':'vol-surface-v2'})
    monkeypatch.setattr(overview,'resolve_snapshot',lambda *a,**k:{'history_surface_dataset':surfaces._empty_surface_df()})
    assert overview.history_frame({'namespace':'vol-surface-v2'}).empty


def test_history_export_contains_only_published_iv_and_explicit_provenance(monkeypatch):
    from tests.vol_surface.test_delta_history_chart import patch
    patch(monkeypatch)
    frame=overview.history_frame(None)
    frame.attrs.update(iv_source=surfaces.PUBLISHED_SURFACE_SOURCE_LABEL,
                       publication_ids={'BRENT:2026-10-28':'publication-test'})
    monkeypatch.setattr(page.realized_comparison_data,'load_comparison',
        lambda *a:(pd.DataFrame({'cob_date':[pd.Timestamp('2026-10-28')],
            'volatility':[.4],'settlement_price':[75.],'realized_volatility':[.3]}),{}))
    # The shared history reader also needs price metadata; isolate that view.
    def history(*a):
        return frame,pd.DataFrame({'cob_date':[pd.Timestamp('2026-10-28')],'settlement_price':[75.]}),{
            'iv_source':surfaces.PUBLISHED_SURFACE_SOURCE_LABEL,
            'publication_ids':{'BRENT:2026-10-28':'publication-test'}}
    monkeypatch.setattr(page.delta_history_data,'load_history',history)
    result=page.export_surface_history(1,{},'2026-10-28','Brent',['2027-01-01'],90,'fixed_expiry')
    sheets=pd.read_excel(io.BytesIO(base64.b64decode(result['content'])),sheet_name=None)
    assert 'Reconstruction' not in sheets
    assert set(sheets)=={'Settlement IV','Settlements','Realized vs implied','Provenance'}
    fields=sheets['Provenance'].set_index('field')['value']
    assert 'implied_volatility_surface_calibrated' in fields.iv_source
    assert 'publication-test' in fields.publication_ids


def test_smile_lookback_does_not_read_legacy_worker_history(monkeypatch):
    legacy=nodes('Brent','2026-10-28')
    canonical=nodes('Brent','2026-10-27')
    monkeypatch.setattr(surfaces,'surface_dataset',legacy)
    figure=page._create_smile_evolution_figure('Brent','month:2027-01-01',
        nodes('Brent','2026-10-30'),nodes('Brent','2026-10-29'),30,'2026-10-30',
        history_surface=canonical)
    names=[trace.name for trace in figure.data]
    assert '2026-10-27' in names and '2026-10-28' not in names


def test_current_gas_surface_keeps_its_source_while_history_uses_publications(monkeypatch):
    legacy=nodes('TTF','2026-10-29')
    canonical=legacy.copy()
    canonical['volatility']=.55
    metadata={'source':surfaces.SURFACE_POSTGRES_SOURCE_LABEL,'fallback_used':True,'error':None}
    monkeypatch.setattr(surfaces,'_load_legacy_surface_data',lambda:(legacy,metadata))
    ids={'TTF:2026-10-29':{'publication_id':'canonical-ttf'}}
    monkeypatch.setattr(surfaces,'_load_published_surface_data',lambda:(surfaces._empty_surface_df(),{}))
    monkeypatch.setattr(surfaces,'_load_published_history_data',lambda:(canonical,ids))
    operational,meta=surfaces.load_surface_data()
    pd.testing.assert_frame_equal(operational,legacy)
    pd.testing.assert_frame_equal(meta['history_surface_dataset'],canonical)
    assert meta['publications']=={} and meta['history_publications']==ids
    monkeypatch.setattr(overview,'resolve_snapshot',lambda *a,**k:{
        'surface_dataset':operational,'history_surface_dataset':canonical,
        'data_cache_state':{'history':{'publications':ids}}})
    assert overview.history_frame({'id':'test'}).volatility.eq(.55).all()
    # Same numbers with a different publication identity must invalidate history.
    changed={**meta,'history_publications':{'TTF:2026-10-29':{'publication_id':'replaced'}}}
    assert surfaces._surface_source_revision(operational,meta)!=surfaces._surface_source_revision(operational,changed)


def test_no_published_iv_still_shows_prices_and_no_reconstruction(monkeypatch):
    from tests.vol_surface.test_delta_history_chart import patch
    patch(monkeypatch)
    monkeypatch.setattr(overview,'history_frame',lambda *a:surfaces._empty_surface_df())
    figure=page._create_shared_delta_history_figure('Brent',['2027-01-01'],['ATM'],90,'2026-10-28')
    assert len(figure.data)==1 and figure.data[0].name=='Jan-27 settlement'
    assert 'No published IV' in figure.layout.annotations[0].text


def test_realized_comparison_keeps_prices_and_rv_when_no_published_iv():
    from tests.vol_surface.test_realized_comparison import fixture
    _,prices,_=fixture('Brent')
    frame=overview.history_frame(None)
    rows,meta=page.realized_comparison_data.build_comparison('Brent','2027-01-01','2026-10-28',90,
        frame,{'Bloomberg PX_SETTLE':prices})
    assert rows.volatility.isna().all()
    assert rows.realized_volatility.notna().any()
    assert meta['coverage']['iv_observed']==0


def test_unavailable_published_history_clears_old_iv_instead_of_using_legacy(monkeypatch):
    monkeypatch.setattr(surfaces,'_ensure_cached_data',lambda *a:None)
    def unavailable(*a):raise SnapshotReferenceError('Published history source unavailable')
    monkeypatch.setattr(overview,'history_frame',unavailable)
    figure,status=page.update_realized_comparison({},'2026-10-28','Brent','2027-01-01',90)
    assert not figure.data and 'unavailable' in status
    assert page.update_history_buckets({},'Brent','2027-01-01',['ATM'])==([],[])


def test_history_failure_does_not_replace_the_current_surface(monkeypatch):
    frame=nodes('Brent')
    frame['option_expiration_date']=pd.Timestamp('2026-12-25')
    monkeypatch.setattr(surfaces,'_load_legacy_surface_data',lambda:(surfaces._empty_surface_df(),
        {'source':'legacy','error':None,'fallback_used':False}))
    ids={'BRENT:2026-10-29':{'publication_id':'approved'}}
    monkeypatch.setattr(surfaces,'_load_published_surface_data',lambda:(frame,ids))
    def unavailable():raise SnapshotReferenceError('Published history unavailable')
    monkeypatch.setattr(surfaces,'_load_published_history_data',unavailable)
    current,meta=surfaces.load_surface_data()
    pd.testing.assert_frame_equal(current,frame)
    assert meta['history_surface_dataset'].empty and meta['history_publications']=={}
    assert 'unavailable' in meta['history_error']


@pytest.mark.parametrize('warm',[False,True])
def test_old_session_snapshot_rebuilds_without_changing_immutable_payload(monkeypatch,tmp_path,warm):
    from contextlib import nullcontext
    store=SnapshotStore(tmp_path/'migration')
    frame=nodes('Brent','2026-10-29')
    state={'initialized':True,'surface':{'source':'test','rows':len(frame),
           'latest_cob_date':'2026-10-29','fallback_used':False,'error':None}}
    old=store.publish(surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE,'old',{
        'atm_dataset':frame,'surface_dataset':frame,'data_cache_state':state})
    monkeypatch.setattr(surfaces,'resolve_snapshot',store.resolve)
    monkeypatch.setattr(surfaces,'latest_snapshot',store.latest)
    monkeypatch.setattr(surfaces,'publish_snapshot',store.publish)
    monkeypatch.setattr(surfaces,'snapshot_lock',lambda *a,**k:nullcontext())
    monkeypatch.setattr(surfaces,'DATA_CACHE_STATE',state.copy())
    monkeypatch.setattr(surfaces,'_ACTIVE_SURFACE_SNAPSHOT_ID',old['snapshot_id'] if warm else None)
    monkeypatch.setattr(surfaces,'atm_dataset',frame)
    monkeypatch.setattr(surfaces,'surface_dataset',frame)
    new_state={**state,'history':{'publications':{'BRENT:2026-10-29':{'publication_id':'new'}}}}
    monkeypatch.setattr(surfaces,'_build_surface_snapshot_payload',lambda *a:({
        'atm_dataset':frame,'surface_dataset':frame,'history_surface_dataset':frame,
        'data_cache_state':new_state},'new-schema'))
    with pytest.raises(SnapshotReferenceError,match='outdated'):
        surfaces._activate_surface_snapshot(old)
    result=page.initialize_vol_surface_page(None,old,'2026-10-29',None,['Brent'],'monthly')
    assert result[0]['snapshot_id']!=old['snapshot_id']
    assert 'history_surface_dataset' in store.resolve(result[0])
    assert 'history_surface_dataset' not in store.resolve(old)
    assert result[2]=='2026-10-29' and result[5]==['Brent']
    store.close()


def test_history_error_does_not_blank_current_smile_or_surface_matrix(monkeypatch):
    current=nodes('Brent','2026-10-29')
    monkeypatch.setattr(surfaces,'_ensure_cached_data',lambda *a:None)
    monkeypatch.setattr(page,'_selected_surface_pair',lambda *a:(current,surfaces._empty_surface_df(),None))
    def unavailable(*a):raise SnapshotReferenceError('Published history unavailable')
    monkeypatch.setattr(overview,'history_frame',unavailable)
    heat,smile,history,tables,_=page.update_surface_section(
        {},'2026-10-29',None,'Brent','month:2027-01-01',['ATM'],30,'vs_atm','fixed_expiry')
    assert heat.data and smile.data and tables.children
    assert not history.data
