"""
JKM (Japan Korea Marker) commodity page.

Asian LNG benchmark with call skew characteristic.
"""

from vol_calibration.components.data_status import calibration_blocked_status

from vol_calibration import ttf_publication as publication_data

from options.vol_calibration.api import prepare_gas_publication_expiry
from vol_calibration import batch_results, batch_adapter
from options.vol_calibration import api as calibration_inputs
from datetime import date, timedelta
from vol_calibration.excel_export import excel_safe_frame
import hashlib
from io import StringIO, BytesIO
import json

import pandas as pd
import numpy as np
import dash_bootstrap_components as dbc
from dash import html, dcc, callback, clientside_callback, Input, Output, State, no_update, ctx
from dash.exceptions import PreventUpdate

from vol_calibration import jkm_batch
from vol_calibration.batch_results import parse_table_data

from vol_calibration.components.parameter_table import (
    create_parameter_table,
    format_params_for_table,
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
)
from options.vol_calibration.api import (
    calibration_eligibility_error,
    calibration_readiness,
    expiry_month,
)
from options.vol_calibration.api import (
    JKM_HYBRID_METHOD,
    JKM_HYBRID_POLICY_VERSION,
)
from vol_calibration.batch_job_controller import BatchJobController
from vol_calibration.batch_job_runner import (
    code_fingerprint as background_code_fingerprint,
)
from vol_calibration.model_version import DEFAULT_CALIBRATION_MODEL_VERSION
from vol_calibration.session_state import restore_product_table
from vol_calibration.operational_surface import (
    create_operational_surface_status,
    create_operational_surface_store,
    operational_surface_frame,
    register_operational_surface_callback,
)
from vol_calibration.ttf_publication import (
    hybrid_publication_frame,
    load_latest_hybrid_publication,
)

from options.vol_calibration.api import load_market_data_with_metadata
from options.vol_calibration.api import get_defaults
from options.vol_calibration.api import (
    get_database_engine,
    load_latest_surface_from_db,
    PARAM_COLUMNS
)

COMMODITY = 'JKM'
COMMODITY_LOWER = COMMODITY.lower()
JKM_BATCH_STATE_VERSION = 1
JKM_NODE_REPRODUCTION_ATOL = 1e-10
JKM_ADVANCED_PARAMS = [
    *PARAM_COLUMNS,
    'left_blend_width',
    'right_blend_width',
]


def get_default_date():
    """Get default date (T-1 settlement date, skip weekends)."""
    today = date.today()
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _apply_published_parameters(table_rows, market_data, publication_payload):
    updated = [dict(row) for row in (table_rows or [])]
    rows_by_period = {
        expiry_month(row.get('expiry')): row
        for row in updated
        if row.get('expiry') is not None
    }
    for expiry in sorted(market_data['expiry'].dropna().unique()):
        try:
            observations = calibration_inputs.select_hybrid_expiry_inputs(market_data, expiry)
        except ValueError:
            continue
        saved = publication_data.published_expiry_result(publication_payload, observations)
        target = rows_by_period.get(expiry_month(expiry))
        if not saved or target is None:
            continue
        parameters = saved.get('parameters') or {}
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
        target['rmse'] = batch_results.format_tv_rmse(target.get('core_tv_rmse', 0.0))
        target['arb_status'] = (
            'Pass'
            if (saved.get('validation') or {}).get('is_valid', False)
            else 'Fail'
        )
        target['calibration_method'] = JKM_HYBRID_METHOD
        target['calibration_policy_version'] = JKM_HYBRID_POLICY_VERSION
    return updated


def _published_surface_for_market(publication_payload):
    return hybrid_publication_frame(publication_payload)


def _publication_candidate_for_expiry(market_data, table_row, expiry):
    observations = calibration_inputs.select_hybrid_expiry_inputs(market_data, expiry)
    return prepare_gas_publication_expiry(
        observations,
        batch_results.model_params(table_row),
        product="JKM",
        left_blend_width=table_row.get("left_blend_width"),
        right_blend_width=table_row.get("right_blend_width"),
        fit_diagnostics=table_row.get("fit_diagnostics"),
    )


def _canonical_value(value):
    if isinstance(value, dict):
        return {str(k): _canonical_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (pd.Timestamp, date)):
        return pd.Timestamp(value).isoformat()
    return value


