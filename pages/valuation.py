import dash
import dash_ag_grid as dag
from dash import html, dcc, Input, Output, State
import pandas as pd
import numpy as np
import configparser
from sqlalchemy import create_engine
import os
from functools import lru_cache

from dataframe_utils import concat_dataframes


#------ code to be able to access config.ini, even having the path in the .virtualenvs is not working without it ------#
try:
    # Get the directory where your script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate to the directory containing config.ini
    # Adjust the number of '..' as needed to reach the correct directory
    config_dir = os.path.abspath(os.path.join(script_dir, '..','..'))  # Go up one level
    CONFIG_FILE_PATH = os.path.join(config_dir, 'config.ini')
except Exception:
    CONFIG_FILE_PATH = 'config.ini'  # Assumes it's in the same directory or the path it is detected

# --- Load Configuration from INI File ---
config_reader = configparser.ConfigParser(interpolation=None)
config_reader.read(CONFIG_FILE_PATH)

# Read values from the ini file sections
DB_CONNECTION_STRING = config_reader.get('DATABASE', 'CONNECTION_STRING', fallback=None)
DB_SCHEMA = config_reader.get('DATABASE', 'SCHEMA', fallback='at_lng')
MO_CONNECTION_STRING = (
    config_reader.get('DATABASE', 'MIDDLE_OFFICE_CONNECTION_STRING', fallback=None)
    or DB_CONNECTION_STRING
)

# ---------------------- Configuration ----------------------
#Default DBAPI (should use psycopg2 as default, pg8000 it's an alternative but we don't have the library installed)
# create engine
engine = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True)
engine_mo = engine if MO_CONNECTION_STRING == DB_CONNECTION_STRING else create_engine(MO_CONNECTION_STRING, pool_pre_ping=True)
_valuation_refresh_key = None



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
    except Exception:
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
    except Exception:
        return []


def _read_pnl_sources_for_date(selected_date, db_engine):
    try:
        query = f"""
                SELECT
                    substrategy, ytd as ytd_hedging, 'All' as year
                FROM {DB_SCHEMA}.pnl_aspect
                WHERE "COB" = '{selected_date}'
                """
        df_aspect_pnl = pd.read_sql(query, engine_mo)
    except Exception:
        df_aspect_pnl = pd.DataFrame()

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
    df = pd.read_sql(query, db_engine)
    return df, df_aspect_pnl


@lru_cache(maxsize=8)
def _fetch_pnl_sources_for_date(selected_date):
    return _read_pnl_sources_for_date(selected_date, engine)


def fetch_pnl_data(db_engine, selected_date, selected_strategies):
    """
    Query 'at_lng.trades_options_valuation' for the given COB date
    and filter to only the specified substrategies.
    Group by (substrategy, year from maturity_date_a).
    Aggregations:
    - Averages for: price, intrinsic_value, time_value
    - Sums for: qty_value, qty_intrinsic_value, qty_time_value
    """
    try:
        if db_engine is engine:
            df, df_aspect_pnl = _fetch_pnl_sources_for_date(selected_date)
        else:
            df, df_aspect_pnl = _read_pnl_sources_for_date(selected_date, db_engine)
        df = df.copy()
        df_aspect_pnl = df_aspect_pnl.copy()
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
        grouped = concat_dataframes([grouped, substrategy_totals, unit_totals, pd.DataFrame([grand_total])], ignore_index=True)

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
                                                              'zzz2' if x == 'All' else x)

            # Sort by unit_quantity, substrategy, then year
            df_sorted = df.sort_values(['unit_quantity', '_sort_substrategy', '_sort_year'])

            # Drop the temporary sorting columns
            df_sorted = df_sorted.drop(columns=['_sort_year', '_sort_substrategy'])
            return df_sorted

        grouped = custom_sort(grouped)
        return grouped
    except Exception:
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

