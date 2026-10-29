"""Read existing shared price sources; no SQL writes or Brent archive dependency."""
import hashlib
import json
import numpy as np
import pandas as pd
from sqlalchemy import text
from options.market_data.realized_comparison import (
    CALENDARS as CALENDARS,
    POLICY,
    SETTLEMENT_SOURCES,
    comparison_calendar,
    evaluate_prices,
)
from dash_options.core.config import get_database_engine
from dash_options.core.database_reads import safe_exception_message
from dash_options.core.workspace_cache import WorkspaceLoadCache
import dash_options.shared.market_data as market_data
from dash_options.core.snapshots import latest_snapshot, publish_snapshot, resolve_snapshot, snapshot_lock

_CACHE = WorkspaceLoadCache(max_entries=24)


def load_price_sources(product, start, end, revision=None):
    def read():
        sources, errors = {}, []
        # Each candidate remains an independent time series. Never splice prices.
        try:
            frame = market_data.load_forward_curves(start,end,[product],force=True)
            sources['Shared final curve'] = frame.rename(columns={'trade_date':'cob_date','maturity_date':'contract_date','price':'settlement_price'})
        except Exception as exc:
            errors.append('Curve: '+safe_exception_message(exc))
        params={'start':start.date(),'end':end.date()}
        specification = SETTLEMENT_SOURCES.get(product)
        if specification:
            _,params['series'],params['source'] = specification
        query = """SELECT settlement_date AS cob_date,contract_month AS contract_date,settlement_price
                FROM at_lng.bbg_settlements WHERE series_type=:series AND source=:source
                AND field='PX_SETTLE' AND tenor_type='month' AND settlement_date BETWEEN :start AND :end"""
        if specification:
            try:
                with get_database_engine().connect() as db:
                    sources['Bloomberg PX_SETTLE']=pd.read_sql(text(query),db,params=params)
            except Exception as exc:
                errors.append('Bloomberg PX_SETTLE: '+safe_exception_message(exc))
        if product in ('Brent','HH'):
            params['commodity'] = 'BRENT' if product=='Brent' else 'LNE'
            query = """WITH candidates AS (
                SELECT snapshot_id,business_date,row_number() OVER (PARTITION BY business_date ORDER BY observed_at DESC,created_at DESC,snapshot_id DESC) AS rank
                FROM at_lng.vol_market_snapshots WHERE commodity=:commodity AND status='complete'
                AND source_name='Bloomberg option-chain settlement'
                AND COALESCE(metadata->>'snapshot_kind','SETTLEMENT')='SETTLEMENT'
                AND business_date BETWEEN :start AND :end)
                SELECT s.business_date AS cob_date,f.contract_date,f.forward AS settlement_price
                FROM candidates s JOIN at_lng.vol_market_forwards f USING(snapshot_id) WHERE s.rank=1"""
            label='Bloomberg settlement forwards'
        else:
            query=None
        if query:
            try:
                with get_database_engine().connect() as db:
                    sources[label]=pd.read_sql(text(query),db,params=params)
            except Exception as exc:
                errors.append(label+': '+safe_exception_message(exc))
        for frame in sources.values():
            for column in ('cob_date','contract_date'):
                frame[column]=pd.to_datetime(frame[column])
        return sources,errors
    return _CACHE.get_or_load((product,str(start.date()),str(end.date()),str(revision)),read,
        force_refresh=False,degraded=lambda value:bool(value[1]),healthy_ttl_seconds=300,degraded_ttl_seconds=10)


