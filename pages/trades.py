import dash
import dash_ag_grid as dag
from dash import html, dcc, Input, Output, State
import io
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
except Exception:
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
    except Exception:
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


COLUMN_GRID_HEADER_MAPPING = {
    'type_option': 'Type',
    'put_call': 'C/P',
    'asset_a': 'Asset A',
    'asset_b': 'Asset B',
    'asset_a_multiplier': 'A Mult',
    'asset_a_premium': 'A Prem',
    'asset_b_multiplier': 'B Mult',
    'asset_b_premium': 'B Prem',
    'price_a': 'Price A',
    'price_b': 'Price B',
    'adjusted_vol_a': 'Vol A',
    'adjusted_vol_b': 'Vol B',
    'expiration_date': 'Expiry',
    'intrinsic_value': 'Intrinsic',
    'time_value': 'Time',
    'qty_intrinsic_value': 'Intrinsic Total',
    'qty_time_value': 'Time Total',
    'qty_pnl': 'Total P&L',
}

TRADES_COLUMN_WIDTHS = {
    'substrategy': 154,
    'type_option': 82,
    'put_call': 58,
    'asset_a': 74,
    'asset_b': 74,
    'asset_a_multiplier': 70,
    'asset_a_premium': 78,
    'asset_b_multiplier': 70,
    'asset_b_premium': 78,
    'price_a': 72,
    'price_b': 72,
    'adjusted_vol_a': 66,
    'adjusted_vol_b': 66,
    'premium': 78,
    'quantity': 84,
    'unit_quantity': 64,
    'strike': 74,
    'expiration_date': 92,
    'intrinsic_value': 82,
    'time_value': 74,
    'qty_intrinsic_value': 108,
    'qty_time_value': 98,
    'qty_pnl': 94,
}

TRADES_DECIMAL_COLUMNS = {
    'asset_a_multiplier',
    'asset_a_premium',
    'asset_b_multiplier',
    'asset_b_premium',
    'price_a',
    'price_b',
    'adjusted_vol_a',
    'adjusted_vol_b',
    'premium',
    'intrinsic_value',
    'time_value',
}

TRADES_INTEGER_COLUMNS = {
    'quantity',
    'strike',
    'qty_intrinsic_value',
    'qty_time_value',
    'qty_pnl',
}

TRADES_NUMERIC_COLUMNS = TRADES_DECIMAL_COLUMNS | TRADES_INTEGER_COLUMNS
TRADES_TEXT_COLUMNS = {
    'substrategy',
    'type_option',
    'put_call',
    'asset_a',
    'asset_b',
    'unit_quantity',
    'expiration_date',
}
SUMMARY_COLUMNS = [
    'substrategy', 'type_option', 'unit_quantity', 'strike', 'expiration_date',
    'premium', 'quantity', 'intrinsic_value', 'time_value',
    'qty_intrinsic_value', 'qty_time_value', 'qty_pnl'
]
DETAIL_COLUMNS = SUMMARY_COLUMNS + [
    'put_call',
    'asset_a',
    'asset_b',
    'asset_a_multiplier',
    'asset_a_premium',
    'asset_b_multiplier',
    'asset_b_premium',
    'price_a',
    'price_b',
    'adjusted_vol_a',
    'adjusted_vol_b',
]


def _trades_raw_number_expression(field):
    return f"Number(params.data && params.data['__{field}_raw'])"


def build_trades_column_defs(df):
    """Create compact AG Grid column definitions for the trades table."""
    column_defs = []

    for col in df.columns:
        full_name = COLUMN_NAME_MAPPING.get(col, col)
        header_name = COLUMN_GRID_HEADER_MAPPING.get(col, full_name)
        width = TRADES_COLUMN_WIDTHS.get(col, 86)
        is_numeric = pd.api.types.is_numeric_dtype(df[col]) or col in TRADES_NUMERIC_COLUMNS
        is_text_column = col in TRADES_TEXT_COLUMNS

        column_def = {
            'headerName': header_name,
            'field': col,
            'sortable': True,
            'filter': False,
            'resizable': True,
            'width': width,
            'minWidth': min(width, 70),
            'maxWidth': max(width + 22, 104),
            'tooltipField': col,
            'headerTooltip': full_name,
            'suppressMovable': col in {'substrategy', 'type_option'},
            'cellClass': (
                'mckinsey-ag-grid-cell mckinsey-ag-grid-text-cell trades-text-cell'
                if is_text_column else
                'mckinsey-ag-grid-cell mckinsey-ag-grid-number-cell trades-number-cell'
            ),
            'headerClass': (
                'mckinsey-ag-grid-header trades-text-header'
                if is_text_column else
                'mckinsey-ag-grid-header trades-number-header'
            ),
        }

        if col in {'substrategy', 'type_option'}:
            column_def.update({'pinned': 'left', 'lockPinned': True})
        if col == 'expiration_date':
            column_def.update({'sort': 'asc'})

        if is_numeric:
            raw_value = _trades_raw_number_expression(col)
            column_def.update({
                'type': 'rightAligned',
                'cellClassRules': {
                    'trades-positive-cell': (
                        f"['intrinsic_value', 'qty_intrinsic_value', 'qty_pnl'].includes('{col}') "
                        f"&& {raw_value} > 0"
                    ),
                    'trades-negative-cell': f"{raw_value} < 0",
                    'trades-missing-cell': (
                        f"params.data === null || params.data === undefined "
                        f"|| params.data['__{col}_raw'] === null || params.data['__{col}_raw'] === undefined "
                        f"|| isNaN(Number(params.data['__{col}_raw']))"
                    ),
                },
            })

        column_defs.append(column_def)

    return column_defs


