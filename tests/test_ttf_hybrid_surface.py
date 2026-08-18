import base64
from io import BytesIO

import numpy as np
import pandas as pd
import pytest

from options.calibration_engine.config.defaults import get_defaults

from vol_calibration.calibration_inputs import (
    TTF_CALL_DELTA_NODES,
    UNDISCOUNTED_CALL_DELTA,
)
from vol_calibration.ttf_hybrid_surface import (
    TTF_CORE_SAMPLE_COUNT,
    TTF_HYBRID_METHOD,
    TTF_HYBRID_POLICY_VERSION,
    build_ttf_pchip_core,
    fit_ttf_hybrid_candidate,
    hybrid_total_variance,
    operational_surface_frame,
)
from vol_calibration.pages import ttf
from vol_calibration.components.smile_grid import create_smile_grid_figure


def _hybrid_observations():
    x = np.asarray(
        [-0.60, -0.40, -0.25, -0.12, -0.04, 0.00, 0.08, 0.20, 0.40, 0.70, 1.10]
    )
    forward = 50.0
    iv = 0.70 + 0.10 * x + 0.03 * x**2
    return pd.DataFrame(
        {
            "expiry": pd.Timestamp("2026-10-01"),
            "option_expiration_date": pd.Timestamp("2026-09-25"),
            "forward": forward,
            "strike": forward * np.exp(x),
            "iv": iv,
            "delta": TTF_CALL_DELTA_NODES[::-1],
            "dte": 90.0,
            "delta_convention": UNDISCOUNTED_CALL_DELTA,
            "source_name": "official",
            "quote_class": "observed",
            "weight": 1.0,
            "calibration_basis": "observed",
        }
    )


def test_pchip_core_reproduces_nodes_and_total_variance_units_exactly():
    observations = _hybrid_observations()
    core = build_ttf_pchip_core(observations)

    expected = (90.0 / 365.0) * core.iv_nodes**2
    assert np.allclose(core.total_variance_nodes, expected, rtol=0.0, atol=1e-14)
    assert np.allclose(
        core.total_variance(core.x_nodes), expected, rtol=0.0, atol=1e-13
    )
    assert core.interpolator.extrapolate is False


def test_pchip_core_rejects_duplicate_strikes_and_non_positive_variance():
    duplicate = _hybrid_observations()
    duplicate.loc[duplicate.index[-1], "strike"] = duplicate.loc[
        duplicate.index[-2], "strike"
    ]
    with pytest.raises(ValueError, match="distinct, finite, positive official strikes"):
        build_ttf_pchip_core(duplicate)

    with pytest.raises(ValueError, match="implied volatilities"):
        build_ttf_pchip_core(_hybrid_observations().assign(iv=0.0))


def test_hybrid_fit_is_deterministic_and_passes_complete_arbitrage_gate():
    observations = _hybrid_observations()
    first = fit_ttf_hybrid_candidate(observations, get_defaults("TTF"), n_starts=1)
    second = fit_ttf_hybrid_candidate(observations, get_defaults("TTF"), n_starts=1)

    assert first["calibration_method"] == TTF_HYBRID_METHOD
    assert first["calibration_policy_version"] == TTF_HYBRID_POLICY_VERSION
    assert first["core_tv_rmse"] == 0.0
    assert first["validation"]["is_valid"] is True
    assert first["validation"]["min_g"] >= 0.006 - 1e-8
    assert first["validation"]["n_points"] == 4001
    assert first["tail_fit_tv_rmse"] == pytest.approx(
        second["tail_fit_tv_rmse"], abs=1e-12
    )
    assert first["params"] == pytest.approx(second["params"], abs=1e-10)


def test_hybrid_is_c1_at_core_and_wing_join_points():
    observations = _hybrid_observations()
    result = fit_ttf_hybrid_candidate(observations, get_defaults("TTF"), n_starts=1)
    core = result["core"]
    width = result["left_blend_width"]
    join_points = (
        core.xmin - width,
        core.xmin,
        core.xmax,
        core.xmax + result["right_blend_width"],
    )
    step = 1e-5
    for join in join_points:
        x = join + np.asarray([-step, 0.0, step])
        values = hybrid_total_variance(
            x,
            core,
            result["params"],
            left_blend_width=result["left_blend_width"],
            right_blend_width=result["right_blend_width"],
        )
        left_derivative = (values[1] - values[0]) / step
        right_derivative = (values[2] - values[1]) / step
        assert left_derivative == pytest.approx(right_derivative, abs=3e-4)


