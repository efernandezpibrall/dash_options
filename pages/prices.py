import dash
from dash import html, dcc
import pandas as pd
import numpy as np
from pandas.tseries.offsets import MonthEnd
from dash import Dash, html, dcc, callback, Input, Output, dash_table, State, callback_context
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import datetime
import configparser # Import the built-in configparser
# Trino
from trino.dbapi import connect
from trino.auth import JWTAuthentication
import os
# Define read_and_prepare_data function locally to avoid import execution issues
def parse_maturity_date(s):
    """Parse maturity date from string to datetime."""
    try:
        if len(s) > 5:
            return pd.to_datetime(s, format='%m/%d/%Y', errors='coerce')
        else:
            return pd.to_datetime(s, format='%b%y', errors='coerce')
    except Exception:
        return pd.NaT

def read_and_prepare_data(query, conn):
    """Read data from database and prepare it."""
    df = read_table_conn(conn, query)
    df['maturity_date'] = df['maturity_date'].apply(parse_maturity_date)
    df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
    df['expiration_date'] = pd.to_datetime(df['expiration_date'], errors='coerce')
    return df

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
try:
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
except Exception as e:
    print(f"Warning: Failed to create gas connection: {e}")
    conn_gas = None

# trinos connection oil
try:
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
except Exception as e:
    print(f"Warning: Failed to create oil connection: {e}")
    conn_oil = None

# trinos connection enverus (for JKM data)
try:
    conn_enverus = connect(
        host= TRINOS_HOST,
        port= TRINOS_PORT,
        user= TRINOS_USERNAME,
        auth= JWTAuthentication(TRINOS_TOKEN),
        http_scheme= "https",
        verify= False,
        catalog= "transformed",
        schema= "enverus",
    )
except Exception as e:
    print(f"Warning: Failed to create enverus connection: {e}")
    conn_enverus = None


# Function to get prices from enverus, inputs: conn, code, category, version_name, from_COB, to_COB
def get_enverus_prices(conn, code, category, version_name, from_COB, to_COB):
    # Get prices from enverus
    query = '''SELECT   code,
                        ondate AS COB,
                        currency,
                        units,
                        forward_curve_tenors_expiry AS expiry,
                        forward_curve_tenors_absolute AS contract,
                        forward_curve_tenors_value AS value
                        FROM enverus.curve
                        WHERE code='{}'
                            AND category='{}'
                            AND version_name='{}'
                            AND ondate_index >= {}
                            AND ondate_index <= {}
                            AND forward_curve_tenors_absolute NOT IN ('M-1','M-2','M-3')
                            AND forward_curve_tenors_value is not null
                        ORDER BY ondate, forward_curve_tenors_tenor
                            '''.format(code,
                                       category,
                                       version_name,
                                       from_COB,
                                       to_COB)
    # Get enverus prices from data lake
    df_enverus = read_table_conn(conn, query)

    # Convert COB to date
    df_enverus['COB'] = pd.to_datetime(df_enverus.COB, format='%Y-%m-%d')

    df_enverus['contract_date'] = np.where(df_enverus['contract']=='SPOT',
                                         df_enverus['COB'],
                                         pd.to_datetime(df_enverus.contract, format='%YM%m', errors='coerce')
                                          )
    # Convert contract to date (setting it at the end of the month)
    df_enverus['expiry'] = pd.to_datetime(df_enverus.expiry, format='%Y-%m-%d')

    return df_enverus


query_gas = '''
SELECT
  trade_date, hub, product, strip as maturity_date, expiration_date, contract, contract_type, settlement_price
FROM
  raw."ice_gas"."cleared_gas"
WHERE
  contract in ('H')
  OR contract = 'PEG'
  OR (contract = 'M' AND product = 'UK NBP Natural Gas Futures')
  OR (product = 'Dutch TTF Natural Gas Futures' AND contract = 'TFM')
  AND ("contract_type" != 'P' AND "contract_type" != 'C')
  AND product NOT LIKE '%daily%'
  AND strike IS NULL
'''

query_oil = '''
SELECT
  trade_date, hub, product, strip as maturity_date, expiration_date, contract, contract_type, settlement_price
FROM
  raw."ice_oil"."cleared_oil"
WHERE
  (product = 'Brent Crude Futures' AND contract = 'B')
  AND strike IS NULL
'''
# Main gas data
# Get underlying prices from data lake
try:
    if conn_gas is not None:
        df_options_gas = read_and_prepare_data(query_gas, conn_gas)
        
        # Updating the 'product' column
        df_options_gas .loc[df_options_gas ['contract'] == 'H', 'product'] = 'HH'
        df_options_gas .loc[df_options_gas ['contract'] == 'PEG', 'product'] = 'PEG'
        df_options_gas .loc[df_options_gas ['contract'] == 'M', 'product'] = 'NBP'
        df_options_gas .loc[df_options_gas ['contract'] == 'M', 'contract'] = 'NBP'
        df_options_gas .loc[df_options_gas ['contract'] == 'TFM', 'product'] = 'TFM'
    else:
        print("Warning: Gas connection is None, skipping gas data")
        df_options_gas = pd.DataFrame()
except Exception as e:
    print(f"Warning: Failed to load gas data: {e}")
    df_options_gas = pd.DataFrame()

