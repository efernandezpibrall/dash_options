"""Brent workflow verification uses a disposable, migrated PostgreSQL."""
from copy import deepcopy
from uuid import uuid4
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from options.vol_calibration import api
from options.vol_calibration.intraday_brent import POLICY, VERSION
from options.vol_calibration.products.brent import calibrate_brent_single_surface
from options.vol_calibration.config.brent_sources import PRICING_MODEL, PRICING_MODEL_VERSION
from options.contracts.option_expiry_engine import calculate_option_expiry
from options.pricing.options_library import american_on_futures_futures_style_price


def fixture_inputs():
    now = pd.Timestamp.now(tz="UTC")
    day = now.tz_convert("Asia/Dubai").tz_localize(None).normalize()
    settlement = day-pd.Timedelta(days=1)
    market, targets, chain = [], [], []
    for offset in (3, 4):
        month = day+pd.offsets.MonthBegin(offset)
        expiry = pd.Timestamp(calculate_option_expiry("ICE_BRENT_218_EXPIRY", month.date()))
        years = (expiry-settlement).days/365.25
        for strike in np.linspace(75, 130, 23):
            k = np.log(strike/100.)
            iv = np.sqrt(.10+.03*(-.2*k+np.sqrt(k*k+.04)))
            side = "P" if strike < 100 else "C"
            market.append(dict(strip=month, strike_price=strike, forward=100., t=years,
                               iv=iv, contract_type=side, weight=1.))
            live_t = (expiry-day).days/365.25
            live_iv = iv+.04
            premium = american_on_futures_futures_style_price(side, 100., strike, live_t, live_iv, 400)
            chain.append(dict(underlying_contract_month=month.date(), option_expiration_date=expiry.date(),
                option_security=f"{month:%Y%m}-{strike}-{side}", put_call=side, strike=strike,
                option_bid=premium*.99, option_ask=premium*1.01, underlying_mid=100.,
                executable_iv_mid=live_iv, executable_iv_bid=live_iv-.004,
                executable_iv_ask=live_iv+.004, executable_iv_status="resolved",
                quote_response_at=now, quote_capture_skew_ms=100, currency="USD", price_unit="USD/BBL",
                pricing_model=PRICING_MODEL, pricing_model_version=PRICING_MODEL_VERSION))
        targets.append(dict(contract_date=month, option_expiration_date=expiry, forward=100.))
    model = calibrate_brent_single_surface(pd.DataFrame(market), pd.DataFrame(targets), settlement.date())
    surface, results = model.publication_candidate("Bloomberg Brent synthetic settlement")
    manifest = dict(commodity="BRENT", version=VERSION, policy=POLICY,
        implementation=api.implementation_fingerprint(), snapshot_id=str(uuid4()),
        snapshot_fingerprint="synthetic", business_date=str(day.date()), cutoff=now.isoformat(),
        window_start=(now-pd.Timedelta(hours=1)).isoformat(), base_publication_id=str(uuid4()),
        settlement_cob=str(settlement.date()), expected_intraday_id=None, evidence_hash="synthetic", base_hash="synthetic")
    return dict(manifest=manifest, chain=chain, base_surface=surface, base_results=results)


@pytest.fixture
def inputs():
    return fixture_inputs()


def persist_fixture(engine, inputs):
    manifest = inputs["manifest"]
    base = api.publish_hybrid_surface(engine, inputs["base_surface"], inputs["base_results"], commodity="BRENT",
        trading_date=manifest["settlement_cob"], settlement_cob=manifest["settlement_cob"], actor_subject="alice",
        created_by="alice", base_publication_id=None, expected_current_publication_id=None,
        idempotency_key="brent-test-base", return_surface=False)
    cutoff = pd.Timestamp.now(tz="UTC")
    with engine.begin() as c:
        c.execute(text("""INSERT INTO at_lng.vol_market_snapshots
            (snapshot_id,commodity,business_date,observed_at,source_name,input_fingerprint,status,option_quote_count,forward_count,metadata)
            VALUES (:id,'BRENT',:day,:at,'synthetic','synthetic','complete',46,2,'{"snapshot_kind":"INTRADAY"}')"""),
            dict(id=manifest["snapshot_id"], day=manifest["business_date"], at=cutoff))
        c.execute(text("""CREATE TABLE at_lng.bbg_option_chain (
            snapshot_id uuid, product text, underlying_contract_month date, option_expiration_date date,
            option_security text, put_call text, strike double precision, option_bid double precision,
            option_ask double precision, underlying_mid double precision, executable_iv_mid double precision,
            executable_iv_bid double precision, executable_iv_ask double precision, executable_iv_status text,
            quote_response_at timestamptz, quote_capture_skew_ms double precision, currency text, price_unit text,
            pricing_model text, pricing_model_version text)"""))
        for r in inputs["chain"]:
            row = {k: v.item() if isinstance(v, np.generic) else v for k, v in r.items()}
            row.update(snapshot_id=manifest["snapshot_id"], product="BRENT")
            c.execute(text("INSERT INTO at_lng.bbg_option_chain ("+",".join(row)+") VALUES ("+",".join(":"+k for k in row)+")"), row)
    return base, api.load_brent_intraday_inputs(engine, manifest["snapshot_id"])


