"""Shared operational IV/prices with separately labelled immutable reconstructions."""
from functools import lru_cache
import gzip
import hashlib
import json
from dash_options.core.paths import PACKAGE_ROOT

import numpy as np
import pandas as pd

import dash_options.features.vol_surface.realized_comparison as realized_comparison_data
from options.market_data.realized_comparison import CALENDARS, comparison_calendar

ARCHIVE = PACKAGE_ROOT / 'resources' / 'volatility_history_archive'
PRICE_UNITS = {'Brent':'USD/bbl','HH':'USD/MMBtu','JKM':'USD/MMBtu',
               'TTF':'EUR/MWh','NBP':'GBp/therm'}
POLICY = 'shared-fixed-month-delta-history-with-separated-archive-v1'


@lru_cache(maxsize=1)
def load_archive():
    manifest = json.loads((ARCHIVE/'manifest.json').read_text())
    content = gzip.decompress((ARCHIVE/'observations.json.gz').read_bytes())
    if hashlib.sha256(content).hexdigest() != manifest['sha256']:
        raise ValueError('Historical reconstruction archive checksum mismatch')
    payload = json.loads(content)
    if payload['schema_version'] != 1:
        raise ValueError('Unsupported historical archive schema')
    points = pd.DataFrame(payload['points'])
    runs = pd.DataFrame(payload['runs'])
    if len(points) != manifest['points'] or len(runs) != manifest['runs']:
        raise ValueError('Historical archive row count mismatch')
    if runs.run_id.duplicated().any() or not set(points.run_id).issubset(set(runs.run_id)):
        raise ValueError('Historical archive run identity mismatch')
    for column in ('cob_date','contract_date','option_expiration_date'):
        points[column] = pd.to_datetime(points[column])
    for column in ('delta','volatility','settlement_price','strike','total_variance'):
        points[column] = pd.to_numeric(points[column], errors='raise')
        if not np.isfinite(points[column]).all() or points[column].le(0).any():
            raise ValueError('Invalid archived reconstruction values')
    if (points.duplicated(['run_id','cob_date','contract_date','delta','put_call']).any()
            or not points.delta.lt(1).all()
            or not points.option_expiration_date.gt(points.cob_date).all()
            or not points['product'].isin(CALENDARS).all()):
        raise ValueError('Invalid archived reconstruction keys or expiry')
    # A scoped replacement run supersedes only its own contracts, not other months.
    chosen = {}
    for row in runs.sort_values(['end_date','created_at','run_id'],ascending=False).to_dict('records'):
        if row['status'] not in ('ready','complete'):
            continue
        for month in row['contracts']:
            chosen.setdefault((row['product'],pd.Timestamp(month)),row['run_id'])
    selected = points.loc[[chosen.get((r.product,r.contract_date)) == r.run_id
                           for r in points.itertuples()]].copy()
    selected['delta_bucket'] = (selected.delta*100).round().astype(int).astype(str)+'C'
    selected.loc[selected.delta.eq(.5),'delta_bucket'] = 'ATM'
    return selected, {'archive_sha256':manifest['sha256'],'runs':payload['runs']}


def available_contracts(product, end, lookback, surface):
    start,end,_,_ = comparison_calendar(product,end,lookback or 90)
    months = set(surface.loc[surface.code.eq(product)&surface.cob_date.between(start,end),'contract_date'])
    try:
        archive,_ = load_archive()
        months.update(archive.loc[archive['product'].eq(product)&archive.cob_date.between(start,end),'contract_date'])
    except (OSError,ValueError,KeyError):
        # The chart reports archive errors while operational data remains available.
        pass
    return sorted(pd.Timestamp(m).strftime('%Y-%m-%d') for m in months)


