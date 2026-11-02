"""Real SQL, but only against a disposable local PostgreSQL cluster."""

import json
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from options.vol_calibration import api
from options.vol_calibration.intraday_surface_reader import SHARING_DELTAS
from options.vol_calibration.intraday_tfo import identity


def dense_month(month, volatility):
    other = [d for d in np.linspace(0.015, 0.985, 430) if round(d, 8) not in SHARING_DELTAS][:390]
    return [
        {
            "contract_date": month,
            "option_expiration_date": "2027-01-25",
            "delta": float(d),
            "put_call": "C",
            "volatility": float(volatility),
            "working_forward": 45.0,
        }
        for d in [*SHARING_DELTAS, *other]
    ]


@pytest.fixture(params=["TTF", "BRENT"])
def product(request):
    return request.param


@pytest.fixture
def saved_revisions(blank_database, product):
    engine = blank_database
    base, first, second, draft = [str(uuid4()) for _ in range(4)]
    base_nodes = dense_month("2027-01-01", np.nextafter(0.5, 1)) + dense_month("2027-02-01", 0.6)
    changed = dense_month("2027-01-01", 0.54)
    # Another eligible month exists in the candidate but was NOT selected to save.
    payload = {
        "months": ["2027-01-01"],
        "candidate": {
            "manifest": {
                "commodity": product,
                "base_publication_id": base,
                "business_date": "2026-10-30",
                "cutoff": "2026-10-30T08:00:00Z",
            },
            "summaries": [{"month": m, "eligible": True} for m in ["2027-01-01", "2027-02-01"]],
            "surface": changed + dense_month("2027-02-01", 0.9),
        },
    }
    with engine.begin() as c:
        c.execute(text("CREATE SCHEMA at_lng"))
        c.execute(text("CREATE TABLE at_lng.vol_intraday_active (commodity text)"))
        c.execute(text("CREATE TABLE at_lng.vol_calibration_runs (run_id uuid PRIMARY KEY, configuration jsonb)"))
        c.execute(
            text("""CREATE TABLE at_lng.vol_surface_publications
            (publication_id uuid PRIMARY KEY,run_id uuid,commodity text,cob_date date,published_at timestamptz,status text)""")
        )
        c.execute(
            text("""CREATE TABLE at_lng.implied_volatility_surface_calibrated
            (publication_id uuid,commodity text,contract_date date,option_expiration_date date,
             delta double precision,put_call text,volatility double precision,working_forward double precision)""")
        )
        c.execute(
            text("""CREATE TABLE at_lng.vol_intraday_drafts
            (draft_id uuid,actor text,content_hash text,payload jsonb,created_at timestamptz DEFAULT now())""")
        )
        for pub, status, stamp, scope, nodes in [
            (base, "superseded", "2026-10-29T18:00Z", "settlement", base_nodes),
            (first, "superseded", "2026-10-30T06:00Z", "intraday", changed + base_nodes[401:]),
            (second, "published", "2026-10-30T07:00Z", "intraday", changed + base_nodes[401:]),
        ]:
            config = {
                "activation_scope": scope,
                "base_publication_id": base,
                "settlement_cob": "2026-10-29",
                "input_manifest": {"changed_months": ["2027-01-01"]},
            }
            c.execute(
                text("INSERT INTO at_lng.vol_calibration_runs VALUES (:id,CAST(:config AS jsonb))"),
                {"id": pub, "config": json.dumps(config)},
            )
            c.execute(
                text("INSERT INTO at_lng.vol_surface_publications VALUES (:id,:id,:product,:day,:stamp,:status)"),
                {
                    "id": pub,
                    "product": product,
                    "day": "2026-10-29" if scope == "settlement" else "2026-10-30",
                    "stamp": stamp,
                    "status": status,
                },
            )
            c.execute(
                text("""INSERT INTO at_lng.implied_volatility_surface_calibrated
                VALUES (:id,:product,:contract_date,:option_expiration_date,:delta,:put_call,:volatility,:working_forward)"""),
                [{**row, "id": pub, "product": product} for row in nodes],
            )
        c.execute(
            text(
                "INSERT INTO at_lng.vol_intraday_drafts (draft_id,actor,content_hash,payload) VALUES (:id,'alice',:hash,CAST(:payload AS jsonb))"
            ),
            {"id": draft, "hash": identity(payload), "payload": json.dumps(payload)},
        )
    return engine, base, first, second, draft


