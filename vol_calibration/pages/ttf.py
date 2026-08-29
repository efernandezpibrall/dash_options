"""
TTF commodity page.

Implements Framework Section 4.1:
- Header with date picker and action buttons
- Excel-like parameter table
- Smile plot grid (3xN)
- Three-way comparison modal for calibration
"""
from datetime import date
import hashlib
from io import StringIO, BytesIO
import json
from uuid import uuid4

import pandas as pd
import numpy as np
import dash_bootstrap_components as dbc
from dash import html, dcc, dash_table, callback, Input, Output, State, no_update, ctx
from dash.exceptions import PreventUpdate
from flask import has_request_context, request

from vol_calibration.components.parameter_table import (
    create_parameter_table,
    format_params_for_table,
    parse_table_data,
)
from vol_calibration.components.smile_grid import create_smile_grid, create_smile_grid_figure
from vol_calibration.components.comparison_modal import (
    create_comparison_modal,
    create_comparison_plot,
    format_comparison_data,
    extract_final_params
)
from vol_calibration.components.data_status import format_data_status
from vol_calibration.data_cache import cached_workspace_callback
from vol_calibration.components.batch_calibration_modal import (
    create_batch_calibration_confirm_modal,
    create_batch_calibration_progress_modal,
    format_batch_result_row,
    create_batch_summary,
    create_batch_results_table,
)
from vol_calibration.feature_flags import (
    ttf_intraday_writes_enabled,
    ttf_publication_enabled,
    writes_enabled as _legacy_writes_enabled,
)
from vol_calibration.auth import resolve_request_identity
from vol_calibration.calibration_inputs import (
    calibration_eligibility_error,
    calibration_readiness,
    expiry_month,
    select_expiry_observations,
)
from vol_calibration.model_version import (
    DEFAULT_CALIBRATION_MODEL_VERSION,
)
from vol_calibration.ttf_hybrid_surface import (
    TTF_HYBRID_METHOD,
    TTF_HYBRID_POLICY_VERSION,
    evaluate_ttf_hybrid_candidate,
    fit_ttf_hybrid_candidate,
    hybrid_iv,
    operational_surface_frame as ttf_hybrid_operational_surface_frame,
)
from vol_calibration.operational_surface import (
    create_operational_surface_status,
    create_operational_surface_store,
    operational_surface_frame,
    register_operational_surface_callback,
)
from vol_calibration.ttf_traded_options import (
    create_ttf_traded_options_status,
    create_ttf_traded_options_store,
    register_ttf_traded_options_callback,
    ttf_traded_options_frame,
)
from vol_calibration.components.ttf_intraday_workspace import (
    create_ttf_adjustment_workspace,
    create_ttf_context_status,
    create_ttf_intraday_trade_panel,
    create_ttf_publication_status,
)
from vol_calibration.ttf_adjustments import apply_ttf_smile_adjustments
from vol_calibration.ttf_intraday import (
    TTF_INTRADAY_PREMIUM_METHOD,
    load_ttf_intraday_trades,
    normalize_ttf_intraday_trade,
    persist_ttf_intraday_trade,
)
from vol_calibration.ttf_market_context import (
    load_ttf_trading_context,
    serialize_ttf_trading_context,
)
from vol_calibration.ttf_publication import (
    load_latest_ttf_publication,
    publish_ttf_surface,
    ttf_publication_frame,
)
from options.ttf_volatility import delta_node_to_strike

# Import calibration engine modules
from options.calibration_engine.io.loaders import (
    load_market_data_with_metadata,
)
from options.calibration_engine.config.defaults import get_defaults
from options.calibration_engine.config.calibration_policies import (
    TTF_WING_V2_OPTIMIZER_OPTIONS,
)
from options.calibration_engine.io.storage import (
    get_database_engine,
    load_latest_surface_from_db,
    PARAM_COLUMNS
)

COMMODITY = 'TTF'
COMMODITY_LOWER = COMMODITY.lower()
# Retained for shared legacy-save tests/callers; TTF publication uses its
# separately scoped feature flag and never routes through this switch.
writes_enabled = _legacy_writes_enabled
TTF_EXTRAPOLATED_STARTS = 3
TTF_EXTRAPOLATED_RETRY_STARTS = 9
TTF_BATCH_STATE_VERSION = 2
TTF_BATCH_CALIBRATION_TARGET = 'settlement_nodes'
TTF_INTRADAY_CALIBRATION_TARGET = 'latest_published_smile'
TTF_NODE_REPRODUCTION_ATOL = 1e-10
TTF_ADVANCED_PARAMS = [
    *PARAM_COLUMNS,
    'left_blend_width',
    'right_blend_width',
]


def _select_ttf_expiry_inputs(market_data, expiry):
    """Return a complete governed observed or extrapolated TTF smile."""
    return select_expiry_observations(
        market_data,
        expiry,
        include_extrapolated=True,
    )


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


def _model_params(values):
    """Extract only Wing parameters from an editable table/store mapping."""
    return {
        name: float(values[name])
        for name in PARAM_COLUMNS
        if name in values and pd.notna(values[name])
    }


def _candidate_params(result):
    """Return editable tail parameters plus the selected join widths."""
    return {
        **_model_params(result.get('params', {})),
        'left_blend_width': float(result['left_blend_width']),
        'right_blend_width': float(result['right_blend_width']),
    }


def _changed_ttf_tail_overrides(tail_row, initial_row):
    """Return only expert values the trader actually changed.

    The expert table is populated from the editable row before calibration.
    Treating all displayed values as overrides would replace the newly fitted
    tail with that stale snapshot and can invalidate an otherwise acceptable
    hybrid.  An unchanged editor therefore contributes no override.
    """
    overrides = {}
    for name in (
        'dc',
        'uc',
        'put_wing_power',
        'call_wing_power',
        'left_blend_width',
        'right_blend_width',
    ):
        value = pd.to_numeric(
            pd.Series([(tail_row or {}).get(name)]), errors='coerce'
        ).iloc[0]
        initial_value = pd.to_numeric(
            pd.Series([(initial_row or {}).get(name)]), errors='coerce'
        ).iloc[0]
        if np.isfinite(value) and (
            not np.isfinite(initial_value)
            or not np.isclose(float(value), float(initial_value), atol=1e-12)
        ):
            overrides[name] = float(value)
    return overrides


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
        _model_params(values),
        left_blend_width=float(left_width),
        right_blend_width=float(right_width),
    )


def _format_tv_rmse(value):
    numeric = pd.to_numeric(pd.Series([value]), errors='coerce').iloc[0]
    return f"{float(numeric):.6f}" if np.isfinite(numeric) else "Unavailable"


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


def _published_node_values(publication_payload, observations):
    """Interpolate only saved IVs onto the governed delta nodes."""
    published = ttf_publication_frame(publication_payload)
    if published.empty or 'contract_date' not in published.columns:
        return None
    expiry = expiry_month(observations['expiry'].iloc[0])
    periods = pd.to_datetime(
        published['contract_date'], errors='coerce'
    ).dt.to_period('M')
    expiry_rows = published[periods == expiry].copy()
    if expiry_rows.empty:
        return None
    for column in ('delta', 'volatility'):
        if column in expiry_rows.columns:
            expiry_rows[column] = pd.to_numeric(
                expiry_rows[column], errors='coerce'
            )
    expiry_rows = expiry_rows.dropna(subset=['delta', 'volatility']).sort_values(
        'delta'
    )
    expiry_rows = expiry_rows.drop_duplicates('delta', keep='last')
    if len(expiry_rows) < 2:
        return None
    target_deltas = pd.to_numeric(observations['delta'], errors='coerce').to_numpy(
        dtype=float
    )
    source_deltas = expiry_rows['delta'].to_numpy(dtype=float)
    if (
        target_deltas.min() < source_deltas.min() - 1e-8
        or target_deltas.max() > source_deltas.max() + 1e-8
    ):
        return None
    return {
        'ivs': np.interp(
            target_deltas,
            source_deltas,
            expiry_rows['volatility'].to_numpy(dtype=float),
        ),
    }


def _settlement_ttf_observations(market_data, expiry):
    """Return the selected COB nodes on the governed working forward and DTE."""
    observations = _select_ttf_expiry_inputs(market_data, expiry).copy()
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


def _base_ttf_observations(market_data, expiry, publication_payload=None):
    """Return the manual intraday base, preferring the latest published smile."""
    observations = _settlement_ttf_observations(market_data, expiry)
    published = _published_node_values(publication_payload, observations)
    if published is not None:
        observations['iv'] = published['ivs']
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


def _published_expiry_result(publication_payload, observations):
    """Return the saved tail parameters for one governed option expiry."""
    target = pd.to_datetime(
        observations['option_expiration_date'].iloc[0], errors='coerce'
    )
    if pd.isna(target):
        return None
    for item in (publication_payload or {}).get('expiry_results') or []:
        item_expiry = pd.to_datetime(
            item.get('option_expiration_date'), errors='coerce'
        )
        if not pd.isna(item_expiry) and item_expiry.date() == target.date():
            return item
    return None


def _apply_published_parameters(table_rows, market_data, publication_payload):
    """Overlay the latest saved tail fit on the editable parameter rows."""
    updated = [dict(row) for row in (table_rows or [])]
    if not updated or market_data is None or market_data.empty:
        return updated
    rows_by_period = {
        expiry_month(row.get('expiry')): row
        for row in updated
        if row.get('expiry') is not None
    }
    for expiry in sorted(market_data['expiry'].dropna().unique()):
        try:
            observations = _select_ttf_expiry_inputs(market_data, expiry)
        except ValueError:
            continue
        saved = _published_expiry_result(publication_payload, observations)
        if not saved:
            continue
        target = rows_by_period.get(expiry_month(expiry))
        parameters = saved.get('parameters') or {}
        if target is None or not isinstance(parameters, dict):
            continue
        for name in (*PARAM_COLUMNS, 'left_blend_width', 'right_blend_width'):
            value = pd.to_numeric(
                pd.Series([parameters.get(name)]), errors='coerce'
            ).iloc[0]
            if np.isfinite(value):
                target[name] = float(value)
        diagnostics = saved.get('diagnostics') or {}
        for name in ('core_tv_rmse', 'tail_fit_tv_rmse', 'iv_rmse'):
            value = pd.to_numeric(
                pd.Series([diagnostics.get(name)]), errors='coerce'
            ).iloc[0]
            if np.isfinite(value):
                target[name] = float(value)
        validation = saved.get('validation') or {}
        if validation.get('is_valid', False):
            target['arb_status'] = 'Pass'
        target['rmse'] = _format_tv_rmse(target.get('core_tv_rmse', 0.0))
        target['calibration_method'] = TTF_HYBRID_METHOD
        target['calibration_policy_version'] = TTF_HYBRID_POLICY_VERSION
    return updated


def _same_day_publication_id(publication_payload, trading_date):
    """Return the optimistic-concurrency ID only for the selected working date."""
    publication_id = (publication_payload or {}).get('publication_id')
    publication_date = pd.to_datetime(
        (publication_payload or {}).get('publication_date'), errors='coerce'
    )
    selected = pd.to_datetime(trading_date, errors='coerce')
    if (
        publication_id
        and not pd.isna(publication_date)
        and not pd.isna(selected)
        and publication_date.date() == selected.date()
    ):
        return str(publication_id)
    return None


def _published_surface_for_market(publication_payload, market_data):
    """Re-express the saved IV-by-delta surface on current forwards and DTEs."""
    published = ttf_publication_frame(publication_payload)
    if published.empty or market_data is None or market_data.empty:
        return published
    frames = []
    published_periods = pd.to_datetime(
        published.get('contract_date'), errors='coerce'
    ).dt.to_period('M')
    for expiry in sorted(market_data['expiry'].dropna().unique()):
        try:
            observations = _select_ttf_expiry_inputs(market_data, expiry)
        except ValueError:
            continue
        rows = published[published_periods == expiry_month(expiry)].copy()
        if rows.empty:
            continue
        rows['delta'] = pd.to_numeric(rows.get('delta'), errors='coerce')
        rows['volatility'] = pd.to_numeric(
            rows.get('volatility'), errors='coerce'
        )
        rows = rows.dropna(subset=['delta', 'volatility'])
        rows = rows[
            rows['delta'].between(0.0, 1.0, inclusive='neither')
            & rows['volatility'].gt(0.0)
        ]
        if rows.empty:
            continue
        forward = float(observations['forward'].iloc[0])
        dte = float(observations['dte'].iloc[0])
        rows['strike'] = [
            delta_node_to_strike(
                forward,
                dte / 365.25,
                float(delta),
                float(iv),
            )
            for delta, iv in zip(rows['delta'], rows['volatility'])
        ]
        rows['total_variance'] = dte / 365.25 * rows['volatility'] ** 2
        rows['working_forward'] = forward
        rows['contract_date'] = pd.Timestamp(observations['expiry'].iloc[0])
        rows['option_expiration_date'] = pd.Timestamp(
            observations['option_expiration_date'].iloc[0]
        )
        frames.append(rows)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _create_ttf_node_editor():
    columns = [
        {'id': 'delta', 'name': 'Call Delta', 'type': 'numeric', 'editable': False},
        {'id': 'strike', 'name': 'Strike', 'type': 'numeric', 'editable': False},
        {
            'id': 'settlement_iv_pct',
            'name': 'Settlement IV (%)',
            'type': 'numeric',
            'editable': False,
        },
        {
            'id': 'published_iv_pct',
            'name': 'Published IV (%)',
            'type': 'numeric',
            'editable': False,
        },
        {
            'id': 'final_iv_pct',
            'name': 'Candidate IV (%)',
            'type': 'numeric',
            'editable': False,
        },
        {
            'id': 'change_vol_points',
            'name': 'Change (vol pts)',
            'type': 'numeric',
            'editable': False,
        },
    ]
    return html.Div(
        [
            html.Div(
                "Generated detail for the selected smile. Direct node overrides are "
                "available only after explicitly unlocking the table.",
                className="text-muted small mb-2",
            ),
            html.Div(id=f'{COMMODITY_LOWER}-node-editor-expiry', className="fw-bold mb-2"),
            dash_table.DataTable(
                id=f'{COMMODITY_LOWER}-node-editor',
                columns=columns,
                data=[],
                editable=True,
                page_action='none',
                style_table={'overflowX': 'auto'},
                style_header={
                    'backgroundColor': '#343a40',
                    'color': 'white',
                    'fontWeight': 'bold',
                    'textAlign': 'center',
                },
                style_cell={'textAlign': 'center', 'padding': '7px'},
                style_cell_conditional=[
                    {
                        'if': {'column_id': 'final_iv_pct'},
                        'backgroundColor': '#d1e7dd',
                        'fontWeight': 'bold',
                    }
                ],
            ),
            html.Div(id=f'{COMMODITY_LOWER}-node-editor-status', className="mt-2"),
        ]
    )


