"""
Three-way comparison modal component.

Implements Framework Section 5.2 Step 3:
- Three-column comparison: Current (read-only) | Candidate (read-only) | Final (editable)
- Overlay smile chart: gray dashed (current), blue dashed (candidate), green solid (final)
- Final RMSE updates in real-time as user edits
- Quick actions: "Copy Candidate to Final", "Reset Final to Current"
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import dash_bootstrap_components as dbc
from dash import html, dcc, dash_table
from typing import Dict, Optional, List
from options.calibration_engine.converters.delta import strike_to_delta


# Parameter columns for comparison table
COMPARISON_PARAMS = ['vr', 'sr', 'pc', 'cc', 'dc', 'uc', 'dsm', 'usm', 'vcr', 'scr', 'ssr', 'put_wing_power', 'call_wing_power']


def create_comparison_modal(commodity: str) -> dbc.Modal:
    """
    Create the three-way comparison modal.

    Parameters
    ----------
    commodity : str
        Commodity code

    Returns
    -------
    dbc.Modal
        Modal component for calibration comparison
    """
    modal_id = f"{commodity.lower()}-comparison-modal"

    modal = dbc.Modal(
        [
            dbc.ModalHeader(
                dbc.ModalTitle("Calibration Comparison"),
                close_button=True,
            ),
            dbc.ModalBody([
                # Expiry info row
                dbc.Row([
                    dbc.Col([
                        html.H5(id=f"{commodity.lower()}-comparison-expiry", className="mb-0"),
                    ], width=6),
                    dbc.Col([
                        html.Div([
                            html.Span("Forward: ", className="text-muted"),
                            html.Span(id=f"{commodity.lower()}-comparison-forward", className="fw-bold"),
                        ]),
                    ], width=6, className="text-end"),
                ], className="mb-3"),

                # Three-column parameter comparison table
                html.H6("Parameter Comparison", className="mb-2"),
                create_comparison_table(commodity),

                # RMSE comparison row
                dbc.Row([
                    dbc.Col([
                        dbc.Card([
                            dbc.CardBody([
                                html.P("Current RMSE", className="text-muted mb-1 small"),
                                html.H4(id=f"{commodity.lower()}-current-rmse", className="mb-0 text-secondary"),
                            ], className="text-center py-2"),
                        ], className="h-100"),
                    ], width=4),
                    dbc.Col([
                        dbc.Card([
                            dbc.CardBody([
                                html.P("Candidate RMSE", className="text-muted mb-1 small"),
                                html.H4(id=f"{commodity.lower()}-candidate-rmse", className="mb-0 text-primary"),
                            ], className="text-center py-2"),
                        ], className="h-100"),
                    ], width=4),
                    dbc.Col([
                        dbc.Card([
                            dbc.CardBody([
                                html.P("Final RMSE", className="text-muted mb-1 small"),
                                html.H4(id=f"{commodity.lower()}-final-rmse", className="mb-0 text-success"),
                            ], className="text-center py-2"),
                        ], className="h-100 border-success"),
                    ], width=4),
                ], className="mb-3"),

                # Overlay smile plot
                html.H6("Smile Comparison", className="mb-2"),
                dcc.Graph(
                    id=f"{commodity.lower()}-comparison-plot",
                    config={'displayModeBar': False},
                    style={'height': '350px'},
                ),

                # Quick action buttons
                dbc.Row([
                    dbc.Col([
                        dbc.Button(
                            "Copy Candidate to Final",
                            id=f"{commodity.lower()}-copy-candidate-btn",
                            color="primary",
                            outline=True,
                            className="me-2",
                        ),
                        dbc.Button(
                            "Reset Final to Current",
                            id=f"{commodity.lower()}-reset-final-btn",
                            color="secondary",
                            outline=True,
                        ),
                    ], width="auto"),
                ], className="mt-3"),
            ]),
            dbc.ModalFooter([
                dbc.Button(
                    "Cancel",
                    id=f"{commodity.lower()}-comparison-cancel-btn",
                    color="secondary",
                    className="me-2",
                ),
                dbc.Button(
                    "Save Final",
                    id=f"{commodity.lower()}-comparison-save-btn",
                    color="success",
                ),
            ]),
        ],
        id=modal_id,
        size="xl",
        is_open=False,
        backdrop="static",
    )

    return modal


def create_comparison_table(commodity: str) -> html.Div:
    """
    Create the three-column comparison table.

    Parameters
    ----------
    commodity : str
        Commodity code

    Returns
    -------
    html.Div
        Container with comparison table
    """
    # Create column definitions
    columns = [
        {'id': 'param', 'name': 'Parameter', 'editable': False},
        {'id': 'current', 'name': 'Current', 'editable': False, 'type': 'numeric'},
        {'id': 'candidate', 'name': 'Candidate', 'editable': False, 'type': 'numeric'},
        {'id': 'final', 'name': 'Final', 'editable': True, 'type': 'numeric'},
    ]

    # Create initial data structure
    data = [{'param': p, 'current': 0, 'candidate': 0, 'final': 0} for p in COMPARISON_PARAMS]

    table = dash_table.DataTable(
        id=f"{commodity.lower()}-comparison-table",
        columns=[
            {
                'id': c['id'],
                'name': c['name'],
                'type': c.get('type', 'text'),
                'editable': c['editable'],
                'format': {'specifier': '.4f'} if c.get('type') == 'numeric' else None,
            }
            for c in columns
        ],
        data=data,
        style_table={'overflowX': 'auto'},
        style_header={
            'backgroundColor': '#f8f9fa',
            'fontWeight': 'bold',
            'textAlign': 'center',
            'padding': '8px',
        },
        style_cell={
            'textAlign': 'center',
            'padding': '8px',
            'minWidth': '80px',
        },
        style_cell_conditional=[
            {'if': {'column_id': 'param'}, 'textAlign': 'left', 'fontWeight': 'bold'},
            {'if': {'column_id': 'current'}, 'backgroundColor': '#f5f5f5', 'color': '#6c757d'},
            {'if': {'column_id': 'candidate'}, 'backgroundColor': '#e7f1ff', 'color': '#0d6efd'},
            {'if': {'column_id': 'final'}, 'backgroundColor': '#d1e7dd', 'color': '#0f5132'},
        ],
        style_data_conditional=[
            {
                'if': {'column_editable': True},
                'backgroundColor': '#d1e7dd',
            },
        ],
    )

    return html.Div([table], className="mb-3")


def create_comparison_plot(
    market_data: pd.DataFrame,
    current_params: Dict,
    candidate_params: Dict,
    final_params: Dict,
    expiry_label: str,
    x_axis: str = 'log_moneyness'
) -> go.Figure:
    """
    Create the overlay comparison plot.

    Parameters
    ----------
    market_data : DataFrame
        Market data for single expiry
    current_params : dict
        Current (yesterday's) parameters
    candidate_params : dict
        Candidate (optimizer) parameters
    final_params : dict
        Final (user-editable) parameters
    expiry_label : str
        Expiry label for title
    x_axis : str
        X-axis type

    Returns
    -------
    go.Figure
        Overlay comparison plot
    """
    import sys
    sys.path.insert(0, '/home/efernandez/development/Github')
    from options.calibration_engine.models.wing_model import wing_model_iv

    fig = go.Figure()

    if market_data.empty:
        return fig

    forward = market_data['forward'].iloc[0]
    market_data = market_data.copy()

    # Calculate x values
    if x_axis == 'log_moneyness':
        market_data['x'] = np.log(market_data['strike'] / forward)
        x_range = [-0.5, 0.5]
        x_label = 'Log-Moneyness (x)'
    elif x_axis == 'moneyness':
        market_data['x'] = market_data['strike'] / forward
        x_range = [0.7, 1.3]
        x_label = 'Moneyness (K/F)'
    else:  # delta
        # Convert to standard delta display: 0 (OTM put) → 0.5 (ATM) → 1 (OTM call)
        # Puts (delta < 0): x = -delta (e.g., -0.25 → 0.25)
        # Calls (delta > 0): x = 1 - delta (e.g., 0.25 → 0.75)
        market_data['x'] = market_data['delta'].apply(
            lambda d: -d if d < 0 else 1 - d
        )
        x_range = [0, 1]
        x_label = 'Delta'

    # Sort by x for proper display ordering
    market_data = market_data.sort_values('x')

    # Market data points
    fig.add_trace(
        go.Scatter(
            x=market_data['x'],
            y=market_data['iv'] * 100,
            mode='markers',
            marker=dict(size=10, color='#6c757d', symbol='circle-open', line=dict(width=2)),
            name='Market',
        )
    )

    # Generate model x-grid
    if x_axis == 'log_moneyness':
        x_model = np.linspace(x_range[0], x_range[1], 100)
        strikes_model = forward * np.exp(x_model)
    elif x_axis == 'moneyness':
        x_model = np.linspace(x_range[0], x_range[1], 100)
        strikes_model = forward * x_model
    else:  # delta
        # For delta axis, we generate put/call wings separately in the helper
        x_model = None
        strikes_model = None

    # Helper to get wing params
    def get_wing_params(params):
        wp = {k: params.get(k, 0) for k in ['vr', 'sr', 'pc', 'cc', 'dc', 'uc', 'dsm', 'usm', 'vcr', 'scr', 'ssr', 'put_wing_power', 'call_wing_power']}
        if wp['ssr'] == 0:
            wp['ssr'] = 1.0
        if wp.get('put_wing_power', 0) == 0:
            wp['put_wing_power'] = 0.5
        if wp.get('call_wing_power', 0) == 0:
            wp['call_wing_power'] = 0.5
        return wp

    # Helper to solve for strike given target delta (reverse-delta mapping)
    def delta_to_strike_iv(target_delta, wing_params, is_put=True, tol=1e-6, max_iter=50):
        """
        Solve for strike and IV given a target delta using Newton-Raphson iteration.
        This guarantees monotonicity in delta space.
        """
        dte = market_data['dte'].iloc[0]

        # Initial guess based on option type
        if is_put:
            strike = forward * 0.9
        else:
            strike = forward * 1.1

        option_type = 'put' if is_put else 'call'

        for _ in range(max_iter):
            iv = wing_model_iv(strike=np.array([strike]), forward=forward, **wing_params)[0]
            current_delta = strike_to_delta(strike, forward, iv, dte, option_type)

            if is_put:
                current_delta = -current_delta

            error = current_delta - target_delta

            if abs(error) < tol:
                break

            dk = strike * 0.001
            iv_up = wing_model_iv(strike=np.array([strike + dk]), forward=forward, **wing_params)[0]
            delta_up = strike_to_delta(strike + dk, forward, iv_up, dte, option_type)
            if is_put:
                delta_up = -delta_up

            d_delta_d_strike = (delta_up - current_delta) / dk

            if abs(d_delta_d_strike) < 1e-10:
                break

            step = -error / d_delta_d_strike
            strike = strike + 0.5 * step
            strike = max(forward * 0.05, min(forward * 5.0, strike))

        final_iv = wing_model_iv(strike=np.array([strike]), forward=forward, **wing_params)[0]
        return strike, final_iv

    # Helper to compute model curve for delta axis using reverse-delta mapping
    def get_delta_model_curve(wing_params):
        """Generate model curve using reverse-delta mapping for guaranteed monotonicity."""
        # PUT wing: display x from 0.005 to 0.48 (extended to show extreme OTM puts)
        x_put_grid = np.linspace(0.005, 0.48, 60)
        iv_put = []
        x_put = []
        for d in x_put_grid:
            try:
                strike, iv = delta_to_strike_iv(d, wing_params, is_put=True)
                iv_put.append(iv)
                x_put.append(d)
            except Exception:
                continue

        # CALL wing: display x from 0.52 to 0.995 (extended to show extreme OTM calls)
        x_call_grid = np.linspace(0.52, 0.995, 60)
        iv_call = []
        x_call = []
        for display_x in x_call_grid:
            call_delta = 1 - display_x
            try:
                strike, iv = delta_to_strike_iv(call_delta, wing_params, is_put=False)
                iv_call.append(iv)
                x_call.append(display_x)
            except Exception:
                continue

        # Combine (already monotonic by construction)
        x_vals = np.array(x_put + x_call)
        iv_vals = np.array(iv_put + iv_call)
        return x_vals, iv_vals

    # Helper to get x values for plotting (for non-delta axes)
    def get_plot_x(model_iv_values):
        x_vals = x_model
        sort_idx = np.argsort(x_vals)
        return x_vals[sort_idx], model_iv_values[sort_idx]

    # Current smile (gray dashed)
    if current_params:
        try:
            wp = get_wing_params(current_params)
            if x_axis == 'delta':
                x_plot, iv_plot = get_delta_model_curve(wp)
            else:
                model_iv = wing_model_iv(strike=strikes_model, forward=forward, **wp)
                x_plot, iv_plot = get_plot_x(model_iv)
            fig.add_trace(
                go.Scatter(
                    x=x_plot,
                    y=iv_plot * 100,
                    mode='lines',
                    line=dict(color='#6c757d', width=2, dash='dash'),
                    name='Current',
                )
            )
        except Exception:
            pass

    # Candidate smile (blue dashed)
    if candidate_params:
        try:
            wp = get_wing_params(candidate_params)
            if x_axis == 'delta':
                x_plot, iv_plot = get_delta_model_curve(wp)
            else:
                model_iv = wing_model_iv(strike=strikes_model, forward=forward, **wp)
                x_plot, iv_plot = get_plot_x(model_iv)
            fig.add_trace(
                go.Scatter(
                    x=x_plot,
                    y=iv_plot * 100,
                    mode='lines',
                    line=dict(color='#0d6efd', width=2, dash='dash'),
                    name='Candidate',
                )
            )
        except Exception:
            pass

    # Final smile (green solid)
    if final_params:
        try:
            wp = get_wing_params(final_params)
            if x_axis == 'delta':
                x_plot, iv_plot = get_delta_model_curve(wp)
            else:
                model_iv = wing_model_iv(strike=strikes_model, forward=forward, **wp)
                x_plot, iv_plot = get_plot_x(model_iv)
            fig.add_trace(
                go.Scatter(
                    x=x_plot,
                    y=iv_plot * 100,
                    mode='lines',
                    line=dict(color='#198754', width=3),
                    name='Final',
                )
            )
        except Exception:
            pass

    # ATM reference line
    if x_axis == 'log_moneyness':
        atm_x = 0
    elif x_axis == 'delta':
        atm_x = 0.5  # ATM is at x = 0.5 in delta display convention
    else:  # moneyness
        atm_x = 1.0
    fig.add_vline(x=atm_x, line=dict(color='gray', dash='dot', width=1))

    fig.update_layout(
        title=f"Smile Comparison - {expiry_label}",
        xaxis_title=x_label,
        yaxis_title='IV (%)',
        height=350,
        margin=dict(t=40, b=40, l=50, r=20),
        showlegend=True,
        legend=dict(orientation='h', y=1.1, x=0.5, xanchor='center'),
        paper_bgcolor='white',
        plot_bgcolor='white',
    )

    fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='lightgray')
    fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='lightgray')

    return fig


def format_comparison_data(
    current_params: Dict,
    candidate_params: Dict,
    final_params: Optional[Dict] = None
) -> List[Dict]:
    """
    Format parameters for the comparison table.

    Parameters
    ----------
    current_params : dict
        Current parameters
    candidate_params : dict
        Candidate parameters
    final_params : dict, optional
        Final parameters (defaults to current if not provided)

    Returns
    -------
    list of dict
        Formatted data for DataTable
    """
    if final_params is None:
        final_params = current_params.copy() if current_params else {}

    data = []
    for param in COMPARISON_PARAMS:
        data.append({
            'param': param,
            'current': current_params.get(param, 0),
            'candidate': candidate_params.get(param, 0),
            'final': final_params.get(param, 0),
        })

    return data


def extract_final_params(table_data: List[Dict]) -> Dict:
    """
    Extract final parameters from comparison table data.

    Parameters
    ----------
    table_data : list of dict
        Data from comparison table

    Returns
    -------
    dict
        Final parameters dictionary
    """
    params = {}
    for row in table_data:
        params[row['param']] = row['final']
    return params
