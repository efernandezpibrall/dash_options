"""The HH publication action must use the validated server-owned candidate."""

import base64
from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest

from dash import no_update
from snapshot_cache import SnapshotStore
from vol_calibration.pages import hh_governed as page


def _candidate():
    return {
        "commodity": "HH",
        "cob_date": "2026-09-25",
        "snapshot_id": "11111111-1111-1111-1111-111111111111",
        "surface": pd.DataFrame(
            {
                "contract_date": [pd.Timestamp("2026-11-01")] * 2,
                "option_expiration_date": [pd.Timestamp("2026-10-26")] * 2,
                "delta": [0.25, 0.75],
                "volatility": [0.30, 0.32],
            }
        ),
        "expiry_results": [
            {
                "option_expiration_date": "2026-10-26",
                "validation": {"is_valid": True},
                "diagnostics": {"quote_count": 2, "term_basis": "observed_fit"},
                "weighted_rmse": 0.001,
            }
        ],
        "input_manifest": {
            "source_snapshots": [{"snapshot_id": "11111111-1111-1111-1111-111111111111"}],
            "base_publication_id": None,
        },
        "point_count": 2,
        "expiry_count": 1,
    }


def test_hh_publish_reuses_server_candidate_and_rejects_tampered_browser_state(
    monkeypatch, tmp_path
):
    store = SnapshotStore(directory=tmp_path / "snapshots")
    calls = {"build": 0, "publish": 0, "source": 0}
    identity = SimpleNamespace(subject="operator-one")

    def build(*_args, **_kwargs):
        calls["build"] += 1
        return _candidate()

    def publish(_engine, surface, results, **kwargs):
        calls["publish"] += 1
        assert surface.equals(_candidate()["surface"])
        assert results == _candidate()["expiry_results"]
        assert kwargs["input_manifest"] == stored["input_manifest"]
        return {"publication_id": "published-id", "row_count": len(surface)}

    def verify_source(_engine, _candidate):
        calls["source"] += 1

    monkeypatch.setattr(page, "_identity", lambda: identity)
    monkeypatch.setattr(page, "build_hh_lne_candidate_surface", build)
    monkeypatch.setattr(page, "get_database_engine", lambda: object())
    monkeypatch.setattr(page, "publish_snapshot", store.publish)
    monkeypatch.setattr(page, "resolve_snapshot", store.resolve)
    monkeypatch.setattr(page, "_verify_candidate_source", verify_source)
    monkeypatch.setattr(page, "hh_publication_enabled", lambda: True)
    monkeypatch.setattr(page, "authorize", lambda *_args: None)
    monkeypatch.setattr(page, "publish_hybrid_surface", publish)

    try:
        browser, rows, _, disabled = page.calibrate_hh_governed(
            1, "2026-09-25", None, None
        )
        assert not disabled and len(rows) == 1
        assert "surface" not in browser and "expiry_results" not in browser
        assert calls["build"] == 1
        stored = page._resolve_candidate(browser, identity)
        second_worker = SnapshotStore(directory=tmp_path / "snapshots")
        try:
            assert second_worker.resolve(
                browser["candidate_ref"],
                expected_namespace=page._candidate_namespace(identity),
            )["surface"].equals(stored["surface"])
        finally:
            second_worker.close()

        figure = page.render_hh_candidate_comparison(
            browser, None, "2026-11"
        )
        assert list(figure.data[0].y) == [30.0, 32.0]
        download = page.export_hh_governed(
            1, browser, None, "2026-09-25"
        )
        workbook = pd.ExcelFile(BytesIO(base64.b64decode(download["content"])))
        assert workbook.sheet_names == [
            "Candidate Surface", "Diagnostics", "Provenance", "Raw Inputs"
        ]
        exported = pd.read_excel(workbook, sheet_name="Candidate Surface")
        assert exported["volatility"].tolist() == [0.30, 0.32]

        result, _, revision = page.publish_hh_governed(
            1, browser, None, ["confirmed"]
        )
        assert result["publication_id"] == "published-id"
        assert revision == {"commodity": "HH", "cob_date": "2026-09-25", "publication_id": "published-id"}
        assert calls == {"build": 1, "publish": 1, "source": 1}

        changed = dict(browser, input_fingerprint="forged")
        unchanged, alert, revision = page.publish_hh_governed(1, changed, None, ["confirmed"])
        assert unchanged is no_update and revision is no_update
        assert "blocked" in str(alert.children).lower()
        assert calls["publish"] == 1

        changed_surface = dict(stored)
        changed_surface["surface"] = stored["surface"].copy()
        changed_surface["surface"].loc[0, "volatility"] = 0.31
        monkeypatch.setattr(page, "resolve_snapshot", lambda *_args, **_kwargs: changed_surface)
        with pytest.raises(ValueError, match="candidate changed"):
            page._resolve_candidate(browser, identity)
        monkeypatch.setattr(page, "resolve_snapshot", store.resolve)

        other = SimpleNamespace(subject="operator-two")
        try:
            page._resolve_candidate(browser, other)
        except Exception as exc:
            assert "namespace" in str(exc).lower()
        else:
            raise AssertionError("Another identity resolved the HH candidate")

        store.cache.clear()
        try:
            page._resolve_candidate(browser, identity)
        except Exception as exc:
            assert "expired" in str(exc).lower()
        else:
            raise AssertionError("Missing HH candidate silently resolved")
    finally:
        store.close()


