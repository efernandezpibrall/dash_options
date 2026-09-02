from __future__ import annotations

import pandas as pd
from options.calibration_engine.io import loaders

from pages import brent_vol_history as history
from vol_calibration import inline_workspace
from vol_calibration.ttf_publication import input_manifest_fingerprint


def _walk(component):
    yield component
    children = getattr(component, "children", None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if hasattr(child, "to_plotly_json"):
            yield from _walk(child)


def _snapshot(product, *, kind="SETTLEMENT", snapshot_id="snapshot-1"):
    return {
        "product": product,
        "business_date": "2026-08-28",
        "observed_at": "2026-08-28T19:00:00Z",
        "snapshot_id": snapshot_id,
        "snapshot_kind": kind,
        "source_revision": "revision-1",
    }


def test_layout_adds_only_stable_lazy_calibration_shell():
    components = list(_walk(history.layout))
    ids = {getattr(component, "id", None) for component in components}

    assert {
        "brent-vol-history-calibration-toggle",
        "brent-vol-history-calibration-panel",
        "brent-vol-history-calibration-context",
    }.issubset(ids)
    assert "vol-trades-inline-brent-publish" not in ids
    assert "hh-governed-calibrate" not in ids


def test_closed_panel_unmounts_without_resolving_calibration_context(monkeypatch):
    monkeypatch.setattr(
        history,
        "resolve_inline_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("closed panel must not execute a calibration loader")
        ),
    )

    children, context = history.render_inline_calibration(
        {"open": False}, _snapshot("BRENT"), "BRENT"
    )

    assert children == []
    assert context is None


def test_context_mapping_pins_exact_sources_and_one_shared_hh_publication(monkeypatch):
    publication_calls = []
    lne_calls = []

    def fake_publication(_engine, cob_date, *, commodity, as_of):
        publication_calls.append((cob_date, commodity, as_of))
        return {
            "publication_id": f"active-{commodity.lower()}",
            "published_at": "2026-08-28T18:00:00Z",
        }

    def fake_lne(_engine, cob_date, *, snapshot_id=None):
        lne_calls.append((cob_date, snapshot_id))
        return {"snapshot_id": snapshot_id or "same-cob-lne"}

    monkeypatch.setattr(
        inline_workspace, "load_latest_hybrid_publication", fake_publication
    )
    monkeypatch.setattr(
        inline_workspace, "resolve_hh_lne_snapshot_reference", fake_lne
    )

    brent = inline_workspace.resolve_inline_context(
        object(), _snapshot("BRENT", snapshot_id="brent-uuid"), "BRENT"
    )
    on = inline_workspace.resolve_inline_context(
        object(), _snapshot("ON", snapshot_id="on-uuid"), "ON"
    )
    lne = inline_workspace.resolve_inline_context(
        object(), _snapshot("LNE", snapshot_id="lne-uuid"), "LNE"
    )
    jkm = inline_workspace.resolve_inline_context(
        object(), _snapshot("JKM", kind="OFFICIAL_COB", snapshot_id=None), "JKM"
    )

    assert brent["calibration_source_id"] == "brent-uuid"
    assert on["calibration_source_id"] == "same-cob-lne"
    assert lne["calibration_source_id"] == "lne-uuid"
    assert on["publication_commodity"] == lne["publication_commodity"] == "HH"
    assert on["base_publication_id"] == lne["base_publication_id"] == "active-hh"
    assert jkm["calibration_source_id"] == "2026-08-28"
    assert jkm["market_snapshot_id"] is None
    assert lne_calls == [
        ("2026-08-28", None),
        ("2026-08-28", "lne-uuid"),
    ]
    assert [call[1] for call in publication_calls] == ["BRENT", "HH", "HH", "JKM"]


def test_lne_intraday_is_rejected_before_any_calibration_load(monkeypatch):
    monkeypatch.setattr(
        inline_workspace,
        "resolve_hh_lne_snapshot_reference",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("intraday LNE must fail before source resolution")
        ),
    )

    try:
        inline_workspace.resolve_inline_context(
            object(), _snapshot("LNE", kind="INTRADAY"), "LNE"
        )
    except ValueError as exc:
        assert "complete LNE SETTLEMENT" in str(exc)
    else:
        raise AssertionError("LNE intraday context was accepted")


def test_jkm_loader_is_exact_cob_and_never_requests_synthetic_fallback(monkeypatch):
    calls = []
    market = pd.DataFrame(
        {
            "expiry": [pd.Timestamp("2026-10-01")],
            "option_expiration_date": [pd.Timestamp("2026-09-24")],
            "delta": [0.5],
            "iv": [0.42],
            "strike": [12.0],
            "forward": [12.1],
            "source_name": ["ICAP official"],
        }
    )

    def fake_loader(product, cob_date, *, source, allow_synthetic_fallback):
        calls.append((product, cob_date, source, allow_synthetic_fallback))
        return {
            "data": market,
            "is_synthetic": False,
            "source": "postgres",
            "last_update": pd.Timestamp("2026-08-28T18:00:00Z"),
        }

    monkeypatch.setattr(loaders, "load_market_data_with_metadata", fake_loader)

    loaded, metadata = history.load_jkm_official_market("2026-08-28")

    assert len(loaded) == 1
    assert metadata["source"] == "postgres"
    assert calls == [("JKM", pd.Timestamp("2026-08-28").date(), "icap", False)]


def test_same_day_publication_concurrency_excludes_older_point_in_time_base():
    older = {
        "publication_id": "prior-revision",
        "publication_date": "2026-08-27",
    }
    same_day = {
        "publication_id": "same-day-revision",
        "publication_date": "2026-08-28",
    }

    assert inline_workspace._same_day_publication_id(older, "2026-08-28") is None
    assert (
        inline_workspace._same_day_publication_id(same_day, "2026-08-28")
        == "same-day-revision"
    )


def test_manifest_fingerprint_is_deterministic_and_tracks_raw_inputs():
    first = {
        "commodity": "HH",
        "source_snapshots": [{"snapshot_id": "lne-1", "revision": "r1"}],
        "raw_observations": [{"source_quote_id": "q1", "iv": 0.42}],
    }
    reordered = {
        "raw_observations": [{"iv": 0.42, "source_quote_id": "q1"}],
        "source_snapshots": [{"revision": "r1", "snapshot_id": "lne-1"}],
        "commodity": "HH",
    }
    changed = {
        **first,
        "raw_observations": [{"source_quote_id": "q1", "iv": 0.43}],
    }

    assert input_manifest_fingerprint(first) == input_manifest_fingerprint(reordered)
    assert input_manifest_fingerprint(first) != input_manifest_fingerprint(changed)