def draft_for(engine, inputs, selected=None):
    candidate = api.fit_brent_intraday(inputs)
    eligible = [s["month"] for s in candidate["summaries"] if s["eligible"]]
    assert len(eligible) == 2, candidate["summaries"]
    draft = api.save_brent_intraday_draft(engine, candidate, selected or eligible[:1], actor="alice")
    return candidate, draft


def test_fit_reconstruction_and_sparse_stale_wrong_model_holds(inputs):
    candidate = api.fit_brent_intraday(inputs)
    assert len(api.validate_brent_intraday_candidate(candidate)) == 802
    changed = deepcopy(candidate); changed["surface"][0]["volatility"] += .001
    with pytest.raises(ValueError, match="identity"):
        api.validate_brent_intraday_candidate(changed)
    bad = deepcopy(inputs); bad["chain"] = bad["chain"][:7]
    assert not any(s["eligible"] for s in api.fit_brent_intraday(bad)["summaries"])
    for row in bad["chain"]:
        row["quote_response_at"] -= pd.Timedelta(minutes=10)
    held = api.fit_brent_intraday(bad)["summaries"][0]
    assert held["excluded"]["Stale or future quote capture"] == 7
    bad = deepcopy(inputs)
    for row in bad["chain"]: row["pricing_model"] = "black76"
    assert not any(s["eligible"] for s in api.fit_brent_intraday(bad)["summaries"])


def test_full_workflow_separate_settlement_retention_and_all_readers(database, inputs):
    base, loaded = persist_fixture(database, inputs)
    candidate, draft = draft_for(database, loaded)
    assert api.load_brent_intraday_draft(database, draft["draft_id"], actor="alice")["candidate"] == candidate
    assert api.save_brent_intraday_draft(database, candidate, draft["months"], actor="alice") == draft
    for fn, actor in ((api.load_brent_intraday_draft, "bob"), (api.load_tfo_intraday_draft, "alice")):
        with pytest.raises(ValueError, match="unavailable"):
            fn(database, draft["draft_id"], actor=actor)
    assert api.list_tfo_intraday_drafts(database, actor="alice") == []
    assert api.resolve_brent_intraday_publication(database, loaded["manifest"]["business_date"]) is None
    receipt = api.publish_brent_intraday_draft(database, draft["draft_id"], actor="alice")
    assert receipt["row_count"] == 802
    assert api.publish_brent_intraday_draft(database, draft["draft_id"], actor="alice")["publication_id"] == receipt["publication_id"]
    day = loaded["manifest"]["business_date"]
    assert api.resolve_brent_intraday_publication(database, day)["publication_id"] == receipt["publication_id"]
    assert api.resolve_tfo_intraday_publication(database, day) is None
    with database.connect() as c:
        active = c.execute(text("SELECT publication_id FROM at_lng.vol_surface_publications WHERE is_active")).scalars().all()
        assert [str(x) for x in active] == [base["publication_id"]]
        retained_month = candidate["summaries"][1]["month"]
        query = text("SELECT strike,delta,volatility,total_variance,working_forward,source_name FROM at_lng.implied_volatility_surface_calibrated WHERE publication_id=CAST(:id AS uuid) AND contract_date=:month ORDER BY strike")
        before = pd.read_sql(query, c, params=dict(id=base["publication_id"], month=retained_month))
        after = pd.read_sql(query, c, params=dict(id=receipt["publication_id"], month=retained_month))
        pd.testing.assert_frame_equal(before, after, check_exact=True)
    from dash_options.features.pricer.surface_reference import load_published_surface_slice
    from dash_options.features.vol_trades.data import load_latest_calibrated_surface
    from options.quotes.brent_quote_valuation import load_brent_market_snapshot, BrentOptionSpec
    month = draft["months"][0]
    assert load_published_surface_slice("BRENT", day, [month], engine=database)["publication_id"] == receipt["publication_id"]
    chart = load_latest_calibrated_surface(day, [month], engine=database, product="BRENT", intraday=True)
    assert chart.attrs["publication_metadata"]["publication_id"] == receipt["publication_id"]
    exact = load_latest_calibrated_surface(day, [month], engine=database, product="BRENT", intraday=False)
    assert exact.empty
    spec = BrentOptionSpec(pd.Timestamp(month).date(), "C", 100., pd.Timestamp.now(tz="UTC").to_pydatetime())
    market = load_brent_market_snapshot(database, spec)
    assert market.surface_publication_id == receipt["publication_id"]
    assert market.forward == 100.


