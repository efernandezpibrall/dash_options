import base64
from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from dash.exceptions import PreventUpdate

from vol_calibration.calibration_inputs import (
    UNDISCOUNTED_CALL_DELTA,
    calibration_eligibility_error,
    expiry_month,
    select_expiry_observations,
)
from vol_calibration.components.batch_calibration_modal import (
    create_batch_calibration_confirm_modal,
)
from vol_calibration.data_cache import clear_workspace_load_cache
from vol_calibration.pages import ttf


def _valid_ttf_observations():
    deltas = np.asarray(
        [0.01, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.99]
    )
    return pd.DataFrame(
        {
            "expiry": pd.Timestamp("2026-09-01"),
            "option_expiration_date": pd.Timestamp("2026-08-27"),
            "forward": 50.0,
            "strike": np.nan,
            "iv": np.linspace(0.62, 0.52, len(deltas)),
            "delta": deltas,
            "dte": 78.0,
            "delta_convention": UNDISCOUNTED_CALL_DELTA,
            "source_name": "official",
            "quote_class": "observed",
            "weight": 1.0,
        }
    )


def test_month_label_is_parsed_as_2026_not_year_26():
    assert expiry_month("Sep-26") == pd.Period("2026-09", freq="M")


def test_reset_candidate_decodes_market_data_once(monkeypatch):
    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-reset-adjustment-btn"),
    )
    real_read_json = ttf.pd.read_json
    decoded = []

    def counted_read_json(*args, **kwargs):
        decoded.append(args[0])
        return real_read_json(*args, **kwargs)

    monkeypatch.setattr(ttf.pd, "read_json", counted_read_json)
    monkeypatch.setattr(
        ttf,
        "format_params_for_table",
        lambda params, market, commodity: [{"expiry": "Sep-26", "vr": 0.25}],
    )
    monkeypatch.setattr(
        ttf,
        "_apply_published_parameters",
        lambda rows, market, publication: rows,
    )
    market_json = _valid_ttf_observations().to_json(
        date_format="iso", orient="split"
    )
    params_json = pd.DataFrame([{"expiry": "2026-09-01", "vr": 0.25}]).to_json(
        date_format="iso", orient="split"
    )

    result = ttf.build_ttf_intraday_candidate(
        None,
        1,
        "Sep-26",
        0.0,
        0.0,
        0.0,
        0.0,
        [],
        [],
        {},
        market_json,
        None,
        [{"expiry": "Sep-26", "vr": 0.31}],
        params_json,
        {},
        {},
        [],
    )

    assert len(decoded) == 2
    assert result[1] == [{"expiry": "Sep-26", "vr": 0.25}]


def test_expiry_selection_excludes_zero_weight_extrapolated_rows():
    market_data = pd.concat(
        [
            _valid_ttf_observations(),
            _valid_ttf_observations().assign(
                source_name="official_template:extrap",
                quote_class="extrapolated",
                weight=0.0,
            ),
        ],
        ignore_index=True,
    )

    selected = select_expiry_observations(market_data, "Sep-26")

    assert len(selected) == 11
    assert set(selected["quote_class"]) == {"observed"}
    assert calibration_eligibility_error(selected) is None


def test_missing_exact_forward_and_option_expiry_fail_closed():
    observations = _valid_ttf_observations().assign(
        forward=np.nan,
        dte=np.nan,
        option_expiration_date=pd.NaT,
    )

    assert calibration_eligibility_error(observations) == (
        "An exact, finite same-COB forward is required for this expiry."
    )


def test_unavailable_cob_disables_calibration_without_synthetic_data(monkeypatch):
    clear_workspace_load_cache()
    monkeypatch.setattr(
        ttf,
        "load_market_data_with_metadata",
        lambda *args, **kwargs: {
            "data": pd.DataFrame(),
            "source": "unavailable",
            "is_synthetic": False,
            "last_update": None,
            "message": "No eligible TTF market data for 2026-06-10",
            "error": "No unified TTF volatility surface for exact COB 2026-06-10",
        },
    )

    result = ttf.load_data("2026-06-10", 0)

    assert result[4] is True
    assert result[6] is True
    assert "No exact-COB TTF volatility surface" in result[5]
    assert "2026-06-10" in result[3]