# Main oil data
# Get underlying prices from data lake
try:
    if conn_oil is not None:
        df_options_oil = read_and_prepare_data(query_oil, conn_oil)
        
        # Updating the 'product' columns where 'contract' is 'Brent Crude Futures'
        df_options_oil .loc[df_options_oil ['contract'] == 'B', 'product'] = 'Brent'
        df_options_oil .loc[df_options_oil ['contract'] == 'B', 'contract'] = 'Brent'
    else:
        print("Warning: Oil connection is None, skipping oil data")
        df_options_oil = pd.DataFrame()
except Exception as e:
    print(f"Warning: Failed to load oil data: {e}")
    df_options_oil = pd.DataFrame()

# Get JKM data from Enverus
try:
    if conn_enverus is not None:
        # Get recent dates for data fetching (last 30 days to match ICE data availability)
        import datetime
        end_date = datetime.datetime.now()
        start_date = end_date - datetime.timedelta(days=30)
        
        df_enverus_jkm = get_enverus_prices(conn=conn_enverus,
                                           code='ICE_JKM_MO',
                                           category='FINANCIAL',
                                           version_name='FINAL',
                                           from_COB=start_date.strftime("%Y%m%d"),
                                           to_COB=end_date.strftime("%Y%m%d"))
        
        # Rename columns to match the ICE data structure
        df_enverus_jkm.rename(columns={"COB":'trade_date',
                                      "contract_date":'maturity_date',
                                      "expiry":'expiration_date',
                                      "value":'settlement_price'}, inplace=True)
        
        # Drop unnecessary columns and add required ones
        df_enverus_jkm.drop(columns=['currency','units','contract'], inplace=True)
        df_enverus_jkm['hub'] = None
        df_enverus_jkm['product'] = 'JKM'
        df_enverus_jkm['contract'] = 'JKM'
        df_enverus_jkm['contract_type'] = None
        
        # Remap the code column to match trades_options_valuation.py
        df_enverus_jkm['code'] = df_enverus_jkm['code'].map({'ICE_JKM_MO': 'JKM'}).fillna(df_enverus_jkm['code'])
        
    else:
        print("Warning: Enverus connection is None, skipping JKM data")
        df_enverus_jkm = pd.DataFrame()
except Exception as e:
    print(f"Warning: Failed to load Enverus JKM data: {e}")
    df_enverus_jkm = pd.DataFrame()

# Combining the DataFrames
df_options = pd.concat([df_options_gas, df_options_oil, df_enverus_jkm], ignore_index=True)


# Styles removed - using CSS classes from assets/styles.css