def _format_trades_display_value(key, value):
    if pd.isna(value):
        return None
    if key == 'expiration_date':
        return pd.to_datetime(value).strftime('%Y-%m-%d')
    if key in TRADES_DECIMAL_COLUMNS:
        return f"{float(value):,.2f}"
    if key in TRADES_INTEGER_COLUMNS:
        return f"{float(value):,.0f}"
    return value


def _raw_trades_numeric_value(key, value):
    if key not in TRADES_NUMERIC_COLUMNS or pd.isna(value):
        return None
    return float(value)


def _clean_trades_records(df):
    records = []
    for row in df.to_dict('records'):
        clean_row = {}
        for key, value in row.items():
            clean_row[key] = _format_trades_display_value(key, value)
            if key in TRADES_NUMERIC_COLUMNS:
                clean_row[f'__{key}_raw'] = _raw_trades_numeric_value(key, value)
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


def _filter_trades_view_columns(df, view_mode):
    column_order = SUMMARY_COLUMNS if view_mode == 'summary' else DETAIL_COLUMNS
    available_columns = [col for col in column_order if col in df.columns]
    return df[available_columns]


# ------------------------------------------------------------------
# 3) BUILD THE DASH LAYOUT
# ------------------------------------------------------------------
ERROR_STYLE_HIDDEN = {'display': 'none'}
ERROR_STYLE_VISIBLE = {'display': 'block'}


def _build_trades_filter_bar():
    return html.Div(
        [
            html.Div(
                [
                    html.Span('COB', className='filter-group-header'),
                    dcc.Dropdown(
                        id='trades-date-dropdown',
                        options=[],
                        value=None,
                        clearable=False,
                        className='inline-dropdown-date trades-filter-dropdown trades-date-dropdown',
                    ),
                ],
                className='filter-group trades-sticky-filter-group trades-date-filter-group',
            ),
            html.Div(
                [
                    html.Span('View', className='filter-group-header'),
                    dcc.RadioItems(
                        id='trades-view-radio',
                        options=[
                            {'label': 'Summary', 'value': 'summary'},
                            {'label': 'Detail', 'value': 'detail'},
                        ],
                        value='summary',
                        className='trades-view-selector',
                    ),
                ],
                className='filter-group trades-sticky-filter-group trades-view-filter-group',
            ),
            html.Div(
                [
                    html.Span('Strategies', className='filter-group-header'),
                    dcc.Dropdown(
                        id='trades-strategy-dropdown',
                        options=[],
                        value=[],
                        multi=True,
                        placeholder='Select strategies...',
                        className='inline-dropdown-multi-strategies trades-filter-dropdown trades-strategy-dropdown',
                    ),
                ],
                className='filter-group trades-sticky-filter-group trades-strategy-filter-group',
            ),
        ],
        className='professional-section-header trades-sticky-filter-bar',
    )


def _build_trades_section_header(title, actions=None):
    return html.Div(
        [
            html.Div(
                [html.H3(title, className='section-title-inline')],
                className='trades-section-title-row',
            ),
            html.Div(actions or [], className='trades-section-actions'),
        ],
        className='trades-section-header',
    )


