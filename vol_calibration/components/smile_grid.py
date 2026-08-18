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
from typing import Dict, Optional, Literal
from options.calibration_engine.converters.delta import strike_to_delta

from vol_calibration.model_version import DEFAULT_CALIBRATION_MODEL_VERSION


# X-axis options
X_AXIS_OPTIONS = [
    {'label': 'Log-Moneyness', 'value': 'log_moneyness'},
    {'label': 'Moneyness (K/F)', 'value': 'moneyness'},
    {'label': 'Delta', 'value': 'delta'},
]


def delta_to_strike_iv(
    target_delta,
    forward,
    dte,
    wing_params,
    wing_model_iv_func,
    is_put=True,
    tol=1e-6,
    max_iter=80,
    model_version=DEFAULT_CALIBRATION_MODEL_VERSION,
):
    """
    Solve for strike and IV given a target delta using bracketed bisection.

    This uses REVERSE-DELTA mapping to guarantee monotonicity in delta space.
    Instead of Strike→IV→Delta (which can be non-monotonic when IV varies with strike),
    we solve Delta→Strike→IV by iteratively finding the strike that produces the target delta.

    Args:
        target_delta: Target delta value (positive, 0-0.5 for OTM options)
        forward: Forward price
        dte: Days to expiration
        wing_params: Wing model parameters dict
        wing_model_iv_func: Wing model IV function
        is_put: True for put wing; False for the call wing. Targets are
            positive absolute deltas in both cases.
        tol: Convergence tolerance
        max_iter: Maximum iterations

    Returns:
        (strike, iv) tuple, or (None, None) if convergence fails
    """
    _, strikes, ivs = delta_curve_to_strike_iv(
        np.asarray([target_delta], dtype=float),
        forward,
        dte,
        wing_params,
        wing_model_iv_func,
        is_put=is_put,
        tol=tol,
        max_iter=max_iter,
        model_version=model_version,
    )
    if not len(strikes):
        return None, None
    return float(strikes[0]), float(ivs[0])


def delta_curve_to_strike_iv(
    target_deltas,
    forward,
    dte,
    wing_params,
    wing_model_iv_func,
    is_put=True,
    tol=1e-6,
    max_iter=80,
    model_version=DEFAULT_CALIBRATION_MODEL_VERSION,
):
    """Map target deltas to the model with a vectorized bracketed solve.

    Extreme Wing-v2 tails can require strikes well beyond five times the
    forward.  A damped Newton solve with a hard 5F cap therefore returned an
    unconverged point while still drawing it.  Bisection is slower per
    iteration but deterministic, derivative-free, and lets us reject any
    target whose delta residual did not actually converge.
    """
    target_deltas = np.asarray(target_deltas, dtype=float)
    if target_deltas.ndim != 1:
        target_deltas = target_deltas.reshape(-1)
    finite_targets = np.isfinite(target_deltas) & (target_deltas > 0.0) & (
        target_deltas < 1.0
    )
    lower = np.full_like(target_deltas, forward * np.exp(-8.0))
    upper = np.full_like(target_deltas, forward * np.exp(8.0))
    option_type = 'put' if is_put else 'call'

    def delta_at(strikes):
        iv = np.asarray(wing_model_iv_func(
            strike=np.asarray(strikes, dtype=float),
            forward=forward,
            model_version=model_version,
            dte=dte,
            **wing_params,
        ), dtype=float)
        delta = np.asarray(
            strike_to_delta(
                strikes,
                forward,
                iv,
                dte,
                option_type,
            ),
            dtype=float,
        )
        if is_put:
            delta = -delta
        return delta, iv

    lower_delta, _ = delta_at(lower)
    upper_delta, _ = delta_at(upper)
    if is_put:
        bracketed = (lower_delta <= target_deltas) & (
            upper_delta >= target_deltas
        )
    else:
        bracketed = (lower_delta >= target_deltas) & (
            upper_delta <= target_deltas
        )

    active = finite_targets & bracketed
    for _ in range(max_iter):
        strikes = np.sqrt(lower * upper)
        current_delta, _ = delta_at(strikes)
        error = current_delta - target_deltas
        unconverged = active & np.isfinite(error) & (np.abs(error) >= tol)
        if not np.any(unconverged):
            break

        if is_put:
            move_lower = unconverged & (error < 0.0)
        else:
            move_lower = unconverged & (error > 0.0)
        move_upper = unconverged & ~move_lower
        lower[move_lower] = strikes[move_lower]
        upper[move_upper] = strikes[move_upper]

    strikes = np.sqrt(lower * upper)
    final_delta, final_iv = delta_at(strikes)
    residual = np.abs(final_delta - target_deltas)
    valid = (
        active
        & np.isfinite(strikes)
        & np.isfinite(final_iv)
        & np.isfinite(residual)
        & (residual < tol)
    )
    return target_deltas[valid], strikes[valid], np.asarray(final_iv)[valid]


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
                    value='delta',
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


