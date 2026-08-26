from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from options.ttf_volatility import STANDARD_CALL_DELTAS, delta_node_to_strike
from vol_calibration.auth import Identity, Role
from vol_calibration.ttf_adjustments import apply_ttf_smile_adjustments
from vol_calibration.ttf_hybrid_surface import build_ttf_pchip_core
from vol_calibration.ttf_intraday import normalize_ttf_intraday_trade
from vol_calibration.ttf_market_context import load_ttf_trading_context
from vol_calibration.ttf_publication import (
    TTFPublicationError,
    _copy_surface_points,
    load_latest_ttf_publication,
    normalize_ttf_publication_surface,
    publish_ttf_surface,
)
from vol_calibration import ttf_publication
from vol_calibration.pages.ttf import (
    _changed_ttf_tail_overrides,
    populate_ttf_expiry_controls,
)
from vol_calibration.pages import ttf as ttf_page


def _observations():
    deltas = STANDARD_CALL_DELTAS
    ivs = np.asarray([0.82, 0.69, 0.62, 0.58, 0.55, 0.54, 0.55, 0.57, 0.61, 0.68, 0.80])
    forward = 40.0
    dte = 90.0
    strikes = [
        delta_node_to_strike(forward, dte / 365.25, delta, iv)
        for delta, iv in zip(deltas, ivs)
    ]
    return pd.DataFrame(
        {
            "expiry": pd.Timestamp("2026-10-01"),
            "option_expiration_date": pd.Timestamp("2026-09-25"),
            "dte": dte,
            "delta": deltas,
            "delta_convention": "undiscounted_call_delta",
            "iv": ivs,
            "strike": strikes,
            "forward": forward,
            "rate": 0.0,
            "source_name": "official",
            "quote_class": "observed",
            "calibration_basis": "observed",
            "weight": 1.0,
        }
    )


def test_trading_context_uses_prior_settlement_but_rolls_dte_to_trading_date():
    source = _observations().assign(dte=57.0)

    def snapshot_loader(product, requested, refresh=False):
        assert product == "TTF"
        assert str(requested) == "2026-08-06"
        assert refresh is True
        return {
            "actual_cob": pd.Timestamp("2026-07-30"),
            "source": "at_lng.implied_volatility_surface_from_prices",
        }

    calls = []

    def market_loader(product, cob_date, allow_synthetic_fallback):
        calls.append((product, cob_date, allow_synthetic_fallback))
        return {
            "data": source,
            "source": "postgres",
            "last_update": pd.Timestamp("2026-07-30T20:00:00Z"),
            "error": None,
        }

    context = load_ttf_trading_context(
        "2026-08-06",
        refresh=True,
        snapshot_loader=snapshot_loader,
        market_loader=market_loader,
    )

    assert context["trading_date"] == "2026-08-06"
    assert context["settlement_cob"] == "2026-07-30"
    assert context["date_fallback_used"] is True
    assert calls == [("TTF", pd.Timestamp("2026-07-30").date(), False)]
    assert set(context["market_data"]["settlement_dte"]) == {57.0}
    assert set(context["market_data"]["dte"]) == {50.0}


def _publication_payload(observations, *, publication_date="2026-07-30"):
    points = pd.DataFrame(
        {
            "contract_date": observations["expiry"],
            "option_expiration_date": observations["option_expiration_date"],
            "delta": observations["delta"],
            "volatility": observations["iv"] + 0.01,
            "strike": observations["strike"],
            "working_forward": 35.0,
            "total_variance": (observations["iv"] + 0.01) ** 2 * 90 / 365.25,
            "surface_region": "core",
            "blend_classification": "pchip_core",
            "calibration_basis": "observed",
            "source_name": "official",
        }
    )
    params = ttf_page.get_defaults("TTF")
    params.update({"left_blend_width": 0.15, "right_blend_width": 0.20})
    return {
        "publication_id": "publication-30jul",
        "publication_date": publication_date,
        "published_at": "2026-08-06T08:00:00+00:00",
        "expiry_results": [
            {
                "option_expiration_date": "2026-09-25",
                "parameters": params,
                "diagnostics": {
                    "core_tv_rmse": 0.0,
                    "tail_fit_tv_rmse": 0.001,
                    "iv_rmse": 0.002,
                },
                "validation": {"is_valid": True},
            }
        ],
        "data": points.to_json(date_format="iso", orient="split"),
    }