layout = html.Div(
    [
        dcc.Download(id='download-trades-table'),
        dcc.Store(id='trades-data-store', storage_type='memory'),
        _build_trades_filter_bar(),
        html.Div(
            [
                _build_trades_section_header(
                    'Trades',
                    actions=[
                        html.Button(
                            'Export',
                            id='export-trades-table-btn',
                            className='custom-export-btn trades-export-button',
                        ),
                    ],
                ),
                dcc.Loading(
                    id='trades-loading',
                    type='circle',
                    children=[
                        html.Div(id='trades-error-message', className='trades-error-message', style=ERROR_STYLE_HIDDEN),
                        html.Div(
                            dag.AgGrid(
                                id='trades-table',
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
                                className='ag-theme-alpine mckinsey-ag-grid supply-dest-summary-grid trades-ag-grid',
                                style={'width': '100%', 'height': 'calc(100vh - 250px)', 'minHeight': '340px'},
                                dangerously_allow_code=True,
                            ),
                            className='trades-table-container',
                        ),
                    ],
                ),
            ],
            className='trades-section',
        ),
    ],
    className='options-dashboard-container trades-page',
)

# ------------------------------------------------------------------
# 4) CALLBACKS
# ------------------------------------------------------------------

@dash.callback(
    Output('trades-date-dropdown', 'options'),
    Output('trades-date-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    State('trades-date-dropdown', 'value'),
)
def update_trades_date_options(n_clicks, current_date):
    del n_clicks
    dates = get_available_dates(engine)
    options = [{'label': date, 'value': date} for date in dates]
    selected_date = current_date if current_date in dates else (dates[0] if dates else None)
    return options, selected_date

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


# 4.2) Fetch grouped trade data when date or strategy selection changes
@dash.callback(
    Output('trades-data-store', 'data'),
    Input('trades-date-dropdown', 'value'),
    Input('trades-strategy-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks')
)
def update_trades_data_store(selected_date, selected_strategies, n_clicks):
    """
    Queries and groups trades data by (substrategy, type_option, strike, expiration_date) 
    for the chosen date, then filters to the selected strategies.
    """
    del n_clicks
    if not selected_date:
        return {'data': None, 'error': '', 'error_visible': False}

    df = fetch_trades_data(engine, selected_date, selected_strategies)
    if df.empty:
        error_message = "Unable to retrieve trades data for the selected date. This may be due to database connectivity issues or missing data for this date."
        return {'data': None, 'error': error_message, 'error_visible': True}

    # Format expiration_date to show only date part (remove time)
    if 'expiration_date' in df.columns:
        df['expiration_date'] = pd.to_datetime(df['expiration_date'])

    return {
        'data': df.to_json(date_format='iso', orient='split'),
        'error': '',
        'error_visible': False,
    }


# 4.3) Update the AG Grid when data or view mode changes
@dash.callback(
    Output('trades-table', 'rowData'),
    Output('trades-table', 'columnDefs'),
    Output('trades-error-message', 'children'),
    Output('trades-error-message', 'style'),
    Input('trades-data-store', 'data'),
    Input('trades-view-radio', 'value')
)
def update_trades_table(data_store, view_mode):
    """
    Renders the current grouped trades dataset in summary or detail mode.
    """
    if not data_store:
        return [], [], "", ERROR_STYLE_HIDDEN

    if data_store.get('error_visible'):
        return [], [], data_store.get('error', ''), ERROR_STYLE_VISIBLE

    if not data_store.get('data'):
        return [], [], "", ERROR_STYLE_HIDDEN

    df = pd.read_json(io.StringIO(data_store['data']), orient='split')
    if df.empty:
        return [], [], "", ERROR_STYLE_HIDDEN

    if 'expiration_date' in df.columns:
        df['expiration_date'] = pd.to_datetime(df['expiration_date'])

    # Filter columns based on view mode
    df = _filter_trades_view_columns(df, view_mode)

    data_records = _clean_trades_records(df)

    columns = build_trades_column_defs(df)

    return data_records, columns, "", ERROR_STYLE_HIDDEN


# 4.4) Export callback for trades table
@dash.callback(
    Output("download-trades-table", "data"),
    Input("export-trades-table-btn", "n_clicks"),
    [State('trades-date-dropdown', 'value'),
     State('trades-view-radio', 'value'),
     State('trades-table', 'rowData'),
     State('trades-table', 'columnDefs')],
    prevent_initial_call=True
)
def export_trades_table(n_clicks, selected_date, view_mode, row_data, column_defs):
    """Export Trades Detail Table to Excel"""
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
        view_suffix = f"_{view_mode}" if view_mode else ""
        filename = f"trades_detail_table_{selected_date}{view_suffix}_{timestamp}.xlsx"

        return dcc.send_data_frame(df_renamed.to_excel, filename, sheet_name="Trades Detail", index=False)

    except Exception:
        raise dash.exceptions.PreventUpdate
