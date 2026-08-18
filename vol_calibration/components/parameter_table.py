"""
Excel-like editable parameter table component.

Implements Framework Section 4.2:
- 14 columns: Expiry + 11 Wing parameters + Arb Status + RMSE
- Inline editing with immediate plot update
- Conditional formatting on RMSE and Arbitrage status
- Row selection highlights corresponding smile plot
"""
from dash import html, dash_table
import numpy as np
import pandas as pd
from typing import List, Dict, Optional

from options.calibration_engine.validation.arbitrage import check_butterfly


# Parameter columns configuration
PARAM_COLUMNS = [
    {'id': 'expiry', 'name': 'Expiry', 'type': 'text', 'editable': False},
    {'id': 'vr', 'name': 'vr', 'type': 'numeric', 'editable': True},
    {'id': 'sr', 'name': 'sr', 'type': 'numeric', 'editable': True},
    {'id': 'pc', 'name': 'pc', 'type': 'numeric', 'editable': True},
    {'id': 'cc', 'name': 'cc', 'type': 'numeric', 'editable': True},
    {'id': 'dc', 'name': 'dc', 'type': 'numeric', 'editable': True},
    {'id': 'uc', 'name': 'uc', 'type': 'numeric', 'editable': True},
    {'id': 'dsm', 'name': 'dsm', 'type': 'numeric', 'editable': True},
    {'id': 'usm', 'name': 'usm', 'type': 'numeric', 'editable': True},
    {'id': 'vcr', 'name': 'VCR', 'type': 'numeric', 'editable': True},
    {'id': 'scr', 'name': 'SCR', 'type': 'numeric', 'editable': True},
    {'id': 'ssr', 'name': 'SSR', 'type': 'numeric', 'editable': True},
    {'id': 'put_wing_power', 'name': 'PTVS', 'type': 'numeric', 'editable': True},
    {'id': 'call_wing_power', 'name': 'CTVS', 'type': 'numeric', 'editable': True},
    {'id': 'arb_status', 'name': 'Arb', 'type': 'text', 'editable': False},
    {'id': 'rmse', 'name': 'RMSE', 'type': 'text', 'editable': False},
]
TTF_BASIS_COLUMN = {
    'id': 'calibration_basis',
    'name': 'Basis',
    'type': 'text',
    'editable': False,
}
TTF_HYBRID_COLUMNS = [
    {
        'id': 'left_blend_width',
        'name': 'Left Blend',
        'type': 'numeric',
        'editable': True,
    },
    {
        'id': 'right_blend_width',
        'name': 'Right Blend',
        'type': 'numeric',
        'editable': True,
    },
    {
        'id': 'tail_fit_tv_rmse',
        'name': 'Wing Tail TV RMSE',
        'type': 'text',
        'editable': False,
    },
    {
        'id': 'iv_rmse',
        'name': 'IV Diagnostic',
        'type': 'text',
        'editable': False,
    },
    {
        'id': 'calibration_method',
        'name': 'Method',
        'type': 'text',
        'editable': False,
    },
]


def parameter_columns_for_commodity(commodity: Optional[str] = None) -> List[Dict]:
    """Return the shared parameter columns plus TTF-only provenance."""
    columns = [dict(column) for column in PARAM_COLUMNS]
    if str(commodity or '').strip().upper() == 'TTF':
        columns.insert(1, dict(TTF_BASIS_COLUMN))
        rmse_column = next(column for column in columns if column['id'] == 'rmse')
        rmse_column['name'] = 'Core TV RMSE'
        arb_index = next(
            index for index, column in enumerate(columns) if column['id'] == 'arb_status'
        )
        for offset, column in enumerate(TTF_HYBRID_COLUMNS):
            columns.insert(arb_index + offset, dict(column))
    return columns

# Column definitions for DataTable
COLUMN_DEFS = [
    {
        'id': col['id'],
        'name': col['name'],
        'type': col['type'],
        'editable': col['editable'],
        'format': {'specifier': '.4f'} if col['type'] == 'numeric' else None,
    }
    for col in PARAM_COLUMNS
]


