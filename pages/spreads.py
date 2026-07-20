import json
import threading

import dash_ag_grid as dag
from dash import html, dcc
import pandas as pd
from dash import callback, Input, Output, State
import plotly.graph_objects as go
import datetime
from sqlalchemy import text

from db_fallback import DB_SCHEMA, fq_table, read_with_fallback, sql_literal


ENVERUS_SPREAD_SOURCES = {
    'TFU': {'code': 'ICE_TFU_MO', 'category': 'FINANCIAL', 'version_name': 'FINAL', 'contract': 'TFU'},
    'HH': {'code': 'ICE_HH', 'category': 'FINANCIAL', 'version_name': 'FINAL', 'contract': 'H'},
}
ENVERUS_CODE_TO_SPREAD_SOURCE = {
    source['code']: product
    for product, source in ENVERUS_SPREAD_SOURCES.items()
}
ENVERUS_CODE_TO_SPREAD_CONTRACT = {
    source['code']: source['contract']
    for source in ENVERUS_SPREAD_SOURCES.values()
}
_SPREADS_DATA_LOCK = threading.Lock()
_spreads_data_cache = {}
_spreads_refresh_generation = 0


def _sql_in_literal(values):
    return ', '.join(sql_literal(value) for value in values)


def _delivery_month_from_enverus_contract(series):
    contract_text = series.astype(str).str.strip()
    maturity_date = pd.to_datetime(contract_text, format='%YM%m', errors='coerce')
    spot_mask = contract_text.eq('SPOT')
    if spot_mask.any():
        maturity_date.loc[spot_mask] = pd.NaT
    return maturity_date


def _normalize_enverus_spreads(df):
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
    normalized['product'] = normalized['code'].map(ENVERUS_CODE_TO_SPREAD_SOURCE).fillna(normalized['code'])
    normalized['hub'] = normalized['product']
    normalized['strip'] = normalized['maturity_date'].dt.strftime('%b-%y')
    normalized['contract'] = normalized['code'].map(ENVERUS_CODE_TO_SPREAD_CONTRACT).fillna(normalized['product'])
    normalized['contract_type'] = None

    normalized = normalized.dropna(subset=['trade_date', 'maturity_date', 'settlement_price'])
    return normalized[columns].reset_index(drop=True)


# Function to load and process data
def _spread_history_query_start(end_date, history_range):
    offsets = {
        '3M': pd.DateOffset(months=3),
        '6M': pd.DateOffset(months=6),
        '1Y': pd.DateOffset(years=1),
        'ALL': pd.DateOffset(years=4),
    }
    return pd.Timestamp(end_date) - offsets.get(history_range or '3M', offsets['3M'])


def load_and_process_data(history_range='3M'):
    """Load TFU and HH spread inputs from transformed.enverus.curve."""
    try:
        end_date = datetime.datetime.now()
        start_date = _spread_history_query_start(end_date, history_range).to_pydatetime()
        from_cob = int(start_date.strftime('%Y%m%d'))
        to_cob = int(end_date.strftime('%Y%m%d'))
        postgres_from_cob = start_date.date()
        postgres_to_cob = end_date.date()
        codes = [source['code'] for source in ENVERUS_SPREAD_SOURCES.values()]
        categories = sorted({source['category'] for source in ENVERUS_SPREAD_SOURCES.values()})
        versions = sorted({source['version_name'] for source in ENVERUS_SPREAD_SOURCES.values()})

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
            context_label='Spreads Enverus load',
        )
        df_spreads = _normalize_enverus_spreads(df_enverus)

        return df_spreads

    except Exception:
        return pd.DataFrame()  # Return empty DataFrame on error


# Initialize DataFrame at module level but allow refresh
df_spreads = pd.DataFrame()


def _invalidate_spreads_data():
    global df_spreads, _spreads_refresh_generation
    with _SPREADS_DATA_LOCK:
        _spreads_refresh_generation += 1
        _spreads_data_cache.clear()
        df_spreads = pd.DataFrame()


def _ensure_spreads_data(history_range='3M'):
    global df_spreads
    history_range = history_range or '3M'
    with _SPREADS_DATA_LOCK:
        cache_key = (history_range, _spreads_refresh_generation)
        cached = _spreads_data_cache.get(cache_key)
        if cached is None:
            cached = load_and_process_data(history_range=history_range)
            _spreads_data_cache[cache_key] = cached
        df_spreads = cached
        return cached