COLUMN_GRID_HEADER_MAPPING = {
    'intrinsic_value': 'Intrinsic',
    'time_value': 'Time',
    'qty_value': 'Value Total',
    'qty_intrinsic_value': 'Intrinsic Total',
    'qty_time_value': 'Time Total',
    'qty_premium': 'Premium',
    'qty_pnl': 'Total P&L',
    'ytd_hedging': 'Aspect YTD',
}


VALUATION_COLUMN_WIDTHS = {
    'substrategy': 154,
    'year': 56,
    'unit_quantity': 64,
    'price': 74,
    'intrinsic_value': 92,
    'time_value': 86,
    'qty_value': 96,
    'qty_intrinsic_value': 108,
    'qty_time_value': 108,
    'qty_premium': 100,
    'qty_pnl': 94,
    'ytd_hedging': 108,
}

VALUATION_DECIMAL_COLUMNS = {'price', 'intrinsic_value', 'time_value'}
VALUATION_TOTAL_COLUMNS = {
    'qty_value',
    'qty_intrinsic_value',
    'qty_time_value',
    'qty_premium',
    'qty_pnl',
    'ytd_hedging',
}


def _parse_display_number_expression():
    return "Number(params.data && params.data['__{field}_raw'])"


def build_valuation_column_defs(df):
    """Create compact AG Grid column definitions for the valuation table."""
    column_defs = []

    for col in df.columns:
        full_name = COLUMN_NAME_MAPPING.get(col, col)
        friendly_name = COLUMN_GRID_HEADER_MAPPING.get(col, full_name)
        width = VALUATION_COLUMN_WIDTHS.get(col, 98)
        is_numeric = pd.api.types.is_numeric_dtype(df[col]) or col in VALUATION_DECIMAL_COLUMNS | VALUATION_TOTAL_COLUMNS
        is_text_column = col in {'substrategy', 'year', 'unit_quantity'}

        column_def = {
            'headerName': friendly_name,
            'field': col,
            'sortable': True,
            'filter': False,
            'resizable': True,
            'width': width,
            'minWidth': min(width, 76),
            'maxWidth': max(width + 20, 104),
            'tooltipField': col,
            'headerTooltip': full_name,
            'suppressMovable': col in {'substrategy', 'year', 'unit_quantity'},
            'cellClass': (
                'mckinsey-ag-grid-cell mckinsey-ag-grid-text-cell valuation-text-cell'
                if is_text_column else
                'mckinsey-ag-grid-cell mckinsey-ag-grid-number-cell valuation-number-cell'
            ),
            'headerClass': (
                'mckinsey-ag-grid-header valuation-text-header'
                if is_text_column else
                'mckinsey-ag-grid-header valuation-number-header'
            ),
        }

        if col == 'substrategy':
            column_def.update({'pinned': 'left', 'lockPinned': True})
        elif col in {'year', 'unit_quantity'}:
            column_def.update({'pinned': 'left', 'lockPinned': True})

        if is_numeric:
            raw_value = _parse_display_number_expression().format(field=col)
            column_def.update({
                'type': 'rightAligned',
                'cellClassRules': {
                    'valuation-positive-cell': (
                        f"['qty_pnl', 'ytd_hedging'].includes('{col}') && {raw_value} > 0"
                    ),
                    'valuation-negative-cell': f"{raw_value} < 0",
                    'valuation-missing-cell': (
                        f"params.data === null || params.data === undefined "
                        f"|| params.data['__{col}_raw'] === null || params.data['__{col}_raw'] === undefined "
                        f"|| isNaN(Number(params.data['__{col}_raw']))"
                    ),
                },
            })

        column_defs.append(column_def)

    return column_defs


def _format_valuation_display_value(key, value):
    if pd.isna(value):
        return None
    if key in VALUATION_DECIMAL_COLUMNS:
        return f"{float(value):,.2f}"
    if key in VALUATION_TOTAL_COLUMNS:
        return f"{float(value):,.0f}"
    return value