def test_latest_publication_rolls_only_iv_shape_to_current_market_inputs():
    settlement = _observations().assign(forward=55.0, dte=50.0)
    payload = _publication_payload(settlement)

    base = ttf_page._base_ttf_observations(
        settlement,
        "2026-10-01",
        payload,
    )

    assert set(base["forward"]) == {55.0}
    assert set(base["dte"]) == {50.0}
    np.testing.assert_allclose(base["iv"], settlement["iv"] + 0.01)
    expected_strikes = [
        delta_node_to_strike(55.0, 50.0 / 365.25, delta, iv)
        for delta, iv in zip(base["delta"], base["iv"])
    ]
    np.testing.assert_allclose(base["strike"], expected_strikes)
    assert ttf_page.prefill_ttf_working_forward(
        "2026-10-01",
        settlement.to_json(date_format="iso", orient="split"),
    ) == pytest.approx(55.0)


def test_publication_candidate_separates_batch_and_manual_targets(monkeypatch):
    market = _observations()
    calls = []
    core = SimpleNamespace(
        strike_nodes=market["strike"].to_numpy(dtype=float),
        iv_nodes=market["iv"].to_numpy(dtype=float),
    )

    def fake_settlement(market_data, expiry):
        del market_data, expiry
        calls.append("settlement")
        return market.copy()

    def fake_published(market_data, expiry, publication_payload):
        del market_data, expiry, publication_payload
        calls.append("published")
        return market.assign(iv=market["iv"] + 0.01)

    def fake_evaluate(observations, table_row):
        del observations
        return {
            "core": core,
            "params": dict(table_row),
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.002,
            "left_blend_width": 0.10,
            "right_blend_width": 0.10,
            "validation": {"is_valid": True},
        }

    monkeypatch.setattr(ttf_page, "_settlement_ttf_observations", fake_settlement)
    monkeypatch.setattr(ttf_page, "_base_ttf_observations", fake_published)
    monkeypatch.setattr(
        ttf_page,
        "_apply_node_edits",
        lambda observations, node_store, expiry=None: observations,
    )
    monkeypatch.setattr(
        ttf_page,
        "_select_ttf_expiry_inputs",
        lambda observations, expiry: observations,
    )
    monkeypatch.setattr(ttf_page, "_evaluate_existing_hybrid", fake_evaluate)
    monkeypatch.setattr(
        ttf_page,
        "hybrid_iv",
        lambda *args, **kwargs: core.iv_nodes.copy(),
    )
    monkeypatch.setattr(
        ttf_page,
        "ttf_hybrid_operational_surface_frame",
        lambda *args, **kwargs: pd.DataFrame({"iv": [0.5]}),
    )
    table_row = {
        **ttf_page.get_defaults("TTF"),
        "left_blend_width": 0.10,
        "right_blend_width": 0.10,
    }

    _, batch_result = ttf_page._publication_candidate_for_expiry(
        market,
        table_row,
        "2026-10-01",
        {},
        {},
        {"publication_id": "prior"},
        calibration_target=ttf_page.TTF_BATCH_CALIBRATION_TARGET,
    )
    _, manual_result = ttf_page._publication_candidate_for_expiry(
        market,
        table_row,
        "2026-10-01",
        {},
        {},
        {"publication_id": "prior"},
        calibration_target=ttf_page.TTF_INTRADAY_CALIBRATION_TARGET,
    )

    assert calls == ["settlement", "published"]
    assert (
        batch_result["diagnostics"]["calibration_target"]
        == ttf_page.TTF_BATCH_CALIBRATION_TARGET
    )
    assert (
        manual_result["diagnostics"]["calibration_target"]
        == ttf_page.TTF_INTRADAY_CALIBRATION_TARGET
    )