def create_parameter_table(
    commodity: str,
    data: Optional[List[Dict]] = None,
    table_id: Optional[str] = None
) -> html.Div:
    """
    Create the Excel-like parameter table.

    Parameters
    ----------
    commodity : str
        Commodity code (BRENT, HH, TTF, JKM)
    data : list of dict, optional
        Initial table data. If None, creates empty table.
    table_id : str, optional
        Custom ID for the table. Defaults to '{commodity}-param-table'

    Returns
    -------
    html.Div
        Container with the parameter table
    """
    if table_id is None:
        table_id = f"{commodity.lower()}-param-table"

    if data is None:
        data = []

    # Create column definitions
    columns = []
    product_columns = parameter_columns_for_commodity(commodity)
    for col in product_columns:
        col_def = {
            'id': col['id'],
            'name': col['name'],
            'type': 'numeric' if col['type'] == 'numeric' else 'text',
            'editable': col['editable'],
        }
        if col['type'] == 'numeric' and col['id'] != 'rmse':
            col_def['format'] = {'specifier': '.4f'}
        columns.append(col_def)

    table = dash_table.DataTable(
        id=table_id,
        columns=columns,
        data=data,
        editable=True,
        row_selectable='single',
        selected_rows=[],
        page_action='none',
        fixed_rows={'headers': True},
        style_table={
            'height': '400px',
            'overflowY': 'auto',
            'overflowX': 'auto',
        },
        style_header={
            'backgroundColor': '#343a40',
            'color': 'white',
            'fontWeight': 'bold',
            'textAlign': 'center',
            'padding': '10px 5px',
        },
        style_cell={
            'textAlign': 'center',
            'padding': '8px 5px',
            'minWidth': '65px',
            'maxWidth': '100px',
            'whiteSpace': 'nowrap',
            'overflow': 'hidden',
            'textOverflow': 'ellipsis',
        },
        style_cell_conditional=[
            {
                'if': {'column_id': 'expiry'},
                'textAlign': 'left',
                'fontWeight': 'bold',
                'minWidth': '90px',
            },
            {
                'if': {'column_id': 'rmse'},
                'fontWeight': 'bold',
            },
            {
                'if': {'column_id': 'calibration_basis'},
                'fontWeight': 'bold',
                'minWidth': '95px',
            },
            {
                'if': {'column_id': 'arb_status'},
                'fontWeight': 'bold',
                'minWidth': '50px',
                'maxWidth': '60px',
            },
        ],
        style_data_conditional=[
            # RMSE conditional formatting: green <0.2%, yellow 0.2-0.5%, red >0.5%
            {
                'if': {
                    'filter_query': '{rmse} < 0.002',
                    'column_id': 'rmse'
                },
                'backgroundColor': '#d4edda',
                'color': '#155724',
            },
            {
                'if': {
                    'filter_query': '{rmse} >= 0.002 && {rmse} < 0.005',
                    'column_id': 'rmse'
                },
                'backgroundColor': '#fff3cd',
                'color': '#856404',
            },
            {
                'if': {
                    'filter_query': '{rmse} >= 0.005',
                    'column_id': 'rmse'
                },
                'backgroundColor': '#f8d7da',
                'color': '#721c24',
            },
            # Arbitrage status conditional formatting
            {
                'if': {
                    'filter_query': '{arb_status} = "Pass"',
                    'column_id': 'arb_status'
                },
                'backgroundColor': '#d4edda',
                'color': '#155724',
            },
            {
                'if': {
                    'filter_query': '{arb_status} = "Warn"',
                    'column_id': 'arb_status'
                },
                'backgroundColor': '#fff3cd',
                'color': '#856404',
            },
            {
                'if': {
                    'filter_query': '{arb_status} = "Fail"',
                    'column_id': 'arb_status'
                },
                'backgroundColor': '#f8d7da',
                'color': '#721c24',
            },
            # Highlight selected row
            {
                'if': {'state': 'selected'},
                'backgroundColor': 'rgba(0, 123, 255, 0.15)',
                'border': '1px solid #007bff',
            },
            # Alternating row colors
            {
                'if': {'row_index': 'odd'},
                'backgroundColor': '#f8f9fa',
            },
        ],
        tooltip_data=[
            {
                col['id']: {'value': get_param_tooltip(col['id']), 'type': 'markdown'}
                for col in product_columns
            }
            for _ in data
        ] if data else [],
        tooltip_header={
            col['id']: get_param_tooltip(col['id'])
            for col in product_columns
        },
        tooltip_duration=None,
    )

    return html.Div([
        table,
    ], className="parameter-table-container")


