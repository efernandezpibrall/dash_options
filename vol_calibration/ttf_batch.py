"""Governed TTF candidates and chronological settlement batch calculations.

This module owns calculation policy and data transforms. It neither registers
Dash callbacks nor publishes a completed candidate.
"""

from vol_calibration import batch_results, calibration_inputs

import numpy as np
import pandas as pd

from options.calibration_engine.io.storage import PARAM_COLUMNS
from options.ttf_volatility import delta_node_to_strike
from options.calibration_engine.config.calibration_policies import TTF_WING_V2_OPTIMIZER_OPTIONS
from vol_calibration.ttf_hybrid_surface import (
    TTF_HYBRID_METHOD,
    TTF_HYBRID_POLICY_VERSION,
    evaluate_ttf_hybrid_candidate,
    fit_ttf_hybrid_candidate,
    HybridFitNoCandidate,
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


TTF_EXTRAPOLATED_STARTS = 3
TTF_EXTRAPOLATED_RETRY_STARTS = 9


def _calibration_basis(observations):
    if observations is None or observations.empty:
        raise ValueError("TTF calibration inputs are unavailable.")
    values = {
        value
        for value in observations['calibration_basis']
        .dropna()
        .astype(str)
        .str.strip()
        .str.lower()
        if value
    }
    if values not in ({'observed'}, {'extrapolated'}):
        raise ValueError("TTF calibration inputs have an invalid basis.")
    return next(iter(values))


def _evaluate_existing_hybrid(observations, values):
    left_width = pd.to_numeric(
        pd.Series([values.get('left_blend_width')]), errors='coerce'
    ).iloc[0]
    right_width = pd.to_numeric(
        pd.Series([values.get('right_blend_width')]), errors='coerce'
    ).iloc[0]
    if not np.isfinite(left_width) or not np.isfinite(right_width):
        raise ValueError("No accepted PCHIP/Wing join is available for this row.")
    return evaluate_ttf_hybrid_candidate(
        observations,
        batch_results.model_params(values),
        left_blend_width=float(left_width),
        right_blend_width=float(right_width),
    )


def _expiry_store_key(value):
    return str(expiry_month(value))


def _apply_node_edits(market_data, node_store, expiry=None):
    """Apply validated in-session node vols to a copy of the market frame."""
    edited = market_data.copy()
    payload = node_store or {}
    target_key = _expiry_store_key(expiry) if expiry is not None else None
    for key, values in payload.items():
        if target_key is not None and key != target_key:
            continue
        if not isinstance(values, dict):
            continue
        entry = values
        values = entry.get('nodes', entry)
        if not isinstance(values, dict):
            continue
        periods = pd.to_datetime(edited['expiry'], errors='coerce').dt.to_period('M')
        mask = periods.astype(str) == key
        forward = pd.to_numeric(
            pd.Series([entry.get('forward')]), errors='coerce'
        ).iloc[0]
        dte = pd.to_numeric(pd.Series([entry.get('dte')]), errors='coerce').iloc[0]
        if np.isfinite(forward) and float(forward) > 0:
            edited.loc[mask, 'forward'] = float(forward)
        if np.isfinite(dte) and float(dte) > 0:
            edited.loc[mask, 'dte'] = float(dte)
        for delta_key, iv_value in values.items():
            delta = pd.to_numeric(pd.Series([delta_key]), errors='coerce').iloc[0]
            iv = pd.to_numeric(pd.Series([iv_value]), errors='coerce').iloc[0]
            if not np.isfinite(delta) or not np.isfinite(iv) or iv <= 0:
                continue
            delta_values = pd.to_numeric(edited['delta'], errors='coerce')
            edited.loc[mask & np.isclose(delta_values, delta, atol=1e-10), 'iv'] = iv
        strikes = entry.get('strikes', {})
        if isinstance(strikes, dict):
            for delta_key, strike_value in strikes.items():
                delta = pd.to_numeric(pd.Series([delta_key]), errors='coerce').iloc[0]
                strike = pd.to_numeric(
                    pd.Series([strike_value]), errors='coerce'
                ).iloc[0]
                if not np.isfinite(delta) or not np.isfinite(strike) or strike <= 0:
                    continue
                delta_values = pd.to_numeric(edited['delta'], errors='coerce')
                edited.loc[
                    mask & np.isclose(delta_values, delta, atol=1e-10),
                    'strike',
                ] = float(strike)
    return edited


def _settlement_ttf_observations(market_data, expiry):
    """Return the selected COB nodes on the governed working forward and DTE."""
    observations = calibration_inputs.select_hybrid_expiry_inputs(market_data, expiry).copy()
    forward = float(observations['forward'].iloc[0])
    dte = float(observations['dte'].iloc[0])
    observations['strike'] = [
        delta_node_to_strike(
            forward,
            dte / 365.25,
            float(delta),
            float(iv),
        )
        for delta, iv in zip(observations['delta'], observations['iv'])
    ]
    return observations


def _accepted_calibration_result(result):
    """Apply the finite-parameter and complete hybrid validation gate."""
    if not isinstance(result, dict):
        return False
    rmse = pd.to_numeric(
        pd.Series([result.get('tail_fit_tv_rmse')]), errors='coerce'
    ).iloc[0]
    params = result.get('params')
    validation = result.get('validation')
    if not np.isfinite(rmse) or not isinstance(params, dict):
        return False
    if not isinstance(validation, dict) or not validation.get('is_valid', False):
        return False
    try:
        values = np.asarray([params[name] for name in PARAM_COLUMNS], dtype=float)
    except (KeyError, TypeError, ValueError):
        return False
    return bool(np.all(np.isfinite(values)))


def _run_ttf_candidate(
    observations,
    initial_params,
    *,
    basis,
    selected_expiry=False,
):
    """Build one governed PCHIP-core / Wing-tail hybrid candidate."""
    if basis not in {'observed', 'extrapolated'}:
        raise ValueError(f"Unsupported TTF calibration basis: {basis}.")

    attempts = []
    first_error = None
    starts = TTF_EXTRAPOLATED_STARTS if basis == 'extrapolated' else 1
    try:
        attempts.append(
            fit_ttf_hybrid_candidate(
                observations,
                batch_results.model_params(initial_params),
                n_starts=starts,
                seed=int(TTF_WING_V2_OPTIMIZER_OPTIONS.get('seed', 42)),
            )
        )
    except Exception as exc:
        first_error = exc

    accepted_first = [result for result in attempts if _accepted_calibration_result(result)]
    # The PCHIP core is the operational smile inside the governed quote range;
    # tail-fit TV RMSE is only a Wing approximation diagnostic.  Retry the
    # extrapolated tail only when the first fit fails the complete hybrid gate,
    # rather than repeatedly chasing a diagnostic threshold that does not
    # improve the operational core.
    needs_retry = basis == 'extrapolated' and not accepted_first
    if needs_retry:
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
                fit_ttf_hybrid_candidate(
                    observations,
                    batch_results.model_params(initial_params),
                    n_starts=TTF_EXTRAPOLATED_RETRY_STARTS,
                    seed=int(TTF_WING_V2_OPTIMIZER_OPTIONS.get('seed', 42)),
                    **resume,
                )
            )
        except Exception:
            pass

    accepted = [result for result in attempts if _accepted_calibration_result(result)]
    if not accepted:
        if first_error is not None:
            raise first_error
        raise ValueError("TTF calibration produced no butterfly-valid candidate.")
    return min(
        accepted,
        key=lambda result: float(result['tail_fit_tv_rmse']),
    )