def _normalized_expiries(data: pd.DataFrame, column: str) -> list[pd.Timestamp]:
    if data is None or data.empty or column not in data.columns:
        return []
    values = pd.to_datetime(data[column], errors='coerce').dropna().dt.normalize()
    return sorted(values.drop_duplicates().tolist())


def _surface_delta_x(surface_data: pd.DataFrame) -> pd.Series:
    """Map governed call-delta coordinates to the calibration display axis."""
    delta_abs = pd.to_numeric(surface_data['delta_abs'], errors='coerce')
    put_call = surface_data.get(
        'put_call',
        pd.Series(index=surface_data.index, dtype='object'),
    ).astype(str).str.lower()
    is_put = put_call.eq('put')
    return delta_abs.where(is_put, 1.0 - delta_abs)


def _market_and_surface_are_identical(
    market_data: pd.DataFrame,
    surface_data: pd.DataFrame,
) -> bool:
    """Compare one expiry on the common governed delta/IV coordinates."""
    if market_data.empty or surface_data.empty:
        return False
    market_delta = pd.to_numeric(market_data.get('delta'), errors='coerce')
    market_x = market_delta.where(market_delta < 0, 1.0 - market_delta).abs()
    market_points = pd.DataFrame({
        'x': market_x,
        'iv': pd.to_numeric(market_data.get('iv'), errors='coerce'),
    }).dropna()
    surface_points = pd.DataFrame({
        'x': _surface_delta_x(surface_data),
        'iv': pd.to_numeric(surface_data.get('volatility'), errors='coerce'),
    }).dropna()
    if len(market_points) != len(surface_points) or market_points.empty:
        return False
    market_points = market_points.sort_values('x').reset_index(drop=True)
    surface_points = surface_points.sort_values('x').reset_index(drop=True)
    return bool(
        np.allclose(market_points['x'], surface_points['x'], atol=1e-8, rtol=0)
        and np.allclose(
            market_points['iv'],
            surface_points['iv'],
            atol=1e-8,
            rtol=0,
        )
    )