def test_publication_candidate_rejects_node_reproduction_failure(monkeypatch):
    market = _observations()
    core = build_ttf_pchip_core(market)
    result = {
        "core": core,
        "params": ttf_page.get_defaults("TTF"),
        "core_tv_rmse": 0.0,
        "tail_fit_tv_rmse": 0.001,
        "iv_rmse": 0.002,
        "left_blend_width": 0.10,
        "right_blend_width": 0.10,
        "validation": {"is_valid": True},
    }
    monkeypatch.setattr(
        ttf_page,
        "_settlement_ttf_observations",
        lambda market_data, expiry: market.copy(),
    )
    monkeypatch.setattr(
        ttf_page,
        "_apply_node_edits",
        lambda observations, node_store, expiry=None: observations,
    )
    monkeypatch.setattr(
        ttf_page,
        "_select_ttf_expiry_inputs",
        lambda observations, expiry: observations,
    )
    monkeypatch.setattr(
        ttf_page,
        "_evaluate_existing_hybrid",
        lambda observations, table_row: result,
    )
    monkeypatch.setattr(
        ttf_page,
        "hybrid_iv",
        lambda *args, **kwargs: core.iv_nodes + 0.001,
    )

    with pytest.raises(ValueError, match="does not reproduce"):
        ttf_page._publication_candidate_for_expiry(
            market,
            ttf_page.get_defaults("TTF"),
            "2026-10-01",
            {},
            {},
            {},
            calibration_target=ttf_page.TTF_BATCH_CALIBRATION_TARGET,
        )


def test_published_surface_and_tail_parameters_are_rebased_for_today():
    settlement = _observations().assign(forward=55.0, dte=50.0)
    payload = _publication_payload(settlement)
    table_rows = [{"expiry": "Oct-26", **ttf_page.get_defaults("TTF")}]

    updated = ttf_page._apply_published_parameters(
        table_rows,
        settlement,
        payload,
    )
    rebased = ttf_page._published_surface_for_market(payload, settlement)

    assert updated[0]["left_blend_width"] == pytest.approx(0.15)
    assert updated[0]["right_blend_width"] == pytest.approx(0.20)
    assert updated[0]["tail_fit_tv_rmse"] == pytest.approx(0.001)
    assert updated[0]["arb_status"] == "Pass"
    assert set(rebased["working_forward"]) == {55.0}
    np.testing.assert_allclose(
        rebased["total_variance"],
        50.0 / 365.25 * rebased["volatility"] ** 2,
    )


def test_same_day_concurrency_id_is_separate_from_older_base_publication():
    payload = _publication_payload(_observations())

    assert ttf_page._same_day_publication_id(payload, "2026-08-06") is None
    assert (
        ttf_page._same_day_publication_id(payload, "2026-07-30")
        == "publication-30jul"
    )


def test_post_commit_readback_can_select_historical_publication_by_id(monkeypatch):
    publication_id = "1ed4d849-c5fe-4b9a-bfe5-ca307ca3d03d"
    run_id = "84f08398-fbe7-436c-bdf8-84f2ebfc8163"
    calls = []

    class Result:
        def __init__(self, values):
            self.values = values

        def mappings(self):
            return self

        def first(self):
            return self.values[0] if self.values else None

        def all(self):
            return self.values

    class Connection:
        def execute(self, statement, params=None):
            sql = str(statement)
            calls.append((sql, params))
            if "SELECT p.publication_id" in sql:
                return Result(
                    [
                        {
                            "publication_id": publication_id,
                            "run_id": run_id,
                            "cob_date": pd.Timestamp("2026-07-30"),
                            "published_at": pd.Timestamp("2026-08-10T06:13:53Z"),
                            "published_by": "publisher@example.com",
                            "configuration": {},
                        }
                    ]
                )
            return Result([])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Engine:
        def connect(self):
            return Connection()

    points = pd.DataFrame(
        {
            "publication_id": [publication_id],
            "run_id": [run_id],
            "contract_date": [pd.Timestamp("2026-10-01")],
        }
    )
    monkeypatch.setattr(
        ttf_publication,
        "ttf_publication_storage_available",
        lambda engine: True,
    )
    monkeypatch.setattr(ttf_publication.pd, "read_sql", lambda *args, **kwargs: points)

    payload = load_latest_ttf_publication(
        Engine(),
        "2026-07-30",
        publication_id=publication_id,
    )

    assert payload["publication_id"] == publication_id
    assert payload["row_count"] == 1
    assert "CAST(:publication_id AS uuid)" in calls[0][0]
    assert calls[0][1] == {"publication_id": publication_id}


