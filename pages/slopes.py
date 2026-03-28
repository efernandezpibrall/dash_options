import dash
from dash import html, dcc
import pandas as pd
import numpy as np
from dash import Dash, html, dcc, callback, Input, Output, dash_table, State, callback_context
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import datetime
import configparser
# Trino
from trino.dbapi import connect
from trino.auth import JWTAuthentication
import os

# ------ code to be able to access config.ini, even having the path in the .virtualenvs is not working without it ------#
try:
    # Get the directory where your script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate to the directory containing config.ini
    # Adjust the number of '..' as needed to reach the correct directory
    config_dir = os.path.abspath(os.path.join(script_dir, '..', '..'))  # Go up one level
    CONFIG_FILE_PATH = os.path.join(config_dir, 'config.ini')
except:
    CONFIG_FILE_PATH = 'config.ini'  # Assumes it's in the same directory or the path it is detected

# --- Load Configuration from INI File ---
config_reader = configparser.ConfigParser(interpolation=None)
config_reader.read(CONFIG_FILE_PATH)

# Read values from the ini file sections
TRINOS_HOST = config_reader.get('TRINOS', 'HOST', fallback=None)
TRINOS_USERNAME = config_reader.get('TRINOS', 'USERNAME', fallback=None)
TRINOS_TOKEN = config_reader.get('TRINOS', 'TOKEN', fallback=None)
TRINOS_PORT = config_reader.get('TRINOS', 'PORT', fallback=None)

# SQL Queries (moved to module level for reuse)
QUERY_GAS = '''
SELECT
  *
FROM
  raw."ice_gas"."cleared_gas"
WHERE
  (product = 'LNG Futures' AND contract = 'JKM')
  AND ("contract_type" != 'P' AND "contract_type" != 'C')
  AND product NOT LIKE '%daily%'
'''

QUERY_OIL = '''
SELECT
  *
FROM
  raw."ice_oil"."cleared_oil"
WHERE
  (product = 'Brent Crude Futures' AND contract = 'B')
'''


# Function to retrieve data from trinos in a df
def read_table_conn(conn, query):
    cur = conn.cursor()
    cur.execute(query)
    rows = cur.fetchall()
    num_fields = len(cur.description)
    field_names = [i[0] for i in cur.description]
    df_data = pd.DataFrame(rows, columns=field_names)
    cur.close()
    return df_data


# Function to create database connections
def create_connections():
    """Create and return database connections"""
    conn_gas = connect(
        host=TRINOS_HOST,
        port=TRINOS_PORT,
        user=TRINOS_USERNAME,
        auth=JWTAuthentication(TRINOS_TOKEN),
        http_scheme="https",
        verify=False,
        catalog="raw",
        schema="ice_gas",
    )

    conn_oil = connect(
        host=TRINOS_HOST,
        port=TRINOS_PORT,
        user=TRINOS_USERNAME,
        auth=JWTAuthentication(TRINOS_TOKEN),
        http_scheme="https",
        verify=False,
        catalog="raw",
        schema="ice_oil",
    )

    return conn_gas, conn_oil


# Function to load and process data
def load_and_process_data():
    """Load data from Trino and process it"""
    try:
        # Create connections
        conn_gas, conn_oil = create_connections()

        # Load data
        df_options_gas = read_table_conn(conn_gas, QUERY_GAS)
        df_options_oil = read_table_conn(conn_oil, QUERY_OIL)

        # Close connections
        conn_gas.close()
        conn_oil.close()

        # Process data
        df_options_gas.loc[df_options_gas['contract'] == 'JKM', 'product'] = 'JKM'
        df_options_oil.loc[df_options_oil['contract'] == 'B', 'product'] = 'Brent'
        df_options_oil.loc[df_options_oil['contract'] == 'B', 'contract'] = 'Brent'

        # Combine DataFrames
        df_options = pd.concat([df_options_gas, df_options_oil], ignore_index=True)

        # Don't pre-calculate Brent indices - they will be generated on-demand
        # when user selects a specific index in the UI
        print("\nBrent data loaded. Indices will be calculated on-demand when selected.")

        return df_options

    except Exception as e:
        print(f"Error loading data: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()  # Return empty DataFrame on error


# RESTORED: Initialize DataFrame at module level but allow refresh
df_options = pd.DataFrame()


def get_previous_business_date():
    """Get the previous business date (excluding weekends)"""
    today = pd.Timestamp.now()

    # If today is Monday, go back to Friday
    if today.weekday() == 0:  # Monday
        prev_date = today - pd.Timedelta(days=4)
    # Otherwise, go back one day
    else:
        prev_date = today - pd.Timedelta(days=2)

    return prev_date.strftime('%Y-%m-%d')


# Define global styles
styles = {
    'page': {
        'backgroundColor': '#f5f7fa',
        'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif',
        'padding': '30px',
        'display': 'flex',
        'flexDirection': 'column',
        'justifyContent': 'flex-start',
        'alignItems': 'center',
        'height': '100vh',
        'width': '100vw',
        'marginTop': '20px'
    },
    'header': {
        'backgroundColor': '#1e3a8a',
        'color': '#ffffff',
        'padding': '20px',
        'borderRadius': '8px',
        'marginBottom': '20px',
        'boxShadow': '0 2px 4px rgba(0,0,0,0.1)',
        'width': '100%'
    },
    'title': {
        'margin': '0',
        'marginBottom': '5px'
    },
    'subtitle': {
        'margin': '0',
        'fontWeight': 'normal'
    },
    'panel': {
        'backgroundColor': '#ffffff',
        'padding': '20px',
        'borderRadius': '8px',
        'boxShadow': '0 1px 3px rgba(0,0,0,0.1)',
        'marginBottom': '20px',
        'width': '100%'
    },
    'label': {
        'color': '#1e3a8a',
        'fontWeight': 'bold',
        'marginRight': '10px',
        'display': 'inline-block',
        'verticalAlign': 'middle'
    },
    'dropdown': {
        'display': 'inline-block',
        'verticalAlign': 'middle'
    },
    'button': {
        'padding': '8px 20px',
        'backgroundColor': '#10b981',
        'color': '#ffffff',
        'border': 'none',
        'borderRadius': '4px',
        'cursor': 'pointer',
        'fontSize': '14px',
        'fontWeight': 'bold'
    },
    'separator': {
        'width': '100%',
        'borderTop': '1px solid #e5e7eb',
        'marginTop': '10px',
        'marginBottom': '20px'
    }
}

# Modified layout for slope analysis
layout = html.Div(
    style=styles['page'],
    children=[
        # Simple trigger store - just to signal refresh
        dcc.Store(id='refresh-trigger', data=0, storage_type='memory'),

        # Store for selected Brent configuration
        dcc.Store(id='selected-brent-config', data='B301', storage_type='memory'),

        # Controls panel
        html.Div(
            style=styles['panel'],
            children=[
                html.Div(
                    style={'textAlign': 'left', 'marginTop': '20px', 'marginBottom': '20px'},
                    children=[
                        # Slope products dropdown (hardcoded for now)
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Slopes:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='slope-product-dropdown',
                                    options=[
                                        {'label': 'JKM/Brent', 'value': 'JKM/Brent'}
                                    ],
                                    value=['JKM/Brent'],
                                    multi=True,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '200px',
                                        'marginRight': '20px'
                                    }
                                ),
                            ]
                        ),
                        # JKM discount input
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "JKM discount:",
                                    style=styles['label']
                                ),
                                dcc.Input(
                                    id='jkm-discount-input',
                                    type='number',
                                    value=0,
                                    placeholder='0.0',
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '100px',
                                        'marginRight': '20px',
                                        'padding': '5px',
                                        'fontSize': '14px'
                                    }
                                ),
                            ]
                        ),
                        # Brent index selection
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Brent Index:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='brent-preset-dropdown',
                                    options=[
                                        {'label': 'Brent', 'value': 'spot'},
                                        {'label': '101 (1M avg, no lag)', 'value': '101'},
                                        {'label': '301 (3M avg, no lag)', 'value': '301'},
                                        {'label': '311 (3M avg, 1M lag)', 'value': '311'},
                                        {'label': '601 (6M avg, no lag)', 'value': '601'},
                                        {'label': 'Custom...', 'value': 'custom'}
                                    ],
                                    value='301',  # Default to 301
                                    clearable=False,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '200px',
                                        'marginRight': '20px'
                                    }
                                ),
                            ]
                        ),
                        # Custom Brent inputs (hidden by default)
                        html.Div(
                            id='brent-custom-inputs',
                            style={'display': 'none'},
                            children=[
                                html.Label("Lag (months):", style={'marginRight': '5px', 'color': '#1e3a8a', 'fontWeight': 'bold'}),
                                dcc.Input(
                                    id='brent-lag-input',
                                    type='number',
                                    min=0,
                                    max=12,
                                    value=3,
                                    style={'width': '60px', 'marginRight': '20px', 'padding': '5px'}
                                ),
                                html.Label("Window (months):", style={'marginRight': '5px', 'color': '#1e3a8a', 'fontWeight': 'bold'}),
                                dcc.Input(
                                    id='brent-window-input',
                                    type='number',
                                    min=1,
                                    max=12,
                                    value=3,
                                    style={'width': '60px', 'marginRight': '20px', 'padding': '5px'}
                                )
                            ]
                        ),
                        # Comparison date picker
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Compare to date:",
                                    style=styles['label']
                                ),
                                dcc.DatePickerSingle(
                                    id='slope-comparison-date-picker',
                                    display_format='YYYY-MM-DD',
                                    with_portal=True,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'marginRight': '20px'
                                    }
                                ),
                            ]
                        ),
                        # Grouping dropdown
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Group by:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='slope-grouping-dropdown',
                                    options=[
                                        {'label': 'Monthly', 'value': 'monthly'},
                                        {'label': 'Quarterly', 'value': 'quarterly'},
                                        {'label': 'Season', 'value': 'season'},
                                        {'label': 'Calendar', 'value': 'calendar'}
                                    ],
                                    value='calendar',
                                    clearable=False,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '150px',
                                        'marginRight': '20px'
                                    }
                                ),
                            ]
                        ),
                        # View mode toggle
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "View mode:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='view-mode-dropdown',
                                    options=[
                                        {'label': 'Strips', 'value': 'strips'},
                                        {'label': 'Rolling', 'value': 'rolling'}
                                    ],
                                    value='strips',
                                    clearable=False,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '120px',
                                        'marginRight': '20px'
                                    }
                                ),
                            ]
                        ),
                        # Rolling periods control (visible only in rolling mode)
                        html.Div(
                            id='rolling-periods-control',
                            style={'display': 'none'},
                            children=[
                                html.Label(
                                    "Rolling periods:",
                                    style=styles['label']
                                ),
                                dcc.Input(
                                    id='rolling-periods-input',
                                    type='number',
                                    min=1,
                                    max=10,
                                    value=5,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '80px',
                                        'marginRight': '20px',
                                        'padding': '5px',
                                        'fontSize': '14px'
                                    }
                                ),
                            ]
                        ),
                    ]
                ),
            ]
        ),
        # Graphs panel
        html.Div(
            style=styles['panel'],
            children=[
                html.Div(
                    style={
                        'display': 'flex',
                        'justifyContent': 'space-between',
                        'alignItems': 'center',
                        'marginBottom': '15px',
                        'paddingBottom': '10px',
                        'borderBottom': '1px solid #e5e7eb'
                    },
                    children=[
                        html.Div(
                            "Slope Charts",
                            style={
                                'color': '#1e3a8a',
                                'fontWeight': 'bold'
                            }
                        ),
                        html.Button(
                            "Download Data (Excel)",
                            id='download-data-button',
                            style={
                                'padding': '8px 16px',
                                'backgroundColor': '#10b981',
                                'color': 'white',
                                'border': 'none',
                                'borderRadius': '4px',
                                'cursor': 'pointer',
                                'fontSize': '14px',
                                'fontWeight': 'bold'
                            }
                        )
                    ]
                ),
                # Store for slope data to be downloaded
                dcc.Store(id='slope-data-store', storage_type='memory'),
                # Download component
                dcc.Download(id='download-dataframe-xlsx'),
                html.Div(
                    id='slope-graphs-container',
                    style={
                        'width': '100%',
                        'backgroundColor': '#fafafa',
                        'borderRadius': '4px',
                        'marginBottom': '30px'
                    }
                )
            ]
        ),
        # Separator
        html.Hr(style=styles['separator']),
        # Tables panel
        html.Div(
            style=styles['panel'],
            children=[
                html.Div(
                    style={
                        'color': '#1e3a8a',
                        'fontWeight': 'bold',
                        'marginBottom': '15px',
                        'paddingBottom': '10px',
                        'borderBottom': '1px solid #e5e7eb'
                    },
                    children="Slope Data Tables"
                ),
                html.Div(
                    id='slope-tables-container',
                    style={'width': '100%'}
                )
            ]
        ),
        # Separator
        html.Hr(style=styles['separator']),
        # Contract Details Inspector Panel
        html.Div(
            style=styles['panel'],
            children=[
                html.Div(
                    style={
                        'color': '#1e3a8a',
                        'fontWeight': 'bold',
                        'marginBottom': '15px',
                        'paddingBottom': '10px',
                        'borderBottom': '1px solid #e5e7eb'
                    },
                    children="Contract Details Inspector (Latest Trade Date)"
                ),
                html.Div(
                    id='contract-details-container',
                    style={'width': '100%'}
                )
            ]
        )
    ]
)