def create_smile_grid_figure(
    market_data: pd.DataFrame,
    params_df: pd.DataFrame,
    x_axis: Literal['log_moneyness', 'moneyness', 'delta'] = 'log_moneyness',
    selected_row: Optional[int] = None,
    num_cols: int = 3,
    model_version: str = DEFAULT_CALIBRATION_MODEL_VERSION,
    operational_surface: Optional[pd.DataFrame] = None,
    operational_metadata: Optional[dict] = None,
    traded_options: Optional[pd.DataFrame] = None,
    traded_options_metadata: Optional[dict] = None,
    published_surface: Optional[pd.DataFrame] = None,
    published_metadata: Optional[dict] = None,
    manual_trades: Optional[pd.DataFrame] = None,
    manual_trades_metadata: Optional[dict] = None,
    market_metadata: Optional[dict] = None,
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
    operational_surface : DataFrame, optional
        Normalized governed surface rows from the ``/vol_surface`` data path.
        These rows are reference-only and are displayed only on the Delta axis.
    operational_metadata : dict, optional
        Requested/actual COB and source provenance for the reference surface.
    traded_options : DataFrame, optional
        Exact-COB raw ICE TFO rows with positive reported volume.
    traded_options_metadata : dict, optional
        Exact-COB and source provenance for the traded-option rows.

    Returns
    -------
    go.Figure
        Plotly figure with subplot grid
    """
    # Import wing model
    from options.calibration_engine.models.wing_model import wing_model_iv

    market_data = market_data.copy() if market_data is not None else pd.DataFrame()
    params_df = params_df.copy() if params_df is not None else pd.DataFrame()
    operational_surface = (
        operational_surface.copy()
        if operational_surface is not None
        else pd.DataFrame()
    )
    operational_metadata = operational_metadata or {}
    traded_options = (
        traded_options.copy()
        if traded_options is not None
        else pd.DataFrame()
    )
    traded_options_metadata = traded_options_metadata or {}
    published_surface = (
        published_surface.copy()
        if published_surface is not None
        else pd.DataFrame()
    )
    published_metadata = published_metadata or {}
    manual_trades = (
        manual_trades.copy()
        if manual_trades is not None
        else pd.DataFrame()
    )
    manual_trades_metadata = manual_trades_metadata or {}
    market_metadata = market_metadata or {}

    if 'expiry' in market_data.columns:
        market_data['expiry'] = pd.to_datetime(
            market_data['expiry'],
            errors='coerce',
        ).dt.normalize()
    if 'expiry' in params_df.columns:
        parsed_param_expiries = pd.to_datetime(
            params_df['expiry'],
            errors='coerce',
            format='mixed',
        )
        parseable_params = parsed_param_expiries.notna()
        params_df.loc[parseable_params, 'expiry'] = (
            parsed_param_expiries.loc[parseable_params].dt.normalize()
        )
    if 'contract_date' in operational_surface.columns:
        operational_surface['contract_date'] = pd.to_datetime(
            operational_surface['contract_date'],
            errors='coerce',
        ).dt.normalize()
    if 'maturity_date' in traded_options.columns:
        traded_options['maturity_date'] = pd.to_datetime(
            traded_options['maturity_date'],
            errors='coerce',
        ).dt.normalize()
    if 'contract_date' in published_surface.columns:
        published_surface['contract_date'] = pd.to_datetime(
            published_surface['contract_date'],
            errors='coerce',
        ).dt.normalize()
    if 'contract_date' in manual_trades.columns:
        manual_trades['contract_date'] = pd.to_datetime(
            manual_trades['contract_date'],
            errors='coerce',
        ).dt.normalize()
    for column in (
        'strike',
        'call_delta',
        'volatility',
        'forward_value',
        'settlement_price',
        'total_volume',
        'open_interest',
    ):
        if column in traded_options.columns:
            traded_options[column] = pd.to_numeric(
                traded_options[column],
                errors='coerce',
            )
    for frame, columns in (
        (
            published_surface,
            ('strike', 'delta', 'volatility', 'working_forward'),
        ),
        (
            manual_trades,
            ('strike', 'call_delta', 'mark_iv', 'forward', 'volume'),
        ),
    ):
        for column in columns:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors='coerce')

    market_expiries = _normalized_expiries(market_data, 'expiry')
    surface_expiries = (
        _normalized_expiries(operational_surface, 'contract_date')
        if x_axis == 'delta'
        else []
    )
    traded_reference_is_exact = (
        str(traded_options_metadata.get('product', '')).strip().upper()
        == 'TTF'
        and str(
            traded_options_metadata.get('surface_source', '')
        ).strip().upper() == 'ICE'
        and bool(traded_options_metadata.get('actual_cob'))
        and traded_options_metadata.get('requested_cob')
        == traded_options_metadata.get('actual_cob')
    )
    traded_expiries = (
        _normalized_expiries(traded_options, 'maturity_date')
        if traded_reference_is_exact
        else []
    )
    published_expiries = _normalized_expiries(
        published_surface, 'contract_date'
    )
    manual_expiries = _normalized_expiries(manual_trades, 'contract_date')
    expiries = sorted(
        set(market_expiries)
        .union(surface_expiries)
        .union(traded_expiries)
        .union(published_expiries)
        .union(manual_expiries)
    )
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
    shown_legend_names = set()
    reference_product = str(
        operational_metadata.get('product', '')
    ).strip().upper()
    reference_is_exact = (
        bool(operational_metadata.get('actual_cob'))
        and operational_metadata.get('requested_cob')
        == operational_metadata.get('actual_cob')
    )

    for idx, expiry in enumerate(expiries):
        row = idx // num_cols + 1
        col = idx % num_cols + 1

        # Filter market data for this expiry
        exp_data = (
            market_data[market_data['expiry'] == expiry].copy()
            if 'expiry' in market_data.columns
            else pd.DataFrame()
        )
        surface_exp_data = (
            operational_surface[
                operational_surface['contract_date'] == expiry
            ].copy()
            if (
                x_axis == 'delta'
                and 'contract_date' in operational_surface.columns
            )
            else pd.DataFrame()
        )
        traded_exp_data = (
            traded_options[
                traded_options['maturity_date'] == expiry
            ].copy()
            if (
                traded_reference_is_exact
                and 'maturity_date' in traded_options.columns
            )
            else pd.DataFrame()
        )
        published_exp_data = (
            published_surface[
                published_surface['contract_date'] == expiry
            ].copy()
            if 'contract_date' in published_surface.columns
            else pd.DataFrame()
        )
        manual_exp_data = (
            manual_trades[manual_trades['contract_date'] == expiry].copy()
            if 'contract_date' in manual_trades.columns
            else pd.DataFrame()
        )

        forward = np.nan
        if not exp_data.empty and 'forward' in exp_data.columns:
            forward = pd.to_numeric(
                pd.Series([exp_data['forward'].iloc[0]]),
                errors='coerce',
            ).iloc[0]

        # ICE rows retain their raw strike/volatility. The selected-COB
        # calibration forward is used only for moneyness and delta positioning.
        if x_axis == 'log_moneyness':
            if not exp_data.empty:
                exp_data['x'] = np.log(exp_data['strike'] / forward)
            if not traded_exp_data.empty:
                traded_exp_data['x'] = np.log(
                    traded_exp_data['strike']
                    / traded_exp_data['forward_value']
                )
            if not published_exp_data.empty:
                published_forward = published_exp_data.get(
                    'working_forward', pd.Series(forward, index=published_exp_data.index)
                ).fillna(forward)
                published_exp_data['x'] = np.log(
                    published_exp_data['strike'] / published_forward
                )
            if not manual_exp_data.empty:
                manual_exp_data['x'] = np.log(
                    manual_exp_data['strike'] / manual_exp_data['forward']
                )
        elif x_axis == 'moneyness':
            if not exp_data.empty:
                exp_data['x'] = exp_data['strike'] / forward
            if not traded_exp_data.empty:
                traded_exp_data['x'] = (
                    traded_exp_data['strike']
                    / traded_exp_data['forward_value']
                )
            if not published_exp_data.empty:
                published_forward = published_exp_data.get(
                    'working_forward', pd.Series(forward, index=published_exp_data.index)
                ).fillna(forward)
                published_exp_data['x'] = (
                    published_exp_data['strike'] / published_forward
                )
            if not manual_exp_data.empty:
                manual_exp_data['x'] = (
                    manual_exp_data['strike'] / manual_exp_data['forward']
                )
        elif x_axis == 'delta':
            # Convert to standard delta display: 0 (OTM put) → 0.5 (ATM) → 1 (OTM call)
            # Puts (delta < 0): x = -delta (e.g., -0.25 → 0.25)
            # Calls (delta > 0): x = 1 - delta (e.g., 0.25 → 0.75)
            if not exp_data.empty:
                exp_data['x'] = exp_data['delta'].apply(
                    lambda d: -d if d < 0 else 1 - d
                )
            if not surface_exp_data.empty:
                surface_exp_data['x'] = _surface_delta_x(surface_exp_data)
            if not traded_exp_data.empty:
                traded_exp_data['x'] = 1.0 - traded_exp_data['call_delta']
            if not published_exp_data.empty:
                published_exp_data['x'] = 1.0 - published_exp_data['delta']
            if not manual_exp_data.empty:
                manual_exp_data['x'] = 1.0 - manual_exp_data['call_delta']

        if x_axis == 'delta':
            x_range = [0, 1]
        else:
            x_samples = []
            for chart_data in (
                exp_data,
                traded_exp_data,
                published_exp_data,
                manual_exp_data,
            ):
                if not chart_data.empty and 'x' in chart_data.columns:
                    x_samples.append(
                        pd.to_numeric(chart_data['x'], errors='coerce')
                        .replace([np.inf, -np.inf], np.nan)
                        .dropna()
                    )
            plotted_x = (
                pd.concat(x_samples, ignore_index=True)
                if x_samples
                else pd.Series(dtype=float)
            )
            if plotted_x.empty:
                x_range = (
                    [-0.5, 0.5]
                    if x_axis == 'log_moneyness'
                    else [0.7, 1.3]
                )
            else:
                data_min, data_max = plotted_x.min(), plotted_x.max()
                padding = (
                    (data_max - data_min) * 0.1
                    if data_max > data_min
                    else 0.1
                )
                if x_axis == 'log_moneyness':
                    x_range = [
                        min(data_min - padding, -0.5),
                        max(data_max + padding, 0.5),
                    ]
                else:
                    x_range = [
                        min(data_min - padding, 0.7),
                        max(data_max + padding, 1.3),
                    ]

        # Sort by x for proper display ordering
        if not exp_data.empty:
            exp_data = exp_data.sort_values('x')
        if not surface_exp_data.empty:
            surface_exp_data = surface_exp_data.sort_values('x')
        if not traded_exp_data.empty:
            traded_exp_data = (
                traded_exp_data
                .replace([np.inf, -np.inf], np.nan)
                .dropna(subset=['x', 'volatility'])
                .sort_values('x')
            )
        if not published_exp_data.empty:
            published_exp_data = (
                published_exp_data
                .replace([np.inf, -np.inf], np.nan)
                .dropna(subset=['x', 'volatility'])
                .sort_values('x')
            )
        if not manual_exp_data.empty:
            manual_exp_data = (
                manual_exp_data
                .replace([np.inf, -np.inf], np.nan)
                .dropna(subset=['x', 'mark_iv'])
                .sort_values('x')
            )

        # Get params for this expiry
        exp_params = (
            params_df[params_df['expiry'] == expiry]
            if 'expiry' in params_df.columns
            else pd.DataFrame()
        )
        if exp_params.empty:
            # Try matching by formatted expiry
            exp_str = pd.to_datetime(expiry).strftime('%b-%y')
            exp_params = (
                params_df[params_df['expiry'] == exp_str]
                if 'expiry' in params_df.columns
                else pd.DataFrame()
            )

        # Determine if this plot should be highlighted
        is_selected = selected_row is not None and idx == selected_row

        combine_market_reference = (
            x_axis == 'delta'
            and reference_product in {'TTF', 'JKM'}
            and reference_is_exact
            and _market_and_surface_are_identical(
                exp_data,
                surface_exp_data,
            )
        )
        is_ttf_settlement_reference = (
            reference_product == 'TTF'
            and (
                reference_is_exact
                or bool(market_metadata.get('settlement_cob'))
            )
        )

        if not exp_data.empty:
            if is_ttf_settlement_reference:
                market_name = 'Settlement vol surface (ICAP)'
            elif combine_market_reference:
                market_name = 'Market / Operational Surface'
            else:
                market_name = 'Market'
            connect_market_points = (
                combine_market_reference or is_ttf_settlement_reference
            )
            market_style = {
                'mode': 'lines+markers' if connect_market_points else 'markers',
                'marker': dict(
                    size=8,
                    color=(
                        '#0f766e'
                        if connect_market_points
                        else '#007bff'
                    ),
                    symbol=(
                        'diamond'
                        if connect_market_points
                        else 'circle'
                    ),
                    line=dict(width=1, color='white'),
                ),
            }
            if connect_market_points:
                market_style['line'] = dict(
                    color='#0f766e',
                    width=2,
                    dash='dash',
                )
            fig.add_trace(
                go.Scatter(
                    x=exp_data['x'],
                    y=exp_data['iv'] * 100,
                    name=market_name,
                    legendgroup=market_name,
                    showlegend=market_name not in shown_legend_names,
                    hovertemplate=(
                        f"<b>{subplot_titles[idx]}</b><br>"
                        f"X: %{{x:.3f}}<br>"
                        f"IV: %{{y:.2f}}%<br>"
                        "<extra></extra>"
                    ),
                    **market_style,
                ),
                row=row, col=col
            )
            shown_legend_names.add(market_name)

        if not surface_exp_data.empty and not combine_market_reference:
            surface_name = (
                'Settlement vol surface (ICAP)'
                if reference_product == 'TTF'
                else 'Operational Surface'
            )
            source = operational_metadata.get('source') or 'unknown'
            actual_cob = operational_metadata.get('actual_cob') or 'unknown'
            customdata = np.column_stack([
                surface_exp_data['delta_abs'],
                surface_exp_data['delta_bucket'],
            ])
            fig.add_trace(
                go.Scatter(
                    x=surface_exp_data['x'],
                    y=surface_exp_data['volatility'] * 100,
                    mode='lines+markers',
                    line=dict(color='#0f766e', width=2, dash='dash'),
                    marker=dict(
                        size=7,
                        color='#0f766e',
                        symbol='diamond',
                        line=dict(width=1, color='white'),
                    ),
                    name=surface_name,
                    legendgroup=surface_name,
                    showlegend=surface_name not in shown_legend_names,
                    customdata=customdata,
                    hovertemplate=(
                        f"<b>{subplot_titles[idx]} Operational Surface</b><br>"
                        f"Surface COB: {actual_cob}<br>"
                        "Display delta: %{x:.3f}<br>"
                        "Governed call delta: %{customdata[0]:.3f}<br>"
                        "Node: %{customdata[1]}<br>"
                        "IV: %{y:.2f}%<br>"
                        f"Source: {source}<br>"
                        "<extra></extra>"
                    ),
                ),
                row=row, col=col,
            )
            shown_legend_names.add(surface_name)

        if not published_exp_data.empty:
            published_name = 'Latest published smile'
            publication_id = published_metadata.get('publication_id') or 'unknown'
            published_at = published_metadata.get('published_at') or 'unknown'
            customdata = np.column_stack(
                [
                    published_exp_data['strike'],
                    published_exp_data['delta'],
                    published_exp_data.get(
                        'surface_region',
                        pd.Series('unknown', index=published_exp_data.index),
                    ),
                ]
            )
            fig.add_trace(
                go.Scatter(
                    x=published_exp_data['x'],
                    y=published_exp_data['volatility'] * 100,
                    mode='lines',
                    line=dict(color='#0f172a', width=2.5),
                    name=published_name,
                    legendgroup=published_name,
                    showlegend=published_name not in shown_legend_names,
                    customdata=customdata,
                    hovertemplate=(
                        f"<b>{subplot_titles[idx]} published smile</b><br>"
                        f"Published: {published_at}<br>"
                        "Strike: %{customdata[0]:.3f}<br>"
                        "Call delta: %{customdata[1]:.4f}<br>"
                        "IV: %{y:.2f}%<br>"
                        "Region: %{customdata[2]}<br>"
                        f"Publication: {publication_id}<br>"
                        "<extra></extra>"
                    ),
                ),
                row=row,
                col=col,
            )
            shown_legend_names.add(published_name)

        if not traded_exp_data.empty:
            traded_name = 'ICE traded options (volume > 0)'
            source = traded_options_metadata.get('source') or 'unknown'
            actual_cob = traded_options_metadata.get('actual_cob') or 'unknown'
            customdata = np.column_stack([
                traded_exp_data['strike'],
                traded_exp_data['call_delta'],
                traded_exp_data['settlement_price'],
                traded_exp_data['total_volume'],
                traded_exp_data['open_interest'],
                traded_exp_data['contract_type'],
                traded_exp_data['quality_status'],
            ])
            fig.add_trace(
                go.Scatter(
                    x=traded_exp_data['x'],
                    y=traded_exp_data['volatility'] * 100,
                    mode='markers',
                    marker=dict(
                        size=6,
                        color='#6d28d9',
                        symbol='circle-open',
                        opacity=0.85,
                        line=dict(width=1.25, color='#6d28d9'),
                    ),
                    name=traded_name,
                    legendgroup=traded_name,
                    showlegend=traded_name not in shown_legend_names,
                    customdata=customdata,
                    hovertemplate=(
                        f"<b>{subplot_titles[idx]} ICE traded option</b><br>"
                        f"COB: {actual_cob}<br>"
                        "Strike: %{customdata[0]:.2f} EUR/MWh<br>"
                        "Call delta: %{customdata[1]:.4f}<br>"
                        "ICE option volatility: %{y:.2f}%<br>"
                        "Settlement: %{customdata[2]:.3f}<br>"
                        "Volume: %{customdata[3]:,.0f} lots<br>"
                        "Open interest: %{customdata[4]:,.0f}<br>"
                        "Contract: %{customdata[5]}<br>"
                        "Quality: %{customdata[6]}<br>"
                        f"Source: {source}<br>"
                        "<extra></extra>"
                    ),
                ),
                row=row,
                col=col,
            )
            shown_legend_names.add(traded_name)

        if not manual_exp_data.empty:
            manual_name = 'Manual intraday trades'
            persistence = manual_trades_metadata.get('persistence') or 'session'
            customdata = np.column_stack(
                [
                    manual_exp_data['strike'],
                    manual_exp_data['call_delta'],
                    manual_exp_data['forward'],
                    manual_exp_data.get(
                        'volume', pd.Series(np.nan, index=manual_exp_data.index)
                    ),
                    manual_exp_data.get(
                        'put_call', pd.Series('', index=manual_exp_data.index)
                    ),
                    manual_exp_data.get(
                        'observed_at', pd.Series('', index=manual_exp_data.index)
                    ),
                ]
            )
            fig.add_trace(
                go.Scatter(
                    x=manual_exp_data['x'],
                    y=manual_exp_data['mark_iv'] * 100,
                    mode='markers',
                    marker=dict(
                        size=5,
                        color='#dc2626',
                        symbol='x',
                        opacity=0.9,
                        line=dict(width=1, color='#dc2626'),
                    ),
                    name=manual_name,
                    legendgroup=manual_name,
                    showlegend=manual_name not in shown_legend_names,
                    customdata=customdata,
                    hovertemplate=(
                        f"<b>{subplot_titles[idx]} manual trade</b><br>"
                        "Strike: %{customdata[0]:.3f}<br>"
                        "Call delta: %{customdata[1]:.4f}<br>"
                        "IV: %{y:.2f}%<br>"
                        "Working forward: %{customdata[2]:.3f}<br>"
                        "Volume: %{customdata[3]:,.0f}<br>"
                        "Option: %{customdata[4]}<br>"
                        "Observed: %{customdata[5]}<br>"
                        f"State: {persistence}<br>"
                        "<extra></extra>"
                    ),
                ),
                row=row,
                col=col,
            )
            shown_legend_names.add(manual_name)

        model_dte = np.nan
        if not exp_data.empty and 'dte' in exp_data.columns:
            model_dte = pd.to_numeric(
                pd.Series([exp_data['dte'].iloc[0]]),
                errors='coerce',
            ).iloc[0]

        # Add model curve only when the calibration inputs are usable.
        if (
            not exp_data.empty
            and not exp_params.empty
            and pd.notna(forward)
            and pd.notna(model_dte)
            and model_dte > 0
        ):
            params = exp_params.iloc[0].to_dict()
            left_blend_width = pd.to_numeric(
                pd.Series([params.get('left_blend_width')]), errors='coerce'
            ).iloc[0]
            right_blend_width = pd.to_numeric(
                pd.Series([params.get('right_blend_width')]), errors='coerce'
            ).iloc[0]
            is_ttf_hybrid = bool(
                np.isfinite(left_blend_width)
                and np.isfinite(right_blend_width)
                and str(params.get('calibration_method', '')).startswith(
                    'PCHIP-core/'
                )
            )

            if is_ttf_hybrid:
                try:
                    from vol_calibration.calibration_inputs import (
                        select_expiry_observations,
                    )
                    from vol_calibration.ttf_hybrid_surface import (
                        operational_surface_frame as ttf_operational_surface_frame,
                    )

                    governed_inputs = select_expiry_observations(
                        exp_data,
                        expiry,
                        include_extrapolated=True,
                    )
                    hybrid_frame = ttf_operational_surface_frame(
                        governed_inputs,
                        params,
                        left_blend_width=float(left_blend_width),
                        right_blend_width=float(right_blend_width),
                        n_points=401,
                    )
                    if x_axis == 'log_moneyness':
                        hybrid_frame['plot_x'] = hybrid_frame['log_moneyness']
                    elif x_axis == 'moneyness':
                        hybrid_frame['plot_x'] = hybrid_frame['strike'] / forward
                    else:
                        hybrid_frame['plot_x'] = 1.0 - hybrid_frame['delta']
                    hybrid_frame = hybrid_frame.sort_values('plot_x')
                    hybrid_name = 'Operational surface (PCHIP core / Wing tails)'
                    fig.add_trace(
                        go.Scatter(
                            x=hybrid_frame['plot_x'],
                            y=hybrid_frame['iv'] * 100,
                            mode='lines',
                            line=dict(color='#fd7e14', width=3),
                            name=hybrid_name,
                            legendgroup=hybrid_name,
                            showlegend=hybrid_name not in shown_legend_names,
                            customdata=np.column_stack(
                                [
                                    hybrid_frame['blend_classification'],
                                    hybrid_frame['total_variance'],
                                    hybrid_frame['strike'],
                                ]
                            ),
                            hovertemplate=(
                                f"<b>{subplot_titles[idx]} hybrid</b><br>"
                                "X: %{x:.4f}<br>"
                                "Strike: %{customdata[2]:.3f}<br>"
                                "IV: %{y:.2f}%<br>"
                                "Total variance: %{customdata[1]:.6f}<br>"
                                "Region: %{customdata[0]}<br>"
                                "<extra></extra>"
                            ),
                        ),
                        row=row,
                        col=col,
                    )
                    shown_legend_names.add(hybrid_name)

                    if is_selected:
                        for region_name, fill_color in (
                            ('left_blend', 'rgba(13, 110, 253, 0.08)'),
                            ('right_blend', 'rgba(13, 110, 253, 0.08)'),
                        ):
                            region = hybrid_frame[
                                hybrid_frame['blend_classification'] == region_name
                            ]
                            if not region.empty:
                                fig.add_vrect(
                                    x0=float(region['plot_x'].min()),
                                    x1=float(region['plot_x'].max()),
                                    fillcolor=fill_color,
                                    line_width=0,
                                    row=row,
                                    col=col,
                                )
                        for boundary in ('left_blend', 'pchip_core', 'right_blend'):
                            region = hybrid_frame[
                                hybrid_frame['blend_classification'] == boundary
                            ]
                            if not region.empty:
                                fig.add_vline(
                                    x=float(region['plot_x'].min()),
                                    line=dict(color='#0d6efd', dash='dot', width=1),
                                    row=row,
                                    col=col,
                                )
                except Exception:
                    is_ttf_hybrid = False

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
            wing_params = {k: params.get(k, np.nan) for k in ['vr', 'sr', 'pc', 'cc', 'dc', 'uc', 'dsm', 'usm', 'vcr', 'scr', 'ssr', 'put_wing_power', 'call_wing_power']}

            # Use reasonable defaults if missing
            if pd.isna(wing_params.get('ssr')):
                wing_params['ssr'] = 1.0
            if pd.isna(wing_params.get('put_wing_power')):
                wing_params['put_wing_power'] = 0.5
            if pd.isna(wing_params.get('call_wing_power')):
                wing_params['call_wing_power'] = 0.5

            try:
                if x_axis == 'delta':
                    # Use REVERSE-DELTA mapping to guarantee monotonicity
                    # Instead of Strike→IV→Delta (non-monotonic), we use Delta→Strike→IV
                    dte = model_dte

                    # PUT wing: display x from 0.005 to 0.48 (extended to show extreme OTM puts)
                    x_put_grid = np.linspace(0.005, 0.48, 60)
                    x_put, _, iv_put = delta_curve_to_strike_iv(
                        x_put_grid,
                        forward,
                        dte,
                        wing_params,
                        wing_model_iv,
                        is_put=True,
                        model_version=model_version,
                    )

                    # CALL wing: display x from 0.52 to 0.995 (extended to show extreme OTM calls)
                    x_call_grid = np.linspace(0.52, 0.995, 60)
                    call_delta_grid = 1.0 - x_call_grid
                    call_deltas, _, iv_call = delta_curve_to_strike_iv(
                        call_delta_grid,
                        forward,
                        dte,
                        wing_params,
                        wing_model_iv,
                        is_put=False,
                        model_version=model_version,
                    )
                    x_call = 1.0 - call_deltas

                    # Combine (already monotonic by construction, no sorting needed)
                    x_model = np.concatenate([x_put, x_call])
                    model_iv = np.concatenate([iv_put, iv_call])
                else:
                    model_iv = wing_model_iv(
                        strike=strikes_model,
                        forward=forward,
                        model_version=model_version,
                        dte=model_dte,
                        **wing_params
                    )
                    sort_idx = np.argsort(x_model)
                    x_model = x_model[sort_idx]
                    model_iv = model_iv[sort_idx]

                model_name = (
                    'Wing tail-fit diagnostic'
                    if is_ttf_hybrid
                    else (
                        'Calibrated surface (Wing-v2)'
                        if reference_product == 'TTF'
                        else 'Model'
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=x_model,
                        y=model_iv * 100,  # Convert to percentage
                        mode='lines',
                        line=dict(
                            color='#2563eb' if is_ttf_hybrid else '#fd7e14',
                            width=1.5 if is_ttf_hybrid else 2,
                            dash='dash' if is_ttf_hybrid else 'solid',
                        ),
                        name=model_name,
                        legendgroup=model_name,
                        showlegend=model_name not in shown_legend_names,
                    ),
                    row=row, col=col
                )
                shown_legend_names.add(model_name)
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
            x=1,
            groupclick='togglegroup',
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
    height: int = 300,
    model_version: str = DEFAULT_CALIBRATION_MODEL_VERSION,
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
    from options.calibration_engine.models.wing_model import wing_model_iv

    fig = go.Figure()

    if market_data.empty:
        return fig

    forward = market_data['forward'].iloc[0]
    market_data = market_data.copy()
    model_dte = pd.to_numeric(
        pd.Series([market_data['dte'].iloc[0]])
        if 'dte' in market_data.columns
        else pd.Series([np.nan]),
        errors='coerce',
    ).iloc[0]

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
        wing_params = {k: params.get(k, np.nan) for k in ['vr', 'sr', 'pc', 'cc', 'dc', 'uc', 'dsm', 'usm', 'vcr', 'scr', 'ssr', 'put_wing_power', 'call_wing_power']}
        if pd.isna(wing_params.get('ssr')):
            wing_params['ssr'] = 1.0
        if pd.isna(wing_params.get('put_wing_power')):
            wing_params['put_wing_power'] = 0.5
        if pd.isna(wing_params.get('call_wing_power')):
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
            model_iv = wing_model_iv(
                strike=strikes_model,
                forward=forward,
                model_version=model_version,
                dte=model_dte,
                **wing_params,
            )
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