def _fit_ttf_observed_task(task):
    key, observations, initial_params = task
    try:
        return key, (True, _run_ttf_candidate(
            observations, initial_params, basis='observed',
            selected_expiry=False,
        ))
    except Exception as exc:
        return key, (False, str(exc))


def calibrate_ttf_batch(
    market_data, table_data, *, skip_good=False, node_store=None,
    checkpoints=None, checkpoint_callback=None, cancellation_check=None,
):
    """Calibrate the settlement batch independently of the Dash callback."""
    params_df = parse_table_data(table_data)

    expiries = sorted(market_data['expiry'].dropna().unique())
    checkpoints = checkpoints or {}
    results = []
    updated_table_data = table_data.copy()
    row_index_by_expiry = {}
    for row_index, row in enumerate(table_data):
        try:
            row_index_by_expiry[expiry_month(row.get('expiry'))] = row_index
        except ValueError:
            continue

    observed_tasks = []
    if not skip_good and len(expiries) >= 8:
        for expiry in expiries:
            if pd.Timestamp(expiry).strftime('%Y-%m-%d') in checkpoints:
                continue
            try:
                row_index = row_index_by_expiry[expiry_month(expiry)]
                observations = _settlement_ttf_observations(market_data, expiry)
                observations = _apply_node_edits(observations, node_store, expiry)
                observations = calibration_inputs.select_hybrid_expiry_inputs(observations, expiry)
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
        _fit_ttf_observed_task,
        environment_variable='TTF_OBSERVED_FIT_WORKERS',
    )

    success_count = 0
    skip_count = 0
    fail_count = 0
    last_successful_params = None

    for expiry in expiries:
        expiry_str = pd.to_datetime(expiry).strftime('%Y-%m-%d')
        row_index = row_index_by_expiry.get(expiry_month(expiry))
        if cancellation_check is not None:
            cancellation_check()
        input_fingerprint = expiry_input_fingerprint(
            market_data, expiry,
            table_data[row_index] if row_index is not None else None,
            policy=TTF_HYBRID_POLICY_VERSION,
            skip_good=skip_good,
            node_edits=(node_store or {}).get(str(expiry_month(expiry))),
        )
        dependency_fingerprint = checkpoint_digest(last_successful_params)
        if expiry_str in checkpoints:
            saved = verified_checkpoint(
                checkpoints[expiry_str],
                expiry=expiry_str,
                input_fingerprint=input_fingerprint,
                dependency_fingerprint=dependency_fingerprint,
            )
            updated_table_data[row_index] = dict(saved['updated_row'])
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
            # Calibrate All establishes the selected settlement surface.  The
            # previous publication remains the manual intraday adjustment base,
            # but it must never replace the settlement IV target here.
            exp_data = _settlement_ttf_observations(market_data, expiry)
            exp_data = _apply_node_edits(exp_data, node_store, expiry)
            exp_data = calibration_inputs.select_hybrid_expiry_inputs(exp_data, expiry)
            basis = _calibration_basis(exp_data)
            eligibility_error = calibration_eligibility_error(exp_data)
            if eligibility_error:
                raise ValueError(eligibility_error)
            current_values = params_df.iloc[row_index].to_dict()
            current_params = batch_results.model_params(current_values)

            try:
                current_result = _evaluate_existing_hybrid(
                    exp_data,
                    current_values,
                )
                old_rmse = float(current_result['core_tv_rmse'])
            except Exception:
                current_result = None
                old_rmse = None

            if (
                basis == 'observed'
                and skip_good
                and old_rmse is not None
                and current_result is not None
                and current_result['validation']['is_valid']
            ):
                results.append(
                    format_batch_result_row(
                        expiry_str,
                        'Skipped',
                        old_rmse,
                        old_rmse,
                        basis=basis,
                    )
                )
                # A governed, already-good observed row remains a valid warm
                # start.  Without retaining it, Skip good fits could sever the
                # sequential chain before the extrapolated tail.
                updated_table_data[row_index]['left_blend_width'] = float(
                    current_result['left_blend_width']
                )
                updated_table_data[row_index]['right_blend_width'] = float(
                    current_result['right_blend_width']
                )
                updated_table_data[row_index]['core_tv_rmse'] = old_rmse
                updated_table_data[row_index]['tail_fit_tv_rmse'] = float(
                    current_result['tail_fit_tv_rmse']
                )
                updated_table_data[row_index]['iv_rmse'] = float(
                    current_result['iv_rmse']
                )
                updated_table_data[row_index]['rmse'] = batch_results.format_tv_rmse(old_rmse)
                updated_table_data[row_index]['arb_status'] = 'Pass'
                updated_table_data[row_index]['calibration_basis'] = basis.title()
                updated_table_data[row_index]['calibration_method'] = (
                    TTF_HYBRID_METHOD
                )
                updated_table_data[row_index]['calibration_policy_version'] = (
                    TTF_HYBRID_POLICY_VERSION
                )
                last_successful_params = current_params.copy()
                skip_count += 1
                continue

            initial_params = (
                last_successful_params
                if basis == 'extrapolated'
                else current_params
            )
            if initial_params is None:
                raise ValueError(
                    "No successful observed TTF calibration is available to "
                    "seed the extrapolated tail."
                )
            prefitted_result = (
                prefitted.get(pd.Timestamp(expiry).isoformat())
                if basis == 'observed' else None
            )
            if prefitted_result is None:
                result = _run_ttf_candidate(
                    exp_data, initial_params, basis=basis,
                    selected_expiry=False,
                )
            else:
                succeeded, payload = prefitted_result
                if not succeeded:
                    raise ValueError(payload)
                result = payload
            new_params = batch_results.model_params(result['params'])
            new_rmse = float(result['core_tv_rmse'])

            for param_key, param_val in new_params.items():
                if param_key in updated_table_data[row_index]:
                    updated_table_data[row_index][param_key] = param_val
            updated_table_data[row_index]['left_blend_width'] = float(
                result['left_blend_width']
            )
            updated_table_data[row_index]['right_blend_width'] = float(
                result['right_blend_width']
            )
            updated_table_data[row_index]['core_tv_rmse'] = new_rmse
            updated_table_data[row_index]['tail_fit_tv_rmse'] = float(
                result['tail_fit_tv_rmse']
            )
            updated_table_data[row_index]['iv_rmse'] = float(result['iv_rmse'])
            updated_table_data[row_index]['rmse'] = batch_results.format_tv_rmse(new_rmse)
            updated_table_data[row_index]['arb_status'] = 'Pass'
            updated_table_data[row_index]['calibration_basis'] = basis.title()
            updated_table_data[row_index]['calibration_method'] = TTF_HYBRID_METHOD
            updated_table_data[row_index]['calibration_policy_version'] = (
                TTF_HYBRID_POLICY_VERSION
            )
            last_successful_params = new_params.copy()

            results.append(
                {
                    **format_batch_result_row(
                    expiry_str,
                    'Success',
                    old_rmse,
                    new_rmse,
                    basis=basis,
                    ),
                    'old_rmse': batch_results.format_tv_rmse(old_rmse),
                    'new_rmse': batch_results.format_tv_rmse(new_rmse),
                    'improvement': '-',
                    'core_tv_rmse': batch_results.format_tv_rmse(new_rmse),
                    'tail_fit_tv_rmse': batch_results.format_tv_rmse(
                        result['tail_fit_tv_rmse']
                    ),
                    'iv_rmse': f"{float(result['iv_rmse']) * 100:.2f}%",
                    'blend_width': f"{float(result['left_blend_width']):.2f}",
                    'min_g': f"{float(result['validation']['min_g']):.6f}",
                    'method': TTF_HYBRID_METHOD,
                }
            )
            success_count += 1
        except Exception:
            if basis is None and row_index is not None:
                basis = str(
                    table_data[row_index].get('calibration_basis', '')
                ).strip().lower() or None
            results.append(
                format_batch_result_row(
                    expiry_str,
                    'Failed',
                    old_rmse,
                    None,
                    basis=basis,
                )
            )
            fail_count += 1
        finally:
            if checkpoint_callback is not None and len(results) > result_count_before:
                checkpoint_callback(make_checkpoint(
                    expiry=expiry_str,
                    result_row=results[-1],
                    updated_row=(
                        updated_table_data[row_index]
                        if row_index is not None else None
                    ),
                    warm_start_after=last_successful_params,
                    input_fingerprint=input_fingerprint,
                    dependency_fingerprint=dependency_fingerprint,
                ))

    return {
        'results': results,
        'table_data': updated_table_data,
        'success_count': success_count,
        'skip_count': skip_count,
        'fail_count': fail_count,
    }
