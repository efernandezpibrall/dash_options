import io

import dash
import dash_ag_grid as dag
from dash import html, dcc, Input, Output, State
import pandas as pd
import numpy as np
from sqlalchemy import text
from functools import lru_cache

from dataframe_utils import concat_dataframes
from db_fallback import DB_SCHEMA
from runtime_config import get_database_engine
_valuation_refresh_key = None
_valuation_data_error = None
_valuation_warning = None
VALUATION_CURRENT_TABLE = f'{DB_SCHEMA}.trades_options_valuation_current'



# ------------------------------------------------------------------
# HELPER FUNCTIONS FOR DATA ACCESS AND FORMATTING
# ------------------------------------------------------------------
def get_available_dates(engine):
    """
    Fetch distinct COB dates from the published valuation view
    in descending order.
    """
    try:
        query = """
        SELECT DISTINCT cob_date
        FROM {valuation_table}
        ORDER BY cob_date DESC
        """.format(valuation_table=VALUATION_CURRENT_TABLE)
        dates_df = pd.read_sql(text(query), engine)
        # Convert each date to string, e.g. '2023-01-01'
        return [d.strftime('%Y-%m-%d') for d in dates_df['cob_date']]
    except Exception:
        return []


def get_strategies(engine, selected_date):
    """
    Return a sorted list of distinct substrategies for the given COB date.
    """
    try:
        query = text(f"""
        SELECT DISTINCT substrategy
        FROM {VALUATION_CURRENT_TABLE}
        WHERE cob_date = :selected_date
        ORDER BY substrategy
        """)
        df_strats = pd.read_sql(query, engine, params={'selected_date': selected_date})
        return sorted(df_strats['substrategy'].dropna().unique().tolist())
    except Exception:
        return []


def _read_pnl_sources_for_date(selected_date, db_engine):
    try:
        query = text(f"""
                SELECT
                    substrategy, currency, ytd as ytd_hedging, 'All' as year
                FROM {DB_SCHEMA}.pnl_aspect
                WHERE "COB" = :selected_date
                """)
        df_aspect_pnl = pd.read_sql(
            query,
            get_database_engine(middle_office=True),
            params={'selected_date': selected_date},
        )
        invalid_aspect_currency = (
            df_aspect_pnl['currency'].isna()
            | ~df_aspect_pnl['currency'].astype('string').str.fullmatch(
                r'[A-Z]{3}',
                na=False,
            )
        )
        if invalid_aspect_currency.any():
            df_aspect_pnl = pd.DataFrame()
            df_aspect_pnl.attrs['currency_warning'] = (
                'Aspect YTD suppressed: its source does not provide a valid '
                'currency for every amount.'
            )
    except Exception:
        df_aspect_pnl = pd.DataFrame()
        df_aspect_pnl.attrs['currency_warning'] = (
            'Aspect YTD suppressed: currency is unavailable from the source.'
        )

    query = text(f"""
    SELECT
        currency,
        unit_quantity,
        substrategy,
        maturity_date_a,
        price,intrinsic_value,time_value,
        qty_value,qty_intrinsic_value,qty_time_value,qty_premium,qty_pnl,
        quantity
    FROM {VALUATION_CURRENT_TABLE}
    WHERE cob_date = :selected_date
    """)
    df = pd.read_sql(query, db_engine, params={'selected_date': selected_date})
    return df, df_aspect_pnl


def fetch_ttf_source_comparison(db_engine, selected_date, selected_strategies):
    """Read published ICAP and diagnostic ICE values for TTF TFO trades."""
    query = text(f"""
        SELECT
            valuation_run_id,
            valuation_revision,
            valuation_methodology_version,
            valuation_published_at,
            currency,
            substrategy,
            buy_sell,
            put_call,
            strike,
            forward_price_used,
            expiration_date,
            volatility_used,
            price,
            qty_value,
            qty_pnl,
            comparison_call_delta_used,
            comparison_volatility_used,
            comparison_price,
            comparison_qty_value,
            comparison_qty_pnl,
            comparison_status,
            volatility_source_name,
            comparison_source_name
        FROM {VALUATION_CURRENT_TABLE}
        WHERE cob_date = :selected_date
          AND contract_convention_code = 'ICE_TTF_TFO'
        ORDER BY substrategy, strike, buy_sell
    """)
    frame = pd.read_sql(
        query,
        db_engine,
        params={'selected_date': selected_date},
    )
    if selected_strategies:
        frame = frame[frame['substrategy'].isin(selected_strategies)]
    return frame


@lru_cache(maxsize=8)
def _fetch_pnl_sources_for_date(selected_date):
    return _read_pnl_sources_for_date(selected_date, get_database_engine())