def _sha256_payload(payload):
    encoded = json.dumps(
        _canonical_value(payload),
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _table_fingerprint(table_data):
    fields = [
        'expiry',
        'calibration_basis',
        'arb_status',
        'calibration_method',
        *PARAM_COLUMNS,
        'left_blend_width',
        'right_blend_width',
        'core_tv_rmse',
        'tail_fit_tv_rmse',
        'iv_rmse',
    ]
    rows = [{field: row.get(field) for field in fields} for row in table_data or []]
    return _sha256_payload(rows)


def _input_fingerprint(trading_date, market_data_json, publication_payload):
    return _sha256_payload(
        {
            'trading_date': pd.Timestamp(trading_date).date().isoformat(),
            'market_data_sha256': hashlib.sha256(
                str(market_data_json or '').encode('utf-8')
            ).hexdigest(),
            'base_publication_id': (
                (publication_payload or {}).get('publication_id') or None
            ),
        }
    )


def _build_batch_state(
    trading_date,
    market_data_json,
    table_data,
    publication_payload,
    results,
):
    return {
        'schema_version': JKM_BATCH_STATE_VERSION,
        'calibration_policy_version': JKM_HYBRID_POLICY_VERSION,
        'trading_date': pd.Timestamp(trading_date).date().isoformat(),
        'input_fingerprint': _input_fingerprint(
            trading_date,
            market_data_json,
            publication_payload,
        ),
        'table_fingerprint': _table_fingerprint(table_data),
        'expiry_count': len(table_data or []),
        'results': list(results or []),
    }


def _batch_state_ready(
    batch_state,
    trading_date,
    market_data_json,
    table_data,
    publication_payload,
):
    if not isinstance(batch_state, dict):
        return False, "Run Calibrate All Expiries for the selected date."
    if batch_state.get('schema_version') != JKM_BATCH_STATE_VERSION:
        return False, "The calibration result is stale; run Calibrate All again."
    if batch_state.get('calibration_policy_version') != JKM_HYBRID_POLICY_VERSION:
        return False, "The JKM calibration policy changed; run Calibrate All again."
    if (
        batch_state.get('background_code_fingerprint')
        and batch_state['background_code_fingerprint'] != background_code_fingerprint()
    ):
        return False, "The calibration code changed; run Calibrate All again."
    if batch_state.get('trading_date') != pd.Timestamp(trading_date).date().isoformat():
        return False, "The calibration belongs to a different trading date."
    if batch_state.get('expiry_count') != len(table_data or []):
        return False, "The calibrated expiry set no longer matches the table."
    if batch_state.get('input_fingerprint') != _input_fingerprint(
        trading_date,
        market_data_json,
        publication_payload,
    ):
        return False, "The market inputs or saved base changed after calibration."
    if batch_state.get('table_fingerprint') != _table_fingerprint(table_data):
        return False, "The parameter table changed after Calibrate All Expiries."
    results = batch_state.get('results') or []
    if len(results) != len(table_data or []) or any(
        result.get('status') not in {'Success', 'Skipped'} for result in results
    ):
        return False, "Every JKM expiry must have an accepted calibration result."
    for row in table_data or []:
        if (
            row.get('arb_status') != 'Pass'
            or row.get('calibration_method') != JKM_HYBRID_METHOD
        ):
            return False, f"{row.get('expiry')} is not validated for publication."
    return True, None


def create_header():
    """Create the page header."""
    return dbc.Row([
        dbc.Col([
            html.H4([
                html.Span("JKM", className="text-warning fw-bold"),
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
                ),
            ], size="sm"),
        ], width=3),
        dbc.Col([
            html.Span(
                id=f'{COMMODITY_LOWER}-data-status',
                children=dbc.Badge("Loading...", color="secondary", className="me-2")
            ),
            dbc.Tooltip(
                id=f'{COMMODITY_LOWER}-data-status-tooltip',
                target=f'{COMMODITY_LOWER}-data-status',
                placement="bottom"
            ),
        ], width="auto"),
        dbc.Col([
            dbc.ButtonGroup([
                dbc.Button([html.I(className="fas fa-sync-alt me-1"), "Reload"], id=f'{COMMODITY_LOWER}-reload-btn', color="secondary", outline=True, size="sm"),
                dbc.Button([html.I(className="fas fa-magic me-1"), "Calibrate"], id=f'{COMMODITY_LOWER}-calibrate-all-btn', disabled=True, color="warning", outline=True, size="sm", title="Calibrate selected expiry"),
                dbc.Button([html.I(className="fas fa-layer-group me-1"), "Calibrate All Expiries"], id=f'{COMMODITY_LOWER}-batch-calibrate-btn', disabled=True, color="warning", size="sm", title="Calibrate all expiries at once"),
                dbc.Button([html.I(className="fas fa-file-excel me-1"), "Export"], id=f'{COMMODITY_LOWER}-export-btn', color="info", outline=True, size="sm"),
            ]),
        ], width="auto", className="ms-auto"),
    ], className="mb-4 align-items-center")


