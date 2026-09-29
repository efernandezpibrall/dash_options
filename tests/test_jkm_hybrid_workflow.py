from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from options.calibration_engine.config.defaults import get_defaults
from vol_calibration.pages import jkm
from vol_calibration.ttf_publication import normalize_ttf_publication_surface


DELTAS = [0.01, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.99]


def _market(expiries):
    rows = []
    for index, (expiry, basis) in enumerate(expiries):
        for node, delta in enumerate(DELTAS):
            rows.append(
                {
                    "expiry": pd.Timestamp(expiry),
                    "option_expiration_date": pd.Timestamp(expiry) - pd.Timedelta(days=3),
                    "dte": 120.0 + 30.0 * index,
                    "delta": delta,
                    "delta_convention": "undiscounted_call_delta",
                    "iv": 0.45 + 0.002 * node,
                    "strike": 14.0 + node,
                    "forward": 18.0 + index,
                    "source_name": (
                        "official"
                        if basis == "observed"
                        else "official_surface_ttf_shape_smile_template_v3:extrap"
                    ),
                    "quote_class": basis,
                    "weight": 1.0 if basis == "observed" else 0.0,
                }
            )
    return pd.DataFrame(rows)


def _table(expiries):
    defaults = get_defaults("JKM")
    return [
        {
            "expiry": pd.Timestamp(expiry).strftime("%b-%y"),
            "calibration_basis": basis.title(),
            **defaults,
            "left_blend_width": None,
            "right_blend_width": None,
            "core_tv_rmse": None,
            "tail_fit_tv_rmse": None,
            "iv_rmse": None,
            "calibration_method": "",
            "arb_status": "Uncalibrated",
            "rmse": "",
        }
        for expiry, basis in expiries
    ]


def _candidate(params, *, valid=True, vr=None):
    fitted = dict(params)
    if vr is not None:
        fitted["vr"] = vr
    return {
        "params": fitted,
        "core_tv_rmse": 0.0,
        "tail_fit_tv_rmse": 0.001,
        "iv_rmse": 0.002,
        "left_blend_width": 0.10,
        "right_blend_width": 0.10,
        "validation": {"is_valid": valid, "min_g": 0.006},
    }


def test_jkm_selected_start_policy_uses_three_then_tail_retry(monkeypatch):
    observations = _market([("2026-10-01", "extrapolated")])
    calls = []

    def fake_fit(data, initial, *, n_starts, seed):
        del data, seed
        calls.append(n_starts)
        return _candidate(initial, valid=n_starts == 9)

    monkeypatch.setattr(jkm, "fit_jkm_hybrid_candidate", fake_fit)
    result = jkm._run_jkm_candidate(
        jkm._select_jkm_expiry_inputs(observations, "Oct-26"),
        get_defaults("JKM"),
        basis="extrapolated",
    )

    assert calls == [3, 9]
    assert result["validation"]["is_valid"] is True


def test_jkm_batch_fits_observed_independently_then_chains_tail(monkeypatch):
    expiries = [
        ("2026-09-01", "observed"),
        ("2026-10-01", "observed"),
        ("2026-11-01", "extrapolated"),
        ("2026-12-01", "extrapolated"),
    ]
    market = _market(expiries)
    table = _table(expiries)
    table[0]["vr"] = 0.21
    table[1]["vr"] = 0.22
    seeds = []

    def fake_run(observations, initial, *, basis):
        seeds.append((basis, float(initial["vr"])))
        target = {0.21: 0.31, 0.22: 0.32, 0.32: 0.33, 0.33: 0.34}[
            round(float(initial["vr"]), 2)
        ]
        return _candidate(get_defaults("JKM"), vr=target)

    monkeypatch.setattr(jkm, "_run_jkm_candidate", fake_run)
    monkeypatch.setattr(
        jkm,
        "_evaluate_existing_hybrid",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("uncalibrated")),
    )

    result = jkm.calibrate_jkm_batch(market, table)

    assert seeds == [
        ("observed", 0.21),
        ("observed", 0.22),
        ("extrapolated", 0.32),
        ("extrapolated", 0.33),
    ]
    assert result["success_count"] == 4
    assert result["fail_count"] == 0
    assert [row["vr"] for row in result["table_data"]] == pytest.approx(
        [0.31, 0.32, 0.33, 0.34]
    )
    assert all(row["arb_status"] == "Pass" for row in result["table_data"])


def test_jkm_batch_resumes_verified_prefix_without_refitting(monkeypatch):
    from vol_calibration.batch_checkpoints import StaleCalibrationCheckpoint

    expiries = [
        ("2026-09-01", "observed"),
        ("2026-10-01", "observed"),
        ("2026-11-01", "extrapolated"),
    ]
    market = _market(expiries)
    table = _table(expiries)
    calls = []

    def fake_run(observations, initial, *, basis):
        calls.append((basis, float(initial["vr"])))
        return _candidate(
            get_defaults("JKM"), vr=0.14 + float(observations["forward"].iloc[0]) / 100
        )

    monkeypatch.setattr(jkm, "_run_jkm_candidate", fake_run)
    monkeypatch.setattr(
        jkm, "_evaluate_existing_hybrid",
        lambda *_args: (_ for _ in ()).throw(ValueError("uncalibrated")),
    )
    saved = {}
    full = jkm.calibrate_jkm_batch(
        market, table, checkpoint_callback=lambda row: saved.update({row["expiry"]: row}),
    )
    assert full["fail_count"] == 0
    assert len(calls) == 3

    calls.clear()
    resumed = jkm.calibrate_jkm_batch(
        market, table, checkpoints={key: saved[key] for key in list(saved)[:2]},
    )
    assert resumed == full
    assert len(calls) == 1
    assert calls[0][0] == "extrapolated"

    edited = [dict(row) for row in table]
    edited[0]["vr"] += 0.01
    with pytest.raises(StaleCalibrationCheckpoint, match="2026-09-01"):
        jkm.calibrate_jkm_batch(market, edited, checkpoints=saved)