def load_history(product, contracts, end, lookback, surface, revision=None):
    start,end,sessions,calendar = comparison_calendar(product,end,lookback or 90)
    months = pd.to_datetime(list(dict.fromkeys(contracts)))
    published = surface.loc[surface.code.eq(product)&surface.contract_date.isin(months)
                            &surface.cob_date.between(start,end)].copy()
    # ATM is defined consistently as the 50-delta call, not an average of calls/puts.
    published = published.loc[~published.delta_bucket.eq('ATM')|published.put_call.eq('call')]
    if published.duplicated(['cob_date','contract_date','delta_bucket']).any():
        raise ValueError('Conflicting operational delta-history observations')
    published = published.loc[published.cob_date.lt(published.option_expiration_date)
                              &published.volatility.gt(0)&np.isfinite(published.volatility)]
    errors = []
    try:
        archive,meta = load_archive()
        archive = archive.loc[archive['product'].eq(product)&archive.contract_date.isin(months)
                              &archive.cob_date.between(start,end)].copy()
    except (OSError,ValueError,KeyError) as exc:
        archive = pd.DataFrame(columns=['cob_date','contract_date','delta_bucket','volatility'])
        meta = {}
        errors.append('Archived reconstructions unavailable: '+str(exc))
    prices,price_metadata = [],{}
    for month in months:
        rows,source = realized_comparison_data.load_comparison(
            product,month.strftime('%Y-%m-%d'),end,lookback,surface,revision)
        rows['contract_date'] = month
        prices.append(rows[['cob_date','contract_date','settlement_price','source_security']])
        price_metadata[month.strftime('%Y-%m-%d')] = source
        errors.extend(source.get('source_errors',[]))
    prices = pd.concat(prices,ignore_index=True) if prices else pd.DataFrame(
        columns=['cob_date','contract_date','settlement_price','source_security'])
    units = {m['price_unit'] for m in price_metadata.values() if m['last_price_date']!='unavailable'}
    price_unit = next(iter(units)) if len(units)==1 else PRICE_UNITS[product]
    if len(units)>1:
        # Never compare USD-converted curves with native quotes on one price axis.
        prices.loc[:,'settlement_price'] = np.nan
        errors.append('Settlement overlay omitted: selected sources have different price units.')
    return published,archive,prices,{**meta,'product':product,'contracts':list(contracts),
        'requested_start':start,'requested_end':end,'sessions':sessions[sessions>=start],
        'calendar':calendar.code,'calendar_version':calendar.version,'policy':POLICY,
        'prices':price_metadata,'price_unit':price_unit,'errors':list(dict.fromkeys(errors)),
        'iv_snapshot_reference':revision}


def rolling_schedule(surface, product, contract, end, lookback):
    """Exact expiry-based tenor membership, independent of missing daily quotes."""
    start, end, sessions, _ = comparison_calendar(product, end, lookback or 90)
    universe = surface.loc[surface.code.eq(product), ['contract_date', 'option_expiration_date']].dropna().drop_duplicates()
    # Ambiguous expiry definitions cannot establish a roll schedule.
    universe = universe.loc[~universe.contract_date.duplicated(keep=False)].sort_values('contract_date')
    current = universe.loc[universe.option_expiration_date.gt(end), 'contract_date'].tolist()
    if pd.Timestamp(contract) not in current:
        raise ValueError('Rolling history requires a contract unexpired on the selected COB')
    rank = current.index(pd.Timestamp(contract))
    rows=[]
    for day in sessions[sessions >= start]:
        eligible = universe.loc[universe.option_expiration_date.gt(day), 'contract_date'].tolist()
        rows.append({'cob_date':day, 'contract_date':eligible[rank] if rank < len(eligible) else pd.NaT})
    return pd.DataFrame(rows), rank+1


def load_rolling_comparison(product, contract, end, lookback, surface, revision=None):
    schedule, rank = rolling_schedule(surface, product, contract, end, lookback)
    frames, sources = [], {}
    for month in schedule.contract_date.dropna().unique():
        key = pd.Timestamp(month).strftime('%Y-%m-%d')
        rows, meta = realized_comparison_data.load_comparison(product, key, end, lookback, surface, revision)
        rows = rows.copy()
        rows['contract_date'] = month
        frames.append(rows)
        sources[key] = meta
    if not frames:
        raise ValueError('No contracts available for the selected rolling tenor')
    rows = schedule.merge(pd.concat(frames, ignore_index=True), on=['cob_date','contract_date'], how='left', validate='one_to_one')
    first = next(iter(sources.values()))
    observed = rows.loc[rows.settlement_price.notna(), 'cob_date']
    meta = {**first, 'contract':contract, 'tenor_rank':rank, 'history_mode':'rolling_tenor',
            'contract_sources':sources,
            'last_price_date':observed.max().strftime('%Y-%m-%d') if len(observed) else 'unavailable',
            'input_fingerprint':hashlib.sha256(pd.util.hash_pandas_object(rows,index=False).values.tobytes()).hexdigest()}
    meta['coverage'] = {**first['coverage'], 'sessions':len(rows), 'iv_expected':len(rows),
                        'iv_observed':int(rows.volatility.notna().sum()),
                        'rv_observed':int(rows.realized_volatility.notna().sum())}
    return rows, meta
