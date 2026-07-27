"""
Smile plot grid component.

Implements Framework Section 4.3:
- Grid of 2D plots (3 columns x N rows), one per expiry
- X-Axis Selector: Log-Moneyness | Moneyness | Delta
- Market data points (blue circles) + Model curve (orange line)
- Real-time update when parameters edited
- Click-to-select links plot to parameter table row
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import dash_bootstrap_components as dbc
from dash import html, dcc
from typing import List, Dict, Optional, Literal
from options.calibration_engine.converters.delta import strike_to_delta


# X-axis options
X_AXIS_OPTIONS = [
    {'label': 'Log-Moneyness', 'value': 'log_moneyness'},
    {'label': 'Moneyness (K/F)', 'value': 'moneyness'},
    {'label': 'Delta', 'value': 'delta'},
]


def delta_to_strike_iv(target_delta, forward, dte, wing_params, wing_model_iv_func, is_put=True, tol=1e-6, max_iter=50):
    """
    Solve for strike and IV given a target delta using Newton-Raphson iteration.

    This uses REVERSE-DELTA mapping to guarantee monotonicity in delta space.
    Instead of Strike→IV→Delta (which can be non-monotonic when IV varies with strike),
    we solve Delta→Strike→IV by iteratively finding the strike that produces the target delta.

    Args:
        target_delta: Target delta value (positive, 0-0.5 for OTM options)
        forward: Forward price
        dte: Days to expiration
        wing_params: Wing model parameters dict
        wing_model_iv_func: Wing model IV function
        is_put: True for put wing (delta 0-0.5), False for call wing (delta 0.5-1)
        tol: Convergence tolerance
        max_iter: Maximum iterations

    Returns:
        (strike, iv) tuple, or (None, None) if convergence fails
    """
    # Initial guess based on option type
    if is_put:
        strike = forward * 0.9  # Start below ATM for puts
    else:
        strike = forward * 1.1  # Start above ATM for calls

    option_type = 'put' if is_put else 'call'

    for _ in range(max_iter):
        iv = wing_model_iv_func(strike=np.array([strike]), forward=forward, **wing_params)[0]
        current_delta = strike_to_delta(strike, forward, iv, dte, option_type)

        # For puts, delta is negative; convert to positive for comparison
        if is_put:
            current_delta = -current_delta

        error = current_delta - target_delta

        if abs(error) < tol:
            break

        # Numerical derivative (finite difference)
        dk = strike * 0.001
        iv_up = wing_model_iv_func(strike=np.array([strike + dk]), forward=forward, **wing_params)[0]
        delta_up = strike_to_delta(strike + dk, forward, iv_up, dte, option_type)
        if is_put:
            delta_up = -delta_up

        d_delta_d_strike = (delta_up - current_delta) / dk

        if abs(d_delta_d_strike) < 1e-10:
            break

        # Newton step with damping for stability
        step = -error / d_delta_d_strike
        strike = strike + 0.5 * step

        # Keep strike positive and within reasonable bounds
        strike = max(forward * 0.05, min(forward * 5.0, strike))

    final_iv = wing_model_iv_func(strike=np.array([strike]), forward=forward, **wing_params)[0]
    return strike, final_iv


def create_smile_grid(
    commodity: str,
    num_expiries: int = 6,
    grid_id: Optional[str] = None
) -> html.Div:
    """
    Create the smile plot grid container.

    Parameters
    ----------
    commodity : str
        Commodity code
    num_expiries : int
        Number of expiry plots to create
    grid_id : str, optional
        Custom ID for the grid

    Returns
    -------
    html.Div
        Container with X-axis selector and smile plot grid
    """
    if grid_id is None:
        grid_id = f"{commodity.lower()}-smile-grid"

    return html.Div([
        # X-axis selector row
        dbc.Row([
            dbc.Col([
                dbc.Label("X-Axis:", className="me-2"),
                dbc.RadioItems(
                    id=f"{commodity.lower()}-x-axis-selector",
                    options=X_AXIS_OPTIONS,
                    value='log_moneyness',
                    inline=True,
                    className="d-inline-flex",
                ),
            ], width="auto"),
        ], className="mb-3 align-items-center"),

        # Smile plot grid - height is set dynamically by the figure callback
        dcc.Graph(
            id=grid_id,
            config={
                'displayModeBar': True,
                'displaylogo': False,
                'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
            },
            # Don't set fixed height here - let figure.update_layout(height=...) control it
        ),
    ], className="smile-grid-container")


def create_smile_grid_figure(
    market_data: pd.DataFrame,
    params_df: pd.DataFrame,
    x_axis: Literal['log_moneyness', 'moneyness', 'delta'] = 'log_moneyness',
    selected_row: Optional[int] = None,
    num_cols: int = 3
) -> go.Figure:
    """
    Create the smile plot grid figure.

    Parameters
    ----------
    market_data : DataFrame
        Market data with columns: expiry, delta, iv, strike, forward
    params_df : DataFrame
        Parameters with expiry and Wing model params
    x_axis : str
        X-axis type: 'log_moneyness', 'moneyness', or 'delta'
    selected_row : int, optional
        Index of selected row to highlight
    num_cols : int
        Number of columns in the grid (default: 3)

    Returns
    -------
    go.Figure
        Plotly figure with subplot grid
    """
    # Import wing model
    import sys
    sys.path.insert(0, '/home/efernandez/development/Github')
    from options.calibration_engine.models.wing_model import wing_model_iv

    # Get unique expiries - display all available expiries
    all_expiries = sorted(market_data['expiry'].unique())
    expiries = all_expiries
    num_expiries = len(expiries)
    num_rows = (num_expiries + num_cols - 1) // num_cols

    if num_expiries == 0:
        return go.Figure()

    # Create subplot titles
    subplot_titles = [pd.to_datetime(exp).strftime('%b-%y') for exp in expiries]

    # Fixed height per row in pixels - this ensures consistent subplot size
    ROW_HEIGHT_PX = 250  # Each row gets exactly this many pixels
    SPACING_PX = 60      # Fixed pixel spacing between rows
    MARGIN_TOP_PX = 50
    MARGIN_BOTTOM_PX = 50

    # Calculate total figure height
    total_height = num_rows * ROW_HEIGHT_PX + (num_rows - 1) * SPACING_PX + MARGIN_TOP_PX + MARGIN_BOTTOM_PX

    # Calculate vertical_spacing as fraction of figure height
    # vertical_spacing is the gap between rows as fraction of total plot area
    # Plot area = total_height - margins
    plot_area = total_height - MARGIN_TOP_PX - MARGIN_BOTTOM_PX
    if num_rows > 1:
        # Total spacing needed = (num_rows - 1) * SPACING_PX
        # As fraction of plot area
        vertical_spacing = (SPACING_PX * (num_rows - 1)) / plot_area / (num_rows - 1)
        vertical_spacing = min(vertical_spacing, 1.0 / (num_rows - 1) - 0.01)
    else:
        vertical_spacing = 0.1

    # Create subplots
    fig = make_subplots(
        rows=num_rows,
        cols=num_cols,
        subplot_titles=subplot_titles,
        horizontal_spacing=0.06,
        vertical_spacing=vertical_spacing,
    )

    # X-axis labels based on selection
    x_labels = {
        'log_moneyness': 'Log-Moneyness (x)',
        'moneyness': 'Moneyness (K/F)',
        'delta': 'Delta',
    }

    for idx, expiry in enumerate(expiries):
        row = idx // num_cols + 1
        col = idx % num_cols + 1

        # Filter market data for this expiry
        exp_data = market_data[market_data['expiry'] == expiry].copy()

        if exp_data.empty:
            continue

        forward = exp_data['forward'].iloc[0]

        # Calculate x values based on selection
        if x_axis == 'log_moneyness':
            exp_data['x'] = np.log(exp_data['strike'] / forward)
            # Dynamic x_range based on actual data with padding
            data_min, data_max = exp_data['x'].min(), exp_data['x'].max()
            padding = (data_max - data_min) * 0.1 if data_max > data_min else 0.1
            x_range = [min(data_min - padding, -0.5), max(data_max + padding, 0.5)]
        elif x_axis == 'moneyness':
            exp_data['x'] = exp_data['strike'] / forward
            # Dynamic x_range based on actual data with padding
            data_min, data_max = exp_data['x'].min(), exp_data['x'].max()
            padding = (data_max - data_min) * 0.1 if data_max > data_min else 0.1
            x_range = [min(data_min - padding, 0.7), max(data_max + padding, 1.3)]
        else:  # delta
            # Convert to standard delta display: 0 (OTM put) → 0.5 (ATM) → 1 (OTM call)
            # Puts (delta < 0): x = -delta (e.g., -0.25 → 0.25)
            # Calls (delta > 0): x = 1 - delta (e.g., 0.25 → 0.75)
            exp_data['x'] = exp_data['delta'].apply(
                lambda d: -d if d < 0 else 1 - d
            )
            x_range = [0, 1]

        # Sort by x for proper display ordering
        exp_data = exp_data.sort_values('x')

        # Get params for this expiry
        exp_params = params_df[params_df['expiry'] == expiry]
        if exp_params.empty:
            # Try matching by formatted expiry
            exp_str = pd.to_datetime(expiry).strftime('%b-%y')
            exp_params = params_df[params_df['expiry'] == exp_str]

        # Determine if this plot should be highlighted
        is_selected = selected_row is not None and idx == selected_row

        # Add market data points (blue circles)
        fig.add_trace(
            go.Scatter(
                x=exp_data['x'],
                y=exp_data['iv'] * 100,  # Convert to percentage
                mode='markers',
                marker=dict(
                    size=8,
                    color='#007bff',
                    line=dict(width=1, color='white'),
                ),
                name='Market',
                showlegend=(idx == 0),
                hovertemplate=(
                    f"<b>{subplot_titles[idx]}</b><br>"
                    f"X: %{{x:.3f}}<br>"
                    f"IV: %{{y:.2f}}%<br>"
                    "<extra></extra>"
                ),
            ),
            row=row, col=col
        )

        # Add model curve if params available
        if not exp_params.empty:
            params = exp_params.iloc[0].to_dict()

            # Generate model curve
            if x_axis == 'log_moneyness':
                x_model = np.linspace(x_range[0], x_range[1], 100)
                strikes_model = forward * np.exp(x_model)
            elif x_axis == 'moneyness':
                x_model = np.linspace(x_range[0], x_range[1], 100)
                strikes_model = forward * x_model
            else:  # delta
                # For delta axis, we need to generate put and call wings separately
                # to avoid artifacts from mixing put/call delta conversions
                pass  # strikes_model will be set in the delta-specific block below

            # Calculate model IVs
            wing_params = {k: params.get(k, 0) for k in ['vr', 'sr', 'pc', 'cc', 'dc', 'uc', 'dsm', 'usm', 'vcr', 'scr', 'ssr', 'put_wing_power', 'call_wing_power']}

            # Use reasonable defaults if missing
            if wing_params['ssr'] == 0:
                wing_params['ssr'] = 1.0
            if wing_params.get('put_wing_power', 0) == 0:
                wing_params['put_wing_power'] = 0.5
            if wing_params.get('call_wing_power', 0) == 0:
                wing_params['call_wing_power'] = 0.5

            try:
                if x_axis == 'delta':
                    # Use REVERSE-DELTA mapping to guarantee monotonicity
                    # Instead of Strike→IV→Delta (non-monotonic), we use Delta→Strike→IV
                    dte = exp_data['dte'].iloc[0]

                    # PUT wing: display x from 0.005 to 0.48 (extended to show extreme OTM puts)
                    x_put_grid = np.linspace(0.005, 0.48, 60)
                    iv_put = []
                    x_put = []
                    for d in x_put_grid:
                        try:
                            strike, iv = delta_to_strike_iv(d, forward, dte, wing_params, wing_model_iv, is_put=True)
                            iv_put.append(iv)
                            x_put.append(d)
                        except Exception:
                            continue

                    # CALL wing: display x from 0.52 to 0.995 (extended to show extreme OTM calls)
                    x_call_grid = np.linspace(0.52, 0.995, 60)
                    iv_call = []
                    x_call = []
                    for display_x in x_call_grid:
                        call_delta = 1 - display_x  # Convert display x back to call delta
                        try:
                            strike, iv = delta_to_strike_iv(call_delta, forward, dte, wing_params, wing_model_iv, is_put=False)
                            iv_call.append(iv)
                            x_call.append(display_x)
                        except Exception:
                            continue

                    # Combine (already monotonic by construction, no sorting needed)
                    x_model = np.array(x_put + x_call)
                    model_iv = np.array(iv_put + iv_call)
                else:
                    model_iv = wing_model_iv(
                        strike=strikes_model,
                        forward=forward,
                        **wing_params
                    )
                    sort_idx = np.argsort(x_model)
                    x_model = x_model[sort_idx]
                    model_iv = model_iv[sort_idx]

                fig.add_trace(
                    go.Scatter(
                        x=x_model,
                        y=model_iv * 100,  # Convert to percentage
                        mode='lines',
                        line=dict(
                            color='#fd7e14',
                            width=2,
                        ),
                        name='Model',
                        showlegend=(idx == 0),
                    ),
                    row=row, col=col
                )
            except Exception:
                pass

        # Add ATM reference line
        if x_axis == 'log_moneyness':
            fig.add_vline(
                x=0, line=dict(color='gray', dash='dash', width=1),
                row=row, col=col
            )
        elif x_axis == 'delta':
            # ATM is at x = 0.5 in delta display convention
            fig.add_vline(
                x=0.5, line=dict(color='gray', dash='dash', width=1),
                row=row, col=col
            )
        elif x_axis == 'moneyness':
            fig.add_vline(
                x=1.0, line=dict(color='gray', dash='dash', width=1),
                row=row, col=col
            )

        # Update subplot axes
        fig.update_xaxes(
            title_text=x_labels[x_axis] if row == num_rows else None,
            range=x_range,
            row=row, col=col
        )
        fig.update_yaxes(
            title_text='IV (%)' if col == 1 else None,
            row=row, col=col
        )

        # Highlight selected subplot
        if is_selected:
            # Add a border around the selected plot (using shapes)
            pass  # TODO: Implement subplot highlight

    # Update overall layout with calculated height
    fig.update_layout(
        height=total_height,
        margin=dict(t=MARGIN_TOP_PX, b=MARGIN_BOTTOM_PX, l=60, r=20),
        showlegend=True,
        legend=dict(
            orientation='h',
            yanchor='bottom',
            y=1.02,
            xanchor='right',
            x=1
        ),
        paper_bgcolor='white',
        plot_bgcolor='white',
    )

    # Add gridlines to all subplots
    fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='lightgray')
    fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='lightgray')

    return fig


def create_single_smile_plot(
    market_data: pd.DataFrame,
    params: Dict,
    expiry_label: str,
    x_axis: Literal['log_moneyness', 'moneyness', 'delta'] = 'log_moneyness',
    height: int = 300
) -> go.Figure:
    """
    Create a single smile plot for one expiry.

    Parameters
    ----------
    market_data : DataFrame
        Market data for single expiry with columns: delta, iv, strike, forward
    params : dict
        Wing model parameters
    expiry_label : str
        Expiry label for title
    x_axis : str
        X-axis type
    height : int
        Plot height in pixels

    Returns
    -------
    go.Figure
        Single smile plot
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
        # Dynamic x_range based on actual data with padding
        data_min, data_max = market_data['x'].min(), market_data['x'].max()
        padding = (data_max - data_min) * 0.1 if data_max > data_min else 0.1
        x_range = [min(data_min - padding, -0.5), max(data_max + padding, 0.5)]
        x_label = 'Log-Moneyness (x)'
    elif x_axis == 'moneyness':
        market_data['x'] = market_data['strike'] / forward
        # Dynamic x_range based on actual data with padding
        data_min, data_max = market_data['x'].min(), market_data['x'].max()
        padding = (data_max - data_min) * 0.1 if data_max > data_min else 0.1
        x_range = [min(data_min - padding, 0.7), max(data_max + padding, 1.3)]
        x_label = 'Moneyness (K/F)'
    else:
        market_data['x'] = market_data['delta']
        # Dynamic x_range based on actual data with padding
        data_min, data_max = market_data['x'].min(), market_data['x'].max()
        padding = (data_max - data_min) * 0.1 if data_max > data_min else 0.1
        x_range = [min(data_min - padding, -0.5), max(data_max + padding, 0.5)]
        x_label = 'Delta'

    market_data = market_data.sort_values('x')

    # Market data points
    fig.add_trace(
        go.Scatter(
            x=market_data['x'],
            y=market_data['iv'] * 100,
            mode='markers',
            marker=dict(size=10, color='#007bff'),
            name='Market',
        )
    )

    # Model curve
    if params:
        wing_params = {k: params.get(k, 0) for k in ['vr', 'sr', 'pc', 'cc', 'dc', 'uc', 'dsm', 'usm', 'vcr', 'scr', 'ssr', 'put_wing_power', 'call_wing_power']}
        if wing_params['ssr'] == 0:
            wing_params['ssr'] = 1.0
        if wing_params.get('put_wing_power', 0) == 0:
            wing_params['put_wing_power'] = 0.5
        if wing_params.get('call_wing_power', 0) == 0:
            wing_params['call_wing_power'] = 0.5

        if x_axis == 'log_moneyness':
            x_model = np.linspace(x_range[0], x_range[1], 100)
            strikes_model = forward * np.exp(x_model)
        elif x_axis == 'moneyness':
            x_model = np.linspace(x_range[0], x_range[1], 100)
            strikes_model = forward * x_model
        else:
            x_model = market_data['x'].values
            strikes_model = market_data['strike'].values

        try:
            model_iv = wing_model_iv(strike=strikes_model, forward=forward, **wing_params)
            sort_idx = np.argsort(x_model)
            fig.add_trace(
                go.Scatter(
                    x=x_model[sort_idx],
                    y=model_iv[sort_idx] * 100,
                    mode='lines',
                    line=dict(color='#fd7e14', width=2),
                    name='Model',
                )
            )
        except Exception:
            pass

    # ATM reference
    atm_x = 0 if x_axis in ['log_moneyness', 'delta'] else 1.0
    fig.add_vline(x=atm_x, line=dict(color='gray', dash='dash', width=1))

    fig.update_layout(
        title=expiry_label,
        xaxis_title=x_label,
        yaxis_title='IV (%)',
        height=height,
        margin=dict(t=40, b=40, l=50, r=20),
        showlegend=True,
        legend=dict(orientation='h', y=1.1),
    )

    return fig
