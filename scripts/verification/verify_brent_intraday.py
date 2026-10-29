"""Replay a saved Brent capture in an owned PostgreSQL, never in production."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.verification.verify_settlement_pipeline import temporary_postgres, apply_migrations
from options.vol_calibration import api
from options.brent_quote_valuation import BrentOptionSpec, load_brent_market_snapshot, QuotePrices, value_brent_quote
from dash_options.features.pricer.surface_reference import load_published_surface_slice
from dash_options.features.vol_trades.data import load_latest_calibrated_surface


def restore_capture(engine, inputs):
    manifest = inputs["manifest"]
    base = api.publish_hybrid_surface(engine, inputs["base_surface"], inputs["base_results"],
        commodity="BRENT", trading_date=manifest["settlement_cob"], settlement_cob=manifest["settlement_cob"],
        actor_subject="replay", created_by="replay", base_publication_id=None,
        expected_current_publication_id=None, idempotency_key="replay-settlement", return_surface=False)
    rows = inputs["chain"]
    # Restore every captured source field, including its exact JSON/float value.
    # Textual ISO dates remain textual here; the analytics loader parses them.
    columns = sorted(set().union(*(r.keys() for r in rows)))
    if any(not name.isidentifier() for name in columns):
        raise ValueError("Unsafe captured column identifier")
    types = {}
    for col in columns:
        value = next((r[col] for r in rows if r.get(col) is not None), None)
        types[col] = ("uuid" if col == "snapshot_id" else "boolean" if isinstance(value,bool)
            else "double precision" if isinstance(value,(float,int)) else "jsonb" if isinstance(value,(dict,list)) else "text")
    with engine.begin() as c:
        c.execute(text("""UPDATE at_lng.vol_surface_publications SET published_at=:at
            WHERE publication_id=CAST(:id AS uuid)"""),
            {"at": pd.Timestamp(manifest["cutoff"])-pd.Timedelta(minutes=1), "id": base["publication_id"]})
        c.execute(text("""INSERT INTO at_lng.vol_market_snapshots
            (snapshot_id,commodity,business_date,observed_at,source_name,input_fingerprint,status,option_quote_count,forward_count,metadata)
            VALUES (:id,'BRENT',:day,:at,'captured replay',:fingerprint,'complete',:count,:months,'{"snapshot_kind":"INTRADAY"}')"""),
            dict(id=manifest["snapshot_id"],day=manifest["business_date"],at=manifest["cutoff"],
                 fingerprint=manifest["snapshot_fingerprint"],count=len(rows),months=len({r["underlying_contract_month"] for r in rows})))
        c.execute(text("CREATE TABLE at_lng.bbg_option_chain ("+",".join('"'+k+'" '+types[k] for k in columns)+")"))
        binds = ",".join("CAST(:"+k+" AS jsonb)" if types[k]=="jsonb" else ":"+k for k in columns)
        statement = text("INSERT INTO at_lng.bbg_option_chain ("+",".join('"'+k+'"' for k in columns)+") VALUES ("+binds+")")
        c.execute(statement, [{k: json.dumps(r.get(k)) if types[k]=="jsonb" and r.get(k) is not None else r.get(k)
                               for k in columns} for r in rows])
    return base, api.load_brent_intraday_inputs(engine, manifest["snapshot_id"])


def verify(engine, inputs, ice_python=None):
    base, loaded = restore_capture(engine, inputs)
    candidate = api.fit_brent_intraday(loaded)
    eligible = [s["month"] for s in candidate["summaries"] if s["eligible"]]
    if not eligible:
        raise ValueError("Capture has no eligible Brent month")
    selected = eligible[:2]
    api.validate_brent_intraday_candidate(candidate)
    draft = api.save_brent_intraday_draft(engine, candidate, selected, actor="replay")
    assert api.load_brent_intraday_draft(engine,draft["draft_id"],actor="replay")["candidate"] == candidate
    # Replay checks the recorded capture's freshness window, not today's live eligibility.
    receipt = api.publish_brent_intraday_draft(engine,draft["draft_id"],actor="replay",
        now=pd.Timestamp(loaded["manifest"]["cutoff"])+pd.Timedelta(minutes=1))
    with engine.connect() as c:
        query = text("""SELECT contract_date,strike,delta,volatility,total_variance,working_forward,
            source_name,put_call,surface_region,blend_classification,calibration_basis,option_expiration_date
            FROM at_lng.implied_volatility_surface_calibrated WHERE publication_id=CAST(:id AS uuid)
            ORDER BY contract_date,strike""")
        before = pd.read_sql(query,c,params={"id":base["publication_id"]})
        after = pd.read_sql(query,c,params={"id":receipt["publication_id"]})
        retained_before = before[~before.contract_date.astype(str).isin(selected)].reset_index(drop=True)
        retained_after = after[~after.contract_date.astype(str).isin(selected)].reset_index(drop=True)
        pd.testing.assert_frame_equal(retained_before,retained_after,check_exact=True)
        assert [str(x) for x in c.execute(text("SELECT publication_id FROM at_lng.vol_surface_publications WHERE is_active")).scalars()] == [base["publication_id"]]
    day = loaded["manifest"]["business_date"]
    ids = {"publication":receipt["publication_id"]}
    ids["pricer"] = load_published_surface_slice("BRENT",day,selected,engine=engine)["publication_id"]
    chart = load_latest_calibrated_surface(day,selected,engine=engine,product="BRENT",intraday=True)
    ids["vol_trades"] = chart.attrs["publication_metadata"]["publication_id"]
    values = []
    for month in selected:
        forward = float(after[after.contract_date.astype(str).eq(month)].working_forward.iloc[0])
        spec = BrentOptionSpec(pd.Timestamp(month).date(),"C",forward,pd.Timestamp.now(tz="UTC").to_pydatetime())
        market = load_brent_market_snapshot(engine,spec)
        ids["ice_"+month] = market.surface_publication_id
        value = value_brent_quote(spec,QuotePrices(),market)
        assert np.isfinite([value.theoretical_price,value.delta,value.gamma,value.vega]).all()
        values.append(dict(month=month,forward=market.forward,volatility=value.our_volatility,premium=value.theoretical_price))
    if ice_python:
        import subprocess
        program = '''import json,sys
import pandas as pd
from sqlalchemy import create_engine
from options.brent_quote_valuation import BrentOptionSpec,load_brent_market_snapshot
v=json.load(sys.stdin)
e=create_engine(v['url'],connect_args={'options':'-c default_transaction_read_only=on'})
result={m:load_brent_market_snapshot(e,BrentOptionSpec(pd.Timestamp(m).date(),'C',100.,pd.Timestamp.now(tz='UTC').to_pydatetime())).surface_publication_id for m in v['months']}
print(json.dumps(result))
e.dispose()
'''
        result = subprocess.run([str(ice_python),'-I','-c',program], check=True, capture_output=True, text=True,
            input=json.dumps(dict(url=engine.url.render_as_string(hide_password=False),months=selected)))
        ids.update({'installed_ice_'+month:value for month,value in json.loads(result.stdout).items()})
    assert len(set(ids.values()))==1
    return dict(status="passed",clock_mode="recorded_capture_replay",snapshot_id=loaded["manifest"]["snapshot_id"],
        source_asof=loaded["manifest"]["cutoff"],settlement_cob=loaded["manifest"]["settlement_cob"],
        eligible_months=eligible,selected_months=selected,retained_months=int(retained_after.contract_date.nunique()),
        unchanged_retained_points=len(retained_after),total_points=receipt["row_count"],
        settlement_unchanged=True,reader_publication_ids=ids,valuations=values)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--capture",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--ice-python",type=Path)
    args=parser.parse_args()
    inputs=api.unpack_settlement(json.loads(args.capture.read_text()))
    with temporary_postgres() as engine:
        apply_migrations(engine)
        report=verify(engine,inputs,args.ice_python)
    args.output.write_text(json.dumps(report,indent=2,default=str))
    print(json.dumps(report,indent=2,default=str))


if __name__=="__main__":
    main()
