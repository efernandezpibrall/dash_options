"""Governed JKM candidates and chronological settlement batch calculations.

This module owns calculation policy and data transforms. It neither registers
Dash callbacks nor publishes a completed candidate.
"""

from vol_calibration import batch_results, calibration_inputs

import numpy as np
import pandas as pd

from options.calibration_engine.io.storage import PARAM_COLUMNS
from vol_calibration.ttf_hybrid_surface import HybridFitNoCandidate
from vol_calibration.jkm_hybrid_surface import (
    JKM_HYBRID_METHOD,
    JKM_HYBRID_POLICY_VERSION,
    evaluate_jkm_hybrid_candidate,
    fit_jkm_hybrid_candidate,
)
from vol_calibration.batch_results import format_batch_result_row, parse_table_data
from vol_calibration.calibration_inputs import (
    calibration_eligibility_error, expiry_month,
)
from vol_calibration.observed_fit_pool import prefit_observed_expiries
from vol_calibration.batch_checkpoints import (
    digest as checkpoint_digest, expiry_input_fingerprint,
    make_checkpoint, verified_checkpoint,
)


JKM_OBSERVED_STARTS = 3
JKM_EXTRAPOLATED_STARTS = 3
JKM_EXTRAPOLATED_RETRY_STARTS = 9


def _calibration_basis(observations):
    values = {
        value
        for value in observations.get('calibration_basis', pd.Series(dtype=str))
        .dropna()
        .astype(str)
        .str.strip()
        .str.lower()
        if value
    }
    if values not in ({'observed'}, {'extrapolated'}):
        raise ValueError("JKM calibration inputs have an invalid basis.")
    return next(iter(values))


def _evaluate_existing_hybrid(observations, values):
    left_width = pd.to_numeric(
        pd.Series([values.get('left_blend_width')]), errors='coerce'
    ).iloc[0]
    right_width = pd.to_numeric(
        pd.Series([values.get('right_blend_width')]), errors='coerce'
    ).iloc[0]
    if not np.isfinite(left_width) or not np.isfinite(right_width):
        raise ValueError("No accepted JKM PCHIP/Wing join is available for this row.")
    return evaluate_jkm_hybrid_candidate(
        observations,
        batch_results.model_params(values),
        left_blend_width=float(left_width),
        right_blend_width=float(right_width),
    )


def _accepted_calibration_result(result):
    if not isinstance(result, dict):
        return False
    params = result.get('params')
    validation = result.get('validation')
    rmse = pd.to_numeric(
        pd.Series([result.get('tail_fit_tv_rmse')]), errors='coerce'
    ).iloc[0]
    if (
        not isinstance(params, dict)
        or not np.isfinite(rmse)
        or not isinstance(validation, dict)
        or not validation.get('is_valid', False)
    ):
        return False
    try:
        values = np.asarray([params[name] for name in PARAM_COLUMNS], dtype=float)
    except (KeyError, TypeError, ValueError):
        return False
    return bool(np.all(np.isfinite(values)))


def _run_jkm_candidate(observations, initial_params, *, basis):
    if basis not in {'observed', 'extrapolated'}:
        raise ValueError(f"Unsupported JKM calibration basis: {basis}.")
    starts = (
        JKM_OBSERVED_STARTS
        if basis == 'observed'
        else JKM_EXTRAPOLATED_STARTS
    )
    attempts = []
    first_error = None
    try:
        attempts.append(
            fit_jkm_hybrid_candidate(
                observations,
                batch_results.model_params(initial_params),
                n_starts=starts,
                seed=42,
            )
        )
    except Exception as exc:
        first_error = exc
    if (
        basis == 'extrapolated'
        and not any(_accepted_calibration_result(item) for item in attempts)
    ):
        try:
            resume = (
                {
                    'resume_start_count': starts,
                    'resume_attempts': first_error.attempts,
                }
                if isinstance(first_error, HybridFitNoCandidate)
                else {}
            )
            attempts.append(
                fit_jkm_hybrid_candidate(
                    observations,
                    batch_results.model_params(initial_params),
                    n_starts=JKM_EXTRAPOLATED_RETRY_STARTS,
                    seed=42,
                    **resume,
                )
            )
        except Exception:
            pass
    accepted = [item for item in attempts if _accepted_calibration_result(item)]
    if not accepted:
        if first_error is not None:
            raise first_error
        raise ValueError("JKM calibration produced no butterfly-valid hybrid.")
    return min(accepted, key=lambda item: float(item['tail_fit_tv_rmse']))