def fetch_pnl_data(db_engine, selected_date, selected_strategies):
    """
    Query the published valuation view for the given COB date
    and filter to only the specified substrategies.
    Group by (substrategy, year from maturity_date_a).
    Aggregations:
    - Averages for: price, intrinsic_value, time_value
    - Sums for: qty_value, qty_intrinsic_value, qty_time_value
    """
    global _valuation_data_error, _valuation_warning
    _valuation_data_error = None
    _valuation_warning = None
    try:
        if db_engine is get_database_engine(required=False):
            df, df_aspect_pnl = _fetch_pnl_sources_for_date(selected_date)
        else:
            df, df_aspect_pnl = _read_pnl_sources_for_date(selected_date, db_engine)
        df = df.copy()
        df_aspect_pnl = df_aspect_pnl.copy()
        _valuation_warning = df_aspect_pnl.attrs.get('currency_warning')
        if df.empty:
            return pd.DataFrame()
        invalid_currency = (
            df['currency'].isna()
            | ~df['currency'].astype('string').str.fullmatch(
                r'[A-Z]{3}',
                na=False,
            )
        )
        if invalid_currency.any():
            raise ValueError(
                'Published valuation rows contain invalid currency codes.'
            )

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
            df.groupby(
                ['currency', 'substrategy', 'year', 'unit_quantity'],
                dropna=False,
            )
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
        substrategy_totals = grouped.groupby(
            ['currency', 'substrategy', 'unit_quantity'],
            dropna=False,
        ).agg({
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
        unit_totals = grouped.groupby(
            ['currency', 'unit_quantity'],
            dropna=False,
        ).agg({
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

        # 3. Add one currency-only total row. Per-unit averages are undefined
        # across unlike contract units and therefore deliberately blank.
        currency_totals = (
            grouped.groupby('currency', dropna=False)
            .agg({
                'qty_value': 'sum',
                'qty_intrinsic_value': 'sum',
                'qty_time_value': 'sum',
                'qty_premium': 'sum',
                'qty_pnl': 'sum',
            })
            .reset_index()
        )
        currency_totals['substrategy'] = 'All'
        currency_totals['year'] = None
        currency_totals['unit_quantity'] = 'All'
        currency_totals['price'] = np.nan
        currency_totals['intrinsic_value'] = np.nan
        currency_totals['time_value'] = np.nan

        # Concatenate all dataframes together
        grouped = concat_dataframes(
            [grouped, substrategy_totals, unit_totals, currency_totals],
            ignore_index=True,
        )

        # Only merge PnL data if it's available
        if not df_aspect_pnl.empty:
            grouped = pd.merge(
                grouped,
                df_aspect_pnl,
                how='left',
                on=['currency', 'substrategy', 'year'],
            )
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
            df_sorted = df.sort_values(
                [
                    'currency',
                    'unit_quantity',
                    '_sort_substrategy',
                    '_sort_year',
                ]
            )

            # Drop the temporary sorting columns
            df_sorted = df_sorted.drop(columns=['_sort_year', '_sort_substrategy'])
            return df_sorted

        grouped = custom_sort(grouped)
        return grouped
    except Exception as exc:
        _valuation_data_error = f'P&L query or aggregation failed: {type(exc).__name__}: {exc}'
        return pd.DataFrame()


# Dictionary to map DB column names to user-friendly display names
COLUMN_NAME_MAPPING = {
    'currency': 'Currency',
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
    'currency': 76,
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
        is_text_column = col in {
            'currency',
            'substrategy',
            'year',
            'unit_quantity',
        }

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
            'suppressMovable': col in {
                'currency',
                'substrategy',
                'year',
                'unit_quantity',
            },
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
        elif col in {'currency', 'year', 'unit_quantity'}:
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


def _format_valuation_display_value(key, value, currency=None):
    if pd.isna(value):
        return None
    if key in VALUATION_DECIMAL_COLUMNS:
        return f"{float(value):,.2f} {currency}" if currency else f"{float(value):,.2f}"
    if key in VALUATION_TOTAL_COLUMNS:
        return f"{float(value):,.2f} {currency}" if currency else f"{float(value):,.2f}"
    return value


def _raw_valuation_numeric_value(key, value):
    if key not in VALUATION_DECIMAL_COLUMNS | VALUATION_TOTAL_COLUMNS or pd.isna(value):
        return None
    return float(value)


def _clean_valuation_records(df):
    records = []
    for row in df.to_dict('records'):
        clean_row = {}
        currency = row.get('currency')
        for key, value in row.items():
            clean_row[key] = _format_valuation_display_value(
                key,
                value,
                currency,
            )
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


def _apply_excel_number_formats(worksheet, frame, formats):
    for column_index, column in enumerate(frame.columns, start=1):
        number_format = formats.get(column)
        if number_format is None:
            continue
        for row_index in range(2, len(frame) + 2):
            worksheet.cell(
                row=row_index,
                column=column_index,
            ).number_format = number_format


# ------------------------------------------------------------------
# 3) BUILD THE DASH LAYOUT
# ------------------------------------------------------------------
ERROR_STYLE_HIDDEN = {'display': 'none'}
ERROR_STYLE_VISIBLE = {'display': 'block'}

TTF_COMPARISON_COLUMN_DEFS = [
    {'headerName': 'Strategy', 'field': 'substrategy', 'pinned': 'left', 'minWidth': 210},
    {'headerName': 'Currency', 'field': 'currency', 'width': 88},
    {'headerName': 'Side', 'field': 'buy_sell', 'width': 76},
    {'headerName': 'Strike', 'field': 'strike', 'type': 'rightAligned', 'width': 84,
     'valueFormatter': {'function': "params.value == null ? '' : Number(params.value).toFixed(2)"}},
    {'headerName': 'ICAP Vol', 'field': 'volatility_used', 'type': 'rightAligned', 'width': 96,
     'valueFormatter': {'function': "params.value == null ? '' : (100 * Number(params.value)).toFixed(4) + '%'"}},
    {'headerName': 'ICE Delta', 'field': 'comparison_call_delta_used', 'type': 'rightAligned', 'width': 98,
     'valueFormatter': {'function': "params.value == null ? '' : (100 * Number(params.value)).toFixed(2) + '%'"}},
    {'headerName': 'ICE Vol', 'field': 'comparison_volatility_used', 'type': 'rightAligned', 'width': 94,
     'valueFormatter': {'function': "params.value == null ? '' : (100 * Number(params.value)).toFixed(4) + '%'"}},
    {'headerName': 'ICE − ICAP', 'field': 'vol_difference_pp', 'type': 'rightAligned', 'width': 104,
     'valueFormatter': {'function': "params.value == null ? '' : Number(params.value).toFixed(4) + ' pp'"}},
    {'headerName': 'ICAP Value', 'field': 'price', 'type': 'rightAligned', 'width': 104,
     'valueFormatter': {'function': "params.value == null ? '' : Number(params.value).toFixed(5) + ' ' + params.data.currency"}},
    {'headerName': 'ICE Value', 'field': 'comparison_price', 'type': 'rightAligned', 'width': 100,
     'valueFormatter': {'function': "params.value == null ? '' : Number(params.value).toFixed(5) + ' ' + params.data.currency"}},
    {'headerName': 'ICAP P&L', 'field': 'qty_pnl', 'type': 'rightAligned', 'width': 110,
     'valueFormatter': {'function': "params.value == null ? '' : d3.format(',.2f')(Number(params.value)) + ' ' + params.data.currency"}},
    {'headerName': 'ICE P&L', 'field': 'comparison_qty_pnl', 'type': 'rightAligned', 'width': 104,
     'valueFormatter': {'function': "params.value == null ? '' : d3.format(',.2f')(Number(params.value)) + ' ' + params.data.currency"}},
    {'headerName': 'Status', 'field': 'comparison_status', 'minWidth': 126},
    {'headerName': 'Revision', 'field': 'valuation_revision', 'width': 86},
]


def _build_ttf_comparison_records(frame):
    if frame is None or frame.empty:
        return []
    result = frame.copy()
    result['vol_difference_pp'] = 100 * (
        pd.to_numeric(result['comparison_volatility_used'], errors='coerce')
        - pd.to_numeric(result['volatility_used'], errors='coerce')
    )
    result['price_difference'] = (
        pd.to_numeric(result['comparison_price'], errors='coerce')
        - pd.to_numeric(result['price'], errors='coerce')
    )
    result['qty_pnl_difference'] = (
        pd.to_numeric(result['comparison_qty_pnl'], errors='coerce')
        - pd.to_numeric(result['qty_pnl'], errors='coerce')
    )
    records = []
    for row in result.to_dict('records'):
        clean = {}
        for key, value in row.items():
            if pd.isna(value):
                clean[key] = None
            elif isinstance(value, (pd.Timestamp, np.datetime64)):
                clean[key] = pd.Timestamp(value).isoformat()
            elif isinstance(value, np.generic):
                clean[key] = value.item()
            else:
                clean[key] = value
        records.append(clean)
    return records


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
        html.P(
            'All values use native contract currency. Subtotals and totals are '
            'calculated independently by currency; no FX conversion or mixed '
            'portfolio total is shown.',
            className='analytics-model-note',
        ),
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
        html.Div(
            [
                _build_valuation_section_header('TTF ICAP Official vs ICE Shadow'),
                html.Div(
                    id='ttf-valuation-comparison-status',
                    className='volatility-status-line volatility-status-neutral',
                ),
                dcc.Loading(
                    type='circle',
                    children=dag.AgGrid(
                        id='ttf-valuation-comparison-grid',
                        rowData=[],
                        columnDefs=TTF_COMPARISON_COLUMN_DEFS,
                        defaultColDef={
                            'sortable': True,
                            'filter': False,
                            'resizable': True,
                            'suppressHeaderMenuButton': True,
                        },
                        dashGridOptions={
                            'domLayout': 'autoHeight',
                            'rowHeight': 26,
                            'headerHeight': 32,
                            'pagination': False,
                            'enableCellTextSelection': True,
                        },
                        className='ag-theme-alpine mckinsey-ag-grid valuation-ag-grid',
                        style={'width': '100%', 'minHeight': '120px'},
                    ),
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
    dates = get_available_dates(get_database_engine(required=False))
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
    strategies = get_strategies(get_database_engine(required=False), selected_date)

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

    df = fetch_pnl_data(get_database_engine(required=False), selected_date, selected_strategies)
    if df.empty:
        error_message = _valuation_data_error or "No P&L rows are available for the selected date and strategies."
        return [], [], error_message, ERROR_STYLE_VISIBLE

    data_records = _clean_valuation_records(df)

    columns = build_valuation_column_defs(df)

    if _valuation_warning:
        return (
            data_records,
            columns,
            _valuation_warning,
            ERROR_STYLE_VISIBLE,
        )
    return data_records, columns, "", ERROR_STYLE_HIDDEN


@dash.callback(
    Output('ttf-valuation-comparison-grid', 'rowData'),
    Output('ttf-valuation-comparison-status', 'children'),
    Input('pnl-date-dropdown', 'value'),
    Input('pnl-strategy-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks'),
)
def update_ttf_valuation_comparison(
    selected_date,
    selected_strategies,
    n_clicks,
):
    del n_clicks
    if not selected_date:
        return [], 'Select a COB date to view published TTF source comparisons.'
    try:
        frame = fetch_ttf_source_comparison(
            get_database_engine(required=False),
            selected_date,
            selected_strategies,
        )
    except Exception as exc:
        return [], (
            'TTF comparison could not be loaded: '
            f'{type(exc).__name__}: {exc}'
        )
    if frame.empty:
        return [], 'No published ICE_TTF_TFO comparisons match the selection.'

    revision = int(frame['valuation_revision'].iloc[0])
    run_id = str(frame['valuation_run_id'].iloc[0])
    currencies = sorted(frame['currency'].dropna().astype(str).unique())
    if len(currencies) != 1:
        return [], 'TTF comparison has missing or mixed currency labels.'
    currency = currencies[0]
    official_pnl = float(
        pd.to_numeric(frame['qty_pnl'], errors='coerce').fillna(0).sum()
    )
    shadow_values = pd.to_numeric(
        frame['comparison_qty_pnl'],
        errors='coerce',
    )
    shadow_label = (
        f'{float(shadow_values.sum()):,.2f}'
        if shadow_values.notna().all()
        else 'diagnostic unavailable'
    )
    status = (
        f'Published revision {revision} · run {run_id} · '
        f'official ICAP P&L {official_pnl:,.2f} {currency} · '
        f'ICE shadow P&L {shadow_label} {currency}'
    )
    return _build_ttf_comparison_records(frame), status


# 4.3) Export callback for P&L table
@dash.callback(
    Output("download-pnl-table", "data"),
    Input("export-pnl-table-btn", "n_clicks"),
    [State('pnl-date-dropdown', 'value'),
     State('pnl-table', 'rowData'),
     State('pnl-table', 'columnDefs'),
     State('ttf-valuation-comparison-grid', 'rowData')],
    prevent_initial_call=True
)
def export_pnl_table(
    n_clicks,
    selected_date,
    row_data,
    column_defs,
    comparison_rows,
):
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

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df_renamed.to_excel(
                writer,
                sheet_name='P&L and Option Values',
                index=False,
            )
            _apply_excel_number_formats(
                writer.sheets['P&L and Option Values'],
                df_renamed,
                {
                    COLUMN_NAME_MAPPING.get(column, column): '#,##0.00'
                    for column in (
                        VALUATION_DECIMAL_COLUMNS
                        | VALUATION_TOTAL_COLUMNS
                    )
                },
            )
            if comparison_rows:
                comparison_export = pd.DataFrame(comparison_rows)
                comparison_export.to_excel(
                    writer,
                    sheet_name='TTF ICAP vs ICE',
                    index=False,
                )
                _apply_excel_number_formats(
                    writer.sheets['TTF ICAP vs ICE'],
                    comparison_export,
                    {
                        'qty_pnl': '#,##0.00',
                        'comparison_qty_pnl': '#,##0.00',
                        'qty_pnl_difference': '#,##0.00',
                    },
                )
        output.seek(0)
        return dcc.send_bytes(output.getvalue(), filename)

    except Exception:
        raise dash.exceptions.PreventUpdate
