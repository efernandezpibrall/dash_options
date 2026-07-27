"""
Excel-like editable parameter table component.

Implements Framework Section 4.2:
- 14 columns: Expiry + 11 Wing parameters + Arb Status + RMSE
- Inline editing with immediate plot update
- Conditional formatting on RMSE and Arbitrage status
- Row selection highlights corresponding smile plot
"""
import dash_bootstrap_components as dbc
from dash import html, dash_table
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
    {'id': 'put_wing_power', 'name': 'PWP', 'type': 'numeric', 'editable': True},
    {'id': 'call_wing_power', 'name': 'CWP', 'type': 'numeric', 'editable': True},
    {'id': 'arb_status', 'name': 'Arb', 'type': 'text', 'editable': False},
    {'id': 'rmse', 'name': 'RMSE', 'type': 'text', 'editable': False},
]

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
    for col in PARAM_COLUMNS:
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
                for col in PARAM_COLUMNS
            }
            for _ in data
        ] if data else [],
        tooltip_header={
            col['id']: get_param_tooltip(col['id'])
            for col in PARAM_COLUMNS
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
        'put_wing_power': 'Put Wing Power: Power exponent for deep put wing (0=flat, 0.5=sqrt, 1=linear)',
        'call_wing_power': 'Call Wing Power: Power exponent for deep call wing (0=flat, 0.5=sqrt, 1=linear)',
        'arb_status': 'Arbitrage Status: Pass/Warn/Fail butterfly arbitrage check',
        'rmse': 'Root Mean Square Error: Fit quality metric',
    }
    return tooltips.get(param_id, '')


def check_arbitrage_status(params: Dict[str, float], forward: float = 50.0, dte: float = 30.0) -> str:
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
    market_data: Optional[pd.DataFrame] = None
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
    for col in PARAM_COLUMNS:
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
        forward = 50.0  # Default
        dte = 30.0  # Default
        if market_data is not None and not market_data.empty:
            expiry_val = row.get('expiry')
            if expiry_val is not None:
                # Try to find matching expiry in market data
                try:
                    # market_data expiry might be datetime or string
                    exp_match = market_data[market_data['expiry'] == expiry_val]
                    if not exp_match.empty and 'forward' in exp_match.columns:
                        forward = exp_match['forward'].iloc[0]
                except Exception:
                    pass

        arb_statuses.append(check_arbitrage_status(wing_params, forward, dte))

    params_df['arb_status'] = arb_statuses

    # Format expiry as string (Mon-YY)
    if 'expiry' in params_df.columns:
        params_df['expiry'] = pd.to_datetime(params_df['expiry']).dt.strftime('%b-%y')

    # Format RMSE as percentage string
    if 'rmse' in params_df.columns:
        params_df['rmse'] = params_df['rmse'].apply(
            lambda x: f"{x*100:.2f}%" if pd.notna(x) else ""
        )

    # Convert to list of dicts
    return params_df[['expiry'] + [c['id'] for c in PARAM_COLUMNS if c['id'] != 'expiry']].to_dict('records')


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

    return df


def update_arb_status_in_row(row: Dict, forward: float = 50.0, dte: float = 30.0) -> str:
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