def _create_ttf_tail_editor():
    columns = [
        {'id': 'dc', 'name': 'Put tail onset x', 'type': 'numeric'},
        {'id': 'uc', 'name': 'Call tail onset x', 'type': 'numeric'},
        {'id': 'put_wing_power', 'name': 'PTVS', 'type': 'numeric'},
        {'id': 'call_wing_power', 'name': 'CTVS', 'type': 'numeric'},
        {'id': 'left_blend_width', 'name': 'Put blend width', 'type': 'numeric'},
        {'id': 'right_blend_width', 'name': 'Call blend width', 'type': 'numeric'},
    ]
    return dash_table.DataTable(
        id='ttf-tail-editor',
        columns=columns,
        data=[],
        editable=True,
        page_action='none',
        style_table={'overflowX': 'auto'},
        style_header={
            'backgroundColor': '#343a40',
            'color': 'white',
            'fontWeight': 'bold',
            'textAlign': 'center',
        },
        style_cell={
            'textAlign': 'center',
            'padding': '7px',
            'minWidth': '120px',
        },
    )


def _ttf_node_editor_rows(
    market_data,
    expiry,
    node_store=None,
    publication_payload=None,
):
    """Return normalized editor rows from original nodes plus accepted edits."""
    settlement_observations = _select_ttf_expiry_inputs(market_data, expiry)
    observations = _base_ttf_observations(
        market_data,
        expiry,
        publication_payload,
    )
    stored_entry = (node_store or {}).get(_expiry_store_key(expiry), {})
    stored = stored_entry.get('nodes', stored_entry)
    published = _published_node_values(publication_payload, settlement_observations)
    published_by_delta = {}
    if published is not None:
        published_by_delta = {
            f"{float(delta):.10f}": float(iv)
            for delta, iv in zip(settlement_observations['delta'], published['ivs'])
        }
    rows = []
    settlement_by_delta = {
        f"{float(row['delta']):.10f}": row
        for _, row in settlement_observations.iterrows()
    }
    for _, observation in observations.sort_values('delta').iterrows():
        delta = round(float(observation['delta']), 2)
        key = f"{delta:.10f}"
        final_iv = pd.to_numeric(
            pd.Series([stored.get(key, observation['iv'])]),
            errors='coerce',
        ).iloc[0]
        settlement_iv = float(settlement_by_delta[key]['iv'])
        published_iv = published_by_delta.get(key)
        comparison_iv = published_iv if published_iv is not None else settlement_iv
        rows.append(
            {
                'delta': delta,
                'strike': round(float(observation['strike']), 4),
                'settlement_iv_pct': round(settlement_iv * 100.0, 4),
                'published_iv_pct': (
                    round(published_iv * 100.0, 4)
                    if published_iv is not None
                    else None
                ),
                'final_iv_pct': round(float(final_iv) * 100.0, 4),
                'change_vol_points': round(
                    (float(final_iv) - comparison_iv) * 100.0,
                    4,
                ),
            }
        )
    return rows


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
                _model_params(initial_params),
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
            attempts.append(
                fit_ttf_hybrid_candidate(
                    observations,
                    _model_params(initial_params),
                    n_starts=TTF_EXTRAPOLATED_RETRY_STARTS,
                    seed=int(TTF_WING_V2_OPTIMIZER_OPTIONS.get('seed', 42)),
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


def _calibration_blocked_status(message):
    return dbc.Alert(
        [
            html.Strong("Calibration blocked: "),
            str(message),
        ],
        color="danger",
        className="mb-0 py-1 px-2",
    )


def get_default_date():
    """Default to the desk trading date; settlement resolves independently."""
    return date.today()


def create_header():
    """Create the page header with date picker and actions."""
    return dbc.Row([
        dbc.Col([
            html.H4([
                html.Span("TTF", className="text-primary fw-bold"),
                html.Span(" Vol Surface", className="text-muted"),
            ], className="mb-0"),
        ], width="auto"),
        dbc.Col([
            dbc.InputGroup([
                dbc.InputGroupText(html.I(className="fas fa-calendar")),
                dcc.DatePickerSingle(
                    id=f'{COMMODITY_LOWER}-date-picker',
                    date=get_default_date(),
                    display_format='DD-MMM-YYYY',
                    className="form-control",
                ),
            ], size="sm"),
        ], width=3),
        dbc.Col([
            # Data status indicator with tooltip
            html.Div([
                html.Span(
                    id=f'{COMMODITY_LOWER}-data-status',
                    children=dbc.Badge(
                        [html.I(className="fas fa-spinner fa-spin me-1"), "Loading..."],
                        color="secondary",
                        pill=True,
                    ),
                ),
                dbc.Tooltip(
                    id=f'{COMMODITY_LOWER}-data-status-tooltip',
                    target=f'{COMMODITY_LOWER}-data-status',
                    placement="bottom",
                ),
            ], className="d-inline-block"),
        ], width="auto"),
        dbc.Col([
            dbc.ButtonGroup([
                dbc.Button(
                    [html.I(className="fas fa-sync-alt me-1"), "Reload"],
                    id=f'{COMMODITY_LOWER}-reload-btn',
                    color="secondary",
                    outline=True,
                    size="sm",
                ),
                dbc.Button(
                    [html.I(className="fas fa-magic me-1"), "Calibrate"],
                    id=f'{COMMODITY_LOWER}-calibrate-all-btn',
                    color="primary",
                    outline=True,
                    size="sm",
                    title="Refit the selected Wing tail to its PCHIP core",
                ),
                dbc.Button(
                    [html.I(className="fas fa-layer-group me-1"), "Calibrate All Expiries"],
                    id=f'{COMMODITY_LOWER}-batch-calibrate-btn',
                    color="primary",
                    size="sm",
                    title="Calibrate all expiries at once",
                ),
                dbc.Button(
                    [html.I(className="fas fa-save me-1"), "Save calibrated surface"],
                    id=f'{COMMODITY_LOWER}-save-all-btn',
                    color="success",
                    outline=True,
                    size="sm",
                    disabled=True,
                    title=(
                        "Run Calibrate All Expiries successfully before saving "
                        "the complete validated TTF surface."
                    ),
                ),
                dbc.Button(
                    [html.I(className="fas fa-file-excel me-1"), "Export"],
                    id=f'{COMMODITY_LOWER}-export-btn',
                    color="info",
                    outline=True,
                    size="sm",
                ),
            ]),
        ], width="auto", className="ms-auto"),
    ], className="mb-4 align-items-center")


# Page layout
layout = dbc.Container([
    # Header
    create_header(),

    # Hidden stores for data
    dcc.Store(id=f'{COMMODITY_LOWER}-market-data-store'),
    create_operational_surface_store(COMMODITY),
    create_ttf_traded_options_store(),
    dcc.Store(id=f'{COMMODITY_LOWER}-params-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-comparison-data-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-batch-results-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-final-nodes-store', storage_type='session'),
    dcc.Store(id='ttf-trading-context-store'),
    dcc.Store(id='ttf-published-surface-store'),
    dcc.Store(id='ttf-intraday-trades-store', storage_type='session'),
    dcc.Store(id='ttf-adjustment-store', storage_type='session'),

    # Main content
    create_ttf_context_status(),
    create_ttf_publication_status(),

    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader([
                    html.H6("Settlement, published, candidate, and trades", className="mb-0"),
                ]),
                dbc.CardBody([
                    create_operational_surface_status(COMMODITY),
                    create_ttf_traded_options_status(),
                    create_smile_grid(COMMODITY),
                ], className="p-2"),
            ]),
        ], width=12),
    ], className="mb-4"),

    create_ttf_intraday_trade_panel(),

    create_ttf_adjustment_workspace(
        _create_ttf_node_editor(),
        _create_ttf_tail_editor(),
    ),

    # The complete parameter row remains an internal callback/session contract.
    # Traders see only the six governed tail controls above.
    html.Div(create_parameter_table(COMMODITY), style={'display': 'none'}),

    # Comparison modal
    create_comparison_modal(
        COMMODITY,
        comparison_params=TTF_ADVANCED_PARAMS,
        param_labels={
            'left_blend_width': 'Left blend width',
            'right_blend_width': 'Right blend width',
        },
        show_basis=True,
        show_hybrid_metrics=True,
    ),

    # Batch calibration modals
    create_batch_calibration_confirm_modal(COMMODITY, hybrid=True),
    create_batch_calibration_progress_modal(COMMODITY),

    # Loading overlay
    dcc.Loading(
        id=f'{COMMODITY_LOWER}-loading',
        type='circle',
        children=html.Div(id=f'{COMMODITY_LOWER}-loading-output'),
    ),

    # Download component for Excel export
    dcc.Download(id=f'{COMMODITY_LOWER}-download-excel'),

], fluid=True)

register_operational_surface_callback(COMMODITY, get_default_date)
register_ttf_traded_options_callback(get_default_date)


# Callbacks