def test_selected_calibration_accepts_sep_26_and_passes_delta_convention(monkeypatch):
    calls = []

    def fake_hybrid(observations, initial_params, *, n_starts, seed):
        calls.append(
            {
                "observations": observations,
                "initial_params": initial_params,
                "n_starts": n_starts,
                "seed": seed,
            }
        )
        return {
            "params": initial_params,
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.01,
            "left_blend_width": 0.10,
            "right_blend_width": 0.10,
            "success": True,
            "butterfly": {"is_valid": True},
            "validation": {"is_valid": True, "min_g": 0.01},
        }

    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-calibrate-all-btn"),
    )
    monkeypatch.setattr(ttf, "fit_ttf_hybrid_candidate", fake_hybrid)
    monkeypatch.setattr(ttf, "create_comparison_plot", lambda *args, **kwargs: {})

    table_data = [
        {
            "expiry": "Sep-26",
            "vr": 0.52,
            "sr": 0.07,
            "pc": 0.31,
            "cc": 0.49,
            "dc": -0.25,
            "uc": 0.30,
            "dsm": 0.50,
            "usm": 0.50,
            "vcr": -0.15,
            "scr": 0.05,
            "ssr": 1.0,
            "put_wing_power": 0.5,
            "call_wing_power": 0.5,
            "rmse": "",
            "arb_status": "Warn",
        }
    ]
    market_json = _valid_ttf_observations().to_json(
        date_format="iso",
        orient="split",
    )

    result = ttf.handle_calibration(
        1,
        None,
        None,
        None,
        None,
        None,
        market_json,
        table_data,
        [0],
        False,
        None,
        "delta",
        "2026-06-10",
        None,
    )

    assert result[0] is True
    assert result[2] == "Expiry: Sep-26"
    assert result[3] == "€50.00/MWh"
    assert set(calls[0]["observations"]["delta_convention"]) == {
        UNDISCOUNTED_CALL_DELTA
    }
    assert calls[0]["n_starts"] == 1
    assert calls[0]["initial_params"]["vr"] == pytest.approx(0.52)


def test_manual_node_edit_refits_tail_before_updating_session_final(monkeypatch):
    observations = _valid_ttf_observations()
    table_row = {
        "expiry": "Sep-26",
        "calibration_basis": "Observed",
        "vr": 0.52,
        "sr": 0.07,
        "pc": 0.31,
        "cc": 0.49,
        "dc": -0.25,
        "uc": 0.30,
        "dsm": 0.50,
        "usm": 0.50,
        "vcr": -0.15,
        "scr": 0.05,
        "ssr": 1.0,
        "put_wing_power": 0.5,
        "call_wing_power": 0.5,
    }
    fitted = {
        "params": {key: table_row[key] for key in ttf.PARAM_COLUMNS},
        "core_tv_rmse": 0.0,
        "tail_fit_tv_rmse": 0.0012,
        "iv_rmse": 0.006,
        "left_blend_width": 0.15,
        "right_blend_width": 0.15,
        "success": True,
        "validation": {"is_valid": True, "min_g": 0.007},
    }
    calls = []

    def fake_run(candidate_observations, initial_params, *, basis, selected_expiry):
        calls.append((candidate_observations.copy(), initial_params, basis, selected_expiry))
        return fitted

    monkeypatch.setattr(ttf, "_run_ttf_candidate", fake_run)
    node_rows = [
        {
            "delta": float(row.delta),
            "final_iv_pct": float(row.iv) * 100.0,
        }
        for row in observations.itertuples()
    ]
    node_rows[7]["final_iv_pct"] += 0.05

    node_store, status, updated_table, rendered_rows = ttf.validate_ttf_node_edits(
        1,
        node_rows,
        observations.to_json(date_format="iso", orient="split"),
        [table_row],
        [0],
        {},
    )

    assert node_store["2026-09"]["0.7000000000"] == pytest.approx(
        node_rows[7]["final_iv_pct"] / 100.0
    )
    assert calls[0][2:] == ("observed", True)
    assert updated_table[0]["left_blend_width"] == pytest.approx(0.15)
    assert updated_table[0]["tail_fit_tv_rmse"] == pytest.approx(0.0012)
    assert updated_table[0]["arb_status"] == "Pass"
    assert rendered_rows[7]["final_iv_pct"] == pytest.approx(
        node_rows[7]["final_iv_pct"]
    )
    assert "Valid session Final" in str(status)


