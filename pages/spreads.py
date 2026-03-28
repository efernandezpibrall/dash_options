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

# ------ code to be able to access config.ini ------#
try:
    # Get the directory where your script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate to the directory containing config.ini
    config_dir = os.path.abspath(os.path.join(script_dir, '..', '..'))  # Go up two levels
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

# SQL Query for TFU and HH contracts
QUERY_SPREADS = '''
SELECT
  *
FROM
  raw."ice_gas"."cleared_gas"
WHERE
  contract IN ('TFU', 'H')
  AND ("contract_type" != 'P' AND "contract_type" != 'C')
  AND strike IS NULL
  AND product NOT LIKE '%daily%'
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


# Function to create database connection
def create_connection():
    """Create and return database connection"""
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
    return conn_gas


# Function to load and process data
def load_and_process_data():
    """Load data from Trino and process it"""
    try:
        # Create connection
        conn_gas = create_connection()

        # Load data
        df_spreads = read_table_conn(conn_gas, QUERY_SPREADS)

        # Close connection
        conn_gas.close()

        # Process data
        print(f"\nLoaded {len(df_spreads)} rows of TFU and HH data.")

        return df_spreads

    except Exception as e:
        print(f"Error loading data: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()  # Return empty DataFrame on error


# Initialize DataFrame at module level but allow refresh
df_spreads = pd.DataFrame()


def get_previous_business_date():
    """Get the previous business date (excluding weekends)"""
    today = pd.Timestamp.now()

    # If today is Monday, go back to Friday
    if today.weekday() == 0:  # Monday
        prev_date = today - pd.Timedelta(days=3)
    # If today is Sunday, go back to Friday
    elif today.weekday() == 6:  # Sunday
        prev_date = today - pd.Timedelta(days=2)
    # Otherwise, go back one day
    else:
        prev_date = today - pd.Timedelta(days=1)

    return prev_date.strftime('%Y-%m-%d')


# ============================================================================
# GROUPING FUNCTIONS
# ============================================================================

def spread_create_strip_identifier(expiration_date, format_type='month_year'):
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


def spread_group_data_by_period(data, grouping_mode):
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

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean',
                'maturity_date': 'first'
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
                    # Winter spans across years (Nov-Mar)
                    if month >= 11:
                        return f"{year}-Winter"
                    else:
                        return f"{year-1}-Winter"

            data['period'] = data['maturity_date'].apply(get_season)
            data = data.dropna(subset=['period'])

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean',
                'maturity_date': 'first'
            }).reset_index()
            return grouped

        elif grouping_mode == 'calendar':
            # For calendar grouping, use maturity_date year (contract delivery year)
            data['period'] = data['maturity_date'].dt.year.astype(str)

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean',
                'maturity_date': 'first'
            }).reset_index()
            return grouped

        data['period'] = data['maturity_date'].dt.strftime('%b-%y')
        return data

    except Exception as e:
        print(f"Error in spread_group_data_by_period: {e}")
        if 'period' not in data.columns:
            data['period'] = 'unknown'
        return data


# ============================================================================
# SPREAD CALCULATION FUNCTION
# ============================================================================

def calculate_spread_data(df, grouping_mode, hh_multiplier=1.0, hh_premium=0, tfu_discount=0):
    """
    Calculate TFU-HH spread with adjustments

    Parameters:
    - df: DataFrame with TFU and HH data
    - grouping_mode: 'monthly', 'quarterly', 'season', or 'calendar'
    - hh_multiplier: Multiplier for HH price (default 1.0)
    - hh_premium: Premium to add to HH price (default 0)
    - tfu_discount: Discount to subtract from TFU price (default 0)

    Returns:
    - DataFrame with columns: [trade_date, strip, spread, tfu_price, hh_price]
    """
    try:
        if df.empty:
            return pd.DataFrame()

        # Make a copy to avoid modifying original
        df = df.copy()

        # Ensure trade_date is datetime
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df['expiration_date'] = pd.to_datetime(df['expiration_date'])

        # Create strip identifier at monthly level first
        df['strip'] = df['expiration_date'].apply(lambda x: spread_create_strip_identifier(x))
        df = df.dropna(subset=['strip'])

        # Separate TFU and HH data
        df_tfu = df[df['contract'] == 'TFU'].copy()
        df_hh = df[df['contract'] == 'H'].copy()

        if df_tfu.empty or df_hh.empty:
            print("Warning: Missing TFU or HH data")
            return pd.DataFrame()

        # Group by period for both contracts
        df_tfu_grouped = spread_group_data_by_period(df_tfu, grouping_mode)
        df_hh_grouped = spread_group_data_by_period(df_hh, grouping_mode)

        # Rename settlement_price to avoid conflicts
        df_tfu_grouped = df_tfu_grouped.rename(columns={'settlement_price': 'tfu_price'})
        df_hh_grouped = df_hh_grouped.rename(columns={'settlement_price': 'hh_price'})

        # Merge on trade_date and period
        df_merged = pd.merge(
            df_tfu_grouped[['trade_date', 'period', 'tfu_price', 'maturity_date']],
            df_hh_grouped[['trade_date', 'period', 'hh_price']],
            on=['trade_date', 'period'],
            how='inner'
        )

        if df_merged.empty:
            print("Warning: No matching periods found after merge")
            return pd.DataFrame()

        # Apply adjustments
        df_merged['tfu_adjusted'] = df_merged['tfu_price'] - tfu_discount
        df_merged['hh_adjusted'] = df_merged['hh_price'] * hh_multiplier + hh_premium

        # Calculate spread: TFU - HH
        df_merged['spread'] = df_merged['tfu_adjusted'] - df_merged['hh_adjusted']

        # Rename period to strip for consistency
        df_merged = df_merged.rename(columns={'period': 'strip'})

        # Sort by trade_date and maturity_date
        df_merged = df_merged.sort_values(['trade_date', 'maturity_date'])

        return df_merged[['trade_date', 'strip', 'spread', 'tfu_price', 'hh_price', 'maturity_date']]

    except Exception as e:
        print(f"Error in calculate_spread_data: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


# ============================================================================
# LAYOUT
# ============================================================================

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

# Modified layout for spread analysis
layout = html.Div(
    style=styles['page'],
    children=[
        # Simple trigger store - just to signal refresh
        dcc.Store(id='spread-refresh-trigger', data=0, storage_type='memory'),

        # Store for spread data (for download)
        dcc.Store(id='spread-data-store', storage_type='memory'),

        # Controls panel
        html.Div(
            style=styles['panel'],
            children=[
                html.Div(
                    style={'textAlign': 'left', 'marginTop': '20px', 'marginBottom': '20px'},
                    children=[
                        # Spread selection dropdown
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Spread:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='spread-selection-dropdown',
                                    options=[
                                        {'label': 'TFU-HH', 'value': 'TFU-HH'}
                                    ],
                                    value='TFU-HH',
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
                        # HH Multiplier input
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "HH Multiplier:",
                                    style=styles['label']
                                ),
                                dcc.Input(
                                    id='hh-multiplier-input',
                                    type='number',
                                    value=1.0,
                                    step=0.1,
                                    placeholder='1.0',
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
                        # HH Premium input
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "HH Premium:",
                                    style=styles['label']
                                ),
                                dcc.Input(
                                    id='hh-premium-input',
                                    type='number',
                                    value=0,
                                    step=0.5,
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
                        # TFU Discount input
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "TFU Discount:",
                                    style=styles['label']
                                ),
                                dcc.Input(
                                    id='tfu-discount-input',
                                    type='number',
                                    value=0,
                                    step=0.5,
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
                        # Grouping dropdown
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Group by:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='spread-grouping-dropdown',
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
                        # Comparison date picker
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Compare to date:",
                                    style=styles['label']
                                ),
                                dcc.DatePickerSingle(
                                    id='spread-comparison-date-picker',
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
                        'marginBottom': '15px'
                    },
                    children=[
                        html.H3(
                            "TFU-HH Spread Over Time",
                            style={'margin': '0', 'color': '#1e3a8a'}
                        ),
                        html.Button(
                            "Download Data",
                            id='spread-download-button',
                            style=styles['button']
                        )
                    ]
                ),
                dcc.Download(id='spread-download-excel'),
                html.Div(id='spread-graphs-container'),
            ]
        ),
        # Tables panel
        html.Div(
            style=styles['panel'],
            children=[
                html.H3(
                    "Latest Spreads by Strip",
                    style={'marginBottom': '15px', 'color': '#1e3a8a'}
                ),
                html.Div(id='spread-tables-container'),
            ]
        ),
    ]
)


# ============================================================================
# CALLBACKS
# ============================================================================

# Callback 1: Refresh data
@callback(
    Output('spread-refresh-trigger', 'data'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=True
)
def refresh_spread_data(n_clicks):
    """Reload spread data from database"""
    global df_spreads
    df_spreads = load_and_process_data()
    return n_clicks if n_clicks else 0


# Callback 2: Initialize comparison date
@callback(
    Output('spread-comparison-date-picker', 'date'),
    Input('spread-refresh-trigger', 'data'),
    prevent_initial_call=False
)
def init_spread_comparison_date(trigger):
    """Initialize comparison date to previous business day"""
    return get_previous_business_date()


# Callback 3: Update graphs
@callback(
    [Output('spread-graphs-container', 'children'),
     Output('spread-data-store', 'data')],
    [Input('spread-grouping-dropdown', 'value'),
     Input('hh-multiplier-input', 'value'),
     Input('hh-premium-input', 'value'),
     Input('tfu-discount-input', 'value'),
     Input('spread-refresh-trigger', 'data')],
    prevent_initial_call=False
)
def update_spread_graphs(grouping_mode, hh_multiplier, hh_premium, tfu_discount, refresh_trigger):
    """Update spread graphs based on parameters"""
    global df_spreads

    # Handle None values
    hh_multiplier = hh_multiplier if hh_multiplier is not None else 1.0
    hh_premium = hh_premium if hh_premium is not None else 0
    tfu_discount = tfu_discount if tfu_discount is not None else 0

    # Load data if not already loaded
    if df_spreads.empty:
        df_spreads = load_and_process_data()

    if df_spreads.empty:
        return [html.Div("No data available. Please refresh data.",
                        style={'color': 'red', 'padding': '20px'})], None

    # Calculate spread data
    spread_data = calculate_spread_data(
        df_spreads,
        grouping_mode,
        hh_multiplier,
        hh_premium,
        tfu_discount
    )

    if spread_data.empty:
        return [html.Div("Unable to calculate spreads. Please check data.",
                        style={'color': 'red', 'padding': '20px'})], None

    # Get unique strips (sorted by maturity date)
    strips = spread_data.sort_values('maturity_date')['strip'].unique()

    # Create figure
    fig = go.Figure()

    # Add trace for each strip
    for strip in strips:
        strip_data = spread_data[spread_data['strip'] == strip].sort_values('trade_date')

        fig.add_trace(go.Scatter(
            x=strip_data['trade_date'],
            y=strip_data['spread'],
            mode='lines+markers',
            name=strip,
            line=dict(width=2),
            marker=dict(size=4),
            hovertemplate='<b>%{fullData.name}</b><br>' +
                         'Date: %{x|%Y-%m-%d}<br>' +
                         'Spread: $%{y:.2f}<br>' +
                         '<extra></extra>'
        ))

    # Update layout
    today = pd.Timestamp.now()
    last_180_days = today - pd.Timedelta(days=180)

    fig.update_layout(
        xaxis=dict(
            title="Trade Date",
            range=[last_180_days, today],
            rangeslider=dict(visible=True),
            type='date'
        ),
        yaxis=dict(
            title="Spread ($/MMBtu)",
            gridcolor='lightgray',
            autorange=True,  # Auto-scale Y axis based on visible data
            fixedrange=False  # Allow Y axis zooming
        ),
        hovermode='x unified',
        plot_bgcolor='white',
        height=600,
        margin=dict(l=50, r=50, t=30, b=50),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1
        )
    )

    # Store data for download
    data_dict = spread_data.to_dict('records')

    return [dcc.Graph(figure=fig, config={'displayModeBar': True})], data_dict


# Callback 4: Update tables
@callback(
    Output('spread-tables-container', 'children'),
    [Input('spread-grouping-dropdown', 'value'),
     Input('hh-multiplier-input', 'value'),
     Input('hh-premium-input', 'value'),
     Input('tfu-discount-input', 'value'),
     Input('spread-comparison-date-picker', 'date'),
     Input('spread-refresh-trigger', 'data')],
    prevent_initial_call=False
)
def update_spread_tables(grouping_mode, hh_multiplier, hh_premium, tfu_discount, comparison_date, refresh_trigger):
    """Update spread tables with latest values and comparison"""
    global df_spreads

    # Handle None values
    hh_multiplier = hh_multiplier if hh_multiplier is not None else 1.0
    hh_premium = hh_premium if hh_premium is not None else 0
    tfu_discount = tfu_discount if tfu_discount is not None else 0

    # Load data if not already loaded
    if df_spreads.empty:
        df_spreads = load_and_process_data()

    if df_spreads.empty:
        return html.Div("No data available.", style={'color': 'red', 'padding': '20px'})

    # Calculate spread data
    spread_data = calculate_spread_data(
        df_spreads,
        grouping_mode,
        hh_multiplier,
        hh_premium,
        tfu_discount
    )

    if spread_data.empty:
        return html.Div("Unable to calculate spreads.", style={'color': 'red', 'padding': '20px'})

    # Get latest trade date
    latest_date = spread_data['trade_date'].max()
    latest_data = spread_data[spread_data['trade_date'] == latest_date].copy()

    # Prepare table data - include maturity_date for sorting
    table_data = latest_data[['strip', 'spread', 'tfu_price', 'hh_price', 'maturity_date']].copy()
    table_data = table_data.sort_values('maturity_date')
    table_data = table_data.drop(columns=['maturity_date'])  # Remove after sorting
    table_data = table_data.round(2)

    columns = [
        {'name': 'Strip', 'id': 'strip'},
        {'name': 'Spread ($/MMBtu)', 'id': 'spread'},
        {'name': 'TFU Price ($/MMBtu)', 'id': 'tfu_price'},
        {'name': 'HH Price ($/MMBtu)', 'id': 'hh_price'}
    ]

    # Add comparison if date is selected
    if comparison_date:
        try:
            comp_date = pd.to_datetime(comparison_date)
            comp_data = spread_data[spread_data['trade_date'] == comp_date]

            if not comp_data.empty:
                # Merge comparison data
                comp_data = comp_data[['strip', 'spread']].copy()
                comp_data = comp_data.rename(columns={'spread': 'comparison_spread'})

                table_data = pd.merge(
                    latest_data[['strip', 'spread', 'tfu_price', 'hh_price', 'maturity_date']],
                    comp_data,
                    on='strip',
                    how='left'
                )

                table_data['change'] = table_data['spread'] - table_data['comparison_spread']
                table_data = table_data.sort_values('maturity_date')
                table_data = table_data.round(2)

                columns.append({'name': f'Spread on {comparison_date}', 'id': 'comparison_spread'})
                columns.append({'name': 'Change', 'id': 'change'})
        except:
            pass

    # Create DataTable
    table = dash_table.DataTable(
        data=table_data.to_dict('records'),
        columns=columns,
        style_table={'overflowX': 'auto'},
        style_cell={
            'textAlign': 'center',
            'padding': '10px',
            'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif'
        },
        style_header={
            'backgroundColor': '#1e3a8a',
            'color': 'white',
            'fontWeight': 'bold'
        },
        style_data_conditional=[
            {
                'if': {'column_id': 'change', 'filter_query': '{change} > 0'},
                'backgroundColor': '#d4edda',
                'color': '#155724'
            },
            {
                'if': {'column_id': 'change', 'filter_query': '{change} < 0'},
                'backgroundColor': '#f8d7da',
                'color': '#721c24'
            },
            {
                'if': {'row_index': 'odd'},
                'backgroundColor': '#f8f9fa'
            }
        ]
    )

    return table


# Callback 5: Download data
@callback(
    Output('spread-download-excel', 'data'),
    Input('spread-download-button', 'n_clicks'),
    State('spread-data-store', 'data'),
    prevent_initial_call=True
)
def download_spread_data(n_clicks, data):
    """Download spread data to Excel"""
    if data is None:
        return None

    df = pd.DataFrame(data)

    # Create filename with timestamp
    timestamp = pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')
    filename = f'tfu_hh_spread_{timestamp}.xlsx'

    return dcc.send_data_frame(df.to_excel, filename, index=False, sheet_name='Spreads')