def _ensure_spread_delivery_strip(data):
    """Keep spread strips aligned to delivery month, not option/future expiry date."""
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


def spread_group_data_by_period(data, grouping_mode):
    """Group price data by different time periods based on maturity_date (contract delivery period)."""
    try:
        data = _ensure_spread_delivery_strip(data)

        data['trade_date'] = pd.to_datetime(data['trade_date'], errors='coerce')
        data = data.dropna(subset=['maturity_date', 'trade_date'])

        if grouping_mode == 'monthly':
            data['period'] = data['maturity_date'].dt.strftime('%b-%y')
            return data

        elif grouping_mode == 'quarterly':
            data['quarter'] = data['maturity_date'].dt.quarter
            data['year'] = data['maturity_date'].dt.year
            data['period'] = data.apply(lambda x: f"{x['year']}-Q{x['quarter']}", axis=1)

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean',
                'maturity_date': 'first'
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
                    # Winter spans across years (Nov-Mar)
                    if month >= 11:
                        return f"{year}-Winter"
                    else:
                        return f"{year-1}-Winter"

            data['period'] = data['maturity_date'].apply(get_season)
            data = data.dropna(subset=['period'])

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean',
                'maturity_date': 'first'
            }).reset_index()
            return grouped

        elif grouping_mode == 'calendar':
            # For calendar grouping, use maturity_date year (contract delivery year)
            data['period'] = data['maturity_date'].dt.year.astype(str)

            grouped = data.groupby(['contract', 'trade_date', 'period']).agg({
                'settlement_price': 'mean',
                'maturity_date': 'first'
            }).reset_index()
            return grouped

        data['period'] = data['maturity_date'].dt.strftime('%b-%y')
        return data

    except Exception:
        if 'period' not in data.columns:
            data['period'] = 'unknown'
        return data


# ============================================================================
# SPREAD CALCULATION FUNCTION
# ============================================================================

def calculate_spread_data(df, grouping_mode, hh_multiplier=1.0, hh_premium=0, tfu_discount=0):
    """
    Calculate TFU-HH spread with adjustments

    Parameters:
    - df: DataFrame with TFU and HH data
    - grouping_mode: 'monthly', 'quarterly', 'season', or 'calendar'
    - hh_multiplier: Multiplier for HH price (default 1.0)
    - hh_premium: Premium to add to HH price (default 0)
    - tfu_discount: Discount to subtract from TFU price (default 0)

    Returns:
    - DataFrame with columns: [trade_date, strip, spread, tfu_price, hh_price]
    """
    try:
        if df.empty:
            return pd.DataFrame()

        # Make a copy to avoid modifying original
        df = df.copy()

        # Ensure trade_date is datetime
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df['expiration_date'] = pd.to_datetime(df['expiration_date'])

        # Create strip identifier at monthly level first
        df = _ensure_spread_delivery_strip(df)
        df = df.dropna(subset=['strip'])

        # Separate TFU and HH data
        df_tfu = df[df['contract'] == 'TFU'].copy()
        df_hh = df[df['contract'].isin(['H', 'HH'])].copy()

        if df_tfu.empty or df_hh.empty:
            return pd.DataFrame()

        # Group by period for both contracts
        df_tfu_grouped = spread_group_data_by_period(df_tfu, grouping_mode)
        df_hh_grouped = spread_group_data_by_period(df_hh, grouping_mode)

        # Rename settlement_price to avoid conflicts
        df_tfu_grouped = df_tfu_grouped.rename(columns={'settlement_price': 'tfu_price'})
        df_hh_grouped = df_hh_grouped.rename(columns={'settlement_price': 'hh_price'})

        # Merge on trade_date and period
        df_merged = pd.merge(
            df_tfu_grouped[['trade_date', 'period', 'tfu_price', 'maturity_date']],
            df_hh_grouped[['trade_date', 'period', 'hh_price']],
            on=['trade_date', 'period'],
            how='inner'
        )

        if df_merged.empty:
            return pd.DataFrame()

        # Apply adjustments
        df_merged['tfu_adjusted'] = df_merged['tfu_price'] - tfu_discount
        df_merged['hh_adjusted'] = df_merged['hh_price'] * hh_multiplier + hh_premium

        # Calculate spread: TFU - HH
        df_merged['spread'] = df_merged['tfu_adjusted'] - df_merged['hh_adjusted']

        # Rename period to strip for consistency
        df_merged = df_merged.rename(columns={'period': 'strip'})

        # Sort by trade_date and maturity_date
        df_merged = df_merged.sort_values(['trade_date', 'maturity_date'])

        return df_merged[['trade_date', 'strip', 'spread', 'tfu_price', 'hh_price', 'maturity_date']]

    except Exception:
        return pd.DataFrame()