def test_hh_source_recheck_rejects_changed_expiry(monkeypatch):
    candidate = _candidate()
    source = {
        "snapshot_id": candidate["snapshot_id"],
        "source_revision": "revision-1",
        "observed_at": "2026-09-25T18:00:00+00:00",
        "option_quote_count": 2,
        "forward_count": 1,
        "chain_row_count": 2,
        "iv_resolved_count": 2,
    }
    candidate["input_manifest"]["source_snapshots"][0].update(
        revision=source["source_revision"],
        observed_at=source["observed_at"],
        option_quote_count=2,
        forward_count=1,
        raw_chain_count=2,
        resolved_iv_count=2,
    )
    options = pd.DataFrame(
        {"source_quote_id": ["option-1"], "settlement_price": [0.2]}
    )
    forwards = pd.DataFrame(
        {
            "source_quote_id": ["forward-1"],
            "strip": [pd.Timestamp("2026-11-01")],
            "settlement_price": [3.0],
        }
    )
    candidate["input_manifest"]["raw_observations"] = [
        {
            "source_quote_id": "option-1",
            "settlement_price": 0.2,
            "calibration_eligible": True,
            "exclusion_reason": None,
        }
    ]
    candidate["input_manifest"]["forwards"] = [
        {
            "source_quote_id": "forward-1",
            "strip": pd.Timestamp("2026-11-01"),
            "settlement_price": 3.0,
        }
    ]
    api = SimpleNamespace(
        resolve_snapshot=lambda *_args: SimpleNamespace(
            business_date=pd.Timestamp("2026-09-25").date()
        ),
        load_options=lambda *_args: options,
        load_forwards=lambda *_args: forwards,
        validate_rows=lambda *_args: None,
        resolve_expiries=lambda *_args: expiry,
        validate_expiries=lambda *_args: None,
    )
    monkeypatch.setattr(page.hh_calibration_module, "_builder_api", lambda: api)
    monkeypatch.setattr(
        page, "resolve_hh_lne_snapshot_reference", lambda *_args, **_kwargs: source
    )
    expiry = pd.DataFrame(
        {
            "maturity_date": [pd.Timestamp("2026-11-01")],
            "option_expiration_date": [pd.Timestamp("2026-10-26")],
        }
    )
    monkeypatch.setattr(page, "resolve_surface_expiry_metadata", lambda *_args: expiry)
    page._verify_candidate_source(object(), candidate)

    options.loc[0, "settlement_price"] = 0.21
    try:
        page._verify_candidate_source(object(), candidate)
    except ValueError as exc:
        assert "settlement rows changed" in str(exc)
    else:
        raise AssertionError("Changed LNE premium passed the HH source recheck")
    options.loc[0, "settlement_price"] = 0.2

    expiry.loc[0, "option_expiration_date"] = pd.Timestamp("2026-10-27")
    try:
        page._verify_candidate_source(object(), candidate)
    except ValueError as exc:
        assert "expiry reference changed" in str(exc)
    else:
        raise AssertionError("Changed expiry metadata passed the HH source recheck")
