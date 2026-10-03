import base64
from io import BytesIO
import pickle

import numpy as np
import pandas as pd
import pytest

from options.calibration_engine.config.defaults import get_defaults

from vol_calibration.calibration_inputs import (
    TTF_CALL_DELTA_NODES,
    UNDISCOUNTED_CALL_DELTA,
)
from vol_calibration.ttf_hybrid_surface import (
    TTF_HYBRID_METHOD,
    TTF_HYBRID_POLICY_VERSION,
    fit_ttf_hybrid_candidate,
)
from vol_calibration.pages import ttf
from vol_calibration import ttf_hybrid_surface as hybrid_module
from vol_calibration import observed_fit_pool
from options.vol_calibration.models import gas_hybrid_fit
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


def test_expanded_retry_reuses_failed_deterministic_starts(monkeypatch):
    monkeypatch.setenv("GAS_START_WORKERS", "1")
    starts = []

    def failed_minimize(_objective, start, **_kwargs):
        starts.append(tuple(np.asarray(start, dtype=float)))
        return type("FailedFit", (), {
            "fun": np.nan,
            "x": np.full(len(start), np.nan),
            "success": False,
            "nit": 0,
            "message": "No finite candidate",
        })()

    monkeypatch.setattr(gas_hybrid_fit, "minimize", failed_minimize)
    observations = _hybrid_observations()
    initial = get_defaults("TTF")
    with pytest.raises(hybrid_module.HybridFitNoCandidate) as first:
        hybrid_module.fit_ttf_hybrid_candidate(
            observations, initial, n_starts=3, seed=42
        )
    first_starts = starts.copy()
    starts.clear()
    with pytest.raises(hybrid_module.HybridFitNoCandidate) as resumed:
        hybrid_module.fit_ttf_hybrid_candidate(
            observations, initial, n_starts=9, seed=42,
            resume_start_count=3, resume_attempts=first.value.attempts,
        )
    new_starts = starts.copy()
    starts.clear()
    with pytest.raises(hybrid_module.HybridFitNoCandidate) as full:
        hybrid_module.fit_ttf_hybrid_candidate(
            observations, initial, n_starts=9, seed=42
        )

    assert len(first_starts) == 3
    assert len(new_starts) == 6
    assert starts == first_starts + new_starts
    assert resumed.value.attempts == full.value.attempts
    assert pickle.loads(pickle.dumps(first.value)).attempts == first.value.attempts


def test_parallel_start_collection_preserves_ordered_retry_attempts(monkeypatch):
    class InlinePool:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def map(self, function, tasks):
            return map(function, tasks)

    starts = []

    def failed_minimize(_objective, start, **_kwargs):
        starts.append(tuple(np.asarray(start, dtype=float)))
        return type("FailedFit", (), {
            "fun": np.nan,
            "x": np.full(len(start), np.nan),
            "success": False,
            "nit": 0,
            "message": "No finite candidate",
        })()

    monkeypatch.setenv("GAS_START_WORKERS", "4")
    monkeypatch.setattr(observed_fit_pool, "ProcessPoolExecutor", InlinePool)
    monkeypatch.setattr(gas_hybrid_fit, "minimize", failed_minimize)
    observations = _hybrid_observations()
    initial = get_defaults("TTF")
    with pytest.raises(hybrid_module.HybridFitNoCandidate) as first:
        hybrid_module.fit_ttf_hybrid_candidate(
            observations, initial, n_starts=3, seed=42
        )
    assert len(starts) == 3
    with pytest.raises(hybrid_module.HybridFitNoCandidate) as resumed:
        hybrid_module.fit_ttf_hybrid_candidate(
            observations, initial, n_starts=9, seed=42,
            resume_start_count=3, resume_attempts=first.value.attempts,
        )
    assert len(starts) == 9
    assert [attempt["start"] for attempt in resumed.value.attempts] == list(range(9))
    assert len(set(starts)) == 9


def test_start_pool_failure_runs_the_original_serial_starts(monkeypatch):
    starts = []

    def unavailable_pool(**_kwargs):
        raise RuntimeError("process creation failed")

    def failed_minimize(_objective, start, **_kwargs):
        starts.append(tuple(np.asarray(start, dtype=float)))
        return type("FailedFit", (), {
            "fun": np.nan,
            "x": np.full(len(start), np.nan),
            "success": False,
            "nit": 0,
            "message": "No finite candidate",
        })()

    monkeypatch.setenv("GAS_START_WORKERS", "4")
    monkeypatch.setattr(observed_fit_pool, "ProcessPoolExecutor", unavailable_pool)
    monkeypatch.setattr(gas_hybrid_fit, "minimize", failed_minimize)
    with pytest.raises(hybrid_module.HybridFitNoCandidate) as failure:
        hybrid_module.fit_ttf_hybrid_candidate(
            _hybrid_observations(), get_defaults("TTF"), n_starts=3, seed=42
        )
    assert len(starts) == 3
    assert [attempt["start"] for attempt in failure.value.attempts] == [0, 1, 2]


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
