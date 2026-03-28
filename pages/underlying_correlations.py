import dash
from dash import html, dcc
import pandas as pd
import numpy as np
from dash import Dash, html, dcc, callback, Input, Output, dash_table, State, callback_context
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import datetime
import configparser # Import the built-in configparser
# Trino
from trino.dbapi import connect
from trino.auth import JWTAuthentication
import os

#------ code to be able to access config.ini, even having the path in the .virtualenvs is not working without it ------#
try:
    # Get the directory where your script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate to the directory containing config.ini
    # Adjust the number of '..' as needed to reach the correct directory
    config_dir = os.path.abspath(os.path.join(script_dir, '..','..'))  # Go up one level
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



# Function to retrieve data from trinos in a df
def read_table_conn(conn,query):
    cur = conn.cursor()
    cur.execute(query)
    rows = cur.fetchall()
    num_fields = len(cur.description)
    field_names = [i[0] for i in cur.description]
    df_data = pd.DataFrame(rows, columns=field_names)
    cur.close()
    return df_data

# trinos connection gas
conn_gas = connect(
    host= TRINOS_HOST,
    port= TRINOS_PORT,
    user= TRINOS_USERNAME,
    auth= JWTAuthentication(TRINOS_TOKEN),
    http_scheme= "https",
    verify= False,
    catalog= "raw",
    schema= "ice_gas",
)
# trinos connection oil
conn_oil = connect(
    host= TRINOS_HOST,
    port= TRINOS_PORT,
    user= TRINOS_USERNAME,
    auth= JWTAuthentication(TRINOS_TOKEN),
    http_scheme= "https",
    verify= False,
    catalog= "raw",
    schema= "ice_oil",
)

def get_prices():
    query_gas = '''
    SELECT
      *
    FROM
      raw."ice_gas"."cleared_gas"
    WHERE
      (product = 'LNG Futures' AND contract = 'JKM')
      OR contract = 'PEG'
      OR (contract = 'M' AND product = 'UK NBP Natural Gas Futures')
      OR (product = 'Dutch TTF Natural Gas Futures' AND contract = 'TFM')
      AND ("contract_type" != 'P' AND "contract_type" != 'C')
      AND product NOT LIKE '%daily%'
    '''

    query_oil = '''
    SELECT
      *
    FROM
      raw."ice_oil"."cleared_oil"
    WHERE
      (product = 'Brent Crude Futures' AND contract = 'B')
    '''
    # Main gas data
    # Get underlying prices from data lake
    df_options_gas = read_table_conn(conn_gas, query_gas)

    # Updating the 'product' column
    df_options_gas.loc[df_options_gas['contract'] == 'JKM', 'product'] = 'JKM'
    df_options_gas.loc[df_options_gas['contract'] == 'PEG', 'product'] = 'PEG'
    df_options_gas.loc[df_options_gas['contract'] == 'M', 'product'] = 'NBP'
    df_options_gas.loc[df_options_gas['contract'] == 'M', 'contract'] = 'NBP'
    df_options_gas.loc[df_options_gas['contract'] == 'TFM', 'product'] = 'TFM'

    # Main oil data
    # Get underlying prices from data lake
    df_options_oil = read_table_conn(conn_oil, query_oil)

    # Updating the 'product' columns where 'contract' is 'Brent Crude Futures'
    df_options_oil.loc[df_options_oil['contract'] == 'B', 'product'] = 'Brent'
    df_options_oil.loc[df_options_oil['contract'] == 'B', 'contract'] = 'Brent'
    # Combining the DataFrames
    df_options = pd.concat([df_options_gas, df_options_oil], ignore_index=True)
    return df_options

df_options = get_prices()


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
        'width': '90%',
        'textAlign': 'left'
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
        'width': '90%'
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
        'padding': '10px 25px',
        'backgroundColor': '#3b82f6',
        'color': 'white',
        'border': 'none',
        'borderRadius': '4px',
        'cursor': 'pointer',
        'fontSize': '16px',
        'fontWeight': 'bold',
        'marginRight': '20px',
        'verticalAlign': 'middle'
    },
    'controlRow': {
        'marginBottom': '15px',
        'textAlign': 'center'
    },
    'sectionTitle': {
        'color': '#1e3a8a',
        'fontWeight': 'bold',
        'marginBottom': '15px',
        'paddingBottom': '10px',
        'borderBottom': '1px solid #e5e7eb',
        'fontSize': '18px'
    }
}