layout = dbc.Container([
    create_header(),
    dcc.Store(id=f'{COMMODITY_LOWER}-market-data-store'),
    create_operational_surface_store(COMMODITY),
    dcc.Store(id=f'{COMMODITY_LOWER}-params-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-selected-action-blocked-store', data=True),
    dcc.Store(id=f'{COMMODITY_LOWER}-batch-action-blocked-store', data=True),
    dcc.Store(id=f'{COMMODITY_LOWER}-comparison-data-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-batch-results-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-batch-job-store', storage_type='session'),
    dcc.Interval(id=f'{COMMODITY_LOWER}-batch-job-poll', interval=5000),
    dcc.Store(id=f'{COMMODITY_LOWER}-published-surface-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-batch-base-publication-store'),
    html.Div(
        id=f'{COMMODITY_LOWER}-publication-status',
        children=dbc.Alert(
            "Latest published JKM smile loading...",
            color="secondary",
            className="py-2 px-3 mb-3 small",
        ),
    ),
    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader(html.H6("Parameters", className="mb-0")),
                dbc.CardBody([create_parameter_table(COMMODITY)], className="p-2"),
            ]),
        ], width=12),
    ], className="mb-4"),
    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader(html.H6("Smile Plots", className="mb-0")),
                dbc.CardBody([
                    create_operational_surface_status(COMMODITY),
                    create_smile_grid(COMMODITY),
                ], className="p-2"),
            ]),
        ], width=12),
    ]),
    create_comparison_modal(
        COMMODITY,
        comparison_params=JKM_ADVANCED_PARAMS,
        param_labels={
            'left_blend_width': 'Left blend width',
            'right_blend_width': 'Right blend width',
        },
        save_label="Apply Final to session",
        save_disabled=False,
        show_basis=True,
        show_hybrid_metrics=True,
    ),
    create_batch_calibration_confirm_modal(COMMODITY, hybrid=True),
    create_batch_calibration_progress_modal(COMMODITY),
    dcc.Loading(id=f'{COMMODITY_LOWER}-loading', type='circle', children=html.Div(id=f'{COMMODITY_LOWER}-loading-output')),
    dcc.Download(id=f'{COMMODITY_LOWER}-download-excel'),
], fluid=True)

register_operational_surface_callback(COMMODITY, get_default_date)


