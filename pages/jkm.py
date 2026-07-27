"""
JKM (Japan Korea Marker) commodity page.

Asian LNG benchmark with call skew characteristic.
"""
import sys
from pathlib import Path
from datetime import date, timedelta
from io import StringIO, BytesIO

import pandas as pd
import numpy as np
import dash_bootstrap_components as dbc
from dash import html, dcc, callback, Input, Output, State, no_update, ctx
from dash.exceptions import PreventUpdate

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from components.parameter_table import create_parameter_table, format_params_for_table, parse_table_data, update_arb_status_in_row
from components.smile_grid import create_smile_grid, create_smile_grid_figure
from components.comparison_modal import (
    create_comparison_modal,
    create_comparison_plot,
    format_comparison_data,
    extract_final_params
)
from components.data_status import create_data_status_badge, format_data_status
from components.batch_calibration_modal import (
    create_batch_calibration_confirm_modal,
    create_batch_calibration_progress_modal,
    format_batch_result_row,
    create_batch_summary,
    create_batch_results_table,
)

from options.calibration_engine.io.loaders import (
    load_market_data, load_market_data_with_metadata, create_sample_data
)
from options.calibration_engine.calibration import calibrate, evaluate_fit
from options.calibration_engine.config.defaults import get_defaults
from options.calibration_engine.io.storage import (
    ParameterStore,
    get_database_engine,
    load_latest_surface_from_db,
    SOURCE_FULL_OPT,
    SOURCE_MANUAL,
    PARAM_COLUMNS
)

COMMODITY = 'JKM'
COMMODITY_LOWER = COMMODITY.lower()


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
                dbc.Button([html.I(className="fas fa-magic me-1"), "Calibrate"], id=f'{COMMODITY_LOWER}-calibrate-all-btn', color="warning", outline=True, size="sm", title="Calibrate selected expiry"),
                dbc.Button([html.I(className="fas fa-layer-group me-1"), "Calibrate All Expiries"], id=f'{COMMODITY_LOWER}-batch-calibrate-btn', color="warning", size="sm", title="Calibrate all expiries at once"),
                dbc.Button([html.I(className="fas fa-save me-1"), "Save All"], id=f'{COMMODITY_LOWER}-save-all-btn', color="primary", outline=True, size="sm"),
                dbc.Button([html.I(className="fas fa-file-excel me-1"), "Export"], id=f'{COMMODITY_LOWER}-export-btn', color="info", outline=True, size="sm"),
            ]),
        ], width="auto", className="ms-auto"),
    ], className="mb-4 align-items-center")


layout = dbc.Container([
    create_header(),
    dcc.Store(id=f'{COMMODITY_LOWER}-market-data-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-params-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-comparison-data-store'),
    dcc.Store(id=f'{COMMODITY_LOWER}-batch-results-store'),
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
                dbc.CardBody([create_smile_grid(COMMODITY)], className="p-2"),
            ]),
        ], width=12),
    ]),
    create_comparison_modal(COMMODITY),
    create_batch_calibration_confirm_modal(COMMODITY),
    create_batch_calibration_progress_modal(COMMODITY),
    dcc.Loading(id=f'{COMMODITY_LOWER}-loading', type='circle', children=html.Div(id=f'{COMMODITY_LOWER}-loading-output')),
    dcc.Download(id=f'{COMMODITY_LOWER}-download-excel'),
], fluid=True)


