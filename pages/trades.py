import dash
from dash import Dash, html, dcc, dash_table, callback, Input, Output, State
import pandas as pd
import numpy as np
import configparser
from sqlalchemy import create_engine
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
DB_CONNECTION_STRING = config_reader.get('DATABASE', 'CONNECTION_STRING', fallback=None)
DB_SCHEMA = config_reader.get('DATABASE', 'SCHEMA', fallback='at_lng')

# ---------------------- Configuration ----------------------
#Default DBAPI (should use psycopg2 as default, pg8000 it's an alternative but we don't have the library installed)
# create engine
engine = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True)


# ------------------------------------------------------------------
# HELPER FUNCTIONS FOR DATA ACCESS AND FORMATTING
# ------------------------------------------------------------------
def get_available_dates(engine):
    """
    Fetch distinct COB dates from 'at_lng.trades_options_valuation'
    in descending order.
    """
    try:
        query = """
        SELECT DISTINCT cob_date
        FROM at_lng.trades_options_valuation
        ORDER BY cob_date DESC
        """
        dates_df = pd.read_sql(query, engine)
        # Convert each date to string, e.g. '2023-01-01'
        return [d.strftime('%Y-%m-%d') for d in dates_df['cob_date']]
    except Exception as e:
        print(f"Failed to fetch available dates: {e}")
        return []


def get_strategies(engine, selected_date):
    """
    Return a sorted list of distinct substrategies for the given COB date.
    """
    try:
        query = f"""
        SELECT DISTINCT substrategy
        FROM at_lng.trades_options_valuation
        WHERE cob_date = '{selected_date}'
        ORDER BY substrategy
        """
        df_strats = pd.read_sql(query, engine)
        return sorted(df_strats['substrategy'].dropna().unique().tolist())
    except Exception as e:
        print(f"Failed to fetch strategies for date={selected_date}: {e}")
        return []


def fetch_trades_data(engine, selected_date, selected_strategies):
    """
    Query 'at_lng.trades_options_valuation' for the given COB date
    and filter to only the specified substrategies.
    Group by (substrategy, type_option, strike, expiration_date).
    Aggregations:
    - Premium: weighted average by quantity
    - Quantity: sum
    - Intrinsic_value: weighted average by quantity
    - Time_value: weighted average by quantity
    - qty_intrinsic_value: sum
    - qty_time_value: sum
    - qty_pnl: sum
    """
    try:
        # Base query for the date (main data)
        query = f"""
        SELECT
            substrategy,
            type_option,
            put_call,
            asset_a,
            asset_b,
            asset_a_multiplier,
            asset_a_premium,
            asset_b_multiplier,
            asset_b_premium,
            price_a,
            price_b,
            adjusted_vol_a,
            adjusted_vol_b,
            premium,
            quantity,
            unit_quantity,
            strike,
            expiration_date,
            intrinsic_value,
            time_value,
            qty_intrinsic_value,
            qty_time_value,
            qty_pnl
        FROM at_lng.trades_options_valuation
        WHERE cob_date = '{selected_date}'
        """
        df = pd.read_sql(query, engine)
        if df.empty:
            return pd.DataFrame()

        # Filter to only the selected substrategies
        if selected_strategies:
            df = df[df['substrategy'].isin(selected_strategies)]
        if df.empty:
            return pd.DataFrame()

        # Convert expiration_date to datetime for proper grouping
        df['expiration_date'] = pd.to_datetime(df['expiration_date'], errors='coerce')

        # Group + aggregate
        grouped = (
            df.groupby(['substrategy', 'type_option', 'put_call', 'asset_a', 'asset_b', 'unit_quantity', 'strike', 'expiration_date'], dropna=False)
            .agg({
                'asset_a_multiplier': 'first',  # constant value
                'asset_a_premium': lambda x: np.average(x, weights=df.loc[x.index, 'quantity']) if x.notna().any() and df.loc[x.index, 'quantity'].sum() > 0 else 0,  # weighted average by quantity
                'asset_b_multiplier': 'first',  # constant value
                'asset_b_premium': lambda x: np.average(x, weights=df.loc[x.index, 'quantity']) if x.notna().any() and df.loc[x.index, 'quantity'].sum() > 0 else 0,  # weighted average by quantity
                'price_a': 'first',  # market price (constant)
                'price_b': 'first',  # market price (constant)
                'adjusted_vol_a': 'first',  # volatility (constant)
                'adjusted_vol_b': 'first',  # volatility (constant)
                'premium': lambda x: np.average(x, weights=df.loc[x.index, 'quantity']) if x.notna().any() and df.loc[x.index, 'quantity'].sum() > 0 else 0,  # weighted average by quantity
                'quantity': 'sum',  # sum
                'intrinsic_value': lambda x: np.average(x, weights=df.loc[x.index, 'quantity']) if x.notna().any() and df.loc[x.index, 'quantity'].sum() > 0 else 0,  # weighted average by quantity
                'time_value': lambda x: np.average(x, weights=df.loc[x.index, 'quantity']) if x.notna().any() and df.loc[x.index, 'quantity'].sum() > 0 else 0,  # weighted average by quantity
                'qty_intrinsic_value': 'sum',  # sum
                'qty_time_value': 'sum',  # sum
                'qty_pnl': 'sum'  # sum
            })
            .reset_index()
        )

        # Sort by substrategy, type_option, put_call, asset_a, asset_b, strike, expiration_date for logical display order
        grouped = grouped.sort_values(['substrategy', 'type_option', 'put_call', 'asset_a', 'asset_b', 'strike', 'expiration_date'])
        
        return grouped
    except Exception as e:
        print(f"Failed to fetch or process trades data for {selected_date}: {e}")
        return pd.DataFrame()


