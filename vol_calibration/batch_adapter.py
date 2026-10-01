"""Translate editable tables and numerical batch outcomes at the UI boundary."""

from __future__ import annotations

from functools import partial

import pandas as pd

from options.vol_calibration.api import (
    BatchCalibrationRequest, calibrate_batch, PARAM_COLUMNS, expiry_month,
    fit_ttf_candidate as _fit_ttf_candidate, fit_jkm_candidate as _fit_jkm_candidate,
)

from vol_calibration import batch_results
from vol_calibration.observed_fit_pool import execute_hybrid_starts, prefit_observed_expiries

# The numerical policy belongs to the engine; the application supplies dispatch.
fit_ttf_candidate = partial(_fit_ttf_candidate, start_executor=execute_hybrid_starts)
fit_jkm_candidate = partial(_fit_jkm_candidate, start_executor=execute_hybrid_starts)

_PARAMETER_FIELDS = {
    *PARAM_COLUMNS, "expiry", "left_blend_width", "right_blend_width",
    "core_tv_rmse", "tail_fit_tv_rmse", "iv_rmse", "rmse", "arb_status",
    "calibration_basis", "calibration_method", "calibration_policy_version",
    "fit_diagnostics",
}


def numeric_parameter_rows(table_data):
    """Remove presentation-only fields and normalize table percentages once."""
    frame = batch_results.parse_table_data(table_data)
    rows = []
    for record in frame.to_dict("records"):
        row = {key: value for key, value in record.items() if key in _PARAMETER_FIELDS}
        for name in ["rmse", "core_tv_rmse", "tail_fit_tv_rmse", "iv_rmse"]:
            if name in row:
                value = pd.to_numeric(pd.Series([row[name]]), errors="coerce").iloc[0]
                row[name] = float(value) if pd.notna(value) else None
        try:
            row["expiry"] = expiry_month(row.get("expiry")).to_timestamp().date().isoformat()
        except ValueError:
            pass  # The engine reports the existing missing-parameter-row error.
        rows.append(row)
    return rows


def numeric_node_overrides(node_store):
    overrides = {}
    for key, entry in (node_store or {}).items():
        if not isinstance(entry, dict):
            continue
        nodes = entry.get("nodes", entry)
        if not isinstance(nodes, dict):
            continue
        overrides[key] = {
            "nodes": dict(nodes),
            "strikes": entry.get("strikes", {}),
            "forward": entry.get("forward"), "dte": entry.get("dte"),
        }
    return overrides


def format_numeric_result(row):
    """Preserve the existing table/export formatting of one numeric result."""
    formatted = batch_results.format_batch_result_row(
        row["expiry"], row["status"], row.get("old_rmse"), row.get("new_rmse"),
        basis=row.get("basis"),
    )
    if row["status"] == "Success":
        formatted.update({
            "old_rmse": batch_results.format_tv_rmse(row.get("old_rmse")),
            "new_rmse": batch_results.format_tv_rmse(row.get("new_rmse")),
            "improvement": "-",
            "core_tv_rmse": batch_results.format_tv_rmse(row.get("core_tv_rmse")),
            "tail_fit_tv_rmse": batch_results.format_tv_rmse(row.get("tail_fit_tv_rmse")),
            "iv_rmse": f"{float(row['iv_rmse']) * 100:.2f}%",
            "blend_width": f"{float(row['blend_width']):.2f}",
            "min_g": f"{float(row['min_g']):.6f}",
            "method": row["method"], "fit_diagnostics": row.get("fit_diagnostics"),
        })
    if row["status"] == "Failed":
        formatted.update({key: value for key, value in row.items()
                          if key not in {"expiry", "status", "old_rmse", "new_rmse", "improvement", "basis"}})
    return formatted


def format_numeric_outcome(outcome, table_data):
    statuses = {expiry_month(row["expiry"]): row["status"] for row in outcome["results"]}
    updated = []
    for original, numeric in zip(table_data, outcome["parameter_rows"]):
        try:
            status = statuses.get(expiry_month(original.get("expiry")))
        except ValueError:
            status = None
        row = dict(original)
        if status in {"Success", "Skipped"}:
            row.update(numeric)
            row["expiry"] = original.get("expiry")
            row["rmse"] = batch_results.format_tv_rmse(numeric.get("rmse"))
            row["calibration_basis"] = str(numeric.get("calibration_basis", "")).title()
        updated.append(row)
    return {
        "results": [format_numeric_result(row) for row in outcome["results"]],
        "table_data": updated,
        **{name: outcome[name] for name in ["success_count", "skip_count", "fail_count"]},
    }


def execute_batch(
    product, market_data, table_data, *, skip_good=False, node_store=None,
    checkpoints=None, checkpoint_callback=None, cancellation_check=None,
):
    request = BatchCalibrationRequest(
        product=product, observations=market_data,
        parameter_rows=numeric_parameter_rows(table_data), skip_good=skip_good,
        node_overrides=numeric_node_overrides(node_store),
    )
    outcome = calibrate_batch(
        request, checkpoints=checkpoints, checkpoint_callback=checkpoint_callback,
        cancellation_check=cancellation_check,
        observed_executor=partial(prefit_observed_expiries,
                                  environment_variable=f"{product}_OBSERVED_FIT_WORKERS"),
        start_executor=execute_hybrid_starts,
    )
    return format_numeric_outcome(outcome, table_data)