# ============================================================================
# PRESENTATION HELPERS
# ============================================================================

SPREAD_GROUPING_OPTIONS = [
    {'label': 'Monthly', 'value': 'monthly'},
    {'label': 'Quarterly', 'value': 'quarterly'},
    {'label': 'Season', 'value': 'season'},
    {'label': 'Calendar', 'value': 'calendar'},
]

SPREAD_HISTORY_OPTIONS = [
    {'label': '3M', 'value': '3M'},
    {'label': '6M', 'value': '6M'},
    {'label': '1Y', 'value': '1Y'},
    {'label': 'All', 'value': 'ALL'},
]

SPREAD_NUMERIC_COLUMNS = {'spread', 'tfu_price', 'hh_price', 'comparison_spread', 'change'}
SPREAD_SIGN_COLUMNS = {'spread', 'comparison_spread', 'change'}
SPREAD_CHART_FONT = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
SPREAD_CHART_TEXT = '#0f172a'
SPREAD_CHART_MUTED = '#64748b'
SPREAD_CHART_GRID = 'rgba(148, 163, 184, 0.18)'
SPREAD_CHART_AXIS = '#94a3b8'
SPREAD_GRAPH_CONFIG = {
    'displayModeBar': 'hover',
    'displaylogo': False,
    'responsive': True,
    'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
}


def _spread_axis(title='', tickformat=None, **overrides):
    axis = {
        'title': dict(text=title, font=dict(size=11, color=SPREAD_CHART_MUTED)),
        'showgrid': True,
        'gridcolor': SPREAD_CHART_GRID,
        'gridwidth': 1,
        'zeroline': False,
        'linecolor': SPREAD_CHART_AXIS,
        'linewidth': 1,
        'tickfont': dict(size=10, color=SPREAD_CHART_MUTED),
        'ticks': 'outside',
        'ticklen': 3,
        'automargin': True,
    }
    if tickformat:
        axis['tickformat'] = tickformat
    axis.update(overrides)
    return axis


def _spread_legend():
    return {
        'orientation': 'h',
        'yanchor': 'top',
        'y': -0.18,
        'xanchor': 'center',
        'x': 0.5,
        'bgcolor': 'rgba(255, 255, 255, 0)',
        'bordercolor': 'rgba(255, 255, 255, 0)',
        'font': dict(size=9, color=SPREAD_CHART_MUTED),
        'itemsizing': 'constant',
        'itemwidth': 46,
        'tracegroupgap': 4,
    }


def _apply_spread_chart_theme(fig, margin=None, height=None):
    fig.update_layout(
        title=dict(text=''),
        font=dict(family=SPREAD_CHART_FONT, size=11, color=SPREAD_CHART_TEXT),
        plot_bgcolor='#f8fafc',
        paper_bgcolor='white',
        margin=margin or dict(l=52, r=18, t=18, b=90),
        hovermode='x unified',
        hoverlabel=dict(
            bgcolor='rgba(255, 255, 255, 0.96)',
            bordercolor='rgba(148, 163, 184, 0.45)',
            font=dict(size=11, color=SPREAD_CHART_TEXT, family=SPREAD_CHART_FONT),
            align='left',
        ),
        legend=_spread_legend(),
        showlegend=True,
        transition=dict(duration=180, easing='cubic-in-out'),
    )
    if height is not None:
        fig.update_layout(height=height)
    return fig


def _build_spreads_section_header(title, actions=None):
    return html.Div(
        [
            html.Div(
                [html.H3(title, className='section-title-inline spreads-section-title')],
                className='spreads-section-title-row',
            ),
            html.Div(actions or [], className='spreads-section-actions'),
        ],
        className='spreads-section-header',
    )


def _build_spreads_chip(label, value=None, tone='neutral'):
    if value is None or value == '':
        return None
    return html.Span(
        [
            html.Span(label, className='spreads-chip-label'),
            html.Span(str(value), className='spreads-chip-value'),
        ],
        className=f'spreads-chip spreads-chip-{tone}',
    )