# Dictionary to map DB column names to user-friendly display names
COLUMN_NAME_MAPPING = {
    'substrategy': 'Strategy',
    'type_option': 'Option Type',
    'put_call': 'Call/Put',
    'asset_a': 'Asset A',
    'asset_b': 'Asset B',
    'asset_a_multiplier': 'Asset A Multiplier',
    'asset_a_premium': 'Asset A Premium',
    'asset_b_multiplier': 'Asset B Multiplier',
    'asset_b_premium': 'Asset B Premium',
    'price_a': 'Price A',
    'price_b': 'Price B',
    'adjusted_vol_a': 'Vol A',
    'adjusted_vol_b': 'Vol B',
    'premium': 'Premium',
    'quantity': 'Quantity',
    'unit_quantity': 'Unit',
    'strike': 'Strike',
    'expiration_date': 'Expiration Date',
    'intrinsic_value': 'Intrinsic Value',
    'time_value': 'Time Value',
    'qty_intrinsic_value': 'Total Intrinsic',
    'qty_time_value': 'Total Time Value',
    'qty_pnl': 'Total P&L'
}


def build_columns_with_numeric_format(df):
    """
    Dynamically create a columns definition for dash_table.
    For each numeric column, apply thousands separators and keep numeric type.
    For non-numeric columns, just treat them as text.
    Use friendly column names from COLUMN_NAME_MAPPING.
    For columns with "Total" in the name, remove decimal places.
    """
    from dash.dash_table.Format import Format, Group, Scheme
    table_columns = []

    for col in df.columns:
        # Use friendly column name if available, otherwise use the original
        friendly_name = COLUMN_NAME_MAPPING.get(col, col)

        if pd.api.types.is_numeric_dtype(df[col]):
            # Determine precision based on column name
            # If the friendly name starts with "Total" or is "Quantity", use 0 decimal places
            precision = 0 if friendly_name.startswith('Total') or friendly_name == 'Quantity' or friendly_name == 'Strike' else 2

            # Numeric column with thousand separators
            col_def = {
                "name": friendly_name,
                "id": col,
                "type": "numeric",
                "format": Format(
                    group=Group.yes,  # enable thousand separators
                    groups=3,
                    precision=precision,
                    scheme=Scheme.fixed
                )
            }
        elif col == 'expiration_date':
            # Date column with special formatting
            col_def = {
                "name": friendly_name,
                "id": col,
                "type": "datetime"
            }
        else:
            # Non-numeric column
            col_def = {
                "name": friendly_name,
                "id": col
            }
        table_columns.append(col_def)
    return table_columns