@callback(
    [Output(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     Output(f'{COMMODITY_LOWER}-params-store', 'data'),
     Output(f'{COMMODITY_LOWER}-data-status', 'children'),
     Output(f'{COMMODITY_LOWER}-data-status-tooltip', 'children'),
     Output(f'{COMMODITY_LOWER}-selected-action-blocked-store', 'data'),
     Output(f'{COMMODITY_LOWER}-calibrate-all-btn', 'title'),
     Output(f'{COMMODITY_LOWER}-batch-action-blocked-store', 'data'),
     Output(f'{COMMODITY_LOWER}-batch-calibrate-btn', 'title')],
    [Input(f'{COMMODITY_LOWER}-date-picker', 'date'),
     Input(f'{COMMODITY_LOWER}-reload-btn', 'n_clicks')],
    prevent_initial_call=False
)
@cached_workspace_callback(COMMODITY, get_default_date)
def load_data(trade_date, reload_clicks):
    """Load market data and parameters."""
    if trade_date is None:
        trade_date = get_default_date()
    else:
        trade_date = pd.to_datetime(trade_date).date()

    result = load_market_data_with_metadata(
        COMMODITY,
        trade_date,
        allow_synthetic_fallback=False,
    )
    market_data = result['data']
    data_source = result.get('source', 'unknown')
    is_synthetic = result.get('is_synthetic', False)
    last_update = result.get('last_update')
    ready, readiness_message = calibration_readiness(
        market_data,
        include_extrapolated=True,
    )

    if market_data.empty or is_synthetic:
        badge_component, tooltip_text = format_data_status(
            data_source=data_source,
            is_synthetic=is_synthetic,
            last_update=last_update,
            trade_date=trade_date,
            commodity=COMMODITY,
            message=result.get('message'),
            error=result.get('error'),
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
        blocked_title = (
            result.get('message')
            or f'No exact-COB {COMMODITY} calibration input for {trade_date}'
        )
        return (
            empty_market_json,
            empty_params_json,
            badge_component,
            tooltip_text,
            True,
            blocked_title,
            True,
            blocked_title,
        )

    historical_params = None
    try:
        engine = get_database_engine()
        if engine is not None:
            historical_params = load_latest_surface_from_db(
                engine,
                COMMODITY,
                trade_date,
            )
    except Exception:
        historical_params = None

    defaults = get_defaults(COMMODITY)
    params_list = []
    loaded_from_db = False
    for expiry in sorted(market_data['expiry'].unique()):
        try:
            exp_data = calibration_inputs.select_hybrid_expiry_inputs(market_data, expiry)
            basis = calibration_inputs.jkm_calibration_basis(exp_data)
        except Exception:
            exp_data = pd.DataFrame()
            basis = ''
        expiry_date = pd.to_datetime(expiry).date()
        params_to_use = defaults.copy()
        if historical_params is not None and not historical_params.empty:
            matching = historical_params[
                historical_params['expiry'] == expiry_date
            ]
            if 'model_version' in matching.columns:
                matching = matching[
                    matching['model_version']
                    == DEFAULT_CALIBRATION_MODEL_VERSION
                ]
            else:
                matching = matching.iloc[0:0]
            if not matching.empty:
                loaded_from_db = True
                row = matching.iloc[0]
                for col in PARAM_COLUMNS:
                    if col in row and pd.notna(row[col]):
                        params_to_use[col] = row[col]
        params_list.append(
            {
                'expiry': expiry,
                'calibration_basis': basis,
                **params_to_use,
                'left_blend_width': np.nan,
                'right_blend_width': np.nan,
                'core_tv_rmse': np.nan,
                'tail_fit_tv_rmse': np.nan,
                'iv_rmse': np.nan,
                'calibration_method': '',
                'calibration_policy_version': '',
                'rmse': np.nan,
            }
        )

    params_df = pd.DataFrame(params_list)
    badge_component, tooltip_text = format_data_status(
        data_source=data_source,
        is_synthetic=is_synthetic,
        last_update=last_update,
        trade_date=trade_date,
        commodity=COMMODITY,
        message=result.get('message'),
        error=result.get('error'),
    )
    if loaded_from_db:
        tooltip_text += ' | Historical params loaded'

    return (
        market_data.to_json(date_format='iso', orient='split'),
        params_df.to_json(date_format='iso', orient='split'),
        badge_component,
        tooltip_text,
        not ready,
        readiness_message,
        not ready,
        readiness_message,
    )


@callback(
    [
        Output(f'{COMMODITY_LOWER}-published-surface-store', 'data'),
        Output(f'{COMMODITY_LOWER}-publication-status', 'children'),
    ],
    [
        Input(f'{COMMODITY_LOWER}-date-picker', 'date'),
        Input(f'{COMMODITY_LOWER}-reload-btn', 'n_clicks'),
    ],
    prevent_initial_call=False,
)
def load_jkm_publication(trading_date, reload_clicks):
    try:
        payload = load_latest_hybrid_publication(
            get_database_engine(),
            pd.Timestamp(trading_date).date(),
            commodity=COMMODITY,
            prefer_exact_cob=True,
            force_refresh=bool(reload_clicks),
        )
    except Exception as exc:
        payload = {
            'publication_id': None,
            'data': pd.DataFrame().to_json(orient='split'),
            'expiry_results': [],
            'error': str(exc),
        }
    if payload.get('publication_id'):
        status = dbc.Alert(
            f"Working calibrated base {payload.get('publication_date')} · "
            f"published {payload.get('published_at')} · "
            f"revision {payload.get('publication_id')}",
            color='success',
            className='py-2 px-3 mb-3 small',
        )
    else:
        detail = payload.get('error') or 'No prior JKM publication exists.'
        status = dbc.Alert(
            f"Latest published JKM smile unavailable · {detail}",
            color='warning',
            className='py-2 px-3 mb-3 small',
        )
    return payload, status


clientside_callback(
    """function(payload) {
        if (!payload) return null;
        const keys = ['publication_id', 'input_manifest_fingerprint',
                      'published_at', 'publication_date', 'policy_version'];
        return Object.fromEntries(keys.map(key => [key, payload[key] ?? null]));
    }""",
    Output(f'{COMMODITY_LOWER}-batch-base-publication-store', 'data'),
    Input(f'{COMMODITY_LOWER}-published-surface-store', 'data'),
)


@callback(
    Output(f'{COMMODITY_LOWER}-param-table', 'data'),
    [
        Input(f'{COMMODITY_LOWER}-params-store', 'data'),
        Input(f'{COMMODITY_LOWER}-published-surface-store', 'data'),
    ],
    State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
    State('vol-calibration-session-state', 'data'),
    State(f'{COMMODITY_LOWER}-date-picker', 'date'),
    prevent_initial_call=True
)
def update_param_table(
    params_json,
    publication_payload,
    market_data_json,
    session_state,
    trade_date,
):
    restored = restore_product_table(session_state, COMMODITY_LOWER, trade_date)
    if restored is not None and all(
        row.get('calibration_basis') for row in restored
    ):
        return restored
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

    formatted = format_params_for_table(
        params_df,
        market_data,
        commodity=COMMODITY,
    )
    return _apply_published_parameters(
        formatted,
        market_data if market_data is not None else pd.DataFrame(),
        publication_payload,
    )


@callback(
    Output(f'{COMMODITY_LOWER}-smile-grid', 'figure'),
    [Input(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     Input(f'{COMMODITY_LOWER}-param-table', 'data'),
     Input(f'{COMMODITY_LOWER}-x-axis-selector', 'value'),
     Input(f'{COMMODITY_LOWER}-param-table', 'selected_rows'),
     Input(f'{COMMODITY_LOWER}-operational-surface-store', 'data'),
     Input(f'{COMMODITY_LOWER}-published-surface-store', 'data')],
    prevent_initial_call=True
)
def update_smile_grid(
    market_data_json,
    table_data,
    x_axis,
    selected_rows,
    operational_payload,
    publication_payload,
):
    if market_data_json is None and operational_payload is None:
        raise PreventUpdate
    market_data = (
        pd.read_json(StringIO(market_data_json), orient='split')
        if market_data_json
        else pd.DataFrame()
    )
    params_df = parse_table_data(table_data or [])
    selected_row = selected_rows[0] if selected_rows else None
    selected_axis = x_axis or 'delta'
    return create_smile_grid_figure(
        market_data,
        params_df,
        selected_axis,
        selected_row,
        model_version=DEFAULT_CALIBRATION_MODEL_VERSION,
        operational_surface=operational_surface_frame(operational_payload),
        operational_metadata=operational_payload,
        published_surface=_published_surface_for_market(publication_payload),
        published_metadata=publication_payload,
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
     State(f'{COMMODITY_LOWER}-data-status', 'children')],
    prevent_initial_call=True
)
def handle_calibration(
    calibrate_clicks,
    cancel_clicks,
    save_clicks,
    copy_clicks,
    reset_clicks,
    comparison_table_data,
    market_data_json,
    table_data,
    selected_rows,
    is_open,
    comparison_store,
    x_axis,
    trade_date_str,
    current_status_badge,
):
    del calibrate_clicks, cancel_clicks, save_clicks, copy_clicks, reset_clicks
    del is_open, trade_date_str, current_status_badge
    triggered_id = ctx.triggered_id
    empty_fig = {}
    default_outputs = (
        False, None, "", "", [], empty_fig, "", "", "", no_update, no_update
    )
    if triggered_id == f'{COMMODITY_LOWER}-comparison-cancel-btn':
        return default_outputs
    if market_data_json is None or table_data is None:
        raise PreventUpdate

    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    params_df = parse_table_data(table_data)
    if triggered_id == f'{COMMODITY_LOWER}-calibrate-all-btn':
        row_idx = selected_rows[0] if selected_rows else 0
        if row_idx >= len(params_df):
            raise PreventUpdate
        expiry = params_df.iloc[row_idx]['expiry']
        try:
            observations = calibration_inputs.select_hybrid_expiry_inputs(market_data, expiry)
            basis = calibration_inputs.jkm_calibration_basis(observations)
            error = calibration_eligibility_error(observations)
            if error:
                raise ValueError(error)
            forward = float(observations['forward'].iloc[0])
            current_values = params_df.iloc[row_idx].to_dict()
            current_params = batch_results.model_params(current_values)
            candidate = batch_adapter.fit_jkm_candidate(
                observations,
                current_params,
                basis=basis,
            )
            candidate_params = batch_results.candidate_params(candidate)
        except Exception as exc:
            return (
                False, None, "", "", [], empty_fig, "", "", "",
                calibration_blocked_status(str(exc)), no_update,
            )

        try:
            current_result = calibration_inputs.evaluate_jkm_parameter_row(
                observations,
                current_values,
            )
            current_params = {
                **current_params,
                'left_blend_width': float(current_result['left_blend_width']),
                'right_blend_width': float(current_result['right_blend_width']),
            }
            current_rmse = float(current_result['core_tv_rmse'])
        except Exception:
            current_result = None
            current_rmse = np.nan

        comparison_data = {
            'expiry': str(expiry),
            'forward': forward,
            'current_params': current_params,
            'candidate_params': candidate_params,
            'final_params': current_params.copy(),
            'current_rmse': current_rmse,
            'candidate_rmse': float(candidate['core_tv_rmse']),
            'current_tail_fit_tv_rmse': (
                float(current_result['tail_fit_tv_rmse'])
                if current_result is not None else None
            ),
            'current_iv_rmse': (
                float(current_result['iv_rmse'])
                if current_result is not None else None
            ),
            'candidate_tail_fit_tv_rmse': float(candidate['tail_fit_tv_rmse']),
            'candidate_iv_rmse': float(candidate['iv_rmse']),
            'final_tail_fit_tv_rmse': (
                float(current_result['tail_fit_tv_rmse'])
                if current_result is not None else None
            ),
            'final_iv_rmse': (
                float(current_result['iv_rmse'])
                if current_result is not None else None
            ),
            'calibration_basis': basis,
            'source_name': str(observations['source_name'].iloc[0]),
            'calibration_method': JKM_HYBRID_METHOD,
            'calibration_policy_version': JKM_HYBRID_POLICY_VERSION,
            'row_idx': row_idx,
        }
        comparison_table = format_comparison_data(
            current_params,
            candidate_params,
            current_params,
            comparison_params=JKM_ADVANCED_PARAMS,
            param_labels={
                'left_blend_width': 'Left blend width',
                'right_blend_width': 'Right blend width',
            },
        )
        fig = create_comparison_plot(
            observations,
            current_params,
            candidate_params,
            current_params,
            expiry_label=expiry,
            x_axis=x_axis or 'log_moneyness',
            model_version=DEFAULT_CALIBRATION_MODEL_VERSION,
        )
        return (
            True,
            comparison_data,
            f"Expiry: {expiry}",
            f"${forward:.2f}/MMBtu",
            comparison_table,
            fig,
            batch_results.format_tv_rmse(current_rmse),
            batch_results.format_tv_rmse(candidate['core_tv_rmse']),
            batch_results.format_tv_rmse(current_rmse),
            no_update,
            no_update,
        )

    if comparison_store is None:
        raise PreventUpdate
    current_params = comparison_store.get('current_params', {})
    candidate_params = comparison_store.get('candidate_params', {})
    expiry = comparison_store.get('expiry', '')
    forward = float(comparison_store.get('forward', 14.0))
    if triggered_id == f'{COMMODITY_LOWER}-copy-candidate-btn':
        final_params = candidate_params.copy()
    elif triggered_id == f'{COMMODITY_LOWER}-reset-final-btn':
        final_params = current_params.copy()
    elif triggered_id == f'{COMMODITY_LOWER}-comparison-save-btn':
        final_params = dict(comparison_store.get('final_params') or {})
    else:
        final_params = extract_final_params(comparison_table_data)

    try:
        observations = calibration_inputs.select_hybrid_expiry_inputs(market_data, expiry)
        final_result = calibration_inputs.evaluate_jkm_parameter_row(observations, final_params)
    except Exception as exc:
        return (
            True, comparison_store, f"Expiry: {expiry}", f"${forward:.2f}/MMBtu",
            comparison_table_data, empty_fig, "", "", "",
            calibration_blocked_status(str(exc)), no_update,
        )

    comparison_data = dict(comparison_store)
    comparison_data['final_params'] = final_params
    comparison_data['final_rmse'] = float(final_result['core_tv_rmse'])
    comparison_data['final_tail_fit_tv_rmse'] = float(
        final_result['tail_fit_tv_rmse']
    )
    comparison_data['final_iv_rmse'] = float(final_result['iv_rmse'])
    comparison_data['final_validation'] = final_result['validation']
    updated_table = [dict(row) for row in table_data]
    target_period = expiry_month(expiry)
    target_row = next(
        (
            row for row in updated_table
            if expiry_month(row.get('expiry')) == target_period
        ),
        None,
    )
    if target_row is None:
        raise PreventUpdate
    calibration_inputs.update_jkm_parameter_row(
        target_row,
        final_result,
        str(comparison_store.get('calibration_basis')).strip().lower(),
    )
    comparison_table = format_comparison_data(
        current_params,
        candidate_params,
        final_params,
        comparison_params=JKM_ADVANCED_PARAMS,
        param_labels={
            'left_blend_width': 'Left blend width',
            'right_blend_width': 'Right blend width',
        },
    )
    fig = create_comparison_plot(
        observations,
        current_params,
        candidate_params,
        final_params,
        expiry_label=expiry,
        x_axis=x_axis or 'log_moneyness',
        model_version=DEFAULT_CALIBRATION_MODEL_VERSION,
    )
    keep_open = triggered_id != f'{COMMODITY_LOWER}-comparison-save-btn'
    return (
        keep_open,
        comparison_data if keep_open else None,
        f"Expiry: {expiry}" if keep_open else "",
        f"${forward:.2f}/MMBtu" if keep_open else "",
        comparison_table if keep_open else [],
        fig if keep_open else empty_fig,
        batch_results.format_tv_rmse(comparison_store.get('current_rmse')) if keep_open else "",
        batch_results.format_tv_rmse(comparison_store.get('candidate_rmse')) if keep_open else "",
        batch_results.format_tv_rmse(final_result['core_tv_rmse']) if keep_open else "",
        no_update,
        updated_table,
    )


@callback(
    Output(f'{COMMODITY_LOWER}-comparison-basis', 'children'),
    Input(f'{COMMODITY_LOWER}-comparison-data-store', 'data'),
    prevent_initial_call=True,
)
def render_comparison_basis(comparison_data):
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
    data = comparison_data or {}

    def iv_value(name):
        value = pd.to_numeric(pd.Series([data.get(name)]), errors='coerce').iloc[0]
        return f"{float(value) * 100:.2f}%" if np.isfinite(value) else "Unavailable"

    return (
        batch_results.format_tv_rmse(data.get('current_tail_fit_tv_rmse')),
        batch_results.format_tv_rmse(data.get('candidate_tail_fit_tv_rmse')),
        batch_results.format_tv_rmse(data.get('final_tail_fit_tv_rmse')),
        iv_value('current_iv_rmse'),
        iv_value('candidate_iv_rmse'),
        iv_value('final_iv_rmse'),
    )


@callback(
    Output(f'{COMMODITY_LOWER}-download-excel', 'data'),
    Input(f'{COMMODITY_LOWER}-export-btn', 'n_clicks'),
    [State(f'{COMMODITY_LOWER}-param-table', 'data'),
     State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     State(f'{COMMODITY_LOWER}-date-picker', 'date')],
    prevent_initial_call=True
)
def export_to_excel(n_clicks, table_data, market_data_json, trade_date):
    """Export parameter table and market data to Excel."""
    if n_clicks is None or table_data is None:
        raise PreventUpdate

    if trade_date is None:
        trade_date = date.today()
    else:
        trade_date = pd.to_datetime(trade_date).date()

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        params_df = pd.DataFrame(table_data)
        excel_safe_frame(params_df).to_excel(writer, sheet_name='Parameters', index=False)

        market_data = pd.DataFrame()
        if market_data_json is not None:
            try:
                market_data = pd.read_json(StringIO(market_data_json), orient='split')
                excel_safe_frame(market_data).to_excel(writer, sheet_name='Market Data', index=False)
            except Exception:
                pass

        operational_frames = []
        if not market_data.empty:
            rows_by_period = {
                expiry_month(row.get('expiry')): row for row in table_data
            }
            for expiry in sorted(market_data['expiry'].dropna().unique()):
                row = rows_by_period.get(expiry_month(expiry))
                if not row or row.get('arb_status') != 'Pass':
                    continue
                try:
                    surface, _ = _publication_candidate_for_expiry(
                        market_data,
                        row,
                        expiry,
                    )
                    operational_frames.append(surface)
                except Exception:
                    continue
        if operational_frames:
            excel_safe_frame(pd.concat(operational_frames, ignore_index=True)).to_excel(
                writer,
                sheet_name='Operational Surface',
                index=False,
            )

        summary_data = {
            'Commodity': [COMMODITY],
            'Model Version': [DEFAULT_CALIBRATION_MODEL_VERSION],
            'Calibration Method': [JKM_HYBRID_METHOD],
            'Calibration Policy': [JKM_HYBRID_POLICY_VERSION],
            'Trade Date': [str(trade_date)],
            'Export Date': [str(date.today())],
            'Number of Expiries': [len(table_data)],
            'Observed Expiries': [sum(
                str(row.get('calibration_basis', '')).lower() == 'observed'
                for row in table_data
            )],
            'Extrapolated Expiries': [sum(
                str(row.get('calibration_basis', '')).lower() == 'extrapolated'
                for row in table_data
            )],
        }

        rmse_values = []
        for row in table_data:
            rmse_str = row.get('rmse', '')
            if isinstance(rmse_str, str):
                try:
                    rmse_values.append(float(rmse_str.replace('%', '')))
                except ValueError:
                    pass
            elif pd.notna(rmse_str):
                rmse_values.append(float(rmse_str))

        if rmse_values:
            summary_data['Average Core TV RMSE'] = [f"{np.mean(rmse_values):.6f}"]
            summary_data['Max Core TV RMSE'] = [f"{np.max(rmse_values):.6f}"]
            summary_data['Min Core TV RMSE'] = [f"{np.min(rmse_values):.6f}"]

        summary_df = pd.DataFrame(summary_data)
        excel_safe_frame(summary_df).to_excel(writer, sheet_name='Summary', index=False)

    output.seek(0)
    excel_data = output.read()
    filename = f"{COMMODITY}_vol_surface_{trade_date.strftime('%Y%m%d')}.xlsx"

    return dcc.send_bytes(excel_data, filename)


# ============================================================================
# Batch Calibration Callbacks
# ============================================================================

@callback(
    Output(f'{COMMODITY_LOWER}-calibrate-all-btn', 'disabled'),
    Output(f'{COMMODITY_LOWER}-batch-calibrate-btn', 'disabled'),
    Input(f'{COMMODITY_LOWER}-selected-action-blocked-store', 'data'),
    Input(f'{COMMODITY_LOWER}-batch-action-blocked-store', 'data'),
    Input(f'{COMMODITY_LOWER}-market-data-store', 'data'),
    Input(f'{COMMODITY_LOWER}-param-table', 'data'),
)
def calibration_actions_disabled(selected_blocked, batch_blocked, market_data_json, table_data):
    """Keep actions unavailable until both UI inputs have arrived."""
    loading = not market_data_json or not table_data
    return loading or bool(selected_blocked), loading or bool(batch_blocked)


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
        bases = [
            str(row.get('calibration_basis', '')).strip().lower()
            for row in (table_data or [])
        ]
        return (
            True,
            f"{expiry_count} expiries: {bases.count('observed')} observed, "
            f"{bases.count('extrapolated')} extrapolated · settlement-node target",
        )
    elif triggered_id in [f'{COMMODITY_LOWER}-batch-cancel-btn', f'{COMMODITY_LOWER}-batch-confirm-btn']:
        return False, no_update

    return is_open, no_update


def _batch_job_controller():
    return BatchJobController(
        product=COMMODITY,
        calibrate=jkm_batch.calibrate_jkm_batch,
        build_state=_build_batch_state,
        state_ready=_batch_state_ready,
    )


@callback(
    [Output(f'{COMMODITY_LOWER}-batch-progress-modal', 'is_open'),
     Output(f'{COMMODITY_LOWER}-batch-progress-bar', 'value'),
     Output(f'{COMMODITY_LOWER}-batch-progress-text', 'children'),
     Output(f'{COMMODITY_LOWER}-batch-results-container', 'children'),
     Output(f'{COMMODITY_LOWER}-batch-progress-close-btn', 'disabled'),
     Output(f'{COMMODITY_LOWER}-batch-results-store', 'data'),
     Output(f'{COMMODITY_LOWER}-param-table', 'data', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-data-status', 'children', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-batch-job-store', 'data')],
    [Input(f'{COMMODITY_LOWER}-batch-confirm-btn', 'n_clicks'),
     Input(f'{COMMODITY_LOWER}-batch-progress-close-btn', 'n_clicks')],
    [State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     State(f'{COMMODITY_LOWER}-param-table', 'data'),
     State(f'{COMMODITY_LOWER}-batch-auto-save', 'value'),
     State(f'{COMMODITY_LOWER}-batch-skip-good-fit', 'value'),
     State(f'{COMMODITY_LOWER}-date-picker', 'date'),
     State(f'{COMMODITY_LOWER}-batch-progress-modal', 'is_open'),
     State(f'{COMMODITY_LOWER}-published-surface-store', 'data')],
    prevent_initial_call=True
)
def run_batch_calibration(confirm_clicks, close_clicks, market_data_json, table_data,
                          auto_save_opts, skip_good_opts, trade_date_str, is_open,
                          publication_payload=None):
    """Run the shared batch lifecycle with this product's candidate rules."""
    return _batch_job_controller().run(
        ctx.triggered_id, market_data_json, table_data, skip_good_opts,
        trade_date_str, publication_payload,
    )


@callback(
    [Output(f'{COMMODITY_LOWER}-batch-progress-modal', 'is_open', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-batch-progress-bar', 'value', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-batch-progress-text', 'children', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-batch-results-container', 'children', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-batch-progress-close-btn', 'disabled', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-batch-results-store', 'data', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-param-table', 'data', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-data-status', 'children', allow_duplicate=True),
     Output(f'{COMMODITY_LOWER}-batch-progress-cancel-btn', 'disabled')],
    [Input(f'{COMMODITY_LOWER}-batch-job-poll', 'n_intervals'),
     Input(f'{COMMODITY_LOWER}-batch-job-store', 'data')],
    [State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     State(f'{COMMODITY_LOWER}-param-table', 'data'),
     State(f'{COMMODITY_LOWER}-date-picker', 'date'),
     State(f'{COMMODITY_LOWER}-batch-base-publication-store', 'data'),
     State(f'{COMMODITY_LOWER}-batch-skip-good-fit', 'value'),
     State(f'{COMMODITY_LOWER}-batch-progress-modal', 'is_open'),
     State(f'{COMMODITY_LOWER}-batch-results-store', 'data')],
    prevent_initial_call=True,
)
def poll_jkm_batch_job(
    _tick, job_reference, market_data_json, table_data, trade_date_str,
    publication_payload, skip_good_opts, is_open, batch_state,
):
    return _batch_job_controller().poll(
        job_reference, market_data_json, table_data, trade_date_str,
        publication_payload, skip_good_opts, is_open, batch_state,
    )


@callback(
    Output(f'{COMMODITY_LOWER}-batch-progress-text', 'children', allow_duplicate=True),
    Input(f'{COMMODITY_LOWER}-batch-progress-cancel-btn', 'n_clicks'),
    State(f'{COMMODITY_LOWER}-batch-job-store', 'data'),
    prevent_initial_call=True,
)
def cancel_jkm_batch_job(_clicks, job_reference):
    return _batch_job_controller().cancel(job_reference)