def test_invalid_manual_node_edit_is_visibly_restored(monkeypatch):
    observations = _valid_ttf_observations()
    table_row = {
        "expiry": "Sep-26",
        **{name: 0.1 for name in ttf.PARAM_COLUMNS},
    }
    node_rows = ttf._ttf_node_editor_rows(observations, "Sep-26")
    node_rows[7]["final_iv_pct"] += 10.0
    monkeypatch.setattr(
        ttf,
        "_run_ttf_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid hybrid")),
    )

    node_store, status, updated_table, rendered_rows = ttf.validate_ttf_node_edits(
        1,
        node_rows,
        observations.to_json(date_format="iso", orient="split"),
        [table_row],
        [0],
        {},
    )

    assert node_store is ttf.no_update
    assert updated_table is ttf.no_update
    assert rendered_rows[7]["final_iv_pct"] == pytest.approx(
        observations.sort_values("delta").iloc[7]["iv"] * 100.0
    )
    assert "invalid hybrid" in str(status)


def test_ttf_acceptance_uses_complete_gate_for_feasible_nonconverged_result():
    result = {
        "success": False,
        "params": {name: 0.1 for name in ttf.PARAM_COLUMNS},
        "tail_fit_tv_rmse": 0.002,
        "validation": {"is_valid": True, "min_g": 0.0061},
    }

    assert ttf._accepted_calibration_result(result) is True


def _valid_extrapolated_observations(expiry="2029-04-01", dte=974.0):
    return _valid_ttf_observations().assign(
        expiry=pd.Timestamp(expiry),
        option_expiration_date=pd.Timestamp(expiry) - pd.Timedelta(days=5),
        dte=dte,
        source_name="official_surface_ttf_shape_smile_template_v3:extrap",
        quote_class="extrapolated",
        weight=0.0,
    )


def test_explicit_ttf_mode_reweights_only_approved_extrapolated_rows():
    selected = select_expiry_observations(
        _valid_extrapolated_observations(),
        "Apr-29",
        include_extrapolated=True,
    )

    assert len(selected) == 11
    assert set(selected["calibration_basis"]) == {"extrapolated"}
    assert set(selected["quote_class"]) == {"extrapolated"}
    assert set(selected["source_name"]) == {
        "official_surface_ttf_shape_smile_template_v3:extrap"
    }
    assert selected["weight"].tolist() == [1.0] * 11
    assert calibration_eligibility_error(selected) is None


def test_explicit_ttf_mode_rejects_mixed_and_unsupported_provenance():
    mixed = pd.concat(
        [
            _valid_ttf_observations(),
            _valid_extrapolated_observations(expiry="2026-09-01", dte=78.0),
        ],
        ignore_index=True,
    )
    with pytest.raises(ValueError, match="homogeneous"):
        select_expiry_observations(
            mixed,
            "Sep-26",
            include_extrapolated=True,
        )

    unsupported = _valid_extrapolated_observations().assign(
        source_name="manual_curve:extrap"
    )
    with pytest.raises(ValueError, match="Unsupported extrapolated"):
        select_expiry_observations(
            unsupported,
            "Apr-29",
            include_extrapolated=True,
        )