def test_operational_surface_labels_core_blends_tails_and_preserves_source():
    observations = _hybrid_observations()
    result = fit_ttf_hybrid_candidate(observations, get_defaults("TTF"), n_starts=1)
    surface = operational_surface_frame(
        observations,
        result["params"],
        left_blend_width=result["left_blend_width"],
        right_blend_width=result["right_blend_width"],
        n_points=TTF_CORE_SAMPLE_COUNT,
    )

    assert len(surface) == TTF_CORE_SAMPLE_COUNT
    assert set(surface["calibration_basis"]) == {"observed"}
    assert set(surface["source_name"]) == {"official"}
    assert {
        "wing_left",
        "left_blend",
        "pchip_core",
        "right_blend",
        "wing_right",
    }.issubset(set(surface["blend_classification"]))
    assert np.all(np.isfinite(surface[["delta", "strike", "iv", "total_variance"]]))
    assert np.all(surface["total_variance"] > 0)


def test_smile_grid_legend_toggles_each_series_across_all_expiries():
    oct_observations = _hybrid_observations()
    nov_observations = oct_observations.assign(
        expiry=pd.Timestamp("2026-11-01"),
        option_expiration_date=pd.Timestamp("2026-10-27"),
        dte=120.0,
    )
    market = pd.concat([oct_observations, nov_observations], ignore_index=True)
    params = {
        **get_defaults("TTF"),
        "left_blend_width": 0.10,
        "right_blend_width": 0.10,
        "calibration_method": TTF_HYBRID_METHOD,
    }
    params_df = pd.DataFrame(
        [
            {"expiry": pd.Timestamp("2026-10-01"), **params},
            {"expiry": pd.Timestamp("2026-11-01"), **params},
        ]
    )

    figure = create_smile_grid_figure(
        market,
        params_df,
        x_axis="delta",
    )

    for trace_name in (
        "Market",
        "Operational surface (PCHIP core / Wing tails)",
        "Wing tail-fit diagnostic",
    ):
        traces = [trace for trace in figure.data if trace.name == trace_name]
        assert len(traces) == 2
        assert {trace.legendgroup for trace in traces} == {trace_name}
        assert [bool(trace.showlegend) for trace in traces] == [True, False]
    assert figure.layout.legend.groupclick == "togglegroup"


def test_ttf_export_adds_reconciled_operational_surface_without_changing_market_rows():
    observations = _hybrid_observations()
    result = fit_ttf_hybrid_candidate(observations, get_defaults("TTF"), n_starts=1)
    table_row = {
        "expiry": "Oct-26",
        "calibration_basis": "Observed",
        **result["params"],
        "left_blend_width": result["left_blend_width"],
        "right_blend_width": result["right_blend_width"],
        "tail_fit_tv_rmse": f"{result['tail_fit_tv_rmse']:.6f}",
        "iv_rmse": f"{result['iv_rmse']:.6f}",
        "rmse": "0.000000",
        "arb_status": "Pass",
        "calibration_method": TTF_HYBRID_METHOD,
    }

    download = ttf.export_to_excel(
        1,
        [table_row],
        observations.drop(columns="calibration_basis").to_json(
            date_format="iso", orient="split"
        ),
        "2026-07-30",
    )
    workbook = pd.ExcelFile(BytesIO(base64.b64decode(download["content"])))
    exported_market = pd.read_excel(workbook, sheet_name="Market Data")
    parameters = pd.read_excel(workbook, sheet_name="Parameters")
    operational = pd.read_excel(workbook, sheet_name="Operational Surface")
    summary = pd.read_excel(workbook, sheet_name="Summary")

    assert len(exported_market) == 11
    assert exported_market["weight"].tolist() == [1.0] * 11
    assert len(operational) == 401
    assert set(operational["calibration_basis"]) == {"observed"}
    assert set(operational["source_name"]) == {"official"}
    assert set(operational["core_tail_classification"]) == {"core", "tail"}
    assert parameters.loc[0, "calibration_method"] == TTF_HYBRID_METHOD
    assert parameters.loc[0, "calibration_policy_version"] == TTF_HYBRID_POLICY_VERSION
    assert summary.loc[0, "Operational Surface Rows"] == 401
