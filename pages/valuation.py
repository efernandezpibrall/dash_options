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
engine_mo = create_engine("postgresql://at_bigdata_middle-officer:aGt%40midOfficer_pg@api.lakehouse.adnoc.ae:30080/postgres")



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


def fetch_pnl_data(engine, selected_date, selected_strategies):
    """
    Query 'at_lng.trades_options_valuation' for the given COB date
    and filter to only the specified substrategies.
    Group by (substrategy, year from maturity_date_a).
    Aggregations:
    - Averages for: price, intrinsic_value, time_value
    - Sums for: qty_value, qty_intrinsic_value, qty_time_value
    """
    try:
        # Try to get PnL data from Trino, but don't fail if it's unavailable
        try:
            query = f"""
                    SELECT
                        substrategy, ytd as ytd_hedging, 'All' as year
                    FROM {DB_SCHEMA}.pnl_aspect
                    WHERE "COB" = '{selected_date}'
                    """
            df_aspect_pnl = pd.read_sql(query, engine_mo)
        except Exception as e:
            print(f"Warning: Could not fetch PnL aspect data from Trino: {e}")
            df_aspect_pnl = pd.DataFrame()

        # Base query for the date (main data)
        query = f"""
        SELECT
            unit_quantity,
            substrategy,
            maturity_date_a,
            price,intrinsic_value,time_value,
            qty_value,qty_intrinsic_value,qty_time_value,qty_premium,qty_pnl,
            quantity
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

        # Convert date to datetime, extract year from maturity_date_a
        df['maturity_date_a'] = pd.to_datetime(df['maturity_date_a'], errors='coerce')
        df['year'] = df['maturity_date_a'].dt.year

        # Group + aggregate
        grouped = (
            df.groupby(['substrategy', 'year', 'unit_quantity'], dropna=False)
            .agg({
                'price': lambda x: np.average(x, weights=df.loc[x.index, 'quantity']),  # weighted average by quantity
                'intrinsic_value': lambda x: np.average(x, weights=df.loc[x.index, 'quantity']),
                # weighted average by quantity
                'time_value': lambda x: np.average(x, weights=df.loc[x.index, 'quantity']),
                # weighted average by quantity
                'qty_value': 'sum',  # total
                'qty_intrinsic_value': 'sum',
                'qty_time_value': 'sum',
                'qty_premium': 'sum',
                'qty_pnl': 'sum'
            })
            .reset_index()
        )

        # 1. Add subtotals by substrategy (across all years)
        substrategy_totals = grouped.groupby(['substrategy', 'unit_quantity']).agg({
            'price': lambda x: np.average(x, weights=grouped.loc[x.index, 'qty_value']),
            'intrinsic_value': lambda x: np.average(x, weights=grouped.loc[x.index, 'qty_value']),
            'time_value': lambda x: np.average(x, weights=grouped.loc[x.index, 'qty_value']),
            'qty_value': 'sum',
            'qty_intrinsic_value': 'sum',
            'qty_time_value': 'sum',
            'qty_premium': 'sum',
            'qty_pnl': 'sum'
        }).reset_index()

        # Set other fields for the substrategy subtotal rows
        substrategy_totals['year'] = 'All'

        # 2. Add totals by Unit across all strategies
        unit_totals = grouped.groupby('unit_quantity').agg({
            'price': lambda x: np.average(x, weights=grouped.loc[x.index, 'qty_value']),
            'intrinsic_value': lambda x: np.average(x, weights=grouped.loc[x.index, 'qty_value']),
            'time_value': lambda x: np.average(x, weights=grouped.loc[x.index, 'qty_value']),
            'qty_value': 'sum',
            'qty_intrinsic_value': 'sum',
            'qty_time_value': 'sum',
            'qty_premium': 'sum',
            'qty_pnl': 'sum'
        }).reset_index()

        # Set other fields for the unit total rows
        unit_totals['substrategy'] = 'All'
        unit_totals['year'] = None

        # 3. Add a grand total row
        grand_total = {
            'substrategy': 'All',
            'year': None,
            'unit_quantity': 'All',
            'price': np.average(grouped['price'], weights=grouped['qty_value']),
            'intrinsic_value': np.average(grouped['intrinsic_value'], weights=grouped['qty_value']),
            'time_value': np.average(grouped['time_value'], weights=grouped['qty_value']),
            'qty_value': grouped['qty_value'].sum(),
            'qty_intrinsic_value': grouped['qty_intrinsic_value'].sum(),
            'qty_time_value': grouped['qty_time_value'].sum(),
            'qty_premium': grouped['qty_premium'].sum(),
            'qty_pnl': grouped['qty_pnl'].sum()
        }

        # Concatenate all dataframes together
        grouped = pd.concat([grouped, substrategy_totals, unit_totals, pd.DataFrame([grand_total])], ignore_index=True)

        # Only merge PnL data if it's available
        if not df_aspect_pnl.empty:
            grouped = pd.merge(grouped, df_aspect_pnl, how='left', on=['substrategy','year'])
        else:
            # Add empty ytd_hedging column if PnL data is not available
            grouped['ytd_hedging'] = None

        # Sort to ensure a logical display order
        # This puts subtotals right after their respective groups
        def custom_sort(df):
            # Create a sort key for proper ordering
            df['_sort_year'] = df['year'].apply(lambda x:
                                                9999 if x == 'All' else
                                                (9998 if x is None else x))
            df['_sort_substrategy'] = df['substrategy'].apply(lambda x:
                                                              'zzz2' if x == 'All' else
                                                              ('zzz3' if x == 'All' else x))

            # Sort by unit_quantity, substrategy, then year
            df_sorted = df.sort_values(['unit_quantity', '_sort_substrategy', '_sort_year'])

            # Drop the temporary sorting columns
            df_sorted = df_sorted.drop(columns=['_sort_year', '_sort_substrategy'])
            return df_sorted

        grouped = custom_sort(grouped)
        return grouped
    except Exception as e:
        print(f"Failed to fetch or process PnL data for {selected_date}: {e}")
        return pd.DataFrame()


# Dictionary to map DB column names to user-friendly display names
COLUMN_NAME_MAPPING = {
    'substrategy': 'Strategy',
    'year': 'Year',
    'unit_quantity': 'Unit',
    'price': 'Price',
    'intrinsic_value': 'Intrinsic Value',
    'time_value': 'Time Value',
    'qty_value': 'Total Value',
    'qty_intrinsic_value': 'Total Intrinsic',
    'qty_time_value': 'Total Time Value',
    'qty_premium': 'Total Premium',
    'qty_pnl': 'Total P&L',
    'ytd_hedging': 'Aspect YTD P&L'
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
            # If the friendly name starts with "Total", use 0 decimal places
            precision = 0 if friendly_name.startswith('Total') or friendly_name.startswith('Aspect') else 2

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
        # Store for Aspect data - preserved exactly as original
        dcc.Store(id='aspect-pnl-store', storage_type='memory'),
        # Download component for table export
        dcc.Download(id="download-pnl-table"),
        # Main content panel
        html.Div(
            style=styles['panel'],
            children=[
                # Filters section with title
                html.Div(
                    className="inline-section-header",
                    style={'marginBottom': '20px', 'paddingBottom': '15px'},
                    children=[
                        # ~~~~~ ROW 1: Date dropdown ~~~~~
                        html.Div([
                            html.Label('Trade Date:', className="inline-filter-label"),
                            dcc.Dropdown(
                                id='pnl-date-dropdown',
                                options=[{'label': d, 'value': d} for d in available_dates],
                                value=available_dates[0] if available_dates else None,
                                clearable=False,
                                className="inline-dropdown-date"
                            )
                        ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap', 'margin-bottom': '8px'}),
                        
                        # ~~~~~ ROW 2: Strategy dropdown ~~~~~
                        html.Div([
                            html.Label('Strategies:', className="inline-filter-label"),
                            dcc.Dropdown(
                                id='pnl-strategy-dropdown',
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
                    id="pnl-loading",
                    type="circle",
                    children=[
                        html.Div([
                            html.H4(
                                "P&L and Option Values Table",
                                style={
                                    'color': '#1e3a8a',
                                    'fontWeight': 'bold',
                                    'margin': '0',
                                    'display': 'inline-block'
                                }
                            ),
                            html.Button(
                                'Export',
                                id='export-pnl-table-btn',
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
                            id='pnl-error-message',
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
                            id='pnl-table',
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
                                    'if': {'filter_query': '{year} = "All"'},
                                    'backgroundColor': 'rgb(245, 245, 245)',
                                    'fontWeight': 'bold',
                                    'fontStyle': 'italic'
                                },
                                {
                                    'if': {'filter_query': '{substrategy} = "All"'},
                                    'backgroundColor': 'rgb(240, 240, 240)',
                                    'fontWeight': 'bold'
                                },
                                {
                                    'if': {'filter_query': '{substrategy} = "All"'},
                                    'backgroundColor': 'rgb(230, 230, 230)',
                                    'fontWeight': 'bold'
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
    Output('pnl-strategy-dropdown', 'options'),
    Output('pnl-strategy-dropdown', 'value'),
    Input('pnl-date-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks')
)
def update_strategy_options(selected_date, n_clicks):
    """
    Whenever the user picks a new date, fetch the distinct substrategies
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
    Output('pnl-table', 'data'),
    Output('pnl-table', 'columns'),
    Output('pnl-error-message', 'children'),
    Output('pnl-error-message', 'style'),
    Input('pnl-date-dropdown', 'value'),
    Input('pnl-strategy-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks')
)
def update_pnl_table(selected_date, selected_strategies, n_clicks):
    """
    Queries and groups data by (substrategy, year) for the chosen date,
    then filters to the selected strategies, and returns the data to the table
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

    df = fetch_pnl_data(engine, selected_date, selected_strategies)
    if df.empty:
        error_message = "⚠️ Unable to retrieve PnL data for the selected date. This may be due to database connectivity issues or missing data for this date."
        return [], [], error_message, error_style_visible

    # Convert DataFrame to dash_table-friendly format
    data_records = df.to_dict('records')

    # Build columns dynamically, formatting numeric columns with thousand separators
    # and using friendly column names
    columns = build_columns_with_numeric_format(df)

    return data_records, columns, "", error_style_hidden


# 4.3) Export callback for P&L table
@dash.callback(
    Output("download-pnl-table", "data"),
    Input("export-pnl-table-btn", "n_clicks"),
    [State('pnl-date-dropdown', 'value'),
     State('pnl-strategy-dropdown', 'value')],
    prevent_initial_call=True
)
def export_pnl_table(n_clicks, selected_date, selected_strategies):
    """Export P&L and Option Values Table to Excel"""
    if n_clicks is None or selected_date is None:
        raise dash.exceptions.PreventUpdate
    
    try:
        # Get the same data as displayed in the table
        df = fetch_pnl_data(engine, selected_date, selected_strategies)
        if df.empty:
            raise dash.exceptions.PreventUpdate

        # Rename columns to user-friendly names
        df_renamed = df.rename(columns=COLUMN_NAME_MAPPING)
        
        # Generate filename with timestamp
        timestamp = pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')
        filename = f"pnl_option_values_table_{selected_date}_{timestamp}.xlsx"
        
        return dcc.send_data_frame(df_renamed.to_excel, filename, sheet_name="P&L and Option Values", index=False)
        
    except Exception as e:
        print(f"Export error: {e}")
        raise dash.exceptions.PreventUpdate
 