def _raw_valuation_numeric_value(key, value):
    if key not in VALUATION_DECIMAL_COLUMNS | VALUATION_TOTAL_COLUMNS or pd.isna(value):
        return None
    return float(value)


def _clean_valuation_records(df):
    records = []
    for row in df.to_dict('records'):
        clean_row = {}
        for key, value in row.items():
            clean_row[key] = _format_valuation_display_value(key, value)
            raw_value = _raw_valuation_numeric_value(key, value)
            if key in VALUATION_DECIMAL_COLUMNS | VALUATION_TOTAL_COLUMNS:
                clean_row[f'__{key}_raw'] = raw_value
        records.append(clean_row)
    return records


def _export_df_from_grid_records(row_data, column_defs):
    if not row_data or not column_defs:
        return pd.DataFrame()

    fields = [
        column.get('field')
        for column in column_defs
        if isinstance(column, dict) and column.get('field') and not str(column.get('field')).startswith('__')
    ]
    if not fields:
        return pd.DataFrame()

    export_records = []
    for row in row_data:
        export_row = {}
        for field in fields:
            raw_key = f'__{field}_raw'
            export_row[field] = row.get(raw_key) if raw_key in row and row.get(raw_key) is not None else row.get(field)
        export_records.append(export_row)

    return pd.DataFrame(export_records, columns=fields)


# ------------------------------------------------------------------
# 3) BUILD THE DASH LAYOUT
# ------------------------------------------------------------------
ERROR_STYLE_HIDDEN = {'display': 'none'}
ERROR_STYLE_VISIBLE = {'display': 'block'}


def _build_valuation_filter_bar():
    return html.Div(
        [
            html.Div(
                [
                    html.Span('COB', className='filter-group-header'),
                    dcc.Dropdown(
                        id='pnl-date-dropdown',
                        options=[],
                        value=None,
                        clearable=False,
                        className='inline-dropdown-date valuation-filter-dropdown valuation-date-dropdown',
                    ),
                ],
                className='filter-group valuation-sticky-filter-group valuation-date-filter-group',
            ),
            html.Div(
                [
                    html.Span('Strategies', className='filter-group-header'),
                    dcc.Dropdown(
                        id='pnl-strategy-dropdown',
                        options=[],
                        value=[],
                        multi=True,
                        placeholder='Select strategies...',
                        className='inline-dropdown-multi-strategies valuation-filter-dropdown valuation-strategy-dropdown',
                    ),
                ],
                className='filter-group valuation-sticky-filter-group valuation-strategy-filter-group',
            ),
        ],
        className='professional-section-header valuation-sticky-filter-bar',
    )


def _build_valuation_section_header(title, actions=None):
    return html.Div(
        [
            html.Div(
                [html.H3(title, className='section-title-inline')],
                className='valuation-section-title-row',
            ),
            html.Div(actions or [], className='valuation-section-actions'),
        ],
        className='valuation-section-header',
    )