@callback(
    [Output(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     Output(f'{COMMODITY_LOWER}-params-store', 'data'),
     Output(f'{COMMODITY_LOWER}-data-status', 'children'),
     Output(f'{COMMODITY_LOWER}-data-status-tooltip', 'children')],
    [Input(f'{COMMODITY_LOWER}-date-picker', 'date'),
     Input(f'{COMMODITY_LOWER}-reload-btn', 'n_clicks')],
    prevent_initial_call=False
)
def load_data(trade_date, reload_clicks):
    """Load market data and parameters."""
    if trade_date is None:
        trade_date = get_default_date()
    else:
        trade_date = pd.to_datetime(trade_date).date()

    try:
        # Load market data with metadata to track source
        result = load_market_data_with_metadata(COMMODITY, trade_date)
        market_data = result['data']
        data_source = result.get('source', 'unknown')
        is_synthetic = result.get('is_synthetic', True)
        last_update = result.get('last_update')

        # Format the data status badge and tooltip
        badge_component, tooltip_text = format_data_status(
            data_source=data_source,
            is_synthetic=is_synthetic,
            last_update=last_update,
            trade_date=trade_date,
            commodity=COMMODITY
        )

        # Try to load historical params from database (T-1)
        historical_params = None
        try:
            engine = get_database_engine()
            if engine is not None:
                historical_params = load_latest_surface_from_db(engine, COMMODITY, trade_date)
        except Exception:
            historical_params = None

        defaults = get_defaults(COMMODITY)
        expiries = market_data['expiry'].unique()
        params_list = []
        loaded_from_db = False

        for expiry in sorted(expiries):
            exp_data = market_data[market_data['expiry'] == expiry]
            forward = exp_data['forward'].iloc[0] if 'forward' in exp_data.columns else 14.0
            expiry_date = pd.to_datetime(expiry).date()

            # Check if we have historical params for this expiry
            params_to_use = defaults.copy()
            if historical_params is not None and not historical_params.empty:
                matching = historical_params[historical_params['expiry'] == expiry_date]
                if not matching.empty:
                    loaded_from_db = True
                    row = matching.iloc[0]
                    for col in PARAM_COLUMNS:
                        if col in row and pd.notna(row[col]):
                            params_to_use[col] = row[col]

            try:
                eval_result = evaluate_fit(params=params_to_use, market_data=exp_data, forward=forward)
                rmse = eval_result['rmse']
            except Exception:
                rmse = 0.0
            params_list.append({'expiry': expiry, **params_to_use, 'rmse': rmse})

        params_df = pd.DataFrame(params_list)

        # Append historical params info to tooltip if applicable
        if loaded_from_db:
            tooltip_text += " | Historical params loaded"

        return market_data.to_json(date_format='iso', orient='split'), params_df.to_json(date_format='iso', orient='split'), badge_component, tooltip_text

    except Exception as e:
        sample_data = create_sample_data(COMMODITY, trade_date, forward=14.0)
        defaults = get_defaults(COMMODITY)
        params_list = [{'expiry': exp, **defaults, 'rmse': 0.0} for exp in sorted(sample_data['expiry'].unique())]
        params_df = pd.DataFrame(params_list)

        # Create synthetic data badge
        badge_component, tooltip_text = format_data_status(
            data_source='synthetic',
            is_synthetic=True,
            last_update=None,
            trade_date=trade_date,
            commodity=COMMODITY
        )
        tooltip_text += f" | Error: {str(e)[:50]}"

        return sample_data.to_json(date_format='iso', orient='split'), params_df.to_json(date_format='iso', orient='split'), badge_component, tooltip_text


@callback(
    Output(f'{COMMODITY_LOWER}-param-table', 'data'),
    Input(f'{COMMODITY_LOWER}-params-store', 'data'),
    State(f'{COMMODITY_LOWER}-market-data-store', 'data'),
    prevent_initial_call=True
)
def update_param_table(params_json, market_data_json):
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

    return format_params_for_table(params_df, market_data)


@callback(
    Output(f'{COMMODITY_LOWER}-smile-grid', 'figure'),
    [Input(f'{COMMODITY_LOWER}-market-data-store', 'data'),
     Input(f'{COMMODITY_LOWER}-param-table', 'data'),
     Input(f'{COMMODITY_LOWER}-x-axis-selector', 'value'),
     Input(f'{COMMODITY_LOWER}-param-table', 'selected_rows')],
    prevent_initial_call=True
)
def update_smile_grid(market_data_json, table_data, x_axis, selected_rows):
    if market_data_json is None or table_data is None:
        raise PreventUpdate
    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    params_df = parse_table_data(table_data)
    selected_row = selected_rows[0] if selected_rows else None
    return create_smile_grid_figure(market_data, params_df, x_axis or 'log_moneyness', selected_row)


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
def handle_calibration(calibrate_clicks, cancel_clicks, save_clicks, copy_clicks, reset_clicks, comparison_table_data,
                       market_data_json, table_data, selected_rows, is_open, comparison_store, x_axis,
                       trade_date_str, current_status_badge):
    triggered_id = ctx.triggered_id
    empty_fig = {}
    default_outputs = (False, None, "", "", [], empty_fig, "", "", "", no_update, no_update)

    if triggered_id == f'{COMMODITY_LOWER}-comparison-cancel-btn':
        return default_outputs
    if triggered_id == f'{COMMODITY_LOWER}-comparison-save-btn':
        # Save final params to database
        save_success = False
        save_error = None

        if comparison_store is not None:
            try:
                engine = get_database_engine()
                if engine is not None:
                    final_params = comparison_store.get('final_params', {})
                    forward = comparison_store.get('forward', 14.0)
                    row_idx = comparison_store.get('row_idx', 0)

                    # Parse trade_date from date picker (use selected date, not today)
                    if trade_date_str:
                        trade_date = pd.to_datetime(trade_date_str).date()
                    else:
                        trade_date = date.today()

                    # Get actual expiry from market data
                    if market_data_json:
                        market_data = pd.read_json(StringIO(market_data_json), orient='split')
                        expiry_dates = sorted(market_data['expiry'].unique())
                        if row_idx < len(expiry_dates):
                            expiry_date = pd.to_datetime(expiry_dates[row_idx]).date()
                        else:
                            expiry_date = None
                            save_error = "Invalid expiry index"
                    else:
                        expiry_date = None
                        save_error = "No market data"

                    if expiry_date is not None:
                        # Recalculate RMSE for final params (not candidate)
                        exp_data = market_data[market_data['expiry'] == market_data['expiry'].unique()[row_idx]]
                        try:
                            final_result = evaluate_fit(final_params, exp_data, forward)
                            final_rmse = final_result['rmse']
                        except Exception:
                            final_rmse = comparison_store.get('candidate_rmse', 0.0)

                        candidate_params = comparison_store.get('candidate_params', {})
                        source = SOURCE_FULL_OPT if final_params == candidate_params else SOURCE_MANUAL

                        store = ParameterStore(db_engine=engine)
                        store.save(
                            commodity=COMMODITY,
                            expiry=expiry_date,
                            params=final_params,
                            calibration_date=trade_date,
                            fit_error=final_rmse,
                            arbitrage_valid=True,
                            trade_date=trade_date,
                            forward=forward,
                            source=source,
                            user_id='dashboard',
                            overwrite=True
                        )
                        save_success = True
                else:
                    save_error = "Database not configured"
            except Exception as e:
                save_error = str(e)
                print(f"Error saving parameters: {e}")

        # Create status badge based on save result and update table if successful
        updated_table_data = no_update
        if save_success:
            status_badge = dbc.Badge(
                [html.I(className="fas fa-check me-1"), "Saved"],
                color="success",
                className="d-flex align-items-center",
                pill=True,
            )
            # Update the table data with saved final params
            if table_data and comparison_store:
                row_idx = comparison_store.get('row_idx', 0)
                final_params = comparison_store.get('final_params', {})
                if row_idx < len(table_data):
                    updated_table_data = table_data.copy()
                    # Update the row with final params
                    for param_key, param_val in final_params.items():
                        if param_key in updated_table_data[row_idx]:
                            updated_table_data[row_idx][param_key] = param_val
                    # Update RMSE display
                    updated_table_data[row_idx]['rmse'] = f"{final_rmse*100:.2f}%"
        elif save_error:
            status_badge = dbc.Badge(
                [html.I(className="fas fa-times me-1"), f"Save failed"],
                color="danger",
                className="d-flex align-items-center",
                pill=True,
            )
        else:
            status_badge = current_status_badge

        return (False, None, "", "", [], empty_fig, "", "", "", status_badge, updated_table_data)
    if market_data_json is None or table_data is None:
        raise PreventUpdate

    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    params_df = parse_table_data(table_data)

    if triggered_id == f'{COMMODITY_LOWER}-calibrate-all-btn':
        row_idx = selected_rows[0] if selected_rows else 0
        if row_idx >= len(params_df):
            raise PreventUpdate

        expiry = params_df.iloc[row_idx]['expiry']
        exp_data = market_data[market_data['expiry'] == market_data['expiry'].unique()[row_idx]]
        forward = exp_data['forward'].iloc[0] if not exp_data.empty else 14.0
        current_params = {k: v for k, v in params_df.iloc[row_idx].to_dict().items() if k not in ['expiry', 'rmse', 'arb_status']}

        try:
            result = calibrate(market_data=exp_data, forward=forward, initial_params=current_params, commodity=COMMODITY)
            candidate_params = result['params']
            candidate_rmse = result['rmse']
        except Exception:
            candidate_params = current_params.copy()
            candidate_rmse = 0.0

        try:
            current_result = evaluate_fit(current_params, exp_data, forward)
            current_rmse = current_result['rmse']
        except Exception:
            current_rmse = 0.0

        comparison_data = {
            'expiry': str(expiry), 'forward': forward, 'current_params': current_params,
            'candidate_params': candidate_params, 'final_params': current_params.copy(),
            'current_rmse': current_rmse, 'candidate_rmse': candidate_rmse, 'row_idx': row_idx,
        }
        comparison_table = format_comparison_data(current_params, candidate_params, current_params)
        fig = create_comparison_plot(exp_data, current_params, candidate_params, current_params, expiry_label=expiry, x_axis=x_axis or 'log_moneyness')

        return (True, comparison_data, f"Expiry: {expiry}", f"${forward:.2f}", comparison_table, fig,
                f"{current_rmse*100:.2f}%", f"{candidate_rmse*100:.2f}%", f"{current_rmse*100:.2f}%", no_update, no_update)

    if comparison_store is None:
        raise PreventUpdate

    current_params = comparison_store.get('current_params', {})
    candidate_params = comparison_store.get('candidate_params', {})
    expiry = comparison_store.get('expiry', '')
    forward = comparison_store.get('forward', 14.0)
    row_idx = comparison_store.get('row_idx', 0)

    if triggered_id == f'{COMMODITY_LOWER}-copy-candidate-btn':
        final_params = candidate_params.copy()
    elif triggered_id == f'{COMMODITY_LOWER}-reset-final-btn':
        final_params = current_params.copy()
    else:
        final_params = extract_final_params(comparison_table_data)

    exp_data = market_data[market_data['expiry'] == market_data['expiry'].unique()[row_idx]]

    try:
        final_result = evaluate_fit(final_params, exp_data, forward)
        final_rmse = final_result['rmse']
    except Exception:
        final_rmse = 0.0

    comparison_data = comparison_store.copy()
    comparison_data['final_params'] = final_params
    comparison_table = format_comparison_data(current_params, candidate_params, final_params)
    fig = create_comparison_plot(exp_data, current_params, candidate_params, final_params, expiry_label=expiry, x_axis=x_axis or 'log_moneyness')

    return (True, comparison_data, f"Expiry: {expiry}", f"${forward:.2f}", comparison_table, fig,
            f"{comparison_store.get('current_rmse', 0)*100:.2f}%", f"{comparison_store.get('candidate_rmse', 0)*100:.2f}%", f"{final_rmse*100:.2f}%", no_update, no_update)


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
     State(f'{COMMODITY_LOWER}-batch-progress-modal', 'is_open')],
    prevent_initial_call=True
)
def run_batch_calibration(confirm_clicks, close_clicks, market_data_json, table_data,
                          auto_save_opts, skip_good_opts, trade_date_str, is_open):
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

    auto_save = 'auto_save' in (auto_save_opts or [])
    skip_good = 'skip_good' in (skip_good_opts or [])

    market_data = pd.read_json(StringIO(market_data_json), orient='split')
    params_df = parse_table_data(table_data)

    if trade_date_str:
        trade_date = pd.to_datetime(trade_date_str).date()
    else:
        trade_date = date.today()

    # Get database engine if auto-save is enabled
    engine = None
    store = None
    if auto_save:
        try:
            engine = get_database_engine()
            if engine is not None:
                store = ParameterStore(db_engine=engine)
        except Exception:
            pass

    expiries = sorted(market_data['expiry'].unique())
    total = len(expiries)
    results = []
    updated_table_data = table_data.copy()

    success_count = 0
    skip_count = 0
    fail_count = 0

    for i, expiry in enumerate(expiries):
        exp_data = market_data[market_data['expiry'] == expiry]
        forward = exp_data['forward'].iloc[0] if not exp_data.empty else 14.0
        expiry_str = pd.to_datetime(expiry).strftime('%Y-%m-%d')
        expiry_date = pd.to_datetime(expiry).date()

        # Get current params
        if i < len(params_df):
            current_params = {k: v for k, v in params_df.iloc[i].to_dict().items()
                            if k not in ['expiry', 'rmse', 'arb_status']}

            # Get current RMSE
            try:
                current_result = evaluate_fit(current_params, exp_data, forward)
                old_rmse = current_result['rmse']
            except Exception:
                old_rmse = 0.0

            # Skip if already well calibrated
            if skip_good and old_rmse < 0.01:
                results.append(format_batch_result_row(expiry_str, 'Skipped', old_rmse, old_rmse))
                skip_count += 1
                continue

            # Run calibration
            try:
                result = calibrate(market_data=exp_data, forward=forward,
                                 initial_params=current_params, commodity=COMMODITY)
                new_params = result['params']
                new_rmse = result['rmse']

                # Update table data
                if i < len(updated_table_data):
                    for param_key, param_val in new_params.items():
                        if param_key in updated_table_data[i]:
                            updated_table_data[i][param_key] = param_val
                    updated_table_data[i]['rmse'] = f"{new_rmse*100:.2f}%"
                    # Recalculate arbitrage status with new params
                    updated_table_data[i]['arb_status'] = update_arb_status_in_row(
                        updated_table_data[i], forward=forward
                    )

                # Auto-save if enabled
                if auto_save and store is not None:
                    try:
                        store.save(
                            commodity=COMMODITY,
                            expiry=expiry_date,
                            params=new_params,
                            calibration_date=trade_date,
                            fit_error=new_rmse,
                            arbitrage_valid=True,
                            trade_date=trade_date,
                            forward=forward,
                            source=SOURCE_FULL_OPT,
                            user_id='dashboard_batch',
                            overwrite=True
                        )
                    except Exception:
                        pass

                results.append(format_batch_result_row(expiry_str, 'Success', old_rmse, new_rmse))
                success_count += 1

            except Exception as e:
                results.append(format_batch_result_row(expiry_str, 'Failed', old_rmse, None))
                fail_count += 1
        else:
            results.append(format_batch_result_row(expiry_str, 'Failed', None, None))
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

    return (True, 100, f"Completed: {success_count} calibrated, {skip_count} skipped, {fail_count} failed",
            results_display, False, results, updated_table_data, status_badge)
