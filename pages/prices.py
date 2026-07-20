import dash
import dash_ag_grid as dag
from dash import html, dcc, Input, Output, State
import json
import threading
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import datetime
from sqlalchemy import text

from db_fallback import DB_SCHEMA, fq_table, read_with_fallback, sql_literal


ENVERUS_UNDERLYING_SOURCES = {
    'HH': {'code': 'ICE_HH', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'NBP': {'code': 'ICE_UKD', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'TFM': {'code': 'ICE_TTF', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'TFU': {'code': 'ICE_TFU_MO', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'Brent': {'code': 'ICE_BRENT_FUTURES', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'JKM': {'code': 'ICE_JKM_MO', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
}
ENVERUS_CODE_TO_PRODUCT = {
    source['code']: product
    for product, source in ENVERUS_UNDERLYING_SOURCES.items()
}


def _sql_in_literal(values):
    return ', '.join(sql_literal(value) for value in values)


def _normalize_enverus_prices(df):
    if df.empty:
        return pd.DataFrame(
            columns=[
                'trade_date',
                'hub',
                'product',
                'maturity_date',
                'expiration_date',
                'contract',
                'contract_type',
                'settlement_price',
                'code',
            ]
        )

    normalized = df.copy()
    normalized['trade_date'] = pd.to_datetime(normalized['COB'], errors='coerce')
    normalized['maturity_date'] = np.where(
        normalized['contract'].eq('SPOT'),
        normalized['trade_date'],
        pd.to_datetime(normalized['contract'], format='%YM%m', errors='coerce'),
    )
    normalized['expiration_date'] = pd.to_datetime(normalized['expiry'], errors='coerce')
    normalized['settlement_price'] = pd.to_numeric(normalized['value'], errors='coerce')
    normalized['product'] = normalized['code'].map(ENVERUS_CODE_TO_PRODUCT).fillna(normalized['code'])
    normalized['contract_type'] = None
    normalized['hub'] = None
    normalized['contract'] = normalized['product']

    normalized = normalized.dropna(subset=['trade_date', 'maturity_date', 'settlement_price'])
    return normalized[
        [
            'trade_date',
            'hub',
            'product',
            'maturity_date',
            'expiration_date',
            'contract',
            'contract_type',
            'settlement_price',
            'code',
        ]
    ].reset_index(drop=True)


def get_enverus_underlying_prices(from_COB, to_COB):
    """Load only the five most recent curve COBs in the requested window."""
    postgres_from_cob = datetime.datetime.strptime(str(from_COB), "%Y%m%d").date()
    postgres_to_cob = datetime.datetime.strptime(str(to_COB), "%Y%m%d").date()
    codes = [source['code'] for source in ENVERUS_UNDERLYING_SOURCES.values()]
    categories = sorted({source['category'] for source in ENVERUS_UNDERLYING_SOURCES.values()})
    versions = sorted({source['version_name'] for source in ENVERUS_UNDERLYING_SOURCES.values()})

    trino_query = '''WITH selected_dates AS (
                        SELECT DISTINCT ondate_index
                        FROM enverus.curve
                        WHERE code IN ({})
                            AND category IN ({})
                            AND version_name IN ({})
                            AND ondate_index >= {}
                            AND ondate_index <= {}
                        ORDER BY ondate_index DESC
                        LIMIT 5
                    )
                    SELECT   code,
                        ondate AS COB,
                        currency,
                        units,
                        forward_curve_tenors_expiry AS expiry,
                        forward_curve_tenors_absolute AS contract,
                        forward_curve_tenors_value AS value
                        FROM enverus.curve
                        WHERE code IN ({})
                            AND category IN ({})
                            AND version_name IN ({})
                            AND ondate_index >= {}
                            AND ondate_index <= {}
                            AND ondate_index IN (SELECT ondate_index FROM selected_dates)
                            AND forward_curve_tenors_absolute NOT IN ('M-1','M-2','M-3')
                            AND forward_curve_tenors_value is not null
                        ORDER BY ondate, forward_curve_tenors_tenor
                            '''.format(
                                _sql_in_literal(codes),
                                _sql_in_literal(categories),
                                _sql_in_literal(versions),
                                int(from_COB),
                                int(to_COB),
                                _sql_in_literal(codes),
                                _sql_in_literal(categories),
                                _sql_in_literal(versions),
                                int(from_COB),
                                int(to_COB),
                            )
    postgres_query = text(
        f'''
        WITH selected_dates AS (
            SELECT DISTINCT cob
            FROM {fq_table(DB_SCHEMA, 'curve')}
            WHERE code = ANY(:codes)
              AND cob >= :from_cob
              AND cob <= :to_cob
            ORDER BY cob DESC
            LIMIT 5
        )
        SELECT  code,
                cob AS "COB",
                currency,
                units,
                expiry,
                contract,
                value::double precision AS value
        FROM {fq_table(DB_SCHEMA, 'curve')}
        WHERE code = ANY(:codes)
          AND cob >= :from_cob
          AND cob <= :to_cob
          AND cob IN (SELECT cob FROM selected_dates)
          AND contract NOT IN ('M-1','M-2','M-3')
          AND value IS NOT NULL
        ORDER BY cob, expiry
        '''
    )
    df_enverus = read_with_fallback(
        trino_query,
        postgres_query,
        catalog='transformed',
        schema='enverus',
        postgres_params={
            'codes': codes,
            'from_cob': postgres_from_cob,
            'to_cob': postgres_to_cob,
        },
        context_label='Underlying prices Enverus load',
    )

    return _normalize_enverus_prices(df_enverus)


df_options = _normalize_enverus_prices(pd.DataFrame())
_prices_data_loaded = False
_prices_data_refresh_key = None
_prices_data_lock = threading.Lock()


def _ensure_prices_data(force=False, refresh_key=None):
    global df_options, _prices_data_loaded, _prices_data_refresh_key
    with _prices_data_lock:
        should_reload = not _prices_data_loaded
        if force:
            should_reload = refresh_key is None or refresh_key != _prices_data_refresh_key

        if should_reload:
            try:
                # Get recent dates for current underlying price monitoring.
                end_date = datetime.datetime.now()
                start_date = end_date - datetime.timedelta(days=30)
                df_options = get_enverus_underlying_prices(
                    from_COB=start_date.strftime("%Y%m%d"),
                    to_COB=end_date.strftime("%Y%m%d"),
                )
            except Exception:
                df_options = _normalize_enverus_prices(pd.DataFrame())
            _prices_data_loaded = True
            if force:
                _prices_data_refresh_key = refresh_key
    return df_options


PRICE_GROUPING_OPTIONS = [
    {'label': 'Monthly', 'value': 'monthly'},
    {'label': 'Quarterly', 'value': 'quarterly'},
    {'label': 'Season', 'value': 'season'},
    {'label': 'Calendar', 'value': 'calendar'},
]

PRICES_CHART_FONT = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
PRICES_CHART_TEXT = '#0f172a'
PRICES_CHART_MUTED = '#64748b'
PRICES_CHART_GRID = 'rgba(148, 163, 184, 0.18)'
PRICES_CHART_AXIS = '#94a3b8'
PRICES_GRAPH_CONFIG = {
    'displayModeBar': 'hover',
    'displaylogo': False,
    'responsive': True,
    'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
}


def _price_axis(title='', tickformat=None, **overrides):
    axis = {
        'title': dict(text=title, font=dict(size=11, color=PRICES_CHART_MUTED)),
        'showgrid': True,
        'gridcolor': PRICES_CHART_GRID,
        'gridwidth': 1,
        'zeroline': False,
        'linecolor': PRICES_CHART_AXIS,
        'linewidth': 1,
        'tickfont': dict(size=10, color=PRICES_CHART_MUTED),
        'ticks': 'outside',
        'ticklen': 3,
        'automargin': True,
    }
    if tickformat:
        axis['tickformat'] = tickformat
    axis.update(overrides)
    return axis


def _price_legend():
    return {
        'orientation': 'h',
        'yanchor': 'top',
        'y': -0.18,
        'xanchor': 'center',
        'x': 0.5,
        'bgcolor': 'rgba(255, 255, 255, 0)',
        'bordercolor': 'rgba(255, 255, 255, 0)',
        'font': dict(size=9, color=PRICES_CHART_MUTED),
        'itemsizing': 'constant',
        'itemwidth': 44,
        'tracegroupgap': 4,
    }


def _apply_price_chart_theme(fig, margin=None, height=None):
    fig.update_layout(
        title=dict(text=''),
        font=dict(family=PRICES_CHART_FONT, size=11, color=PRICES_CHART_TEXT),
        plot_bgcolor='#f8fafc',
        paper_bgcolor='white',
        margin=margin or dict(l=48, r=18, t=18, b=88),
        hovermode='x unified',
        hoverlabel=dict(
            bgcolor='rgba(255, 255, 255, 0.96)',
            bordercolor='rgba(148, 163, 184, 0.45)',
            font=dict(size=11, color=PRICES_CHART_TEXT, family=PRICES_CHART_FONT),
            align='left',
        ),
        legend=_price_legend(),
        showlegend=True,
        transition=dict(duration=180, easing='cubic-in-out'),
    )
    if height is not None:
        fig.update_layout(height=height)
    return fig


def _build_prices_section_header(title, actions=None):
    return html.Div(
        [
            html.Div(
                [html.H3(title, className='section-title-inline prices-section-title')],
                className='prices-section-title-row',
            ),
            html.Div(actions or [], className='prices-section-actions'),
        ],
        className='prices-section-header',
    )


def _build_prices_chip(label, value=None, tone='neutral'):
    if value is None or value == '':
        return None
    return html.Span(
        [
            html.Span(label, className='prices-chip-label'),
            html.Span(str(value), className='prices-chip-value'),
        ],
        className=f'prices-chip prices-chip-{tone}',
    )


def _build_prices_chart_header(title, chips=None):
    chip_nodes = [chip for chip in (chips or []) if chip is not None]
    return html.Div(
        [
            html.Div(
                [html.H5(title, className='prices-chart-card-title')],
                className='prices-chart-card-title-group',
            ),
            html.Div(chip_nodes, className='prices-chart-card-chips'),
        ],
        className='prices-chart-card-header',
    )


def _build_prices_chart_card(graph, title, chips=None, className=None):
    classes = ['prices-chart-card']
    if className:
        classes.append(className)
    return html.Div(
        [_build_prices_chart_header(title, chips=chips), graph],
        className=' '.join(classes),
    )


def _build_prices_message(message, tone='neutral'):
    return html.Div(message, className=f'prices-empty-state prices-empty-state-{tone}')


def _price_period_label(date_value, grouping_mode):
    date_value = pd.Timestamp(date_value)
    if grouping_mode == 'monthly':
        return date_value.strftime("%b'%y")
    if grouping_mode == 'quarterly':
        return f"Q{date_value.quarter}'{str(date_value.year)[-2:]}"
    if grouping_mode == 'season':
        season = 'Sum' if 5 <= date_value.month <= 9 else 'Win'
        return f"{season}'{str(date_value.year)[-2:]}"
    if grouping_mode == 'calendar':
        return str(date_value.year)
    return date_value.strftime("%b'%y")


def _price_period_sort_key(label, grouping_mode):
    label = str(label)
    try:
        if grouping_mode == 'monthly':
            parsed = pd.to_datetime(label, format="%b'%y")
            return (parsed.year, parsed.month, 0)
        if grouping_mode == 'quarterly':
            quarter_part, year_part = label.split("'")
            return (2000 + int(year_part), int(quarter_part.replace('Q', '')), 0)
        if grouping_mode == 'season':
            season_part, year_part = label.split("'")
            season_order = {'Win': 0, 'Sum': 1}.get(season_part, 9)
            return (2000 + int(year_part), season_order, 0)
        if grouping_mode == 'calendar':
            return (int(label), 0, 0)
    except Exception:
        pass
    return (9999, 99, label)


def _sort_price_period_columns(columns, grouping_mode):
    return sorted(columns, key=lambda col: _price_period_sort_key(col, grouping_mode))


def _clean_price_grid_records(df, numeric_fields):
    if df is None or df.empty:
        return []

    numeric_fields = set(numeric_fields or [])
    records = []
    for row in df.to_dict('records'):
        clean_row = {}
        for key, value in row.items():
            if pd.isna(value):
                clean_row[key] = None
            elif key in numeric_fields:
                numeric_value = float(value)
                clean_row[key] = f"{numeric_value:,.2f}"
                clean_row[f'__{key}_raw'] = numeric_value
            else:
                clean_row[key] = value
        records.append(clean_row)
    return records


def _price_raw_number_expression(field):
    raw_key = json.dumps(f'__{field}_raw')
    return f"Number(params.data && params.data[{raw_key}])"


def _build_price_column_defs(columns, numeric_fields=None, sign_fields=None):
    numeric_fields = set(numeric_fields or [])
    sign_fields = set(sign_fields or [])
    column_defs = []

    for col in columns:
        field = col['id']
        header = col['name']
        is_numeric = field in numeric_fields or col.get('type') == 'numeric'
        width = 94 if field == 'product' else max(72, min(122, 28 + len(str(header)) * 7))
        column_def = {
            'headerName': header,
            'field': field,
            'sortable': True,
            'filter': False,
            'resizable': True,
            'width': width,
            'minWidth': 78 if field == 'product' else 64,
            'maxWidth': 150 if field == 'product' else 150,
            'tooltipField': field,
            'headerTooltip': header,
            'cellClass': 'prices-table-text-cell',
            'headerClass': 'prices-table-text-header',
        }

        if field == 'product':
            column_def.update({'pinned': 'left', 'lockPinned': True})

        if is_numeric:
            raw_value = _price_raw_number_expression(field)
            class_rules = {
                'prices-negative-cell': f"{raw_value} < 0",
                'prices-missing-cell': (
                    f"params.data === null || params.data === undefined "
                    f"|| params.data[{json.dumps(f'__{field}_raw')}] === null "
                    f"|| params.data[{json.dumps(f'__{field}_raw')}] === undefined "
                    f"|| isNaN(Number(params.data[{json.dumps(f'__{field}_raw')}]))"
                ),
            }
            if field in sign_fields:
                class_rules['prices-positive-cell'] = f"{raw_value} > 0"

            column_def.update({
                'type': 'rightAligned',
                'cellClass': 'prices-table-number-cell',
                'headerClass': 'prices-table-number-header',
                'cellClassRules': class_rules,
            })
        column_defs.append(column_def)

    return column_defs


def _build_price_grid(grid_id, columns, data, numeric_fields=None, sign_fields=None, className=None):
    classes = ['ag-theme-alpine', 'mckinsey-ag-grid', 'prices-data-grid']
    if className:
        classes.append(className)
    return dag.AgGrid(
        id=grid_id,
        rowData=data,
        columnDefs=_build_price_column_defs(columns, numeric_fields=numeric_fields, sign_fields=sign_fields),
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
            'domLayout': 'autoHeight',
            'rowHeight': 30,
            'headerHeight': 34,
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
        className=' '.join(classes),
        style={'width': '100%'},
        dangerously_allow_code=True,
    )


def _build_price_table_panel(title, grid, chips=None, className=None):
    classes = ['prices-table-panel']
    if className:
        classes.append(className)
    return html.Div(
        [
            html.Div(
                [
                    html.H4(title, className='prices-table-panel-title'),
                    html.Div([chip for chip in (chips or []) if chip is not None], className='prices-table-panel-chips'),
                ],
                className='prices-table-panel-header',
            ),
            grid,
        ],
        className=' '.join(classes),
    )


def _build_prices_filter_bar():
    return html.Div(
        [
            html.Div(
                [
                    html.Span('Products', className='filter-group-header'),
                    dcc.Dropdown(
                        id='prices-unified-product-dropdown',
                        options=[],
                        value=[],
                        multi=True,
                        placeholder='Select products...',
                        className='prices-filter-dropdown prices-product-dropdown',
                    ),
                ],
                className='filter-group prices-sticky-filter-group prices-products-group',
            ),
            html.Div(
                [
                    html.Span('Current', className='filter-group-header'),
                    html.Div(
                        dcc.DatePickerSingle(
                            id='prices-unified-date-picker',
                            display_format='YYYY-MM-DD',
                            with_portal=True,
                            className='prices-date-picker',
                        ),
                        className='prices-date-control',
                    ),
                ],
                className='filter-group prices-sticky-filter-group prices-date-group',
            ),
            html.Div(
                [
                    html.Span('Previous', className='filter-group-header'),
                    html.Div(
                        dcc.DatePickerSingle(
                            id='prices-table-prev-date-picker',
                            display_format='YYYY-MM-DD',
                            with_portal=True,
                            className='prices-date-picker',
                        ),
                        className='prices-date-control',
                    ),
                ],
                className='filter-group prices-sticky-filter-group prices-date-group',
            ),
            html.Div(
                [
                    html.Span('Group By', className='filter-group-header'),
                    dcc.RadioItems(
                        id='prices-unified-grouping-dropdown',
                        options=PRICE_GROUPING_OPTIONS,
                        value='monthly',
                        inline=True,
                        className='prices-segmented-selector prices-grouping-selector',
                        inputStyle={'display': 'none'},
                        labelStyle={'marginRight': '0'},
                    ),
                ],
                className='filter-group prices-sticky-filter-group prices-grouping-group',
            ),
        ],
        className='professional-section-header prices-sticky-filter-bar',
    )


layout = html.Div(
    [
        dcc.Download(id='download-prices-table'),

        _build_prices_filter_bar(),

        html.Div(
            [
                _build_prices_section_header('Price Charts'),
                html.Div(id='prices-graphs-container', className='prices-section-body prices-chart-body'),
            ],
            className='prices-section prices-chart-section',
        ),
        html.Div(
            [
                _build_prices_section_header(
                    'Current Prices',
                    actions=[
                        html.Button(
                            'Export',
                            id='export-prices-table-btn',
                            className='custom-export-btn prices-export-button',
                        )
                    ],
                ),
                html.Div(id='prices-tables-container', className='prices-section-body prices-table-body'),
            ],
            className='prices-section prices-data-section',
        ),
    ],
    className='options-dashboard-container prices-page',
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
        price_data = _ensure_prices_data(force=bool(n_clicks), refresh_key=n_clicks)

        # Check if df_options exists and is accessible
        if price_data is None or not isinstance(price_data, pd.DataFrame):
            return default_date, default_prev_date

        # Check if required columns exist
        if 'trade_date' in price_data.columns:
            # Convert date column safely
            temp_df = price_data.copy()
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

    except Exception:
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
    except Exception:
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
        price_data = _ensure_prices_data(force=bool(n_clicks), refresh_key=n_clicks)

        # Check if df_options exists and is accessible
        if price_data is None or not isinstance(price_data, pd.DataFrame):
            return [], []

        # Check if required columns exist
        if 'contract' not in price_data.columns:
            return [], []

        # Get unique, non-null products
        product_codes = price_data['contract'].dropna().unique()

        if len(product_codes) == 0:
            return [], []

        # Sort product codes
        product_codes = sorted(product_codes)

        dropdown_options = [{'label': code, 'value': code} for code in product_codes]
        return dropdown_options, product_codes

    except Exception:
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
        price_data = _ensure_prices_data(force=bool(n_clicks), refresh_key=n_clicks)

        # Check if df_options exists and is accessible
        if price_data is None or not isinstance(price_data, pd.DataFrame):
            raise dash.exceptions.PreventUpdate

        # Convert current date
        current_date_dt = pd.to_datetime(current_date)

        # Convert dataframe date column
        price_data = price_data.copy()
        price_data['trade_date'] = pd.to_datetime(price_data['trade_date'], errors='coerce')

        # Drop NaT values and get unique dates
        valid_dates = price_data['trade_date'].dropna().unique()

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

    except Exception:
        raise dash.exceptions.PreventUpdate

# ============== CONTENT UPDATE CALLBACKS =================

# Update graphs callback
@dash.callback(
    Output('prices-graphs-container', 'children'),
    [Input('prices-unified-date-picker', 'date'),
     Input('prices-unified-grouping-dropdown', 'value'),
     Input('prices-unified-product-dropdown', 'value')],
    prevent_initial_call=True
)


def update_graphs(selected_date, grouping_mode, selected_products):
    try:
        if selected_date is None or not selected_products:
            return html.Div()

        # Convert the selected date
        selected_date = pd.to_datetime(selected_date)

        df, data_error = _prepare_price_dataset(selected_products)
        if data_error:
            tone = 'danger' if data_error.startswith(('DataFrame', 'Missing')) else 'warning'
            message = 'No data available for selected products.' if data_error == 'No data for selected products.' else data_error
            return _build_prices_message(message, tone=tone)

        cards = []
        product_codes = sorted(df['contract'].dropna().unique())

        if not product_codes:
            return _build_prices_message('No products found in data.', tone='warning')

        palette = ['#2E86C1', '#64748b', '#94a3b8', '#7c8da1', '#a8b3c4']

        def prepare_curve(cob_data):
            if grouping_mode != 'monthly':
                cob_data = group_data_by_period(cob_data, grouping_mode)
                if 'period' in cob_data.columns:
                    period_cols = _sort_price_period_columns(cob_data['period'].dropna().unique().tolist(), grouping_mode)
                    period_order = {period: idx for idx, period in enumerate(period_cols)}
                    cob_data = cob_data.assign(_period_order=cob_data['period'].map(period_order).fillna(9999))
                    cob_data = cob_data.sort_values('_period_order')
                    return cob_data, cob_data['period'], 'settlement_price'

            cob_data = cob_data.sort_values('maturity_date')
            return cob_data, cob_data['maturity_date'], 'settlement_price'

        for i, code in enumerate(product_codes):
            try:
                fig = go.Figure()
                code_df = df[df['contract'] == code]

                if code_df.empty:
                    continue

                # Get the 5 most recent dates safely
                recent_dates = [
                    pd.Timestamp(date_value)
                    for date_value in code_df['trade_date'].drop_duplicates().sort_values(ascending=False).head(5).values
                ]
                selected_date_available = False

                for trace_index, cob_date in enumerate(recent_dates):
                    cob_date = pd.Timestamp(cob_date)
                    cob_data = code_df[code_df['trade_date'] == cob_date]

                    if cob_data.empty:
                        continue

                    try:
                        cob_data, x_values, y_column = prepare_curve(cob_data)
                    except Exception:
                        cob_data = cob_data.sort_values('maturity_date')
                        x_values = cob_data['maturity_date']
                        y_column = 'settlement_price'

                    is_selected_date = cob_date.date() == selected_date.date()
                    if is_selected_date:
                        selected_date_available = True

                    line_style = (
                        dict(width=2.4, color='#2E86C1')
                        if is_selected_date
                        else dict(width=1.25, color=palette[min(trace_index, len(palette) - 1)], dash='dot')
                    )
                    marker_style = dict(size=4.5 if is_selected_date else 3.5)
                    legend_name = f"{cob_date.strftime('%Y-%m-%d')}"
                    if is_selected_date:
                        legend_name = f"{legend_name} Current"

                    # Only add trace if we have data
                    if not cob_data.empty and len(cob_data) > 0:
                        fig.add_trace(go.Scatter(
                            x=x_values,
                            y=cob_data[y_column],
                            mode='lines+markers',
                            name=legend_name,
                            line=line_style,
                            marker=marker_style,
                        ))

                if not selected_date_available and not code_df.empty:
                    try:
                        closest_date = find_closest_date(code_df, 'trade_date', selected_date)
                        recent_normalized = {pd.Timestamp(date_value).normalize() for date_value in recent_dates}

                        if closest_date is not None and pd.Timestamp(closest_date).normalize() not in recent_normalized:
                            closest_date = pd.Timestamp(closest_date)
                            cob_data = code_df[code_df['trade_date'] == closest_date]

                            if not cob_data.empty:
                                try:
                                    cob_data, x_values, y_column = prepare_curve(cob_data)
                                except Exception:
                                    cob_data = cob_data.sort_values('maturity_date')
                                    x_values = cob_data['maturity_date']
                                    y_column = 'settlement_price'

                                fig.add_trace(go.Scatter(
                                    x=x_values,
                                    y=cob_data[y_column],
                                    mode='lines+markers',
                                    name=f"{closest_date.strftime('%Y-%m-%d')} Closest",
                                    line=dict(width=1.4, color='#f59e0b', dash='dot'),
                                    marker=dict(size=4),
                                ))
                    except Exception:
                        pass

                # Determine x-axis title based on grouping mode
                x_axis_title = "Maturity"
                if grouping_mode == 'quarterly':
                    x_axis_title = "Quarter"
                elif grouping_mode == 'season':
                    x_axis_title = "Season"
                elif grouping_mode == 'calendar':
                    x_axis_title = "Year"
                elif grouping_mode == 'monthly':
                    x_axis_title = "Month"

                xaxis_kwargs = {}
                if grouping_mode != 'monthly':
                    xaxis_kwargs['type'] = 'category'
                xaxis = _price_axis(
                    x_axis_title,
                    tickformat="%b'%y" if grouping_mode == 'monthly' else None,
                    **xaxis_kwargs,
                )
                fig.update_layout(
                    xaxis=xaxis,
                    yaxis=_price_axis('Settlement Price', zeroline=True, zerolinecolor='rgba(148, 163, 184, 0.38)'),
                )
                _apply_price_chart_theme(fig, height=316)

                graph = dcc.Graph(
                    figure=fig,
                    config=PRICES_GRAPH_CONFIG,
                    className='prices-chart-graph',
                    style={'height': '100%'},
                )
                cards.append(
                    _build_prices_chart_card(
                        graph,
                        f'{code} Forward Curve',
                        chips=[
                            _build_prices_chip('COB', selected_date.strftime('%Y-%m-%d'), tone='primary'),
                            _build_prices_chip('Group', grouping_mode.capitalize()),
                            _build_prices_chip('Curves', len(fig.data)),
                        ],
                        className='prices-main-chart-card',
                    )
                )

            except Exception:
                continue

        return html.Div(cards, className='prices-chart-grid') if cards else _build_prices_message(
            'No graphs could be generated.',
            tone='warning',
        )

    except Exception as e:
        return _build_prices_message(f"Error generating graphs: {str(e)}", tone='danger')


def group_data_by_period(data, grouping_mode):
    """Group price data by different time periods."""
    try:
        data = data.copy()
        data['maturity_date'] = pd.to_datetime(data['maturity_date'], errors='coerce')
        data = data.dropna(subset=['maturity_date'])

        if grouping_mode == 'monthly':
            data['period'] = data['maturity_date'].apply(lambda date_value: _price_period_label(date_value, 'monthly'))
            return data

        elif grouping_mode == 'quarterly':
            data['period'] = data['maturity_date'].apply(lambda date_value: _price_period_label(date_value, 'quarterly'))

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        elif grouping_mode == 'season':
            data['period'] = data['maturity_date'].apply(lambda date_value: _price_period_label(date_value, 'season'))
            data = data.dropna(subset=['period'])

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        elif grouping_mode == 'calendar':
            data['period'] = data['maturity_date'].apply(lambda date_value: _price_period_label(date_value, 'calendar'))

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        data['period'] = data['maturity_date'].apply(lambda date_value: _price_period_label(date_value, 'monthly'))
        return data

    except Exception:
        # Return original data in case of error
        if 'period' not in data.columns:
            data['period'] = 'unknown'
        return data



def _prepare_price_dataset(selected_products):
    price_data = _ensure_prices_data()

    if price_data is None or not isinstance(price_data, pd.DataFrame):
        return None, 'DataFrame not available.'

    required_columns = ['trade_date', 'contract', 'maturity_date', 'settlement_price']
    missing_columns = [col for col in required_columns if col not in price_data.columns]
    if missing_columns:
        return None, f"Missing columns: {', '.join(missing_columns)}."

    df = price_data.copy()
    df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
    df['maturity_date'] = pd.to_datetime(df['maturity_date'], errors='coerce')
    df['settlement_price'] = pd.to_numeric(df['settlement_price'], errors='coerce')
    df = df.dropna(subset=['trade_date', 'maturity_date', 'settlement_price'])

    if df.empty:
        return None, 'No valid data available after cleaning.'

    product_df = df[df['contract'].isin(selected_products)]
    if product_df.empty:
        return None, 'No data for selected products.'

    return product_df, None


def _build_current_price_pivot(product_df, selected_date, grouping_mode):
    selected_date = pd.to_datetime(selected_date)
    current_data = product_df[product_df['trade_date'].dt.normalize() == selected_date.normalize()]
    if current_data.empty:
        return pd.DataFrame(), [], current_data

    current_data = group_data_by_period(current_data, grouping_mode)
    current_data['product'] = current_data['contract']
    current_pivot = current_data.pivot_table(
        values='settlement_price',
        index=['product'],
        columns=['period'],
        aggfunc='first'
    ).reset_index()
    period_cols = [col for col in current_pivot.columns if col != 'product']
    sorted_period_cols = _sort_price_period_columns(period_cols, grouping_mode)
    if sorted_period_cols:
        current_pivot = current_pivot[['product'] + sorted_period_cols]
    return current_pivot, sorted_period_cols, current_data


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

        product_df, data_error = _prepare_price_dataset(selected_products)
        if data_error:
            tone = 'danger' if data_error.startswith(('DataFrame', 'Missing')) else 'warning'
            return _build_prices_message(data_error, tone=tone)

        selected_date = pd.to_datetime(selected_date)
        prev_date = pd.to_datetime(prev_selected_date) if prev_selected_date else None

        # Get previous data if available
        prev_data = pd.DataFrame()
        if prev_date is not None:
            prev_data = product_df[product_df['trade_date'].dt.normalize() == prev_date.normalize()]

        try:
            current_pivot, sorted_date_cols, current_data = _build_current_price_pivot(
                product_df,
                selected_date,
                grouping_mode,
            )
        except Exception as e:
            return _build_prices_message(f"Error grouping data: {str(e)}", tone='danger')

        if current_data.empty:
            return _build_prices_message(
                f"No data found for selected date: {selected_date.strftime('%Y-%m-%d')}.",
                tone='warning',
            )

        if not sorted_date_cols:
            return _build_prices_message('No period data available for selected parameters.', tone='warning')

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

                change_cols = [col for col in sorted_date_cols if col in current_pivot.columns and col in prev_pivot.columns]
                if change_cols:
                    current_values = current_pivot.set_index('product').reindex(all_products)[change_cols]
                    prev_values = prev_pivot.set_index('product').reindex(all_products)[change_cols]
                    current_values = current_values.apply(pd.to_numeric, errors='coerce')
                    prev_values = prev_values.apply(pd.to_numeric, errors='coerce')
                    changes_pivot = (current_values - prev_values).reset_index()
            except Exception:
                # Continue without changes table if there's an error
                changes_pivot = pd.DataFrame()

        columns = [{'name': 'Product', 'id': 'product'}]
        for col in sorted_date_cols:
            columns.append({'name': col, 'id': col, 'type': 'numeric'})

        product_count = len(current_pivot['product'].dropna().unique()) if 'product' in current_pivot.columns else 0
        current_grid = _build_price_grid(
            'prices-volatility-table',
            columns,
            _clean_price_grid_records(current_pivot, sorted_date_cols),
            numeric_fields=sorted_date_cols,
            sign_fields=[],
            className='prices-current-grid',
        )
        panels = [
            _build_price_table_panel(
                'Current Forward Prices',
                current_grid,
                chips=[
                    _build_prices_chip('COB', selected_date.strftime('%Y-%m-%d'), tone='primary'),
                    _build_prices_chip('Group', grouping_mode.capitalize()),
                    _build_prices_chip('Products', product_count),
                ],
            )
        ]

        if not changes_pivot.empty:
            change_cols = [col for col in sorted_date_cols if col in changes_pivot.columns]
            changes_grid = _build_price_grid(
                'prices-changes-table',
                columns,
                _clean_price_grid_records(changes_pivot, change_cols),
                numeric_fields=sorted_date_cols,
                sign_fields=change_cols,
                className='prices-change-grid',
            )
            panels.append(
                _build_price_table_panel(
                    'Change vs Previous COB',
                    changes_grid,
                    chips=[
                        _build_prices_chip('Current', selected_date.strftime('%Y-%m-%d'), tone='primary'),
                        _build_prices_chip('Previous', prev_date.strftime('%Y-%m-%d') if prev_date is not None else None),
                    ],
                )
            )

        return html.Div(panels, className='prices-table-stack')

    except Exception as e:
        return _build_prices_message(f"Error generating tables: {str(e)}", tone='danger')


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
        product_df, data_error = _prepare_price_dataset(selected_products)
        if data_error:
            raise dash.exceptions.PreventUpdate

        selected_date = pd.to_datetime(selected_date)
        current_pivot, sorted_period_cols, current_data = _build_current_price_pivot(
            product_df,
            selected_date,
            grouping_mode,
        )
        if current_data.empty:
            raise dash.exceptions.PreventUpdate

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
        
    except Exception:
        raise dash.exceptions.PreventUpdate


@dash.callback(
    Output('prices-unified-grouping-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=True
)
def reset_unified_grouping(n_clicks):
    # This function will be triggered by the main refresh button
    return 'monthly'
