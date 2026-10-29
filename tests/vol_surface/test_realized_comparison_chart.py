import numpy as np
import pandas as pd

from dash_options.features.vol_surface import page as vol_surface
import dash_options.features.vol_surface.realized_comparison as data


def comparison():
    dates=pd.bdate_range('2026-07-20','2026-10-15')
    rows=pd.DataFrame({'cob_date':dates,'realized_volatility':.30,'volatility':.40,
                       'spread':.10,'window_start':dates-pd.Timedelta(days=30),'window_end':dates,
                       'return_count':22,'days_to_expiry':100,'iv_source':'Operational surface history',
                       'source_security':'COZ31 Comdty'})
    rows.loc[1,'volatility']=np.nan
    rows.loc[1,'spread']=np.nan
    return rows,{'input_fingerprint':'prices-v1','requested_start':pd.Timestamp('2026-07-18'),
                 'requested_end':pd.Timestamp('2026-10-15'),'contract':'2031-12-01','run_id':'settlement-run','price_source':'Shared final curve','last_price_date':'2026-10-15'}


def test_comparison_has_common_vol_axis_and_separate_spread_with_visible_iv_gap(monkeypatch):
    monkeypatch.setattr(data,'load_comparison',lambda *a:comparison())
    fig,status=vol_surface._create_realized_comparison_figure('Brent','2031-12-01',90,'2026-10-15',pd.DataFrame())
    assert len(fig.data)==3
    assert fig.data[0].yaxis==fig.data[1].yaxis=='y'
    assert fig.data[2].yaxis=='y2'
    assert fig.layout.yaxis.tickformat=='.0%'
    assert fig.layout.yaxis2.title.text=='IV − RV (vol points)'
    assert np.isnan(fig.data[0].y[1]) and not fig.data[0].connectgaps
    assert fig.data[2].y[0]==10
    assert fig.layout.meta['rolling_window_calendar_days']==30
    assert 'Dec-31' in status and '63/64' in status
    assert 'transition' not in fig.to_plotly_json()['layout']


def test_contract_control_includes_entire_surface_and_expired_backfilled_month(monkeypatch):
    months=pd.date_range('2026-12-01','2031-12-01',freq='MS')
    monkeypatch.setattr(vol_surface.surface_data,'_ensure_cached_data',lambda *a:None)
    monkeypatch.setattr(vol_surface.surface_data,'_get_surface_snapshot',lambda *a:pd.DataFrame({'contract_date':months}))
    style,options,value=vol_surface.update_comparison_contracts(None,'2026-10-15','Brent','2031-12-01')
    assert style['display']=='block' and len(options)==61 and value=='2031-12-01'
    assert options[0]['value']=='2026-12-01' and options[-1]['value']=='2031-12-01'
    assert vol_surface.update_comparison_contracts(None,'2026-10-15','TTF',value)[0]['display']=='block'


def test_settlement_failure_is_visible(monkeypatch):
    def fail(*args):
        raise ValueError('Incomplete rolling warm-up')
    monkeypatch.setattr(data,'load_comparison',fail)
    fig,status=vol_surface._create_realized_comparison_figure('Brent','2027-01-01',90,'2026-10-15',pd.DataFrame())
    assert not fig.data and 'unavailable' in status and 'unavailable' in fig.layout.annotations[0].text