@pytest.mark.parametrize("quote_class", ["synthetic", "interpolated", "fitted"])
def test_explicit_ttf_mode_rejects_non_governed_quote_classes(quote_class):
    rows = _valid_extrapolated_observations().assign(quote_class=quote_class)
    with pytest.raises(ValueError, match="Unsupported TTF calibration quote class"):
        select_expiry_observations(
            rows,
            "Apr-29",
            include_extrapolated=True,
        )


def test_explicit_ttf_mode_rejects_incomplete_or_invalid_slices():
    with pytest.raises(ValueError, match="complete governed 11-node"):
        select_expiry_observations(
            _valid_extrapolated_observations().iloc[:-1],
            "Apr-29",
            include_extrapolated=True,
        )

    with pytest.raises(ValueError, match="same-COB forward"):
        select_expiry_observations(
            _valid_extrapolated_observations().assign(forward=np.nan),
            "Apr-29",
            include_extrapolated=True,
        )

    with pytest.raises(ValueError, match="positive actual DTE"):
        select_expiry_observations(
            _valid_extrapolated_observations().assign(dte=0.0),
            "Apr-29",
            include_extrapolated=True,
        )

    duplicate_delta = _valid_extrapolated_observations().copy()
    duplicate_delta.loc[duplicate_delta.index[-1], "delta"] = 0.90
    with pytest.raises(ValueError, match="distinct"):
        select_expiry_observations(
            duplicate_delta,
            "Apr-29",
            include_extrapolated=True,
        )

    with pytest.raises(ValueError, match="implied volatilities"):
        select_expiry_observations(
            _valid_extrapolated_observations().assign(iv=0.0),
            "Apr-29",
            include_extrapolated=True,
        )

    with pytest.raises(ValueError, match="undiscounted call-delta"):
        select_expiry_observations(
            _valid_extrapolated_observations().assign(
                delta_convention="premium_adjusted_delta"
            ),
            "Apr-29",
            include_extrapolated=True,
        )


def test_explicit_ttf_mode_rejects_missing_or_mixed_source_provenance():
    with pytest.raises(ValueError, match="provenance is missing"):
        select_expiry_observations(
            _valid_extrapolated_observations().drop(columns="source_name"),
            "Apr-29",
            include_extrapolated=True,
        )

    mixed_source = _valid_extrapolated_observations().copy()
    mixed_source.loc[mixed_source.index[-1], "source_name"] = (
        "official_surface_ttf_shape_smile_template_v4:extrap"
    )
    with pytest.raises(ValueError, match="homogeneous"):
        select_expiry_observations(
            mixed_source,
            "Apr-29",
            include_extrapolated=True,
        )


def _table_row(expiry, basis, vr):
    return {
        "expiry": expiry,
        "calibration_basis": basis.title(),
        **{**ttf.get_defaults("TTF"), "vr": vr},
        "rmse": "2.00%",
        "arb_status": "Pass",
    }


def test_selected_apr_29_uses_editable_row_and_extrapolated_retry(monkeypatch):
    calls = []

    def fake_hybrid(observations, initial_params, *, n_starts, seed):
        calls.append(
            {
                "observations": observations,
                "initial_params": initial_params,
                "n_starts": n_starts,
                "seed": seed,
            }
        )
        if n_starts == 3:
            raise RuntimeError("first hybrid attempt failed validation")
        params = {**initial_params, "vr": 0.41}
        return {
            "params": params,
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.01,
            "left_blend_width": 0.15,
            "right_blend_width": 0.15,
            "success": True,
            "butterfly": {"is_valid": True},
            "validation": {"is_valid": True, "min_g": 0.01},
        }

    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-calibrate-all-btn"),
    )
    monkeypatch.setattr(ttf, "fit_ttf_hybrid_candidate", fake_hybrid)
    monkeypatch.setattr(ttf, "create_comparison_plot", lambda *args, **kwargs: {})

    table_data = [_table_row("Apr-29", "extrapolated", 0.54)]
    market_json = _valid_extrapolated_observations().to_json(
        date_format="iso",
        orient="split",
    )
    result = ttf.handle_calibration(
        1,
        None,
        None,
        None,
        None,
        None,
        market_json,
        table_data,
        [0],
        False,
        None,
        "delta",
        "2026-07-30",
        None,
    )

    assert result[0] is True
    assert result[1]["calibration_basis"] == "extrapolated"
    assert result[1]["source_name"].endswith(":extrap")
    assert result[1]["candidate_rmse"] == pytest.approx(0.0)
    assert result[1]["candidate_tail_fit_tv_rmse"] == pytest.approx(0.001)
    assert result[1]["candidate_params"]["vr"] == pytest.approx(0.41)
    assert [call["n_starts"] for call in calls] == [3, 9]
    assert all(call["initial_params"]["vr"] == pytest.approx(0.54) for call in calls)