layout = html.Div([
    # Download components for table exports
    dcc.Download(id="download-prices-table"),
    
    # Top section with controls following greeks.py pattern
    html.Div([
        # Controls section - matching greeks.py inline layout
        html.Div([
            # First row - Main controls
            html.Div([
                html.Label('Products:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='prices-unified-product-dropdown',
                    options=[],  # Will be populated in callback
                    value=[],
                    multi=True,
                    placeholder='Select products...',
                    className="inline-dropdown-multi"
                ),
                html.Label('Current Date:', className="inline-filter-label"),
                dcc.DatePickerSingle(
                    id='prices-unified-date-picker',
                    display_format='YYYY-MM-DD',
                    with_portal=True,
                    className="inline-date-picker"
                ),
                html.Label('Previous Date:', className="inline-filter-label"),
                dcc.DatePickerSingle(
                    id='prices-table-prev-date-picker',
                    display_format='YYYY-MM-DD',
                    with_portal=True,
                    className="inline-date-picker"
                )
            ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap', 'margin-bottom': '8px'}),
            
            # Second row - Grouping only
            html.Div([
                html.Label('Group By:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='prices-unified-grouping-dropdown',
                    options=[
                        {'label': 'Monthly', 'value': 'monthly'},
                        {'label': 'Quarterly', 'value': 'quarterly'},
                        {'label': 'Season', 'value': 'season'},
                        {'label': 'Calendar', 'value': 'calendar'}
                    ],
                    value='monthly',  # Default selection
                    clearable=False,
                    className="inline-dropdown-aggregation"
                )
            ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap'})
        ], className="inline-section-header")
    ], style={'margin-bottom': '24px'}),
    # Graphs section with Enterprise Standard header
    html.H3('Prices Charts', className='greeks-title'),
    html.Div(
        id='prices-graphs-container',
        style={'margin-bottom': '24px'}
    ),
    
    # Tables section with Enterprise Standard headers
    html.Div([
        html.Div([
            html.H3('Current Prices', className='greeks-title', style={'display': 'inline-block', 'margin-right': '15px'}),
            html.Button(
                'Export',
                id='export-prices-table-btn',
                className='custom-export-btn',
                style={'display': 'inline-block'}
            )
        ], style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '12px'}),
        html.Div(
            id='prices-tables-container'
        )
    ])
    ]
)


# Unified initialize date pickers callback
@dash.callback(
    [Output('prices-unified-date-picker', 'date'),
     Output('prices-table-prev-date-picker', 'date')],
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=False
)
def init_date_pickers(n_clicks):
    try:
        # Create a new default date (today)
        default_date = pd.Timestamp.now().strftime('%Y-%m-%d')
        default_prev_date = (pd.Timestamp.now() - pd.Timedelta(days=1)).strftime('%Y-%m-%d')

        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None or not isinstance(df_options, pd.DataFrame):
            return default_date, default_prev_date

        # Check if required columns exist
        if 'trade_date' in df_options.columns:
            # Convert date column safely
            temp_df = df_options.copy()
            temp_df['trade_date'] = pd.to_datetime(temp_df['trade_date'], errors='coerce')

            # Get clean unique dates
            valid_dates = temp_df['trade_date'].dropna().unique()

            if len(valid_dates) > 0:
                # Sort dates
                all_dates = sorted(valid_dates)

                # Get the latest date
                latest_date = pd.Timestamp(all_dates[-1]).strftime('%Y-%m-%d')

                # Get previous date if available
                if len(all_dates) > 1:
                    prev_date = pd.Timestamp(all_dates[-2]).strftime('%Y-%m-%d')
                else:
                    prev_date = latest_date

                return latest_date, prev_date

        return default_date, default_prev_date

    except Exception as e:
        print(f"Error in init_date_pickers: {e}")
        return pd.Timestamp.now().strftime('%Y-%m-%d'), (pd.Timestamp.now() - pd.Timedelta(days=1)).strftime('%Y-%m-%d')


# Helper function to safely find the closest date
def find_closest_date(df, date_column, target_date):
    """
    Safely find the closest date to the target date in the dataframe.

    Args:
        df: DataFrame containing dates
        date_column: Name of the column containing dates
        target_date: Target date to find closest to

    Returns:
        Closest date or None if error/empty dataframe
    """
    try:
        if df.empty or date_column not in df.columns:
            return None

        # Ensure date_column contains datetime objects
        dates = pd.to_datetime(df[date_column], errors='coerce')
        dates = dates.dropna()

        if dates.empty:
            return None

        # Find closest date without using .iloc directly
        date_diff = abs(dates - target_date)
        min_diff_idx = date_diff.argmin()

        if min_diff_idx < len(dates):
            return dates.iloc[min_diff_idx]
        return None
    except Exception as e:
        print(f"Error finding closest date: {e}")
        return None



# Initialize products dropdown callback
@dash.callback(
    [Output('prices-unified-product-dropdown', 'options'),
     Output('prices-unified-product-dropdown', 'value')],
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=False
)
def init_unified_products(n_clicks):
    try:
        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None or not isinstance(df_options, pd.DataFrame):
            return [], []

        # Check if required columns exist
        if 'contract' not in df_options.columns:
            return [], []

        # Get unique, non-null products
        product_codes = df_options['contract'].dropna().unique()

        if len(product_codes) == 0:
            return [], []

        # Sort product codes
        product_codes = sorted(product_codes)

        dropdown_options = [{'label': code, 'value': code} for code in product_codes]
        dropdown_values = product_codes[:5] if len(
            product_codes) > 5 else product_codes  # Limit to 5 products initially
        return dropdown_options, dropdown_values

    except Exception as e:
        print(f"Error in init_unified_products: {e}")
        return [], []


# Update the manually set previous date callback to reference the unified date picker
@dash.callback(
    Output('prices-table-prev-date-picker', 'date', allow_duplicate=True),
    [Input('refresh-options-data', 'n_clicks'),
     State('prices-unified-date-picker', 'date')],
    prevent_initial_call=True
)

def set_prev_date(n_clicks, current_date):
    try:
        if n_clicks is None or current_date is None:
            raise dash.exceptions.PreventUpdate

        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None or not isinstance(df_options, pd.DataFrame):
            raise dash.exceptions.PreventUpdate

        # Convert current date
        current_date_dt = pd.to_datetime(current_date)

        # Convert dataframe date column
        df_options['trade_date'] = pd.to_datetime(df_options['trade_date'], errors='coerce')

        # Drop NaT values and get unique dates
        valid_dates = df_options['trade_date'].dropna().unique()

        if len(valid_dates) == 0:
            raise dash.exceptions.PreventUpdate

        all_dates = sorted(valid_dates)

        # Find previous closest date
        prev_date = None
        for date in reversed(all_dates):
            if pd.Timestamp(date) < current_date_dt:
                prev_date = pd.Timestamp(date).strftime('%Y-%m-%d')
                break

        # If no previous date found, just return the current date
        return prev_date if prev_date else current_date

    except Exception as e:
        print(f"Error in set_prev_date: {e}")
        raise dash.exceptions.PreventUpdate

# Initialize product dropdown
@dash.callback(
    [Output('prices-table-product-dropdown', 'options'),
     Output('prices-table-product-dropdown', 'value')],
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=False
)
def init_products(n_clicks):
    try:
        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None or not isinstance(df_options, pd.DataFrame):
            return [], []

        # Check if required columns exist
        if 'contract' not in df_options.columns:
            return [], []

        # Get unique, non-null products
        product_codes = df_options['contract'].dropna().unique()

        if len(product_codes) == 0:
            return [], []

        # Sort product codes
        product_codes = sorted(product_codes)

        dropdown_options = [{'label': code, 'value': code} for code in product_codes]
        dropdown_values = product_codes[:5] if len(
            product_codes) > 5 else product_codes  # Limit to 5 products initially
        return dropdown_options, dropdown_values

    except Exception as e:
        print(f"Error in init_products: {e}")
        return [], []


# ============== CONTENT UPDATE CALLBACKS =================

# Update graphs callback
@dash.callback(
    Output('prices-graphs-container', 'children'),
    [Input('prices-unified-date-picker', 'date'),
     Input('prices-unified-grouping-dropdown', 'value'),
     Input('prices-unified-product-dropdown', 'value')],
    prevent_initial_call=True
)


def update_graphs(selected_date, grouping_mode, selected_products):  # Added grouping_mode parameter
    try:
        ctx = callback_context
        if not ctx.triggered:
            raise dash.exceptions.PreventUpdate

        if selected_date is None or not selected_products:  # Check if products are selected
            return html.Div()

        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None or not isinstance(df_options, pd.DataFrame):
            return html.Div(html.H4("Error: DataFrame not available", style={'color': 'red'}))

        # Check if required columns exist
        required_columns = ['trade_date', 'contract', 'maturity_date', 'settlement_price']
        missing_columns = [col for col in required_columns if col not in df_options.columns]

        if missing_columns:
            return html.Div(html.H4(f"Error: Missing columns: {', '.join(missing_columns)}", style={'color': 'red'}))

        # Make a copy to avoid modifying the original
        df = df_options.copy()

        # Convert date columns safely
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
        df['maturity_date'] = pd.to_datetime(df['maturity_date'], errors='coerce')

        # Convert the selected date
        selected_date = pd.to_datetime(selected_date)

        # Drop rows with NaT in date columns
        df = df.dropna(subset=['trade_date', 'maturity_date'])

        # Ensure settlement_price is numeric
        df['settlement_price'] = pd.to_numeric(df['settlement_price'], errors='coerce')
        df = df.dropna(subset=['settlement_price'])

        if df.empty:
            return html.Div(html.H4("No valid data available after cleaning", style={'color': 'red'}))

        # Filter by selected products
        df = df[df['contract'].isin(selected_products)]

        if df.empty:
            return html.Div(html.H4("No data available for selected products", style={'color': 'red'}))

        graphs = []
        row = []

        product_codes = sorted(df['product'].unique())

        if not product_codes:
            return html.Div(html.H4("No products found in data", style={'color': 'red'}))

        for i, code in enumerate(product_codes):
            try:
                fig = go.Figure()
                code_df = df[df['product'] == code]

                if code_df.empty:
                    continue

                # Get the 5 most recent dates safely
                recent_dates = code_df['trade_date'].drop_duplicates().sort_values(ascending=False).head(5).values
                has_selected_or_closest = False

                for cob_date in recent_dates:
                    cob_date = pd.Timestamp(cob_date)
                    cob_data = code_df[code_df['trade_date'] == cob_date]

                    if cob_data.empty:
                        continue

                    # Apply grouping to the data
                    if grouping_mode != 'monthly':
                        try:
                            cob_data = group_data_by_period(cob_data, grouping_mode)
                            # After grouping, we need to ensure we have maturity_date and settlement_price
                            if 'maturity_date' not in cob_data.columns and 'period' in cob_data.columns:
                                # For grouped data, use period as x-axis
                                x_values = cob_data['period']

                                # Sort periods based on grouping_mode
                                if grouping_mode == 'quarterly':
                                    def quarter_key(q_str):
                                        try:
                                            year, quarter = q_str.split('-')
                                            return (int(year), int(quarter[1]))
                                        except:
                                            return (9999, 0)

                                    sorted_indices = sorted(range(len(x_values)),
                                                            key=lambda i: quarter_key(x_values.iloc[i]))
                                    x_values = x_values.iloc[sorted_indices]
                                    cob_data = cob_data.iloc[sorted_indices]

                                elif grouping_mode == 'season':
                                    def season_key(s_str):
                                        try:
                                            year, season = s_str.split('-')
                                            season_val = 0 if season == 'Winter' else 1
                                            return (int(year), season_val)
                                        except:
                                            return (9999, 0)

                                    sorted_indices = sorted(range(len(x_values)),
                                                            key=lambda i: season_key(x_values.iloc[i]))
                                    x_values = x_values.iloc[sorted_indices]
                                    cob_data = cob_data.iloc[sorted_indices]

                                elif grouping_mode == 'calendar':
                                    sorted_indices = sorted(range(len(x_values)),
                                                            key=lambda i: int(x_values.iloc[i]) if x_values.iloc[
                                                                i].isdigit() else 9999)
                                    x_values = x_values.iloc[sorted_indices]
                                    cob_data = cob_data.iloc[sorted_indices]
                            else:
                                # Sort by maturity date to ensure line connects points properly
                                cob_data = cob_data.sort_values('maturity_date')
                                x_values = cob_data['maturity_date']
                        except Exception as e:
                            print(f"Error grouping data for graphs: {e}")
                            # Fall back to ungrouped data
                            cob_data = cob_data.sort_values('maturity_date')
                            x_values = cob_data['maturity_date']
                    else:
                        # Sort by maturity date to ensure line connects points properly
                        cob_data = cob_data.sort_values('maturity_date')
                        x_values = cob_data['maturity_date']

                    is_selected_date = cob_date.date() == selected_date.date()
                    if is_selected_date:
                        has_selected_or_closest = True

                    line_style = dict(
                        width=1.5,
                        color='blue',
                        dash='dash'
                    ) if is_selected_date else dict(width=1)

                    legend_name = f"{cob_date.strftime('%Y-%m-%d')}"
                    if is_selected_date:
                        legend_name = f"★ {legend_name} (SELECTED) ★"

                    # Only add trace if we have data
                    if not cob_data.empty and len(cob_data) > 0:
                        # Use the appropriate column based on grouping
                        y_column = 'settlement_price'
                        if 'settlement_price' not in cob_data.columns and grouping_mode != 'monthly':
                            # For grouped data where the column name might have changed
                            y_column = cob_data.columns[-1]  # Assuming last column is the value

                        fig.add_trace(go.Scatter(
                            x=x_values,
                            y=cob_data[y_column],
                            mode='lines+markers',
                            name=legend_name,
                            line=line_style
                        ))

                # If not has_selected_or_closest:
                if not has_selected_or_closest:
                    # Simple and safe approach - find closest date
                    try:
                        # Create a new dataframe with just the dates and their differences
                        date_df = pd.DataFrame({
                            'trade_date': code_df['trade_date'].unique(),
                            'diff': [abs((d - selected_date).total_seconds()) for d in code_df['trade_date'].unique()]
                        })
                        # Sort by difference
                        date_df = date_df.sort_values('diff')
                        # If we have any dates
                        if not date_df.empty:
                            # Get closest date
                            closest_date = date_df.iloc[0]['trade_date']
                            # Get data for this date
                            cob_data = code_df[code_df['trade_date'] == closest_date]

                           # Apply grouping to the data
                            if grouping_mode != 'monthly':
                                try:
                                    cob_data = group_data_by_period(cob_data, grouping_mode)
                                    # After grouping, use period as x-axis if needed
                                    if 'maturity_date' not in cob_data.columns and 'period' in cob_data.columns:
                                        x_values = cob_data['period']
                                    else:
                                        cob_data = cob_data.sort_values('maturity_date')
                                        x_values = cob_data['maturity_date']
                                except Exception as e:
                                    print(f"Error grouping closest date data: {e}")
                                    cob_data = cob_data.sort_values('maturity_date')
                                    x_values = cob_data['maturity_date']
                            else:
                                cob_data = cob_data.sort_values('maturity_date')
                                x_values = cob_data['maturity_date']

                            # Add to plot if we have data
                            if not cob_data.empty:
                                # Use the appropriate column based on grouping
                                y_column = 'settlement_price'
                                if 'settlement_price' not in cob_data.columns and grouping_mode != 'monthly':
                                    # For grouped data where the column name might have changed
                                    y_column = cob_data.columns[-1]  # Assuming last column is the value

                                fig.add_trace(go.Scatter(
                                    x=x_values,
                                    y=cob_data[y_column],
                                    mode='lines+markers',
                                    name=f"⚠ {closest_date.strftime('%Y-%m-%d')} - CLOSEST AVAILABLE",
                                    line=dict(width=1, color='orange', dash='dot')
                                ))
                    except Exception as e:
                        print(f"Error finding closest date: {e}")

                # If still no match, try finding any closest date
                if not has_selected_or_closest and not code_df.empty:
                    try:
                        # Use the safe helper function to find any closest date
                        any_closest_date = find_closest_date(code_df, 'trade_date', selected_date)

                        if any_closest_date is not None and any_closest_date not in recent_dates:
                            cob_data = code_df[code_df['trade_date'] == any_closest_date]

                            if not cob_data.empty:
                                # Apply grouping to the data
                                if grouping_mode != 'monthly':
                                    try:
                                        cob_data = group_data_by_period(cob_data, grouping_mode)
                                        # After grouping, use period as x-axis if needed
                                        if 'maturity_date' not in cob_data.columns and 'period' in cob_data.columns:
                                            x_values = cob_data['period']
                                        else:
                                            cob_data = cob_data.sort_values('maturity_date')
                                            x_values = cob_data['maturity_date']
                                    except Exception as e:
                                        print(f"Error grouping any closest date data: {e}")
                                        cob_data = cob_data.sort_values('maturity_date')
                                        x_values = cob_data['maturity_date']
                                else:
                                    # Sort by maturity date for proper line
                                    cob_data = cob_data.sort_values('maturity_date')
                                    x_values = cob_data['maturity_date']

                                line_style = dict(
                                    width=1,
                                    color='orange',
                                    dash='dot'
                                )

                                # Use the appropriate column based on grouping
                                y_column = 'settlement_price'
                                if 'settlement_price' not in cob_data.columns and grouping_mode != 'monthly':
                                    # For grouped data where the column name might have changed
                                    y_column = cob_data.columns[-1]  # Assuming last column is the value

                                fig.add_trace(go.Scatter(
                                    x=x_values,
                                    y=cob_data[y_column],
                                    mode='lines+markers',
                                    name=f"⚠ {any_closest_date.strftime('%Y-%m-%d')} - CLOSEST AVAILABLE",
                                    line=line_style
                                ))
                    except Exception as e:
                        print(f"Error finding any closest date: {e}")

                # Determine x-axis title based on grouping mode
                x_axis_title = "Maturity Date"
                if grouping_mode == 'quarterly':
                    x_axis_title = "Quarter"
                elif grouping_mode == 'season':
                    x_axis_title = "Season"
                elif grouping_mode == 'calendar':
                    x_axis_title = "Year"
                elif grouping_mode == 'monthly':
                    x_axis_title = "Month"

                fig.update_layout(
                    # Professional Title Styling
                    title=dict(
                        text=f"{code} Price ({grouping_mode.capitalize()})",
                        font=dict(size=18, color='#1f2937', family='Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif'),
                        x=0.5,
                        y=0.96,
                        xanchor='center',
                        yanchor='top'
                    ),
                    
                    # X-Axis Professional Styling  
                    xaxis=dict(
                        title=dict(text='', font=dict(size=12, color='#374151')),
                        showgrid=True,
                        gridcolor='rgba(200, 200, 200, 0.3)',
                        gridwidth=0.5,
                        linecolor='#d1d5db',
                        linewidth=1,
                        tickfont=dict(size=10, color='#6b7280')
                    ),
                    
                    # Y-Axis Professional Styling
                    yaxis=dict(
                        title=dict(text='Settlement Price', font=dict(size=12, color='#374151')),
                        showgrid=True,
                        gridcolor='rgba(200, 200, 200, 0.3)',
                        gridwidth=0.5,
                        linecolor='#d1d5db',
                        linewidth=1,
                        tickfont=dict(size=10, color='#6b7280'),
                        zeroline=True,
                        zerolinecolor='rgba(150, 150, 150, 0.4)',
                        zerolinewidth=1
                    ),
                    
                    # Professional Legend
                    legend=dict(
                        orientation='h',
                        yanchor='top',
                        y=-0.15,
                        xanchor='center',
                        x=0.5,
                        font=dict(size=10, color='#374151'),
                        itemsizing='constant'
                    ),
                    
                    # Professional Background
                    plot_bgcolor='rgba(248, 249, 250, 0.5)',
                    paper_bgcolor='white',
                    margin=dict(l=70, r=70, t=80, b=100),
                    
                    # Enhanced Interactivity
                    hovermode='x unified',
                    hoverlabel=dict(
                        bgcolor='rgba(255, 255, 255, 0.95)',
                        bordercolor='rgba(200, 200, 200, 0.8)',
                        font=dict(size=11, color='#1f2937')
                    )
                )

                row.append(html.Div(dcc.Graph(figure=fig, config={'displayModeBar': False}),
                                    style={'display': 'inline-block', 'width': '33%', 'padding': '0',
                                           'marginBottom': '10px'}))

                if (i + 1) % 3 == 0:
                    graphs.append(html.Div(row, style={'display': 'flex'}))
                    row = []

            except Exception as e:
                print(f"Error creating graph for product {code}: {e}")
                continue

        if row:
            graphs.append(html.Div(row, style={'display': 'flex'}))

        return graphs if graphs else html.Div(html.H4("No graphs could be generated", style={'color': 'red'}))

    except Exception as e:
        print(f"Error in update_graphs: {e}")
        return html.Div(html.H4(f"Error generating graphs: {str(e)}", style={'color': 'red'}))


def group_data_by_period(data, grouping_mode):
    """Group price data by different time periods."""
    try:
        data = data.copy()
        data['maturity_date'] = pd.to_datetime(data['maturity_date'], errors='coerce')
        data = data.dropna(subset=['maturity_date'])

        if grouping_mode == 'monthly':
            data['period'] = data['maturity_date'].dt.strftime('%m-%y')
            return data

        elif grouping_mode == 'quarterly':
            data['quarter'] = data['maturity_date'].dt.quarter
            data['year'] = data['maturity_date'].dt.year
            data['period'] = data.apply(lambda x: f"{x['year']}-Q{x['quarter']}", axis=1)

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
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

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        elif grouping_mode == 'calendar':
            data['period'] = data['maturity_date'].dt.year.astype(str)

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        data['period'] = data['maturity_date'].dt.strftime('%m-%y')
        return data

    except Exception as e:
        print(f"Error in group_data_by_period: {e}")
        # Return original data in case of error
        if 'period' not in data.columns:
            data['period'] = 'unknown'
        return data



# Update tables callback
@dash.callback(
    Output('prices-tables-container', 'children'),
    [Input('prices-unified-date-picker', 'date'),
     Input('prices-table-prev-date-picker', 'date'),
     Input('prices-unified-product-dropdown', 'value'),
     Input('prices-unified-grouping-dropdown', 'value')],
    prevent_initial_call=True
)


def update_tables(selected_date, prev_selected_date, selected_products, grouping_mode):
    try:
        if selected_date is None or not selected_products:
            return html.Div()

        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None or not isinstance(df_options, pd.DataFrame):
            return html.Div(html.H4("Error: DataFrame not available", style={'color': 'red'}))

        # Check if required columns exist
        required_columns = ['trade_date', 'contract', 'maturity_date', 'settlement_price']
        missing_columns = [col for col in required_columns if col not in df_options.columns]

        if missing_columns:
            return html.Div(html.H4(f"Error: Missing columns: {', '.join(missing_columns)}", style={'color': 'red'}))

        # Make a copy to avoid modifying the original
        df = df_options.copy()

        # Convert date columns safely
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
        df['maturity_date'] = pd.to_datetime(df['maturity_date'], errors='coerce')

        # Convert the selected dates
        selected_date = pd.to_datetime(selected_date)
        prev_date = pd.to_datetime(prev_selected_date) if prev_selected_date else None

        # Drop rows with NaT in date columns
        df = df.dropna(subset=['trade_date', 'maturity_date'])

        # Ensure settlement_price is numeric
        df['settlement_price'] = pd.to_numeric(df['settlement_price'], errors='coerce')
        df = df.dropna(subset=['settlement_price'])

        if df.empty:
            return html.Div(html.H4("No valid data available after cleaning", style={'color': 'red'}))

        product_df = df[df['contract'].isin(selected_products)]

        if product_df.empty:
            return html.Div(html.H4("No data for selected products", style={'color': 'red'}))

        # Get current data
        current_data = product_df[product_df['trade_date'].dt.normalize() == selected_date.normalize()]

        # Get previous data if available
        prev_data = pd.DataFrame()
        if prev_date is not None:
            prev_data = product_df[product_df['trade_date'].dt.normalize() == prev_date.normalize()]

        if current_data.empty:
            return html.Div(html.H4(f"No data found for selected date: {selected_date.strftime('%Y-%m-%d')}",
                                    style={'color': 'red'}))

        # Group data by period
        try:
            current_data = group_data_by_period(current_data, grouping_mode)
            current_data['product'] = current_data['contract']
        except Exception as e:
            print(f"Error grouping current data: {e}")
            return html.Div(html.H4(f"Error grouping data: {str(e)}", style={'color': 'red'}))

        # Create pivot table for current data
        try:
            current_pivot = current_data.pivot_table(
                values='settlement_price',
                index=['product'],
                columns=['period'],
                aggfunc='first'
            ).reset_index()
        except Exception as e:
            print(f"Error creating pivot table: {e}")
            return html.Div(html.H4(f"Error creating data table: {str(e)}", style={'color': 'red'}))

        # Get date columns
        date_cols = [col for col in current_pivot.columns if col != 'product']

        if not date_cols:
            return html.Div(html.H4("No period data available for selected parameters", style={'color': 'red'}))

        # Sort date columns based on grouping mode
        try:
            if grouping_mode == 'monthly':
                def month_year_to_date(month_year):
                    try:
                        month, year = month_year.split('-')
                        return pd.to_datetime(f"20{year}-{month}-01")
                    except:
                        return pd.to_datetime('2100-01-01')

                sorted_date_cols = sorted(date_cols, key=month_year_to_date)

            elif grouping_mode == 'quarterly':
                def quarter_key(q_str):
                    try:
                        year, quarter = q_str.split('-')
                        return (int(year), int(quarter[1]))
                    except:
                        return (9999, 0)

                sorted_date_cols = sorted(date_cols, key=quarter_key)

            elif grouping_mode == 'season':
                def season_key(s_str):
                    try:
                        year, season = s_str.split('-')
                        season_val = 0 if season == 'Winter' else 1
                        return (int(year), season_val)
                    except:
                        return (9999, 0)

                sorted_date_cols = sorted(date_cols, key=season_key)

            elif grouping_mode == 'calendar':
                sorted_date_cols = sorted(date_cols, key=lambda x: int(x) if x.isdigit() else 9999)
            else:
                # Default sorting
                sorted_date_cols = date_cols
        except Exception as e:
            print(f"Error sorting date columns: {e}")
            sorted_date_cols = date_cols  # Fall back to unsorted columns

        # Reorder columns
        try:
            current_pivot = current_pivot[['product'] + sorted_date_cols]
        except Exception as e:
            print(f"Error reordering columns: {e}")
            # Continue with original order if error

        # Initialize changes pivot
        changes_pivot = pd.DataFrame()

        # Calculate changes if previous data exists
        if not prev_data.empty and not current_data.empty and not current_pivot.empty:
            try:
                prev_data = group_data_by_period(prev_data, grouping_mode)
                prev_data['product'] = prev_data['contract']

                prev_pivot = prev_data.pivot_table(
                    values='settlement_price',
                    index=['product'],
                    columns=['period'],
                    aggfunc='first'
                ).reset_index()

                # Get all products from both current and previous data
                all_products = sorted(set(current_pivot['product'].tolist() +
                                          ([] if prev_pivot.empty else prev_pivot['product'].tolist())))

                changes_pivot = pd.DataFrame({'product': all_products})

                # Calculate changes for each period
                for col in sorted_date_cols:
                    if col in prev_pivot.columns:
                        changes_pivot[col] = None

                        for product in all_products:
                            current_row = current_pivot[current_pivot['product'] == product]
                            current_val = current_row[col].iloc[
                                0] if not current_row.empty and col in current_row.columns and len(
                                current_row) > 0 else None

                            prev_row = prev_pivot[prev_pivot['product'] == product]
                            prev_val = prev_row[col].iloc[0] if not prev_row.empty and col in prev_row.columns and len(
                                prev_row) > 0 else None

                            if current_val is not None and prev_val is not None:
                                idx = changes_pivot[changes_pivot['product'] == product].index[0]
                                changes_pivot.at[idx, col] = current_val - prev_val
            except Exception as e:
                print(f"Error calculating changes: {e}")
                # Continue without changes table if there's an error
                changes_pivot = pd.DataFrame()

        # Prepare tables for display
        current_display = current_pivot
        columns = [{"name": "Product", "id": "product"}]
        for col in sorted_date_cols:
            columns.append({"name": col, "id": col, "type": "numeric", "format": {"specifier": ".2f"}})

        current_table_data = current_display.replace({float('nan'): None}).to_dict('records')

        current_table = html.Div([
            dash_table.DataTable(
                id='prices-volatility-table',
                columns=columns,
                data=current_table_data,
                style_table={
                    'overflowX': 'auto'
                },
                style_cell={
                    'textAlign': 'center',
                    'padding': '4px',
                    'fontSize': 12,
                    'minWidth': '40px',
                    'maxWidth': '100px',
                    'color': 'black'
                },
                style_cell_conditional=[
                    {'if': {'column_id': 'product'},
                     'textAlign': 'left',
                     'fontWeight': 'bold',
                     'width': '80px',
                     'color': 'black'
                     }
                ],
                style_header={
                    'backgroundColor': 'rgb(230, 230, 230)',
                    'fontWeight': 'bold',
                    'textAlign': 'center',
                    'padding': '4px',
                    'fontSize': 12,
                    'whiteSpace': 'normal',
                    'height': 'auto',
                    'color': 'black'
                },
                style_data_conditional=[
                    {
                        'if': {'row_index': 'odd'},
                        'backgroundColor': 'rgb(248, 248, 248)'
                    }
                ],
                fixed_rows={'headers': True},
                page_action='none',
                tooltip_delay=0,
                tooltip_duration=None
            )
        ], style={'marginBottom': '15px', 'width': '100%'})

        changes_table = None
        if not changes_pivot.empty:
            changes_table_data = changes_pivot.replace({float('nan'): None}).to_dict('records')

            conditional_styling = [
                {
                    'if': {'row_index': 'odd'},
                    'backgroundColor': 'rgb(248, 248, 248)'
                }
            ]

            for col in sorted_date_cols:
                if col in changes_pivot.columns:
                    conditional_styling.extend([
                        {
                            'if': {
                                'column_id': col,
                                'filter_query': f'{{{col}}} > 0'
                            },
                            'color': 'green',
                            'fontWeight': 'bold'
                        },
                        {
                            'if': {
                                'column_id': col,
                                'filter_query': f'{{{col}}} < 0'
                            },
                            'color': 'red',
                            'fontWeight': 'bold'
                        }
                    ])

            changes_table = html.Div([
                dash_table.DataTable(
                    id='prices-changes-table',
                    columns=columns,
                    data=changes_table_data,
                    style_table={
                        'overflowX': 'auto'
                    },
                    style_cell={
                        'textAlign': 'center',
                        'padding': '4px',
                        'fontSize': 12,
                        'minWidth': '40px',
                        'maxWidth': '100px',
                        'color': 'black'
                    },
                    style_cell_conditional=[
                        {'if': {'column_id': 'product'},
                         'textAlign': 'left',
                         'fontWeight': 'bold',
                         'width': '80px',
                         'color': 'black'
                         }
                    ],
                    style_header={
                        'backgroundColor': 'rgb(230, 230, 230)',
                        'fontWeight': 'bold',
                        'textAlign': 'center',
                        'padding': '4px',
                        'fontSize': 12,
                        'whiteSpace': 'normal',
                        'height': 'auto',
                        'color': 'black'
                    },
                    style_data_conditional=conditional_styling,
                    fixed_rows={'headers': True},
                    page_action='none',
                    tooltip_delay=0,
                    tooltip_duration=None
                )
            ], style={'width': '100%'})
        else:
            changes_table = html.Div()

        # Empty div instead of a title
        grouping_title = html.Div()
        return html.Div([grouping_title, current_table, changes_table])

    except Exception as e:
        print(f"Error in update_tables: {e}")
        return html.Div(html.H4(f"Error generating tables: {str(e)}", style={'color': 'red'}))


# Export prices table callback
@dash.callback(
    Output("download-prices-table", "data"),
    Input("export-prices-table-btn", "n_clicks"),
    [State('prices-unified-date-picker', 'date'),
     State('prices-unified-product-dropdown', 'value'),
     State('prices-unified-grouping-dropdown', 'value')],
    prevent_initial_call=True
)
def export_prices_table(n_clicks, selected_date, selected_products, grouping_mode):
    if n_clicks is None or selected_date is None or not selected_products:
        raise dash.exceptions.PreventUpdate
    
    try:
        # Check if df_options exists and is accessible
        if 'df_options' not in globals() or df_options is None or not isinstance(df_options, pd.DataFrame):
            raise dash.exceptions.PreventUpdate
        
        # Make a copy to avoid modifying the original
        df = df_options.copy()
        
        # Convert date columns safely
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
        df['maturity_date'] = pd.to_datetime(df['maturity_date'], errors='coerce')
        
        # Convert the selected date
        selected_date = pd.to_datetime(selected_date)
        
        # Drop rows with NaT in date columns
        df = df.dropna(subset=['trade_date', 'maturity_date'])
        
        # Ensure settlement_price is numeric
        df['settlement_price'] = pd.to_numeric(df['settlement_price'], errors='coerce')
        df = df.dropna(subset=['settlement_price'])
        
        if df.empty:
            raise dash.exceptions.PreventUpdate
        
        product_df = df[df['contract'].isin(selected_products)]
        
        if product_df.empty:
            raise dash.exceptions.PreventUpdate
        
        # Get current data
        current_data = product_df[product_df['trade_date'].dt.normalize() == selected_date.normalize()]
        
        if current_data.empty:
            raise dash.exceptions.PreventUpdate
        
        # Group data by period
        current_data = group_data_by_period(current_data, grouping_mode)
        current_data['product'] = current_data['contract']
        
        # Create pivot table for current data
        current_pivot = current_data.pivot_table(
            values='settlement_price',
            index=['product'],
            columns=['period'],
            aggfunc='first'
        ).reset_index()
        
        # Format the data for export
        export_df = current_pivot.copy()
        
        # Round numeric columns to 4 decimal places
        for col in export_df.columns:
            if col != 'product' and export_df[col].dtype in ['float64', 'float32']:
                export_df[col] = export_df[col].round(4)
        
        return dcc.send_data_frame(
            export_df.to_csv, 
            f"prices_data_{selected_date.strftime('%Y%m%d')}_{grouping_mode}.csv", 
            index=False
        )
        
    except Exception as e:
        print(f"Error in export_prices_table: {e}")
        raise dash.exceptions.PreventUpdate


# # Reset grouping dropdown
# @dash.callback(
#     Output('prices-table-grouping-dropdown', 'value'),
#     Input('prices-table-refresh-button', 'n_clicks'),
#     prevent_initial_call=True
# )
# def reset_grouping(n_clicks):
#     return 'monthly'
#
# # New callback to reset the graph grouping dropdown
# @dash.callback(
#     Output('prices-graph-grouping-dropdown', 'value'),
#     Input('prices-graph-refresh-button', 'n_clicks'),
#     prevent_initial_call=True
# )
# def reset_graph_grouping(n_clicks):
#     return 'daily'

@dash.callback(
    Output('prices-unified-grouping-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=True
)
def reset_unified_grouping(n_clicks):
    # This function will be triggered by the main refresh button
    return 'monthly'

 