# The enhanced layout preserving original functionality
layout = html.Div(
    style=styles['page'],
    children=[
        # Header section
        html.Div(
            style=styles['header'],
            children=[
                html.H1("Correlation Analysis Dashboard", style=styles['title']),
            ]
        ),

        # Controls section
        html.Div(
            style={
                'width': '90%',
                'margin': '0 auto',
                'marginBottom': '30px',
                'padding': '20px',
                'backgroundColor': '#ffffff',
                'borderRadius': '8px',
                'boxShadow': '0 2px 4px rgba(0,0,0,0.1)'
            },
            children=[
                # Section title - left-aligned
                html.Div(
                    style={**styles['sectionTitle'], 'textAlign': 'left'},
                    # children="Analysis Controls"
                ),

                # First row of controls (rearranged) - left-aligned
                html.Div(
                    style={**styles['controlRow'], 'textAlign': 'left'},
                    children=[
                        # Calculate button
                        html.Button(
                            "Calculate Correlation",
                            id='correlations-calculate-button',
                            style={
                                'padding': '10px 25px',
                                'backgroundColor': '#10b981',  # Green button for primary action
                                'color': 'white',
                                'border': 'none',
                                'borderRadius': '4px',
                                'cursor': 'pointer',
                                'fontSize': '16px',
                                'fontWeight': 'bold',
                                'marginRight': '20px',
                                'verticalAlign': 'middle'
                            }
                        ),

                        # Calculation type selector
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Calculation:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='correlations-type-dropdown',
                                    options=[
                                        {'label': 'Price', 'value': 'price'},
                                        {'label': 'Relative Returns', 'value': 'relative_returns'},
                                        {'label': 'Log Returns', 'value': 'log_returns'}
                                    ],
                                    value='price',  # Default selection
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

                        # Group by selector
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Group by:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='correlations-grouping-dropdown',
                                    options=[
                                        {'label': 'Monthly', 'value': 'monthly'},
                                        {'label': 'Quarterly', 'value': 'quarterly'},
                                        {'label': 'Season', 'value': 'season'},
                                        {'label': 'Calendar', 'value': 'calendar'}
                                    ],
                                    value='monthly',  # Default selection
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

                        # Rolling window selector
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Window (days):",
                                    style=styles['label']
                                ),
                                dcc.Input(
                                    id='correlations-rolling-window',
                                    type='number',
                                    value=10,  # Default 10-day window
                                    min=2,  # Minimum window size
                                    max=100,  # Maximum window size
                                    step=1,  # Integer values only
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '80px',
                                        'marginRight': '20px',
                                        'padding': '8px',
                                        'border': '1px solid #e5e7eb',
                                        'borderRadius': '4px'
                                    }
                                ),
                            ]
                        ),
                    ]
                ),

                # Second row of controls (product selectors)
                html.Div(
                    style={
                        'marginBottom': '15px',
                        'textAlign': 'left',
                        'padding': '10px',
                        'backgroundColor': '#f9fafb',
                        'borderRadius': '6px'
                    },
                    children=[
                        html.Div(
                            style={
                                'marginBottom': '10px',
                                'color': '#1e3a8a',
                                'fontWeight': 'bold',
                                'fontSize': '16px'
                            },
                            children="Product Selection"
                        ),

                        # First product selector
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "First Product:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='correlations-first-product-dropdown',
                                    options=[],  # Will be populated in callback
                                    value=None,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '150px',
                                        'marginRight': '20px'
                                    }
                                ),
                            ]
                        ),

                        # Second product selector
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Second Product:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='correlations-second-product-dropdown',
                                    options=[],  # Will be populated in callback
                                    value=None,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '150px'
                                    }
                                ),
                            ]
                        ),
                    ]
                ),

                # Third row of controls (contract selectors)
                html.Div(
                    style={
                        'marginBottom': '15px',
                        'textAlign': 'left',
                        'padding': '10px',
                        'backgroundColor': '#f9fafb',
                        'borderRadius': '6px'
                    },
                    children=[
                        html.Div(
                            style={
                                'marginBottom': '10px',
                                'color': '#1e3a8a',
                                'fontWeight': 'bold',
                                'fontSize': '16px'
                            },
                            children="Contract Selection"
                        ),

                        # First contract selector
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "First Contract:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='correlations-first-contract-dropdown',
                                    options=[],  # Will be populated in callback
                                    value=None,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '200px',
                                        'marginRight': '20px'
                                    }
                                ),
                            ]
                        ),

                        # Second contract selector
                        html.Div(
                            style={'display': 'inline-block'},
                            children=[
                                html.Label(
                                    "Second Contract:",
                                    style=styles['label']
                                ),
                                dcc.Dropdown(
                                    id='correlations-second-contract-dropdown',
                                    options=[],  # Will be populated in callback
                                    value=None,
                                    style={
                                        'display': 'inline-block',
                                        'verticalAlign': 'middle',
                                        'width': '200px'
                                    }
                                ),
                            ]
                        ),
                    ]
                ),
            ]
        ),

        # Results section
        html.Div(
            id='correlation-results-container',
            style={
                'width': '90%',
                'margin': '0 auto',
                'marginTop': '30px',
                'padding': '20px',
                'backgroundColor': '#ffffff',
                'borderRadius': '8px',
                'boxShadow': '0 2px 4px rgba(0,0,0,0.1)',
                'display': 'none'  # Initially hidden, as in the original
            },
            children=[
                # Section title
                html.Div(
                    style=styles['sectionTitle'],
                    children="Correlation Results"
                ),

                # Correlation value display
                html.Div(
                    children=[
                        html.Div(
                            id='correlation-value-display',
                            style={
                                'fontSize': '24px',
                                'fontWeight': 'bold',
                                'textAlign': 'center',
                                'marginBottom': '30px',
                                'color': '#1e3a8a',
                                'padding': '15px',
                                'backgroundColor': '#f0f9ff',
                                'borderRadius': '6px'
                            }
                        ),
                    ]
                ),

                # Plots in same row (scatter + time series)
                html.Div(
                    style={
                        'marginBottom': '30px',
                        'display': 'flex',
                        'backgroundColor': '#f9fafb',
                        'borderRadius': '8px',
                        'padding': '15px'
                    },
                    children=[
                        # Scatter plot
                        html.Div(
                            id='correlation-plot-container',
                            style={
                                'width': '50%',
                                'display': 'inline-block',
                                'verticalAlign': 'top',
                                'padding': '10px'
                            }
                        ),

                        # Time series plot
                        html.Div(
                            id='time-series-plot-container',
                            style={
                                'width': '50%',
                                'display': 'inline-block',
                                'verticalAlign': 'top',
                                'padding': '10px'
                            }
                        ),
                    ]
                ),

                # Data table
                html.Div(
                    id='correlation-data-table-container',
                    style={
                        'backgroundColor': '#ffffff',
                        'borderRadius': '6px',
                        'boxShadow': '0 1px 2px rgba(0,0,0,0.05)'
                    }
                )
            ]
        ),
    ]
)


