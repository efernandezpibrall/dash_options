# pages/price_shocks.py
from dash import html, dcc, dash_table, callback, Output, Input, State, Dash, html, dcc, ALL
import plotly.graph_objects as go
import dash_bootstrap_components as dbc
import plotly.express as px
import dash
import os
import configparser

import plotly.express as px
import pandas as pd

from fundamentals_macro.kpler_fundamentals import *
from fundamentals_macro.sendouts_terminals_analysis import *
from fundamentals_macro.energy_aspect import *

import dash
from dash import Dash, html, dcc, dash_table, Input, Output, callback_context
import pandas as pd
import psycopg2
from sqlalchemy import create_engine

#------ code to be able to access config.ini, even having the path in the .virtualenvs is not working without it ------#
try:
    # Get the directory where your script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate to the directory containing config.ini
    # Adjust the number of '..' as needed to reach the correct directory
    config_dir = os.path.abspath(os.path.join(script_dir, '..','..'))  # Go up two levels
    CONFIG_FILE_PATH = os.path.join(config_dir, 'config.ini')
except:
    CONFIG_FILE_PATH = 'config.ini'  # Assumes it's in the same directory or the path it is detected

# --- Load Configuration from INI File ---
config_reader = configparser.ConfigParser(interpolation=None)
config_reader.read(CONFIG_FILE_PATH)

# Read values from the ini file sections
DB_CONNECTION_STRING = config_reader.get('DATABASE', 'CONNECTION_STRING', fallback=None)
DB_SCHEMA = config_reader.get('DATABASE', 'SCHEMA', fallback='at_lng')

# --- Essential Variable Checks ---
if not DB_CONNECTION_STRING:
    raise ValueError(f"Missing DATABASE CONNECTION_STRING in {CONFIG_FILE_PATH}")

# create engine
engine = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True)

# ------------------------------------------------------------------
# 1) Create the Dash app
# ------------------------------------------------------------------

# Define available dropdown options
DROPDOWN_OPTIONS = [
    'option_value_year', 'delta_S1', 'delta_S2', 'gamma_S1', 'gamma_S2',
    'gamma_S1S2', 'vega_sigma1', 'vega_sigma2', 'corr_sensitivity', 'theta', 'vega_equiv'
]

# ------------------------------------------------------------------
# 2) Define helper function to convert DataFrame -> Dash Table props
# ------------------------------------------------------------------
def pivot_to_columns_and_data(df: pd.DataFrame):
    """
    Resets the index of a pivoted DataFrame, then returns
    the columns (list of dict) and data (list of dict).
    """
    df_reset = df.reset_index()
    columns = [{"name": str(col), "id": str(col)} for col in df_reset.columns]
    data = df_reset.to_dict("records")
    return columns, data

# ------------------------------------------------------------------
# 3) Define initial layout with:
#    - A Dropdown for metric selection
#    - A Refresh Data button
#    - A DataTable for each pivot
# ------------------------------------------------------------------
layout = html.Div([
    html.H1("Option Value Tables"),

    dcc.Dropdown(
        id='metric-dropdown',
        options=[{'label': metric, 'value': metric} for metric in DROPDOWN_OPTIONS],
        value='option_value_year',  # Default selection
        clearable=False
    ),

    html.Button("Refresh Data", id="refresh-button", n_clicks=0),

    html.H3("All Contract Years"),
    dash_table.DataTable(id='table-all-years', columns=[], data=[]),


    html.H3("Contract Year 2026 (Brent vs. JKM)"),
    dash_table.DataTable(id='table-2026', columns=[], data=[]),

    html.H3("Contract Year 2027 (Brent vs. JKM)"),
    dash_table.DataTable(id='table-2027', columns=[], data=[]),

    html.H3("Contract Year 2028 (Brent vs. JKM)"),
    dash_table.DataTable(id='table-2028', columns=[], data=[]),

    html.H3("Contract Year 2029 (Brent vs. JKM)"),
    dash_table.DataTable(id='table-2029', columns=[], data=[]),

    html.H3("Contract Year 2030 (Brent vs. JKM)"),
    dash_table.DataTable(id='table-2030', columns=[], data=[]),
])

# ------------------------------------------------------------------
# 4) Define the callback to refresh data and update tables
# ------------------------------------------------------------------
@callback(
    [
        Output('table-all-years', 'columns'),
        Output('table-all-years', 'data'),


        Output('table-2026', 'columns'),
        Output('table-2026', 'data'),

        Output('table-2027', 'columns'),
        Output('table-2027', 'data'),

        Output('table-2028', 'columns'),
        Output('table-2028', 'data'),

        Output('table-2029', 'columns'),
        Output('table-2029', 'data'),

        Output('table-2030', 'columns'),
        Output('table-2030', 'data'),
    ],
    [Input('refresh-button', 'n_clicks'), Input('metric-dropdown', 'value')],
)
def refresh_data(n_clicks, selected_metric):
    """
    When the 'Refresh Data' button is clicked or a metric is selected,
    pull the latest data from the DB, create each pivot table, and return new columns/data.
    """
    if n_clicks == 0:
        return ([], [],
                [], [],
                [], [],
                [], [],
                [], [],
                [], [])

    # Example DB read (adjust to your DB credentials):
    # with psycopg2.connect(
    #         dbname="mydb", user="myuser", password="secret", host="localhost"
    # ) as conn:
    #     df_price_shocks = pd.read_sql(
    #         '''
    #         SELECT *
    #         FROM at_lng.options_price_shocks
    #         WHERE "trade_date" = (
    #             SELECT MAX(trade_date)
    #             FROM at_lng.options_price_shocks
    #         )
    #         ''',
    #         con=conn
    #     )

    # Simulated DataFrame for demonstration:
    df_price_shocks=pd.read_sql('''select * FROM at_lng.options_price_shocks
                        WHERE "trade_date" = (select max(trade_date) from at_lng.options_price_shocks)
                        ''',engine)

    # Create each pivot table
    # ------------------------------------------------
    # "All years" (price_change_a vs price_change_b)
    option_price_change_all = df_price_shocks.pivot_table(
        values=selected_metric,  # Use the selected metric
        index='price_change_a',
        columns='price_change_b'
    ).round(2)

    # For each specific year
    def pivot_for_year(df, year):
        sub_df = df[df.contract_year == year].round(2)
        return sub_df.pivot_table(
            values=selected_metric,  # Use the selected metric
            index='ICE_BRENT_FUTURES',
            columns='ICE_JKM'
        )


    pvt_2026 = pivot_for_year(df_price_shocks, 2026)
    pvt_2027 = pivot_for_year(df_price_shocks, 2027)
    pvt_2028 = pivot_for_year(df_price_shocks, 2028)
    pvt_2029 = pivot_for_year(df_price_shocks, 2029)
    pvt_2030 = pivot_for_year(df_price_shocks, 2030)

    # Convert each pivoted DataFrame to dash_table columns & data
    # ------------------------------------------------
    cols_all, data_all = pivot_to_columns_and_data(option_price_change_all)

    cols_2026, data_2026 = pivot_to_columns_and_data(pvt_2026)
    cols_2027, data_2027 = pivot_to_columns_and_data(pvt_2027)
    cols_2028, data_2028 = pivot_to_columns_and_data(pvt_2028)
    cols_2029, data_2029 = pivot_to_columns_and_data(pvt_2029)
    cols_2030, data_2030 = pivot_to_columns_and_data(pvt_2030)

    return (
        cols_all, data_all,
        cols_2026, data_2026,
        cols_2027, data_2027,
        cols_2028, data_2028,
        cols_2029, data_2029,
        cols_2030, data_2030,
    )