def _build_spreads_chart_header(title, chips=None):
    chip_nodes = [chip for chip in (chips or []) if chip is not None]
    return html.Div(
        [
            html.Div(
                [html.H5(title, className='spreads-chart-card-title')],
                className='spreads-chart-card-title-group',
            ),
            html.Div(chip_nodes, className='spreads-chart-card-chips'),
        ],
        className='spreads-chart-card-header',
    )


def _build_spreads_chart_card(graph, title, chips=None, className=None):
    classes = ['spreads-chart-card']
    if className:
        classes.append(className)
    return html.Div(
        [_build_spreads_chart_header(title, chips=chips), graph],
        className=' '.join(classes),
    )


def _build_spreads_message(message, tone='neutral'):
    return html.Div(message, className=f'spreads-empty-state spreads-empty-state-{tone}')


def _history_start_date(latest_date, history_range):
    latest_date = pd.Timestamp(latest_date)
    if history_range == '3M':
        return latest_date - pd.DateOffset(months=3)
    if history_range == '6M':
        return latest_date - pd.DateOffset(months=6)
    if history_range == '1Y':
        return latest_date - pd.DateOffset(years=1)
    return None


def _history_label(history_range):
    return {
        '3M': 'Last 3M',
        '6M': 'Last 6M',
        '1Y': 'Last 1Y',
        'ALL': 'All',
    }.get(history_range, 'Last 3M')


def _normalize_trade_dates(df):
    if df is None or df.empty or 'trade_date' not in df.columns:
        return pd.Series(dtype='datetime64[ns]')
    return pd.to_datetime(df['trade_date'], errors='coerce').dt.normalize().dropna()


def _previous_available_trade_date(df):
    dates = sorted(_normalize_trade_dates(df).drop_duplicates())
    if len(dates) >= 2:
        return pd.Timestamp(dates[-2]).strftime('%Y-%m-%d')
    if len(dates) == 1:
        return pd.Timestamp(dates[0]).strftime('%Y-%m-%d')
    return None


def _resolve_comparison_date(spread_data, comparison_date, latest_date):
    if not comparison_date:
        return None
    try:
        target_date = pd.to_datetime(comparison_date, errors='coerce')
        latest_date = pd.to_datetime(latest_date, errors='coerce')
        if pd.isna(target_date) or pd.isna(latest_date):
            return None

        dates = sorted(_normalize_trade_dates(spread_data).drop_duplicates())
        if not dates:
            return None

        target_date = target_date.normalize()
        latest_date = latest_date.normalize()
        candidates = [pd.Timestamp(date) for date in dates if pd.Timestamp(date) <= target_date and pd.Timestamp(date) < latest_date]
        if not candidates:
            return None
        return max(candidates)
    except Exception:
        return None


def _clean_spread_grid_records(df, numeric_fields):
    if df is None or df.empty:
        return []

    numeric_fields = set(numeric_fields or [])
    records = []
    for row in df.to_dict('records'):
        clean_row = {}
        for key, value in row.items():
            if pd.isna(value):
                clean_row[key] = None
                if key in numeric_fields:
                    clean_row[f'__{key}_raw'] = None
            elif key in numeric_fields:
                numeric_value = float(value)
                clean_row[key] = f"{numeric_value:,.2f}"
                clean_row[f'__{key}_raw'] = numeric_value
            else:
                clean_row[key] = value
        records.append(clean_row)
    return records


def _spread_raw_number_expression(field):
    raw_key = json.dumps(f'__{field}_raw')
    return f"Number(params.data && params.data[{raw_key}])"


def _spread_column_width(field, header, records):
    values = [str(record.get(field, '')) for record in records if record.get(field) is not None]
    max_len = max([len(str(header)), *(len(value) for value in values)] or [len(str(header))])
    if field == 'strip':
        return max(92, min(130, 34 + max_len * 7))
    return max(86, min(150, 32 + max_len * 7))