# Initialize product dropdowns callback
@callback(
    [Output('correlations-first-product-dropdown', 'options'),
     Output('correlations-second-product-dropdown', 'options')],
    Input('correlations-calculate-button', 'n_clicks'),
    prevent_initial_call=False
)
def init_product_dropdowns(n_clicks):
    try:
        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None or not isinstance(df_options, pd.DataFrame):
            # Return empty options if no data
            return [], []

        # Check if required columns exist
        if 'product' not in df_options.columns:
            return [], []

        # Get unique, non-null products
        product_values = df_options['product'].dropna().unique()

        if len(product_values) == 0:
            return [], []

        # Sort product values
        product_values = sorted(product_values)

        # Create dropdown options
        dropdown_options = [{'label': product, 'value': product} for product in product_values]

        return dropdown_options, dropdown_options

    except Exception as e:
        print(f"Error initializing product dropdowns: {e}")
        return [], []


# Initialize contract dropdowns based on selected products
@callback(
    [Output('correlations-first-contract-dropdown', 'options'),
     Output('correlations-second-contract-dropdown', 'options'),
     Output('correlations-first-contract-dropdown', 'value'),
     Output('correlations-second-contract-dropdown', 'value')],
    [Input('correlations-first-product-dropdown', 'value'),
     Input('correlations-second-product-dropdown', 'value')],
    prevent_initial_call=True
)
def update_contract_dropdowns(first_product, second_product):
    if first_product is None and second_product is None:
        return [], [], None, None

    try:
        df_options = get_prices()

        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None:
            return [], [], None, None

        # Initialize contract options
        first_contracts = []
        second_contracts = []

        # Get contracts for first product if selected
        if first_product is not None:
            first_product_df = df_options[df_options['product'] == first_product]
            if 'expiration_date' in first_product_df.columns:
                # Convert to datetime and get unique values
                first_product_df['expiration_date'] = pd.to_datetime(first_product_df['expiration_date'],
                                                                     errors='coerce')
                first_contracts = first_product_df['expiration_date'].dropna().dt.strftime('%Y-%m-%d').unique()
                first_contracts = sorted(first_contracts)

        # Get contracts for second product if selected
        if second_product is not None:
            second_product_df = df_options[df_options['product'] == second_product]
            if 'expiration_date' in second_product_df.columns:
                # Convert to datetime and get unique values
                second_product_df['expiration_date'] = pd.to_datetime(second_product_df['expiration_date'],
                                                                      errors='coerce')
                second_contracts = second_product_df['expiration_date'].dropna().dt.strftime('%Y-%m-%d').unique()
                second_contracts = sorted(second_contracts)

        # Create dropdown options
        first_options = [{'label': contract, 'value': contract} for contract in first_contracts]
        second_options = [{'label': contract, 'value': contract} for contract in second_contracts]

        # Set default values (first contract for each if available)
        first_value = first_contracts[0] if len(first_contracts) > 0 else None
        second_value = second_contracts[0] if len(second_contracts) > 0 else None

        return first_options, second_options, first_value, second_value

    except Exception as e:
        print(f"Error updating contract dropdowns: {e}")
        return [], [], None, None


# Initialize date range picker
@callback(
    [Output('correlations-date-range', 'min_date_allowed'),
     Output('correlations-date-range', 'max_date_allowed'),
     Output('correlations-date-range', 'start_date'),
     Output('correlations-date-range', 'end_date')],
    Input('correlations-calculate-button', 'n_clicks'),
    prevent_initial_call=False
)
def init_date_range(n_clicks):
    try:
        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None:
            today = datetime.datetime.now().date()
            one_year_ago = (datetime.datetime.now() - datetime.timedelta(days=365)).date()
            return one_year_ago, today, one_year_ago, today

        # Ensure 'trade_date' column exists and convert to datetime
        if 'trade_date' not in df_options.columns:
            today = datetime.datetime.now().date()
            one_year_ago = (datetime.datetime.now() - datetime.timedelta(days=365)).date()
            return one_year_ago, today, one_year_ago, today

        # Convert to datetime
        df_options['trade_date'] = pd.to_datetime(df_options['trade_date'], errors='coerce')

        # Get min and max dates
        min_date = df_options['trade_date'].min().date()
        max_date = df_options['trade_date'].max().date()

        # Set default date range to last 6 months
        default_start = (max_date - datetime.timedelta(days=180))
        default_start = max(default_start, min_date)  # Ensure start date isn't before min_date

        return min_date, max_date, default_start, max_date

    except Exception as e:
        print(f"Error initializing date range: {e}")
        today = datetime.datetime.now().date()
        one_year_ago = (datetime.datetime.now() - datetime.timedelta(days=365)).date()
        return one_year_ago, today, one_year_ago, today