def build_comparison(product, contract, end_date, lookback, surface_frame, sources, errors=()):
    start,end,sessions,calendar=comparison_calendar(product,end_date,lookback)
    month=pd.Timestamp(contract)
    candidates=[]
    for priority,(label,frame) in enumerate(sources.items()):
        selected=frame.loc[frame.contract_date.eq(month)]
        result=evaluate_prices(selected,sessions)
        visible=result.loc[result.cob_date.ge(start)]
        # Prefer the greatest complete-window coverage, then latest observed price.
        latest=selected.cob_date.max() if not selected.empty else pd.Timestamp.min
        candidates.append((int(visible.realized_volatility.notna().sum()),latest,priority,label,result))
    if candidates:
        _,_,_,label,rows=max(candidates,key=lambda x:x[:3])
    else:
        label='Unavailable'
        rows=evaluate_prices(pd.DataFrame(columns=['cob_date','settlement_price']),sessions)
    implied=surface_frame.loc[surface_frame.code.eq(product)&surface_frame.contract_date.eq(month)
        &surface_frame.delta_bucket.eq('ATM')&surface_frame.put_call.eq('call'),
        ['cob_date','volatility','option_expiration_date']].copy()
    implied=implied.loc[implied.cob_date.between(start,end)]
    # Preserve independent IV even when no RV price is available.
    if implied.cob_date.duplicated().any():
        raise ValueError('Conflicting 50-delta call observations')
    rows=rows.loc[rows.cob_date.ge(start)].merge(implied,on='cob_date',how='outer',validate='one_to_one').sort_values('cob_date')
    expired=rows.cob_date.ge(rows.option_expiration_date)
    rows.loc[expired,'volatility']=np.nan
    rows['days_to_expiry']=(rows.option_expiration_date-rows.cob_date).dt.days
    rows['iv_source']='Operational surface · 50Δ call'
    rows['source_security']=label
    rows['spread']=rows.volatility-rows.realized_volatility
    rows['window_start']=rows.cob_date-pd.Timedelta(days=30)
    rows['window_end']=rows.cob_date
    fingerprint=hashlib.sha256(pd.util.hash_pandas_object(rows,index=False).values.tobytes()).hexdigest()
    dates=rows.loc[rows.settlement_price.notna(),'cob_date']
    last=dates.max().strftime('%Y-%m-%d') if len(dates) else 'unavailable'
    meta={'product':product,'contract':contract,'requested_start':start,'requested_end':end,
        'input_fingerprint':fingerprint,'policy':POLICY,'price_source':label,'last_price_date':last,
        'calendar':calendar.code,'calendar_version':calendar.version,'source_errors':list(errors),
        'source_coverage':{c[3]:c[0] for c in candidates}}
    meta['price_unit']={'Brent':'USD/bbl','HH':'USD/MMBtu','JKM':'USD/MMBtu','TTF':'EUR/MWh','NBP':'GBp/therm'}[product]
    if label=='Shared final curve':
        raw=sources[label].loc[sources[label].contract_date.eq(month)]
        if {'currency','units'}.issubset(raw.columns):
            units=raw[['currency','units']].drop_duplicates()
            if len(units)>1:
                raise ValueError('Historical curve units change within the selected contract')
            if len(units)==1:
                meta['price_unit']=str(units.iloc[0]['currency'])+'/'+str(units.iloc[0]['units'])
    return rows,meta


def load_comparison(product,contract,end_date,lookback,surface_frame,revision=None):
    # Capture the full supported range once per surface snapshot/product/COB.
    # Changing contract or lookback must not silently reread revised prices.
    _,end,sessions,_=comparison_calendar(product,end_date,90)
    price_reference=None
    if isinstance(revision,dict):
        resolve_snapshot(revision,expected_namespace='vol-surface-v2')
        identity=hashlib.sha256(json.dumps([POLICY,revision,product,str(end.date())],sort_keys=True,default=str).encode()).hexdigest()
        namespace='realized-prices-v1-'+identity
        with snapshot_lock(namespace):
            price_reference=latest_snapshot(namespace)
            if price_reference is None:
                payload=load_price_sources(product,sessions[0],end,revision)
                price_reference=publish_snapshot(namespace,identity,payload,group='realized-prices')
            sources,errors=resolve_snapshot(price_reference,expected_namespace=namespace)
    else:
        sources,errors=load_price_sources(product,sessions[0],end,revision)
    rows,meta=build_comparison(product,contract,end_date,lookback or 90,surface_frame,sources,errors)
    meta['price_snapshot_reference']=price_reference
    return rows,meta