def test_calibration_review_prefers_active_exact_cob_before_pit_fallback(
    monkeypatch,
):
    publication_id = "139aa83d-775c-4de4-abad-3967dc393730"
    run_id = "84f08398-fbe7-436c-bdf8-84f2ebfc8163"
    calls = []

    class Result:
        def __init__(self, values):
            self.values = values

        def mappings(self):
            return self

        def first(self):
            return self.values[0] if self.values else None

        def all(self):
            return self.values

    class Connection:
        def execute(self, statement, params=None):
            sql = str(statement)
            calls.append((sql, params))
            if "SELECT p.publication_id" in sql:
                return Result(
                    [
                        {
                            "publication_id": publication_id,
                            "run_id": run_id,
                            "cob_date": pd.Timestamp("2026-08-21"),
                            "published_at": pd.Timestamp("2026-08-24T12:30:20Z"),
                            "published_by": "publisher@example.com",
                            "configuration": {},
                        }
                    ]
                )
            return Result([])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Engine:
        def connect(self):
            return Connection()

    points = pd.DataFrame(
        {
            "publication_id": [publication_id],
            "run_id": [run_id],
            "contract_date": [pd.Timestamp("2026-10-01")],
        }
    )
    monkeypatch.setattr(
        ttf_publication,
        "ttf_publication_storage_available",
        lambda engine: True,
    )
    monkeypatch.setattr(ttf_publication.pd, "read_sql", lambda *args, **kwargs: points)

    payload = load_latest_ttf_publication(
        Engine(),
        "2026-08-21",
        as_of=datetime(2026, 8, 21, 23, 59, tzinfo=timezone.utc),
        prefer_exact_cob=True,
    )

    sql, params = calls[0]
    assert payload["publication_id"] == publication_id
    assert "p.cob_date = :trading_date" in sql
    assert "p.cob_date < :trading_date AND p.published_at <= :as_of" in sql
    assert "CASE WHEN p.cob_date = :trading_date THEN 0 ELSE 1 END" in sql
    assert params["trading_date"] == pd.Timestamp("2026-08-21").date()


def test_ttf_page_requests_exact_cob_publication_for_review(monkeypatch):
    captured = {}

    def fake_load(engine, trading_date, **kwargs):
        captured.update(engine=engine, trading_date=trading_date, kwargs=kwargs)
        return {
            "publication_id": "publication-21aug",
            "publication_date": "2026-08-21",
            "published_at": "2026-08-24T12:30:20Z",
            "expiry_count": 64,
        }

    monkeypatch.setattr(ttf_page, "get_database_engine", lambda: "engine")
    monkeypatch.setattr(ttf_page, "load_latest_ttf_publication", fake_load)

    payload, _ = ttf_page.load_ttf_publication("2026-08-21", 0)

    assert payload["publication_id"] == "publication-21aug"
    assert captured == {
        "engine": "engine",
        "trading_date": pd.Timestamp("2026-08-21").date(),
        "kwargs": {"prefer_exact_cob": True},
    }