# ------------------------------------------------------------------
# 3) BUILD THE DASH LAYOUT
# ------------------------------------------------------------------
# 3.1) Get list of available COB dates for the date dropdown
available_dates = get_available_dates(engine)

# Define global styles
styles = {
    'page': {
        'backgroundColor': '#f5f7fa',
        'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif',
        'padding': '20px',
    },
    'header': {
        'backgroundColor': '#1e3a8a',
        'color': '#ffffff',
        'padding': '20px',
        'borderRadius': '8px',
        'marginBottom': '20px',
        'boxShadow': '0 2px 4px rgba(0,0,0,0.1)'
    },
    'panel': {
        'backgroundColor': '#ffffff',
        'padding': '20px',
        'borderRadius': '8px',
        'boxShadow': '0 1px 3px rgba(0,0,0,0.1)',
        'marginBottom': '20px'
    },
    'sectionTitle': {
        'color': '#1e3a8a',
        'fontWeight': 'bold',
        'marginBottom': '15px',
        'textAlign': 'left'
    },
    'label': {
        'color': '#1e3a8a',
        'fontWeight': 'bold',
        'marginBottom': '5px',
        'display': 'block'
    },
    'button': {
        'backgroundColor': '#3b82f6',
        'color': '#ffffff',
        'border': 'none',
        'borderRadius': '4px',
        'padding': '8px 16px',
        'cursor': 'pointer',
        'fontWeight': 'bold',
        'marginLeft': '10px',
        'verticalAlign': 'middle'
    },
    'refreshButton': {
        'backgroundColor': '#10b981',
        'color': '#ffffff',
        'border': 'none',
        'borderRadius': '4px',
        'padding': '8px 16px',
        'cursor': 'pointer',
        'fontWeight': 'bold',
        'marginLeft': '10px',
        'verticalAlign': 'middle'
    },
    'filterRow': {
        'marginBottom': '20px'
    },
    'dataTable': {
        'overflowX': 'auto'
    },
    'tableHeader': {
        'backgroundColor': '#e5e7eb',
        'fontWeight': 'bold',
        'textAlign': 'center',
        'padding': '10px',
        'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif'
    },
    'tableCell': {
        'textAlign': 'center',
        'padding': '10px',
        'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif'
    }
}