# Helper function for grouping data by period
def group_data_by_period(data, grouping_mode):
    """Group price data by different time periods."""
    try:
        data = data.copy()
        data['expiration_date'] = pd.to_datetime(data['expiration_date'], errors='coerce')
        data = data.dropna(subset=['expiration_date'])

        if grouping_mode == 'monthly':
            data['period'] = data['expiration_date'].dt.strftime('%m-%y')
            return data

        elif grouping_mode == 'quarterly':
            data['quarter'] = data['expiration_date'].dt.quarter
            data['year'] = data['expiration_date'].dt.year
            data['period'] = data.apply(lambda x: f"{x['year']}-Q{x['quarter']}", axis=1)

            grouped = data.groupby(['product', 'trade_date', 'period']).agg({
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

            data['period'] = data['expiration_date'].apply(get_season)
            data = data.dropna(subset=['period'])

            grouped = data.groupby(['product', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        elif grouping_mode == 'calendar':
            data['period'] = data['expiration_date'].dt.year.astype(str)

            grouped = data.groupby(['product', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        data['period'] = data['expiration_date'].dt.strftime('%m-%y')
        return data

    except Exception as e:
        print(f"Error in group_data_by_period: {e}")
        # Return original data in case of error
        if 'period' not in data.columns:
            data['period'] = 'unknown'
        return data


# Helper function to calculate returns based on the selected type
def calculate_returns(data, calculation_type):
    """Calculate returns based on the selected calculation type."""
    try:
        data = data.copy()
        data = data.sort_values('trade_date')

        if calculation_type == 'price':
            # Use raw prices, no calculation needed
            return data

        elif calculation_type == 'relative_returns':
            # Calculate percentage change (relative returns)
            data_groups = data.groupby('product')
            result_frames = []

            # Process each group separately
            for name, group in data_groups:
                group = group.sort_values('trade_date')
                group['value'] = group['settlement_price'].pct_change()
                result_frames.append(group)

            if result_frames:
                data = pd.concat(result_frames)

            return data.dropna(subset=['value'])

        elif calculation_type == 'log_returns':
            # Calculate log returns
            data_groups = data.groupby('product')
            result_frames = []

            # Process each group separately
            for name, group in data_groups:
                group = group.sort_values('trade_date')
                group['value'] = np.log(group['settlement_price'] / group['settlement_price'].shift(1))
                result_frames.append(group)

            if result_frames:
                data = pd.concat(result_frames)

            return data.dropna(subset=['value'])

        # Default return raw prices
        return data

    except Exception as e:
        print(f"Error in calculate_returns: {e}")
        return data


# Calculate correlation and update results
@callback(
    [Output('correlation-value-display', 'children'),
     Output('correlation-plot-container', 'children'),
     Output('time-series-plot-container', 'children'),
     Output('correlation-data-table-container', 'children'),
     Output('correlation-results-container', 'style')],
    [Input('correlations-calculate-button', 'n_clicks')],
    [State('correlations-first-product-dropdown', 'value'),
     State('correlations-second-product-dropdown', 'value'),
     State('correlations-first-contract-dropdown', 'value'),
     State('correlations-second-contract-dropdown', 'value'),
     State('correlations-type-dropdown', 'value'),
     State('correlations-grouping-dropdown', 'value'),
     # State('correlations-date-range', 'start_date'),
     # State('correlations-date-range', 'end_date'),
     State('correlations-rolling-window', 'value'),
     State('correlation-results-container', 'style')],
    prevent_initial_call=True
)


def calculate_correlation(n_clicks, first_product, second_product, first_contract, second_contract,
                          calculation_type, grouping_mode, rolling_window, current_style):
    if n_clicks is None or first_product is None or second_product is None or first_contract is None or second_contract is None:
        return "Please select all required parameters", None, None, None, {'display': 'none'}

    try:
        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None:
            error_message = "Error: DataFrame not available"
            return error_message, None, None, None, {'display': 'block'}

        # # Convert start and end dates to datetime
        # start_date = pd.to_datetime(start_date)
        # end_date = pd.to_datetime(end_date)

        # Make a copy of the dataframe to avoid modifying the original
        df = df_options.copy()

        # Convert date columns
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
        df['expiration_date'] = pd.to_datetime(df['expiration_date'], errors='coerce')

        # Filter by date range
        # df = df[(df['trade_date'] >= start_date) & (df['trade_date'] <= end_date)]

        if df.empty:
            error_message = "No data available for the selected date range"
            return error_message, None, None, None, {'display': 'block'}

        # For correlations, we need to approach grouping differently since we're analyzing specific contracts

        # First, filter by product
        first_product_data = df[df['product'] == first_product]
        second_product_data = df[df['product'] == second_product]

        if first_product_data.empty or second_product_data.empty:
            error_message = "No data available for one or both selected products"
            return error_message, None, None, None, {'display': 'block'}

        # Apply grouping based on the selected mode
        if grouping_mode == 'monthly':
            # For monthly, just filter for the specific contract
            first_contract_date = pd.to_datetime(first_contract)
            first_data = first_product_data[first_product_data['expiration_date'].dt.date == first_contract_date.date()]

            second_contract_date = pd.to_datetime(second_contract)
            second_data = second_product_data[
                second_product_data['expiration_date'].dt.date == second_contract_date.date()]
        else:
            # For other groupings, we need to group the data appropriately
            try:
                # Group first product data
                if grouping_mode == 'quarterly':
                    # Get the quarter and year from the selected contract
                    first_contract_date = pd.to_datetime(first_contract)
                    first_quarter = first_contract_date.quarter
                    first_year = first_contract_date.year

                    # Filter for contracts in the same quarter
                    first_data = first_product_data[
                        (first_product_data['expiration_date'].dt.quarter == first_quarter) &
                        (first_product_data['expiration_date'].dt.year == first_year)
                        ]

                    # Calculate average settlement price for each trade date in this quarter
                    first_data = first_data.groupby('trade_date').agg({
                        'settlement_price': 'mean',
                        'product': 'first',
                        'expiration_date': 'first'  # Keep one expiration date for reference
                    }).reset_index()

                elif grouping_mode == 'season':
                    # Determine if selected contract is in summer or winter
                    first_contract_date = pd.to_datetime(first_contract)
                    first_month = first_contract_date.month
                    first_year = first_contract_date.year

                    if 5 <= first_month <= 9:  # Summer (May to September)
                        season_filter = (first_product_data['expiration_date'].dt.month.between(5, 9))
                    else:  # Winter (October to April)
                        season_filter = ~(first_product_data['expiration_date'].dt.month.between(5, 9))

                    # Filter for contracts in the same season and year
                    first_data = first_product_data[
                        season_filter &
                        (first_product_data['expiration_date'].dt.year == first_year)
                        ]

                    # Calculate average settlement price for each trade date in this season
                    first_data = first_data.groupby('trade_date').agg({
                        'settlement_price': 'mean',
                        'product': 'first',
                        'expiration_date': 'first'  # Keep one expiration date for reference
                    }).reset_index()

                elif grouping_mode == 'calendar':
                    # Group by calendar year
                    first_contract_date = pd.to_datetime(first_contract)
                    first_year = first_contract_date.year

                    # Filter for contracts in the same year
                    first_data = first_product_data[first_product_data['expiration_date'].dt.year == first_year]

                    # Calculate average settlement price for each trade date in this year
                    first_data = first_data.groupby('trade_date').agg({
                        'settlement_price': 'mean',
                        'product': 'first',
                        'expiration_date': 'first'  # Keep one expiration date for reference
                    }).reset_index()

                # Group second product data using the same approach
                if grouping_mode == 'quarterly':
                    second_contract_date = pd.to_datetime(second_contract)
                    second_quarter = second_contract_date.quarter
                    second_year = second_contract_date.year

                    second_data = second_product_data[
                        (second_product_data['expiration_date'].dt.quarter == second_quarter) &
                        (second_product_data['expiration_date'].dt.year == second_year)
                        ]

                    second_data = second_data.groupby('trade_date').agg({
                        'settlement_price': 'mean',
                        'product': 'first',
                        'expiration_date': 'first'
                    }).reset_index()

                elif grouping_mode == 'season':
                    second_contract_date = pd.to_datetime(second_contract)
                    second_month = second_contract_date.month
                    second_year = second_contract_date.year

                    if 5 <= second_month <= 9:  # Summer
                        season_filter = (second_product_data['expiration_date'].dt.month.between(5, 9))
                    else:  # Winter
                        season_filter = ~(second_product_data['expiration_date'].dt.month.between(5, 9))

                    second_data = second_product_data[
                        season_filter &
                        (second_product_data['expiration_date'].dt.year == second_year)
                        ]

                    second_data = second_data.groupby('trade_date').agg({
                        'settlement_price': 'mean',
                        'product': 'first',
                        'expiration_date': 'first'
                    }).reset_index()

                elif grouping_mode == 'calendar':
                    second_contract_date = pd.to_datetime(second_contract)
                    second_year = second_contract_date.year

                    second_data = second_product_data[second_product_data['expiration_date'].dt.year == second_year]

                    second_data = second_data.groupby('trade_date').agg({
                        'settlement_price': 'mean',
                        'product': 'first',
                        'expiration_date': 'first'
                    }).reset_index()

                # Add period column for reference
                def add_period_info(data, mode, contract_date):
                    data = data.copy()
                    if mode == 'quarterly':
                        quarter = contract_date.quarter
                        year = contract_date.year
                        data['period'] = f"{year}-Q{quarter}"
                    elif mode == 'season':
                        month = contract_date.month
                        year = contract_date.year
                        season = "Summer" if 5 <= month <= 9 else "Winter"
                        data['period'] = f"{year}-{season}"
                    elif mode == 'calendar':
                        data['period'] = str(contract_date.year)
                    return data

                first_data = add_period_info(first_data, grouping_mode, pd.to_datetime(first_contract))
                second_data = add_period_info(second_data, grouping_mode, pd.to_datetime(second_contract))

            except Exception as e:
                print(f"Error in grouping: {e}")
                # If there's an error in grouping, fall back to just filtering by specific contract
                first_contract_date = pd.to_datetime(first_contract)
                first_data = first_product_data[
                    first_product_data['expiration_date'].dt.date == first_contract_date.date()]

                second_contract_date = pd.to_datetime(second_contract)
                second_data = second_product_data[
                    second_product_data['expiration_date'].dt.date == second_contract_date.date()]

        # Apply calculation type transformations
        if calculation_type != 'price':
            first_data = calculate_returns(first_data, calculation_type)
            second_data = calculate_returns(second_data, calculation_type)

            if first_data.empty or second_data.empty:
                error_message = "Insufficient data to calculate returns"
                return error_message, None, None, None, {'display': 'block'}

            # For returns, we use the calculated 'value' column
            value_column = 'value'
        else:
            # For price, we use the settlement_price column
            value_column = 'settlement_price'

        # Merge the two datasets on trade_date
        merged_data = pd.merge(
            first_data[['trade_date', value_column]].rename(columns={value_column: f'{first_product}_value'}),
            second_data[['trade_date', value_column]].rename(columns={value_column: f'{second_product}_value'}),
            on='trade_date',
            how='inner'
        )

        if merged_data.empty or len(merged_data) < 2:
            error_message = "Insufficient overlapping data points for correlation calculation"
            return error_message, None, None, None, {'display': 'block'}

        # Main correlation calculation - update to use rolling window if applicable
        if rolling_window and rolling_window < len(merged_data):
            # Calculate correlation using the rolling window (most recent periods)
            # Sort data by date and take the last 'rolling_window' periods
            recent_data = merged_data.sort_values('trade_date').tail(rolling_window)
            correlation = recent_data[f'{first_product}_value'].corr(recent_data[f'{second_product}_value'])
            correlation_description = f"Correlation (last {rolling_window} days): "
        else:
            # Calculate full-period correlation
            correlation = merged_data[f'{first_product}_value'].corr(merged_data[f'{second_product}_value'])
            correlation_description = "Correlation (full period): "

        # Create correlation value display
        correlation_value = html.Div([
            html.Span(correlation_description),
            html.Span(f"{correlation:.4f}",
                      style={'color': 'green' if correlation > 0 else 'red'})
        ])

        # Create scatter plot - modified to show only last window days
        scatter_fig = go.Figure()
        if rolling_window and rolling_window < len(merged_data):
            # Use only the recent data points for scatter plot
            recent_data = merged_data.sort_values('trade_date').tail(rolling_window)
            scatter_fig.add_trace(go.Scatter(
                x=recent_data[f'{first_product}_value'],
                y=recent_data[f'{second_product}_value'],
                mode='markers',
                marker=dict(
                    size=10,
                    color='rgba(0, 123, 255, 0.7)',
                    line=dict(
                        color='rgba(0, 123, 255, 1.0)',
                        width=1
                    )
                ),
                name=f'Last {rolling_window} days'
            ))
            # Add regression line for recent data
            x_range = np.linspace(recent_data[f'{first_product}_value'].min(),
                                  recent_data[f'{first_product}_value'].max(), 100)
            if len(recent_data) > 1:  # Need at least 2 points for regression
                from scipy import stats
                slope, intercept, r_value, p_value, std_err = stats.linregress(
                    recent_data[f'{first_product}_value'],
                    recent_data[f'{second_product}_value']
                )
                scatter_fig.add_trace(go.Scatter(
                    x=x_range,
                    y=slope * x_range + intercept,
                    mode='lines',
                    line=dict(color='red', width=2),
                    name=f'Regression Line (r={r_value:.4f})'
                ))
        else:
            # If window is too large, use all data points
            scatter_fig.add_trace(go.Scatter(
                x=merged_data[f'{first_product}_value'],
                y=merged_data[f'{second_product}_value'],
                mode='markers',
                marker=dict(
                    size=10,
                    color='rgba(0, 123, 255, 0.7)',
                    line=dict(
                        color='rgba(0, 123, 255, 1.0)',
                        width=1
                    )
                ),
                name='All Data Points'
            ))
            # Add regression line for all data
            x_range = np.linspace(merged_data[f'{first_product}_value'].min(),
                                  merged_data[f'{first_product}_value'].max(), 100)
            if len(merged_data) > 1:  # Need at least 2 points for regression
                from scipy import stats
                slope, intercept, r_value, p_value, std_err = stats.linregress(
                    merged_data[f'{first_product}_value'],
                    merged_data[f'{second_product}_value']
                )
                scatter_fig.add_trace(go.Scatter(
                    x=x_range,
                    y=slope * x_range + intercept,
                    mode='lines',
                    line=dict(color='red', width=2),
                    name=f'Regression Line (r={r_value:.4f})'
                ))

        # Get description for plot titles based on grouping
        def get_grouping_description(group_mode, contract_date):
            date_obj = pd.to_datetime(contract_date)
            if group_mode == 'monthly':
                return f"{date_obj.strftime('%b %Y')} contract"
            elif group_mode == 'quarterly':
                return f"Q{date_obj.quarter} {date_obj.year} contracts"
            elif group_mode == 'season':
                season = "Summer" if 5 <= date_obj.month <= 9 else "Winter"
                return f"{season} {date_obj.year} contracts"
            elif group_mode == 'calendar':
                return f"{date_obj.year} contracts"
            return ""

        first_desc = get_grouping_description(grouping_mode, first_contract)
        second_desc = get_grouping_description(grouping_mode, second_contract)

        # Scatter plot title now includes grouping information
        scatter_fig.update_layout(
            title=f"Correlation: {first_product} ({first_desc}) vs {second_product} ({second_desc})",
            xaxis_title=f"{first_product} {calculation_type.replace('_', ' ').title()}",
            yaxis_title=f"{second_product} {calculation_type.replace('_', ' ').title()}",
            height=500,
            margin=dict(l=40, r=40, t=40, b=40),
            template="plotly_white"
        )

        # Create time series plot for the products
        timeseries_fig = go.Figure()

        # Sort data chronologically to ensure proper line connections
        merged_data = merged_data.sort_values('trade_date')

        # Use the raw values directly without normalization
        timeseries_fig.add_trace(go.Scatter(
            x=merged_data['trade_date'],
            y=merged_data[f'{first_product}_value'],
            mode='lines+markers',
            name=first_product,
            line=dict(width=2),
            marker=dict(size=7)
        ))

        timeseries_fig.add_trace(go.Scatter(
            x=merged_data['trade_date'],
            y=merged_data[f'{second_product}_value'],
            mode='lines+markers',
            name=second_product,
            line=dict(width=2, dash='dash'),
            marker=dict(size=7)
        ))
        # Get default date range focusing on the last year
        if not merged_data.empty:
            max_date = merged_data['trade_date'].max()
            one_year_before = max_date - pd.DateOffset(years=1)
            # Find closest available date if one_year_before is before the earliest date
            if one_year_before < merged_data['trade_date'].min():
                one_year_before = merged_data['trade_date'].min()
            # Set initial range to focus on last year
            timeseries_fig.update_layout(
                xaxis=dict(
                    range=[one_year_before, max_date],
                    autorange=False
                )
            )
        # Setup dual y-axes for cases where the scales are very different
        if abs(merged_data[f'{first_product}_value'].mean() / merged_data[f'{second_product}_value'].mean() - 1) > 0.5:
            # Values are significantly different, use dual axes
            timeseries_fig.update_layout(
                yaxis=dict(
                    title=f"{first_product} {calculation_type.replace('_', ' ').title()}",
                    titlefont=dict(color="#1f77b4"),
                    tickfont=dict(color="#1f77b4")
                ),
                yaxis2=dict(
                    title=f"{second_product} {calculation_type.replace('_', ' ').title()}",
                    titlefont=dict(color="#ff7f0e"),
                    tickfont=dict(color="#ff7f0e"),
                    anchor="x",
                    overlaying="y",
                    side="right"
                )
            )

            # Update second trace to use second y-axis
            timeseries_fig.data[1].update(yaxis="y2")

            y_axis_title = None  # Already set in the dual axes
        else:
            # Values are comparable, use a single axis
            y_axis_title = calculation_type.replace('_', ' ').title()

        # Update the layout with enhanced appearance and better readability
        layout_updates = {
            "title": f"Time Series: {first_product} ({first_desc}) vs {second_product} ({second_desc})",
            "xaxis_title": "Trade Date",
            "height": 500,
            "template": "plotly_white",
            "legend": dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1
            ),
            "hovermode": "x unified",  # Show all points at the same x position
            "margin": dict(l=50, r=50, t=50, b=50)
        }

        # Add y-axis title only for single axis case
        if y_axis_title:
            layout_updates["yaxis_title"] = y_axis_title

        timeseries_fig.update_layout(**layout_updates)

        # Add range slider for easy date navigation
        timeseries_fig.update_xaxes(
            rangeslider_visible=True,
            rangeslider_thickness=0.05
        )

        # Create the historical rolling correlation chart
        historical_corr_fig = go.Figure()

        # Process data for the historical correlation
        if len(merged_data) >= rolling_window:
            # Calculate the rolling correlation for historical data
            sorted_data = merged_data.sort_values('trade_date')
            historical_rolling_corr = sorted_data[f'{first_product}_value'].rolling(rolling_window).corr(
                sorted_data[f'{second_product}_value'])

            # Prepare data for plotting
            corr_data = pd.DataFrame({
                'date': sorted_data['trade_date'],
                'correlation': historical_rolling_corr
            }).dropna()

            # Add the main rolling correlation line
            historical_corr_fig.add_trace(go.Scatter(
                x=corr_data['date'],
                y=corr_data['correlation'],
                mode='lines',
                name=f'{rolling_window}-Day Rolling Correlation',
                line=dict(width=3, color='purple'),
            ))

            # Add fill to highlight positive and negative correlation regions
            historical_corr_fig.add_trace(go.Scatter(
                x=corr_data['date'],
                y=corr_data['correlation'],
                mode='none',
                fill='tozeroy',
                fillcolor='rgba(0, 128, 0, 0.2)',  # Light green for positive areas
                hoverinfo='skip',
                showlegend=False,
                visible=True
            ))

            # Add horizontal reference lines at key correlation values
            for level, color, dash in [
                (1.0, 'green', 'dash'),
                (0.5, 'green', 'dot'),
                (0.0, 'gray', 'solid'),
                (-0.5, 'red', 'dot'),
                (-1.0, 'red', 'dash')
            ]:
                historical_corr_fig.add_shape(
                    type="line",
                    x0=corr_data['date'].min(),
                    y0=level,
                    x1=corr_data['date'].max(),
                    y1=level,
                    line=dict(color=color, width=1, dash=dash)
                )
                # Focus on last year by default, same as time series
                if not corr_data.empty:
                    max_date = corr_data['date'].max()
                    one_year_before = max_date - pd.DateOffset(years=1)
                    # Find closest available date if one_year_before is before the earliest date
                    if one_year_before < corr_data['date'].min():
                        one_year_before = corr_data['date'].min()
                    # Set initial range to focus on last year
                    historical_corr_fig.update_layout(
                        xaxis=dict(
                            range=[one_year_before, max_date],
                            autorange=False
                        )
                    )
            # Update layout
            historical_corr_fig.update_layout(
                title=f"Historical {rolling_window}-Day Rolling Correlation",
                xaxis_title="Date",
                yaxis_title="Correlation Coefficient",
                yaxis=dict(
                    range=[-1.1, 1.1],
                    tickvals=[-1, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1],
                    zeroline=True,
                    zerolinecolor='black',
                    zerolinewidth=2
                ),
                height=400,
                template="plotly_white",
                margin=dict(l=50, r=50, t=50, b=50),
                hovermode="x unified",
                hoverlabel=dict(
                    bgcolor="white",
                    font_size=12
                ),
                # Add annotations for correlation strength reference
                annotations=[
                    dict(x=1.02, y=0.9, xref="paper", yref="y", text="Strong Positive", showarrow=False,
                         font=dict(color="green")),
                    dict(x=1.02, y=0.5, xref="paper", yref="y", text="Moderate Positive", showarrow=False,
                         font=dict(color="green")),
                    dict(x=1.02, y=0, xref="paper", yref="y", text="No Correlation", showarrow=False,
                         font=dict(color="gray")),
                    dict(x=1.02, y=-0.5, xref="paper", yref="y", text="Moderate Negative", showarrow=False,
                         font=dict(color="red")),
                    dict(x=1.02, y=-0.9, xref="paper", yref="y", text="Strong Negative", showarrow=False,
                         font=dict(color="red"))
                ]
            )

            # Add range slider and selector buttons
            historical_corr_fig.update_xaxes(
                rangeslider_visible=True,
                rangeslider_thickness=0.05,
                rangeselector=dict(
                    buttons=list([
                        dict(count=1, label="1m", step="month", stepmode="backward"),
                        dict(count=6, label="6m", step="month", stepmode="backward"),
                        dict(count=1, label="YTD", step="year", stepmode="todate"),
                        dict(count=1, label="1y", step="year", stepmode="backward"),
                        dict(step="all")
                    ])
                )
            )

            # Create the historical correlation plot component
            historical_corr_plot = dcc.Graph(figure=historical_corr_fig)
        else:
            # Not enough data for historical correlation
            historical_corr_plot = html.Div([
                html.P("Insufficient data for historical rolling correlation.",
                       style={'textAlign': 'center', 'color': 'red', 'marginTop': '50px'})
            ])

        # Create data table
        table_data = merged_data.copy()
        # Sort data chronologically for the table, then reverse to show latest dates first
        table_data = table_data.sort_values('trade_date', ascending=False)
        if not table_data.empty:
            # Get last 3 months of data
            max_date = table_data['trade_date'].max()
            three_months_ago = max_date - pd.DateOffset(months=3)
            table_data = table_data[table_data['trade_date'] >= three_months_ago]
        table_data['trade_date'] = table_data['trade_date'].dt.strftime('%Y-%m-%d')

        # Add correlation column for each row using the specified rolling window
        rolling_window = max(2, min(rolling_window, len(merged_data)))  # Ensure window is valid

        # Need to calculate rolling correlation on chronologically sorted data
        sorted_data = merged_data.sort_values('trade_date')

        if len(sorted_data) >= rolling_window:
            # Calculate rolling correlation with the specified window
            rolling_corr = sorted_data[f'{first_product}_value'].rolling(rolling_window).corr(
                sorted_data[f'{second_product}_value'])

            # Map the rolling correlation back to the original sorted dates
            corr_dict = dict(zip(sorted_data.index, rolling_corr))
            table_data['rolling_correlation'] = table_data.index.map(corr_dict)

            # Create a new column for the window size display
            rolling_window_label = f"{rolling_window}D Rolling Correlation"
        else:
            # Not enough data for rolling correlation with this window
            rolling_window_label = "Insufficient data for rolling correlation"

        # Format table columns
        table_columns = [
            {"name": "Trade Date", "id": "trade_date"},
            {"name": f"{first_product}", "id": f"{first_product}_value", "type": "numeric",
             "format": {"specifier": ".4f"}},
            {"name": f"{second_product}", "id": f"{second_product}_value", "type": "numeric",
             "format": {"specifier": ".4f"}}
        ]

        if 'rolling_correlation' in table_data.columns:
            table_columns.append(
                {"name": rolling_window_label, "id": "rolling_correlation", "type": "numeric",
                 "format": {"specifier": ".4f"}}
            )

        # Create data table container separate from rolling correlation plot
        data_table_container = html.Div(dash_table.DataTable(
            id='correlation-data-table',
            columns=table_columns,
            data=table_data.to_dict('records'),
            style_table={
                'overflowX': 'auto',
                'height': '300px',
                'overflowY': 'auto'
            },
            style_cell={
                'textAlign': 'center',
                'padding': '8px',
                'fontSize': 12,
                'color': 'black'
            },
            style_header={
                'backgroundColor': 'rgb(230, 230, 230)',
                'fontWeight': 'bold',
                'textAlign': 'center',
                'fontSize': 14,
                'color': 'black'
            },
            style_data_conditional=[
                                       {
                                           'if': {'row_index': 'odd'},
                                           'backgroundColor': 'rgb(248, 248, 248)'
                                       }
                                   ] + (
                                       [
                                           {
                                               'if': {
                                                   'column_id': 'rolling_correlation',
                                                   'filter_query': '{rolling_correlation} > 0'
                                               },
                                               'color': 'green',
                                               'fontWeight': 'bold'
                                           },
                                           {
                                               'if': {
                                                   'column_id': 'rolling_correlation',
                                                   'filter_query': '{rolling_correlation} < 0'
                                               },
                                               'color': 'red',
                                               'fontWeight': 'bold'
                                           }
                                       ] if 'rolling_correlation' in table_data.columns else []
                                   ),
            sort_action='native',
            filter_action='native',
            page_action='native',
            page_size=15
        ))

        # Create plot components
        scatter_plot = dcc.Graph(figure=scatter_fig)
        timeseries_plot = dcc.Graph(figure=timeseries_fig)

        # Show results container
        results_style = {'width': '90%', 'margin': '0 auto', 'marginTop': '30px', 'display': 'block'}

        # Add a time series chart for rolling correlation if enough data points
        rolling_corr_plot = None
        if len(sorted_data) >= rolling_window:
            # Create rolling correlation time series
            rolling_corr_fig = go.Figure()

            # Process data for the plot
            plot_data = sorted_data.copy()
            plot_data['rolling_correlation'] = rolling_corr
            plot_data = plot_data.dropna(subset=['rolling_correlation'])

            # Add the rolling correlation line
            rolling_corr_fig.add_trace(go.Scatter(
                x=plot_data['trade_date'],
                y=plot_data['rolling_correlation'],
                mode='lines+markers',
                name=f'{rolling_window}-Day Rolling Correlation',
                line=dict(width=2, color='purple'),
                marker=dict(size=7)
            ))

            # Add a zero line for reference
            rolling_corr_fig.add_shape(
                type="line",
                x0=plot_data['trade_date'].min(),
                y0=0,
                x1=plot_data['trade_date'].max(),
                y1=0,
                line=dict(color="gray", width=1, dash="dash")
            )

            # Update layout
            rolling_corr_fig.update_layout(
                title=f"{rolling_window}-Day Rolling Correlation: {first_product} vs {second_product}",
                xaxis_title="Trade Date",
                yaxis_title="Correlation Coefficient",
                yaxis=dict(
                    range=[-1.1, 1.1],  # Correlation is between -1 and 1
                    tickvals=[-1, -0.5, 0, 0.5, 1],
                    zeroline=True,
                    zerolinecolor='gray',
                    zerolinewidth=1
                ),
                height=400,
                template="plotly_white",
                margin=dict(l=50, r=50, t=50, b=50),
                hovermode="x unified"
            )

            # Add range slider
            rolling_corr_fig.update_xaxes(
                rangeslider_visible=True,
                rangeslider_thickness=0.05
            )

            rolling_corr_plot = dcc.Graph(figure=rolling_corr_fig)

        # Return all components
        # The order of components in the correlation-data-table-container div will be:
        # 1. Historical rolling correlation plot
        # 2. Data table
        data_table_div = html.Div([
            html.Div([
                historical_corr_plot
            ], style={'marginBottom': '30px'}),
            data_table_container
        ])

        return correlation_value, scatter_plot, timeseries_plot, data_table_div, results_style

    except Exception as e:
        print(f"Error calculating correlation: {e}")
        error_message = f"Error calculating correlation: {str(e)}"
        return error_message, None, None, None, {'display': 'block'}
 