def test_toolbar_save_publishes_a_complete_successful_batch(monkeypatch):
    market = pd.DataFrame({"expiry": [pd.Timestamp("2026-10-01")]})
    rows = [
        {
            "expiry": "Oct-26",
            **ttf_page.get_defaults("TTF"),
            "calibration_basis": "Observed",
            "left_blend_width": 0.1,
            "right_blend_width": 0.1,
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.01,
            "arb_status": "Pass",
            "calibration_method": ttf_page.TTF_HYBRID_METHOD,
            "calibration_policy_version": ttf_page.TTF_HYBRID_POLICY_VERSION,
        }
    ]
    results = [{"expiry": "2026-10-01", "status": "Success"}]
    market_json = market.to_json(date_format="iso", orient="split")
    batch = ttf_page._build_ttf_batch_state(
        "2026-07-30", market_json, rows, {}, {}, results
    )
    identity = Identity(
        subject="publisher@example.com",
        roles=frozenset({Role.PUBLISHER}),
        authenticated=True,
        auth_source="test",
    )
    captured = {}

    monkeypatch.setattr(
        ttf_page,
        "ctx",
        SimpleNamespace(triggered_id="ttf-save-all-btn"),
    )
    monkeypatch.setattr(ttf_page, "ttf_publication_enabled", lambda: True)
    monkeypatch.setattr(ttf_page, "_current_identity", lambda: identity)
    monkeypatch.setattr(ttf_page, "get_database_engine", lambda: "engine")
    monkeypatch.setattr(
        ttf_page,
        "_publication_candidate_for_expiry",
        lambda *args, **kwargs: (
            pd.DataFrame({"contract_date": [pd.Timestamp("2026-10-01")]}),
            {"option_expiration_date": "2026-09-25"},
        ),
    )

    def fake_publish(engine, surface, results, **kwargs):
        captured.update(
            engine=engine,
            surface=surface,
            results=results,
            kwargs=kwargs,
        )
        return {
            "publication_date": "2026-07-30",
            "published_at": "2026-08-07T12:00:00Z",
            "publication_id": "publication-1",
            "expiry_count": 1,
            "row_count": 1,
        }

    monkeypatch.setattr(ttf_page, "publish_ttf_surface", fake_publish)

    result = ttf_page.publish_ttf_intraday_surface(
        1,
        None,
        "2026-07-30",
        {"settlement_cob": "2026-07-30"},
        {},
        market_json,
        rows,
        {},
        {},
        {"trades": []},
        batch,
    )

    assert captured["engine"] == "engine"
    assert captured["kwargs"]["created_by"] == "publisher@example.com"
    assert list(captured["kwargs"]["expected_expiries"]) == [
        pd.Period("2026-10", freq="M")
    ]
    assert result[0]["publication_id"] == "publication-1"
    assert result[-1] == []


def test_toolbar_save_rejects_stale_batch_before_building_or_writing(monkeypatch):
    market = pd.DataFrame({"expiry": [pd.Timestamp("2026-10-01")]})
    market_json = market.to_json(date_format="iso", orient="split")
    rows = [
        {
            "expiry": "Oct-26",
            **ttf_page.get_defaults("TTF"),
            "calibration_basis": "Observed",
            "left_blend_width": 0.1,
            "right_blend_width": 0.1,
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.01,
            "arb_status": "Pass",
            "calibration_method": ttf_page.TTF_HYBRID_METHOD,
            "calibration_policy_version": ttf_page.TTF_HYBRID_POLICY_VERSION,
        }
    ]
    stale = ttf_page._build_ttf_batch_state(
        "2026-07-29",
        market_json,
        rows,
        {},
        {},
        [{"expiry": "2026-10-01", "status": "Success"}],
    )
    identity = Identity(
        subject="publisher@example.com",
        roles=frozenset({Role.PUBLISHER}),
        authenticated=True,
        auth_source="test",
    )
    monkeypatch.setattr(
        ttf_page,
        "ctx",
        SimpleNamespace(triggered_id="ttf-save-all-btn"),
    )
    monkeypatch.setattr(ttf_page, "ttf_publication_enabled", lambda: True)
    monkeypatch.setattr(ttf_page, "_current_identity", lambda: identity)
    monkeypatch.setattr(
        ttf_page,
        "_publication_candidate_for_expiry",
        lambda *args, **kwargs: pytest.fail("stale batch built publication points"),
    )
    monkeypatch.setattr(
        ttf_page,
        "publish_ttf_surface",
        lambda *args, **kwargs: pytest.fail("stale batch reached the database"),
    )

    output = ttf_page.publish_ttf_intraday_surface(
        1,
        None,
        "2026-07-30",
        {"settlement_cob": "2026-07-30"},
        {},
        market_json,
        rows,
        {},
        {},
        {"trades": []},
        stale,
    )

    assert output[0] is ttf_page.no_update
    assert output[1] is output[2]
    assert "different trading date" in str(output[1].children)


