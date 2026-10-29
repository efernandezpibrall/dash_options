"""Read-only completeness evidence, including each missing contract/date."""
import argparse
import json
from pathlib import Path
import pandas as pd
import dash_options.shared.surfaces as surface_data
from dash_options.features.vol_surface.realized_comparison import load_price_sources, build_comparison
from options.market_data.realized_comparison import CALENDARS,comparison_calendar,evaluate_prices


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--end',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    surface,metadata=surface_data.load_surface_data()
    surface.to_pickle(args.output/'surface.pkl')
    summaries,observations,comparisons=[],[],[]
    for product in CALENDARS:
        start,end,sessions,calendar=comparison_calendar(product,args.end,90)
        sources,errors=load_price_sources(product,sessions[0],end)
        months=sorted(surface.loc[surface.code.eq(product)&surface.cob_date.eq(end),'contract_date'].unique())
        for label,frame in sources.items():
            frame.to_pickle(args.output/(product+'_'+label.replace(' ','_')+'.pkl'))
        for month in months:
            contract=pd.Timestamp(month).strftime('%Y-%m-%d')
            rows,meta=build_comparison(product,contract,args.end,90,surface,sources,errors)
            rows['product']=product
            rows['contract']=contract
            comparisons.append(rows)
            summaries.append({'product':product,'contract':contract,'source':meta['price_source'],
                'last_price':meta['last_price_date'],'rv_observations':int(rows.realized_volatility.notna().sum()),
                'iv_observations':int(rows.volatility.notna().sum()),'paired':int(rows.spread.notna().sum()),
                'latest_rv_valid':bool(rows.iloc[-1].realized_volatility==rows.iloc[-1].realized_volatility),
                'calendar':calendar.code,'source_errors':json.dumps(errors)})
            for label,frame in sources.items():
                daily=evaluate_prices(frame.loc[frame.contract_date.eq(month)],sessions)
                daily['product']=product
                daily['contract']=contract
                daily['source']=label
                observations.append(daily)
        print(product,'contracts',len(months),'errors',errors,flush=True)
    summary=pd.DataFrame(summaries)
    summary.to_csv(args.output/'contract_coverage.csv',index=False)
    pd.concat(observations,ignore_index=True).to_csv(args.output/'daily_source_coverage.csv',index=False)
    pd.concat(comparisons,ignore_index=True).to_csv(args.output/'daily_comparison_coverage.csv',index=False)
    print(summary.groupby('product').agg(contracts=('contract','size'),contracts_with_rv=('rv_observations',lambda x:int(x.gt(0).sum())),latest_rv_valid=('latest_rv_valid','sum'),paired=('paired','sum')).to_string())

if __name__=='__main__':
    main()