def test_stale_source_corruption_and_settlement_lane_rejection(database, inputs):
    base, loaded = persist_fixture(database, inputs)
    candidate, draft = draft_for(database, loaded)
    with pytest.raises(ValueError, match="stale"):
        api.publish_brent_intraday_draft(database, draft["draft_id"], actor="alice", now=pd.Timestamp(loaded["manifest"]["cutoff"])+pd.Timedelta(minutes=31))
    with pytest.raises(api.HybridPublicationError, match="separate intraday"):
        api.publish_hybrid_surface(database, pd.DataFrame(candidate["surface"]), candidate["expiry_results"],
            commodity="BRENT", trading_date=loaded["manifest"]["business_date"], settlement_cob=loaded["manifest"]["settlement_cob"],
            actor_subject="alice", created_by="alice", base_publication_id=base["publication_id"],
            expected_current_publication_id=None, idempotency_key="invalid-settlement", input_manifest=candidate["manifest"])
    with database.begin() as c:
        c.execute(text("UPDATE at_lng.bbg_option_chain SET option_bid=option_bid+0.001"))
    with pytest.raises(ValueError, match="Source evidence"):
        api.publish_brent_intraday_draft(database, draft["draft_id"], actor="alice")
    fresh = api.load_brent_intraday_inputs(database, loaded["manifest"]["snapshot_id"])
    _, draft = draft_for(database, fresh)
    with database.begin() as c:
        c.execute(text("CREATE FUNCTION at_lng.corrupt_brent() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.volatility=NEW.volatility+0.001; RETURN NEW; END $$"))
        c.execute(text("CREATE TRIGGER corrupt_brent BEFORE INSERT ON at_lng.implied_volatility_surface_calibrated FOR EACH ROW EXECUTE FUNCTION at_lng.corrupt_brent()"))
    with pytest.raises(api.HybridPublicationError, match="readback differs"):
        api.publish_brent_intraday_draft(database, draft["draft_id"], actor="alice")
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM at_lng.vol_intraday_active")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar_one() == 1

def test_competing_publishers_and_reader_rollback(database, inputs, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.exc import DBAPIError
    base, loaded = persist_fixture(database, inputs)
    candidate, first = draft_for(database, loaded)
    second = api.save_brent_intraday_draft(database, candidate, [candidate["summaries"][1]["month"]], actor="bob")
    def publish(pair):
        draft, actor = pair
        try:
            return api.publish_brent_intraday_draft(database, draft["draft_id"], actor=actor)["publication_id"]
        except (ValueError, api.HybridPublicationError) as exc:
            return type(exc).__name__
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, [(first, "alice"), (second, "bob")]))
    successes = [x for x in outcomes if len(x) == 36]
    assert len(successes) == 1, outcomes
    day = loaded["manifest"]["business_date"]
    active = api.resolve_brent_intraday_publication(database, day)
    assert active["publication_id"] == successes[0]
    with pytest.raises(DBAPIError):
        with database.begin() as c:
            c.execute(text("UPDATE at_lng.vol_surface_publications SET is_active=true WHERE publication_id=CAST(:id AS uuid)"), {"id": successes[0]})
    monkeypatch.setenv("OPTIONS_BRENT_INTRADAY_READS_ENABLED", "0")
    assert api.resolve_brent_intraday_publication(database, day) is None
    from dash_options.features.pricer.surface_reference import load_published_surface_slice
    from options.quotes.brent_quote_valuation import resolve_brent_surface_publication
    assert load_published_surface_slice("BRENT", day, first["months"], engine=database)["publication_id"] == base["publication_id"]
    assert str(resolve_brent_surface_publication(database, pd.Timestamp.now(tz="UTC").to_pydatetime())["publication_id"]) == base["publication_id"]
    with pytest.raises(ValueError, match="disabled"):
        api.publish_brent_intraday_draft(database, first["draft_id"], actor="alice")