def test_trader_controls_are_identifiable_and_keep_the_core_valid():
    observations = _observations()
    adjusted, diagnostics = apply_ttf_smile_adjustments(
        observations,
        {
            "level": 1.0,
            "skew": 2.0,
            "put_curvature": 3.0,
            "call_curvature": 4.0,
        },
    )

    changes = (adjusted["iv"] - observations["iv"]) * 100.0
    atm = np.flatnonzero(np.isclose(observations["delta"], 0.50))[0]
    assert changes.iloc[atm] == pytest.approx(1.0)
    assert changes.iloc[0] == pytest.approx(1.0 - 1.96 + 4.0 * 0.98**2)
    assert changes.iloc[-1] == pytest.approx(1.0 + 1.96 + 3.0 * 0.98**2)
    assert diagnostics["recipe"]["unit"] == "volatility percentage points"
    assert build_ttf_pchip_core(adjusted).time_to_expiry > 0


def test_non_node_trade_target_is_matched_locally():
    observations = _observations()
    core = build_ttf_pchip_core(observations)
    target_strike = float(np.sqrt(core.strike_nodes[4] * core.strike_nodes[5]))
    target_x = np.log(target_strike / core.forward)
    base_iv = np.sqrt(float(core.total_variance(target_x)) / core.time_to_expiry)

    adjusted, diagnostics = apply_ttf_smile_adjustments(
        observations,
        {},
        selected_trade={
            "trade_id": "trade-1",
            "strike": target_strike,
            "mark_iv": base_iv + 0.015,
        },
    )

    target = diagnostics["selected_trade"]
    assert target["matched_iv"] == pytest.approx(base_iv + 0.015, abs=1e-10)
    assert target["trade_id"] == "trade-1"
    assert not np.allclose(adjusted["iv"], observations["iv"])


def test_unchanged_expert_tail_values_do_not_replace_new_fit():
    initial = {
        "dc": -0.4,
        "uc": 0.35,
        "put_wing_power": 0.6,
        "call_wing_power": 0.7,
        "left_blend_width": 0.1,
        "right_blend_width": 0.1,
    }
    assert _changed_ttf_tail_overrides(dict(initial), initial) == {}

    edited = dict(initial)
    edited["right_blend_width"] = 0.2
    assert _changed_ttf_tail_overrides(edited, initial) == {
        "right_blend_width": 0.2
    }


def test_requested_expiry_initializes_both_ttf_workspaces():
    rows = [
        {"expiry": "Sep-26", "calibration_basis": "Observed"},
        {"expiry": "Oct-26", "calibration_basis": "Observed"},
    ]
    options, workspace, trade_options, trade = populate_ttf_expiry_controls(
        rows,
        "2026-10-01",
        None,
        None,
    )

    assert options == trade_options
    assert workspace == "Oct-26"
    assert trade == "Oct-26"


def test_extrapolated_tail_accepts_first_valid_fit_regardless_of_diagnostic_rmse(
    monkeypatch,
):
    calls = []
    initial = ttf_page.get_defaults("TTF")

    def fake_fit(observations, initial_params, *, n_starts, seed):
        del observations, seed
        calls.append(n_starts)
        return {
            "params": dict(initial_params),
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.003,
            "iv_rmse": 0.01,
            "left_blend_width": 0.10,
            "right_blend_width": 0.10,
            "validation": {"is_valid": True, "min_g": 0.01},
        }

    monkeypatch.setattr(ttf_page, "fit_ttf_hybrid_candidate", fake_fit)
    result = ttf_page._run_ttf_candidate(
        _observations().assign(
            quote_class="extrapolated",
            calibration_basis="extrapolated",
            source_name="official_surface_ttf_shape_smile_template_v1:extrap",
        ),
        initial,
        basis="extrapolated",
    )

    assert calls == [3]
    assert result["tail_fit_tv_rmse"] == pytest.approx(0.003)