# The enhanced layout preserving original functionality
layout = html.Div([
        # Download component for table export
        dcc.Download(id="download-trades-table"),
        
        # Main content panel
        html.Div(
            style=styles['panel'],
            children=[
                # Filters section with title
                html.Div(
                    className="inline-section-header",
                    style={'marginBottom': '20px', 'paddingBottom': '15px'},
                    children=[
                        # ~~~~~ ROW 1: Date dropdown and View selector ~~~~~
                        html.Div([
                            html.Label('Trade Date:', className="inline-filter-label"),
                            dcc.Dropdown(
                                id='trades-date-dropdown',
                                options=[{'label': d, 'value': d} for d in available_dates],
                                value=available_dates[0] if available_dates else None,
                                clearable=False,
                                className="inline-dropdown-date"
                            ),
                            html.Label('View:', className="inline-filter-label"),
                            dcc.RadioItems(
                                id='trades-view-radio',
                                options=[
                                    {'label': 'Summary', 'value': 'summary'},
                                    {'label': 'Detail', 'value': 'detail'}
                                ],
                                value='summary',
                                inline=True,
                                style={'display': 'flex', 'gap': '10px', 'align-items': 'center'}
                            )
                        ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap', 'margin-bottom': '8px'}),
                        
                        # ~~~~~ ROW 2: Strategy dropdown ~~~~~
                        html.Div([
                            html.Label('Strategies:', className="inline-filter-label"),
                            dcc.Dropdown(
                                id='trades-strategy-dropdown',
                                options=[],  # will be set by callback
                                value=[],  # default to empty or all
                                multi=True,
                                placeholder="Select strategies...",
                                className="inline-dropdown-multi-strategies",
                                style={'width': '800px'}
                            )
                        ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap', 'width': '100%'}),
                    ]
                ),
                # Spinner + DataTable section
                dcc.Loading(
                    id="trades-loading",
                    type="circle",
                    children=[
                        html.Div([
                            html.H4(
                                "Trades Detail Table",
                                style={
                                    'color': '#1e3a8a',
                                    'fontWeight': 'bold',
                                    'margin': '0',
                                    'display': 'inline-block'
                                }
                            ),
                            html.Button(
                                'Export',
                                id='export-trades-table-btn',
                                className='custom-export-btn',
                                style={
                                    'margin-left': '12px',
                                    'font-size': '11px',
                                    'padding': '4px 8px',
                                    'background-color': '#2E86C1',
                                    'color': 'white',
                                    'border': 'none',
                                    'border-radius': '3px',
                                    'cursor': 'pointer',
                                    'font-weight': '500'
                                }
                            )
                        ], style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '15px'}),
                        html.Div(
                            id='trades-error-message',
                            style={
                                'textAlign': 'center',
                                'color': '#dc2626',
                                'backgroundColor': '#fef2f2',
                                'border': '1px solid #fecaca',
                                'borderRadius': '8px',
                                'padding': '15px',
                                'margin': '10px 0',
                                'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif',
                                'display': 'none'
                            }
                        ),
                        dash_table.DataTable(
                            id='trades-table',
                            style_table={'overflowX': 'auto'},
                            style_cell={
                                'textAlign': 'center',
                                'padding': '10px',
                                'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif'
                            },
                            style_header={
                                'backgroundColor': '#e5e7eb',
                                'fontWeight': 'bold',
                                'textAlign': 'center'
                            },
                            style_data_conditional=[
                                {
                                    'if': {'row_index': 'odd'},
                                    'backgroundColor': 'rgb(248, 248, 248)'
                                },
                                {
                                    'if': {
                                        'filter_query': '{intrinsic_value} > 0'
                                    },
                                    'border': '2px solid #10b981',
                                    'borderRadius': '4px'
                                }
                            ],
                            fill_width=False,
                            export_format='xlsx'
                        )
                    ]
                )
            ]
        )
    ]
)

# ------------------------------------------------------------------
# 4) CALLBACKS
# ------------------------------------------------------------------

# 4.1) Populate the strategy dropdown based on the selected date
@dash.callback(
    Output('trades-strategy-dropdown', 'options'),
    Output('trades-strategy-dropdown', 'value'),
    Input('trades-date-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks')
)
def update_strategy_options(selected_date, n_clicks):
    """
    Whenever the user picks a new date or clicks refresh, fetch the distinct substrategies
    for that date from the DB, then set them as the strategy dropdown options.
    Default the value to *all* strategies.
    """
    if not selected_date:
        return [], []

    # Get the list of available strategies
    strategies = get_strategies(engine, selected_date)

    # Build the dropdown options
    options = [{'label': s, 'value': s} for s in strategies]

    # Default the selected value to "all strategies" = everything
    return options, strategies


# 4.2) Update the DataTable when date or strategy selection changes
@dash.callback(
    Output('trades-table', 'data'),
    Output('trades-table', 'columns'),
    Output('trades-error-message', 'children'),
    Output('trades-error-message', 'style'),
    Input('trades-date-dropdown', 'value'),
    Input('trades-strategy-dropdown', 'value'),
    Input('trades-view-radio', 'value'),
    Input('refresh-options-data', 'n_clicks')
)
def update_trades_table(selected_date, selected_strategies, view_mode, n_clicks):
    """
    Queries and groups trades data by (substrategy, type_option, strike, expiration_date) 
    for the chosen date, then filters to the selected strategies, and returns the data to the table
    with thousands separators for numeric columns.
    """
    # Define error message style (hidden by default)
    error_style_hidden = {
        'textAlign': 'center',
        'color': '#dc2626',
        'backgroundColor': '#fef2f2',
        'border': '1px solid #fecaca',
        'borderRadius': '8px',
        'padding': '15px',
        'margin': '10px 0',
        'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif',
        'display': 'none'
    }
    
    # Define error message style (visible)
    error_style_visible = {
        'textAlign': 'center',
        'color': '#dc2626',
        'backgroundColor': '#fef2f2',
        'border': '1px solid #fecaca',
        'borderRadius': '8px',
        'padding': '15px',
        'margin': '10px 0',
        'fontFamily': 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif',
        'display': 'block'
    }
    
    if not selected_date:
        return [], [], "", error_style_hidden

    df = fetch_trades_data(engine, selected_date, selected_strategies)
    if df.empty:
        error_message = "⚠️ Unable to retrieve trades data for the selected date. This may be due to database connectivity issues or missing data for this date."
        return [], [], error_message, error_style_visible

    # Format expiration_date to show only date part (remove time)
    if 'expiration_date' in df.columns:
        df['expiration_date'] = pd.to_datetime(df['expiration_date']).dt.strftime('%Y-%m-%d')

    # Filter columns based on view mode
    if view_mode == 'summary':
        summary_columns = [
            'substrategy', 'type_option', 'unit_quantity', 'strike', 'expiration_date',
            'premium', 'quantity', 'intrinsic_value', 'time_value', 
            'qty_intrinsic_value', 'qty_time_value', 'qty_pnl'
        ]
        # Keep only summary columns that exist in the dataframe
        available_columns = [col for col in summary_columns if col in df.columns]
        df = df[available_columns]

    # Convert DataFrame to dash_table-friendly format
    data_records = df.to_dict('records')

    # Build columns dynamically, formatting numeric columns with thousand separators
    # and using friendly column names
    columns = build_columns_with_numeric_format(df)

    return data_records, columns, "", error_style_hidden


# 4.3) Export callback for trades table
@dash.callback(
    Output("download-trades-table", "data"),
    Input("export-trades-table-btn", "n_clicks"),
    [State('trades-date-dropdown', 'value'),
     State('trades-strategy-dropdown', 'value'),
     State('trades-view-radio', 'value')],
    prevent_initial_call=True
)
def export_trades_table(n_clicks, selected_date, selected_strategies, view_mode):
    """Export Trades Detail Table to Excel"""
    if n_clicks is None or selected_date is None:
        raise dash.exceptions.PreventUpdate
    
    try:
        # Get the same data as displayed in the table
        df = fetch_trades_data(engine, selected_date, selected_strategies)
        if df.empty:
            raise dash.exceptions.PreventUpdate

        # Format expiration_date to show only date part (remove time)
        if 'expiration_date' in df.columns:
            df['expiration_date'] = pd.to_datetime(df['expiration_date']).dt.strftime('%Y-%m-%d')

        # Filter columns based on view mode (same logic as the table)
        if view_mode == 'summary':
            summary_columns = [
                'substrategy', 'type_option', 'unit_quantity', 'strike', 'expiration_date',
                'premium', 'quantity', 'intrinsic_value', 'time_value', 
                'qty_intrinsic_value', 'qty_time_value', 'qty_pnl'
            ]
            # Keep only summary columns that exist in the dataframe
            available_columns = [col for col in summary_columns if col in df.columns]
            df = df[available_columns]

        # Rename columns to user-friendly names
        df_renamed = df.rename(columns=COLUMN_NAME_MAPPING)
        
        # Generate filename with timestamp
        timestamp = pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')
        view_suffix = f"_{view_mode}" if view_mode else ""
        filename = f"trades_detail_table_{selected_date}{view_suffix}_{timestamp}.xlsx"
        
        return dcc.send_data_frame(df_renamed.to_excel, filename, sheet_name="Trades Detail", index=False)
        
    except Exception as e:
        print(f"Export error: {e}")
        raise dash.exceptions.PreventUpdate