def get_param_tooltip(param_id: str) -> str:
    """Get tooltip text for a parameter."""
    tooltips = {
        'expiry': 'Option expiry date',
        'calibration_basis': (
            'Observed uses the official smile; Extrapolated uses the governed '
            'official TTF template tail.'
        ),
        'vr': 'Vol Ref: ATM reference volatility',
        'sr': 'Slope Ref: Skew at ATM (positive=call skew, negative=put skew)',
        'pc': 'Put Curvature: Curvature of put wing',
        'cc': 'Call Curvature: Curvature of call wing',
        'dc': 'Down Cutoff: Log-moneyness where put wing flattens',
        'uc': 'Up Cutoff: Log-moneyness where call wing flattens',
        'dsm': 'Down Smoothing: Transition range for put wing',
        'usm': 'Up Smoothing: Transition range for call wing',
        'vcr': 'Vol Change Rate: ATM vol change per spot move',
        'scr': 'Slope Change Rate: Skew change per spot move',
        'ssr': 'Smile Scale Rate: Sticky-delta (1) vs sticky-strike (0)',
        'put_wing_power': 'Put Total-Variance Slope: Wing-v2 asymptotic slope beta in [0, 2); 0 gives a flat tail',
        'call_wing_power': 'Call Total-Variance Slope: Wing-v2 asymptotic slope beta in [0, 2); 0 gives a flat tail',
        'left_blend_width': 'Log-moneyness width of the C1 put-side PCHIP/Wing transition.',
        'right_blend_width': 'Log-moneyness width of the C1 call-side PCHIP/Wing transition.',
        'tail_fit_tv_rmse': 'Total-variance RMSE of the internal Wing approximation to 201 PCHIP core samples.',
        'iv_rmse': 'Secondary IV RMSE diagnostic for the internal Wing approximation; the operational core remains PCHIP.',
        'calibration_method': 'The governed operational surface construction method.',
        'arb_status': 'Arbitrage Status: Pass/Warn/Fail butterfly arbitrage check',
        'rmse': 'TTF: exact PCHIP core total-variance RMSE. Other products: model IV RMSE.',
    }
    return tooltips.get(param_id, '')


def check_arbitrage_status(
    params: Dict[str, float],
    forward: Optional[float] = None,
    dte: Optional[float] = None,
) -> str:
    """
    Check arbitrage status for a set of Wing Model parameters.

    Parameters
    ----------
    params : dict
        Wing Model parameters (vr, sr, pc, cc, dc, uc, dsm, usm, vcr, scr, ssr,
        put_wing_power, call_wing_power)

    forward : float
        Forward price for the expiry

    dte : float
        Days to expiry

    Returns
    -------
    str
        'Pass' if no violations, 'Warn' if marginal (min_g between -0.01 and 0),
        'Fail' if butterfly arbitrage detected
    """
    try:
        if (
            forward is None
            or dte is None
            or not np.isfinite(float(forward))
            or not np.isfinite(float(dte))
            or float(forward) <= 0
            or float(dte) <= 0
        ):
            return 'Warn'
        result = check_butterfly(
            params=params,
            forward=forward,
            dte=dte,
            moneyness_range=(-0.40, 0.40),
            n_points=50,
            tol=1e-6
        )

        if result['is_valid']:
            # Check if marginal (min_g close to zero)
            if result['min_g'] < 0.001:
                return 'Warn'
            return 'Pass'
        else:
            return 'Fail'

    except Exception:
        # If check fails, return warning
        return 'Warn'


