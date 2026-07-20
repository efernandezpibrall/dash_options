import dash
import dash_ag_grid as dag
from dash import html, dcc, Input, Output, State
import datetime
import io
import threading
import pandas as pd
import plotly.graph_objects as go
from sqlalchemy import text

from db_fallback import DB_SCHEMA, fq_table, read_with_fallback, sql_literal


_SLOPES_DATA_LOCK = threading.Lock()
_slopes_data_cache = {}
_slopes_refresh_generation = 0

ENVERUS_SLOPE_SOURCES = {
    'JKM': {'code': 'ICE_JKM_MO', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
    'Brent': {'code': 'ICE_BRENT_FUTURES', 'category': 'FINANCIAL', 'version_name': 'FINAL'},
}
ENVERUS_CODE_TO_SLOPE_PRODUCT = {
    source['code']: product
    for product, source in ENVERUS_SLOPE_SOURCES.items()
}


def _sql_in_literal(values):
    return ', '.join(sql_literal(value) for value in values)


def _delivery_month_from_enverus_contract(series):
    """Parse Enverus absolute tenor strings like 2026M08 into delivery months."""
    contract_text = series.astype(str).str.strip()
    maturity_date = pd.to_datetime(contract_text, format='%YM%m', errors='coerce')
    spot_mask = contract_text.eq('SPOT')
    if spot_mask.any():
        maturity_date.loc[spot_mask] = pd.NaT
    return maturity_date


def _normalize_enverus_slopes(df):
    columns = [
        'trade_date',
        'hub',
        'product',
        'strip',
        'maturity_date',
        'expiration_date',
        'contract',
        'contract_type',
        'settlement_price',
        'code',
    ]
    if df.empty:
        return pd.DataFrame(columns=columns)

    normalized = df.copy()
    normalized['trade_date'] = pd.to_datetime(normalized['COB'], errors='coerce')
    normalized['maturity_date'] = _delivery_month_from_enverus_contract(normalized['contract'])
    normalized['expiration_date'] = pd.to_datetime(normalized['expiry'], errors='coerce')
    normalized['settlement_price'] = pd.to_numeric(normalized['value'], errors='coerce')
    normalized['product'] = normalized['code'].map(ENVERUS_CODE_TO_SLOPE_PRODUCT).fillna(normalized['code'])
    normalized['hub'] = normalized['product']
    normalized['strip'] = normalized['maturity_date'].dt.strftime('%b-%y')
    normalized['contract_type'] = None
    normalized['contract'] = normalized['product']

    normalized = normalized.dropna(subset=['trade_date', 'maturity_date', 'settlement_price'])
    return normalized[columns].reset_index(drop=True)


# Function to load and process data
def _history_query_start(end_date, history_range):
    offsets = {
        '3M': pd.DateOffset(months=3),
        '6M': pd.DateOffset(months=6),
        '1Y': pd.DateOffset(years=1),
        'ALL': pd.DateOffset(years=4),
    }
    return pd.Timestamp(end_date) - offsets.get(history_range or '3M', offsets['3M'])


def load_and_process_data(history_range='3M'):
    """Load JKM and Brent slope inputs from transformed.enverus.curve only."""
    try:
        end_date = datetime.datetime.now()
        start_date = _history_query_start(end_date, history_range).to_pydatetime()
        from_cob = int(start_date.strftime('%Y%m%d'))
        to_cob = int(end_date.strftime('%Y%m%d'))
        postgres_from_cob = start_date.date()
        postgres_to_cob = end_date.date()
        codes = [source['code'] for source in ENVERUS_SLOPE_SOURCES.values()]
        categories = sorted({source['category'] for source in ENVERUS_SLOPE_SOURCES.values()})
        versions = sorted({source['version_name'] for source in ENVERUS_SLOPE_SOURCES.values()})

        trino_query = '''SELECT   code,
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
                                AND forward_curve_tenors_absolute NOT IN ('M-1','M-2','M-3')
                                AND forward_curve_tenors_value is not null
                            ORDER BY ondate, forward_curve_tenors_tenor
                                '''.format(
                                    _sql_in_literal(codes),
                                    _sql_in_literal(categories),
                                    _sql_in_literal(versions),
                                    from_cob,
                                    to_cob,
                                )
        postgres_query = text(
            f'''
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
            context_label='Slopes Enverus load',
        )
        df_options = _normalize_enverus_slopes(df_enverus)

        # Don't pre-calculate Brent indices - they will be generated on-demand
        # when user selects a specific index in the UI

        return df_options

    except Exception:
        return pd.DataFrame()  # Return empty DataFrame on error


# Initialize DataFrame at module level but allow refresh.
df_options = pd.DataFrame()


def _invalidate_slopes_data():
    global df_options, _slopes_refresh_generation
    with _SLOPES_DATA_LOCK:
        _slopes_refresh_generation += 1
        _slopes_data_cache.clear()
        df_options = pd.DataFrame()


def _ensure_slopes_data(history_range='3M'):
    global df_options
    history_range = history_range or '3M'
    with _SLOPES_DATA_LOCK:
        cache_key = (history_range, _slopes_refresh_generation)
        cached = _slopes_data_cache.get(cache_key)
        if cached is None:
            cached = load_and_process_data(history_range=history_range)
            _slopes_data_cache[cache_key] = cached
        df_options = cached
        return cached


def _normalize_slope_trade_dates(df):
    if df is None or df.empty or 'trade_date' not in df.columns:
        return pd.Series(dtype='datetime64[ns]')
    return pd.to_datetime(df['trade_date'], errors='coerce').dt.normalize().dropna()


def get_previous_available_date(df):
    dates = sorted(_normalize_slope_trade_dates(df).drop_duplicates())
    if len(dates) >= 2:
        return pd.Timestamp(dates[-2]).strftime('%Y-%m-%d')
    if len(dates) == 1:
        return pd.Timestamp(dates[0]).strftime('%Y-%m-%d')
    return None


def _resolve_slope_comparison_date(slope_data, comparison_date, latest_date):
    target = pd.to_datetime(comparison_date, errors='coerce')
    latest = pd.to_datetime(latest_date, errors='coerce')
    if pd.isna(target) or pd.isna(latest):
        return None
    dates = sorted(_normalize_slope_trade_dates(slope_data).drop_duplicates())
    candidates = [
        pd.Timestamp(value)
        for value in dates
        if pd.Timestamp(value) <= target.normalize() and pd.Timestamp(value) < latest.normalize()
    ]
    return max(candidates) if candidates else None


SLOPES_CHART_FONT = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
SLOPES_CHART_TEXT = '#0f172a'
SLOPES_CHART_MUTED = '#64748b'
SLOPES_CHART_GRID = 'rgba(148, 163, 184, 0.18)'
SLOPES_CHART_AXIS = '#94a3b8'
SLOPES_GRAPH_CONFIG = {
    'displayModeBar': 'hover',
    'displaylogo': False,
    'responsive': True,
    'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
}

SLOPE_COLUMN_WIDTHS = {
    'strip': 132,
    'trade_date': 118,
    'slope_percentage': 122,
    'comparison_date': 130,
    'comparison_slope': 144,
    'slope_change': 112,
    'jkm_price': 126,
    'brent_price': 118,
    'jkm_strip': 112,
    'rolling_label': 116,
    'window_start': 118,
    'window_end': 84,
    'brent_contracts': 440,
    'brent_avg': 126,
    'slope': 112,
}
SLOPE_NUMERIC_COLUMNS = {
    'slope_percentage',
    'comparison_slope',
    'slope_change',
    'jkm_price',
    'brent_price',
    'brent_avg',
    'slope',
}


def _slope_axis(title='', tickformat=None, **overrides):
    axis = {
        'title': dict(text=title, font=dict(size=11, color=SLOPES_CHART_MUTED)),
        'showgrid': True,
        'gridcolor': SLOPES_CHART_GRID,
        'gridwidth': 1,
        'zeroline': False,
        'linecolor': SLOPES_CHART_AXIS,
        'linewidth': 1,
        'tickfont': dict(size=10, color=SLOPES_CHART_MUTED),
        'ticks': 'outside',
        'ticklen': 3,
        'automargin': True,
    }
    if tickformat:
        axis['tickformat'] = tickformat
    axis.update(overrides)
    return axis


def _slope_legend():
    return {
        'orientation': 'h',
        'yanchor': 'top',
        'y': -0.18,
        'xanchor': 'center',
        'x': 0.5,
        'bgcolor': 'rgba(255, 255, 255, 0)',
        'bordercolor': 'rgba(255, 255, 255, 0)',
        'font': dict(size=9, color=SLOPES_CHART_MUTED),
        'itemsizing': 'constant',
        'itemwidth': 34,
        'tracegroupgap': 4,
    }


def _apply_slope_chart_theme(fig, margin=None, height=None):
    fig.update_layout(
        title=dict(text=''),
        font=dict(family=SLOPES_CHART_FONT, size=11, color=SLOPES_CHART_TEXT),
        plot_bgcolor='#f8fafc',
        paper_bgcolor='white',
        margin=margin or dict(l=46, r=18, t=18, b=88),
        hovermode='x unified',
        hoverlabel=dict(
            bgcolor='rgba(255, 255, 255, 0.96)',
            bordercolor='rgba(148, 163, 184, 0.45)',
            font=dict(size=11, color=SLOPES_CHART_TEXT, family=SLOPES_CHART_FONT),
            align='left',
        ),
        legend=_slope_legend(),
        showlegend=True,
        transition=dict(duration=180, easing='cubic-in-out'),
    )
    if height is not None:
        fig.update_layout(height=height)
    return fig


SLOPE_HISTORY_RANGE_OPTIONS = [
    {'label': '3M', 'value': '3M'},
    {'label': '6M', 'value': '6M'},
    {'label': '1Y', 'value': '1Y'},
    {'label': 'All', 'value': 'ALL'},
]


def _get_slope_history_cutoff(max_date, history_range):
    if pd.isna(max_date) or history_range == 'ALL':
        return None

    offsets = {
        '3M': pd.DateOffset(months=3),
        '6M': pd.DateOffset(months=6),
        '1Y': pd.DateOffset(years=1),
    }
    return max_date - offsets.get(history_range, offsets['3M'])


def _build_slope_history_range_selector():
    return dcc.RadioItems(
        id='slope-history-range-selector',
        options=SLOPE_HISTORY_RANGE_OPTIONS,
        value='3M',
        inline=True,
        className='slopes-segmented-selector slopes-history-range-selector',
        inputStyle={'display': 'none'},
        labelStyle={'marginRight': '0'},
    )


def _build_slopes_section_header(title, title_controls=None, actions=None):
    title_children = [html.H3(title, className='section-title-inline slopes-section-title')]
    if title_controls:
        title_children.append(html.Div(title_controls, className='slopes-section-title-controls'))

    return html.Div(
        [
            html.Div(
                title_children,
                className='slopes-section-title-row',
            ),
            html.Div(actions or [], className='slopes-section-actions'),
        ],
        className='slopes-section-header',
    )


def _build_slopes_chart_header(title):
    return html.Div(
        html.Div(
            [html.H5(title, className='slopes-chart-card-title')],
            className='slopes-chart-card-title-group',
        ),
        className='slopes-chart-card-header',
    )


def _build_slopes_chart_card(graph, title, className=None):
    classes = ['slopes-chart-card']
    if className:
        classes.append(className)
    return html.Div([_build_slopes_chart_header(title), graph], className=' '.join(classes))


def _build_slopes_message(message, tone='neutral'):
    return html.Div(message, className=f'slopes-empty-state slopes-empty-state-{tone}')


def _clean_ag_grid_records(df):
    if df is None or df.empty:
        return []
    records = []
    for row in df.to_dict('records'):
        clean_row = {}
        for key, value in row.items():
            if pd.isna(value):
                clean_row[key] = None
            elif key in SLOPE_NUMERIC_COLUMNS:
                clean_row[key] = f"{float(value):,.2f}"
                clean_row[f'__{key}_raw'] = float(value)
            else:
                clean_row[key] = value
        records.append(clean_row)
    return records


def _slope_raw_number_expression(field):
    return f"Number(params.data && params.data['__{field}_raw'])"


def _build_slope_column_defs(columns):
    column_defs = []
    for col in columns:
        field = col['id']
        is_numeric = col.get('type') == 'numeric' or field in SLOPE_NUMERIC_COLUMNS
        width = SLOPE_COLUMN_WIDTHS.get(field, 118)
        column_def = {
            'headerName': col['name'],
            'field': field,
            'sortable': True,
            'filter': False,
            'resizable': True,
            'width': width,
            'minWidth': min(width, 88),
            'maxWidth': 520 if field == 'brent_contracts' else max(width + 36, 118),
            'tooltipField': field,
            'headerTooltip': col['name'],
            'cellClass': 'slopes-table-text-cell',
            'headerClass': 'slopes-table-text-header',
        }
        if field in {'strip', 'jkm_strip', 'rolling_label'}:
            column_def.update({'pinned': 'left', 'lockPinned': True})
        if is_numeric:
            raw_value = _slope_raw_number_expression(field)
            column_def.update({
                'type': 'rightAligned',
                'cellClass': 'slopes-table-number-cell',
                'headerClass': 'slopes-table-number-header',
                'cellClassRules': {
                    'slopes-positive-cell': (
                        f"['slope_percentage', 'comparison_slope', 'slope_change', 'slope'].includes('{field}') "
                        f"&& {raw_value} > 0"
                    ),
                    'slopes-negative-cell': f"{raw_value} < 0",
                    'slopes-missing-cell': (
                        f"params.data === null || params.data === undefined "
                        f"|| params.data['__{field}_raw'] === null || params.data['__{field}_raw'] === undefined "
                        f"|| isNaN(Number(params.data['__{field}_raw']))"
                    ),
                },
            })
        if field == 'brent_contracts':
            column_def.update({'cellClass': 'slopes-table-text-cell slopes-contracts-cell'})
        column_defs.append(column_def)
    return column_defs


def _build_slope_grid(columns, data, className=None):
    classes = ['ag-theme-alpine', 'mckinsey-ag-grid', 'slopes-data-grid']
    if className:
        classes.append(className)
    return dag.AgGrid(
        rowData=data,
        columnDefs=_build_slope_column_defs(columns),
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


def _build_slope_table_panel(title, grid, chips=None, className=None):
    classes = ['slopes-table-panel']
    if className:
        classes.append(className)
    return html.Div(
        [
            html.Div(
                [
                    html.H4(title, className='slopes-table-panel-title'),
                    html.Div(chips or [], className='slopes-table-panel-chips'),
                ],
                className='slopes-table-panel-header',
            ),
            grid,
        ],
        className=' '.join(classes),
    )


def _build_slope_chip(label, value=None, tone='neutral'):
    if value is None or value == '':
        return None
    return html.Span(
        [
            html.Span(label, className='slopes-chip-label'),
            html.Span(str(value), className='slopes-chip-value'),
        ],
        className=f'slopes-chip slopes-chip-{tone}',
    )


def _build_slopes_filter_bar():
    return html.Div(
        [
            html.Div(
                [
                    html.Span('Slopes', className='filter-group-header'),
                    dcc.Dropdown(
                        id='slope-product-dropdown',
                        options=[{'label': 'JKM/Brent', 'value': 'JKM/Brent'}],
                        value=['JKM/Brent'],
                        multi=True,
                        clearable=False,
                        className='slopes-filter-dropdown slopes-product-dropdown',
                    ),
                ],
                className='filter-group slopes-sticky-filter-group slopes-products-group',
            ),
            html.Div(
                [
                    html.Span('JKM Discount', className='filter-group-header'),
                    dcc.Input(
                        id='jkm-discount-input',
                        type='number',
                        value=0,
                        placeholder='0.0',
                        className='slopes-number-input slopes-discount-input',
                    ),
                ],
                className='filter-group slopes-sticky-filter-group slopes-number-group',
            ),
            html.Div(
                [
                    html.Span('Brent Index', className='filter-group-header'),
                    dcc.Dropdown(
                        id='brent-preset-dropdown',
                        options=[
                            {'label': 'Brent', 'value': 'spot'},
                            {'label': '101 (1M avg, no lag)', 'value': '101'},
                            {'label': '301 (3M avg, no lag)', 'value': '301'},
                            {'label': '311 (3M avg, 1M lag)', 'value': '311'},
                            {'label': '601 (6M avg, no lag)', 'value': '601'},
                            {'label': 'Custom...', 'value': 'custom'}
                        ],
                        value='301',
                        clearable=False,
                        className='slopes-filter-dropdown slopes-brent-dropdown',
                    ),
                ],
                className='filter-group slopes-sticky-filter-group slopes-brent-group',
            ),
            html.Div(
                id='brent-custom-inputs',
                className='filter-group slopes-sticky-filter-group slopes-custom-brent-group',
                style={'display': 'none'},
                children=[
                    html.Span('Lag', className='filter-group-header'),
                    dcc.Input(
                        id='brent-lag-input',
                        type='number',
                        min=0,
                        max=12,
                        value=3,
                        className='slopes-number-input slopes-small-number-input',
                    ),
                    html.Span('Window', className='filter-group-header'),
                    dcc.Input(
                        id='brent-window-input',
                        type='number',
                        min=1,
                        max=12,
                        value=3,
                        className='slopes-number-input slopes-small-number-input',
                    ),
                ],
            ),
            html.Div(
                [
                    html.Span('Compare', className='filter-group-header'),
                    html.Div(
                        dcc.DatePickerSingle(
                            id='slope-comparison-date-picker',
                            display_format='YYYY-MM-DD',
                            with_portal=True,
                            className='slopes-date-picker',
                        ),
                        className='slopes-date-control',
                    ),
                ],
                className='filter-group slopes-sticky-filter-group slopes-date-group',
            ),
            html.Div(
                [
                    html.Span('Group By', className='filter-group-header'),
                    dcc.RadioItems(
                        id='slope-grouping-dropdown',
                        options=[
                            {'label': 'Monthly', 'value': 'monthly'},
                            {'label': 'Quarterly', 'value': 'quarterly'},
                            {'label': 'Season', 'value': 'season'},
                            {'label': 'Calendar', 'value': 'calendar'},
                        ],
                        value='calendar',
                        inline=True,
                        className='slopes-segmented-selector slopes-grouping-selector',
                        inputStyle={'display': 'none'},
                        labelStyle={'marginRight': '0'},
                    ),
                ],
                className='filter-group slopes-sticky-filter-group slopes-grouping-group',
            ),
            html.Div(
                [
                    html.Span('View', className='filter-group-header'),
                    dcc.RadioItems(
                        id='view-mode-dropdown',
                        options=[
                            {'label': 'Strips', 'value': 'strips'},
                            {'label': 'Rolling', 'value': 'rolling'},
                        ],
                        value='strips',
                        inline=True,
                        className='slopes-segmented-selector slopes-view-selector',
                        inputStyle={'display': 'none'},
                        labelStyle={'marginRight': '0'},
                    ),
                ],
                className='filter-group slopes-sticky-filter-group slopes-view-group',
            ),
            html.Div(
                id='rolling-periods-control',
                className='filter-group slopes-sticky-filter-group slopes-rolling-periods-group',
                style={'display': 'none'},
                children=[
                    html.Span('Periods', className='filter-group-header'),
                    dcc.Input(
                        id='rolling-periods-input',
                        type='number',
                        min=1,
                        max=10,
                        value=5,
                        className='slopes-number-input slopes-small-number-input',
                    ),
                ],
            ),
        ],
        className='professional-section-header slopes-sticky-filter-bar',
    )


layout = html.Div(
    [
        dcc.Store(id='refresh-trigger', data=0, storage_type='memory'),
        dcc.Store(id='selected-brent-config', data='B301', storage_type='memory'),
        dcc.Store(id='slope-data-store', storage_type='memory'),
        dcc.Download(id='download-dataframe-xlsx'),

        _build_slopes_filter_bar(),

        html.Div(
            [
                _build_slopes_section_header(
                    'Slope Charts',
                    title_controls=[_build_slope_history_range_selector()],
                    actions=[
                        html.Button(
                            'Export Data',
                            id='download-data-button',
                            className='custom-export-btn slopes-export-button',
                        )
                    ],
                ),
                html.Div(id='slope-graphs-container', className='slopes-section-body slopes-chart-body'),
            ],
            className='slopes-section slopes-chart-section',
        ),
        html.Div(
            [
                _build_slopes_section_header('Slope Data'),
                html.Div(id='slope-tables-container', className='slopes-section-body slopes-table-body'),
            ],
            className='slopes-section slopes-data-section',
        ),
        html.Div(
            [
                _build_slopes_section_header('Contract Details'),
                html.Div(id='contract-details-container', className='slopes-section-body slopes-contract-body'),
            ],
            className='slopes-section slopes-contract-section',
        ),
    ],
    className='options-dashboard-container slopes-page',
)


# SIMPLIFIED: Data refresh callback that updates global variable and triggers refresh
@dash.callback(
    Output('refresh-trigger', 'data'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=False
)
def refresh_data_and_ui(n_clicks):
    """Refresh data and update UI elements"""
    if n_clicks:
        _invalidate_slopes_data()
    return n_clicks or 0


# Initialize comparison date picker (previous business date)
@dash.callback(
    Output('slope-comparison-date-picker', 'date'),
    Input('refresh-trigger', 'data'),
    prevent_initial_call=False
)
def init_slope_comparison_date(refresh_trigger):
    del refresh_trigger
    return get_previous_available_date(_ensure_slopes_data('3M'))


# Toggle custom Brent inputs visibility
@dash.callback(
    Output('brent-custom-inputs', 'style'),
    Input('brent-preset-dropdown', 'value')
)
def toggle_custom_inputs(preset_value):
    """Show/hide custom inputs based on preset selection"""
    if preset_value == 'custom':
        return {'display': 'inline-flex'}
    else:
        return {'display': 'none'}


# Toggle rolling periods control visibility
@dash.callback(
    Output('rolling-periods-control', 'style'),
    Input('view-mode-dropdown', 'value')
)
def toggle_rolling_periods_control(view_mode):
    """Show/hide rolling periods control based on view mode"""
    if view_mode == 'rolling':
        return {'display': 'inline-flex'}
    else:
        return {'display': 'none'}


# Parse Brent index selection and update store
@dash.callback(
    Output('selected-brent-config', 'data'),
    [Input('brent-preset-dropdown', 'value'),
     Input('brent-lag-input', 'value'),
     Input('brent-window-input', 'value')]
)
def parse_brent_selection(preset, custom_lag, custom_window):
    """
    Parse user selection and return contract code to use for filtering

    Parameters:
    -----------
    preset : str
        Preset value from dropdown ('spot', '101', '301', '311', '601', 'custom')
    custom_lag : int
        Custom lag months (only used if preset='custom')
    custom_window : int
        Custom window months (only used if preset='custom')

    Returns:
    --------
    str : Contract code like 'Brent' (spot), 'B101', 'B301', 'B311', 'B601', or custom
    """
    if preset == 'spot':
        return 'Brent'  # Use original spot Brent
    elif preset == 'custom':
        # For custom, generate code in format BXYZ (X=window, Y=lag, Z=delivery months)
        window = custom_window if custom_window is not None else 3
        lag = custom_lag if custom_lag is not None else 0
        return f'B{window}{lag}1'
    else:
        # Preset like '101', '301', '311', '601'
        return f'B{preset}'


# ============================================================================
# ROLLING CONTRACTS HELPER FUNCTIONS
# ============================================================================

def calculate_year_rolling_label(trade_date, maturity_date):
    """
    Calculate rolling year label (Y+1, Y+2, etc.)

    Parameters:
    -----------
    trade_date : datetime
        The observation date
    maturity_date : datetime
        The contract delivery period

    Returns:
    --------
    str : Rolling label like 'Y+1', 'Y+2', etc.

    Example:
    --------
    trade_date = 2024-03-15, maturity_date = 2025-xx-xx → 'Y+1' (next year)
    trade_date = 2024-03-15, maturity_date = 2026-xx-xx → 'Y+2' (2 years ahead)
    """
    try:
        years_ahead = maturity_date.year - trade_date.year
        if years_ahead < 0:
            return None  # Historical contract, skip
        return f'Y+{years_ahead}'
    except Exception:
        return None


def calculate_month_rolling_label(trade_date, maturity_date):
    """
    Calculate rolling month label (M+1, M+2, etc.)

    Parameters:
    -----------
    trade_date : datetime
        The observation date
    maturity_date : datetime
        The contract delivery period

    Returns:
    --------
    str : Rolling label like 'M+1', 'M+2', etc.

    Example:
    --------
    trade_date = 2024-03-15, maturity_date = 2024-04-01 → 'M+1' (next month)
    trade_date = 2024-03-15, maturity_date = 2024-05-01 → 'M+2' (2 months ahead)
    """
    try:
        months_ahead = (maturity_date.year - trade_date.year) * 12 + (maturity_date.month - trade_date.month)
        if months_ahead < 0:
            return None  # Historical contract, skip
        return f'M+{months_ahead}'
    except Exception:
        return None


def calculate_quarter_rolling_label(trade_date, maturity_date):
    """
    Calculate rolling quarter label (Q+1, Q+2, etc.)

    Parameters:
    -----------
    trade_date : datetime
        The observation date
    maturity_date : datetime
        The contract delivery period

    Returns:
    --------
    str : Rolling label like 'Q+1', 'Q+2', etc.

    Example:
    --------
    trade_date = 2024-03-15 (Q1), maturity_date in Q2-2024 → 'Q+1' (next quarter)
    trade_date = 2024-03-15 (Q1), maturity_date in Q3-2024 → 'Q+2' (2 quarters ahead)
    """
    try:
        trade_quarter = (trade_date.year * 4) + ((trade_date.month - 1) // 3)
        maturity_quarter = (maturity_date.year * 4) + ((maturity_date.month - 1) // 3)
        quarters_ahead = maturity_quarter - trade_quarter
        if quarters_ahead < 0:
            return None  # Historical contract, skip
        return f'Q+{quarters_ahead}'
    except Exception:
        return None


def calculate_season_rolling_label(trade_date, maturity_date):
    """
    Calculate rolling season label (S+1, S+2, etc.)

    Season definition:
    - Summer: May-September (months 5-9)
    - Winter: October-April (months 10-12, 1-4)

    Parameters:
    -----------
    trade_date : datetime
        The observation date
    maturity_date : datetime
        The contract delivery period

    Returns:
    --------
    str : Rolling label like 'S+1', 'S+2', etc.

    Example:
    --------
    trade_date = 2024-03-15 (Winter), maturity_date in Summer-2024 → 'S+1' (next season)
    trade_date = 2024-03-15 (Winter), maturity_date in Winter-2024 → 'S+2' (2 seasons ahead)
    """
    try:
        def get_season_number(date):
            """Calculate absolute season number from a date"""
            year = date.year
            month = date.month
            # Summer = May-Sep (months 5-9)
            # Winter = Oct-Apr (months 10-12, 1-4)
            if 5 <= month <= 9:
                # Summer season
                return year * 2
            else:
                # Winter season
                # Oct-Dec belongs to current year's winter (season 2)
                # Jan-Apr belongs to previous year's winter (season 2)
                if month >= 10:
                    return year * 2 + 1
                else:
                    return (year - 1) * 2 + 1

        trade_season = get_season_number(trade_date)
        maturity_season = get_season_number(maturity_date)
        seasons_ahead = maturity_season - trade_season

        if seasons_ahead < 0:
            return None  # Historical contract, skip
        return f'S+{seasons_ahead}'
    except Exception:
        return None


def calculate_rolling_contracts(slope_data, grouping_mode, num_periods=5):
    """
    Convert slope data from strips view to rolling contracts view

    Parameters:
    -----------
    slope_data : pd.DataFrame
        Calculated slope data with columns: trade_date, strip, slope_percentage, etc.
    grouping_mode : str
        Grouping mode ('monthly', 'quarterly', 'season', 'calendar')
    num_periods : int
        Number of rolling periods to display (1-10)

    Returns:
    --------
    pd.DataFrame : Slope data with rolling labels instead of strips

    Process:
    --------
    For each row in slope_data:
        1. Calculate rolling label based on trade_date and strip's maturity_date
        2. Replace strip with rolling label
        3. Filter to keep only first N rolling periods

    Example:
    --------
    Input (Strips view):
        trade_date='2024-03-15', strip='2025', slope=150
        trade_date='2024-03-15', strip='2026', slope=145
        trade_date='2024-04-01', strip='2025', slope=148

    Output (Rolling view, calendar mode):
        trade_date='2024-03-15', strip='Y+1', slope=150  (2025 is next year)
        trade_date='2024-03-15', strip='Y+2', slope=145  (2026 is 2 years ahead)
        trade_date='2024-04-01', strip='Y+1', slope=148  (2025 is still next year)
    """
    try:
        df = slope_data.copy()

        # Parse maturity_date from strip
        if grouping_mode == 'monthly':
            # Strip format: 'Jan-25'
            df['maturity_date'] = pd.to_datetime(df['strip'], format='%b-%y', errors='coerce')
        elif grouping_mode == 'quarterly':
            # Strip format: '2025-Q1'
            df['maturity_date'] = pd.to_datetime(df['strip'].str.replace(r'-Q(\d)', r'-\1', regex=True) + '-01',
                                                 format='%Y-%m-%d', errors='coerce')
        elif grouping_mode == 'season':
            # Strip format: '2025-Summer' or '2025-Winter'
            def parse_season(season_str):
                try:
                    year, season = season_str.split('-')
                    year = int(year)
                    if season == 'Summer':
                        return pd.Timestamp(year=year, month=7, day=1)  # Mid-summer
                    else:  # Winter
                        return pd.Timestamp(year=year, month=1, day=1)  # Mid-winter
                except Exception:
                    return None
            df['maturity_date'] = df['strip'].apply(parse_season)
        elif grouping_mode == 'calendar':
            # Strip format: '2025'
            df['maturity_date'] = pd.to_datetime(df['strip'] + '-01-01', format='%Y-%m-%d', errors='coerce')

        # Ensure trade_date is datetime
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')

        # Drop rows with invalid dates
        df = df.dropna(subset=['trade_date', 'maturity_date'])

        # Calculate rolling labels based on grouping mode
        if grouping_mode == 'calendar':
            df['rolling_label'] = df.apply(lambda row: calculate_year_rolling_label(row['trade_date'], row['maturity_date']), axis=1)
        elif grouping_mode == 'monthly':
            df['rolling_label'] = df.apply(lambda row: calculate_month_rolling_label(row['trade_date'], row['maturity_date']), axis=1)
        elif grouping_mode == 'quarterly':
            df['rolling_label'] = df.apply(lambda row: calculate_quarter_rolling_label(row['trade_date'], row['maturity_date']), axis=1)
        elif grouping_mode == 'season':
            df['rolling_label'] = df.apply(lambda row: calculate_season_rolling_label(row['trade_date'], row['maturity_date']), axis=1)

        # Drop rows where rolling label couldn't be calculated (historical contracts)
        df = df.dropna(subset=['rolling_label'])

        # Extract numeric offset from rolling label (e.g., 'Y+2' → 2)
        df['rolling_offset'] = df['rolling_label'].str.extract(r'\+(\d+)').astype(int)

        # Filter to keep only first N periods (0 to num_periods-1)
        df = df[df['rolling_offset'] < num_periods]

        # Replace strip with rolling label
        df['original_strip'] = df['strip']  # Keep original for reference
        df['strip'] = df['rolling_label']

        # Drop temporary columns
        df = df.drop(columns=['maturity_date', 'rolling_label', 'rolling_offset'])

        return df

    except Exception:
        return slope_data  # Return original data on error

# ============================================================================


def slope_group_data_by_period(data, grouping_mode):
    """Group price data by different time periods based on maturity_date (contract delivery period)."""
    try:
        data = _ensure_delivery_strip(data)

        data['trade_date'] = pd.to_datetime(data['trade_date'], errors='coerce')
        data = data.dropna(subset=['maturity_date', 'trade_date'])

        if grouping_mode == 'monthly':
            data['period'] = data['maturity_date'].dt.strftime('%b-%y')
            return data

        elif grouping_mode == 'quarterly':
            data['quarter'] = data['maturity_date'].dt.quarter
            data['year'] = data['maturity_date'].dt.year
            data['period'] = data.apply(lambda x: f"{x['year']}-Q{x['quarter']}", axis=1)

            grouped = data.groupby(['product', 'contract', 'trade_date', 'period']).agg({
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

            grouped = data.groupby(['product', 'contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        elif grouping_mode == 'calendar':
            # For calendar grouping, use maturity_date year (contract delivery year)
            data['period'] = data['maturity_date'].dt.year.astype(str)

            grouped = data.groupby(['product', 'contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean'
            }).reset_index()
            return grouped

        data['period'] = data['maturity_date'].dt.strftime('%b-%y')
        return data

    except Exception:
        if 'period' not in data.columns:
            data['period'] = 'unknown'
        return data


def _ensure_delivery_strip(data):
    """Keep strip aligned to the delivery month, not the contract expiry date."""
    data = data.copy()

    if 'maturity_date' in data.columns:
        data['maturity_date'] = pd.to_datetime(data['maturity_date'], errors='coerce')
    else:
        data['maturity_date'] = pd.NaT

    if 'strip' in data.columns:
        strip_text = data['strip'].astype(str).str.strip()
        strip_maturity = pd.to_datetime(strip_text, format='%b-%y', errors='coerce')

        missing_strip_maturity = strip_maturity.isna()
        if missing_strip_maturity.any():
            strip_maturity.loc[missing_strip_maturity] = pd.to_datetime(
                strip_text.loc[missing_strip_maturity],
                errors='coerce',
            )

        missing_maturity = data['maturity_date'].isna()
        data.loc[missing_maturity, 'maturity_date'] = strip_maturity.loc[missing_maturity]

    if data['maturity_date'].isna().any() and 'expiration_date' in data.columns:
        fallback_expiry = pd.to_datetime(data['expiration_date'], errors='coerce')
        missing_maturity = data['maturity_date'].isna()
        data.loc[missing_maturity, 'maturity_date'] = fallback_expiry.loc[missing_maturity]

    data['strip'] = data['maturity_date'].dt.strftime('%b-%y')
    return data


# ============================================================================
# BRENT INDEX CALCULATION FUNCTIONS
# ============================================================================

def get_averaging_window(maturity_date, lag_months, window_months):
    """
    Calculate the start and end dates for Brent averaging window

    Naming Convention XYZ:
    - X = Number of months in averaging window (PRECEDING delivery)
    - Y = Additional lag months (gap between averaging window end and delivery)
    - Z = Always 1 (for one month of delivery)

    The averaging window always PRECEDES the delivery month.
    - 0 lag = averaging window ends right before delivery (no gap)
    - 1 lag = 1 month gap between averaging window end and delivery

    Parameters:
    -----------
    maturity_date : datetime
        The LNG delivery month
    lag_months : int
        Additional gap between averaging window end and delivery month
    window_months : int
        Number of months in the averaging window (counting backward from window end)

    Returns:
    --------
    tuple : (window_start_date, window_end_date)

    Examples:
    ---------
    For Apr-26 delivery (maturity_date = 2026-04-01):

    301 (3-month avg, 0-month lag):
        window_months=3, lag_months=0
        → Window END: Apr-26 - 1 - 0 = Mar-26 (M-1, no gap)
        → Window START: Mar-26 - (3-1) = Jan-26 (M-3)
        → Window: Jan-26 to Mar-26 (3 months BEFORE delivery)

    311 (3-month avg, 1-month lag):
        window_months=3, lag_months=1
        → Window END: Apr-26 - 1 - 1 = Feb-26 (M-2, 1 month gap)
        → Window START: Feb-26 - (3-1) = Dec-25 (M-4)
        → Window: Dec-25 to Feb-26 (with 1 month gap to delivery)

    601 (6-month avg, 0-month lag):
        window_months=6, lag_months=0
        → Window END: Apr-26 - 1 - 0 = Mar-26 (M-1, no gap)
        → Window START: Mar-26 - (6-1) = Oct-25 (M-6)
        → Window: Oct-25 to Mar-26 (6 months BEFORE delivery)

    101 (1-month avg, 0-month lag):
        window_months=1, lag_months=0
        → Window END: Apr-26 - 1 - 0 = Mar-26 (M-1, no gap)
        → Window START: Mar-26 - (1-1) = Mar-26 (M-1)
        → Window: Mar-26 to Mar-26 (1 month BEFORE delivery)
    """
    from dateutil.relativedelta import relativedelta

    # Calculate end of averaging window
    # Formula: delivery month - 1 (to get month before) - lag_months (additional gap)
    window_end = maturity_date - relativedelta(months=1) - relativedelta(months=lag_months)

    # Calculate start of averaging window
    # Go back (window_months - 1) from the window end
    window_start = window_end - relativedelta(months=window_months - 1)

    # Get first day of window start month
    window_start_date = window_start.replace(day=1)

    # Get last day of window end month
    window_end_date = (window_end.replace(day=1) + relativedelta(months=1) - relativedelta(days=1))

    return window_start_date, window_end_date


def generate_brent_index_on_demand(df_combined, brent_contract, df_grouped_jkm):
    """
    Generate a single Brent index on-demand, only for the periods/strips needed in the chart

    OPTIMIZED: Uses vectorized operations instead of row iteration for 10-100x performance boost

    Parameters:
    -----------
    df_combined : pd.DataFrame
        Combined dataframe with JKM and original Brent data
    brent_contract : str
        Contract code like 'B31', 'B33', 'B61', 'B63'
    df_grouped_jkm : pd.DataFrame
        Already grouped JKM data - we'll match only these (trade_date, strip) combinations

    Returns:
    --------
    pd.DataFrame : Synthetic Brent index data for only the needed combinations
    """
    try:
        # If spot Brent is requested, process it to ensure strip format consistency
        if brent_contract == 'Brent':
            df_spot = df_combined[df_combined['contract'] == 'Brent'].copy()

            # Ensure dates are datetime
            df_spot['trade_date'] = pd.to_datetime(df_spot['trade_date'], errors='coerce')
            df_spot = _ensure_delivery_strip(df_spot)

            return df_spot

        # Parse window and lag from contract code
        # Format: BXYZ where X=window months, Y=lag months, Z=delivery months (always 1)
        # Examples: B301 = 3-month window, 0-month lag, 1 delivery
        #           B311 = 3-month window, 1-month lag, 1 delivery
        #           B601 = 6-month window, 0-month lag, 1 delivery
        if not brent_contract.startswith('B') or len(brent_contract) < 3:
            return pd.DataFrame()

        try:
            window_months = int(brent_contract[1])
            lag_months = int(brent_contract[2])
        except (ValueError, IndexError):
            return pd.DataFrame()


        # Filter for original Brent data only
        df_brent_raw = df_combined[df_combined['contract'].isin(['B', 'Brent'])].copy()

        if df_brent_raw.empty:
            return pd.DataFrame()

        # Ensure dates are datetime
        df_brent_raw['trade_date'] = pd.to_datetime(df_brent_raw['trade_date'], errors='coerce')

        df_brent_raw = _ensure_delivery_strip(df_brent_raw)

        # Get only the (trade_date, strip) combinations that exist in JKM data
        # For monthly grouping, this will have ALL trade_dates for each strip
        # For other groupings, this will be aggregated
        needed_combinations = df_grouped_jkm[['trade_date', 'strip']].drop_duplicates().copy()

        # Parse maturity_date for JKM strips
        needed_combinations['maturity_date'] = pd.to_datetime(needed_combinations['strip'], format='%b-%y', errors='coerce')
        needed_combinations = needed_combinations.dropna(subset=['maturity_date'])

        # OPTIMIZATION: Calculate averaging windows for all combinations at once
        from dateutil.relativedelta import relativedelta

        needed_combinations['window_start'] = needed_combinations['maturity_date'].apply(
            lambda x: (x - relativedelta(months=lag_months) - relativedelta(months=window_months - 1)).replace(day=1)
        )
        needed_combinations['window_end'] = needed_combinations['maturity_date'].apply(
            lambda x: (x - relativedelta(months=lag_months)).replace(day=1) + relativedelta(months=1) - relativedelta(days=1)
        )

        # Avoid a cartesian product by matching first on trade date.
        merged = needed_combinations.merge(
            df_brent_raw[['trade_date', 'maturity_date', 'settlement_price']],
            on='trade_date',
            suffixes=('_jkm', '_brent')
        )

        # Filter: Brent maturity must be within the averaging window
        filtered = merged[
            (merged['maturity_date_brent'] >= merged['window_start']) &
            (merged['maturity_date_brent'] <= merged['window_end'])
        ]

        # Group by JKM combination and calculate average Brent price
        result = filtered.groupby(['trade_date', 'strip', 'maturity_date_jkm']).agg({
            'settlement_price': 'mean'
        }).reset_index()

        result.columns = ['trade_date', 'strip', 'maturity_date', 'settlement_price']

        # Add metadata columns
        result['product'] = 'Brent'
        result['contract'] = brent_contract
        result['contract_type'] = None
        result['expiration_date'] = None


        return result[['trade_date', 'product', 'contract', 'strip', 'maturity_date', 'settlement_price', 'contract_type', 'expiration_date']]

    except Exception:
        return pd.DataFrame()

def slope_calculate_slope_data(df, grouping_mode, jkm_discount=0, brent_contract='Brent'):
    """
    Calculate JKM/Brent slope for matching strips using all available historical data

    Parameters:
    -----------
    df : pd.DataFrame
        Combined dataframe with JKM and Brent data
    grouping_mode : str
        How to group data ('monthly', 'quarterly', 'season', 'calendar')
    jkm_discount : float
        Discount to apply to JKM prices
    brent_contract : str
        Which Brent contract to use ('Brent', 'B31', 'B33', 'B61', 'B63', etc.)

    Returns:
    --------
    pd.DataFrame : Calculated slopes with trade_date, strip, slope_percentage, etc.
    """
    try:
        # Process all available data (no date filtering)
        df = df.copy()
        df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce')
        df['expiration_date'] = pd.to_datetime(df['expiration_date'], errors='coerce')

        if df.empty:
            return pd.DataFrame()

        # KEY CHANGE: Get UNGROUPED JKM data first for Brent index generation
        df_jkm_ungrouped = df[df['product'] == 'JKM'].copy()

        if df_jkm_ungrouped.empty:
            return pd.DataFrame()

        # Standardize delivery-month strips before generating Brent indices.
        df_jkm_ungrouped = _ensure_delivery_strip(df_jkm_ungrouped)

        # Generate Brent index at MONTHLY level (before grouping)
        # This ensures we calculate B33 for each month, then group it later
        brent_data = generate_brent_index_on_demand(df, brent_contract, df_jkm_ungrouped)

        if brent_data.empty:
            return pd.DataFrame()

        # NOW group both JKM and Brent by the specified period
        jkm_grouped = slope_group_data_by_period(df_jkm_ungrouped, grouping_mode)
        brent_grouped = slope_group_data_by_period(brent_data, grouping_mode)


        # Create strip identifiers for both
        if 'period' in jkm_grouped.columns:
            jkm_grouped['strip'] = jkm_grouped['period']
        else:
            jkm_grouped = _ensure_delivery_strip(jkm_grouped)

        if 'period' in brent_grouped.columns:
            brent_grouped['strip'] = brent_grouped['period']
        else:
            brent_grouped = _ensure_delivery_strip(brent_grouped)


        # Apply JKM discount
        if jkm_discount != 0:
            jkm_grouped['settlement_price'] = jkm_grouped['settlement_price'] - jkm_discount

        # Merge on trade_date and strip to find matching pairs
        merged = pd.merge(
            jkm_grouped[['trade_date', 'strip', 'settlement_price']].rename(columns={'settlement_price': 'jkm_price'}),
            brent_grouped[['trade_date', 'strip', 'settlement_price']].rename(columns={'settlement_price': 'brent_price'}),
            on=['trade_date', 'strip'],
            how='inner'
        )

        if merged.empty:
            return pd.DataFrame()

        # Calculate slope as percentage: (JKM/Brent) * 100
        merged['slope_percentage'] = ((merged['jkm_price'] / merged['brent_price'])) * 100
        merged['slope_percentage'] = merged['slope_percentage'].round(2)  # Round to 2 decimals
        merged['slope_name'] = 'JKM/Brent'

        return merged[['trade_date', 'strip', 'slope_percentage', 'slope_name', 'jkm_price', 'brent_price']]

    except Exception:
        return pd.DataFrame()


def _build_slope_graphs_from_store(data_store, selected_slopes, history_range):
    if not selected_slopes:
        return html.Div()

    if not data_store:
        return _build_slopes_message("No slope data available for the selected parameters", 'danger')

    if data_store.get('error'):
        return _build_slopes_message(data_store['error'], data_store.get('tone', 'danger'))

    if not data_store.get('slope_data'):
        return _build_slopes_message("No slope data available for the selected parameters", 'danger')

    slope_data = pd.read_json(io.StringIO(data_store['slope_data']), orient='split')
    if slope_data.empty:
        return _build_slopes_message("No slope data available for the selected parameters", 'danger')

    slope_data['trade_date'] = pd.to_datetime(slope_data['trade_date'], errors='coerce')
    slope_data = slope_data.dropna(subset=['trade_date'])
    if slope_data.empty:
        return _build_slopes_message("No valid trade dates found in slope data", 'warning')

    grouping_mode = data_store.get('grouping_mode') or 'calendar'
    brent_contract = data_store.get('brent_contract') or 'Brent'
    jkm_discount = data_store.get('jkm_discount') or 0
    view_mode = data_store.get('view_mode') or 'strips'
    brent_label_map = {
        'Brent': 'Brent',
        'B101': '101 (1M avg, no lag)',
        'B301': '301 (3M avg, no lag)',
        'B311': '311 (3M avg, 1M lag)',
        'B601': '601 (6M avg, no lag)'
    }
    brent_label = brent_label_map.get(brent_contract, brent_contract)
    history_range = history_range or '3M'
    history_label = next(
        (option['label'] for option in SLOPE_HISTORY_RANGE_OPTIONS if option['value'] == history_range),
        '3M',
    )

    graphs = []

    for slope_name in selected_slopes:
        if slope_name != 'JKM/Brent':
            continue

        max_date = slope_data['trade_date'].max()
        min_zoom_date = _get_slope_history_cutoff(max_date, history_range)
        chart_slope_data = slope_data.copy()
        if min_zoom_date is not None:
            chart_slope_data = chart_slope_data[chart_slope_data['trade_date'] >= min_zoom_date]

        if chart_slope_data.empty:
            continue

        fig = go.Figure()
        strips = sorted(chart_slope_data['strip'].unique())

        for strip in strips:
            strip_data = chart_slope_data[chart_slope_data['strip'] == strip].sort_values('trade_date')
            if strip_data.empty:
                continue

            fig.add_trace(go.Scatter(
                x=strip_data['trade_date'],
                y=strip_data['slope_percentage'],
                mode='lines+markers',
                name=f"{strip}",
                line=dict(width=2),
                marker=dict(size=4),
            ))

        min_chart_date = chart_slope_data['trade_date'].min()
        view_label = "Rolling View" if view_mode == 'rolling' else "Strips View"
        title_parts = [f"JKM/Brent {brent_label} Slope", history_label, grouping_mode.capitalize(), view_label]
        if jkm_discount:
            title_parts.append(f"JKM Discount: {jkm_discount}")

        _apply_slope_chart_theme(fig, height=468)
        fig.update_xaxes(
            **_slope_axis(
                "Trade Date",
                range=[min_chart_date, max_date],
                type='date',
            )
        )
        fig.update_yaxes(
            **_slope_axis(
                "Slope (%)",
                autorange=True,
                fixedrange=False,
            )
        )

        graphs.append(
            _build_slopes_chart_card(
                dcc.Graph(
                    figure=fig,
                    config=SLOPES_GRAPH_CONFIG,
                    className='slopes-chart-graph',
                    style={'height': '468px'},
                ),
                " | ".join(title_parts),
                className='slopes-main-chart-card',
            )
        )

    if not graphs:
        return _build_slopes_message("No graphs could be generated", 'danger')

    return html.Div(graphs, className='slopes-chart-grid')


# Update calculated data callback - loads all historical data
@dash.callback(
    Output('slope-data-store', 'data'),
    [Input('slope-grouping-dropdown', 'value'),
     Input('slope-product-dropdown', 'value'),
     Input('jkm-discount-input', 'value'),
     Input('selected-brent-config', 'data'),
     Input('view-mode-dropdown', 'value'),
     Input('rolling-periods-input', 'value'),
     Input('slope-history-range-selector', 'value'),
     Input('refresh-trigger', 'data')],
    prevent_initial_call=True
)
def update_slope_data_store(
    grouping_mode,
    selected_slopes,
    jkm_discount,
    brent_contract,
    view_mode,
    num_rolling_periods,
    history_range,
    refresh_trigger,
):
    try:
        if not selected_slopes:
            return None

        slope_source_data = _ensure_slopes_data(history_range or '3M')
        if slope_source_data is None or slope_source_data.empty:
            return {
                'error': "No data available. Please click Refresh to load data.",
                'tone': 'warning',
            }

        # Calculate slope data using all available historical data with selected Brent contract
        slope_data = slope_calculate_slope_data(slope_source_data, grouping_mode, jkm_discount or 0, brent_contract)

        if slope_data.empty:
            return {
                'error': "No slope data available for the selected parameters",
                'tone': 'danger',
            }

        # Apply rolling view transformation if needed
        if view_mode == 'rolling':
            num_periods = num_rolling_periods if num_rolling_periods else 5
            slope_data = calculate_rolling_contracts(slope_data, grouping_mode, num_periods)
            if slope_data.empty:
                return {
                    'error': "No rolling contract data available",
                    'tone': 'danger',
                }

        # Store slope data for download along with JKM and Brent data
        # Prepare data to store
        data_to_store = {
            'slope_data': slope_data.to_json(date_format='iso', orient='split'),
            'grouping_mode': grouping_mode,
            'brent_contract': brent_contract,
            'jkm_discount': jkm_discount or 0,
            'view_mode': view_mode,
            'history_range': history_range or '3M',
            'source_rows': len(slope_source_data),
            'source_start': pd.to_datetime(slope_source_data['trade_date']).min().strftime('%Y-%m-%d'),
            'source_end': pd.to_datetime(slope_source_data['trade_date']).max().strftime('%Y-%m-%d'),
        }

        return data_to_store

    except Exception as e:
        return {
            'error': f"Error generating graphs: {str(e)}",
            'tone': 'danger',
        }


# Update graphs callback - reuses the calculated graph dataset
@dash.callback(
    Output('slope-graphs-container', 'children'),
    [Input('slope-data-store', 'data'),
     Input('slope-product-dropdown', 'value'),
     Input('slope-history-range-selector', 'value')],
    prevent_initial_call=True
)
def update_slope_graphs(data_store, selected_slopes, history_range):
    return _build_slope_graphs_from_store(data_store, selected_slopes, history_range)


# Update tables callback - reuses the calculated graph dataset
@dash.callback(
    Output('slope-tables-container', 'children'),
    [Input('slope-data-store', 'data'),
     Input('slope-comparison-date-picker', 'date')],
    [State('slope-product-dropdown', 'value'),
     State('jkm-discount-input', 'value'),
     State('view-mode-dropdown', 'value')],
    prevent_initial_call=True
)
def update_slope_tables(data_store, comparison_date, selected_slopes, jkm_discount, view_mode):
    try:
        if not selected_slopes:
            return html.Div()

        if not data_store or not data_store.get('slope_data'):
            return _build_slopes_message("No slope data available for the selected parameters", 'danger')

        slope_data = pd.read_json(io.StringIO(data_store['slope_data']), orient='split')
        if slope_data.empty:
            return _build_slopes_message("No slope data available for the selected parameters", 'danger')

        slope_data['trade_date'] = pd.to_datetime(slope_data['trade_date'], errors='coerce')
        slope_data = slope_data.dropna(subset=['trade_date'])
        if slope_data.empty:
            return _build_slopes_message("No valid slope dates available", 'danger')

        # Create pivot table for slope data
        try:
            # Get the latest trade date for each strip
            latest_data = slope_data.loc[slope_data.groupby('strip')['trade_date'].idxmax()]

            if latest_data.empty:
                return _build_slopes_message("No latest slope data available", 'danger')

            # Get comparison data if comparison date is provided
            comparison_data = pd.DataFrame()
            resolved_comparison_date = None
            if comparison_date:
                latest_trade_date = pd.Timestamp(slope_data['trade_date'].max()).normalize()
                resolved_comparison_date = _resolve_slope_comparison_date(
                    slope_data,
                    comparison_date,
                    latest_trade_date,
                )
                if resolved_comparison_date is not None:
                    comparison_data = slope_data[
                        slope_data['trade_date'].dt.normalize() == resolved_comparison_date
                    ].copy()

            # Create table showing latest slopes by strip
            table_data = latest_data[['strip', 'trade_date', 'slope_percentage', 'jkm_price', 'brent_price']].copy()
            table_data['trade_date'] = table_data['trade_date'].dt.strftime('%Y-%m-%d')
            table_data = table_data.round({'slope_percentage': 2, 'jkm_price': 2, 'brent_price': 2})

            # Add comparison columns if comparison data exists
            if not comparison_data.empty:
                # Merge comparison data
                comparison_summary = comparison_data[['strip', 'slope_percentage', 'trade_date']].copy()
                comparison_summary = comparison_summary.drop_duplicates('strip', keep='last')
                comparison_summary.columns = ['strip', 'comparison_slope', 'comparison_date']
                comparison_summary['comparison_date'] = pd.to_datetime(
                    comparison_summary['comparison_date']).dt.strftime('%Y-%m-%d')
                comparison_summary['comparison_slope'] = comparison_summary['comparison_slope'].round(2)

                table_data = table_data.merge(comparison_summary, on='strip', how='left')

                # Calculate change
                table_data['slope_change'] = table_data['slope_percentage'] - table_data['comparison_slope']
                table_data['slope_change'] = table_data['slope_change'].round(2)

                strip_column_name = "Rolling Contract" if view_mode == 'rolling' else "Strip"
                columns = [
                    {"name": strip_column_name, "id": "strip"},
                    {"name": "Latest Date", "id": "trade_date"},
                    {"name": "Latest Slope (%)", "id": "slope_percentage", "type": "numeric",
                     "format": {"specifier": ".2f"}},
                    {"name": "Comparison Date", "id": "comparison_date"},
                    {"name": "Comparison Slope (%)", "id": "comparison_slope", "type": "numeric",
                     "format": {"specifier": ".2f"}},
                    {"name": "Change (%)", "id": "slope_change", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": f"JKM Price{' (Discounted)' if jkm_discount else ''}", "id": "jkm_price",
                     "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Brent Price", "id": "brent_price", "type": "numeric", "format": {"specifier": ".2f"}}
                ]

            else:
                strip_column_name = "Rolling Contract" if view_mode == 'rolling' else "Strip"
                columns = [
                    {"name": strip_column_name, "id": "strip"},
                    {"name": "Latest Date", "id": "trade_date"},
                    {"name": "Slope (%)", "id": "slope_percentage", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": f"JKM Price{' (Discounted)' if jkm_discount else ''}", "id": "jkm_price",
                     "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Brent Price", "id": "brent_price", "type": "numeric", "format": {"specifier": ".2f"}}
                ]

            # Build table title with view mode
            view_label = "Rolling Contract" if view_mode == 'rolling' else "Strip"
            comparison_label = (
                resolved_comparison_date.strftime('%Y-%m-%d')
                if resolved_comparison_date is not None
                else None
            )
            table_title = f"Latest Slope Data by {view_label}" + (f" (vs {comparison_label})" if comparison_label else "")

            grid = _build_slope_grid(columns, _clean_ag_grid_records(table_data), 'slopes-latest-grid')
            chips = [
                _build_slope_chip('Rows', len(table_data), 'primary'),
                _build_slope_chip('Mode', view_label),
            ]
            if comparison_label:
                chips.append(_build_slope_chip('Compare', comparison_label))

            return _build_slope_table_panel(table_title, grid, [chip for chip in chips if chip])

        except Exception as e:
            return _build_slopes_message(f"Error creating slope table: {str(e)}", 'danger')

    except Exception as e:
        return _build_slopes_message(f"Error generating tables: {str(e)}", 'danger')


# Download data callback - exports slope data to Excel
@dash.callback(
    Output('download-dataframe-xlsx', 'data'),
    Input('download-data-button', 'n_clicks'),
    State('slope-data-store', 'data'),
    prevent_initial_call=True
)
def download_slope_data(n_clicks, data_store):
    """Export slope data along with JKM and Brent data to Excel file"""
    try:
        if data_store is None:
            return None

        # Extract stored data
        slope_data_json = data_store.get('slope_data')
        grouping_mode = data_store.get('grouping_mode')
        brent_contract = data_store.get('brent_contract')
        jkm_discount = data_store.get('jkm_discount', 0)
        view_mode = data_store.get('view_mode', 'strips')

        if slope_data_json is None:
            return None

        # Parse JSON back to DataFrame
        slope_data = pd.read_json(io.StringIO(slope_data_json), orient='split')

        if slope_data.empty:
            return None

        # Prepare data in long format with columns: trade_date, strip, slope, brent_index, jkm
        export_data = slope_data[['trade_date', 'strip', 'slope_percentage', 'brent_price', 'jkm_price']].copy()

        # Format dates
        export_data['trade_date'] = pd.to_datetime(export_data['trade_date']).dt.strftime('%Y-%m-%d')

        # Round values
        export_data['slope_percentage'] = export_data['slope_percentage'].round(2)
        export_data['brent_price'] = export_data['brent_price'].round(2)
        export_data['jkm_price'] = export_data['jkm_price'].round(2)

        # Rename columns for clarity
        export_data = export_data.rename(columns={
            'slope_percentage': 'Slope (%)',
            'brent_price': f'{brent_contract} Price',
            'jkm_price': 'JKM Price'
        })

        # Reorder columns: trade_date, strip, slope, brent, jkm
        export_data = export_data[['trade_date', 'strip', 'Slope (%)', f'{brent_contract} Price', 'JKM Price']]

        # Sort by trade_date and strip
        export_data = export_data.sort_values(['trade_date', 'strip']).reset_index(drop=True)

        # Create filename with metadata
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        view_label = 'rolling' if view_mode == 'rolling' else 'strips'
        filename = f"jkm_brent_slopes_{grouping_mode}_{view_label}_{brent_contract}_{timestamp}.xlsx"

        # Write to Excel using BytesIO
        output = io.BytesIO()

        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # Write export data
            export_data.to_excel(writer, sheet_name='Data', index=False)

            # Add metadata sheet
            metadata = pd.DataFrame({
                'Parameter': ['Grouping Mode', 'View Mode', 'Brent Contract', 'JKM Discount', 'Export Date'],
                'Value': [
                    grouping_mode,
                    view_mode,
                    brent_contract,
                    f'${jkm_discount:.2f}' if jkm_discount else '$0.00',
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                ]
            })
            metadata.to_excel(writer, sheet_name='Metadata', index=False)

            # Auto-adjust column widths
            for sheet_name in writer.sheets:
                worksheet = writer.sheets[sheet_name]
                for column in worksheet.columns:
                    max_length = 0
                    column_letter = column[0].column_letter
                    for cell in column:
                        try:
                            if len(str(cell.value)) > max_length:
                                max_length = len(str(cell.value))
                        except Exception:
                            pass
                    adjusted_width = min(max_length + 2, 50)
                    worksheet.column_dimensions[column_letter].width = adjusted_width

        output.seek(0)

        return dcc.send_bytes(output.getvalue(), filename)

    except Exception:
        return None


# Contract details inspector callback - shows underlying contracts for latest trade date
@dash.callback(
    Output('contract-details-container', 'children'),
    [Input('slope-grouping-dropdown', 'value'),
     Input('jkm-discount-input', 'value'),
     Input('selected-brent-config', 'data'),
     Input('view-mode-dropdown', 'value'),
     Input('rolling-periods-input', 'value'),
     Input('refresh-trigger', 'data')],
    prevent_initial_call=True
)
def update_contract_details(grouping_mode, jkm_discount, brent_contract, view_mode, num_rolling_periods, refresh_trigger):
    """Show detailed breakdown of contracts used for slope calculation at the latest trade date"""
    try:
        if df_options is None or df_options.empty:
            return _build_slopes_message("No data available", 'warning')

        # Get the latest trade date
        df_temp = df_options.copy()
        df_temp['trade_date'] = pd.to_datetime(df_temp['trade_date'], errors='coerce')
        latest_trade_date = df_temp['trade_date'].max()

        if pd.isna(latest_trade_date):
            return _build_slopes_message("No valid trade dates found", 'danger')


        # Filter data for latest trade date
        df_latest = df_temp[df_temp['trade_date'] == latest_trade_date].copy()

        # Get JKM data at monthly level (ungrouped)
        df_jkm_monthly = df_latest[df_latest['product'] == 'JKM'].copy()
        df_jkm_monthly['expiration_date'] = pd.to_datetime(df_jkm_monthly['expiration_date'], errors='coerce')
        df_jkm_monthly = _ensure_delivery_strip(df_jkm_monthly)

        # Apply JKM discount if specified
        if jkm_discount:
            df_jkm_monthly['settlement_price'] = df_jkm_monthly['settlement_price'] - jkm_discount

        # Get Brent data at monthly level
        df_brent_monthly = df_latest[df_latest['contract'].isin(['B', 'Brent'])].copy()
        df_brent_monthly = _ensure_delivery_strip(df_brent_monthly)


        # Calculate Brent indices for each JKM contract
        brent_details = []

        if brent_contract != 'Brent':
            # Parse window and lag from contract code (format: BXYZ)
            try:
                window_months = int(brent_contract[1])
                lag_months = int(brent_contract[2])
            except Exception:
                return _build_slopes_message(f"Invalid Brent contract: {brent_contract}", 'danger')

            for _, jkm_row in df_jkm_monthly.iterrows():
                jkm_strip = jkm_row['strip']
                jkm_maturity = jkm_row['maturity_date']
                jkm_price = jkm_row['settlement_price']

                if pd.isna(jkm_maturity):
                    continue

                # Calculate averaging window
                window_start_date, window_end_date = get_averaging_window(jkm_maturity, lag_months, window_months)

                # Find Brent contracts in this window
                brent_in_window = df_brent_monthly[
                    (df_brent_monthly['maturity_date'] >= window_start_date) &
                    (df_brent_monthly['maturity_date'] <= window_end_date)
                ].copy()

                if not brent_in_window.empty:
                    brent_avg = brent_in_window['settlement_price'].mean()
                    brent_contracts = ', '.join([f"{row['strip']}: ${row['settlement_price']:.2f}"
                                                for _, row in brent_in_window.iterrows()])
                else:
                    brent_avg = None
                    brent_contracts = 'No data'

                # Calculate rolling label if in rolling mode
                rolling_label = None
                if view_mode == 'rolling':
                    if grouping_mode == 'calendar':
                        rolling_label = calculate_year_rolling_label(latest_trade_date, jkm_maturity)
                    elif grouping_mode == 'monthly':
                        rolling_label = calculate_month_rolling_label(latest_trade_date, jkm_maturity)
                    elif grouping_mode == 'quarterly':
                        rolling_label = calculate_quarter_rolling_label(latest_trade_date, jkm_maturity)
                    elif grouping_mode == 'season':
                        rolling_label = calculate_season_rolling_label(latest_trade_date, jkm_maturity)

                brent_details.append({
                    'jkm_strip': jkm_strip,
                    'rolling_label': rolling_label,
                    'jkm_price': jkm_price,
                    'window_start': window_start_date.strftime('%b-%y'),
                    'window_end': window_end_date.strftime('%b-%y'),
                    'brent_contracts': brent_contracts,
                    'brent_avg': brent_avg,
                    'slope': ((jkm_price / brent_avg) * 100) if brent_avg else None
                })
        else:
            # For spot Brent, just match by strip
            for _, jkm_row in df_jkm_monthly.iterrows():
                jkm_strip = jkm_row['strip']
                jkm_maturity = jkm_row['maturity_date']
                jkm_price = jkm_row['settlement_price']

                brent_match = df_brent_monthly[df_brent_monthly['strip'] == jkm_strip]
                if not brent_match.empty:
                    brent_price = brent_match['settlement_price'].iloc[0]

                    # Calculate rolling label if in rolling mode
                    rolling_label = None
                    if view_mode == 'rolling' and not pd.isna(jkm_maturity):
                        if grouping_mode == 'calendar':
                            rolling_label = calculate_year_rolling_label(latest_trade_date, jkm_maturity)
                        elif grouping_mode == 'monthly':
                            rolling_label = calculate_month_rolling_label(latest_trade_date, jkm_maturity)
                        elif grouping_mode == 'quarterly':
                            rolling_label = calculate_quarter_rolling_label(latest_trade_date, jkm_maturity)
                        elif grouping_mode == 'season':
                            rolling_label = calculate_season_rolling_label(latest_trade_date, jkm_maturity)

                    brent_details.append({
                        'jkm_strip': jkm_strip,
                        'rolling_label': rolling_label,
                        'jkm_price': jkm_price,
                        'window_start': jkm_strip,
                        'window_end': jkm_strip,
                        'brent_contracts': f"{jkm_strip}: ${brent_price:.2f}",
                        'brent_avg': brent_price,
                        'slope': ((jkm_price / brent_price) * 100) if brent_price else None
                    })

        df_details = pd.DataFrame(brent_details)

        if df_details.empty:
            return _build_slopes_message("No contract details available for latest trade date", 'warning')

        # Group by calendar/season/quarterly if needed
        if grouping_mode != 'monthly':
            # Add period column based on grouping mode
            df_details['maturity_date'] = pd.to_datetime(df_details['jkm_strip'], format='%b-%y', errors='coerce')

            if grouping_mode == 'calendar':
                df_details['period'] = df_details['maturity_date'].dt.year.astype(str)
            elif grouping_mode == 'quarterly':
                df_details['quarter'] = df_details['maturity_date'].dt.quarter
                df_details['year'] = df_details['maturity_date'].dt.year
                df_details['period'] = df_details.apply(lambda x: f"{x['year']}-Q{x['quarter']}", axis=1)
            elif grouping_mode == 'season':
                def get_season(date):
                    month = date.month
                    year = date.year
                    if 5 <= month <= 9:
                        return f"{year}-Summer"
                    else:
                        return f"{year}-Winter"
                df_details['period'] = df_details['maturity_date'].apply(get_season)

            # Create grouped summary tables
            grouped_periods = df_details.groupby('period')

            all_tables = []

            for period, group in grouped_periods:
                # Calculate aggregated values for this period
                avg_jkm = group['jkm_price'].mean()
                avg_brent = group['brent_avg'].mean()
                avg_slope = (avg_jkm / avg_brent * 100) if avg_brent else None

                # Create detail table for this period
                if view_mode == 'rolling':
                    detail_data = group[['jkm_strip', 'rolling_label', 'jkm_price', 'window_start', 'window_end', 'brent_contracts', 'brent_avg', 'slope']].copy()
                else:
                    detail_data = group[['jkm_strip', 'jkm_price', 'window_start', 'window_end', 'brent_contracts', 'brent_avg', 'slope']].copy()
                detail_data = detail_data.round({'jkm_price': 2, 'brent_avg': 2, 'slope': 2})

                # Build columns based on view mode
                if view_mode == 'rolling':
                    table_columns = [
                        {"name": "JKM Strip", "id": "jkm_strip"},
                        {"name": "Rolling Label", "id": "rolling_label"},
                        {"name": "JKM Price", "id": "jkm_price", "type": "numeric", "format": {"specifier": ".2f"}},
                        {"name": "Brent Window", "id": "window_start"},
                        {"name": "to", "id": "window_end"},
                        {"name": "Brent Contracts Used", "id": "brent_contracts"},
                        {"name": f"{brent_contract} Avg", "id": "brent_avg", "type": "numeric", "format": {"specifier": ".2f"}},
                        {"name": "Slope (%)", "id": "slope", "type": "numeric", "format": {"specifier": ".2f"}}
                    ]
                else:
                    table_columns = [
                        {"name": "JKM Strip", "id": "jkm_strip"},
                        {"name": "JKM Price", "id": "jkm_price", "type": "numeric", "format": {"specifier": ".2f"}},
                        {"name": "Brent Window", "id": "window_start"},
                        {"name": "to", "id": "window_end"},
                        {"name": "Brent Contracts Used", "id": "brent_contracts"},
                        {"name": f"{brent_contract} Avg", "id": "brent_avg", "type": "numeric", "format": {"specifier": ".2f"}},
                        {"name": "Slope (%)", "id": "slope", "type": "numeric", "format": {"specifier": ".2f"}}
                    ]

                period_grid = _build_slope_grid(
                    table_columns,
                    _clean_ag_grid_records(detail_data),
                    'slopes-contract-grid',
                )
                period_table = _build_slope_table_panel(
                    f"Period {period}",
                    period_grid,
                    [
                        _build_slope_chip('Avg JKM', f"${avg_jkm:.2f}", 'primary'),
                        _build_slope_chip('Avg Brent', f"${avg_brent:.2f}"),
                        _build_slope_chip('Avg Slope', f"{avg_slope:.2f}%" if avg_slope is not None else None),
                    ],
                    className='slopes-period-panel',
                )
                all_tables.append(period_table)

            return html.Div([
                html.Div(
                    [
                        _build_slope_chip('Trade Date', latest_trade_date.strftime('%Y-%m-%d'), 'primary'),
                        _build_slope_chip('Brent', brent_contract),
                        _build_slope_chip('Grouping', grouping_mode.capitalize()),
                    ],
                    className='slopes-status-line',
                ),
                *all_tables
            ], className='slopes-contract-panels')

        else:
            # Monthly view - single table
            if view_mode == 'rolling':
                detail_data = df_details[['jkm_strip', 'rolling_label', 'jkm_price', 'window_start', 'window_end', 'brent_contracts', 'brent_avg', 'slope']].copy()
            else:
                detail_data = df_details[['jkm_strip', 'jkm_price', 'window_start', 'window_end', 'brent_contracts', 'brent_avg', 'slope']].copy()
            detail_data = detail_data.round({'jkm_price': 2, 'brent_avg': 2, 'slope': 2})

            # Build columns based on view mode
            if view_mode == 'rolling':
                table_columns = [
                    {"name": "JKM Strip", "id": "jkm_strip"},
                    {"name": "Rolling Label", "id": "rolling_label"},
                    {"name": "JKM Price", "id": "jkm_price", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Brent Window", "id": "window_start"},
                    {"name": "to", "id": "window_end"},
                    {"name": "Brent Contracts Used", "id": "brent_contracts"},
                    {"name": f"{brent_contract} Avg", "id": "brent_avg", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Slope (%)", "id": "slope", "type": "numeric", "format": {"specifier": ".2f"}}
                ]
            else:
                table_columns = [
                    {"name": "JKM Strip", "id": "jkm_strip"},
                    {"name": "JKM Price", "id": "jkm_price", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Brent Window", "id": "window_start"},
                    {"name": "to", "id": "window_end"},
                    {"name": "Brent Contracts Used", "id": "brent_contracts"},
                    {"name": f"{brent_contract} Avg", "id": "brent_avg", "type": "numeric", "format": {"specifier": ".2f"}},
                    {"name": "Slope (%)", "id": "slope", "type": "numeric", "format": {"specifier": ".2f"}}
                ]

            monthly_grid = _build_slope_grid(
                table_columns,
                _clean_ag_grid_records(detail_data),
                'slopes-contract-grid',
            )
            return html.Div(
                [
                    html.Div(
                        [
                            _build_slope_chip('Trade Date', latest_trade_date.strftime('%Y-%m-%d'), 'primary'),
                            _build_slope_chip('Brent', brent_contract),
                            _build_slope_chip('Grouping', grouping_mode.capitalize()),
                        ],
                        className='slopes-status-line',
                    ),
                    _build_slope_table_panel('Monthly Contract Details', monthly_grid, className='slopes-period-panel'),
                ],
                className='slopes-contract-panels',
            )

    except Exception as e:
        return _build_slopes_message(f"Error generating contract details: {str(e)}", 'danger')