# SIMPLIFIED: Data refresh callback that updates global variable and triggers refresh
@dash.callback(
    Output('refresh-trigger', 'data'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=False
)
def refresh_data_and_ui(n_clicks):
    """Refresh data and update UI elements"""
    global df_options

    try:
        # Load data
        df_options = load_and_process_data()
        return n_clicks or 0

    except Exception as e:
        print(f"Error loading data: {str(e)}")
        return n_clicks or 0


# Initialize comparison date picker (previous business date)
@dash.callback(
    Output('slope-comparison-date-picker', 'date'),
    Input('refresh-trigger', 'data'),
    prevent_initial_call=False
)
def init_slope_comparison_date(refresh_trigger):
    return get_previous_business_date()


# Toggle custom Brent inputs visibility
@dash.callback(
    Output('brent-custom-inputs', 'style'),
    Input('brent-preset-dropdown', 'value')
)
def toggle_custom_inputs(preset_value):
    """Show/hide custom inputs based on preset selection"""
    if preset_value == 'custom':
        return {'display': 'inline-block', 'marginLeft': '10px'}
    else:
        return {'display': 'none'}


# Toggle rolling periods control visibility
@dash.callback(
    Output('rolling-periods-control', 'style'),
    Input('view-mode-dropdown', 'value')
)
def toggle_rolling_periods_control(view_mode):
    """Show/hide rolling periods control based on view mode"""
    if view_mode == 'rolling':
        return {'display': 'inline-block', 'marginLeft': '10px'}
    else:
        return {'display': 'none'}


# Parse Brent index selection and update store
@dash.callback(
    Output('selected-brent-config', 'data'),
    [Input('brent-preset-dropdown', 'value'),
     Input('brent-lag-input', 'value'),
     Input('brent-window-input', 'value')]
)
def parse_brent_selection(preset, custom_lag, custom_window):
    """
    Parse user selection and return contract code to use for filtering

    Parameters:
    -----------
    preset : str
        Preset value from dropdown ('spot', '101', '301', '311', '601', 'custom')
    custom_lag : int
        Custom lag months (only used if preset='custom')
    custom_window : int
        Custom window months (only used if preset='custom')

    Returns:
    --------
    str : Contract code like 'Brent' (spot), 'B101', 'B301', 'B311', 'B601', or custom
    """
    if preset == 'spot':
        return 'Brent'  # Use original spot Brent
    elif preset == 'custom':
        # For custom, generate code in format BXYZ (X=window, Y=lag, Z=delivery months)
        window = custom_window if custom_window is not None else 3
        lag = custom_lag if custom_lag is not None else 0
        return f'B{window}{lag}1'
    else:
        # Preset like '101', '301', '311', '601'
        return f'B{preset}'


# ============================================================================
# ROLLING CONTRACTS HELPER FUNCTIONS
# ============================================================================

def calculate_year_rolling_label(trade_date, maturity_date):
    """
    Calculate rolling year label (Y+1, Y+2, etc.)

    Parameters:
    -----------
    trade_date : datetime
        The observation date
    maturity_date : datetime
        The contract delivery period

    Returns:
    --------
    str : Rolling label like 'Y+1', 'Y+2', etc.

    Example:
    --------
    trade_date = 2024-03-15, maturity_date = 2025-xx-xx → 'Y+1' (next year)
    trade_date = 2024-03-15, maturity_date = 2026-xx-xx → 'Y+2' (2 years ahead)
    """
    try:
        years_ahead = maturity_date.year - trade_date.year
        if years_ahead < 0:
            return None  # Historical contract, skip
        return f'Y+{years_ahead}'
    except:
        return None


def calculate_month_rolling_label(trade_date, maturity_date):
    """
    Calculate rolling month label (M+1, M+2, etc.)

    Parameters:
    -----------
    trade_date : datetime
        The observation date
    maturity_date : datetime
        The contract delivery period

    Returns:
    --------
    str : Rolling label like 'M+1', 'M+2', etc.

    Example:
    --------
    trade_date = 2024-03-15, maturity_date = 2024-04-01 → 'M+1' (next month)
    trade_date = 2024-03-15, maturity_date = 2024-05-01 → 'M+2' (2 months ahead)
    """
    try:
        months_ahead = (maturity_date.year - trade_date.year) * 12 + (maturity_date.month - trade_date.month)
        if months_ahead < 0:
            return None  # Historical contract, skip
        return f'M+{months_ahead}'
    except:
        return None


def calculate_quarter_rolling_label(trade_date, maturity_date):
    """
    Calculate rolling quarter label (Q+1, Q+2, etc.)

    Parameters:
    -----------
    trade_date : datetime
        The observation date
    maturity_date : datetime
        The contract delivery period

    Returns:
    --------
    str : Rolling label like 'Q+1', 'Q+2', etc.

    Example:
    --------
    trade_date = 2024-03-15 (Q1), maturity_date in Q2-2024 → 'Q+1' (next quarter)
    trade_date = 2024-03-15 (Q1), maturity_date in Q3-2024 → 'Q+2' (2 quarters ahead)
    """
    try:
        trade_quarter = (trade_date.year * 4) + ((trade_date.month - 1) // 3)
        maturity_quarter = (maturity_date.year * 4) + ((maturity_date.month - 1) // 3)
        quarters_ahead = maturity_quarter - trade_quarter
        if quarters_ahead < 0:
            return None  # Historical contract, skip
        return f'Q+{quarters_ahead}'
    except:
        return None


def calculate_season_rolling_label(trade_date, maturity_date):
    """
    Calculate rolling season label (S+1, S+2, etc.)

    Season definition:
    - Summer: May-September (months 5-9)
    - Winter: October-April (months 10-12, 1-4)

    Parameters:
    -----------
    trade_date : datetime
        The observation date
    maturity_date : datetime
        The contract delivery period

    Returns:
    --------
    str : Rolling label like 'S+1', 'S+2', etc.

    Example:
    --------
    trade_date = 2024-03-15 (Winter), maturity_date in Summer-2024 → 'S+1' (next season)
    trade_date = 2024-03-15 (Winter), maturity_date in Winter-2024 → 'S+2' (2 seasons ahead)
    """
    try:
        def get_season_number(date):
            """Calculate absolute season number from a date"""
            year = date.year
            month = date.month
            # Summer = May-Sep (months 5-9)
            # Winter = Oct-Apr (months 10-12, 1-4)
            if 5 <= month <= 9:
                # Summer season
                return year * 2
            else:
                # Winter season
                # Oct-Dec belongs to current year's winter (season 2)
                # Jan-Apr belongs to previous year's winter (season 2)
                if month >= 10:
                    return year * 2 + 1
                else:
                    return (year - 1) * 2 + 1

        trade_season = get_season_number(trade_date)
        maturity_season = get_season_number(maturity_date)
        seasons_ahead = maturity_season - trade_season

        if seasons_ahead < 0:
            return None  # Historical contract, skip
        return f'S+{seasons_ahead}'
    except:
        return None


def calculate_rolling_contracts(slope_data, grouping_mode, num_periods=5):
    """
    Convert slope data from strips view to rolling contracts view

    Parameters:
    -----------
    slope_data : pd.DataFrame
        Calculated slope data with columns: trade_date, strip, slope_percentage, etc.
    grouping_mode : str
        Grouping mode ('monthly', 'quarterly', 'season', 'calendar')
    num_periods : int
        Number of rolling periods to display (1-10)

    Returns:
    --------
    pd.DataFrame : Slope data with rolling labels instead of strips

    Process:
    --------
    For each row in slope_data:
        1. Calculate rolling label based on trade_date and strip's maturity_date
        2. Replace strip with rolling label
        3. Filter to keep only first N rolling periods

    Example:
    --------
    Input (Strips view):
        trade_date='2024-03-15', strip='2025', slope=150
        trade_date='2024-03-15', strip='2026', slope=145
        trade_date='2024-04-01', strip='2025', slope=148

    Output (Rolling view, calendar mode):
        trade_date='2024-03-15', strip='Y+1', slope=150  (2025 is next year)
        trade_date='2024-03-15', strip='Y+2', slope=145  (2026 is 2 years ahead)
        trade_date='2024-04-01', strip='Y+1', slope=148  (2025 is still next year)
    """
    try:
        df = slope_data.copy()

        # Parse maturity_date from strip
        if grouping_mode == 'monthly':
            # Strip format: 'Jan-25'
            df['maturity_date'] = pd.to_datetime(df['strip'], format='%b-%y', errors='coerce')
        elif grouping_mode == 'quarterly':
            # Strip format: '2025-Q1'
            df['maturity_date'] = pd.to_datetime(df['strip'].str.replace(r'-Q(\d)', r'-\1', regex=True) + '-01',
                                                 format='%Y-%m-%d', errors='coerce')
        elif grouping_mode == 'season':
            # Strip format: '2025-Summer' or '2025-Winter'
            def parse_season(season_str):
                try:
                    year, season = season_str.split('-')
                    year = int(year)
                    if season == 'Summer':
                        return pd.Timestamp(year=year, month=7, day=1)  # Mid-summer
                    else:  # Winter
                        return pd.Timestamp(year=year, month=1, day=1)  # Mid-winter
                except:
                    return None
            df['maturity_date'] = df['strip'].apply(parse_season)
        elif grouping_mode == 'calendar':
            # Strip format: '2025'
            df['maturity_date'] = pd.to_datetime(df['strip'] + '-01-01', format='%Y-%m-%d', errors='coerce')

        # Ensure trade_date is datetime
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')

        # Drop rows with invalid dates
        df = df.dropna(subset=['trade_date', 'maturity_date'])

        # Calculate rolling labels based on grouping mode
        if grouping_mode == 'calendar':
            df['rolling_label'] = df.apply(lambda row: calculate_year_rolling_label(row['trade_date'], row['maturity_date']), axis=1)
        elif grouping_mode == 'monthly':
            df['rolling_label'] = df.apply(lambda row: calculate_month_rolling_label(row['trade_date'], row['maturity_date']), axis=1)
        elif grouping_mode == 'quarterly':
            df['rolling_label'] = df.apply(lambda row: calculate_quarter_rolling_label(row['trade_date'], row['maturity_date']), axis=1)
        elif grouping_mode == 'season':
            df['rolling_label'] = df.apply(lambda row: calculate_season_rolling_label(row['trade_date'], row['maturity_date']), axis=1)

        # Drop rows where rolling label couldn't be calculated (historical contracts)
        df = df.dropna(subset=['rolling_label'])

        # Extract numeric offset from rolling label (e.g., 'Y+2' → 2)
        df['rolling_offset'] = df['rolling_label'].str.extract(r'\+(\d+)').astype(int)

        # Filter to keep only first N periods (0 to num_periods-1)
        df = df[df['rolling_offset'] < num_periods]

        # Replace strip with rolling label
        df['original_strip'] = df['strip']  # Keep original for reference
        df['strip'] = df['rolling_label']

        # Drop temporary columns
        df = df.drop(columns=['maturity_date', 'rolling_label', 'rolling_offset'])

        return df

    except Exception as e:
        print(f"Error calculating rolling contracts: {e}")
        import traceback
        traceback.print_exc()
        return slope_data  # Return original data on error

# ============================================================================


def slope_group_data_by_period(data, grouping_mode):
    """Group price data by different time periods based on maturity_date (contract delivery period)."""
    try:
        data = data.copy()

        # Parse maturity_date from strip field if it exists, otherwise use expiration_date
        if 'strip' in data.columns:
            # Strip field format is typically 'MMM-YY' like 'Jan-25'
            data['maturity_date'] = pd.to_datetime(data['strip'], format='%b-%y', errors='coerce')
        elif 'maturity_date' not in data.columns:
            # Fall back to expiration_date if maturity_date doesn't exist
            data['maturity_date'] = pd.to_datetime(data['expiration_date'], errors='coerce')
        else:
            data['maturity_date'] = pd.to_datetime(data['maturity_date'], errors='coerce')

        data['trade_date'] = pd.to_datetime(data['trade_date'], errors='coerce')
        data = data.dropna(subset=['maturity_date', 'trade_date'])

        if grouping_mode == 'monthly':
            data['period'] = data['maturity_date'].dt.strftime('%b-%y')
            return data

        elif grouping_mode == 'quarterly':
            data['quarter'] = data['maturity_date'].dt.quarter
            data['year'] = data['maturity_date'].dt.year
            data['period'] = data.apply(lambda x: f"{x['year']}-Q{x['quarter']}", axis=1)

            grouped = data.groupby(['product', 'contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        elif grouping_mode == 'season':
            def get_season(date):
                if pd.isna(date):
                    return None
                month = date.month
                year = date.year
                if 5 <= month <= 9:
                    return f"{year}-Summer"
                else:
                    return f"{year}-Winter"

            data['period'] = data['maturity_date'].apply(get_season)
            data = data.dropna(subset=['period'])

            grouped = data.groupby(['product', 'contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        elif grouping_mode == 'calendar':
            # For calendar grouping, use maturity_date year (contract delivery year)
            data['period'] = data['maturity_date'].dt.year.astype(str)

            grouped = data.groupby(['product', 'contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        data['period'] = data['maturity_date'].dt.strftime('%b-%y')
        return data

    except Exception as e:
        print(f"Error in group_data_by_period: {e}")
        if 'period' not in data.columns:
            data['period'] = 'unknown'
        return data


def slope_create_strip_identifier(expiration_date, format_type='month_year'):
    """Create strip identifier from expiration date"""
    try:
        if pd.isna(expiration_date):
            return None

        date = pd.to_datetime(expiration_date)

        if format_type == 'month_year':
            # Format: Sep-34 or 9/1/2034
            month_name = date.strftime('%b')
            year_short = date.strftime('%y')
            return f"{month_name}-{year_short}"

        return None
    except:
        return None


# ============================================================================
# BRENT INDEX CALCULATION FUNCTIONS
# ============================================================================

def get_averaging_window(maturity_date, lag_months, window_months):
    """
    Calculate the start and end dates for Brent averaging window

    Naming Convention XYZ:
    - X = Number of months in averaging window (PRECEDING delivery)
    - Y = Additional lag months (gap between averaging window end and delivery)
    - Z = Always 1 (for one month of delivery)

    The averaging window always PRECEDES the delivery month.
    - 0 lag = averaging window ends right before delivery (no gap)
    - 1 lag = 1 month gap between averaging window end and delivery

    Parameters:
    -----------
    maturity_date : datetime
        The LNG delivery month
    lag_months : int
        Additional gap between averaging window end and delivery month
    window_months : int
        Number of months in the averaging window (counting backward from window end)

    Returns:
    --------
    tuple : (window_start_date, window_end_date)

    Examples:
    ---------
    For Apr-26 delivery (maturity_date = 2026-04-01):

    301 (3-month avg, 0-month lag):
        window_months=3, lag_months=0
        → Window END: Apr-26 - 1 - 0 = Mar-26 (M-1, no gap)
        → Window START: Mar-26 - (3-1) = Jan-26 (M-3)
        → Window: Jan-26 to Mar-26 (3 months BEFORE delivery)

    311 (3-month avg, 1-month lag):
        window_months=3, lag_months=1
        → Window END: Apr-26 - 1 - 1 = Feb-26 (M-2, 1 month gap)
        → Window START: Feb-26 - (3-1) = Dec-25 (M-4)
        → Window: Dec-25 to Feb-26 (with 1 month gap to delivery)

    601 (6-month avg, 0-month lag):
        window_months=6, lag_months=0
        → Window END: Apr-26 - 1 - 0 = Mar-26 (M-1, no gap)
        → Window START: Mar-26 - (6-1) = Oct-25 (M-6)
        → Window: Oct-25 to Mar-26 (6 months BEFORE delivery)

    101 (1-month avg, 0-month lag):
        window_months=1, lag_months=0
        → Window END: Apr-26 - 1 - 0 = Mar-26 (M-1, no gap)
        → Window START: Mar-26 - (1-1) = Mar-26 (M-1)
        → Window: Mar-26 to Mar-26 (1 month BEFORE delivery)
    """
    from dateutil.relativedelta import relativedelta

    # Calculate end of averaging window
    # Formula: delivery month - 1 (to get month before) - lag_months (additional gap)
    window_end = maturity_date - relativedelta(months=1) - relativedelta(months=lag_months)

    # Calculate start of averaging window
    # Go back (window_months - 1) from the window end
    window_start = window_end - relativedelta(months=window_months - 1)

    # Get first day of window start month
    window_start_date = window_start.replace(day=1)

    # Get last day of window end month
    window_end_date = (window_end.replace(day=1) + relativedelta(months=1) - relativedelta(days=1))

    return window_start_date, window_end_date


def calculate_lagged_brent_index(df_brent, trade_date, maturity_date, lag_months, window_months):
    """
    Generic function to calculate lagged Brent index with any lag/window combination

    Parameters:
    -----------
    df_brent : pd.DataFrame
        Raw Brent data with columns: trade_date, settlement_price, maturity_date
    trade_date : datetime
        The observation date (when we're looking at the price)
    maturity_date : datetime
        The delivery month for the LNG contract
    lag_months : int
        Number of months to lag from delivery (e.g., 3, 6, 12)
    window_months : int
        Number of months to average (e.g., 1, 3, 6)

    Returns:
    --------
    float : The calculated Brent index price, or None if insufficient data

    Example:
    --------
    For January 2025 LNG delivery with lag_months=3, window_months=3:
    - Reference month: Jan 2025 - 3 months = Oct 2024
    - Window end: Oct 2024
    - Window start: Oct 2024 - (3-1) months = Aug 2024
    - Find all Brent contracts with maturity_date in Aug-Oct 2024
    - Average their settlement prices as observed on trade_date
    """
    try:
        # Get averaging window dates based on LNG maturity_date
        window_start_date, window_end_date = get_averaging_window(maturity_date, lag_months, window_months)

        # Filter Brent data:
        # - Must be observed on or before the trade_date (no forward-looking)
        # - Must have maturity_date within the averaging window
        # - Must be original spot Brent (contract = 'B' or 'Brent')
        mask = (
            (df_brent['trade_date'] == trade_date) &
            (df_brent['maturity_date'] >= window_start_date) &
            (df_brent['maturity_date'] <= window_end_date) &
            (df_brent['contract'].isin(['B', 'Brent']))
        )

        filtered_brent = df_brent[mask]

        # If no data available in window, return None
        if filtered_brent.empty:
            return None

        # Calculate average price across all Brent contracts in the window
        avg_price = filtered_brent['settlement_price'].mean()

        return avg_price

    except Exception as e:
        print(f"Error calculating lagged Brent index: {e}")
        return None


def generate_brent_index_on_demand(df_combined, brent_contract, df_grouped_jkm):
    """
    Generate a single Brent index on-demand, only for the periods/strips needed in the chart

    OPTIMIZED: Uses vectorized operations instead of row iteration for 10-100x performance boost

    Parameters:
    -----------
    df_combined : pd.DataFrame
        Combined dataframe with JKM and original Brent data
    brent_contract : str
        Contract code like 'B31', 'B33', 'B61', 'B63'
    df_grouped_jkm : pd.DataFrame
        Already grouped JKM data - we'll match only these (trade_date, strip) combinations

    Returns:
    --------
    pd.DataFrame : Synthetic Brent index data for only the needed combinations
    """
    try:
        # If spot Brent is requested, process it to ensure strip format consistency
        if brent_contract == 'Brent':
            df_spot = df_combined[df_combined['contract'] == 'Brent'].copy()

            # Ensure dates are datetime
            df_spot['trade_date'] = pd.to_datetime(df_spot['trade_date'], errors='coerce')

            # Parse maturity_date from strip column and reformat strip to MMM-YY
            if 'strip' in df_spot.columns:
                df_spot['maturity_date'] = pd.to_datetime(df_spot['strip'], errors='coerce')
                df_spot['strip'] = df_spot['maturity_date'].dt.strftime('%b-%y')

            return df_spot

        # Parse window and lag from contract code
        # Format: BXYZ where X=window months, Y=lag months, Z=delivery months (always 1)
        # Examples: B301 = 3-month window, 0-month lag, 1 delivery
        #           B311 = 3-month window, 1-month lag, 1 delivery
        #           B601 = 6-month window, 0-month lag, 1 delivery
        if not brent_contract.startswith('B') or len(brent_contract) < 3:
            print(f"Invalid Brent contract format: {brent_contract}")
            return pd.DataFrame()

        try:
            window_months = int(brent_contract[1])
            lag_months = int(brent_contract[2])
        except (ValueError, IndexError):
            print(f"Could not parse window/lag from contract: {brent_contract}")
            return pd.DataFrame()

        print(f"\nGenerating {brent_contract} on-demand (lag={lag_months}, window={window_months})...")

        # Filter for original Brent data only
        df_brent_raw = df_combined[df_combined['contract'].isin(['B', 'Brent'])].copy()

        if df_brent_raw.empty:
            print("No Brent data found")
            return pd.DataFrame()

        # Ensure dates are datetime
        df_brent_raw['trade_date'] = pd.to_datetime(df_brent_raw['trade_date'], errors='coerce')

        # Parse maturity_date from strip column
        if 'strip' in df_brent_raw.columns:
            df_brent_raw['maturity_date'] = pd.to_datetime(df_brent_raw['strip'], errors='coerce')
            df_brent_raw['strip'] = df_brent_raw['maturity_date'].dt.strftime('%b-%y')
        else:
            print("ERROR: No strip column found in Brent data")
            return pd.DataFrame()

        # Get only the (trade_date, strip) combinations that exist in JKM data
        # For monthly grouping, this will have ALL trade_dates for each strip
        # For other groupings, this will be aggregated
        needed_combinations = df_grouped_jkm[['trade_date', 'strip']].drop_duplicates().copy()
        print(f"  Need to calculate for {len(needed_combinations)} combinations")
        print(f"  Unique strips: {needed_combinations['strip'].nunique()}")
        print(f"  Unique trade_dates: {needed_combinations['trade_date'].nunique()}")
        print(f"  Sample JKM combinations:")
        print(needed_combinations.head(10))

        # Parse maturity_date for JKM strips
        needed_combinations['maturity_date'] = pd.to_datetime(needed_combinations['strip'], format='%b-%y', errors='coerce')
        needed_combinations = needed_combinations.dropna(subset=['maturity_date'])
        print(f"  After parsing maturity_date: {len(needed_combinations)} combinations")
        print(f"  Sample parsed maturity_dates:")
        print(needed_combinations[['strip', 'maturity_date']].head())

        # OPTIMIZATION: Calculate averaging windows for all combinations at once
        from dateutil.relativedelta import relativedelta

        needed_combinations['window_start'] = needed_combinations['maturity_date'].apply(
            lambda x: (x - relativedelta(months=lag_months) - relativedelta(months=window_months - 1)).replace(day=1)
        )
        needed_combinations['window_end'] = needed_combinations['maturity_date'].apply(
            lambda x: (x - relativedelta(months=lag_months)).replace(day=1) + relativedelta(months=1) - relativedelta(days=1)
        )
        print(f"  Sample averaging windows:")
        print(needed_combinations[['strip', 'maturity_date', 'window_start', 'window_end']].head())

        # DEBUG: Show Brent data info
        print(f"  Brent raw data: {len(df_brent_raw)} rows")
        print(f"  Brent unique trade_dates: {df_brent_raw['trade_date'].nunique()}")
        print(f"  Brent maturity_date range: {df_brent_raw['maturity_date'].min()} to {df_brent_raw['maturity_date'].max()}")
        print(f"  Sample Brent data:")
        print(df_brent_raw[['trade_date', 'strip', 'maturity_date', 'settlement_price']].head())

        # OPTIMIZATION: Use merge_asof for efficient time-based join
        # Instead of cartesian product, merge on trade_date first (exact match)
        merged = needed_combinations.merge(
            df_brent_raw[['trade_date', 'maturity_date', 'settlement_price']],
            on='trade_date',
            suffixes=('_jkm', '_brent')
        )
        print(f"  After merge on trade_date: {len(merged)} rows")
        if len(merged) > 0:
            print(f"  Sample merged data:")
            print(merged[['trade_date', 'strip', 'maturity_date_jkm', 'maturity_date_brent', 'window_start', 'window_end']].head())

        # Filter: Brent maturity must be within the averaging window
        filtered = merged[
            (merged['maturity_date_brent'] >= merged['window_start']) &
            (merged['maturity_date_brent'] <= merged['window_end'])
        ]
        print(f"  After filtering by averaging window: {len(filtered)} rows")
        if len(filtered) > 0:
            print(f"  Sample filtered data:")
            print(filtered[['trade_date', 'strip', 'maturity_date_brent', 'window_start', 'window_end', 'settlement_price']].head())

        # Group by JKM combination and calculate average Brent price
        result = filtered.groupby(['trade_date', 'strip', 'maturity_date_jkm']).agg({
            'settlement_price': 'mean'
        }).reset_index()

        result.columns = ['trade_date', 'strip', 'maturity_date', 'settlement_price']

        # Add metadata columns
        result['product'] = 'Brent'
        result['contract'] = brent_contract
        result['contract_type'] = None
        result['expiration_date'] = None

        print(f"  {brent_contract}: {len(result)} calculated, {len(needed_combinations) - len(result)} skipped")

        return result[['trade_date', 'product', 'contract', 'strip', 'maturity_date', 'settlement_price', 'contract_type', 'expiration_date']]

    except Exception as e:
        print(f"Error generating Brent index on-demand: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


def generate_brent_indices(df_combined, lag_window_configs):
    """
    Generate synthetic Brent index data for multiple lag/window combinations

    Parameters:
    -----------
    df_combined : pd.DataFrame
        Combined dataframe with JKM and original Brent data
    lag_window_configs : list of tuples
        [(lag_months, window_months), ...]
        Example: [(3, 1), (3, 3), (6, 1), (6, 3)]

    Returns:
    --------
    pd.DataFrame : Combined dataframe with original + synthetic Brent indices

    Process:
    --------
    For each (trade_date, strip) pair in original Brent data:
        For each (lag, window) configuration:
            Calculate lagged index price
            Create synthetic row with contract = f'B{lag}{window}'
    """
    try:
        print("Starting Brent index generation...")

        # Filter for original Brent data only
        df_brent_raw = df_combined[df_combined['contract'].isin(['B', 'Brent'])].copy()

        if df_brent_raw.empty:
            print("No Brent data found for index generation")
            return df_combined

        # Ensure dates are datetime
        df_brent_raw['trade_date'] = pd.to_datetime(df_brent_raw['trade_date'], errors='coerce')

        # Parse maturity_date from strip column (strip contains contract delivery date)
        if 'strip' in df_brent_raw.columns:
            # Strip from database is in date format - parse it directly as maturity_date
            df_brent_raw['maturity_date'] = pd.to_datetime(df_brent_raw['strip'], errors='coerce')
            print(f"  Parsed maturity_date from strip column")
            print(f"  Sample strips from DB: {df_brent_raw['strip'].head().tolist()}")
            print(f"  Sample maturity_dates: {df_brent_raw['maturity_date'].head().tolist()}")

            # Convert strip to standard "MMM-YY" format for consistency with rest of application
            df_brent_raw['strip'] = df_brent_raw['maturity_date'].dt.strftime('%b-%y')
            print(f"  Converted strips to standard format: {df_brent_raw['strip'].head().tolist()}")
        else:
            print(f"  ERROR: No strip column found in Brent data")
            return df_combined

        # Debug: Check for NaN values
        nan_count = df_brent_raw['maturity_date'].isna().sum()
        if nan_count > 0:
            print(f"  WARNING: {nan_count} rows have NaN maturity_date")
            sample_nan = df_brent_raw[df_brent_raw['maturity_date'].isna()][['strip', 'expiration_date']].head()
            print(f"  Sample rows with NaN maturity_date:\n{sample_nan}")

        # Get unique (trade_date, strip, maturity_date) combinations
        unique_combinations = df_brent_raw[['trade_date', 'strip', 'maturity_date']].drop_duplicates()
        print(f"  Found {len(unique_combinations)} unique combinations to process")

        synthetic_rows = []

        # Generate indices for each configuration
        for config in lag_window_configs:
            lag_months, window_months = config
            contract_code = f'B{lag_months}{window_months}'

            print(f"Calculating {contract_code} (lag={lag_months}, window={window_months})...")

            count_calculated = 0
            count_skipped = 0

            for _, row in unique_combinations.iterrows():
                trade_date = row['trade_date']
                maturity_date = row['maturity_date']
                strip = row['strip']

                # Skip if missing data
                if pd.isna(trade_date) or pd.isna(maturity_date):
                    count_skipped += 1
                    continue

                # Calculate lagged price
                lagged_price = calculate_lagged_brent_index(
                    df_brent_raw,
                    trade_date,
                    maturity_date,
                    lag_months,
                    window_months
                )

                if lagged_price is not None:
                    # Create synthetic row matching original Brent structure
                    synthetic_row = {
                        'trade_date': trade_date,
                        'product': 'Brent',
                        'contract': contract_code,
                        'strip': strip,
                        'maturity_date': maturity_date,
                        'settlement_price': lagged_price,
                        'contract_type': None,
                        'expiration_date': df_brent_raw[
                            (df_brent_raw['trade_date'] == trade_date) &
                            (df_brent_raw['strip'] == strip)
                        ]['expiration_date'].iloc[0] if 'expiration_date' in df_brent_raw.columns else None
                    }
                    synthetic_rows.append(synthetic_row)
                    count_calculated += 1
                else:
                    count_skipped += 1

            print(f"  {contract_code}: {count_calculated} calculated, {count_skipped} skipped (insufficient data)")

        # Create DataFrame from synthetic rows
        if synthetic_rows:
            df_synthetic = pd.DataFrame(synthetic_rows)

            # Combine original + synthetic
            df_result = pd.concat([df_combined, df_synthetic], ignore_index=True)

            print(f"Total rows before: {len(df_combined)}, after: {len(df_result)}")
            print(f"Added {len(df_synthetic)} synthetic Brent index rows")

            return df_result
        else:
            print("No synthetic indices could be calculated")
            return df_combined

    except Exception as e:
        print(f"Error generating Brent indices: {e}")
        import traceback
        traceback.print_exc()
        return df_combined


def validate_brent_indices(df_combined):
    """
    Validate generated Brent indices

    Returns:
    --------
    dict : Validation results with completeness percentages
    """
    try:
        spot_data = df_combined[df_combined['contract'].isin(['B', 'Brent'])]
        spot_count = len(spot_data)

        validation = {
            'spot_exists': spot_count > 0,
            'spot_count': spot_count,
            'completeness': {}
        }

        # Check each synthetic index
        for contract in df_combined['contract'].unique():
            if contract not in ['B', 'Brent', 'JKM']:
                contract_data = df_combined[df_combined['contract'] == contract]
                completeness_pct = (len(contract_data) / spot_count * 100) if spot_count > 0 else 0
                validation['completeness'][contract] = round(completeness_pct, 1)

        return validation

    except Exception as e:
        print(f"Error validating Brent indices: {e}")
        return {'error': str(e)}

# ============================================================================


def slope_calculate_slope_data(df, grouping_mode, jkm_discount=0, brent_contract='Brent'):
    """
    Calculate JKM/Brent slope for matching strips using all available historical data

    Parameters:
    -----------
    df : pd.DataFrame
        Combined dataframe with JKM and Brent data
    grouping_mode : str
        How to group data ('monthly', 'quarterly', 'season', 'calendar')
    jkm_discount : float
        Discount to apply to JKM prices
    brent_contract : str
        Which Brent contract to use ('Brent', 'B31', 'B33', 'B61', 'B63', etc.)

    Returns:
    --------
    pd.DataFrame : Calculated slopes with trade_date, strip, slope_percentage, etc.
    """
    try:
        # Process all available data (no date filtering)
        df = df.copy()
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
        df['expiration_date'] = pd.to_datetime(df['expiration_date'], errors='coerce')

        if df.empty:
            return pd.DataFrame()

        # KEY CHANGE: Get UNGROUPED JKM data first for Brent index generation
        df_jkm_ungrouped = df[df['product'] == 'JKM'].copy()

        if df_jkm_ungrouped.empty:
            return pd.DataFrame()

        # Create strip identifier for UNGROUPED data before generating Brent indices
        # This ensures we have strip column to extract (trade_date, strip) combinations
        # ALWAYS recreate strip from expiration_date to ensure correct format (MMM-YY)
        df_jkm_ungrouped['strip'] = df_jkm_ungrouped['expiration_date'].apply(slope_create_strip_identifier)
        print(f"DEBUG - Created strips for ungrouped JKM data")
        print(f"  Sample strips: {df_jkm_ungrouped['strip'].head(10).tolist()}")

        # Generate Brent index at MONTHLY level (before grouping)
        # This ensures we calculate B33 for each month, then group it later
        brent_data = generate_brent_index_on_demand(df, brent_contract, df_jkm_ungrouped)

        if brent_data.empty:
            print(f"No Brent data available for contract {brent_contract}")
            return pd.DataFrame()

        # NOW group both JKM and Brent by the specified period
        jkm_grouped = slope_group_data_by_period(df_jkm_ungrouped, grouping_mode)
        brent_grouped = slope_group_data_by_period(brent_data, grouping_mode)

        print(f"\nDEBUG - After grouping:")
        print(f"  JKM grouped: {len(jkm_grouped)} rows")
        print(f"  JKM unique trade_dates: {jkm_grouped['trade_date'].nunique()}")
        print(f"  Brent grouped: {len(brent_grouped)} rows")
        print(f"  Brent unique trade_dates: {brent_grouped['trade_date'].nunique()}")

        # Create strip identifiers for both
        if 'period' in jkm_grouped.columns:
            jkm_grouped['strip'] = jkm_grouped['period']
        else:
            jkm_grouped['strip'] = jkm_grouped['expiration_date'].apply(slope_create_strip_identifier)

        if 'period' in brent_grouped.columns:
            brent_grouped['strip'] = brent_grouped['period']
        else:
            brent_grouped['strip'] = brent_grouped['expiration_date'].apply(slope_create_strip_identifier)

        print(f"\nDEBUG - After strip creation:")
        print(f"  JKM unique (trade_date, strip): {jkm_grouped[['trade_date', 'strip']].drop_duplicates().shape[0]}")
        print(f"  Brent unique (trade_date, strip): {brent_grouped[['trade_date', 'strip']].drop_duplicates().shape[0]}")
        print(f"  Sample JKM data:")
        print(jkm_grouped[['trade_date', 'strip', 'settlement_price']].head(10))
        print(f"  Sample Brent data:")
        print(brent_grouped[['trade_date', 'strip', 'settlement_price']].head(10))

        # Apply JKM discount
        if jkm_discount != 0:
            jkm_grouped['settlement_price'] = jkm_grouped['settlement_price'] - jkm_discount

        # Merge on trade_date and strip to find matching pairs
        merged = pd.merge(
            jkm_grouped[['trade_date', 'strip', 'settlement_price']].rename(columns={'settlement_price': 'jkm_price'}),
            brent_grouped[['trade_date', 'strip', 'settlement_price']].rename(columns={'settlement_price': 'brent_price'}),
            on=['trade_date', 'strip'],
            how='inner'
        )

        if merged.empty:
            return pd.DataFrame()

        # Calculate slope as percentage: (JKM/Brent) * 100
        merged['slope_percentage'] = ((merged['jkm_price'] / merged['brent_price'])) * 100
        merged['slope_percentage'] = merged['slope_percentage'].round(2)  # Round to 2 decimals
        merged['slope_name'] = 'JKM/Brent'

        return merged[['trade_date', 'strip', 'slope_percentage', 'slope_name', 'jkm_price', 'brent_price']]

    except Exception as e:
        print(f"Error calculating slope data: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


# Update graphs callback - loads all historical data
@dash.callback(
    [Output('slope-graphs-container', 'children'),
     Output('slope-data-store', 'data')],
    [Input('slope-grouping-dropdown', 'value'),
     Input('slope-product-dropdown', 'value'),
     Input('jkm-discount-input', 'value'),
     Input('selected-brent-config', 'data'),
     Input('view-mode-dropdown', 'value'),
     Input('rolling-periods-input', 'value'),
     Input('refresh-trigger', 'data')],
    prevent_initial_call=True
)
def update_slope_graphs(grouping_mode, selected_slopes, jkm_discount, brent_contract, view_mode, num_rolling_periods, refresh_trigger):
    try:
        if not selected_slopes:
            return html.Div(), None

        # Use global DataFrame directly
        if df_options is None or df_options.empty:
            return html.Div(html.H4("No data available. Please click Refresh to load data.", style={'color': 'orange'})), None

        # Calculate slope data using all available historical data with selected Brent contract
        slope_data = slope_calculate_slope_data(df_options, grouping_mode, jkm_discount or 0, brent_contract)

        if slope_data.empty:
            return html.Div(html.H4("No slope data available for the selected parameters", style={'color': 'red'})), None

        # Apply rolling view transformation if needed
        if view_mode == 'rolling':
            num_periods = num_rolling_periods if num_rolling_periods else 5
            slope_data = calculate_rolling_contracts(slope_data, grouping_mode, num_periods)
            if slope_data.empty:
                return html.Div(html.H4("No rolling contract data available", style={'color': 'red'})), None

        # Get Brent index label for title
        brent_label_map = {
            'Brent': 'Brent',
            'B101': '101 (1M avg, no lag)',
            'B301': '301 (3M avg, no lag)',
            'B311': '311 (3M avg, 1M lag)',
            'B601': '601 (6M avg, no lag)'
        }
        brent_label = brent_label_map.get(brent_contract, brent_contract)

        graphs = []

        for slope_name in selected_slopes:
            if slope_name == 'JKM/Brent':
                fig = go.Figure()

                # Get unique strips and sort them
                strips = sorted(slope_data['strip'].unique())

                for strip in strips:
                    strip_data = slope_data[slope_data['strip'] == strip].sort_values('trade_date')

                    if not strip_data.empty:
                        fig.add_trace(go.Scatter(
                            x=strip_data['trade_date'],
                            y=strip_data['slope_percentage'],
                            mode='lines+markers',
                            name=f"{strip}",
                            line=dict(width=2)
                        ))

                # Calculate date range for initial zoom (last 180 days)
                max_date = slope_data['trade_date'].max()
                min_zoom_date = max_date - pd.Timedelta(days=180)

                # Build title with Brent index info and view mode
                view_label = "Rolling View" if view_mode == 'rolling' else "Strips View"
                title_parts = [f"JKM/Brent {brent_label} Slope - {grouping_mode.capitalize()} {view_label}"]
                if jkm_discount:
                    title_parts.append(f"JKM Discount: {jkm_discount}")

                fig.update_layout(
                    title={
                        'text': " | ".join(title_parts),
                        'y': 0.97,
                        'x': 0.5,
                        'xanchor': 'center',
                        'yanchor': 'top',
                        'font': {'size': 16}
                    },
                    xaxis_title="Trade Date",
                    yaxis_title="Slope (%)",
                    legend_title="Strip",
                    hovermode='x unified',
                    margin=dict(b=80, t=60, l=60, r=40),
                    xaxis=dict(
                        range=[min_zoom_date, max_date],
                        rangeslider=dict(visible=True),
                        type='date'
                    ),
                    yaxis=dict(
                        autorange=True,  # Auto-scale Y axis based on visible data
                        fixedrange=False  # Allow Y axis zooming
                    )
                )

                graphs.append(html.Div(
                    dcc.Graph(figure=fig, config={'displayModeBar': True}),
                    style={'width': '100%', 'marginBottom': '20px'}
                ))

        # Store slope data for download along with JKM and Brent data
        # Prepare data to store
        data_to_store = {
            'slope_data': slope_data.to_json(date_format='iso', orient='split'),
            'grouping_mode': grouping_mode,
            'brent_contract': brent_contract,
            'jkm_discount': jkm_discount or 0,
            'view_mode': view_mode
        }

        return (graphs if graphs else html.Div(html.H4("No graphs could be generated", style={'color': 'red'})),
                data_to_store)

    except Exception as e:
        print(f"Error in update_slope_graphs: {e}")
        return html.Div(html.H4(f"Error generating graphs: {str(e)}", style={'color': 'red'})), None


# Update tables callback - loads all historical data
@dash.callback(
    Output('slope-tables-container', 'children'),
    [Input('slope-grouping-dropdown', 'value'),
     Input('slope-product-dropdown', 'value'),
     Input('jkm-discount-input', 'value'),
     Input('slope-comparison-date-picker', 'date'),
     Input('selected-brent-config', 'data'),
     Input('view-mode-dropdown', 'value'),
     Input('rolling-periods-input', 'value'),
     Input('refresh-trigger', 'data')],
    prevent_initial_call=True
)
def update_slope_tables(grouping_mode, selected_slopes, jkm_discount, comparison_date, brent_contract, view_mode, num_rolling_periods, refresh_trigger):
    try:
        if not selected_slopes:
            return html.Div()

        # Use global DataFrame directly
        if df_options is None or df_options.empty:
            return html.Div(html.H4("No data available. Please click Refresh to load data.", style={'color': 'orange'}))

        # Calculate slope data using all available historical data with selected Brent contract
        slope_data = slope_calculate_slope_data(df_options, grouping_mode, jkm_discount or 0, brent_contract)

        if slope_data.empty:
            return html.Div(html.H4("No slope data available for the selected parameters", style={'color': 'red'}))

        # Apply rolling view transformation if needed
        if view_mode == 'rolling':
            num_periods = num_rolling_periods if num_rolling_periods else 5
            slope_data = calculate_rolling_contracts(slope_data, grouping_mode, num_periods)
            if slope_data.empty:
                return html.Div(html.H4("No rolling contract data available", style={'color': 'red'}))

        # Create pivot table for slope data
        try:
            # Get the latest trade date for each strip
            latest_data = slope_data.loc[slope_data.groupby('strip')['trade_date'].idxmax()]

            if latest_data.empty:
                return html.Div(html.H4("No latest slope data available", style={'color': 'red'}))

            # Get comparison data if comparison date is provided
            comparison_data = pd.DataFrame()
            if comparison_date:
                comparison_date_dt = pd.to_datetime(comparison_date)

                # Find closest date to comparison date for each strip
                comparison_rows = []
                for strip in slope_data['strip'].unique():
                    strip_data = slope_data[slope_data['strip'] == strip]
                    # Find the closest trade date to comparison date
                    closest_idx = (strip_data['trade_date'] - comparison_date_dt).abs().idxmin()
                    if closest_idx in strip_data.index:
                        comparison_rows.append(strip_data.loc[closest_idx])

                if comparison_rows:
                    comparison_data = pd.DataFrame(comparison_rows)

            # Create table showing latest slopes by strip
            table_data = latest_data[['strip', 'trade_date', 'slope_percentage', 'jkm_price', 'brent_price']].copy()
            table_data['trade_date'] = table_data['trade_date'].dt.strftime('%Y-%m-%d')
            table_data = table_data.round({'slope_percentage': 2, 'jkm_price': 2, 'brent_price': 2})

            # Add comparison columns if comparison data exists
            if not comparison_data.empty:
                # Merge comparison data
                comparison_summary = comparison_data[['strip', 'slope_percentage', 'trade_date']].copy()
                comparison_summary.columns = ['strip', 'comparison_slope', 'comparison_date']
                comparison_summary['comparison_date'] = pd.to_datetime(
                    comparison_summary['comparison_date']).dt.strftime('%Y-%m-%d')
                comparison_summary['comparison_slope'] = comparison_summary['comparison_slope'].round(2)

                table_data = table_data.merge(comparison_summary, on='strip', how='left')

                # Calculate change
                table_data['slope_change'] = table_data['slope_percentage'] - table_data['comparison_slope']
                table_data['slope_change'] = table_data['slope_change'].round(2)

                strip_column_name = "Rolling Contract" if view_mode == 'rolling' else "Strip"
                columns = [
                    {"name": strip_column_name, "id": "strip"},
                    {"name": "Latest Date", "id": "trade_date"},
                    {"name": "Latest Slope (%)", "id": "slope_percentage", "type": "numeric",
                     "format": {"specifier": ".2f"}},
                    {"name": "Comparison Date", "id": "comparison_date"},
                    {"name": "Comparison Slope (%)", "id": "comparison_slope", "type": "numeric",
                     "format": {"specifier": ".2f"}},
                    {"name": "Change (%)", "id": "slope_change", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": f"JKM Price{' (Discounted)' if jkm_discount else ''}", "id": "jkm_price",
                     "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Brent Price", "id": "brent_price", "type": "numeric", "format": {"specifier": ".2f"}}
                ]

                # Enhanced conditional styling for comparison
                style_data_conditional = [
                    {
                        'if': {'row_index': 'odd'},
                        'backgroundColor': 'rgb(248, 248, 248)'
                   },
                    {
                        'if': {
                            'column_id': 'slope_change',
                            'filter_query': '{slope_change} > 0'
                        },
                        'color': 'green',
                        'fontWeight': 'bold'
                    },
                    {
                        'if': {
                            'column_id': 'slope_change',
                            'filter_query': '{slope_change} < 0'
                       },
                        'color': 'red',
                        'fontWeight': 'bold'
                    }
                ]
            else:
                strip_column_name = "Rolling Contract" if view_mode == 'rolling' else "Strip"
                columns = [
                    {"name": strip_column_name, "id": "strip"},
                    {"name": "Latest Date", "id": "trade_date"},
                    {"name": "Slope (%)", "id": "slope_percentage", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": f"JKM Price{' (Discounted)' if jkm_discount else ''}", "id": "jkm_price",
                     "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Brent Price", "id": "brent_price", "type": "numeric", "format": {"specifier": ".2f"}}
                ]

                style_data_conditional = [
                    {
                        'if': {'row_index': 'odd'},
                        'backgroundColor': 'rgb(248, 248, 248)'
                    },
                    {
                        'if': {
                            'column_id': 'slope_percentage',
                            'filter_query': '{slope_percentage} > 0'
                        },
                        'color': 'green',
                        'fontWeight': 'bold'
                    },
                    {
                        'if': {
                            'column_id': 'slope_percentage',
                            'filter_query': '{slope_percentage} < 0'
                        },
                        'color': 'red',
                        'fontWeight': 'bold'
                    }
                ]

            # Build table title with view mode
            view_label = "Rolling Contract" if view_mode == 'rolling' else "Strip"
            table_title = f"Latest Slope Data by {view_label}" + (f" (vs {comparison_date})" if comparison_date else "")

            slope_table = html.Div([
                html.H4(table_title, style={'color': '#1e3a8a', 'marginBottom': '10px'}),
                dash_table.DataTable(
                    columns=columns,
                    data=table_data.to_dict('records'),
                    style_table={'overflowX': 'auto'},
                    style_cell={
                        'textAlign': 'center',
                        'padding': '8px',
                        'fontSize': 12,
                        'color': 'black'
                    },
                    style_cell_conditional=[
                        {'if': {'column_id': 'strip'},
                         'textAlign': 'left',
                         'fontWeight': 'bold'
                         }
                    ],
                    style_header={
                        'backgroundColor': 'rgb(230, 230, 230)',
                        'fontWeight': 'bold',
                        'textAlign': 'center',
                        'color': 'black'
                    },
                    style_data_conditional=style_data_conditional,
                    sort_action="native",
                    fill_width=False
                )
            ], style={'width': '100%'})

            return slope_table

        except Exception as e:
            print(f"Error creating slope table: {e}")
            return html.Div(html.H4(f"Error creating slope table: {str(e)}", style={'color': 'red'}))

    except Exception as e:
        print(f"Error in update_slope_tables: {e}")
        return html.Div(html.H4(f"Error generating tables: {str(e)}", style={'color': 'red'}))


# Download data callback - exports slope data to Excel
@dash.callback(
    Output('download-dataframe-xlsx', 'data'),
    Input('download-data-button', 'n_clicks'),
    State('slope-data-store', 'data'),
    prevent_initial_call=True
)
def download_slope_data(n_clicks, data_store):
    """Export slope data along with JKM and Brent data to Excel file"""
    try:
        if data_store is None:
            return None

        # Extract stored data
        slope_data_json = data_store.get('slope_data')
        grouping_mode = data_store.get('grouping_mode')
        brent_contract = data_store.get('brent_contract')
        jkm_discount = data_store.get('jkm_discount', 0)
        view_mode = data_store.get('view_mode', 'strips')

        if slope_data_json is None:
            return None

        # Parse JSON back to DataFrame
        slope_data = pd.read_json(slope_data_json, orient='split')

        if slope_data.empty:
            return None

        # Prepare data in long format with columns: trade_date, strip, slope, brent_index, jkm
        export_data = slope_data[['trade_date', 'strip', 'slope_percentage', 'brent_price', 'jkm_price']].copy()

        # Format dates
        export_data['trade_date'] = pd.to_datetime(export_data['trade_date']).dt.strftime('%Y-%m-%d')

        # Round values
        export_data['slope_percentage'] = export_data['slope_percentage'].round(2)
        export_data['brent_price'] = export_data['brent_price'].round(2)
        export_data['jkm_price'] = export_data['jkm_price'].round(2)

        # Rename columns for clarity
        export_data = export_data.rename(columns={
            'slope_percentage': 'Slope (%)',
            'brent_price': f'{brent_contract} Price',
            'jkm_price': 'JKM Price'
        })

        # Reorder columns: trade_date, strip, slope, brent, jkm
        export_data = export_data[['trade_date', 'strip', 'Slope (%)', f'{brent_contract} Price', 'JKM Price']]

        # Sort by trade_date and strip
        export_data = export_data.sort_values(['trade_date', 'strip']).reset_index(drop=True)

        # Create filename with metadata
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        view_label = 'rolling' if view_mode == 'rolling' else 'strips'
        filename = f"jkm_brent_slopes_{grouping_mode}_{view_label}_{brent_contract}_{timestamp}.xlsx"

        # Write to Excel using BytesIO
        import io
        output = io.BytesIO()

        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # Write export data
            export_data.to_excel(writer, sheet_name='Data', index=False)

            # Add metadata sheet
            metadata = pd.DataFrame({
                'Parameter': ['Grouping Mode', 'View Mode', 'Brent Contract', 'JKM Discount', 'Export Date'],
                'Value': [
                    grouping_mode,
                    view_mode,
                    brent_contract,
                    f'${jkm_discount:.2f}' if jkm_discount else '$0.00',
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                ]
            })
            metadata.to_excel(writer, sheet_name='Metadata', index=False)

            # Auto-adjust column widths
            for sheet_name in writer.sheets:
                worksheet = writer.sheets[sheet_name]
                for column in worksheet.columns:
                    max_length = 0
                    column_letter = column[0].column_letter
                    for cell in column:
                        try:
                            if len(str(cell.value)) > max_length:
                                max_length = len(str(cell.value))
                        except:
                            pass
                    adjusted_width = min(max_length + 2, 50)
                    worksheet.column_dimensions[column_letter].width = adjusted_width

        output.seek(0)

        return dcc.send_bytes(output.getvalue(), filename)

    except Exception as e:
        print(f"Error downloading slope data: {e}")
        import traceback
        traceback.print_exc()
        return None


# Contract details inspector callback - shows underlying contracts for latest trade date
@dash.callback(
    Output('contract-details-container', 'children'),
    [Input('slope-grouping-dropdown', 'value'),
     Input('jkm-discount-input', 'value'),
     Input('selected-brent-config', 'data'),
     Input('view-mode-dropdown', 'value'),
     Input('rolling-periods-input', 'value'),
     Input('refresh-trigger', 'data')],
    prevent_initial_call=True
)
def update_contract_details(grouping_mode, jkm_discount, brent_contract, view_mode, num_rolling_periods, refresh_trigger):
    """Show detailed breakdown of contracts used for slope calculation at the latest trade date"""
    try:
        if df_options is None or df_options.empty:
            return html.Div(html.H4("No data available", style={'color': 'orange'}))

        # Get the latest trade date
        df_temp = df_options.copy()
        df_temp['trade_date'] = pd.to_datetime(df_temp['trade_date'], errors='coerce')
        latest_trade_date = df_temp['trade_date'].max()

        if pd.isna(latest_trade_date):
            return html.Div(html.H4("No valid trade dates found", style={'color': 'red'}))

        print(f"\n=== CONTRACT DETAILS INSPECTOR ===")
        print(f"Latest trade date: {latest_trade_date}")
        print(f"Grouping mode: {grouping_mode}")
        print(f"Brent contract: {brent_contract}")

        # Filter data for latest trade date
        df_latest = df_temp[df_temp['trade_date'] == latest_trade_date].copy()

        # Get JKM data at monthly level (ungrouped)
        df_jkm_monthly = df_latest[df_latest['product'] == 'JKM'].copy()
        df_jkm_monthly['expiration_date'] = pd.to_datetime(df_jkm_monthly['expiration_date'], errors='coerce')
        df_jkm_monthly['strip'] = df_jkm_monthly['expiration_date'].apply(slope_create_strip_identifier)
        df_jkm_monthly['maturity_date'] = pd.to_datetime(df_jkm_monthly['strip'], format='%b-%y', errors='coerce')

        # Apply JKM discount if specified
        if jkm_discount:
            df_jkm_monthly['settlement_price'] = df_jkm_monthly['settlement_price'] - jkm_discount

        # Get Brent data at monthly level
        df_brent_monthly = df_latest[df_latest['contract'].isin(['B', 'Brent'])].copy()
        df_brent_monthly['maturity_date'] = pd.to_datetime(df_brent_monthly['strip'], errors='coerce')
        df_brent_monthly['strip'] = df_brent_monthly['maturity_date'].dt.strftime('%b-%y')

        print(f"JKM monthly contracts: {len(df_jkm_monthly)}")
        print(f"Brent monthly contracts: {len(df_brent_monthly)}")

        # Calculate Brent indices for each JKM contract
        brent_details = []

        if brent_contract != 'Brent':
            # Parse window and lag from contract code (format: BXYZ)
            try:
                window_months = int(brent_contract[1])
                lag_months = int(brent_contract[2])
            except:
                return html.Div(html.H4(f"Invalid Brent contract: {brent_contract}", style={'color': 'red'}))

            from dateutil.relativedelta import relativedelta

            for _, jkm_row in df_jkm_monthly.iterrows():
                jkm_strip = jkm_row['strip']
                jkm_maturity = jkm_row['maturity_date']
                jkm_price = jkm_row['settlement_price']

                if pd.isna(jkm_maturity):
                    continue

                # Calculate averaging window
                window_start_date, window_end_date = get_averaging_window(jkm_maturity, lag_months, window_months)

                # Find Brent contracts in this window
                brent_in_window = df_brent_monthly[
                    (df_brent_monthly['maturity_date'] >= window_start_date) &
                    (df_brent_monthly['maturity_date'] <= window_end_date)
                ].copy()

                if not brent_in_window.empty:
                    brent_avg = brent_in_window['settlement_price'].mean()
                    brent_contracts = ', '.join([f"{row['strip']}: ${row['settlement_price']:.2f}"
                                                for _, row in brent_in_window.iterrows()])
                else:
                    brent_avg = None
                    brent_contracts = 'No data'

                # Calculate rolling label if in rolling mode
                rolling_label = None
                if view_mode == 'rolling':
                    if grouping_mode == 'calendar':
                        rolling_label = calculate_year_rolling_label(latest_trade_date, jkm_maturity)
                    elif grouping_mode == 'monthly':
                        rolling_label = calculate_month_rolling_label(latest_trade_date, jkm_maturity)
                    elif grouping_mode == 'quarterly':
                        rolling_label = calculate_quarter_rolling_label(latest_trade_date, jkm_maturity)
                    elif grouping_mode == 'season':
                        rolling_label = calculate_season_rolling_label(latest_trade_date, jkm_maturity)

                brent_details.append({
                    'jkm_strip': jkm_strip,
                    'rolling_label': rolling_label,
                    'jkm_price': jkm_price,
                    'window_start': window_start_date.strftime('%b-%y'),
                    'window_end': window_end_date.strftime('%b-%y'),
                    'brent_contracts': brent_contracts,
                    'brent_avg': brent_avg,
                    'slope': ((jkm_price / brent_avg) * 100) if brent_avg else None
                })
        else:
            # For spot Brent, just match by strip
            for _, jkm_row in df_jkm_monthly.iterrows():
                jkm_strip = jkm_row['strip']
                jkm_maturity = jkm_row['maturity_date']
                jkm_price = jkm_row['settlement_price']

                brent_match = df_brent_monthly[df_brent_monthly['strip'] == jkm_strip]
                if not brent_match.empty:
                    brent_price = brent_match['settlement_price'].iloc[0]

                    # Calculate rolling label if in rolling mode
                    rolling_label = None
                    if view_mode == 'rolling' and not pd.isna(jkm_maturity):
                        if grouping_mode == 'calendar':
                            rolling_label = calculate_year_rolling_label(latest_trade_date, jkm_maturity)
                        elif grouping_mode == 'monthly':
                            rolling_label = calculate_month_rolling_label(latest_trade_date, jkm_maturity)
                        elif grouping_mode == 'quarterly':
                            rolling_label = calculate_quarter_rolling_label(latest_trade_date, jkm_maturity)
                        elif grouping_mode == 'season':
                            rolling_label = calculate_season_rolling_label(latest_trade_date, jkm_maturity)

                    brent_details.append({
                        'jkm_strip': jkm_strip,
                        'rolling_label': rolling_label,
                        'jkm_price': jkm_price,
                        'window_start': jkm_strip,
                        'window_end': jkm_strip,
                        'brent_contracts': f"{jkm_strip}: ${brent_price:.2f}",
                        'brent_avg': brent_price,
                        'slope': ((jkm_price / brent_price) * 100) if brent_price else None
                    })

        df_details = pd.DataFrame(brent_details)

        if df_details.empty:
            return html.Div(html.H4("No contract details available for latest trade date", style={'color': 'orange'}))

        # Group by calendar/season/quarterly if needed
        if grouping_mode != 'monthly':
            # Add period column based on grouping mode
            df_details['maturity_date'] = pd.to_datetime(df_details['jkm_strip'], format='%b-%y', errors='coerce')

            if grouping_mode == 'calendar':
                df_details['period'] = df_details['maturity_date'].dt.year.astype(str)
            elif grouping_mode == 'quarterly':
                df_details['quarter'] = df_details['maturity_date'].dt.quarter
                df_details['year'] = df_details['maturity_date'].dt.year
                df_details['period'] = df_details.apply(lambda x: f"{x['year']}-Q{x['quarter']}", axis=1)
            elif grouping_mode == 'season':
                def get_season(date):
                    month = date.month
                    year = date.year
                    if 5 <= month <= 9:
                        return f"{year}-Summer"
                    else:
                        return f"{year}-Winter"
                df_details['period'] = df_details['maturity_date'].apply(get_season)

            # Create grouped summary tables
            grouped_periods = df_details.groupby('period')

            all_tables = []

            for period, group in grouped_periods:
                # Calculate aggregated values for this period
                avg_jkm = group['jkm_price'].mean()
                avg_brent = group['brent_avg'].mean()
                avg_slope = (avg_jkm / avg_brent * 100) if avg_brent else None

                # Create detail table for this period
                if view_mode == 'rolling':
                    detail_data = group[['jkm_strip', 'rolling_label', 'jkm_price', 'window_start', 'window_end', 'brent_contracts', 'brent_avg', 'slope']].copy()
                else:
                    detail_data = group[['jkm_strip', 'jkm_price', 'window_start', 'window_end', 'brent_contracts', 'brent_avg', 'slope']].copy()
                detail_data = detail_data.round({'jkm_price': 2, 'brent_avg': 2, 'slope': 2})

                # Build columns based on view mode
                if view_mode == 'rolling':
                    table_columns = [
                        {"name": "JKM Strip", "id": "jkm_strip"},
                        {"name": "Rolling Label", "id": "rolling_label"},
                        {"name": "JKM Price", "id": "jkm_price", "type": "numeric", "format": {"specifier": ".2f"}},
                        {"name": "Brent Window", "id": "window_start"},
                        {"name": "to", "id": "window_end"},
                        {"name": "Brent Contracts Used", "id": "brent_contracts"},
                        {"name": f"{brent_contract} Avg", "id": "brent_avg", "type": "numeric", "format": {"specifier": ".2f"}},
                        {"name": "Slope (%)", "id": "slope", "type": "numeric", "format": {"specifier": ".2f"}}
                    ]
                else:
                    table_columns = [
                        {"name": "JKM Strip", "id": "jkm_strip"},
                        {"name": "JKM Price", "id": "jkm_price", "type": "numeric", "format": {"specifier": ".2f"}},
                        {"name": "Brent Window", "id": "window_start"},
                        {"name": "to", "id": "window_end"},
                        {"name": "Brent Contracts Used", "id": "brent_contracts"},
                        {"name": f"{brent_contract} Avg", "id": "brent_avg", "type": "numeric", "format": {"specifier": ".2f"}},
                        {"name": "Slope (%)", "id": "slope", "type": "numeric", "format": {"specifier": ".2f"}}
                    ]

                period_table = html.Div([
                    html.H5(f"Period: {period}", style={'color': '#1e3a8a', 'marginTop': '20px', 'marginBottom': '10px'}),
                    html.Div([
                        html.Span(f"Avg JKM: ${avg_jkm:.2f}", style={'marginRight': '20px', 'fontWeight': 'bold'}),
                        html.Span(f"Avg Brent ({brent_contract}): ${avg_brent:.2f}", style={'marginRight': '20px', 'fontWeight': 'bold'}),
                        html.Span(f"Avg Slope: {avg_slope:.2f}%", style={'fontWeight': 'bold', 'color': 'green'})
                    ], style={'marginBottom': '10px', 'padding': '10px', 'backgroundColor': '#f0f9ff', 'borderRadius': '4px'}),
                    dash_table.DataTable(
                        columns=table_columns,
                        data=detail_data.to_dict('records'),
                        style_table={'overflowX': 'auto'},
                        style_cell={
                            'textAlign': 'left',
                            'padding': '8px',
                            'fontSize': 11,
                            'whiteSpace': 'normal',
                            'height': 'auto'
                        },
                        style_cell_conditional=[
                            {'if': {'column_id': 'brent_contracts'}, 'minWidth': '300px', 'maxWidth': '500px'},
                            {'if': {'column_id': 'jkm_price'}, 'textAlign': 'center'},
                            {'if': {'column_id': 'brent_avg'}, 'textAlign': 'center'},
                            {'if': {'column_id': 'slope'}, 'textAlign': 'center'}
                        ],
                        style_header={
                            'backgroundColor': 'rgb(230, 230, 230)',
                            'fontWeight': 'bold',
                            'textAlign': 'center',
                            'color': 'black'
                        },
                        style_data_conditional=[
                            {'if': {'row_index': 'odd'}, 'backgroundColor': 'rgb(248, 248, 248)'}
                        ]
                    )
                ])
                all_tables.append(period_table)

            return html.Div([
                html.Div(f"Trade Date: {latest_trade_date.strftime('%Y-%m-%d')}",
                        style={'fontSize': '14px', 'marginBottom': '15px', 'color': '#666'}),
                *all_tables
            ])

        else:
            # Monthly view - single table
            if view_mode == 'rolling':
                detail_data = df_details[['jkm_strip', 'rolling_label', 'jkm_price', 'window_start', 'window_end', 'brent_contracts', 'brent_avg', 'slope']].copy()
            else:
                detail_data = df_details[['jkm_strip', 'jkm_price', 'window_start', 'window_end', 'brent_contracts', 'brent_avg', 'slope']].copy()
            detail_data = detail_data.round({'jkm_price': 2, 'brent_avg': 2, 'slope': 2})

            # Build columns based on view mode
            if view_mode == 'rolling':
                table_columns = [
                    {"name": "JKM Strip", "id": "jkm_strip"},
                    {"name": "Rolling Label", "id": "rolling_label"},
                    {"name": "JKM Price", "id": "jkm_price", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Brent Window", "id": "window_start"},
                    {"name": "to", "id": "window_end"},
                    {"name": "Brent Contracts Used", "id": "brent_contracts"},
                    {"name": f"{brent_contract} Avg", "id": "brent_avg", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Slope (%)", "id": "slope", "type": "numeric", "format": {"specifier": ".2f"}}
                ]
            else:
                table_columns = [
                    {"name": "JKM Strip", "id": "jkm_strip"},
                    {"name": "JKM Price", "id": "jkm_price", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Brent Window", "id": "window_start"},
                    {"name": "to", "id": "window_end"},
                    {"name": "Brent Contracts Used", "id": "brent_contracts"},
                    {"name": f"{brent_contract} Avg", "id": "brent_avg", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Slope (%)", "id": "slope", "type": "numeric", "format": {"specifier": ".2f"}}
                ]

            return html.Div([
                html.Div(f"Trade Date: {latest_trade_date.strftime('%Y-%m-%d')}",
                        style={'fontSize': '14px', 'marginBottom': '15px', 'color': '#666'}),
                dash_table.DataTable(
                    columns=table_columns,
                    data=detail_data.to_dict('records'),
                    style_table={'overflowX': 'auto'},
                    style_cell={
                        'textAlign': 'left',
                        'padding': '8px',
                        'fontSize': 11,
                        'whiteSpace': 'normal',
                        'height': 'auto'
                    },
                    style_cell_conditional=[
                        {'if': {'column_id': 'brent_contracts'}, 'minWidth': '300px', 'maxWidth': '500px'},
                        {'if': {'column_id': 'jkm_price'}, 'textAlign': 'center'},
                        {'if': {'column_id': 'brent_avg'}, 'textAlign': 'center'},
                        {'if': {'column_id': 'slope'}, 'textAlign': 'center'}
                    ],
                    style_header={
                        'backgroundColor': 'rgb(230, 230, 230)',
                        'fontWeight': 'bold',
                        'textAlign': 'center',
                        'color': 'black'
                    },
                    style_data_conditional=[
                        {'if': {'row_index': 'odd'}, 'backgroundColor': 'rgb(248, 248, 248)'}
                    ]
                )
            ])

    except Exception as e:
        print(f"Error in update_contract_details: {e}")
        import traceback
        traceback.print_exc()
        return html.Div(html.H4(f"Error generating contract details: {str(e)}", style={'color': 'red'}))