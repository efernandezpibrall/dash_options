import numpy as np
import pandas as pd
import pytest
from options.realized_comparison import comparison_calendar,evaluate_prices
import dash_options.features.vol_surface.realized_comparison as data
from dash_options.features.vol_surface import page as vol_surface


def fixture(product='TTF'):
    start,end,sessions,calendar=comparison_calendar(product,'2026-10-28',90)
    prices=pd.DataFrame({'cob_date':sessions,'contract_date':pd.Timestamp('2027-01-01'),
        'settlement_price':50*np.exp(.01*np.sin(np.arange(len(sessions))))})
    iv=pd.DataFrame({'cob_date':sessions,'code':product,'contract_date':pd.Timestamp('2027-01-01'),
        'delta_bucket':'ATM','put_call':'call','volatility':.4,'option_expiration_date':pd.Timestamp('2026-12-28')})
    return sessions,prices,iv


@pytest.mark.parametrize('product',data.CALENDARS)
def test_five_products_real_prices_and_iv_remain_aligned(product):
    sessions,prices,iv=fixture(product)
    rows,meta=data.build_comparison(product,'2027-01-01','2026-10-28',90,iv,{'settlements':prices})
    assert rows.realized_volatility.notna().all()
    assert rows.volatility.eq(.4).all()
    assert np.allclose(rows.spread,.4-rows.realized_volatility)
    assert meta['product']==product


def test_missing_session_invalidates_window_but_retains_iv():
    sessions,prices,iv=fixture()
    prices=prices.loc[prices.cob_date.ne('2026-10-19')]
    rows,_=data.build_comparison('TTF','2027-01-01','2026-10-28',90,iv,{'settlements':prices})
    last=rows.iloc[-1]
    assert np.isnan(last.realized_volatility) and last.volatility==.4 and np.isnan(last.spread)
    assert rows.loc[rows.cob_date.eq('2026-10-19'),'volatility'].iloc[0]==.4


def test_sources_never_spliced_and_deterministic_selection():
    sessions,prices,iv=fixture()
    first=prices.loc[prices.cob_date.le('2026-09-01')]
    second=prices.loc[prices.cob_date.gt('2026-09-01')].copy()
    second.settlement_price*=10
    rows,meta=data.build_comparison('TTF','2027-01-01','2026-10-28',90,iv,{'first':first,'second':second})
    assert meta['price_source'] in ('first','second')
    assert rows.loc[rows.cob_date.eq('2026-09-02'),'realized_volatility'].isna().all()


def test_invalid_and_duplicate_prices_are_gaps():
    sessions,prices,_=fixture()
    prices.loc[prices.cob_date.eq('2026-10-19'),'settlement_price']=0
    prices=pd.concat([prices,prices.loc[prices.cob_date.eq('2026-10-20')]])
    rows=evaluate_prices(prices,sessions).set_index('cob_date')
    assert rows.loc['2026-10-19':'2026-10-20','settlement_price'].isna().all()
    assert np.isnan(rows.iloc[-1].realized_volatility)


def test_no_price_source_retains_iv_and_expiry_masks_iv():
    _,_,iv=fixture()
    iv.option_expiration_date=pd.Timestamp('2026-10-27')
    rows,_=data.build_comparison('TTF','2027-01-01','2026-10-28',90,iv,{})
    assert rows.realized_volatility.isna().all()
    assert rows.loc[rows.cob_date.eq('2026-10-26'),'volatility'].iloc[0]==.4
    assert rows.loc[rows.cob_date.ge('2026-10-27'),'volatility'].isna().all()


def test_product_controls_and_empty_contracts(monkeypatch):
    monkeypatch.setattr(vol_surface.surface_data,'_ensure_cached_data',lambda *a:None)
    monkeypatch.setattr(vol_surface.surface_data,'_get_surface_snapshot',lambda *a:pd.DataFrame({'contract_date':pd.to_datetime(['2027-02-01'])}))
    for product in data.CALENDARS:
        style,options,value=vol_surface.update_comparison_contracts(None,'2026-10-28',product,'2027-01-01')
        assert style['display']=='block' and value=='2027-02-01'
    monkeypatch.setattr(vol_surface.surface_data,'_get_surface_snapshot',lambda *a:pd.DataFrame({'contract_date':pd.to_datetime([])}))
    assert vol_surface.update_comparison_contracts(None,'2026-10-28','TTF',None)[2] is None


def test_graph_exposes_source_and_gaps(monkeypatch):
    _,prices,iv=fixture()
    rows,meta=data.build_comparison('TTF','2027-01-01','2026-10-28',90,iv,{'settlements':prices})
    monkeypatch.setattr(data,'load_comparison',lambda *a:(rows,meta))
    fig,status=vol_surface._create_realized_comparison_figure('TTF','2027-01-01',90,'2026-10-28',iv)
    assert len(fig.data)==3 and fig.layout.yaxis.tickformat=='.0%'
    assert not fig.data[0].connectgaps and not fig.data[1].connectgaps
    assert 'settlements' in status and '2026-10-28' in status


def test_jkm_uses_only_existing_curve_and_recovers_after_backfill(monkeypatch):
    sessions,prices,iv=fixture('JKM')
    curve=prices.rename(columns={'cob_date':'trade_date','contract_date':'maturity_date','settlement_price':'price'})
    monkeypatch.setattr(data.market_data,'load_forward_curves',lambda *a,**k:curve)
    monkeypatch.setattr(data,'get_database_engine',lambda:pytest.fail('JKM must not query Bloomberg tables'))
    sources,errors=data.load_price_sources('JKM',sessions[0],sessions[-1],revision='jkm-recovery-test')
    rows,_=data.build_comparison('JKM','2027-01-01','2026-10-28',90,iv,sources,errors)
    assert list(sources)==['Shared final curve'] and not errors
    assert rows.realized_volatility.notna().all()


def test_price_capture_is_immutable_across_lookbacks(monkeypatch,tmp_path):
    from dash_options.core.snapshots import SnapshotStore
    store=SnapshotStore(tmp_path/'snapshots')
    for name,method in [('latest_snapshot','latest'),('publish_snapshot','publish'),
                        ('resolve_snapshot','resolve'),('snapshot_lock','lock')]:
        monkeypatch.setattr(data,name,getattr(store,method))
    _,prices,iv=fixture()
    calls=[]
    def read(*args):
        calls.append(args)
        return {'settlements':prices.copy()},[]
    monkeypatch.setattr(data,'load_price_sources',read)
    reference=store.publish('vol-surface-v2','test-iv',{})
    first,meta=data.load_comparison('TTF','2027-01-01','2026-10-28',90,iv,reference)
    prices.loc[prices.cob_date.eq('2026-10-19'),'settlement_price']=np.nan
    second,new_meta=data.load_comparison('TTF','2027-01-01','2026-10-28',30,iv,reference)
    assert len(calls)==1
    assert second.realized_volatility.notna().all()
    assert meta['price_snapshot_reference']==new_meta['price_snapshot_reference']
    store.close()


def test_ttf_uses_underlying_trading_days_not_option_expiry_business_days():
    _,_,sessions,calendar=comparison_calendar('TTF','2026-10-28',90)
    assert pd.Timestamp('2026-08-31') in sessions
    assert not calendar.is_business_day('2025-12-26')
    assert not calendar.is_business_day('2026-04-06')