def test_new_settlement_invalidates_intraday_and_old_drafts(database, inputs):
    base, loaded = persist_fixture(database, inputs)
    candidate, draft = draft_for(database, loaded)
    receipt = api.publish_brent_intraday_draft(database, draft["draft_id"], actor="alice")
    day = loaded["manifest"]["business_date"]
    replacement = api.publish_hybrid_surface(database, inputs["base_surface"], inputs["base_results"], commodity="BRENT",
        trading_date=day, settlement_cob=day, actor_subject="settlement", created_by="settlement",
        base_publication_id=None, expected_current_publication_id=None, idempotency_key="new-settlement", return_surface=False)
    assert api.resolve_brent_intraday_publication(database, day) is None
    with pytest.raises(ValueError, match="no longer current"):
        api.publish_brent_intraday_draft(database, draft["draft_id"], actor="alice")
    from dash_options.features.pricer.surface_reference import load_published_surface_slice
    assert load_published_surface_slice("BRENT", day, draft["months"], engine=database)["publication_id"] == replacement["publication_id"]


def test_ui_exact_candidate_isolation_flags_and_old_publication_rejection(inputs, monkeypatch, tmp_path):
    import dash_options.features.vol_trades.intraday.brent_workspace as ui
    from dash_options.features.vol_trades.intraday.jobs import brent_jobs as jobs, ttf_jobs as tfo_jobs
    from dash_options.core.snapshots import SnapshotStore
    from dash_options.calibration.auth import Identity, Role
    from dash_options.features.vol_trades.market_window import history_identity
    from types import SimpleNamespace
    from dash import no_update
    store = SnapshotStore(tmp_path/"cache")
    monkeypatch.setattr(jobs, "snapshot_store", store)
    monkeypatch.setattr(tfo_jobs, "snapshot_store", store)
    candidate = api.fit_brent_intraday(inputs)
    history = dict(product="BRENT", snapshot_kind="INTRADAY", snapshot_id=inputs["manifest"]["snapshot_id"],
                   observed_at=inputs["manifest"]["cutoff"], business_date=inputs["manifest"]["business_date"])
    window = dict(history_key=history_identity(history), cutoff_at=inputs["manifest"]["cutoff"], start_at=inputs["manifest"]["window_start"])
    request = ui.market_request(history, window, "BRENT")
    assert request and ui.market_request(history, window, "BRENT", request) is no_update
    assert ui.market_request(history, window, "TFO") is None
    ref = jobs.retain(candidate, "alice")
    assert jobs.resolve(ref, "alice") == candidate
    for resolve, actor in ((jobs.resolve, "bob"), (tfo_jobs.resolve, "alice")):
        with pytest.raises(Exception): resolve(ref, actor)
    monkeypatch.setattr(ui, "identity", lambda: Identity("alice", frozenset({Role.CALIBRATOR, Role.PUBLISHER}), True, "test"))
    monkeypatch.setattr(ui, "calibration_enabled", lambda: True)
    monkeypatch.setattr(ui, "brent_intraday_writes_enabled", lambda: False)
    monkeypatch.setattr(ui, "brent_intraday_publication_enabled", lambda: True)
    view = dict(request_key=request["key"], ref=ref)
    rendered = ui.render(view, request, None)
    assert rendered[-1] and rendered[-2]
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="brent-intraday-publish-card"))
    result = ui.save_or_publish(0, 1, view, request, [candidate["summaries"][0]["month"]], None)
    assert "disabled" in result[1]
    assert ui.chart(None, view, None, request, False) == (no_update, no_update)
    import dash_options.calibration.workspaces.brent as legacy
    monkeypatch.setattr(legacy, "brent_publication_enabled", lambda: True)
    context = dict(market_snapshot_kind="INTRADAY", market_snapshot_id="a", market_as_of="b")
    assert legacy.enable_brent_publication(["confirmed"], dict(snapshot_id="a", market_as_of="b"), context)
    message, revision = legacy.publish_brent_candidate(1, ["confirmed"], {"a": 1}, context)
    assert "Use Review intraday" in str(message)
    assert revision is no_update
    store.cache.close()
