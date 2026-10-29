"""The shared reader replaces the formerly Brent-only chart contract."""
import gzip
import json
import numpy as np
import pandas as pd
import pytest

import dash_options.features.vol_surface.delta_history as data
from options.market_data.realized_comparison import comparison_calendar
from dash_options.features.vol_surface import page as vol_surface


def fixture(product='Brent'):
    _,_,sessions,_=comparison_calendar(product,'2026-10-28',90)
    sessions=sessions[sessions>='2026-07-31']
    frame=pd.DataFrame([dict(code=product,cob_date=d,contract_date=pd.Timestamp(m),
        option_expiration_date=pd.Timestamp('2026-12-24'),delta_bucket=b,put_call='call',
        volatility=.4,delta=.5 if b=='ATM' else .1)
        for d in sessions for m in ['2027-01-01','2027-02-01'] for b in ['ATM','10C']])
    return frame,sessions


def patch(monkeypatch,product='Brent',archived=None):
    frame,sessions=fixture(product)
    monkeypatch.setattr(vol_surface.surface_data,'surface_dataset',frame)
    empty=pd.DataFrame(columns=['product','cob_date','contract_date','delta_bucket','volatility'])
    monkeypatch.setattr(data,'load_archive',lambda:(empty if archived is None else archived,{'archive_sha256':'abc'}))
    def price(product,month,*args):
        return pd.DataFrame({'cob_date':sessions,'settlement_price':75.,'source_security':'Shared settlement'}),{
            'price_unit':data.PRICE_UNITS[product],'last_price_date':'2026-10-28','source_errors':[]}
    monkeypatch.setattr(data.realized_comparison_data,'load_comparison',price)
    return frame,sessions


@pytest.mark.parametrize('product',list(data.CALENDARS))
def test_fixed_history_all_products_has_distinct_contracts_and_correct_units(monkeypatch,product):
    _,sessions=patch(monkeypatch,product)
    fig=vol_surface._create_delta_history_figure(product,'month:2027-01-01',['ATM','10C'],90,
        '2026-10-28','fixed_expiry',['2027-01-01','2027-02-01'])
    assert len(fig.data)==6
    assert all(len(t.x)==len(sessions) for t in fig.data)
    assert fig.layout.yaxis.tickformat=='.0%'
    assert fig.layout.yaxis2.title.text=='Settlement ('+data.PRICE_UNITS[product]+')'
    assert fig.layout.meta['contracts']==['2027-01-01','2027-02-01']
    assert not any(t.connectgaps for t in fig.data)
    assert 'transition' not in fig.to_plotly_json()['layout']


def test_archived_and_published_values_remain_separate(monkeypatch):
    frame,_=fixture()
    archive=frame.rename(columns={'code':'product'}).copy()
    archive.volatility=.7
    patch(monkeypatch,archived=archive)
    fig=vol_surface._create_shared_delta_history_figure('Brent',['2027-01-01'],['ATM'],90,'2026-10-28')
    assert len(fig.data)==3
    assert set(fig.data[0].y)=={.4} and set(fig.data[1].y)=={.7}
    assert fig.data[0].line.dash=='solid' and fig.data[1].line.dash=='dot'
    assert 'Archived reconstruction' in fig.data[1].name


def test_missing_and_expired_iv_remain_gaps_while_prices_continue(monkeypatch):
    frame,_=patch(monkeypatch)
    frame.loc[frame.contract_date.eq('2027-01-01'),'option_expiration_date']=pd.Timestamp('2026-10-26')
    frame=frame.loc[frame.cob_date.ne('2026-10-19')]
    monkeypatch.setattr(vol_surface.surface_data,'surface_dataset',frame)
    fig=vol_surface._create_shared_delta_history_figure('Brent',['2027-01-01'],['ATM'],90,'2026-10-28')
    iv=pd.Series(fig.data[0].y,index=fig.data[0].x)
    assert np.isnan(iv.loc['2026-10-19']) and iv.loc['2026-10-26':].isna().all()
    assert fig.data[1].y[-1]==75


def test_archive_failure_preserves_operational_data_and_warns(monkeypatch):
    patch(monkeypatch)
    def fail():raise ValueError('checksum mismatch')
    monkeypatch.setattr(data,'load_archive',fail)
    fig=vol_surface._create_shared_delta_history_figure('Brent',['2027-01-01'],['ATM'],90,'2026-10-28')
    assert len(fig.data)==2
    assert 'checksum mismatch' in fig.layout.annotations[0].text


def test_corrupt_archive_is_rejected(monkeypatch,tmp_path):
    (tmp_path/'manifest.json').write_text(json.dumps({'sha256':'wrong','points':0,'runs':0}))
    (tmp_path/'observations.json.gz').write_bytes(gzip.compress(b'{}'))
    monkeypatch.setattr(data,'ARCHIVE',tmp_path)
    data.load_archive.cache_clear()
    with pytest.raises(ValueError,match='checksum'):
        data.load_archive()
    data.load_archive.cache_clear()


def test_contract_controls_use_data_for_all_products(monkeypatch):
    frame,_=patch(monkeypatch,'JKM')
    monkeypatch.setattr(vol_surface.surface_data,'_ensure_cached_data',lambda *a:None)
    monkeypatch.setattr(vol_surface.surface_data,'_get_surface_snapshot',lambda *a:frame)
    options,values=vol_surface.update_history_contracts(None,'2026-10-28','JKM',90,[])
    assert [o['value'] for o in options]==['2027-01-01','2027-02-01']
    assert values==['2027-01-01','2027-02-01']
    for product in data.CALENDARS:
        assert vol_surface.show_history_contracts(product,'fixed_expiry')['display']=='flex'
        assert vol_surface.show_history_contracts(product,'rolling_tenor')['display']=='none'


def test_mixed_price_units_are_not_overlaid(monkeypatch):
    frame,_=patch(monkeypatch,'NBP')
    original=data.realized_comparison_data.load_comparison
    def price(product,month,*args):
        rows,meta=original(product,month,*args)
        meta['price_unit']='USD/MMBtu' if month=='2027-01-01' else 'GBp/therm'
        return rows,meta
    monkeypatch.setattr(data.realized_comparison_data,'load_comparison',price)
    *_,prices,meta=data.load_history('NBP',['2027-01-01','2027-02-01'],'2026-10-28',90,frame)
    assert prices.settlement_price.isna().all() and 'different price units' in meta['errors'][0]