def test_extrapolated_tail_retries_when_first_fit_fails_complete_gate(monkeypatch):
    calls = []
    initial = ttf_page.get_defaults("TTF")

    def fake_fit(observations, initial_params, *, n_starts, seed):
        del observations, seed
        calls.append(n_starts)
        return {
            "params": dict(initial_params),
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.003 if n_starts == 3 else 0.0025,
            "iv_rmse": 0.01,
            "left_blend_width": 0.10,
            "right_blend_width": 0.10,
            "validation": {
                "is_valid": n_starts == 9,
                "min_g": 0.01 if n_starts == 9 else -0.01,
            },
        }

    monkeypatch.setattr(ttf_page, "fit_ttf_hybrid_candidate", fake_fit)
    result = ttf_page._run_ttf_candidate(
        _observations().assign(
            quote_class="extrapolated",
            calibration_basis="extrapolated",
            source_name="official_surface_ttf_shape_smile_template_v1:extrap",
        ),
        initial,
        basis="extrapolated",
    )

    assert calls == [3, 9]
    assert result["validation"]["is_valid"] is True


def test_manual_trade_with_entered_iv_computes_delta():
    values = {
        "business_date": "2026-08-06",
        "contract_date": "2026-10-01",
        "option_expiration_date": "2026-09-25",
        "put_call": "P",
        "strike": 42.0,
        "mark_iv": 68.0,
        "volume": 250,
        "forward": 40.0,
    }
    trade = normalize_ttf_intraday_trade(
        values,
        entered_by="trader@example.com",
        now=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
    )

    assert trade["mark_iv"] == pytest.approx(0.68)
    assert 0 < trade["call_delta"] < 1
    assert trade["dte"] == 50.0
    assert trade["delta_convention"] == "undiscounted_call_delta"
    assert trade["method"] == "manual_ttf_black76_v1"

    with pytest.raises(ValueError, match="Working forward"):
        normalize_ttf_intraday_trade(
            {**values, "forward": None},
            entered_by="trader@example.com",
        )


def test_sep_26_trade_derives_iv_from_premium_without_confirmation_toggle():
    trade = normalize_ttf_intraday_trade(
        {
            "business_date": "2026-08-06",
            "contract_date": "2026-09-01",
            "option_expiration_date": "2026-08-26",
            "put_call": "C",
            "strike": 53.0,
            "mark_iv": None,
            "mark_price": 3.3,
            "volume": 60,
            "forward": 54.44,
        },
        entered_by="trader@example.com",
        now=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
    )

    assert trade["mark_iv"] == pytest.approx(0.5017523604877542)
    assert trade["call_delta"] == pytest.approx(0.6129534743882423)
    assert trade["mark_price"] == pytest.approx(3.3)
    assert trade["iv_source"] == "Premium inversion"
    assert trade["method"] == "manual_ttf_black76_premium_inversion_v1"

    with pytest.raises(ValueError, match="not both"):
        normalize_ttf_intraday_trade(
            {
                "business_date": "2026-08-06",
                "contract_date": "2026-09-01",
                "option_expiration_date": "2026-08-26",
                "put_call": "C",
                "strike": 53.0,
                "mark_iv": 50.0,
                "mark_price": 3.3,
                "forward": 54.44,
            },
            entered_by="trader@example.com",
        )