def test_jkm_interrupted_expiry_never_checkpoints_previous_result_as_current(monkeypatch):
    expiries = [
        ("2026-09-01", "observed"),
        ("2026-10-01", "observed"),
    ]
    market = _market(expiries)
    saved = []

    def interrupt_on_second(observations, initial, *, basis):
        if pd.Timestamp(observations["expiry"].iloc[0]).month == 10:
            raise KeyboardInterrupt
        return _candidate(get_defaults("JKM"), vr=0.31)

    monkeypatch.setattr(jkm, "_run_jkm_candidate", interrupt_on_second)
    monkeypatch.setattr(
        jkm, "_evaluate_existing_hybrid",
        lambda *_args: (_ for _ in ()).throw(ValueError("uncalibrated")),
    )
    with pytest.raises(KeyboardInterrupt):
        jkm.calibrate_jkm_batch(
            market, _table(expiries), checkpoint_callback=saved.append,
        )
    assert [row["expiry"] for row in saved] == ["2026-09-01"]


def test_jkm_batch_consumes_parallel_observed_results_in_expiry_order(monkeypatch):
    expiries = [
        (pd.Timestamp("2026-09-01") + pd.DateOffset(months=index), "observed")
        for index in range(8)
    ] + [("2027-05-01", "extrapolated")]
    market = _market(expiries)
    table = _table(expiries)
    seen = []

    def fake_prefit(tasks, _worker, *, environment_variable):
        assert environment_variable == "JKM_OBSERVED_FIT_WORKERS"
        assert len(tasks) == 8
        return {
            key: (True, _candidate(get_defaults("JKM"), vr=0.31 + index / 100))
            for index, (key, _observations, _initial) in enumerate(tasks)
        }

    def fake_run(_observations, initial, *, basis):
        seen.append((basis, initial["vr"]))
        assert basis == "extrapolated"
        return _candidate(get_defaults("JKM"), vr=0.40)

    monkeypatch.setattr(jkm, "prefit_observed_expiries", fake_prefit)
    monkeypatch.setattr(jkm, "_run_jkm_candidate", fake_run)
    monkeypatch.setattr(
        jkm, "_evaluate_existing_hybrid",
        lambda *_args: (_ for _ in ()).throw(ValueError("uncalibrated")),
    )

    result = jkm.calibrate_jkm_batch(market, table)

    assert result["success_count"] == 9
    assert result["fail_count"] == 0
    assert seen == [("extrapolated", pytest.approx(0.38))]
    assert [row["vr"] for row in result["table_data"][:8]] == pytest.approx(
        [0.31 + index / 100 for index in range(8)]
    )


def test_jkm_batch_state_rejects_table_or_market_changes():
    expiries = [("2026-09-01", "observed")]
    table = _table(expiries)
    candidate = _candidate(get_defaults("JKM"))
    jkm._update_hybrid_row(table[0], candidate, "observed")
    results = [{"expiry": "2026-09-01", "status": "Success"}]
    state = jkm._build_batch_state(
        "2026-08-21",
        "market-a",
        table,
        {"publication_id": None},
        results,
    )

    assert jkm._batch_state_ready(
        state,
        "2026-08-21",
        "market-a",
        table,
        {"publication_id": None},
    ) == (True, None)
    changed = [dict(table[0], sr=0.123)]
    ready, reason = jkm._batch_state_ready(
        state,
        "2026-08-21",
        "market-a",
        changed,
        {"publication_id": None},
    )
    assert ready is False
    assert "table changed" in reason.lower()


def test_generic_publication_normalizes_jkm_policy_and_provenance():
    surface = pd.DataFrame(
        {
            "expiry": [pd.Timestamp("2026-10-01")],
            "option_expiration_date": [pd.Timestamp("2026-09-28")],
            "strike": [18.0],
            "delta": [0.5],
            "iv": [0.45],
            "total_variance": [0.02],
            "working_forward": [18.0],
            "core_tail_classification": ["core"],
            "blend_classification": ["pchip_core"],
            "calibration_basis": ["observed"],
            "source_name": ["official"],
        }
    )

    normalized = normalize_ttf_publication_surface(
        surface,
        trading_date=date(2026, 8, 21),
        commodity="JKM",
    )

    assert normalized.loc[0, "commodity"] == "JKM"
    assert normalized.loc[0, "calibration_method"] == jkm.JKM_HYBRID_METHOD
    assert (
        normalized.loc[0, "calibration_policy_version"]
        == jkm.JKM_HYBRID_POLICY_VERSION
    )
