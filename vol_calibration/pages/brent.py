"""
BRENT commodity page.

Oil options with put skew characteristic.
"""
from datetime import date, timedelta
from io import StringIO, BytesIO

import pandas as pd
import numpy as np
import dash_bootstrap_components as dbc
from dash import html, dcc, callback, Input, Output, State, no_update, ctx
from dash.exceptions import PreventUpdate

from vol_calibration.components.brent_adjustment_table import (
    create_brent_adjustment_table,
    format_brent_adjustment_rows,
    parse_brent_adjustment_rows,
)
from vol_calibration.components.brent_adjustment_plot import (
    create_brent_adjustment_comparison,
    create_brent_adjustment_grid,
)
from vol_calibration.components.smile_grid import create_smile_grid
from vol_calibration.components.comparison_modal import (
    create_comparison_modal,
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
from vol_calibration.feature_flags import writes_enabled
from vol_calibration.brent_intraday import (
    ADJUSTMENT_LABELS,
    ADJUSTMENT_PARAMS,
    BrentAdjustmentError,
    baseline_row,
    calibrate_adjustment,
    evaluate_adjustment,
    prepare_adjustment_fit,
    select_expiry_rows,
    select_surface_slice,
    validate_adjustment,
)
from vol_calibration.session_state import restore_product_table
from vol_calibration.operational_surface import (
    create_operational_surface_status,
    create_operational_surface_store,
    load_operational_surface_payload,
    operational_surface_frame,
    register_operational_surface_callback,
)

from options.calibration_engine.io.loaders import load_market_data_with_metadata
COMMODITY = 'BRENT'
COMMODITY_LOWER = COMMODITY.lower()
BRENT_ADJUSTMENT_MODEL_VERSION = 'brent_svi_intraday_residual_v1'


def get_default_date():
    """Get default date (T-1 settlement date, skip weekends)."""
    today = date.today()
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def create_header():
    """Create the page header."""
    return dbc.Row([
        dbc.Col([
            html.H4([
                html.Span("BRENT", className="text-danger fw-bold"),
                html.Span(" Intraday SVI Adjustment", className="text-muted"),
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
            html.Div([
                html.Span(
                    id=f'{COMMODITY_LOWER}-data-status',
                    children=dbc.Badge([html.I(className="fas fa-spinner fa-spin me-1"), "Loading..."], color="secondary", pill=True),
                ),
                dbc.Tooltip(id=f'{COMMODITY_LOWER}-data-status-tooltip', target=f'{COMMODITY_LOWER}-data-status', placement="bottom"),
            ], className="d-inline-block"),
        ], width="auto"),
        dbc.Col([
            dbc.ButtonGroup([
                dbc.Button(
                    [html.I(className="fas fa-history me-1"), "Settlement History"],
                    href="/brent_vol_history",
                    color="secondary",
                    outline=True,
                    size="sm",
                ),
                dbc.Button([html.I(className="fas fa-sync-alt me-1"), "Reload"], id=f'{COMMODITY_LOWER}-reload-btn', color="secondary", outline=True, size="sm"),
                dbc.Button([html.I(className="fas fa-magic me-1"), "Calibrate Adjustment"], id=f'{COMMODITY_LOWER}-calibrate-all-btn', color="danger", outline=True, size="sm", title="Calibrate selected expiry against the official SVI baseline"),
                dbc.Button([html.I(className="fas fa-layer-group me-1"), "Adjust All Expiries"], id=f'{COMMODITY_LOWER}-batch-calibrate-btn', color="danger", size="sm", title="Calibrate SVI-relative adjustments for all eligible expiries"),
                dbc.Button(
                    [html.I(className="fas fa-save me-1"), "Save All"],
                    id=f'{COMMODITY_LOWER}-save-all-btn',
                    color="primary",
                    outline=True,
                    size="sm",
                    disabled=not writes_enabled(),
                    title="Saving is disabled during the migration release.",
                ),
                dbc.Button([html.I(className="fas fa-file-excel me-1"), "Export"], id=f'{COMMODITY_LOWER}-export-btn', color="info", outline=True, size="sm"),
            ]),
        ], width="auto", className="ms-auto"),
    ], className="mb-4 align-items-center")


layout = dbc.Container([
    create_header(),
    dcc.Store(id=f'{COMMODITY_LOWER}-market-data-store'),
    create_operational_surface_store(COMMODITY),
    dcc.Store(id=f'{COMMODITY_LOWER}-params-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-comparison-data-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-batch-results-store'),
    dbc.Alert(
        [
            html.Strong("Calibration contract: "),
            "the official SVI surface is the zero-adjustment baseline. Blue points are eligible American futures-style observations; grey crosses are observed but excluded references and never move the fit.",
        ],
        color="info",
        className="py-2 px-3 mb-3 small",
    ),
    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader(html.H6("SVI-relative adjustment parameters", className="mb-0")),
                dbc.CardBody([create_brent_adjustment_table()], className="p-2"),
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
        comparison_params=list(ADJUSTMENT_PARAMS),
        param_labels=ADJUSTMENT_LABELS,
        save_label="Apply Final to Draft",
        save_disabled=False,
        save_title="Apply the validated adjustment to this browser session only; no database publication.",
    ),
    create_batch_calibration_confirm_modal(COMMODITY),
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
     Output(f'{COMMODITY_LOWER}-calibrate-all-btn', 'disabled'),
     Output(f'{COMMODITY_LOWER}-calibrate-all-btn', 'title'),
     Output(f'{COMMODITY_LOWER}-batch-calibrate-btn', 'disabled'),
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

    # Load market data with metadata
    load_result = load_market_data_with_metadata(
        COMMODITY,
        trade_date,
        allow_synthetic_fallback=False,
    )
    market_data = load_result['data']
    data_source = load_result['source']
    is_synthetic = load_result['is_synthetic']
    last_update = load_result['last_update']

    if market_data.empty or is_synthetic:
        badge, tooltip = format_data_status(
            data_source=data_source,
            is_synthetic=is_synthetic,
            last_update=last_update,
            trade_date=trade_date,
            commodity=COMMODITY,
            message=load_result.get('message'),
            error=load_result.get('error'),
        )
        empty_market_json = pd.DataFrame(
            columns=[
                'expiry', 'dte', 'delta', 'iv', 'strike', 'forward',
                'calibration_eligible', 'exclusion_reason',
            ]
        ).to_json(date_format='iso', orient='split')
        empty_params_json = pd.DataFrame(
            columns=[
                'expiry', *ADJUSTMENT_PARAMS, 'eligible_points',
                'excluded_points', 'validation', 'rmse', 'message',
            ]
        ).to_json(date_format='iso', orient='split')
        blocked_title = (
            load_result.get('message')
            or f"No observed {COMMODITY} option data for {trade_date}"
        )
        return (
            empty_market_json,
            empty_params_json,
            badge,
            tooltip,
            True,
            blocked_title,
            True,
            blocked_title,
        )

    # Resolve the same governed SVI snapshot used by /vol_surface.  It is the
    # immutable zero-adjustment anchor, not another set of observations to fit.
    try:
        operational_payload = load_operational_surface_payload(COMMODITY, trade_date)
        operational_surface = operational_surface_frame(operational_payload)
    except Exception as exc:
        operational_payload = {'error': str(exc)}
        operational_surface = pd.DataFrame()

    params_df = pd.DataFrame(
        [
            baseline_row(
                select_expiry_rows(market_data, expiry),
                select_surface_slice(operational_surface, expiry),
                expiry,
            )
            for expiry in sorted(pd.to_datetime(market_data['expiry']).unique())
        ]
    )
    ready_count = int(
        params_df.get('validation', pd.Series(dtype=str)).eq('Pass').sum()
    )
    exact_baseline = bool(
        operational_payload.get('actual_cob')
        and operational_payload.get('requested_cob')
        == operational_payload.get('actual_cob')
    )
    calibration_ready = ready_count > 0 and exact_baseline

    # Create status badge and tooltip
    badge, tooltip = format_data_status(
        data_source,
        is_synthetic,
        last_update,
        trade_date,
        COMMODITY,
        message=load_result.get('message'),
        error=load_result.get('error'),
    )
    calibration_mode = load_result.get('calibration_mode') or 'unknown'
    tooltip_parts = [
        tooltip,
        f"Mode: {calibration_mode}",
        f"Eligible expiries: {ready_count}",
        "Anchor: exact-COB official SVI" if exact_baseline else "Anchor unavailable or prior-COB",
    ]
    if not load_result.get('provenance_complete', False):
        tooltip_parts.append('Source timestamps incomplete')
    action_title = (
        "No expiry has at least 8 eligible body strikes and an exact-COB SVI baseline"
        if not calibration_ready
        else (
            "Run historical EOD replay adjustment"
            if calibration_mode == 'eod_settlement_replay'
            else "Calibrate selected intraday snapshot against official SVI"
        )
    )

    return (
        market_data.to_json(date_format='iso', orient='split'),
        params_df.to_json(date_format='iso', orient='split'),
        badge,
        " | ".join(tooltip_parts),
        not calibration_ready,
        action_title,
        not calibration_ready,
        action_title,
    )


@callback(
    Output(f'{COMMODITY_LOWER}-param-table', 'data'),
    Input(f'{COMMODITY_LOWER}-params-store', 'data'),
    State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
    State('vol-calibration-session-state', 'data'),
    State(f'{COMMODITY_LOWER}-date-picker', 'date'),
    prevent_initial_call=True
)
def update_param_table(params_json, market_data_json, session_state, trade_date):
    restored = restore_product_table(session_state, COMMODITY_LOWER, trade_date)
    if restored is not None and all(
        all(name in row for name in ADJUSTMENT_PARAMS) for row in restored
    ):
        return restored
    if params_json is None:
        return []

    params_df = pd.read_json(StringIO(params_json), orient='split')

    return format_brent_adjustment_rows(params_df)


@callback(
    Output(f'{COMMODITY_LOWER}-smile-grid', 'figure'),
    [Input(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     Input(f'{COMMODITY_LOWER}-param-table', 'data'),
     Input(f'{COMMODITY_LOWER}-x-axis-selector', 'value'),
     Input(f'{COMMODITY_LOWER}-param-table', 'selected_rows'),
     Input(f'{COMMODITY_LOWER}-operational-surface-store', 'data')],
    prevent_initial_call=True
)
def update_smile_grid(
    market_data_json,
    table_data,
    x_axis,
    selected_rows,
    operational_payload,
):
    if market_data_json is None and operational_payload is None:
        raise PreventUpdate
    market_data = (
        pd.read_json(StringIO(market_data_json), orient='split')
        if market_data_json
        else pd.DataFrame()
    )
    params_df = parse_brent_adjustment_rows(table_data or [])
    selected_row = selected_rows[0] if selected_rows else None
    selected_axis = x_axis or 'delta'
    return create_brent_adjustment_grid(
        market_data,
        params_df,
        selected_axis,
        selected_row,
        operational_surface_frame(operational_payload),
        operational_payload,
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
     State(f'{COMMODITY_LOWER}-operational-surface-store', 'data')],
    prevent_initial_call=True
)
def handle_calibration(calibrate_clicks, cancel_clicks, save_clicks, copy_clicks, reset_clicks, comparison_table_data,
                       market_data_json, table_data, selected_rows, is_open, comparison_store, x_axis,
                       trade_date_str, current_status_badge, operational_payload=None):
    triggered_id = ctx.triggered_id
    empty_fig = {}
    default_outputs = (False, None, "", "", [], empty_fig, "", "", "", no_update, no_update)

    if triggered_id == f'{COMMODITY_LOWER}-comparison-cancel-btn':
        return default_outputs
    if market_data_json is None or table_data is None:
        raise PreventUpdate

    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    params_df = parse_brent_adjustment_rows(table_data)
    surface = operational_surface_frame(operational_payload)

    def failure_outputs(message):
        badge = dbc.Badge(
            [html.I(className="fas fa-times-circle me-1"), "Calibration failed"],
            color="danger",
            pill=True,
            title=str(message),
        )
        return (False, None, "", "", [], empty_fig, "", "", "", badge, no_update)

    if triggered_id == f'{COMMODITY_LOWER}-calibrate-all-btn':
        row_idx = selected_rows[0] if selected_rows else 0
        if row_idx >= len(params_df):
            raise PreventUpdate

        expiry = params_df.iloc[row_idx]['expiry']
        exp_data = select_expiry_rows(market_data, expiry)
        surface_slice = select_surface_slice(surface, expiry)
        current_params = {
            name: float(params_df.iloc[row_idx].get(name, 0.0))
            for name in ADJUSTMENT_PARAMS
        }
        try:
            if not operational_payload or (
                operational_payload.get('requested_cob')
                != operational_payload.get('actual_cob')
            ):
                raise BrentAdjustmentError(
                    'An exact-COB official Brent SVI baseline is required.'
                )
            result = calibrate_adjustment(
                exp_data,
                surface_slice,
                expiry=expiry,
                full_surface=surface,
                cob_date=trade_date_str,
            )
            candidate_params = result['params']
            candidate_rmse = result['rmse']
            prepared, _ = prepare_adjustment_fit(exp_data, surface_slice)
            current_result = evaluate_adjustment(current_params, prepared)
            current_rmse = current_result['rmse']
        except (BrentAdjustmentError, TypeError, ValueError) as exc:
            return failure_outputs(exc)

        forward = float(prepared['forward'].iloc[0])

        comparison_data = {
            'expiry': str(expiry), 'forward': forward, 'current_params': current_params,
            'candidate_params': candidate_params, 'final_params': current_params.copy(),
            'current_rmse': current_rmse, 'candidate_rmse': candidate_rmse, 'row_idx': row_idx,
            'validation': result['validation'],
            'shrink_factor': result['shrink_factor'],
        }
        comparison_table = format_comparison_data(
            current_params,
            candidate_params,
            current_params,
            comparison_params=list(ADJUSTMENT_PARAMS),
            param_labels=ADJUSTMENT_LABELS,
        )
        fig = create_brent_adjustment_comparison(
            exp_data,
            surface_slice,
            current_params,
            candidate_params,
            current_params,
            expiry_label=expiry,
            x_axis=x_axis or 'log_moneyness',
        )

        return (True, comparison_data, f"Expiry: {expiry}", f"${forward:.2f}", comparison_table, fig,
                f"{current_rmse*100:.2f}%", f"{candidate_rmse*100:.2f}%", f"{current_rmse*100:.2f}%", no_update, no_update)

    if comparison_store is None:
        raise PreventUpdate

    current_params = comparison_store.get('current_params', {})
    candidate_params = comparison_store.get('candidate_params', {})
    expiry = comparison_store.get('expiry', '')
    forward = comparison_store.get('forward', 75.0)
    row_idx = comparison_store.get('row_idx', 0)

    if triggered_id == f'{COMMODITY_LOWER}-copy-candidate-btn':
        final_params = candidate_params.copy()
    elif triggered_id == f'{COMMODITY_LOWER}-reset-final-btn':
        final_params = current_params.copy()
    else:
        final_params = extract_final_params(comparison_table_data)

    exp_data = select_expiry_rows(market_data, expiry)
    surface_slice = select_surface_slice(surface, expiry)
    validation_error = None
    try:
        if not operational_payload or (
            operational_payload.get('requested_cob')
            != operational_payload.get('actual_cob')
        ):
            raise BrentAdjustmentError(
                'An exact-COB official Brent SVI baseline is required.'
            )
        prepared, nodes = prepare_adjustment_fit(exp_data, surface_slice)
        validation = validate_adjustment(
            final_params,
            nodes,
            forward=float(prepared['forward'].iloc[0]),
            dte=float(prepared['dte'].iloc[0]),
            expiry=expiry,
            full_surface=surface,
            cob_date=trade_date_str,
        )
        if not validation['is_valid']:
            raise BrentAdjustmentError(validation.get('reason') or 'Validation failed')
        final_result = evaluate_adjustment(final_params, prepared)
        final_rmse = final_result['rmse']
    except (BrentAdjustmentError, TypeError, ValueError) as exc:
        validation_error = str(exc)
        final_rmse = np.nan

    comparison_data = comparison_store.copy()
    comparison_data['final_params'] = final_params
    comparison_table = format_comparison_data(
        current_params,
        candidate_params,
        final_params,
        comparison_params=list(ADJUSTMENT_PARAMS),
        param_labels=ADJUSTMENT_LABELS,
    )
    fig = create_brent_adjustment_comparison(
        exp_data,
        surface_slice,
        current_params,
        candidate_params,
        final_params,
        expiry_label=expiry,
        x_axis=x_axis or 'log_moneyness',
    )

    final_rmse_label = f"{final_rmse*100:.2f}%" if np.isfinite(final_rmse) else "Invalid"
    if triggered_id == f'{COMMODITY_LOWER}-comparison-save-btn':
        if validation_error or not np.isfinite(final_rmse):
            badge = dbc.Badge(
                [html.I(className="fas fa-times-circle me-1"), "Draft rejected"],
                color="danger",
                pill=True,
                title=validation_error or 'Final adjustment is invalid.',
            )
            return (
                True, comparison_data, f"Expiry: {expiry}", f"${forward:.2f}",
                comparison_table, fig,
                f"{comparison_store.get('current_rmse', np.nan)*100:.2f}%",
                f"{comparison_store.get('candidate_rmse', np.nan)*100:.2f}%",
                final_rmse_label, badge, no_update,
            )

        if row_idx >= len(table_data):
            raise PreventUpdate
        updated_table_data = [dict(row) for row in table_data]
        for name in ADJUSTMENT_PARAMS:
            updated_table_data[row_idx][name] = float(final_params[name])
        updated_table_data[row_idx]['rmse'] = final_rmse_label
        updated_table_data[row_idx]['validation'] = 'Pass'
        updated_table_data[row_idx]['message'] = (
            'Validated session draft; not published to the database.'
        )
        badge = dbc.Badge(
            [html.I(className="fas fa-check me-1"), "Draft applied · not published"],
            color="success",
            pill=True,
        )
        return (
            False, None, "", "", [], empty_fig, "", "", "", badge,
            updated_table_data,
        )

    return (True, comparison_data, f"Expiry: {expiry}", f"${forward:.2f}", comparison_table, fig,
            f"{comparison_store.get('current_rmse', np.nan)*100:.2f}%", f"{comparison_store.get('candidate_rmse', np.nan)*100:.2f}%", final_rmse_label, no_update, no_update)


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
        params_df.to_excel(writer, sheet_name='Parameters', index=False)

        if market_data_json is not None:
            try:
                market_data = pd.read_json(StringIO(market_data_json), orient='split')
                market_data.to_excel(writer, sheet_name='Market Data', index=False)
            except Exception:
                pass

        summary_data = {
            'Commodity': [COMMODITY],
            'Model Version': [BRENT_ADJUSTMENT_MODEL_VERSION],
            'Baseline': ['Official Brent SVI surface'],
            'Trade Date': [str(trade_date)],
            'Export Date': [str(date.today())],
            'Number of Expiries': [len(table_data)],
        }

        rmse_values = []
        for row in table_data:
            rmse_str = row.get('rmse', '')
            if isinstance(rmse_str, str) and '%' in rmse_str:
                try:
                    rmse_values.append(float(rmse_str.replace('%', '')) / 100)
                except ValueError:
                    pass

        if rmse_values:
            summary_data['Average RMSE'] = [f"{np.mean(rmse_values)*100:.2f}%"]
            summary_data['Max RMSE'] = [f"{np.max(rmse_values)*100:.2f}%"]
            summary_data['Min RMSE'] = [f"{np.min(rmse_values)*100:.2f}%"]

        summary_df = pd.DataFrame(summary_data)
        summary_df.to_excel(writer, sheet_name='Summary', index=False)

    output.seek(0)
    excel_data = output.read()
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
        return True, str(expiry_count)
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
     State(f'{COMMODITY_LOWER}-operational-surface-store', 'data')],
    prevent_initial_call=True
)
def run_batch_calibration(confirm_clicks, close_clicks, market_data_json, table_data,
                          auto_save_opts, skip_good_opts, trade_date_str, is_open,
                          operational_payload=None):
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

    skip_good = 'skip_good' in (skip_good_opts or [])

    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    params_df = parse_brent_adjustment_rows(table_data)
    surface = operational_surface_frame(operational_payload)

    if trade_date_str:
        trade_date = pd.to_datetime(trade_date_str).date()
    else:
        trade_date = date.today()

    results = []
    updated_table_data = table_data.copy()

    success_count = 0
    skip_count = 0
    fail_count = 0

    exact_baseline = bool(
        operational_payload
        and operational_payload.get('requested_cob')
        == operational_payload.get('actual_cob')
    )
    for i, row in params_df.iterrows():
        expiry = row['expiry']
        expiry_str = str(expiry)
        exp_data = select_expiry_rows(market_data, expiry)
        surface_slice = select_surface_slice(surface, expiry)
        old_rmse = None
        try:
            if not exact_baseline:
                raise BrentAdjustmentError(
                    'An exact-COB official Brent SVI baseline is required.'
                )
            prepared, _ = prepare_adjustment_fit(exp_data, surface_slice)
            current_params = {
                name: float(row.get(name, 0.0)) for name in ADJUSTMENT_PARAMS
            }
            old_rmse = evaluate_adjustment(current_params, prepared)['rmse']
            if skip_good and old_rmse < 0.002:
                results.append(
                    format_batch_result_row(
                        expiry_str, 'Skipped', old_rmse, old_rmse
                    )
                )
                skip_count += 1
                continue

            result = calibrate_adjustment(
                exp_data,
                surface_slice,
                expiry=expiry,
                full_surface=surface,
                cob_date=trade_date,
            )
            new_params = result['params']
            new_rmse = result['rmse']
            for param_key, param_val in new_params.items():
                updated_table_data[i][param_key] = param_val
            updated_table_data[i]['rmse'] = f"{new_rmse*100:.2f}%"
            updated_table_data[i]['validation'] = 'Pass'
            updated_table_data[i]['message'] = (
                f"SVI-relative fit; shrink factor {result['shrink_factor']:.3f}"
            )
            results.append(
                format_batch_result_row(
                    expiry_str, 'Success', old_rmse, new_rmse
                )
            )
            success_count += 1
        except (BrentAdjustmentError, TypeError, ValueError):
            results.append(
                format_batch_result_row(expiry_str, 'Failed', old_rmse, None)
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
            [html.I(className="fas fa-check me-1"), f"Adjusted {success_count} expiries"],
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

    return (True, 100, f"Completed: {success_count} adjusted, {skip_count} skipped, {fail_count} failed",
            results_display, False, results, updated_table_data, status_badge)