layout = html.Div(
    [
        dcc.Download(id='download-pnl-table'),
        _build_valuation_filter_bar(),
        html.Div(
            [
                _build_valuation_section_header(
                    'P&L and Option Values',
                    actions=[
                        html.Button(
                            'Export',
                            id='export-pnl-table-btn',
                            className='custom-export-btn valuation-export-button',
                        ),
                    ],
                ),
                dcc.Loading(
                    id='pnl-loading',
                    type='circle',
                    children=[
                        html.Div(id='pnl-error-message', className='valuation-error-message', style=ERROR_STYLE_HIDDEN),
                        html.Div(
                            dag.AgGrid(
                                id='pnl-table',
                                rowData=[],
                                columnDefs=[],
                                defaultColDef={
                                    'sortable': True,
                                    'filter': False,
                                    'resizable': True,
                                    'suppressHeaderMenuButton': True,
                                    'suppressHeaderFilterButton': True,
                                    'wrapHeaderText': False,
                                    'autoHeaderHeight': False,
                                },
                                dashGridOptions={
                                    'domLayout': 'normal',
                                    'rowHeight': 24,
                                    'headerHeight': 30,
                                    'pagination': False,
                                    'suppressPaginationPanel': True,
                                    'enableCellTextSelection': True,
                                    'ensureDomOrder': True,
                                    'animateRows': False,
                                    'rowSelection': {
                                        'mode': 'singleRow',
                                        'checkboxes': False,
                                        'enableClickSelection': True,
                                    },
                                },
                                rowClassRules={
                                    'valuation-grand-total-row': "params.data && params.data.substrategy === 'All'",
                                    'valuation-subtotal-row': "params.data && params.data.year === 'All'",
                                },
                                className='ag-theme-alpine mckinsey-ag-grid supply-dest-summary-grid valuation-ag-grid',
                                style={'width': '100%', 'height': 'calc(100vh - 250px)', 'minHeight': '320px'},
                                dangerously_allow_code=True,
                            ),
                            className='valuation-table-container',
                        ),
                    ],
                ),
            ],
            className='valuation-section',
        ),
    ],
    className='options-dashboard-container valuation-page',
)

# ------------------------------------------------------------------
# 4) CALLBACKS
# ------------------------------------------------------------------

@dash.callback(
    Output('pnl-date-dropdown', 'options'),
    Output('pnl-date-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    State('pnl-date-dropdown', 'value'),
)
def update_pnl_date_options(n_clicks, current_date):
    del n_clicks
    dates = get_available_dates(engine)
    options = [{'label': date, 'value': date} for date in dates]
    selected_date = current_date if current_date in dates else (dates[0] if dates else None)
    return options, selected_date

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


# 4.2) Update the AG Grid when date or strategy selection changes
@dash.callback(
    Output('pnl-table', 'rowData'),
    Output('pnl-table', 'columnDefs'),
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
    global _valuation_refresh_key
    if n_clicks != _valuation_refresh_key:
        _fetch_pnl_sources_for_date.cache_clear()
        _valuation_refresh_key = n_clicks

    if not selected_date:
        return [], [], "", ERROR_STYLE_HIDDEN

    df = fetch_pnl_data(engine, selected_date, selected_strategies)
    if df.empty:
        error_message = "Unable to retrieve P&L data for the selected date. This may be due to database connectivity issues or missing data for this date."
        return [], [], error_message, ERROR_STYLE_VISIBLE

    data_records = _clean_valuation_records(df)

    columns = build_valuation_column_defs(df)

    return data_records, columns, "", ERROR_STYLE_HIDDEN


# 4.3) Export callback for P&L table
@dash.callback(
    Output("download-pnl-table", "data"),
    Input("export-pnl-table-btn", "n_clicks"),
    [State('pnl-date-dropdown', 'value'),
     State('pnl-table', 'rowData'),
     State('pnl-table', 'columnDefs')],
    prevent_initial_call=True
)
def export_pnl_table(n_clicks, selected_date, row_data, column_defs):
    """Export P&L and Option Values Table to Excel"""
    if n_clicks is None or selected_date is None:
        raise dash.exceptions.PreventUpdate

    try:
        # Export the current AG Grid state to avoid a duplicate DB read.
        df = _export_df_from_grid_records(row_data, column_defs)
        if df.empty:
            raise dash.exceptions.PreventUpdate

        # Rename columns to user-friendly names
        df_renamed = df.rename(columns=COLUMN_NAME_MAPPING)

        # Generate filename with timestamp
        timestamp = pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')
        filename = f"pnl_option_values_table_{selected_date}_{timestamp}.xlsx"

        return dcc.send_data_frame(df_renamed.to_excel, filename, sheet_name="P&L and Option Values", index=False)

    except Exception:
        raise dash.exceptions.PreventUpdate