def _build_spread_column_defs(columns, records, numeric_fields=None, sign_fields=None):
    numeric_fields = set(numeric_fields or [])
    sign_fields = set(sign_fields or [])
    column_defs = []

    for col in columns:
        field = col['id']
        header = col['name']
        is_numeric = field in numeric_fields or col.get('type') == 'numeric'
        width = _spread_column_width(field, header, records)
        column_def = {
            'headerName': header,
            'field': field,
            'sortable': True,
            'filter': False,
            'resizable': True,
            'width': width,
            'minWidth': 82 if field == 'strip' else 76,
            'maxWidth': 165,
            'tooltipField': field,
            'headerTooltip': header,
            'cellClass': 'spreads-table-text-cell',
            'headerClass': 'spreads-table-text-header',
        }

        if field == 'strip':
            column_def.update({'pinned': 'left', 'lockPinned': True})

        if is_numeric:
            raw_value = _spread_raw_number_expression(field)
            column_def.update({
                'type': 'rightAligned',
                'cellClass': 'spreads-table-number-cell',
                'headerClass': 'spreads-table-number-header',
                'cellClassRules': {
                    'spreads-positive-cell': f"{json.dumps(sorted(sign_fields))}.includes({json.dumps(field)}) && {raw_value} > 0",
                    'spreads-negative-cell': f"{raw_value} < 0",
                    'spreads-missing-cell': (
                        f"params.data === null || params.data === undefined "
                        f"|| params.data[{json.dumps(f'__{field}_raw')}] === null "
                        f"|| params.data[{json.dumps(f'__{field}_raw')}] === undefined "
                        f"|| isNaN(Number(params.data[{json.dumps(f'__{field}_raw')}]))"
                    ),
                },
            })
        column_defs.append(column_def)

    return column_defs