def test_copy_candidate_updates_extrapolated_final_in_session(monkeypatch):
    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-copy-candidate-btn"),
    )
    monkeypatch.setattr(
        ttf,
        "_evaluate_existing_hybrid",
        lambda *args, **kwargs: {
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.01,
            "left_blend_width": 0.10,
            "right_blend_width": 0.10,
            "validation": {"is_valid": True, "min_g": 0.01},
        },
    )
    monkeypatch.setattr(ttf, "create_comparison_plot", lambda *args, **kwargs: {})

    current = dict(ttf.get_defaults("TTF"))
    candidate = {
        **current,
        "vr": 0.41,
        "left_blend_width": 0.10,
        "right_blend_width": 0.10,
    }
    current = {
        **current,
        "left_blend_width": 0.10,
        "right_blend_width": 0.10,
    }
    comparison_store = {
        "expiry": "Apr-29",
        "forward": 50.0,
        "current_params": current,
        "candidate_params": candidate,
        "final_params": current,
        "current_rmse": 0.02,
        "candidate_rmse": 0.001,
        "calibration_basis": "extrapolated",
        "source_name": "official_surface_ttf_shape_smile_template_v3:extrap",
        "row_idx": 0,
    }
    result = ttf.handle_calibration(
        None,
        None,
        None,
        1,
        None,
        None,
        _valid_extrapolated_observations().to_json(
            date_format="iso",
            orient="split",
        ),
        [_table_row("Apr-29", "extrapolated", current["vr"])],
        [0],
        True,
        comparison_store,
        "delta",
        "2026-07-30",
        None,
    )

    assert result[1]["final_params"]["vr"] == pytest.approx(0.41)
    assert result[1]["final_rmse"] == pytest.approx(0.0)
    assert result[10][0]["vr"] == pytest.approx(0.41)
    assert result[10][0]["rmse"] == "0.000000"
    assert result[10][0]["arb_status"] == "Pass"
    assert result[10][0]["calibration_basis"] == "Extrapolated"

    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-reset-final-btn"),
    )
    reset_result = ttf.handle_calibration(
        None,
        None,
        None,
        None,
        1,
        result[4],
        _valid_extrapolated_observations().to_json(
            date_format="iso",
            orient="split",
        ),
        result[10],
        [0],
        True,
        result[1],
        "delta",
        "2026-07-30",
        None,
    )
    assert reset_result[1]["final_params"]["vr"] == pytest.approx(current["vr"])
    assert reset_result[10][0]["vr"] == pytest.approx(current["vr"])


def test_batch_confirmation_reports_exact_ttf_basis_breakdown(monkeypatch):
    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-batch-calibrate-btn"),
    )
    table_data = [
        *[_table_row(f"Observed-{index}", "observed", 0.30) for index in range(31)],
        *[
            _table_row(f"Extrapolated-{index}", "extrapolated", 0.30)
            for index in range(33)
        ],
    ]

    result = ttf.toggle_batch_confirm_modal(1, None, None, table_data, False)

    assert result == (
        True,
        "64 expiries: 31 observed, 33 extrapolated · settlement-node target",
    )