def _update_hybrid_row(row, result, basis):
    for name, value in batch_results.candidate_params(result).items():
        row[name] = float(value)
    row['core_tv_rmse'] = float(result['core_tv_rmse'])
    row['tail_fit_tv_rmse'] = float(result['tail_fit_tv_rmse'])
    row['iv_rmse'] = float(result['iv_rmse'])
    row['rmse'] = batch_results.format_tv_rmse(result['core_tv_rmse'])
    row['arb_status'] = 'Pass'
    row['calibration_basis'] = basis.title()
    row['calibration_method'] = JKM_HYBRID_METHOD
    row['calibration_policy_version'] = JKM_HYBRID_POLICY_VERSION


def _fit_jkm_observed_task(task):
    key, observations, initial_params = task
    try:
        return key, (True, _run_jkm_candidate(
            observations, initial_params, basis='observed'
        ))
    except Exception as exc:
        return key, (False, str(exc))


def calibrate_jkm_batch(
    market_data, table_data, *, skip_good=False, checkpoints=None,
    checkpoint_callback=None, cancellation_check=None,
):
    """Chronologically calibrate all observed and governed extrapolated smiles."""
    params_df = parse_table_data(table_data)
    updated = [dict(row) for row in table_data]
    row_by_period = {}
    for index, row in enumerate(table_data):
        try:
            row_by_period[expiry_month(row.get('expiry'))] = index
        except ValueError:
            continue

    expiries = sorted(market_data['expiry'].dropna().unique())
    checkpoints = checkpoints or {}
    observed_tasks = []
    if not skip_good and len(expiries) >= 8:
        for expiry in expiries:
            if pd.Timestamp(expiry).strftime('%Y-%m-%d') in checkpoints:
                continue
            try:
                row_index = row_by_period[expiry_month(expiry)]
                observations = calibration_inputs.select_hybrid_expiry_inputs(market_data, expiry)
                if (
                    _calibration_basis(observations) == 'observed'
                    and not calibration_eligibility_error(observations)
                ):
                    initial = batch_results.model_params(params_df.iloc[row_index].to_dict())
                    observed_tasks.append(
                        (pd.Timestamp(expiry).isoformat(), observations, initial)
                    )
            except (KeyError, IndexError, ValueError, TypeError):
                # The chronological loop reports the original per-expiry error.
                continue
    prefitted = prefit_observed_expiries(
        observed_tasks,
        _fit_jkm_observed_task,
        environment_variable='JKM_OBSERVED_FIT_WORKERS',
    )

    results = []
    success_count = 0
    skip_count = 0
    fail_count = 0
    last_successful_params = None
    for expiry in expiries:
        expiry_str = pd.Timestamp(expiry).strftime('%Y-%m-%d')
        row_index = row_by_period.get(expiry_month(expiry))
        if cancellation_check is not None:
            cancellation_check()
        input_fingerprint = expiry_input_fingerprint(
            market_data, expiry,
            table_data[row_index] if row_index is not None else None,
            policy=JKM_HYBRID_POLICY_VERSION,
            skip_good=skip_good,
        )
        dependency_fingerprint = checkpoint_digest(last_successful_params)
        if expiry_str in checkpoints:
            saved = verified_checkpoint(
                checkpoints[expiry_str],
                expiry=expiry_str,
                input_fingerprint=input_fingerprint,
                dependency_fingerprint=dependency_fingerprint,
            )
            updated[row_index] = dict(saved['updated_row'])
            last_successful_params = dict(saved['warm_start_after'])
            results.append(dict(saved['result_row']))
            if saved['result_row']['status'] == 'Skipped':
                skip_count += 1
            else:
                success_count += 1
            continue
        basis = None
        old_rmse = None
        result_count_before = len(results)
        try:
            if row_index is None or row_index >= len(params_df):
                raise ValueError("No editable parameter row exists for this expiry.")
            observations = calibration_inputs.select_hybrid_expiry_inputs(market_data, expiry)
            basis = _calibration_basis(observations)
            eligibility_error = calibration_eligibility_error(observations)
            if eligibility_error:
                raise ValueError(eligibility_error)
            current_values = params_df.iloc[row_index].to_dict()
            current_params = batch_results.model_params(current_values)
            try:
                current_result = _evaluate_existing_hybrid(
                    observations,
                    current_values,
                )
                old_rmse = float(current_result['core_tv_rmse'])
            except Exception:
                current_result = None

            if (
                basis == 'observed'
                and skip_good
                and current_result is not None
                and current_result['validation']['is_valid']
            ):
                _update_hybrid_row(updated[row_index], current_result, basis)
                last_successful_params = batch_results.model_params(current_result['params'])
                results.append(
                    format_batch_result_row(
                        expiry_str,
                        'Skipped',
                        old_rmse,
                        old_rmse,
                        basis=basis,
                    )
                )
                skip_count += 1
                continue

            initial_params = (
                current_params if basis == 'observed' else last_successful_params
            )
            if initial_params is None:
                raise ValueError(
                    "No successful observed JKM calibration is available to seed "
                    "the extrapolated tail."
                )
            prefitted_result = (
                prefitted.get(pd.Timestamp(expiry).isoformat())
                if basis == 'observed' else None
            )
            if prefitted_result is None:
                candidate = _run_jkm_candidate(
                    observations, initial_params, basis=basis,
                )
            else:
                succeeded, payload = prefitted_result
                if not succeeded:
                    raise ValueError(payload)
                candidate = payload
            _update_hybrid_row(updated[row_index], candidate, basis)
            last_successful_params = batch_results.model_params(candidate['params'])
            results.append(
                {
                    **format_batch_result_row(
                        expiry_str,
                        'Success',
                        old_rmse,
                        float(candidate['core_tv_rmse']),
                        basis=basis,
                    ),
                    'old_rmse': batch_results.format_tv_rmse(old_rmse),
                    'new_rmse': batch_results.format_tv_rmse(candidate['core_tv_rmse']),
                    'improvement': '-',
                    'core_tv_rmse': batch_results.format_tv_rmse(candidate['core_tv_rmse']),
                    'tail_fit_tv_rmse': batch_results.format_tv_rmse(
                        candidate['tail_fit_tv_rmse']
                    ),
                    'iv_rmse': f"{float(candidate['iv_rmse']) * 100:.2f}%",
                    'blend_width': f"{float(candidate['left_blend_width']):.2f}",
                    'min_g': f"{float(candidate['validation']['min_g']):.6f}",
                    'method': JKM_HYBRID_METHOD,
                }
            )
            success_count += 1
        except Exception as exc:
            if basis is None and row_index is not None:
                basis = str(
                    table_data[row_index].get('calibration_basis', '')
                ).strip().lower() or None
            failed = format_batch_result_row(
                expiry_str,
                'Failed',
                old_rmse,
                None,
                basis=basis,
            )
            failed['error'] = str(exc)
            results.append(failed)
            fail_count += 1
        finally:
            if checkpoint_callback is not None and len(results) > result_count_before:
                checkpoint_callback(make_checkpoint(
                    expiry=expiry_str,
                    result_row=results[-1],
                    updated_row=(
                        updated[row_index] if row_index is not None else None
                    ),
                    warm_start_after=last_successful_params,
                    input_fingerprint=input_fingerprint,
                    dependency_fingerprint=dependency_fingerprint,
                ))
    return {
        'results': results,
        'table_data': updated,
        'success_count': success_count,
        'skip_count': skip_count,
        'fail_count': fail_count,
    }