def _build_spread_grid(grid_id, columns, table_df, numeric_fields=None, sign_fields=None, className=None):
    records = _clean_spread_grid_records(table_df, numeric_fields)
    classes = ['ag-theme-alpine', 'mckinsey-ag-grid', 'spreads-data-grid']
    if className:
        classes.append(className)
    return dag.AgGrid(
        id=grid_id,
        rowData=records,
        columnDefs=_build_spread_column_defs(columns, records, numeric_fields=numeric_fields, sign_fields=sign_fields),
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


def _build_spread_table_panel(title, grid, chips=None, className=None):
    classes = ['spreads-table-panel']
    if className:
        classes.append(className)
    return html.Div(
        [
            html.Div(
                [
                    html.H4(title, className='spreads-table-panel-title'),
                    html.Div([chip for chip in (chips or []) if chip is not None], className='spreads-table-panel-chips'),
                ],
                className='spreads-table-panel-header',
            ),
            grid,
        ],
        className=' '.join(classes),
    )


def _build_spreads_filter_bar():
    return html.Div(
        [
            html.Div(
                [
                    html.Span('Spread', className='filter-group-header'),
                    dcc.Dropdown(
                        id='spread-selection-dropdown',
                        options=[{'label': 'TFU-HH', 'value': 'TFU-HH'}],
                        value='TFU-HH',
                        clearable=False,
                        className='spreads-filter-dropdown spreads-spread-dropdown',
                    ),
                ],
                className='filter-group spreads-sticky-filter-group spreads-spread-group',
            ),
            html.Div(
                [
                    html.Span('HH Mult', className='filter-group-header'),
                    dcc.Input(
                        id='hh-multiplier-input',
                        type='number',
                        value=1.0,
                        step=0.1,
                        placeholder='1.0',
                        className='spreads-number-input spreads-small-number-input',
                    ),
                ],
                className='filter-group spreads-sticky-filter-group spreads-number-group',
            ),
            html.Div(
                [
                    html.Span('HH Prem', className='filter-group-header'),
                    dcc.Input(
                        id='hh-premium-input',
                        type='number',
                        value=0,
                        step=0.5,
                        placeholder='0.0',
                        className='spreads-number-input spreads-small-number-input',
                    ),
                ],
                className='filter-group spreads-sticky-filter-group spreads-number-group',
            ),
            html.Div(
                [
                    html.Span('TFU Disc', className='filter-group-header'),
                    dcc.Input(
                        id='tfu-discount-input',
                        type='number',
                        value=0,
                        step=0.5,
                        placeholder='0.0',
                        className='spreads-number-input spreads-small-number-input',
                    ),
                ],
                className='filter-group spreads-sticky-filter-group spreads-number-group',
            ),
            html.Div(
                [
                    html.Span('Group By', className='filter-group-header'),
                    dcc.RadioItems(
                        id='spread-grouping-dropdown',
                        options=SPREAD_GROUPING_OPTIONS,
                        value='calendar',
                        inline=True,
                        className='spreads-segmented-selector spreads-grouping-selector',
                        inputStyle={'display': 'none'},
                        labelStyle={'marginRight': '0'},
                    ),
                ],
                className='filter-group spreads-sticky-filter-group spreads-grouping-group',
            ),
            html.Div(
                [
                    html.Span('History', className='filter-group-header'),
                    dcc.RadioItems(
                        id='spread-history-range-selector',
                        options=SPREAD_HISTORY_OPTIONS,
                        value='3M',
                        inline=True,
                        className='spreads-segmented-selector spreads-history-range-selector',
                        inputStyle={'display': 'none'},
                        labelStyle={'marginRight': '0'},
                    ),
                ],
                className='filter-group spreads-sticky-filter-group spreads-history-group',
            ),
            html.Div(
                [
                    html.Span('Compare', className='filter-group-header'),
                    html.Div(
                        dcc.DatePickerSingle(
                            id='spread-comparison-date-picker',
                            display_format='YYYY-MM-DD',
                            with_portal=True,
                            className='spreads-date-picker',
                        ),
                        className='spreads-date-control',
                    ),
                ],
                className='filter-group spreads-sticky-filter-group spreads-date-group',
            ),
        ],
        className='professional-section-header spreads-sticky-filter-bar',
    )


# ============================================================================
# LAYOUT
# ============================================================================

layout = html.Div(
    [
        dcc.Store(id='spread-refresh-trigger', data=0, storage_type='memory'),
        dcc.Store(id='spread-data-store', storage_type='memory'),
        dcc.Download(id='spread-download-excel'),

        _build_spreads_filter_bar(),

        html.Div(
            [
                _build_spreads_section_header('Spread Charts'),
                html.Div(id='spread-graphs-container', className='spreads-section-body spreads-chart-body'),
            ],
            className='spreads-section spreads-chart-section',
        ),
        html.Div(
            [
                _build_spreads_section_header(
                    'Latest Spreads',
                    actions=[
                        html.Button(
                            'Export',
                            id='spread-download-button',
                            className='custom-export-btn spreads-export-button',
                        )
                    ],
                ),
                html.Div(id='spread-tables-container', className='spreads-section-body spreads-table-body'),
            ],
            className='spreads-section spreads-data-section',
        ),
    ],
    className='options-dashboard-container spreads-page',
)


# ============================================================================
# CALLBACKS
# ============================================================================

# Callback 1: Refresh data
@callback(
    Output('spread-refresh-trigger', 'data'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=True
)
def refresh_spread_data(n_clicks):
    """Reload spread data from database"""
    if n_clicks:
        _invalidate_spreads_data()
    return n_clicks if n_clicks else 0


# Callback 2: Initialize comparison date
@callback(
    Output('spread-comparison-date-picker', 'date'),
    Input('spread-refresh-trigger', 'data'),
    prevent_initial_call=False
)
def init_spread_comparison_date(trigger):
    """Initialize comparison date to the previous available spread COB."""
    del trigger
    return _previous_available_trade_date(_ensure_spreads_data('3M'))


def _build_spread_chart_from_store(spread_store, history_range, grouping_mode):
    grouping_mode = grouping_mode or 'calendar'
    history_range = history_range or '3M'

    if not spread_store:
        return _build_spreads_message('No data available. Please refresh data.', tone='danger')

    if isinstance(spread_store, dict) and spread_store.get('error'):
        return _build_spreads_message(spread_store['error'], tone=spread_store.get('tone', 'danger'))

    spread_data = pd.DataFrame(spread_store)
    if spread_data.empty:
        return _build_spreads_message('Unable to calculate spreads. Please check data.', tone='danger')

    spread_data = spread_data.copy()
    spread_data['trade_date'] = pd.to_datetime(spread_data['trade_date'], errors='coerce')
    spread_data = spread_data.dropna(subset=['trade_date'])

    if spread_data.empty:
        return _build_spreads_message('No valid trade dates found in spread data.', tone='warning')

    latest_date = pd.Timestamp(spread_data['trade_date'].max()).normalize()
    start_date = _history_start_date(latest_date, history_range)
    chart_data = spread_data.copy()
    if start_date is not None:
        chart_data = chart_data[chart_data['trade_date'].dt.normalize() >= pd.Timestamp(start_date).normalize()]

    if chart_data.empty:
        chart_data = spread_data[spread_data['trade_date'].dt.normalize() == latest_date]

    strips = chart_data.sort_values('maturity_date')['strip'].unique()

    fig = go.Figure()
    palette = [
        '#2E86C1',
        '#10b981',
        '#f59e0b',
        '#7c3aed',
        '#ef4444',
        '#64748b',
        '#14b8a6',
        '#a855f7',
        '#0f766e',
        '#334155',
    ]

    for trace_index, strip in enumerate(strips):
        strip_data = chart_data[chart_data['strip'] == strip].sort_values('trade_date')
        color = palette[trace_index % len(palette)]

        fig.add_trace(go.Scatter(
            x=strip_data['trade_date'],
            y=strip_data['spread'],
            mode='lines+markers',
            name=strip,
            line=dict(width=1.9, color=color),
            marker=dict(size=3.6, color=color),
            hovertemplate='<b>%{fullData.name}</b><br>' +
                         'Date: %{x|%Y-%m-%d}<br>' +
                         'Spread: $%{y:.2f}<br>' +
                         '<extra></extra>'
        ))

    xaxis = _spread_axis('Trade Date', tickformat='%b %d')
    if start_date is not None:
        xaxis['range'] = [pd.Timestamp(start_date), latest_date]

    fig.update_layout(
        xaxis=xaxis,
        yaxis=_spread_axis(
            'Spread ($/MMBtu)',
            zeroline=True,
            zerolinecolor='rgba(148, 163, 184, 0.40)',
            fixedrange=False,
        ),
    )
    _apply_spread_chart_theme(fig, height=492)

    graph = dcc.Graph(
        figure=fig,
        config=SPREAD_GRAPH_CONFIG,
        className='spreads-chart-graph',
        style={'height': '100%'},
    )
    chart_card = _build_spreads_chart_card(
        graph,
        'TFU-HH Spread',
        chips=[
            _build_spreads_chip('Latest', latest_date.strftime('%Y-%m-%d'), tone='primary'),
            _build_spreads_chip('Group', grouping_mode.capitalize()),
            _build_spreads_chip('History', _history_label(history_range)),
            _build_spreads_chip('Strips', len(strips)),
        ],
        className='spreads-main-chart-card',
    )

    return html.Div([chart_card], className='spreads-chart-grid')


# Callback 3: Update calculated data
@callback(
    Output('spread-data-store', 'data'),
    [Input('spread-grouping-dropdown', 'value'),
     Input('hh-multiplier-input', 'value'),
     Input('hh-premium-input', 'value'),
     Input('tfu-discount-input', 'value'),
     Input('spread-history-range-selector', 'value'),
     Input('spread-refresh-trigger', 'data')],
    prevent_initial_call=False
)
def update_spread_data_store(
    grouping_mode,
    hh_multiplier,
    hh_premium,
    tfu_discount,
    history_range,
    refresh_trigger,
):
    """Update calculated spread data based on parameters."""
    # Handle None values
    grouping_mode = grouping_mode or 'calendar'
    hh_multiplier = hh_multiplier if hh_multiplier is not None else 1.0
    hh_premium = hh_premium if hh_premium is not None else 0
    tfu_discount = tfu_discount if tfu_discount is not None else 0

    del refresh_trigger
    spread_source_data = _ensure_spreads_data(history_range or '3M')

    if spread_source_data.empty:
        return {
            'error': 'No data available. Please refresh data.',
            'tone': 'danger',
        }

    # Calculate spread data
    spread_data = calculate_spread_data(
        spread_source_data,
        grouping_mode,
        hh_multiplier,
        hh_premium,
        tfu_discount
    )

    if spread_data.empty:
        return {
            'error': 'Unable to calculate spreads. Please check data.',
            'tone': 'danger',
        }

    spread_data = spread_data.copy()
    spread_data['trade_date'] = pd.to_datetime(spread_data['trade_date'], errors='coerce')
    spread_data = spread_data.dropna(subset=['trade_date'])

    if spread_data.empty:
        return {
            'error': 'No valid trade dates found in spread data.',
            'tone': 'warning',
        }

    return spread_data.to_dict('records')


# Callback 4: Update graphs
@callback(
    Output('spread-graphs-container', 'children'),
    [Input('spread-data-store', 'data'),
     Input('spread-history-range-selector', 'value'),
     Input('spread-grouping-dropdown', 'value')],
    prevent_initial_call=False
)
def update_spread_graphs(spread_store, history_range, grouping_mode):
    """Update spread graphs from the already-calculated spread dataset."""
    return _build_spread_chart_from_store(spread_store, history_range, grouping_mode)


# Callback 5: Update tables
@callback(
    Output('spread-tables-container', 'children'),
    [Input('spread-data-store', 'data'),
     Input('spread-comparison-date-picker', 'date')],
    State('spread-grouping-dropdown', 'value'),
    prevent_initial_call=False
)
def update_spread_tables(spread_store, comparison_date, grouping_mode):
    """Update spread tables with latest values and comparison"""
    grouping_mode = grouping_mode or 'calendar'
    if not spread_store:
        return _build_spreads_message('No data available.', tone='danger')
    if isinstance(spread_store, dict) and spread_store.get('error'):
        return _build_spreads_message(spread_store['error'], tone=spread_store.get('tone', 'danger'))

    spread_data = pd.DataFrame(spread_store)
    if spread_data.empty:
        return _build_spreads_message('Unable to calculate spreads.', tone='danger')

    spread_data = spread_data.copy()
    spread_data['trade_date'] = pd.to_datetime(spread_data['trade_date'], errors='coerce')
    spread_data = spread_data.dropna(subset=['trade_date'])

    if spread_data.empty:
        return _build_spreads_message('No valid trade dates found in spread data.', tone='warning')

    # Get latest trade date
    latest_date = pd.Timestamp(spread_data['trade_date'].max()).normalize()
    latest_data = spread_data[spread_data['trade_date'].dt.normalize() == latest_date].copy()

    # Prepare table data - include maturity_date for sorting
    table_data = latest_data[['strip', 'spread', 'tfu_price', 'hh_price', 'maturity_date']].copy()

    columns = [
        {'name': 'Strip', 'id': 'strip'},
        {'name': 'Spread ($/MMBtu)', 'id': 'spread'},
        {'name': 'TFU ($/MMBtu)', 'id': 'tfu_price'},
        {'name': 'HH ($/MMBtu)', 'id': 'hh_price'},
    ]

    # Add comparison if date is selected
    resolved_comparison_date = None
    if comparison_date:
        try:
            resolved_comparison_date = _resolve_comparison_date(spread_data, comparison_date, latest_date)
            comp_data = pd.DataFrame()
            if resolved_comparison_date is not None:
                comp_data = spread_data[spread_data['trade_date'].dt.normalize() == resolved_comparison_date]

            if not comp_data.empty:
                # Merge comparison data
                comp_data = comp_data[['strip', 'spread']].copy()
                comp_data = comp_data.rename(columns={'spread': 'comparison_spread'})

                table_data = pd.merge(
                    table_data,
                    comp_data,
                    on='strip',
                    how='left'
                )

                table_data['change'] = table_data['spread'] - table_data['comparison_spread']

                columns.append({'name': f"{resolved_comparison_date.strftime('%Y-%m-%d')} Spread", 'id': 'comparison_spread'})
                columns.append({'name': 'Change', 'id': 'change'})
        except Exception:
            pass

    table_data = table_data.sort_values('maturity_date').drop(columns=['maturity_date'])

    numeric_fields = [column['id'] for column in columns if column['id'] in SPREAD_NUMERIC_COLUMNS]
    grid = _build_spread_grid(
        'spread-latest-grid',
        columns,
        table_data,
        numeric_fields=numeric_fields,
        sign_fields=SPREAD_SIGN_COLUMNS,
        className='spreads-latest-grid',
    )

    panel = _build_spread_table_panel(
        'TFU-HH by Strip',
        grid,
        chips=[
            _build_spreads_chip('COB', latest_date.strftime('%Y-%m-%d'), tone='primary'),
            _build_spreads_chip(
                'Compare',
                resolved_comparison_date.strftime('%Y-%m-%d') if resolved_comparison_date is not None else None,
            ),
            _build_spreads_chip('Group', grouping_mode.capitalize()),
            _build_spreads_chip('Rows', len(table_data)),
        ],
    )
    return html.Div([panel], className='spreads-table-stack')


# Callback 5: Download data
@callback(
    Output('spread-download-excel', 'data'),
    Input('spread-download-button', 'n_clicks'),
    State('spread-data-store', 'data'),
    prevent_initial_call=True
)
def download_spread_data(n_clicks, data):
    """Download spread data to Excel"""
    if data is None or (isinstance(data, dict) and data.get('error')):
        return None

    df = pd.DataFrame(data)

    # Create filename with timestamp
    timestamp = pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')
    filename = f'tfu_hh_spread_{timestamp}.xlsx'

    return dcc.send_data_frame(df.to_excel, filename, index=False, sheet_name='Spreads')