def test_hybrid_batch_confirmation_sets_realistic_duration_expectation():
    hybrid_modal = create_batch_calibration_confirm_modal("TTF", hybrid=True)
    standard_modal = create_batch_calibration_confirm_modal("HH")

    assert "several minutes" in repr(hybrid_modal)
    assert "several seconds" in repr(standard_modal)


def test_ttf_export_preserves_market_weights_and_canonical_provenance():
    market_data = pd.concat(
        [_valid_ttf_observations(), _valid_extrapolated_observations()],
        ignore_index=True,
    )
    table_data = [
        _table_row("Sep-26", "observed", 0.32),
        _table_row("Apr-29", "extrapolated", 0.41),
    ]

    download = ttf.export_to_excel(
        1,
        table_data,
        market_data.to_json(date_format="iso", orient="split"),
        "2026-07-30",
    )
    excel_file = pd.ExcelFile(BytesIO(base64.b64decode(download["content"])))
    parameters = pd.read_excel(excel_file, sheet_name="Parameters")
    market_export = pd.read_excel(excel_file, sheet_name="Market Data")
    summary = pd.read_excel(excel_file, sheet_name="Summary")

    assert list(parameters["calibration_basis"]) == ["observed", "extrapolated"]
    assert list(parameters["source_name"]) == [
        "official",
        "official_surface_ttf_shape_smile_template_v3:extrap",
    ]
    assert market_export["weight"].tolist() == [1.0] * 11 + [0.0] * 11
    assert set(market_export["quote_class"]) == {"observed", "extrapolated"}
    assert summary.loc[0, "Observed Expiries"] == 1
    assert summary.loc[0, "Extrapolated Expiries"] == 1


def test_batch_chains_tail_retries_and_continues_after_failure(monkeypatch):
    market_data = pd.concat(
        [
            _valid_ttf_observations().assign(
                expiry=pd.Timestamp("2029-03-01"),
                option_expiration_date=pd.Timestamp("2029-02-23"),
                dte=938.0,
            ),
            _valid_extrapolated_observations("2029-04-01", 969.0),
            _valid_extrapolated_observations("2029-05-01", 999.0),
            _valid_extrapolated_observations("2029-06-01", 1030.0),
        ],
        ignore_index=True,
    )
    table_data = [
        _table_row("Jun-29", "extrapolated", 0.56),
        _table_row("Mar-29", "observed", 0.53),
        _table_row("May-29", "extrapolated", 0.55),
        _table_row("Apr-29", "extrapolated", 0.54),
    ]
    calls = []

    def fake_hybrid(observations, initial_params, *, n_starts, seed):
        period = pd.to_datetime(observations["expiry"].iloc[0]).to_period("M")
        calls.append(
            {
                "period": str(period),
                "starts": n_starts,
                "seed_vr": initial_params["vr"],
            }
        )
        if str(period) == "2029-05":
            raise RuntimeError("isolated expiry failure")
        if str(period) == "2029-04" and n_starts == 3:
            raise RuntimeError("three-start hybrid failed validation")
        params = dict(initial_params)
        if str(period) == "2029-03":
            params["vr"] = 0.31
        elif str(period) == "2029-04":
            params["vr"] = 0.41
        else:
            params["vr"] = 0.61
        return {
            "params": params,
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.01,
            "left_blend_width": 0.10,
            "right_blend_width": 0.10,
            "success": True,
            "butterfly": {"is_valid": True},
            "validation": {"is_valid": True, "min_g": 0.01},
        }

    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-batch-confirm-btn"),
    )
    monkeypatch.setattr(ttf, "writes_enabled", lambda: False)
    monkeypatch.setattr(ttf, "fit_ttf_hybrid_candidate", fake_hybrid)

    result = ttf.run_batch_calibration(
        1,
        None,
        market_data.to_json(date_format="iso", orient="split"),
        table_data,
        [],
        [],
        "2026-07-30",
        False,
    )

    batch_rows = result[5]["results"]
    assert [row["status"] for row in batch_rows] == [
        "Success",
        "Success",
        "Failed",
        "Success",
    ]
    assert [row["basis"] for row in batch_rows] == [
        "Observed",
        "Extrapolated",
        "Extrapolated",
        "Extrapolated",
    ]
    assert [(call["period"], call["starts"]) for call in calls] == [
        ("2029-03", 1),
        ("2029-04", 3),
        ("2029-04", 9),
        ("2029-05", 3),
        ("2029-05", 9),
        ("2029-06", 3),
    ]
    assert calls[-1]["seed_vr"] == pytest.approx(0.41)

    updated_by_expiry = {row["expiry"]: row for row in result[6]}
    assert updated_by_expiry["Apr-29"]["vr"] == pytest.approx(0.41)
    assert updated_by_expiry["May-29"]["vr"] == pytest.approx(0.55)
    assert updated_by_expiry["Jun-29"]["vr"] == pytest.approx(0.61)