@callback(
    [Output(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     Output(f'{COMMODITY_LOWER}-params-store', 'data'),
     Output(f'{COMMODITY_LOWER}-data-status', 'children'),
     Output(f'{COMMODITY_LOWER}-data-status-tooltip', 'children'),
     Output(f'{COMMODITY_LOWER}-calibrate-all-btn', 'disabled'),
     Output(f'{COMMODITY_LOWER}-calibrate-all-btn', 'title'),
     Output(f'{COMMODITY_LOWER}-batch-calibrate-btn', 'disabled'),
     Output(f'{COMMODITY_LOWER}-batch-calibrate-btn', 'title'),
     Output('ttf-trading-context-store', 'data')],
    [Input(f'{COMMODITY_LOWER}-date-picker', 'date'),
     Input(f'{COMMODITY_LOWER}-reload-btn', 'n_clicks')],
    prevent_initial_call=False
)
@cached_workspace_callback(COMMODITY, get_default_date)
def load_data(trade_date, reload_clicks):
    """Load market data and parameters when date changes or reload is clicked."""
    if trade_date is None:
        trade_date = get_default_date()
    else:
        trade_date = pd.to_datetime(trade_date).date()

    try:
        triggered_id = ctx.triggered_id
    except Exception:
        triggered_id = None
    context = load_ttf_trading_context(
        trade_date,
        refresh=triggered_id == f'{COMMODITY_LOWER}-reload-btn',
        market_loader=load_market_data_with_metadata,
    )
    market_data = context['market_data']
    data_source = context['source']
    is_synthetic = False
    last_update = context.get('last_update')
    settlement_cob = context.get('settlement_cob')

    ready, readiness_message = calibration_readiness(
        market_data,
        include_extrapolated=True,
    )
    if market_data.empty:
        badge, tooltip = format_data_status(
            data_source=data_source,
            is_synthetic=is_synthetic,
            last_update=last_update,
            trade_date=trade_date,
            commodity=COMMODITY,
            message=context.get('message'),
            error=context.get('error'),
        )
        empty_market_json = pd.DataFrame(
            columns=[
                'expiry',
                'option_expiration_date',
                'dte',
                'delta',
                'delta_convention',
                'iv',
                'strike',
                'forward',
                'rate',
                'source_name',
                'quote_class',
                'weight',
            ]
        ).to_json(date_format='iso', orient='split')
        empty_params_json = pd.DataFrame(
            columns=[
                'expiry',
                'calibration_basis',
                *PARAM_COLUMNS,
                'left_blend_width',
                'right_blend_width',
                'core_tv_rmse',
                'tail_fit_tv_rmse',
                'iv_rmse',
                'calibration_method',
                'calibration_policy_version',
                'rmse',
            ]
        ).to_json(date_format='iso', orient='split')
        return (
            empty_market_json,
            empty_params_json,
            badge,
            tooltip,
            True,
            readiness_message,
            True,
            readiness_message,
            serialize_ttf_trading_context(context),
        )

    # Try to load historical params from database (T-1)
    historical_params = None
    try:
        engine = get_database_engine()
        if engine is not None:
            historical_params = load_latest_surface_from_db(
                engine,
                COMMODITY,
                pd.Timestamp(settlement_cob).date() if settlement_cob else trade_date,
            )
    except Exception:
        historical_params = None

    # Get default params as fallback
    defaults = get_defaults(COMMODITY)

    # Create params DataFrame with one row per expiry
    expiries = market_data['expiry'].unique()
    params_list = []
    loaded_from_db = False
    incompatible_historical_params = False

    for expiry in sorted(expiries):
        try:
            exp_data = _select_ttf_expiry_inputs(market_data, expiry)
            basis = _calibration_basis(exp_data)
        except ValueError:
            exp_data = pd.DataFrame()
            basis = None
        expiry_date = pd.to_datetime(expiry).date()

        # Check if we have historical params for this expiry
        params_to_use = defaults.copy()
        if historical_params is not None and not historical_params.empty:
            matching = historical_params[historical_params['expiry'] == expiry_date]
            had_historical_match = not matching.empty
            if 'model_version' in matching.columns:
                matching = matching[
                    matching['model_version']
                    == DEFAULT_CALIBRATION_MODEL_VERSION
                ]
            else:
                matching = matching.iloc[0:0]
            if had_historical_match and matching.empty:
                incompatible_historical_params = True
            if not matching.empty:
                loaded_from_db = True
                row = matching.iloc[0]
                for col in PARAM_COLUMNS:
                    if col in row and pd.notna(row[col]):
                        params_to_use[col] = row[col]

        # Historical Wing-only rows do not define an operational hybrid.  They
        # remain useful deterministic starts, but hybrid diagnostics are only
        # populated after an accepted in-session calibration.
        eligibility_error = calibration_eligibility_error(exp_data)
        core_tv_rmse = 0.0 if eligibility_error is None else np.nan

        params_list.append({
            'expiry': expiry,
            'calibration_basis': basis,
            **params_to_use,
            'left_blend_width': np.nan,
            'right_blend_width': np.nan,
            'core_tv_rmse': core_tv_rmse,
            'tail_fit_tv_rmse': np.nan,
            'iv_rmse': np.nan,
            'calibration_method': TTF_HYBRID_METHOD,
            'calibration_policy_version': TTF_HYBRID_POLICY_VERSION,
            # Backward-compatible table/session key.  For TTF this is the core
            # total-variance RMSE, not the legacy node-IV RMSE.
            'rmse': core_tv_rmse,
        })

    params_df = pd.DataFrame(params_list)

    # Format for storage
    market_data_json = market_data.to_json(date_format='iso', orient='split')
    params_json = params_df.to_json(date_format='iso', orient='split')

    # Create status badge and tooltip
    badge, tooltip = format_data_status(
        data_source=data_source,
        is_synthetic=is_synthetic,
        last_update=last_update,
        trade_date=trade_date,
        commodity=COMMODITY,
        message=context.get('message'),
        error=context.get('error'),
    )

    # Add params source to tooltip
    tooltip_parts = [tooltip]
    tooltip_parts.append(
        f"Trading date: {trade_date} | Settlement COB: {settlement_cob or 'unavailable'}"
    )
    if loaded_from_db:
        tooltip_parts.append("Params: Historical (T-1)")
    elif incompatible_historical_params:
        tooltip_parts.append(
            "Params: Wing-v1 history ignored; Wing-v2 defaults loaded"
        )
    else:
        tooltip_parts.append("Params: Defaults")

    tooltip_text = " | ".join(tooltip_parts)
    return (
        market_data_json,
        params_json,
        badge,
        tooltip_text,
        not ready,
        readiness_message,
        not ready,
        readiness_message,
        serialize_ttf_trading_context(context),
    )


def _current_identity():
    headers = request.headers if has_request_context() else {}
    remote_addr = request.remote_addr if has_request_context() else None
    return resolve_request_identity(headers, remote_addr=remote_addr)


def _manual_trade_table_rows(trades):
    rows = []
    for trade in trades or []:
        observed_at = pd.to_datetime(trade.get('observed_at'), errors='coerce')
        contract_date = pd.to_datetime(trade.get('contract_date'), errors='coerce')
        mark_iv = pd.to_numeric(
            pd.Series([trade.get('mark_iv')]), errors='coerce'
        ).iloc[0]
        method = str(trade.get('method') or '')
        rows.append(
            {
                **trade,
                'observed_at': (
                    observed_at.strftime('%H:%M:%S')
                    if not pd.isna(observed_at)
                    else 'Unknown'
                ),
                'contract_label': (
                    contract_date.strftime('%b-%y')
                    if not pd.isna(contract_date)
                    else 'Unknown'
                ),
                'mark_iv_pct': (
                    round(float(mark_iv) * 100.0, 4)
                    if np.isfinite(mark_iv)
                    else None
                ),
                'iv_source': (
                    'Premium inversion'
                    if method == TTF_INTRADAY_PREMIUM_METHOD
                    else trade.get('iv_source', 'Entered IV')
                ),
                'call_delta': round(float(trade['call_delta']), 6),
                'persistence': trade.get('persistence', 'Session'),
            }
        )
    return rows


@callback(
    Output('ttf-trading-context-status', 'children'),
    Input('ttf-trading-context-store', 'data'),
    prevent_initial_call=False,
)
def render_ttf_trading_context(context):
    context = context or {}
    trading_date = context.get('trading_date') or 'unknown'
    settlement_cob = context.get('settlement_cob') or 'unavailable'
    if context.get('error'):
        return dbc.Alert(
            f"Trading date {trading_date} · settlement unavailable · "
            f"{context['error']}",
            color='danger',
            className='py-2 px-3 mb-2 small',
        )
    color = 'warning' if context.get('date_fallback_used') else 'success'
    fallback = (
        ' · prior official COB fallback'
        if context.get('date_fallback_used')
        else ' · exact official COB'
    )
    return dbc.Alert(
        f"Trading date {trading_date} · settlement nodes {settlement_cob}"
        f"{fallback} · {int(context.get('market_row_count') or 0)} rows",
        color=color,
        className='py-2 px-3 mb-2 small',
    )


@callback(
    [
        Output('ttf-published-surface-store', 'data'),
        Output('ttf-publication-status', 'children'),
    ],
    [
        Input('ttf-date-picker', 'date'),
        Input('ttf-reload-btn', 'n_clicks'),
    ],
    prevent_initial_call=False,
)
def load_ttf_publication(trading_date, reload_clicks):
    del reload_clicks
    selected = pd.to_datetime(trading_date or get_default_date()).date()
    try:
        engine = get_database_engine()
        payload = load_latest_ttf_publication(
            engine,
            selected,
            prefer_exact_cob=True,
        )
    except Exception as exc:
        payload = {
            'publication_id': None,
            'trading_date': selected.isoformat(),
            'row_count': 0,
            'expiry_count': 0,
            'data': pd.DataFrame().to_json(orient='split'),
            'error': str(exc),
        }
    if payload.get('publication_id'):
        status = dbc.Alert(
            f"Working calibrated base {payload.get('publication_date')} · "
            f"published {payload.get('published_at')} · "
            f"{payload.get('expiry_count')} expiries · immutable revision "
            f"{payload.get('publication_id')}",
            color='success',
            className='py-2 px-3 mb-3 small',
        )
    else:
        detail = payload.get('error') or 'No prior TTF publication exists.'
        status = dbc.Alert(
            f"Latest published smile unavailable · {detail}",
            color='warning',
            className='py-2 px-3 mb-3 small',
        )
    return payload, status


@callback(
    [
        Output('ttf-workspace-expiry', 'options'),
        Output('ttf-workspace-expiry', 'value'),
        Output('ttf-intraday-expiry', 'options'),
        Output('ttf-intraday-expiry', 'value'),
    ],
    [
        Input('ttf-param-table', 'data'),
        Input('vol-calibration-requested-expiry', 'data'),
    ],
    [
        State('ttf-workspace-expiry', 'value'),
        State('ttf-intraday-expiry', 'value'),
    ],
    prevent_initial_call=True,
)
def populate_ttf_expiry_controls(
    table_data,
    requested_expiry,
    workspace_value,
    trade_value,
):
    rows = table_data or []
    options = [
        {
            'label': (
                f"{row.get('expiry')} · "
                f"{str(row.get('calibration_basis', 'Unknown')).title()}"
            ),
            'value': row.get('expiry'),
        }
        for row in rows
        if row.get('expiry') is not None
    ]
    values = [item['value'] for item in options]
    default = values[0] if values else None
    requested_match = next(
        (
            value
            for value in values
            if requested_expiry is not None
            and expiry_month(value) == expiry_month(requested_expiry)
        ),
        None,
    )
    workspace = (
        workspace_value
        if workspace_value in values
        else requested_match or default
    )
    trade = trade_value if trade_value in values else workspace
    return options, workspace, options, trade


@callback(
    Output('ttf-param-table', 'selected_rows'),
    Input('ttf-workspace-expiry', 'value'),
    State('ttf-param-table', 'data'),
    prevent_initial_call=True,
)
def sync_ttf_workspace_selection(expiry, table_data):
    if expiry is None:
        return []
    target = expiry_month(expiry)
    for index, row in enumerate(table_data or []):
        try:
            if expiry_month(row.get('expiry')) == target:
                return [index]
        except ValueError:
            continue
    return []


@callback(
    Output('ttf-intraday-forward', 'value'),
    [
        Input('ttf-intraday-expiry', 'value'),
        Input('ttf-market-data-store', 'data'),
    ],
    prevent_initial_call=True,
)
def prefill_ttf_working_forward(expiry, market_data_json):
    if not expiry or not market_data_json:
        return None
    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    try:
        observations = _select_ttf_expiry_inputs(market_data, expiry)
    except ValueError:
        return None
    return round(float(observations['forward'].iloc[0]), 4)


@callback(
    [
        Output('ttf-intraday-trades-store', 'data'),
        Output('ttf-intraday-trade-table', 'data'),
        Output('ttf-intraday-entry-status', 'children'),
    ],
    [
        Input('ttf-date-picker', 'date'),
        Input('ttf-reload-btn', 'n_clicks'),
        Input('ttf-intraday-add-btn', 'n_clicks'),
    ],
    [
        State('ttf-intraday-trades-store', 'data'),
        State('ttf-intraday-expiry', 'value'),
        State('ttf-intraday-put-call', 'value'),
        State('ttf-intraday-strike', 'value'),
        State('ttf-intraday-iv', 'value'),
        State('ttf-intraday-premium', 'value'),
        State('ttf-intraday-volume', 'value'),
        State('ttf-intraday-forward', 'value'),
        State('ttf-intraday-notes', 'value'),
        State('ttf-market-data-store', 'data'),
    ],
    prevent_initial_call=False,
)
def manage_ttf_intraday_trades(
    trading_date,
    reload_clicks,
    add_clicks,
    store,
    expiry,
    put_call,
    strike,
    mark_iv,
    premium,
    volume,
    forward,
    notes,
    market_data_json,
):
    del reload_clicks, add_clicks
    selected_date = pd.to_datetime(trading_date or get_default_date()).date()
    triggered = ctx.triggered_id
    if triggered != 'ttf-intraday-add-btn':
        trades = []
        persistence = 'Session'
        try:
            engine = get_database_engine()
            trades = load_ttf_intraday_trades(engine, selected_date)
            for trade in trades:
                trade['persistence'] = 'Database'
            if trades:
                persistence = 'Database'
        except Exception:
            trades = []
        payload = {
            'business_date': selected_date.isoformat(),
            'trades': trades,
            'persistence': persistence,
        }
        return payload, _manual_trade_table_rows(trades), ''

    if not market_data_json or not expiry:
        return no_update, no_update, dbc.Alert(
            'Select an eligible contract month first.',
            color='danger',
            className='py-1 px-2 mb-0',
        )
    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    try:
        observations = _select_ttf_expiry_inputs(market_data, expiry)
        option_expiration_date = observations['option_expiration_date'].iloc[0]
        identity = _current_identity()
        entered_by = identity.subject or 'session-trader'
        trade = normalize_ttf_intraday_trade(
            {
                'business_date': selected_date,
                'contract_date': observations['expiry'].iloc[0],
                'option_expiration_date': option_expiration_date,
                'put_call': put_call,
                'strike': strike,
                'mark_iv': mark_iv,
                'mark_price': premium,
                'volume': volume,
                'forward': forward,
                'notes': notes,
            },
            entered_by=entered_by,
        )
        trade['persistence'] = 'Session'
        if ttf_intraday_writes_enabled() and identity.authenticated:
            trade = persist_ttf_intraday_trade(
                get_database_engine(),
                trade,
                identity,
            )
            trade['persistence'] = 'Database'
    except Exception as exc:
        return no_update, no_update, dbc.Alert(
            str(exc), color='danger', className='py-1 px-2 mb-0'
        )

    current = store or {}
    existing = (
        list(current.get('trades') or [])
        if current.get('business_date') == selected_date.isoformat()
        else []
    )
    existing.append(trade)
    payload = {
        'business_date': selected_date.isoformat(),
        'trades': existing,
        'persistence': trade['persistence'],
    }
    iv_message = (
        f"IV {trade['mark_iv'] * 100:.2f}% derived from premium {trade['mark_price']:.4g}"
        if trade['method'] == TTF_INTRADAY_PREMIUM_METHOD
        else f"IV {trade['mark_iv'] * 100:.2f}% entered"
    )
    status = dbc.Alert(
        f"Trade added · call delta {trade['call_delta']:.4f} · "
        f"{iv_message} · {trade['persistence']}",
        color='success',
        className='py-1 px-2 mb-0',
    )
    return payload, _manual_trade_table_rows(existing), status


@callback(
    Output(f'{COMMODITY_LOWER}-param-table', 'data'),
    [
        Input(f'{COMMODITY_LOWER}-params-store', 'data'),
        Input('ttf-published-surface-store', 'data'),
    ],
    State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
    prevent_initial_call=True
)
def update_param_table(params_json, publication_payload, market_data_json):
    """Build editable rows from governed defaults plus the latest publication."""
    if params_json is None:
        return []

    params_df = pd.read_json(StringIO(params_json), orient='split')

    # Parse market data for arbitrage check (forward prices)
    market_data = None
    if market_data_json is not None:
        try:
            market_data = pd.read_json(StringIO(market_data_json), orient='split')
        except Exception:
            pass

    rows = format_params_for_table(params_df, market_data, commodity=COMMODITY)
    return _apply_published_parameters(rows, market_data, publication_payload)


@callback(
    [
        Output('ttf-final-nodes-store', 'data', allow_duplicate=True),
        Output('ttf-adjustment-store', 'data', allow_duplicate=True),
        Output('ttf-adjust-level', 'value', allow_duplicate=True),
        Output('ttf-adjust-skew', 'value', allow_duplicate=True),
        Output('ttf-adjust-put-curvature', 'value', allow_duplicate=True),
        Output('ttf-adjust-call-curvature', 'value', allow_duplicate=True),
        Output('ttf-use-selected-trade', 'value'),
        Output('ttf-node-unlock', 'value'),
        Output('ttf-batch-results-store', 'data', allow_duplicate=True),
    ],
    [
        Input('ttf-date-picker', 'date'),
        Input('ttf-reload-btn', 'n_clicks'),
    ],
    prevent_initial_call=True,
)
def clear_ttf_unsaved_state_on_date_change(trading_date, reload_clicks):
    """Do not let an unsaved calibration leak across a date or reload."""
    del trading_date, reload_clicks
    return {}, {}, 0.0, 0.0, 0.0, 0.0, [], [], {}


@callback(
    [
        Output(f'{COMMODITY_LOWER}-node-editor', 'data'),
        Output(f'{COMMODITY_LOWER}-node-editor-expiry', 'children'),
    ],
    [
        Input(f'{COMMODITY_LOWER}-market-data-store', 'data'),
        Input(f'{COMMODITY_LOWER}-param-table', 'selected_rows'),
        Input(f'{COMMODITY_LOWER}-param-table', 'data'),
        Input(f'{COMMODITY_LOWER}-final-nodes-store', 'data'),
        Input('ttf-published-surface-store', 'data'),
        Input('ttf-workspace-expiry', 'value'),
    ],
    prevent_initial_call=True,
)
def populate_ttf_node_editor(
    market_data_json,
    selected_rows,
    table_data,
    node_store,
    publication_payload,
    workspace_expiry,
):
    if not market_data_json or not table_data:
        return [], "No eligible expiry selected"
    row_index = selected_rows[0] if selected_rows else 0
    if row_index >= len(table_data):
        return [], "No eligible expiry selected"
    expiry = workspace_expiry or table_data[row_index].get('expiry')
    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    try:
        observations = _select_ttf_expiry_inputs(market_data, expiry)
    except ValueError as exc:
        return [], str(exc)
    rows = _ttf_node_editor_rows(
        market_data,
        expiry,
        node_store,
        publication_payload,
    )
    basis = _calibration_basis(observations).title()
    return rows, f"{expiry} · {basis}"


@callback(
    Output('ttf-node-editor', 'columns'),
    Input('ttf-node-unlock', 'value'),
    State('ttf-node-editor', 'columns'),
    prevent_initial_call=True,
)
def toggle_ttf_node_override(unlock, columns):
    updated = [dict(column) for column in (columns or [])]
    for column in updated:
        column['editable'] = bool(
            column.get('id') == 'final_iv_pct' and 'unlock' in (unlock or [])
        )
    return updated


@callback(
    Output('ttf-tail-editor', 'data'),
    [
        Input('ttf-workspace-expiry', 'value'),
        Input('ttf-param-table', 'data'),
    ],
    prevent_initial_call=True,
)
def populate_ttf_tail_editor(expiry, table_data):
    if expiry is None:
        return []
    target = expiry_month(expiry)
    for row in table_data or []:
        try:
            if expiry_month(row.get('expiry')) != target:
                continue
        except ValueError:
            continue
        return [
            {
                name: row.get(name)
                for name in (
                    'dc',
                    'uc',
                    'put_wing_power',
                    'call_wing_power',
                    'left_blend_width',
                    'right_blend_width',
                )
            }
        ]
    return []


@callback(
    Output('ttf-adjustment-basis', 'children'),
    [
        Input('ttf-workspace-expiry', 'value'),
        Input('ttf-market-data-store', 'data'),
        Input('ttf-published-surface-store', 'data'),
    ],
    prevent_initial_call=True,
)
def render_ttf_adjustment_basis(expiry, market_data_json, publication_payload):
    if not expiry or not market_data_json:
        return 'No expiry selected'
    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    try:
        observations = _select_ttf_expiry_inputs(market_data, expiry)
        basis = _calibration_basis(observations).title()
        published = _published_node_values(publication_payload, observations)
    except ValueError as exc:
        return str(exc)
    base = 'latest published smile' if published is not None else 'settlement nodes'
    return f"Basis: {basis} · adjustment base: {base}"


def _find_table_row(table_data, expiry):
    target = expiry_month(expiry)
    for index, row in enumerate(table_data or []):
        try:
            if expiry_month(row.get('expiry')) == target:
                return index
        except ValueError:
            continue
    raise ValueError(f"No editable TTF row exists for {expiry}.")


@callback(
    [
        Output('ttf-final-nodes-store', 'data', allow_duplicate=True),
        Output('ttf-param-table', 'data', allow_duplicate=True),
        Output('ttf-adjustment-status', 'children'),
        Output('ttf-adjustment-store', 'data'),
        Output('ttf-adjust-level', 'value'),
        Output('ttf-adjust-skew', 'value'),
        Output('ttf-adjust-put-curvature', 'value'),
        Output('ttf-adjust-call-curvature', 'value'),
    ],
    [
        Input('ttf-build-candidate-btn', 'n_clicks'),
        Input('ttf-reset-adjustment-btn', 'n_clicks'),
    ],
    [
        State('ttf-workspace-expiry', 'value'),
        State('ttf-adjust-level', 'value'),
        State('ttf-adjust-skew', 'value'),
        State('ttf-adjust-put-curvature', 'value'),
        State('ttf-adjust-call-curvature', 'value'),
        State('ttf-use-selected-trade', 'value'),
        State('ttf-intraday-trade-table', 'selected_rows'),
        State('ttf-intraday-trades-store', 'data'),
        State('ttf-market-data-store', 'data'),
        State('ttf-published-surface-store', 'data'),
        State('ttf-param-table', 'data'),
        State('ttf-params-store', 'data'),
        State('ttf-final-nodes-store', 'data'),
        State('ttf-adjustment-store', 'data'),
        State('ttf-tail-editor', 'data'),
    ],
    prevent_initial_call=True,
)
def build_ttf_intraday_candidate(
    build_clicks,
    reset_clicks,
    expiry,
    level,
    skew,
    put_curvature,
    call_curvature,
    use_selected_trade,
    selected_trade_rows,
    trade_store,
    market_data_json,
    publication_payload,
    table_data,
    original_params_json,
    node_store,
    adjustment_store,
    tail_rows,
):
    del build_clicks, reset_clicks
    if not expiry or not market_data_json or not table_data:
        raise PreventUpdate
    row_index = _find_table_row(table_data, expiry)
    key = _expiry_store_key(expiry)

    if ctx.triggered_id == 'ttf-reset-adjustment-btn':
        updated_nodes = dict(node_store or {})
        updated_nodes.pop(key, None)
        updated_adjustments = dict(adjustment_store or {})
        updated_adjustments.pop(key, None)
        restored_table = [dict(row) for row in table_data]
        if original_params_json:
            original = pd.read_json(StringIO(original_params_json), orient='split')
            market_data = pd.read_json(StringIO(market_data_json), orient='split')
            original_records = format_params_for_table(
                original,
                market_data,
                commodity=COMMODITY,
            )
            original_records = _apply_published_parameters(
                original_records,
                market_data,
                publication_payload,
            )
            original_index = _find_table_row(original_records, expiry)
            restored_table[row_index] = dict(original_records[original_index])
        return (
            updated_nodes,
            restored_table,
            dbc.Alert(
                'Selected smile reset to its published/settlement base.',
                color='secondary',
                className='py-1 px-2 mb-0',
            ),
            updated_adjustments,
            0.0,
            0.0,
            0.0,
            0.0,
        )

    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    try:
        base_observations = _base_ttf_observations(
            market_data,
            expiry,
            publication_payload,
        )
        selected_trade = None
        if 'target' in (use_selected_trade or []):
            trade_index = selected_trade_rows[0] if selected_trade_rows else None
            trades = list((trade_store or {}).get('trades') or [])
            if trade_index is None or trade_index >= len(trades):
                raise ValueError('Select one intraday trade to use as a target.')
            selected_trade = trades[trade_index]
            if expiry_month(selected_trade.get('contract_date')) != expiry_month(expiry):
                raise ValueError('The selected trade belongs to a different expiry.')
            base_observations['forward'] = float(selected_trade['forward'])
            base_observations['strike'] = [
                delta_node_to_strike(
                    float(selected_trade['forward']),
                    float(base_observations['dte'].iloc[0]) / 365.25,
                    float(delta),
                    float(iv),
                )
                for delta, iv in zip(
                    base_observations['delta'], base_observations['iv']
                )
            ]

        adjusted, diagnostics = apply_ttf_smile_adjustments(
            base_observations,
            {
                'level': level,
                'skew': skew,
                'put_curvature': put_curvature,
                'call_curvature': call_curvature,
            },
            selected_trade=selected_trade,
        )
        # Recompute delta-node strikes after the final node vols so the current
        # working-forward/delta convention remains internally consistent.
        working_forward = float(adjusted['forward'].iloc[0])
        working_dte = float(adjusted['dte'].iloc[0])
        adjusted['strike'] = [
            delta_node_to_strike(
                working_forward,
                working_dte / 365.25,
                float(delta),
                float(iv),
            )
            for delta, iv in zip(adjusted['delta'], adjusted['iv'])
        ]
        basis = _calibration_basis(adjusted)
        initial = table_data[row_index]
        fitted = _run_ttf_candidate(
            adjusted,
            initial,
            basis=basis,
            selected_expiry=True,
        )
        final_params = _candidate_params(fitted)
        tail_row = (tail_rows or [{}])[0]
        final_params.update(_changed_ttf_tail_overrides(tail_row, initial))
        final_result = _evaluate_existing_hybrid(adjusted, final_params)
    except Exception as exc:
        return (
            no_update,
            no_update,
            _calibration_blocked_status(str(exc)),
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
        )

    node_values = {
        f"{float(delta):.10f}": float(iv)
        for delta, iv in zip(adjusted['delta'], adjusted['iv'])
    }
    strike_values = {
        f"{float(delta):.10f}": float(strike)
        for delta, strike in zip(adjusted['delta'], adjusted['strike'])
    }
    updated_nodes = dict(node_store or {})
    updated_nodes[key] = {
        'nodes': node_values,
        'strikes': strike_values,
        'forward': working_forward,
        'dte': working_dte,
        'option_expiration_date': pd.Timestamp(
            adjusted['option_expiration_date'].iloc[0]
        ).date().isoformat(),
        'validation': final_result['validation'],
        'diagnostics': diagnostics,
    }

    updated_table = [dict(row) for row in table_data]
    target_row = updated_table[row_index]
    for name, value in _candidate_params(final_result).items():
        target_row[name] = float(value)
    target_row['core_tv_rmse'] = 0.0
    target_row['tail_fit_tv_rmse'] = float(final_result['tail_fit_tv_rmse'])
    target_row['iv_rmse'] = float(final_result['iv_rmse'])
    target_row['rmse'] = _format_tv_rmse(0.0)
    target_row['arb_status'] = 'Pass'
    target_row['calibration_basis'] = basis.title()
    target_row['calibration_method'] = TTF_HYBRID_METHOD
    target_row['calibration_policy_version'] = TTF_HYBRID_POLICY_VERSION

    identity = _current_identity()
    updated_adjustments = dict(adjustment_store or {})
    updated_adjustments[key] = {
        **diagnostics,
        'created_by': identity.subject or 'session-trader',
        'expiry': str(expiry),
        'working_forward': working_forward,
        'dte': working_dte,
        'validation': final_result['validation'],
        'tail_fit_tv_rmse': float(final_result['tail_fit_tv_rmse']),
        'iv_rmse': float(final_result['iv_rmse']),
    }
    trade_copy = ''
    if diagnostics.get('selected_trade'):
        trade_copy = (
            f" · matched trade IV {diagnostics['selected_trade']['matched_iv'] * 100:.2f}%"
        )
    status = dbc.Alert(
        f"Candidate valid · Core TV RMSE 0.000000 · min g "
        f"{final_result['validation']['min_g']:.6f}{trade_copy}",
        color='success',
        className='py-1 px-2 mb-0',
    )
    return (
        updated_nodes,
        updated_table,
        status,
        updated_adjustments,
        no_update,
        no_update,
        no_update,
        no_update,
    )


@callback(
    [
        Output(f'{COMMODITY_LOWER}-final-nodes-store', 'data'),
        Output(f'{COMMODITY_LOWER}-node-editor-status', 'children'),
        Output(f'{COMMODITY_LOWER}-param-table', 'data', allow_duplicate=True),
        Output(f'{COMMODITY_LOWER}-node-editor', 'data', allow_duplicate=True),
    ],
    Input(f'{COMMODITY_LOWER}-node-editor', 'data_timestamp'),
    [
        State(f'{COMMODITY_LOWER}-node-editor', 'data'),
        State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
        State(f'{COMMODITY_LOWER}-param-table', 'data'),
        State(f'{COMMODITY_LOWER}-param-table', 'selected_rows'),
        State(f'{COMMODITY_LOWER}-final-nodes-store', 'data'),
    ],
    prevent_initial_call=True,
)
def validate_ttf_node_edits(
    edit_timestamp,
    node_rows,
    market_data_json,
    table_data,
    selected_rows,
    node_store,
):
    if edit_timestamp is None or not node_rows or not market_data_json or not table_data:
        raise PreventUpdate
    row_index = selected_rows[0] if selected_rows else 0
    if row_index >= len(table_data):
        raise PreventUpdate
    expiry = table_data[row_index].get('expiry')
    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    restored_rows = _ttf_node_editor_rows(market_data, expiry, node_store)
    values = {}
    for row in node_rows:
        delta = pd.to_numeric(pd.Series([row.get('delta')]), errors='coerce').iloc[0]
        iv_pct = pd.to_numeric(
            pd.Series([row.get('final_iv_pct')]), errors='coerce'
        ).iloc[0]
        if not np.isfinite(delta) or not np.isfinite(iv_pct) or iv_pct <= 0:
            return (
                no_update,
                _calibration_blocked_status(
                    "All 11 node vols must be finite and strictly positive."
                ),
                no_update,
                restored_rows,
            )
        values[f"{float(delta):.10f}"] = float(iv_pct) / 100.0

    candidate_store = dict(node_store or {})
    store_key = _expiry_store_key(expiry)
    existing_entry = candidate_store.get(store_key, {})
    if isinstance(existing_entry, dict) and 'nodes' in existing_entry:
        candidate_store[store_key] = {**existing_entry, 'nodes': values}
    else:
        candidate_store[store_key] = values
    edited_market = _apply_node_edits(market_data, candidate_store, expiry)
    try:
        observations = _select_ttf_expiry_inputs(edited_market, expiry)
        basis = _calibration_basis(observations)
        result = _run_ttf_candidate(
            observations,
            table_data[row_index],
            basis=basis,
            selected_expiry=True,
        )
    except Exception as exc:
        return (
            no_update,
            _calibration_blocked_status(str(exc)),
            no_update,
            restored_rows,
        )

    updated_table_data = [dict(row) for row in table_data]
    session_row = updated_table_data[row_index]
    for param_key, param_val in _candidate_params(result).items():
        session_row[param_key] = float(param_val)
    session_row['core_tv_rmse'] = float(result['core_tv_rmse'])
    session_row['tail_fit_tv_rmse'] = float(result['tail_fit_tv_rmse'])
    session_row['iv_rmse'] = float(result['iv_rmse'])
    session_row['rmse'] = _format_tv_rmse(result['core_tv_rmse'])
    session_row['arb_status'] = 'Pass'
    session_row['calibration_basis'] = basis.title()
    session_row['calibration_method'] = TTF_HYBRID_METHOD
    session_row['calibration_policy_version'] = TTF_HYBRID_POLICY_VERSION

    status = dbc.Alert(
        (
            "Valid session Final · Core TV RMSE 0.000000 · Wing tail TV RMSE "
            f"{result['tail_fit_tv_rmse']:.6f} · min g "
            f"{result['validation']['min_g']:.6f}"
        ),
        color="success",
        className="mb-0 py-1 px-2",
    )
    accepted_rows = _ttf_node_editor_rows(market_data, expiry, candidate_store)
    return candidate_store, status, updated_table_data, accepted_rows


@callback(
    Output(f'{COMMODITY_LOWER}-comparison-basis', 'children'),
    Input(f'{COMMODITY_LOWER}-comparison-data-store', 'data'),
    prevent_initial_call=True,
)
def render_comparison_basis(comparison_data):
    """Expose immutable TTF input provenance in the comparison modal."""
    basis = str((comparison_data or {}).get('calibration_basis', '')).strip()
    return basis.title() if basis else "Unavailable"


@callback(
    [
        Output(f'{COMMODITY_LOWER}-current-tail-tv-rmse', 'children'),
        Output(f'{COMMODITY_LOWER}-candidate-tail-tv-rmse', 'children'),
        Output(f'{COMMODITY_LOWER}-final-tail-tv-rmse', 'children'),
        Output(f'{COMMODITY_LOWER}-current-iv-rmse', 'children'),
        Output(f'{COMMODITY_LOWER}-candidate-iv-rmse', 'children'),
        Output(f'{COMMODITY_LOWER}-final-iv-rmse', 'children'),
    ],
    Input(f'{COMMODITY_LOWER}-comparison-data-store', 'data'),
    prevent_initial_call=True,
)
def render_hybrid_comparison_metrics(comparison_data):
    """Render native-TV and secondary IV diagnostics without unit mixing."""
    data = comparison_data or {}

    def tv_value(name):
        return _format_tv_rmse(data.get(name))

    def iv_value(name):
        value = pd.to_numeric(
            pd.Series([data.get(name)]), errors='coerce'
        ).iloc[0]
        return f"{float(value) * 100:.2f}%" if np.isfinite(value) else "Unavailable"

    return (
        tv_value('current_tail_fit_tv_rmse'),
        tv_value('candidate_tail_fit_tv_rmse'),
        tv_value('final_tail_fit_tv_rmse'),
        iv_value('current_iv_rmse'),
        iv_value('candidate_iv_rmse'),
        iv_value('final_iv_rmse'),
    )


@callback(
    Output(f'{COMMODITY_LOWER}-smile-grid', 'figure'),
    [Input(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     Input(f'{COMMODITY_LOWER}-param-table', 'data'),
     Input(f'{COMMODITY_LOWER}-x-axis-selector', 'value'),
     Input(f'{COMMODITY_LOWER}-param-table', 'selected_rows'),
     Input(f'{COMMODITY_LOWER}-operational-surface-store', 'data'),
     Input(f'{COMMODITY_LOWER}-traded-options-store', 'data'),
     Input(f'{COMMODITY_LOWER}-final-nodes-store', 'data'),
     Input('ttf-published-surface-store', 'data'),
     Input('ttf-intraday-trades-store', 'data'),
     Input('ttf-trading-context-store', 'data')],
    prevent_initial_call=True
)
def update_smile_grid(
    market_data_json,
    table_data,
    x_axis,
    selected_rows,
    operational_payload,
    traded_options_payload,
    node_store,
    publication_payload,
    manual_trade_payload,
    trading_context,
):
    """Update smile grid when data or x-axis changes."""
    if market_data_json is None and operational_payload is None:
        raise PreventUpdate

    market_data = (
        pd.read_json(StringIO(market_data_json), orient='split')
        if market_data_json
        else pd.DataFrame()
    )
    market_data = _apply_node_edits(market_data, node_store)
    params_df = parse_table_data(table_data or [])
    selected_row = selected_rows[0] if selected_rows else None
    selected_axis = x_axis or 'delta'
    return create_smile_grid_figure(
        market_data=market_data,
        params_df=params_df,
        x_axis=selected_axis,
        selected_row=selected_row,
        model_version=DEFAULT_CALIBRATION_MODEL_VERSION,
        operational_surface=operational_surface_frame(operational_payload),
        operational_metadata=operational_payload,
        traded_options=ttf_traded_options_frame(
            traded_options_payload,
            market_data,
        ),
        traded_options_metadata=traded_options_payload,
        published_surface=_published_surface_for_market(
            publication_payload,
            market_data,
        ),
        published_metadata=publication_payload,
        manual_trades=pd.DataFrame(
            (manual_trade_payload or {}).get('trades') or []
        ),
        manual_trades_metadata=manual_trade_payload,
        market_metadata=trading_context,
    )


@callback(
    [Output(f'{COMMODITY_LOWER}-comparison-modal', 'is_open'),
     Output(f'{COMMODITY_LOWER}-comparison-data-store', 'data'),
     Output(f'{COMMODITY_LOWER}-comparison-expiry', 'children'),
     Output(f'{COMMODITY_LOWER}-comparison-forward', 'children'),
     Output(f'{COMMODITY_LOWER}-comparison-table', 'data'),
     Output(f'{COMMODITY_LOWER}-comparison-plot', 'figure'),
     Output(f'{COMMODITY_LOWER}-current-rmse', 'children'),
     Output(f'{COMMODITY_LOWER}-candidate-rmse', 'children'),
     Output(f'{COMMODITY_LOWER}-final-rmse', 'children'),
     Output(f'{COMMODITY_LOWER}-data-status', 'children', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-param-table', 'data', allow_duplicate=True)],
    [Input(f'{COMMODITY_LOWER}-calibrate-all-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-comparison-cancel-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-comparison-save-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-copy-candidate-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-reset-final-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-comparison-table', 'data')],
    [State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     State(f'{COMMODITY_LOWER}-param-table', 'data'),
     State(f'{COMMODITY_LOWER}-param-table', 'selected_rows'),
     State(f'{COMMODITY_LOWER}-comparison-modal', 'is_open'),
     State(f'{COMMODITY_LOWER}-comparison-data-store', 'data'),
     State(f'{COMMODITY_LOWER}-x-axis-selector', 'value'),
     State(f'{COMMODITY_LOWER}-date-picker', 'date'),
     State(f'{COMMODITY_LOWER}-data-status', 'children'),
     State(f'{COMMODITY_LOWER}-final-nodes-store', 'data')],
    prevent_initial_call=True
)
def handle_calibration(
    calibrate_clicks, cancel_clicks, save_clicks, copy_clicks, reset_clicks,
    comparison_table_data,
    market_data_json, table_data, selected_rows, is_open, comparison_store, x_axis,
    trade_date_str, current_status_badge, node_store=None
):
    """Handle calibration workflow and comparison modal."""
    triggered_id = ctx.triggered_id

    # Default outputs (11 values now including status badge and param table)
    empty_fig = {}
    default_outputs = (False, None, "", "", [], empty_fig, "", "", "", no_update, no_update)

    if triggered_id == f'{COMMODITY_LOWER}-comparison-cancel-btn':
        return default_outputs

    if triggered_id == f'{COMMODITY_LOWER}-comparison-save-btn':
        # A PCHIP-core/Wing-tail hybrid is a new, session-only provenance.  It
        # must never enter the Wing-only ParameterStore even if the global
        # migration write flag is enabled later.
        raise PreventUpdate

    if market_data_json is None or table_data is None:
        raise PreventUpdate

    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    market_data = _apply_node_edits(market_data, node_store)
    params_df = parse_table_data(table_data)

    if triggered_id == f'{COMMODITY_LOWER}-calibrate-all-btn':
        row_idx = selected_rows[0] if selected_rows else 0

        if row_idx >= len(params_df):
            raise PreventUpdate

        expiry = params_df.iloc[row_idx]['expiry']
        try:
            exp_data = _select_ttf_expiry_inputs(market_data, expiry)
            basis = _calibration_basis(exp_data)
            eligibility_error = calibration_eligibility_error(exp_data)
        except Exception as exc:
            eligibility_error = str(exc)
            exp_data = pd.DataFrame()
            basis = None
        if eligibility_error:
            return (
                False,
                None,
                "",
                "",
                [],
                empty_fig,
                "",
                "",
                "",
                _calibration_blocked_status(eligibility_error),
                no_update,
            )

        forward = float(exp_data['forward'].iloc[0])

        # Get current params
        current_params = _model_params(params_df.iloc[row_idx].to_dict())

        # Run calibration
        try:
            result = _run_ttf_candidate(
                exp_data,
                current_params,
                basis=basis,
                selected_expiry=True,
            )
            candidate_params = _candidate_params(result)
            candidate_rmse = float(result['core_tv_rmse'])
        except Exception as exc:
            return (
                False,
                None,
                "",
                "",
                [],
                empty_fig,
                "",
                "",
                "",
                _calibration_blocked_status(str(exc)),
                no_update,
            )

        # The PCHIP core reproduces its nodes exactly.  An older Wing-only row
        # may not yet have an accepted hybrid join, so keep that state explicit
        # instead of manufacturing a legacy IV RMSE.
        try:
            current_values = params_df.iloc[row_idx].to_dict()
            current_result = _evaluate_existing_hybrid(exp_data, current_values)
            current_params = {
                **current_params,
                'left_blend_width': float(current_result['left_blend_width']),
                'right_blend_width': float(current_result['right_blend_width']),
            }
            current_rmse = float(current_result['core_tv_rmse'])
        except Exception:
            current_result = None
            current_rmse = np.nan

        # Store comparison data
        comparison_data = {
            'expiry': str(expiry),
            'forward': forward,
            'current_params': current_params,
            'candidate_params': candidate_params,
            'final_params': current_params.copy(),
            'current_rmse': current_rmse,
            'candidate_rmse': candidate_rmse,
            'current_tail_fit_tv_rmse': (
                float(current_result['tail_fit_tv_rmse'])
                if current_result is not None
                else None
            ),
            'current_iv_rmse': (
                float(current_result['iv_rmse'])
                if current_result is not None
                else None
            ),
            'candidate_tail_fit_tv_rmse': float(result['tail_fit_tv_rmse']),
            'candidate_iv_rmse': float(result['iv_rmse']),
            'final_tail_fit_tv_rmse': (
                float(current_result['tail_fit_tv_rmse'])
                if current_result is not None
                else None
            ),
            'final_iv_rmse': (
                float(current_result['iv_rmse'])
                if current_result is not None
                else None
            ),
            'candidate_validation': result['validation'],
            'calibration_basis': basis,
            'source_name': str(exp_data['source_name'].iloc[0]),
            'calibration_method': TTF_HYBRID_METHOD,
            'calibration_policy_version': TTF_HYBRID_POLICY_VERSION,
            'calibration_diagnostics': {
                'butterfly': result.get('butterfly'),
                'solver_converged': result.get('solver_converged'),
                'accepted_feasible_candidate': result.get(
                    'accepted_feasible_candidate'
                ),
            },
            'row_idx': row_idx,
        }

        # Create comparison table data
        comparison_table = format_comparison_data(
            current_params,
            candidate_params,
            current_params,
            comparison_params=TTF_ADVANCED_PARAMS,
            param_labels={
                'left_blend_width': 'Left blend width',
                'right_blend_width': 'Right blend width',
            },
        )

        # Create comparison plot
        fig = create_comparison_plot(
            exp_data, current_params, candidate_params, current_params,
            expiry_label=expiry,
            x_axis=x_axis or 'log_moneyness',
            model_version=DEFAULT_CALIBRATION_MODEL_VERSION,
        )

        return (
            True,  # Open modal
            comparison_data,
            f"Expiry: {expiry}",
            f"€{forward:.2f}/MWh",
            comparison_table,
            fig,
            _format_tv_rmse(current_rmse),
            _format_tv_rmse(candidate_rmse),
            _format_tv_rmse(current_rmse),
            no_update,
            no_update,
        )

    # Handle copy/reset buttons and table edits
    if comparison_store is None:
        raise PreventUpdate

    current_params = comparison_store.get('current_params', {})
    candidate_params = comparison_store.get('candidate_params', {})
    expiry = comparison_store.get('expiry', '')
    forward = comparison_store.get('forward', 45.0)
    row_idx = comparison_store.get('row_idx', 0)

    if triggered_id == f'{COMMODITY_LOWER}-copy-candidate-btn':
        final_params = candidate_params.copy()
    elif triggered_id == f'{COMMODITY_LOWER}-reset-final-btn':
        final_params = current_params.copy()
    else:
        # Extract final params from table edits
        final_params = extract_final_params(comparison_table_data)

    # Get market data for this expiry
    exp_data = _select_ttf_expiry_inputs(market_data, expiry)

    # Rebuild and validate the complete hybrid after every Advanced tail edit.
    try:
        final_result = _evaluate_existing_hybrid(exp_data, final_params)
        final_rmse = float(final_result['core_tv_rmse'])
    except Exception as exc:
        return (
            True,
            comparison_store,
            f"Expiry: {expiry}",
            f"€{forward:.2f}/MWh",
            comparison_table_data,
            empty_fig,
            "",
            "",
            "",
            _calibration_blocked_status(str(exc)),
            no_update,
        )

    # Update comparison data
    comparison_data = comparison_store.copy()
    comparison_data['final_params'] = final_params
    comparison_data['final_rmse'] = float(final_rmse)
    comparison_data['final_tail_fit_tv_rmse'] = float(
        final_result['tail_fit_tv_rmse']
    )
    comparison_data['final_iv_rmse'] = float(final_result['iv_rmse'])
    comparison_data['final_validation'] = final_result['validation']

    # "Final" is the editable in-session surface, independently of database
    # publication.  This keeps the manual workflow usable while write controls
    # are disabled and lets reviewed extrapolated candidates flow to export.
    updated_table_data = [dict(row) for row in table_data]
    session_row_idx = None
    target_expiry = expiry_month(expiry)
    for table_row_idx, table_row in enumerate(updated_table_data):
        try:
            if expiry_month(table_row.get('expiry')) == target_expiry:
                session_row_idx = table_row_idx
                break
        except ValueError:
            continue
    if session_row_idx is None:
        raise PreventUpdate
    session_row = updated_table_data[session_row_idx]
    for param_key, param_val in final_params.items():
        if param_key in TTF_ADVANCED_PARAMS:
            session_row[param_key] = float(param_val)
    session_row['core_tv_rmse'] = final_rmse
    session_row['tail_fit_tv_rmse'] = float(final_result['tail_fit_tv_rmse'])
    session_row['iv_rmse'] = float(final_result['iv_rmse'])
    session_row['rmse'] = _format_tv_rmse(final_rmse)
    session_row['arb_status'] = 'Pass'
    session_row['calibration_method'] = TTF_HYBRID_METHOD
    session_row['calibration_policy_version'] = TTF_HYBRID_POLICY_VERSION
    session_row['calibration_basis'] = str(
        comparison_store.get('calibration_basis', '')
    ).strip().title()

    # Update table and plot
    comparison_table = format_comparison_data(
        current_params,
        candidate_params,
        final_params,
        comparison_params=TTF_ADVANCED_PARAMS,
        param_labels={
            'left_blend_width': 'Left blend width',
            'right_blend_width': 'Right blend width',
        },
    )

    fig = create_comparison_plot(
        exp_data, current_params, candidate_params, final_params,
        expiry_label=expiry,
        x_axis=x_axis or 'log_moneyness',
        model_version=DEFAULT_CALIBRATION_MODEL_VERSION,
    )

    return (
        True,
        comparison_data,
        f"Expiry: {expiry}",
        f"€{forward:.2f}/MWh",
        comparison_table,
        fig,
        _format_tv_rmse(comparison_store.get('current_rmse')),
        _format_tv_rmse(comparison_store.get('candidate_rmse')),
        _format_tv_rmse(final_rmse),
        no_update,
        updated_table_data,
    )


def _publication_candidate_for_expiry(
    market_data,
    table_row,
    expiry,
    node_store,
    adjustment_store,
    publication_payload=None,
    *,
    calibration_target=TTF_INTRADAY_CALIBRATION_TARGET,
):
    if calibration_target == TTF_BATCH_CALIBRATION_TARGET:
        base_observations = _settlement_ttf_observations(market_data, expiry)
    elif calibration_target == TTF_INTRADAY_CALIBRATION_TARGET:
        base_observations = _base_ttf_observations(
            market_data,
            expiry,
            publication_payload,
        )
    else:
        raise ValueError(f"Unsupported TTF calibration target: {calibration_target}")
    edited_market = _apply_node_edits(base_observations, node_store, expiry)
    observations = _select_ttf_expiry_inputs(edited_market, expiry)
    result = _evaluate_existing_hybrid(observations, table_row)
    reproduced_ivs = hybrid_iv(
        result['core'].strike_nodes,
        result['core'],
        result['params'],
        left_blend_width=float(result['left_blend_width']),
        right_blend_width=float(result['right_blend_width']),
    )
    node_error = np.asarray(reproduced_ivs) - result['core'].iv_nodes
    max_node_error = float(np.max(np.abs(node_error)))
    if not np.isfinite(max_node_error) or max_node_error > TTF_NODE_REPRODUCTION_ATOL:
        raise ValueError(
            "TTF publication candidate does not reproduce its governed 11-node "
            f"core (max IV error {max_node_error:.12g})."
        )
    surface = ttf_hybrid_operational_surface_frame(
        observations,
        _model_params(table_row),
        left_blend_width=float(result['left_blend_width']),
        right_blend_width=float(result['right_blend_width']),
        n_points=401,
    )
    surface['contract_date'] = pd.Timestamp(observations['expiry'].iloc[0])
    surface['option_expiration_date'] = pd.Timestamp(
        observations['option_expiration_date'].iloc[0]
    )
    surface['working_forward'] = float(observations['forward'].iloc[0])
    key = _expiry_store_key(expiry)
    diagnostics = dict((adjustment_store or {}).get(key) or {})
    return surface, {
        'option_expiration_date': pd.Timestamp(
            observations['option_expiration_date'].iloc[0]
        ).date().isoformat(),
        'parameters': {
            **_model_params(table_row),
            'left_blend_width': float(result['left_blend_width']),
            'right_blend_width': float(result['right_blend_width']),
        },
        'diagnostics': {
            **diagnostics,
            'calibration_target': calibration_target,
            'node_reproduction_max_iv_error': max_node_error,
            'core_tv_rmse': float(result['core_tv_rmse']),
            'tail_fit_tv_rmse': float(result['tail_fit_tv_rmse']),
            'iv_rmse': float(result['iv_rmse']),
        },
        'validation': result['validation'],
        'weighted_rmse': float(result['tail_fit_tv_rmse']),
        'unweighted_rmse': float(result['tail_fit_tv_rmse']),
        'max_error': None,
        'optimizer_success': True,
    }


_BATCH_SAVE_STATUSES = frozenset({'Success', 'Skipped'})
_BATCH_TABLE_NUMERIC_FIELDS = (
    *PARAM_COLUMNS,
    'left_blend_width',
    'right_blend_width',
    'core_tv_rmse',
    'tail_fit_tv_rmse',
    'iv_rmse',
)


def _batch_expiry_period(value):
    if isinstance(value, pd.Period):
        return value.asfreq('M')
    return expiry_month(value)


def _batch_results_ready(batch_results, expected_expiries):
    """Return whether one complete successful batch is ready to publish."""
    if not batch_results or not expected_expiries:
        return False

    try:
        expected_periods = [
            _batch_expiry_period(value) for value in expected_expiries
        ]
        result_periods = [
            _batch_expiry_period(result.get('expiry')) for result in batch_results
        ]
    except (AttributeError, TypeError, ValueError):
        return False

    if any(
        str(result.get('status') or '').strip() not in _BATCH_SAVE_STATUSES
        for result in batch_results
    ):
        return False
    if len(result_periods) != len(set(result_periods)):
        return False
    if len(expected_periods) != len(set(expected_periods)):
        return False
    return (
        len(result_periods) == len(expected_periods)
        and set(result_periods) == set(expected_periods)
    )


def _normalized_trading_date(value):
    parsed = pd.to_datetime(value, errors='coerce')
    if pd.isna(parsed):
        raise ValueError("Select a valid TTF trading date.")
    return parsed.date().isoformat()


def _finite_number(value):
    parsed = pd.to_numeric(pd.Series([value]), errors='coerce').iloc[0]
    return float(parsed) if np.isfinite(parsed) else None


def _canonical_store_value(value):
    """Return stable JSON-compatible session state for fingerprinting."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_store_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_store_value(item) for item in value]
    if isinstance(value, (pd.Timestamp, date)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (bool, str)) or value is None:
        return value
    return str(value)


def _sha256_payload(payload):
    encoded = json.dumps(
        _canonical_store_value(payload),
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _batch_table_payload(table_data):
    rows = []
    for row in table_data or []:
        try:
            expiry = str(_batch_expiry_period(row.get('expiry')))
        except (AttributeError, TypeError, ValueError):
            expiry = str(row.get('expiry') or '').strip()
        item = {
            'expiry': expiry,
            'calibration_basis': str(
                row.get('calibration_basis') or ''
            ).strip().lower(),
            'arb_status': str(row.get('arb_status') or '').strip(),
            'calibration_method': str(
                row.get('calibration_method') or ''
            ).strip(),
        }
        for field in _BATCH_TABLE_NUMERIC_FIELDS:
            value = row.get(field)
            if field == 'core_tv_rmse' and value is None:
                value = row.get('rmse')
            item[field] = _finite_number(value)
        rows.append(item)
    return sorted(rows, key=lambda row: row['expiry'])


def _batch_table_fingerprint(table_data):
    return _sha256_payload(_batch_table_payload(table_data))


def _batch_input_fingerprint(
    trading_date,
    market_data_json,
    node_store,
    publication_payload,
):
    return _sha256_payload(
        {
            'trading_date': _normalized_trading_date(trading_date),
            'market_data_sha256': hashlib.sha256(
                str(market_data_json or '').encode('utf-8')
            ).hexdigest(),
            'node_store': node_store or {},
            'base_publication_id': (
                (publication_payload or {}).get('publication_id') or None
            ),
        }
    )


def _table_publication_ready(table_data):
    if not table_data:
        return False, "No calibrated TTF parameter rows are available."
    periods = []
    for row in table_data:
        try:
            periods.append(_batch_expiry_period(row.get('expiry')))
        except (AttributeError, TypeError, ValueError):
            return False, "A parameter row has an invalid expiry."
        label = str(row.get('expiry') or periods[-1])
        if str(row.get('arb_status') or '').strip() != 'Pass':
            return False, f"{label} is not validated for publication."
        if str(row.get('calibration_method') or '').strip() != TTF_HYBRID_METHOD:
            return False, f"{label} is not calibrated with the governed TTF hybrid."
        for field in PARAM_COLUMNS:
            if _finite_number(row.get(field)) is None:
                return False, f"{label} has an invalid {field} parameter."
        for field in ('left_blend_width', 'right_blend_width'):
            value = _finite_number(row.get(field))
            if value is None or value <= 0:
                return False, f"{label} has no accepted PCHIP/Wing join."
        for field in ('core_tv_rmse', 'tail_fit_tv_rmse', 'iv_rmse'):
            value = _finite_number(
                row.get(field, row.get('rmse') if field == 'core_tv_rmse' else None)
            )
            if value is None or value < 0:
                return False, f"{label} has incomplete calibration diagnostics."
    if len(periods) != len(set(periods)):
        return False, "The parameter table contains duplicate expiries."
    return True, None


def _build_ttf_batch_state(
    trading_date,
    market_data_json,
    table_data,
    node_store,
    publication_payload,
    results,
):
    return {
        'schema_version': TTF_BATCH_STATE_VERSION,
        'calibration_policy_version': TTF_HYBRID_POLICY_VERSION,
        'calibration_target': TTF_BATCH_CALIBRATION_TARGET,
        'trading_date': _normalized_trading_date(trading_date),
        'input_fingerprint': _batch_input_fingerprint(
            trading_date,
            market_data_json,
            node_store,
            publication_payload,
        ),
        'table_fingerprint': _batch_table_fingerprint(table_data),
        'expiry_count': len(table_data or []),
        'results': list(results or []),
    }


def _batch_state_results(batch_state):
    if not isinstance(batch_state, dict):
        return []
    results = batch_state.get('results')
    return results if isinstance(results, list) else []


def _batch_state_ready(
    batch_state,
    trading_date,
    market_data_json,
    table_data,
    node_store,
    publication_payload,
):
    if not isinstance(batch_state, dict):
        return False, "Run Calibrate All Expiries for the selected date."
    if batch_state.get('schema_version') != TTF_BATCH_STATE_VERSION:
        return False, "The calibration result is stale; run Calibrate All Expiries again."
    if batch_state.get('calibration_policy_version') != TTF_HYBRID_POLICY_VERSION:
        return False, "The calibration policy changed; run Calibrate All Expiries again."
    if batch_state.get('calibration_target') != TTF_BATCH_CALIBRATION_TARGET:
        return False, "The calibration target changed; run Calibrate All Expiries again."
    try:
        selected_date = _normalized_trading_date(trading_date)
    except ValueError as exc:
        return False, str(exc)
    if batch_state.get('trading_date') != selected_date:
        return False, "The calibration belongs to a different trading date."
    if batch_state.get('expiry_count') != len(table_data or []):
        return False, "The calibrated expiry set no longer matches the table."
    try:
        current_input = _batch_input_fingerprint(
            trading_date,
            market_data_json,
            node_store,
            publication_payload,
        )
    except ValueError as exc:
        return False, str(exc)
    if batch_state.get('input_fingerprint') != current_input:
        return False, "The market inputs, node edits, or saved base have changed."
    if batch_state.get('table_fingerprint') != _batch_table_fingerprint(table_data):
        return False, "The parameter table changed after Calibrate All Expiries."
    table_ready, reason = _table_publication_ready(table_data)
    if not table_ready:
        return False, reason
    expected_expiries = [row.get('expiry') for row in table_data or []]
    if not _batch_results_ready(_batch_state_results(batch_state), expected_expiries):
        return False, "Every expiry must have an accepted calibration result."
    return True, None


def _publication_created_by(
    identity,
    adjustment_store,
    batch_results,
    expected_expiries,
):
    """Resolve the accountable creator for manual or full-batch publication."""
    creators = {
        str(value.get('created_by')).strip()
        for value in (adjustment_store or {}).values()
        if isinstance(value, dict) and value.get('created_by')
    }
    if len(creators) > 1:
        raise ValueError(
            "A complete publication must have one accountable candidate creator."
        )
    created_by = next(iter(creators), None)
    if created_by:
        return created_by
    if _batch_results_ready(batch_results, expected_expiries):
        created_by = str(identity.subject or '').strip()
        if created_by:
            return created_by
    raise ValueError(
        "Calibrate all expiries successfully or build a validated candidate "
        "before saving."
    )


@callback(
    [
        Output('ttf-save-all-btn', 'disabled'),
        Output('ttf-save-all-btn', 'title'),
    ],
    [
        Input('ttf-batch-results-store', 'data'),
        Input('ttf-param-table', 'data'),
        Input('ttf-date-picker', 'date'),
        Input('ttf-market-data-store', 'data'),
        Input('ttf-final-nodes-store', 'data'),
        Input('ttf-published-surface-store', 'data'),
    ],
)
def enable_ttf_batch_save(
    batch_state,
    table_data,
    trading_date,
    market_data_json,
    node_store,
    publication_payload,
):
    """Enable save only for the exact complete surface currently displayed."""
    if not ttf_publication_enabled():
        return True, "TTF calibrated-surface saving is disabled by configuration."
    ready, reason = _batch_state_ready(
        batch_state,
        trading_date,
        market_data_json,
        table_data,
        node_store,
        publication_payload,
    )
    if not ready:
        return True, reason
    return (
        False,
        f"Save {len(table_data or [])} validated expiries for "
        f"{_normalized_trading_date(trading_date)}.",
    )


@callback(
    [
        Output('ttf-published-surface-store', 'data', allow_duplicate=True),
        Output('ttf-publication-status', 'children', allow_duplicate=True),
        Output('ttf-adjustment-status', 'children', allow_duplicate=True),
        Output('ttf-final-nodes-store', 'data', allow_duplicate=True),
        Output('ttf-adjustment-store', 'data', allow_duplicate=True),
        Output('ttf-adjust-level', 'value', allow_duplicate=True),
        Output('ttf-adjust-skew', 'value', allow_duplicate=True),
        Output('ttf-adjust-put-curvature', 'value', allow_duplicate=True),
        Output('ttf-adjust-call-curvature', 'value', allow_duplicate=True),
        Output('ttf-use-selected-trade', 'value', allow_duplicate=True),
        Output('ttf-node-unlock', 'value', allow_duplicate=True),
        Output('ttf-batch-results-store', 'data', allow_duplicate=True),
    ],
    [
        Input('ttf-save-all-btn', 'n_clicks'),
        Input('ttf-publish-btn', 'n_clicks'),
    ],
    [
        State('ttf-date-picker', 'date'),
        State('ttf-trading-context-store', 'data'),
        State('ttf-published-surface-store', 'data'),
        State('ttf-market-data-store', 'data'),
        State('ttf-param-table', 'data'),
        State('ttf-final-nodes-store', 'data'),
        State('ttf-adjustment-store', 'data'),
        State('ttf-intraday-trades-store', 'data'),
        State('ttf-batch-results-store', 'data'),
    ],
    prevent_initial_call=True,
)
def publish_ttf_intraday_surface(
    save_clicks,
    publish_clicks,
    trading_date,
    trading_context,
    current_publication,
    market_data_json,
    table_data,
    node_store,
    adjustment_store,
    trade_store,
    batch_state,
):
    triggered_id = ctx.triggered_id
    if (
        triggered_id not in {'ttf-save-all-btn', 'ttf-publish-btn'}
        or not (save_clicks or publish_clicks)
        or not market_data_json
        or not table_data
    ):
        raise PreventUpdate
    try:
        if not ttf_publication_enabled():
            raise PermissionError("TTF publication is disabled.")
        identity = _current_identity()
        market_data = pd.read_json(StringIO(market_data_json), orient='split')
        rows_by_expiry = {
            expiry_month(row.get('expiry')): row for row in table_data
        }
        batch_results = _batch_state_results(batch_state)
        if triggered_id == 'ttf-save-all-btn':
            ready, reason = _batch_state_ready(
                batch_state,
                trading_date,
                market_data_json,
                table_data,
                node_store,
                current_publication,
            )
            if not ready:
                raise ValueError(reason)
        surfaces = []
        results = []
        for expiry in sorted(market_data['expiry'].dropna().unique()):
            period = expiry_month(expiry)
            row = rows_by_expiry.get(period)
            if row is None:
                raise ValueError(f"Missing parameter row for {period}.")
            surface, result = _publication_candidate_for_expiry(
                market_data,
                row,
                expiry,
                node_store,
                adjustment_store,
                current_publication,
                calibration_target=(
                    TTF_BATCH_CALIBRATION_TARGET
                    if triggered_id == 'ttf-save-all-btn'
                    else TTF_INTRADAY_CALIBRATION_TARGET
                ),
            )
            surfaces.append(surface)
            results.append(result)

        complete_surface = pd.concat(surfaces, ignore_index=True)
        expected_expiries = len(rows_by_expiry)
        actual_expiries = pd.to_datetime(
            complete_surface['contract_date'], errors='coerce'
        ).dt.to_period('M').nunique()
        if actual_expiries != expected_expiries:
            raise ValueError(
                f"Complete publication requires {expected_expiries} expiries; "
                f"built {actual_expiries}."
            )
        created_by = _publication_created_by(
            identity,
            adjustment_store,
            batch_results,
            rows_by_expiry.keys(),
        )
        manual_trade_ids = [
            str(trade.get('trade_id'))
            for trade in (trade_store or {}).get('trades', [])
            if trade.get('trade_id')
        ]
        payload = publish_ttf_surface(
            get_database_engine(),
            complete_surface,
            results,
            trading_date=trading_date,
            settlement_cob=(trading_context or {}).get('settlement_cob'),
            identity=identity,
            created_by=created_by,
            base_publication_id=(current_publication or {}).get('publication_id'),
            expected_current_publication_id=_same_day_publication_id(
                current_publication,
                trading_date,
            ),
            idempotency_key=(
                f"ttf:{pd.Timestamp(trading_date).date().isoformat()}:"
                f"{uuid4()}"
            ),
            manual_trade_ids=manual_trade_ids,
            expected_expiries=rows_by_expiry.keys(),
            notes=(
                'Published from selected settlement nodes after complete batch '
                'calibration.'
                if triggered_id == 'ttf-save-all-btn'
                else 'Published from the TTF intraday adjustment workspace.'
            ),
        )
    except Exception as exc:
        message = dbc.Alert(
            f"Publication blocked: {exc}",
            color='danger',
            className='py-1 px-2 mb-0',
        )
        return (
            no_update,
            message,
            message,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
        )

    publication_status = dbc.Alert(
        f"Working calibrated base {payload.get('publication_date')} · "
        f"published {payload.get('published_at')} · "
        f"revision {payload.get('publication_id')}",
        color='success',
        className='py-2 px-3 mb-3 small',
    )
    adjustment_status = dbc.Alert(
        f"Published {payload.get('expiry_count')} expiries and "
        f"{payload.get('row_count')} immutable points.",
        color='success',
        className='py-1 px-2 mb-0',
    )
    return (
        payload,
        publication_status,
        adjustment_status,
        {},
        {},
        0.0,
        0.0,
        0.0,
        0.0,
        [],
        [],
        [],
    )


@callback(
    Output(f'{COMMODITY_LOWER}-download-excel', 'data'),
    Input(f'{COMMODITY_LOWER}-export-btn', 'n_clicks'),
    [State(f'{COMMODITY_LOWER}-param-table', 'data'),
     State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     State(f'{COMMODITY_LOWER}-date-picker', 'date'),
     State(f'{COMMODITY_LOWER}-traded-options-store', 'data'),
     State(f'{COMMODITY_LOWER}-final-nodes-store', 'data'),
     State('ttf-trading-context-store', 'data'),
     State('ttf-published-surface-store', 'data'),
     State('ttf-intraday-trades-store', 'data'),
     State('ttf-adjustment-store', 'data')],
    prevent_initial_call=True
)
def export_to_excel(
    n_clicks,
    table_data,
    market_data_json,
    trade_date,
    traded_options_payload=None,
    node_store=None,
    trading_context=None,
    publication_payload=None,
    manual_trade_payload=None,
    adjustment_store=None,
):
    """Export parameters, calibration inputs, and displayed traded options."""
    if n_clicks is None or table_data is None:
        raise PreventUpdate

    # Parse trade date
    if trade_date is None:
        trade_date = date.today()
    else:
        trade_date = pd.to_datetime(trade_date).date()

    market_data = (
        pd.read_json(StringIO(market_data_json), orient='split')
        if market_data_json is not None
        else pd.DataFrame()
    )
    params_df = pd.DataFrame(table_data).copy()
    source_names = []
    canonical_bases = []
    for row in params_df.to_dict('records'):
        basis = str(row.get('calibration_basis', '')).strip().lower()
        source_name = 'unavailable'
        if (
            not market_data.empty
            and {'quote_class', 'source_name'}.issubset(market_data.columns)
        ):
            observations = _select_ttf_expiry_inputs(
                market_data,
                row.get('expiry'),
            )
            basis = _calibration_basis(observations)
            source_name = str(observations['source_name'].iloc[0])
        canonical_bases.append(basis)
        source_names.append(source_name)
    params_df['calibration_basis'] = canonical_bases
    params_df['source_name'] = source_names
    params_df['calibration_method'] = TTF_HYBRID_METHOD
    params_df['calibration_policy_version'] = TTF_HYBRID_POLICY_VERSION

    edited_market = _apply_node_edits(market_data, node_store)
    operational_frames = []
    for row in params_df.to_dict('records'):
        left_width = pd.to_numeric(
            pd.Series([row.get('left_blend_width')]), errors='coerce'
        ).iloc[0]
        right_width = pd.to_numeric(
            pd.Series([row.get('right_blend_width')]), errors='coerce'
        ).iloc[0]
        if not np.isfinite(left_width) or not np.isfinite(right_width):
            continue
        observations = _select_ttf_expiry_inputs(
            edited_market,
            row.get('expiry'),
        )
        validation = _evaluate_existing_hybrid(observations, row)
        params_df.loc[
            params_df['expiry'] == row.get('expiry'),
            'tail_fit_tv_rmse',
        ] = float(validation['tail_fit_tv_rmse'])
        params_df.loc[
            params_df['expiry'] == row.get('expiry'),
            'iv_rmse',
        ] = float(validation['iv_rmse'])
        operational_frames.append(
            ttf_hybrid_operational_surface_frame(
                observations,
                _model_params(row),
                left_blend_width=float(left_width),
                right_blend_width=float(right_width),
                n_points=401,
            )
        )
    operational_surface = (
        pd.concat(operational_frames, ignore_index=True)
        if operational_frames
        else pd.DataFrame()
    )

    # Create Excel file in memory
    output = BytesIO()
    intraday_export_requested = any(
        value is not None
        for value in (
            trading_context,
            publication_payload,
            manual_trade_payload,
            adjustment_store,
            node_store,
        )
    )

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        # Sheet 1: Parameters
        params_df.to_excel(writer, sheet_name='Parameters', index=False)

        if not operational_surface.empty:
            operational_surface.to_excel(
                writer,
                sheet_name='Operational Surface',
                index=False,
            )

        if not market_data.empty and intraday_export_requested:
            settlement_nodes = market_data.copy()
            settlement_nodes['trading_date'] = (
                (trading_context or {}).get('trading_date') or str(trade_date)
            )
            settlement_nodes['settlement_cob'] = (
                (trading_context or {}).get('settlement_cob')
            )
            settlement_nodes.to_excel(
                writer,
                sheet_name='Settlement Nodes',
                index=False,
            )

            candidate_nodes = edited_market.copy()
            candidate_nodes['node_state'] = [
                (
                    'adjusted'
                    if _expiry_store_key(expiry) in (node_store or {})
                    else 'base'
                )
                for expiry in candidate_nodes['expiry']
            ]
            candidate_nodes.to_excel(
                writer,
                sheet_name='Candidate Nodes',
                index=False,
            )

            published_surface = ttf_publication_frame(publication_payload)
            if not published_surface.empty:
                published_surface.to_excel(
                    writer,
                    sheet_name='Latest Published',
                    index=False,
                )

            manual_trades = pd.DataFrame(
                (manual_trade_payload or {}).get('trades') or []
            )
            if not manual_trades.empty:
                manual_trades.to_excel(
                    writer,
                    sheet_name='Intraday Manual Trades',
                    index=False,
                )

            recipes = []
            validation_rows = []
            for expiry_key, value in (adjustment_store or {}).items():
                recipe = dict(value.get('recipe') or {})
                recipes.append(
                    {
                        'expiry': expiry_key,
                        **recipe,
                        'created_by': value.get('created_by'),
                        'working_forward': value.get('working_forward'),
                        'dte': value.get('dte'),
                        'selected_trade_id': (
                            (value.get('selected_trade') or {}).get('trade_id')
                        ),
                    }
                )
                validation_rows.append(
                    {
                        'expiry': expiry_key,
                        **(value.get('validation') or {}),
                        'tail_fit_tv_rmse': value.get('tail_fit_tv_rmse'),
                        'iv_rmse': value.get('iv_rmse'),
                    }
                )
            if recipes:
                pd.DataFrame(recipes).to_excel(
                    writer,
                    sheet_name='Adjustment Recipe',
                    index=False,
                )
            if validation_rows:
                pd.DataFrame(validation_rows).to_excel(
                    writer,
                    sheet_name='Validation',
                    index=False,
                )

            tail_columns = [
                column
                for column in (
                    'expiry',
                    'dc',
                    'uc',
                    'put_wing_power',
                    'call_wing_power',
                    'left_blend_width',
                    'right_blend_width',
                    'tail_fit_tv_rmse',
                    'iv_rmse',
                    'calibration_basis',
                )
                if column in params_df.columns
            ]
            params_df[tail_columns].to_excel(
                writer,
                sheet_name='Tail Parameters',
                index=False,
            )

        # Sheet 2: Market Data (if available)
        if not market_data.empty:
            market_data.to_excel(writer, sheet_name='Market Data', index=False)

        traded_options = ttf_traded_options_frame(
            traded_options_payload,
            market_data,
        )
        if not traded_options.empty:
            traded_options_export = traded_options.copy()
            for timestamp_column in ('vendor_published_at', 'ingested_at'):
                if timestamp_column not in traded_options_export.columns:
                    continue
                traded_options_export[timestamp_column] = (
                    pd.to_datetime(
                        traded_options_export[timestamp_column],
                        errors='coerce',
                    )
                )
            traded_options_export.to_excel(
                writer,
                sheet_name='Traded Options',
                index=False,
            )

        # Sheet 3: Summary
        summary_data = {
            'Commodity': [COMMODITY],
            'Model Version': [DEFAULT_CALIBRATION_MODEL_VERSION],
            'Trade Date': [str(trade_date)],
            'Trading Date': [
                (trading_context or {}).get('trading_date') or str(trade_date)
            ],
            'Settlement COB': [
                (trading_context or {}).get('settlement_cob')
            ],
            'Latest Publication ID': [
                (publication_payload or {}).get('publication_id')
            ],
            'Latest Publication Timestamp': [
                (publication_payload or {}).get('published_at')
            ],
            'Export Date': [str(date.today())],
            'Number of Expiries': [len(table_data)],
            'Observed Expiries': [canonical_bases.count('observed')],
            'Extrapolated Expiries': [canonical_bases.count('extrapolated')],
            'ICE Traded Option Rows': [len(traded_options)],
            'Manual Intraday Trade Rows': [
                len((manual_trade_payload or {}).get('trades') or [])
            ],
            'ICE Reported Volume': [
                int(pd.to_numeric(
                    traded_options.get('total_volume'),
                    errors='coerce',
                ).sum())
                if not traded_options.empty
                else 0
            ],
            'ICE Traded Options Source': [
                (traded_options_payload or {}).get('source', 'unavailable')
            ],
            'Calibration Method': [TTF_HYBRID_METHOD],
            'Calibration Policy Version': [TTF_HYBRID_POLICY_VERSION],
            'Operational Surface Rows': [len(operational_surface)],
        }

        # Calculate native total-variance diagnostics without interpreting them
        # as IV percentages.
        rmse_values = []
        tail_rmse_values = []
        for row in table_data:
            core_value = pd.to_numeric(
                pd.Series([row.get('rmse')]), errors='coerce'
            ).iloc[0]
            tail_value = pd.to_numeric(
                pd.Series([row.get('tail_fit_tv_rmse')]), errors='coerce'
            ).iloc[0]
            if np.isfinite(core_value):
                rmse_values.append(float(core_value))
            if np.isfinite(tail_value):
                tail_rmse_values.append(float(tail_value))

        if rmse_values:
            summary_data['Average Core TV RMSE'] = [float(np.mean(rmse_values))]
            summary_data['Max Core TV RMSE'] = [float(np.max(rmse_values))]
        if tail_rmse_values:
            summary_data['Average Wing Tail TV RMSE'] = [
                float(np.mean(tail_rmse_values))
            ]
            summary_data['Max Wing Tail TV RMSE'] = [
                float(np.max(tail_rmse_values))
            ]

        summary_df = pd.DataFrame(summary_data)
        summary_df.to_excel(writer, sheet_name='Summary', index=False)

    # Get the data
    output.seek(0)
    excel_data = output.read()

    # Generate filename
    filename = f"{COMMODITY}_vol_surface_{trade_date.strftime('%Y%m%d')}.xlsx"

    return dcc.send_bytes(excel_data, filename)


# ============================================================================
# Batch Calibration Callbacks
# ============================================================================

@callback(
    [Output(f'{COMMODITY_LOWER}-batch-confirm-modal', 'is_open'),
     Output(f'{COMMODITY_LOWER}-batch-expiry-count', 'children')],
    [Input(f'{COMMODITY_LOWER}-batch-calibrate-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-batch-cancel-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-batch-confirm-btn', 'n_clicks')],
    [State(f'{COMMODITY_LOWER}-param-table', 'data'),
     State(f'{COMMODITY_LOWER}-batch-confirm-modal', 'is_open')],
    prevent_initial_call=True
)
def toggle_batch_confirm_modal(open_clicks, cancel_clicks, confirm_clicks, table_data, is_open):
    """Open/close the batch calibration confirmation modal."""
    triggered_id = ctx.triggered_id

    if triggered_id == f'{COMMODITY_LOWER}-batch-calibrate-btn':
        expiry_count = len(table_data) if table_data else 0
        basis_values = [
            str(row.get('calibration_basis', '')).strip().lower()
            for row in (table_data or [])
        ]
        observed_count = basis_values.count('observed')
        extrapolated_count = basis_values.count('extrapolated')
        if observed_count or extrapolated_count:
            summary = (
                f"{expiry_count} expiries: {observed_count} observed, "
                f"{extrapolated_count} extrapolated · settlement-node target"
            )
        else:
            summary = str(expiry_count)
        return True, summary
    elif triggered_id in [f'{COMMODITY_LOWER}-batch-cancel-btn', f'{COMMODITY_LOWER}-batch-confirm-btn']:
        return False, no_update

    return is_open, no_update


@callback(
    [Output(f'{COMMODITY_LOWER}-batch-progress-modal', 'is_open'),
     Output(f'{COMMODITY_LOWER}-batch-progress-bar', 'value'),
     Output(f'{COMMODITY_LOWER}-batch-progress-text', 'children'),
     Output(f'{COMMODITY_LOWER}-batch-results-container', 'children'),
     Output(f'{COMMODITY_LOWER}-batch-progress-close-btn', 'disabled'),
     Output(f'{COMMODITY_LOWER}-batch-results-store', 'data'),
     Output(f'{COMMODITY_LOWER}-param-table', 'data', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-data-status', 'children', allow_duplicate=True)],
    [Input(f'{COMMODITY_LOWER}-batch-confirm-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-batch-progress-close-btn', 'n_clicks')],
    [State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     State(f'{COMMODITY_LOWER}-param-table', 'data'),
     State(f'{COMMODITY_LOWER}-batch-auto-save', 'value'),
     State(f'{COMMODITY_LOWER}-batch-skip-good-fit', 'value'),
     State(f'{COMMODITY_LOWER}-date-picker', 'date'),
     State(f'{COMMODITY_LOWER}-batch-progress-modal', 'is_open'),
     State(f'{COMMODITY_LOWER}-final-nodes-store', 'data'),
     State('ttf-published-surface-store', 'data')],
    prevent_initial_call=True
)
def run_batch_calibration(confirm_clicks, close_clicks, market_data_json, table_data,
                          auto_save_opts, skip_good_opts, trade_date_str, is_open,
                          node_store=None, publication_payload=None):
    """Run batch calibration on all expiries."""
    triggered_id = ctx.triggered_id

    # Close button pressed
    if triggered_id == f'{COMMODITY_LOWER}-batch-progress-close-btn':
        return False, 0, "", [], True, no_update, no_update, no_update

    # Confirm button pressed - run calibration
    if triggered_id != f'{COMMODITY_LOWER}-batch-confirm-btn':
        raise PreventUpdate

    if market_data_json is None or table_data is None:
        raise PreventUpdate

    # Server-side publication invariant: the hybrid has no governed database
    # provenance yet, so batch output is always session/export-only.
    skip_good = 'skip_good' in (skip_good_opts or [])

    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    params_df = parse_table_data(table_data)

    expiries = sorted(market_data['expiry'].dropna().unique())
    results = []
    updated_table_data = table_data.copy()
    row_index_by_expiry = {}
    for row_index, row in enumerate(table_data):
        try:
            row_index_by_expiry[expiry_month(row.get('expiry'))] = row_index
        except ValueError:
            continue

    success_count = 0
    skip_count = 0
    fail_count = 0
    last_successful_params = None

    for expiry in expiries:
        expiry_str = pd.to_datetime(expiry).strftime('%Y-%m-%d')
        row_index = row_index_by_expiry.get(expiry_month(expiry))
        basis = None
        old_rmse = None

        try:
            if row_index is None or row_index >= len(params_df):
                raise ValueError("No editable parameter row exists for this expiry.")
            # Calibrate All establishes the selected settlement surface.  The
            # previous publication remains the manual intraday adjustment base,
            # but it must never replace the settlement IV target here.
            exp_data = _settlement_ttf_observations(market_data, expiry)
            exp_data = _apply_node_edits(exp_data, node_store, expiry)
            exp_data = _select_ttf_expiry_inputs(exp_data, expiry)
            basis = _calibration_basis(exp_data)
            eligibility_error = calibration_eligibility_error(exp_data)
            if eligibility_error:
                raise ValueError(eligibility_error)
            current_values = params_df.iloc[row_index].to_dict()
            current_params = _model_params(current_values)

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
                updated_table_data[row_index]['rmse'] = _format_tv_rmse(old_rmse)
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
            result = _run_ttf_candidate(
                exp_data,
                initial_params,
                basis=basis,
                selected_expiry=False,
            )
            new_params = _model_params(result['params'])
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
            updated_table_data[row_index]['rmse'] = _format_tv_rmse(new_rmse)
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
                    'old_rmse': _format_tv_rmse(old_rmse),
                    'new_rmse': _format_tv_rmse(new_rmse),
                    'improvement': '-',
                    'core_tv_rmse': _format_tv_rmse(new_rmse),
                    'tail_fit_tv_rmse': _format_tv_rmse(
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

    # Create results display
    results_display = html.Div([
        create_batch_summary(results),
        create_batch_results_table(results),
    ])

    # Create status badge
    if fail_count == 0:
        status_badge = dbc.Badge(
            [html.I(className="fas fa-check me-1"), f"Calibrated {success_count} expiries"],
            color="success",
            pill=True,
        )
    else:
        status_badge = dbc.Badge(
            [html.I(className="fas fa-exclamation-triangle me-1"),
             f"{success_count} OK, {fail_count} failed"],
            color="warning",
            pill=True,
        )

    batch_state = _build_ttf_batch_state(
        trade_date_str,
        market_data_json,
        updated_table_data,
        node_store,
        publication_payload,
        results,
    )
    return (True, 100, f"Completed: {success_count} calibrated, {skip_count} skipped, {fail_count} failed",
            results_display, False, batch_state, updated_table_data, status_badge)