def test_publication_normalization_requires_full_hybrid_provenance():
    frame = pd.DataFrame(
        {
            "expiry": [pd.Timestamp("2026-10-01")],
            "option_expiration_date": [pd.Timestamp("2026-09-25")],
            "strike": [42.0],
            "delta": [0.4],
            "iv": [0.68],
            "total_variance": [0.68**2 * 50 / 365],
            "working_forward": [40.0],
            "core_tail_classification": ["core"],
            "blend_classification": ["pchip_core"],
            "calibration_basis": ["observed"],
            "source_name": ["official"],
        }
    )
    normalized = normalize_ttf_publication_surface(
        frame,
        trading_date="2026-08-06",
    )

    assert normalized.loc[0, "contract_date"] == pd.Timestamp("2026-10-01")
    assert normalized.loc[0, "volatility"] == pytest.approx(0.68)
    assert normalized.loc[0, "calibration_method"] == "PCHIP-core/Wing-v2-tail hybrid"

    with pytest.raises(TTFPublicationError, match="working_forward"):
        normalize_ttf_publication_surface(
            frame.drop(columns="working_forward"),
            trading_date="2026-08-06",
        )


def test_publication_rejects_self_approval_before_touching_storage():
    identity = Identity(
        subject="owner@example.com",
        roles=frozenset({Role.APPROVER}),
        authenticated=True,
        auth_source="test",
    )
    with pytest.raises(PermissionError, match="their own run"):
        publish_ttf_surface(
            None,
            pd.DataFrame(),
            [],
            trading_date="2026-08-06",
            settlement_cob="2026-07-30",
            identity=identity,
            created_by="owner@example.com",
            base_publication_id=None,
            expected_current_publication_id=None,
            idempotency_key="test",
        )


def test_surface_points_use_postgres_copy_with_csv_escaping():
    copied = {}

    class Cursor:
        def copy_expert(self, sql, buffer):
            copied["sql"] = sql
            copied["payload"] = buffer.read()

        def close(self):
            copied["closed"] = True

    connection = SimpleNamespace(
        connection=SimpleNamespace(
            driver_connection=SimpleNamespace(cursor=lambda: Cursor())
        )
    )
    row = {
        column: f"value-{column}"
        for column in ttf_publication._SURFACE_INSERT_COLUMNS
    }
    row["source_name"] = "official,with-comma"
    row["input_fingerprint"] = None

    assert _copy_surface_points(connection, [row]) is True
    assert copied["sql"].startswith(
        "COPY at_lng.implied_volatility_surface_calibrated"
    )
    assert '"official,with-comma"' in copied["payload"]
    assert copied["payload"].rstrip().endswith(r"\N")
    assert copied["closed"] is True


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260806_01_ttf_intraday_workflow.py"
)


class _OperationRecorder:
    def __init__(self):
        self.tables = {}
        self.indexes = {}
        self.added_columns = []
        self.checks = []
        self.statements = []

    def create_table(self, name, *objects, **kwargs):
        self.tables[name] = {"objects": objects, "kwargs": kwargs}

    def create_index(self, name, table, columns, **kwargs):
        self.indexes[name] = (table, columns, kwargs)

    def add_column(self, table, column, **kwargs):
        self.added_columns.append((table, column, kwargs))

    def create_check_constraint(self, name, table, condition, **kwargs):
        self.checks.append((name, table, condition, kwargs))

    def execute(self, statement):
        self.statements.append(str(statement))


def test_intraday_migration_is_additive_and_append_only(monkeypatch):
    spec = importlib.util.spec_from_file_location("ttf_intraday_migration", MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = _OperationRecorder()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.down_revision == "20260803_01"
    assert set(operations.tables) == {
        "vol_calibration_intraday_trades",
        "vol_calibration_run_trade_inputs",
    }
    added = {column.name for _, column, _ in operations.added_columns}
    assert {
        "total_variance",
        "working_forward",
        "surface_region",
        "blend_classification",
        "calibration_basis",
        "calibration_method",
        "calibration_policy_version",
    } <= added
    sql = "\n".join(operations.statements)
    assert "TTF intraday trades are append-only" in sql
    assert "ttf_vol_surface_publication_current" in sql
    assert "option_volatility_surface_comparison_current" not in sql
    hybrid_checks = [
        kwargs
        for name, _, _, kwargs in operations.checks
        if name == "ck_ttf_calibrated_surface_hybrid_fields"
    ]
    assert hybrid_checks == [{"schema": "at_lng", "postgresql_not_valid": True}]
    with pytest.raises(RuntimeError, match="no destructive downgrade"):
        migration.downgrade()