def test_batch_targets_settlement_nodes_and_tracks_node_edits(monkeypatch):
    market_data = _valid_ttf_observations()
    market_json = market_data.to_json(date_format="iso", orient="split")
    table_data = [_table_row("Sep-26", "observed", 0.32)]
    node_store = {"2026-09": [{"delta": 0.65, "iv": 0.37}]}
    publication = {"publication_id": "base-publication"}
    observed_calls = {}

    def fake_settlement(market, expiry):
        observed_calls["settlement_target"] = True
        return ttf._select_ttf_expiry_inputs(market, expiry)

    def fake_node_edits(observations, edits, expiry=None):
        observed_calls["node_store"] = edits
        observed_calls["expiry"] = expiry
        return observations.copy()

    def fake_hybrid(observations, initial_params, *, n_starts, seed):
        return {
            "params": dict(initial_params),
            "core_tv_rmse": 0.0,
            "tail_fit_tv_rmse": 0.001,
            "iv_rmse": 0.01,
            "left_blend_width": 0.10,
            "right_blend_width": 0.10,
            "success": True,
            "butterfly": {"is_valid": True},
            "validation": {"is_valid": True, "min_g": 0.01},
        }

    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-batch-confirm-btn"),
    )
    monkeypatch.setattr(ttf, "_settlement_ttf_observations", fake_settlement)
    monkeypatch.setattr(
        ttf,
        "_base_ttf_observations",
        lambda *args, **kwargs: pytest.fail(
            "batch calibration used the published intraday base"
        ),
    )
    monkeypatch.setattr(ttf, "_apply_node_edits", fake_node_edits)
    monkeypatch.setattr(ttf, "fit_ttf_hybrid_candidate", fake_hybrid)

    output = ttf.run_batch_calibration(
        1,
        None,
        market_json,
        table_data,
        [],
        [],
        "2026-07-30",
        False,
        node_store,
        publication,
    )

    assert observed_calls["settlement_target"] is True
    assert observed_calls["node_store"] is node_store
    assert pd.Timestamp(observed_calls["expiry"]).to_period("M") == pd.Period(
        "2026-09", freq="M"
    )
    ready, reason = ttf._batch_state_ready(
        output[5],
        "2026-07-30",
        market_json,
        output[6],
        node_store,
        publication,
    )
    assert ready is True
    assert reason is None
    assert output[5]["calibration_target"] == ttf.TTF_BATCH_CALIBRATION_TARGET


def test_hybrid_comparison_cannot_persist_even_when_writes_enabled(monkeypatch):
    monkeypatch.setattr(ttf, "writes_enabled", lambda: True)
    monkeypatch.setattr(
        ttf,
        "ctx",
        SimpleNamespace(triggered_id="ttf-comparison-save-btn"),
    )
    with pytest.raises(PreventUpdate):
        ttf.handle_calibration(
            None,
            None,
            1,
            None,
            None,
            None,
            None,
            None,
            None,
            True,
            {"calibration_basis": "extrapolated"},
            "delta",
            "2026-07-30",
            None,
        )
