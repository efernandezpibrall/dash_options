import base64
import io
from copy import deepcopy

import pandas as pd
import pytest
from dash import _callback

from dash_options.core import snapshots
from dash_options.core.snapshots import SnapshotStore
from dash_options.calibration.auth import Identity, Role
from dash_options.features.vol_surface import selector, page
from dash_options.shared import surfaces


@pytest.fixture
def selected_sources(monkeypatch, tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    monkeypatch.setattr(snapshots, "snapshot_store", store)
    selector.CATALOG.clear()
    base = pd.DataFrame(
        [
            {
                "contract_date": pd.Timestamp(m),
                "option_expiration_date": pd.Timestamp("2026-12-25"),
                "delta": d,
                "volatility": v,
                "put_call": "C",
                "working_forward": 70.0,
            }
            for m, v in [("2027-01-01", 0.5), ("2027-02-01", 0.6)]
            for d in surfaces.PUBLISHED_SURFACE_DELTAS
        ]
    )
    data = {}
    for revision, source, bump in [("pub1", "intraday", 0.01), ("pub2", "intraday", 0.03), ("draft1", "draft", 0.04)]:
        frame = base.copy()
        frame.loc[frame.contract_date.eq("2027-01-01"), "volatility"] += bump
        data[revision] = {
            "commodity": "BRENT",
            "source": source,
            "revision_id": revision,
            "timestamp": "2026-10-30T07:00:00Z",
            "cutoff": "2026-10-30T06:58:00Z",
            "business_date": "2026-10-30",
            "base_publication_id": "base1",
            "settlement_cob": "2026-10-29",
            "changed_months": ["2027-01-01"],
            "surface": frame,
            "base_surface": base,
            "owner": "alice" if source == "draft" else None,
        }
    monkeypatch.setattr(selector, "get_database_engine", lambda **k: None)
    monkeypatch.setattr(
        selector, "current_request_identity", lambda: Identity("alice", frozenset({Role.VIEWER}), True, "test")
    )
    monkeypatch.setattr(
        selector.api,
        "list_intraday_surface_revisions",
        lambda engine, **k: [
            {"revision_id": key, "timestamp": value["timestamp"], "source": value["source"]}
            for key, value in reversed(data.items())
            if value["source"] == k["source"]
        ],
    )

    def load(engine, revision, **kwargs):
        assert kwargs["commodity"] == "BRENT"
        assert kwargs["source"] == data[revision]["source"]
        if kwargs["source"] == "draft":
            assert kwargs["actor"] == "alice"
        return deepcopy(data[revision])

    monkeypatch.setattr(selector.api, "load_intraday_surface_revision", load)
    yield data
    store.close()


def test_default_anchor_revision_pinning_and_comparison(selected_sources, monkeypatch):
    result = selector.select_revision("Brent", "intraday", None, None, None)
    assert result[1] == "pub2" and result[3] == "anchor"
    pinned = result[4]
    view = selector.resolve_view(pinned, "Brent", "intraday", "pub2", "anchor")
    assert view["current"].loc[view["current"].delta_bucket.eq("ATM"), "volatility"].tolist() == [0.53, 0.6]
    assert view["previous"].loc[view["previous"].delta_bucket.eq("ATM"), "volatility"].tolist() == [0.5, 0.6]
    assert view["metadata"]["month_origins"]["2027-02"] == "Inherited 2026-10-29"
    selected_sources["pub3"] = {**deepcopy(selected_sources["pub2"]), "revision_id": "pub3"}
    refreshed = selector.select_revision("Brent", "intraday", "pub2", "anchor", pinned, refresh=True)
    assert refreshed[1] == "pub2" and refreshed[4]["ref"] == pinned["ref"]
    compared = selector.select_revision("Brent", "intraday", "pub2", "pub1", pinned)
    view = selector.resolve_view(compared[4], "Brent", "intraday", "pub2", "pub1")
    assert view["metadata"]["comparison_id"] == "pub1"
    assert view["previous"].loc[view["previous"].delta_bucket.eq("ATM"), "volatility"].tolist() == [0.51, 0.6]
    # Both choices survive aging out of the bounded discovery list.
    monkeypatch.setattr(selector.api, "list_intraday_surface_revisions", lambda *a, **k: [])
    retained = selector.select_revision("Brent", "intraday", "pub2", "pub1", compared[4], refresh=True)
    assert retained[1:4:2] == ("pub2", "pub1")


def test_historical_draft_aliases_do_not_override_canonical_nodes(selected_sources):
    saved = selected_sources['draft1']['surface']
    saved['expiry'] = pd.Timestamp('1999-01-01')
    saved['iv'] = 9.99
    saved['commodity'] = 'HH'
    saved['cob_date'] = pd.Timestamp('1999-01-01')
    selected = selector.select_revision('Brent', 'draft', 'draft1', 'anchor', None)[4]
    view = selector.resolve_view(selected, 'Brent', 'draft', 'draft1', 'anchor')
    current = view['current']
    assert current.columns.is_unique
    assert current.code.eq('Brent').all()
    assert current.cob_date.eq(pd.Timestamp('2026-10-30')).all()
    assert sorted(current.contract_date.unique()) == list(pd.to_datetime(['2027-01-01', '2027-02-01']))
    assert current.loc[current.delta_bucket.eq('ATM'), 'volatility'].tolist() == [.54, .6]


def test_private_snapshot_rechecks_owner_product_selection_and_flag(selected_sources, monkeypatch):
    result = selector.select_revision("Brent", "draft", None, None, None)
    selected = result[4]
    assert selector.resolve_view(selected, "Brent", "draft")["owner"] == "alice"
    for product, source, revision in [
        ("TTF", "draft", "draft1"),
        ("Brent", "intraday", "draft1"),
        ("Brent", "draft", "other"),
    ]:
        with pytest.raises(selector.SelectionError):
            selector.resolve_view(selected, product, source, revision, "anchor")
    monkeypatch.setattr(
        selector, "current_request_identity", lambda: Identity("bob", frozenset({Role.VIEWER}), True, "test")
    )
    with pytest.raises(selector.SelectionError, match="trader"):
        selector.resolve_view(selected, "Brent", "draft")
    monkeypatch.setattr(selector, "current_request_identity", Identity.anonymous)
    with pytest.raises(selector.SelectionError, match="Sign in"):
        selector.resolve_view(selected, "Brent", "draft")
    monkeypatch.setenv("OPTIONS_BRENT_INTRADAY_READS_ENABLED", "0")
    with pytest.raises(selector.SelectionError, match="disabled"):
        selector.resolve_view(selected, "Brent", "draft")


def test_invalid_or_expired_reference_never_falls_back(selected_sources, monkeypatch):
    selection = selector.select_revision("Brent", "intraday", None, None, None)[4]
    monkeypatch.setattr(surfaces, "_get_surface_snapshot", lambda *a: pytest.fail("Unexpected settlement fallback"))
    with pytest.raises(selector.SelectionError):
        page._selected_surface_pair("Brent", "2026-10-29", None, "intraday", None, "pub2", "anchor")
    forged = deepcopy(selection)
    forged["ref"]["namespace"] = "another-page"
    with pytest.raises(selector.SelectionError, match="expired"):
        selector.resolve_view(forged, "Brent", "intraday")
    with pytest.raises(selector.SelectionError):
        selector.resolve_view(["malformed"], "Brent", "intraday")


def test_page_charts_matrix_export_use_same_revision_and_history_stays_daily(selected_sources, monkeypatch):
    selection = selector.select_revision("Brent", "draft", None, None, None)[4]
    monkeypatch.setattr(surfaces, "_ensure_cached_data", lambda *a: None)
    # Replaying an older fit must not add settlement smile overlays from its future.
    future = selector._normalize(selected_sources["pub2"]["surface"], "BRENT", "2026-10-31")
    monkeypatch.setattr(surfaces, "surface_dataset", future)
    history_calls = []

    def history(*args, **kwargs):
        history_calls.append((args, kwargs))
        return page._empty_figure("daily", "History")

    monkeypatch.setattr(page, "_create_delta_history_figure", history)
    result = page.update_surface_section(
        "daily-ref",
        "2026-11-01",
        "2026-10-28",
        "Brent",
        "month:2027-01-01",
        ["ATM"],
        30,
        "vs_previous",
        "fixed_expiry",
        [],
        selection,
        "draft",
        "draft1",
        "anchor",
    )
    heat, smile, _, tables, _ = result
    assert heat.data[0].z[0][0] == pytest.approx(0.04)
    assert heat.data[0].z[1][0] == 0
    assert "Private draft" in smile.data[0].name
    assert smile.data[0].y[0] == 0.54
    assert len(smile.data) == 2
    assert history_calls[0][0][4] == "2026-11-01"
    assert history_calls[0][1]["snapshot_reference"] == "daily-ref"
    grid = tables.children[0].children[1]
    assert grid.rowData[0]["origin"] == "Draft fit"
    assert grid.rowData[1]["origin"] == "Inherited 2026-10-29"
    exported = page.export_surface_table(
        1, "2026-10-29", "2026-10-28", "Brent", "month:2027-01-01", "daily-ref", selection, "draft", "draft1", "anchor"
    )
    book = pd.ExcelFile(io.BytesIO(base64.b64decode(exported["content"])))
    assert "Surface Selection" in book.sheet_names
    assert pd.read_excel(book, "Brent Surface")["ATM"].tolist() == [0.54, 0.6]
    assert "draft_draft1" in exported["filename"]


def test_selector_callback_is_registered_with_coherent_outputs():
    from dash_options.bootstrap import app

    callbacks = {**_callback.GLOBAL_CALLBACK_MAP, **app.callback_map}
    selector_keys = [key for key in callbacks if "surface-selector-status" in key]
    assert len(selector_keys) == 1
    entry = callbacks[selector_keys[0]]
    assert {i["id"] for i in entry["inputs"]} >= {
        "surface-source-dropdown",
        "surface-revision-dropdown",
        "surface-comparison-dropdown",
    }