def test_history_reads_superseded_revision_and_original_anchor(saved_revisions, product):
    engine, base, first, second, _ = saved_revisions
    listed = api.list_intraday_surface_revisions(engine, commodity=product, source="intraday")
    assert [r["revision_id"] for r in listed] == [second, first]
    selected = api.load_intraday_surface_revision(engine, first, commodity=product, source="intraday")
    assert selected["base_publication_id"] == base
    assert len(selected["surface"]) == 22
    assert selected["base_surface"].iloc[0].volatility == np.nextafter(0.5, 1)
    assert selected["business_date"] == "2026-10-30"


def test_saved_draft_reuses_only_saved_months_and_original_precision(saved_revisions, product):
    engine, base, _, _, draft = saved_revisions
    selected = api.load_intraday_surface_revision(engine, draft, commodity=product, source="draft", actor="alice")
    monthly = selected["surface"].groupby("contract_date").volatility.first()
    assert monthly.loc[pd.Timestamp("2027-01-01")] == 0.54
    assert monthly.loc[pd.Timestamp("2027-02-01")] == 0.6
    assert selected["owner"] == "alice"
    assert selected["base_publication_id"] == base
    assert [
        r["revision_id"]
        for r in api.list_intraday_surface_revisions(engine, commodity=product, source="draft", actor="alice")
    ] == [draft]
    assert api.list_intraday_surface_revisions(engine, commodity=product, source="draft", actor="bob") == []
    with pytest.raises(ValueError, match="unavailable"):
        api.load_intraday_surface_revision(engine, draft, commodity=product, source="draft", actor="bob")
    with pytest.raises(PermissionError):
        api.load_intraday_surface_revision(engine, draft, commodity=product, source="draft")


def test_product_source_and_feature_flags_cannot_be_bypassed(saved_revisions, monkeypatch, product):
    engine, base, first, _, draft = saved_revisions
    other = "BRENT" if product == "TTF" else "TTF"
    for selected_product, source, revision in [
        (other, "intraday", first),
        (product, "intraday", base),
        (other, "draft", draft),
    ]:
        with pytest.raises(ValueError):
            api.load_intraday_surface_revision(
                engine, revision, commodity=selected_product, source=source, actor="alice"
            )
    monkeypatch.setenv(
        "OPTIONS_TFO_INTRADAY_READS_ENABLED" if product == "TTF" else "OPTIONS_BRENT_INTRADAY_READS_ENABLED", "0"
    )
    with pytest.raises(ValueError, match="unavailable"):
        api.load_intraday_surface_revision(engine, first, commodity=product, source="intraday")


def test_incomplete_persisted_grid_is_rejected(saved_revisions, product):
    engine, _, first, _, _ = saved_revisions
    with engine.begin() as c:
        c.execute(
            text("DELETE FROM at_lng.implied_volatility_surface_calibrated WHERE publication_id=:id AND delta=.5"),
            {"id": first},
        )
    with pytest.raises(ValueError, match="incomplete or invalid"):
        api.load_intraday_surface_revision(engine, first, commodity=product, source="intraday")


def test_tampered_saved_payload_is_rejected(saved_revisions, product):
    engine, _, _, _, draft = saved_revisions
    with engine.begin() as c:
        c.execute(
            text("UPDATE at_lng.vol_intraday_drafts SET payload=jsonb_set(payload,'{months}','[]') WHERE draft_id=:id"),
            {"id": draft},
        )
    with pytest.raises(ValueError, match="integrity"):
        api.load_intraday_surface_revision(engine, draft, commodity=product, source="draft", actor="alice")