def format_params_for_table(
    params_df: pd.DataFrame,
    market_data: Optional[pd.DataFrame] = None,
    commodity: Optional[str] = None,
) -> List[Dict]:
    """
    Format parameters DataFrame for the DataTable.

    Parameters
    ----------
    params_df : DataFrame
        Parameters with columns: expiry, vr, sr, pc, cc, dc, uc, dsm, usm, vcr, scr, ssr, rmse

    market_data : DataFrame, optional
        Market data with expiry and forward columns for arbitrage checking

    Returns
    -------
    list of dict
        Data formatted for dash_table.DataTable
    """
    if params_df.empty:
        return []

    params_df = params_df.copy()

    # Ensure all columns exist
    product_columns = parameter_columns_for_commodity(commodity)
    for col in product_columns:
        if col['id'] not in params_df.columns:
            params_df[col['id']] = 0.0 if col['type'] == 'numeric' else ''

    # Calculate arbitrage status for each row
    arb_statuses = []
    for idx, row in params_df.iterrows():
        # Extract Wing Model params
        wing_params = {
            'vr': row.get('vr', 0.3),
            'sr': row.get('sr', 0.0),
            'pc': row.get('pc', 0.1),
            'cc': row.get('cc', 0.1),
            'dc': row.get('dc', -0.2),
            'uc': row.get('uc', 0.2),
            'dsm': row.get('dsm', 0.05),
            'usm': row.get('usm', 0.05),
            'vcr': row.get('vcr', 0.0),
            'scr': row.get('scr', 0.0),
            'ssr': row.get('ssr', 1.0),
            'put_wing_power': row.get('put_wing_power', 0.5),
            'call_wing_power': row.get('call_wing_power', 0.5),
        }

        # Get forward price from market_data if available
        forward = None
        dte = None
        if market_data is not None and not market_data.empty:
            expiry_val = row.get('expiry')
            if expiry_val is not None:
                try:
                    target_period = pd.Period(pd.to_datetime(expiry_val), freq='M')
                    market_periods = pd.to_datetime(
                        market_data['expiry'], errors='coerce'
                    ).dt.to_period('M')
                    exp_match = market_data.loc[market_periods == target_period]
                    if not exp_match.empty:
                        if 'forward' in exp_match.columns:
                            forward = exp_match['forward'].iloc[0]
                        if 'dte' in exp_match.columns:
                            dte = exp_match['dte'].iloc[0]
                except Exception:
                    pass

        if str(commodity or '').strip().upper() == 'TTF':
            left_width = pd.to_numeric(
                pd.Series([row.get('left_blend_width')]), errors='coerce'
            ).iloc[0]
            right_width = pd.to_numeric(
                pd.Series([row.get('right_blend_width')]), errors='coerce'
            ).iloc[0]
            arb_statuses.append(
                'Pass'
                if np.isfinite(left_width) and np.isfinite(right_width)
                else 'Uncalibrated'
            )
        else:
            arb_statuses.append(check_arbitrage_status(wing_params, forward, dte))

    params_df['arb_status'] = arb_statuses

    # Format expiry as string (Mon-YY)
    if 'expiry' in params_df.columns:
        params_df['expiry'] = pd.to_datetime(params_df['expiry']).dt.strftime('%b-%y')

    if 'calibration_basis' in params_df.columns:
        params_df['calibration_basis'] = (
            params_df['calibration_basis'].astype(str).str.strip().str.title()
        )

    is_ttf = str(commodity or '').strip().upper() == 'TTF'
    # TTF total-variance metrics retain their native units.  Shared products
    # keep the historical percentage IV-RMSE presentation.
    if 'rmse' in params_df.columns:
        params_df['rmse'] = params_df['rmse'].apply(
            lambda x: (
                f"{float(x):.6f}"
                if is_ttf and pd.notna(x)
                else (f"{x*100:.2f}%" if pd.notna(x) else "")
            )
        )
    if is_ttf and 'tail_fit_tv_rmse' in params_df.columns:
        params_df['tail_fit_tv_rmse'] = params_df['tail_fit_tv_rmse'].apply(
            lambda x: f"{float(x):.6f}" if pd.notna(x) else ""
        )
    if is_ttf and 'iv_rmse' in params_df.columns:
        params_df['iv_rmse'] = params_df['iv_rmse'].apply(
            lambda x: f"{float(x) * 100:.2f}%" if pd.notna(x) else ""
        )

    # Convert to list of dicts
    return params_df[
        ['expiry'] + [c['id'] for c in product_columns if c['id'] != 'expiry']
    ].to_dict('records')


def parse_table_data(data: List[Dict]) -> pd.DataFrame:
    """
    Parse DataTable data back to DataFrame.

    Parameters
    ----------
    data : list of dict
        Data from dash_table.DataTable

    Returns
    -------
    DataFrame
        Parameters DataFrame
    """
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)

    # Parse RMSE from percentage string back to decimal
    if 'rmse' in df.columns:
        df['rmse'] = df['rmse'].apply(
            lambda x: float(x.replace('%', '')) / 100 if isinstance(x, str) and '%' in x else x
        )

    if 'calibration_basis' in df.columns:
        df['calibration_basis'] = (
            df['calibration_basis'].astype(str).str.strip().str.lower()
        )

    return df


def update_arb_status_in_row(
    row: Dict,
    forward: Optional[float] = None,
    dte: Optional[float] = None,
) -> str:
    """
    Calculate arbitrage status for a single row.

    Parameters
    ----------
    row : dict
        Row data with Wing Model parameters
    forward : float
        Forward price for the expiry
    dte : float
        Days to expiry

    Returns
    -------
    str
        'Pass', 'Warn', or 'Fail'
    """
    wing_params = {
        'vr': row.get('vr', 0.3),
        'sr': row.get('sr', 0.0),
        'pc': row.get('pc', 0.1),
        'cc': row.get('cc', 0.1),
        'dc': row.get('dc', -0.2),
        'uc': row.get('uc', 0.2),
        'dsm': row.get('dsm', 0.05),
        'usm': row.get('usm', 0.05),
        'vcr': row.get('vcr', 0.0),
        'scr': row.get('scr', 0.0),
        'ssr': row.get('ssr', 1.0),
        'put_wing_power': row.get('put_wing_power', 0.5),
        'call_wing_power': row.get('call_wing_power', 0.5),
    }
    return check_arbitrage_status(wing_params, forward, dte)
