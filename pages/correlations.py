import io
import logging

import dash
from dash import Input, Output, State, callback, dcc, html
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from dash_utils import triggered_id
from market_data import FORWARD_CURVE_PRODUCTS, clear_forward_curve_cache, load_forward_curves
from source_status import make_source_status


PRODUCTS = list(FORWARD_CURVE_PRODUCTS)
MIN_OBSERVATIONS = 10
GROUPING_OPTIONS = [
    {'label': 'Monthly', 'value': 'monthly'},
    {'label': 'Quarterly', 'value': 'quarterly'},
    {'label': 'Seasonal', 'value': 'seasonal'},
    {'label': 'Calendar', 'value': 'calendar'},
]
HISTORY_OPTIONS = [
    {'label': '3M', 'value': '3M'},
    {'label': '6M', 'value': '6M'},
    {'label': '1Y', 'value': '1Y'},
    {'label': 'All', 'value': 'ALL'},
]
ROLLING_OPTIONS = [
    {'label': '10D', 'value': 10},
    {'label': '20D', 'value': 20},
    {'label': '60D', 'value': 60},
]
CHART_CONFIG = {'displaylogo': False, 'responsive': True, 'displayModeBar': 'hover'}
LOGGER = logging.getLogger(__name__)


def _history_window(history_range, end_date=None):
    end = pd.Timestamp(end_date or pd.Timestamp.now()).normalize()
    offsets = {
        '3M': pd.DateOffset(months=3),
        '6M': pd.DateOffset(months=6),
        '1Y': pd.DateOffset(years=1),
        'ALL': pd.DateOffset(years=4),
    }
    return end - offsets.get(history_range or '1Y', offsets['1Y']), end


def assign_delivery_periods(curves, grouping):
    data = curves.copy()
    maturity = pd.to_datetime(data['maturity_date'], errors='coerce')
    data = data[maturity.notna()].copy()
    maturity = pd.to_datetime(data['maturity_date'])
    grouping = grouping or 'monthly'

    if grouping == 'monthly':
        data['period'] = maturity.dt.strftime('%Y-%m')
        data['period_start'] = maturity.dt.to_period('M').dt.to_timestamp()
        data['required_months'] = 1
    elif grouping == 'quarterly':
        quarter = maturity.dt.to_period('Q')
        data['period'] = quarter.astype(str).str.replace('Q', '-Q', regex=False)
        data['period_start'] = quarter.dt.start_time
        data['required_months'] = 3
    elif grouping == 'calendar':
        data['period'] = 'Cal-' + maturity.dt.year.astype(str)
        data['period_start'] = pd.to_datetime(maturity.dt.year.astype(str) + '-01-01')
        data['required_months'] = 12
    elif grouping == 'seasonal':
        months = maturity.dt.month
        years = maturity.dt.year
        is_summer = months.between(5, 9)
        winter_year = years.where(months >= 10, years - 1)
        data['period'] = np.where(
            is_summer,
            'Summer ' + years.astype(str),
            'Winter ' + winter_year.astype(str) + '/' + (winter_year + 1).astype(str).str[-2:],
        )
        data['period_start'] = pd.to_datetime(
            np.where(
                is_summer,
                years.astype(str) + '-05-01',
                winter_year.astype(str) + '-10-01',
            )
        )
        data['required_months'] = np.where(is_summer, 5, 7)
    else:
        raise ValueError(f'Unsupported grouping: {grouping}')

    data['delivery_month'] = maturity.dt.to_period('M').dt.to_timestamp()
    return data


def group_forward_prices(curves, grouping):
    if curves is None or curves.empty:
        return pd.DataFrame(
            columns=['trade_date', 'product', 'period', 'period_start', 'price', 'delivery_months']
        )
    data = assign_delivery_periods(curves, grouping)
    grouped = (
        data.groupby(
            ['trade_date', 'product', 'period', 'period_start', 'required_months'],
            as_index=False,
        )
        .agg(price=('price', 'mean'), delivery_months=('delivery_month', 'nunique'))
    )
    grouped = grouped[grouped['delivery_months'] >= grouped['required_months']].copy()
    return grouped.sort_values(['period_start', 'trade_date', 'product']).reset_index(drop=True)


def period_options(grouped, selected_products):
    if grouped.empty:
        return []
    selected = grouped[grouped['product'].isin(selected_products or PRODUCTS)].copy()
    if selected.empty:
        return []
    latest_date = selected['trade_date'].max()
    latest = selected[selected['trade_date'] == latest_date]
    coverage = (
        latest.groupby(['period', 'period_start'], as_index=False)['product']
        .nunique()
        .rename(columns={'product': 'product_count'})
        .sort_values(['period_start', 'period'])
    )
    return [
        {
            'label': f"{row.period} · {int(row.product_count)}/{len(selected_products or PRODUCTS)} markets",
            'value': row.period,
        }
        for row in coverage.itertuples(index=False)
    ]


def calculate_correlation_analysis(grouped, period, selected_products, pair_a, pair_b, rolling_window):
    selected = grouped[
        grouped['period'].eq(period) & grouped['product'].isin(selected_products or PRODUCTS)
    ].copy()
    prices = selected.pivot_table(index='trade_date', columns='product', values='price', aggfunc='first').sort_index()
    prices = prices.reindex(columns=selected_products or PRODUCTS)
    returns = np.log(prices).diff().replace([np.inf, -np.inf], np.nan)

    present = returns.notna().astype(np.int64)
    overlap = present.T.dot(present).astype('Int64')
    correlations = returns.corr(min_periods=MIN_OBSERVATIONS)

    pair = returns[[pair_a, pair_b]].dropna() if pair_a in returns and pair_b in returns else pd.DataFrame()
    regression = {'correlation': np.nan, 'beta': np.nan, 'intercept': np.nan, 'r_squared': np.nan}
    if len(pair) >= MIN_OBSERVATIONS and pair[pair_a].std() > 0 and pair[pair_b].std() > 0:
        correlation = float(pair[pair_a].corr(pair[pair_b]))
        beta, intercept = np.polyfit(pair[pair_a], pair[pair_b], 1)
        regression = {
            'correlation': correlation,
            'beta': float(beta),
            'intercept': float(intercept),
            'r_squared': correlation * correlation,
        }

    window = max(int(rolling_window or 20), 2)
    rolling = pd.Series(dtype=float, name='rolling_correlation')
    if pair_a in returns and pair_b in returns:
        rolling = returns[pair_a].rolling(window, min_periods=max(MIN_OBSERVATIONS, window // 2)).corr(
            returns[pair_b]
        )
        rolling.name = 'rolling_correlation'

    return {
        'prices': prices,
        'returns': returns,
        'correlations': correlations,
        'overlap': overlap,
        'pair': pair,
        'rolling': rolling,
        'regression': regression,
    }


def _empty_figure(message):
    figure = go.Figure()
    figure.update_layout(
        template='plotly_white',
        xaxis={'visible': False},
        yaxis={'visible': False},
        margin=dict(l=20, r=20, t=20, b=20),
    )
    figure.add_annotation(text=message, x=0.5, y=0.5, showarrow=False, xref='paper', yref='paper')
    return figure


def _matrix_figure(analysis):
    matrix = analysis['correlations']
    if matrix.empty:
        return _empty_figure('Insufficient overlapping returns for a correlation matrix.')
    off_diagonal = matrix.copy()
    np.fill_diagonal(off_diagonal.values, np.nan)
    if not off_diagonal.notna().any().any():
        return _empty_figure('Insufficient overlapping returns for a correlation matrix.')
    overlap = analysis['overlap'].reindex(index=matrix.index, columns=matrix.columns)
    hover = np.empty(matrix.shape, dtype=object)
    for row_index, row_name in enumerate(matrix.index):
        for column_index, column_name in enumerate(matrix.columns):
            value = matrix.iloc[row_index, column_index]
            observations = overlap.iloc[row_index, column_index]
            hover[row_index, column_index] = (
                f'{row_name} / {column_name}<br>Correlation: '
                f'{value:.3f}<br>Overlapping returns: {observations}'
                if pd.notna(value)
                else f'{row_name} / {column_name}<br>Insufficient overlap: {observations}'
            )
    figure = go.Figure(
        go.Heatmap(
            z=matrix.to_numpy(dtype=float),
            x=matrix.columns,
            y=matrix.index,
            zmin=-1,
            zmax=1,
            zmid=0,
            colorscale='RdBu',
            reversescale=True,
            text=hover,
            hovertemplate='%{text}<extra></extra>',
            colorbar={'title': 'ρ'},
        )
    )
    figure.update_layout(template='plotly_white', margin=dict(l=70, r=30, t=20, b=55), height=410)
    return figure


def _rolling_figure(analysis, pair_a, pair_b, rolling_window):
    rolling = analysis['rolling'].dropna()
    if rolling.empty:
        return _empty_figure(
            f'Not enough observations for a {rolling_window}-day rolling correlation.'
        )
    figure = go.Figure(go.Scatter(x=rolling.index, y=rolling, mode='lines', name=f'{pair_a}/{pair_b}'))
    figure.add_hline(y=0, line_color='#94a3b8', line_width=1)
    figure.update_layout(
        template='plotly_white',
        margin=dict(l=55, r=20, t=20, b=45),
        height=360,
        yaxis={'range': [-1, 1], 'title': 'Rolling ρ'},
        xaxis={'title': 'COB'},
        showlegend=False,
    )
    return figure


def _scatter_figure(analysis, pair_a, pair_b):
    pair = analysis['pair']
    regression = analysis['regression']
    if len(pair) < MIN_OBSERVATIONS or pd.isna(regression['beta']):
        return _empty_figure(
            f'Need at least {MIN_OBSERVATIONS} non-constant overlapping returns for regression.'
        )
    figure = go.Figure(
        go.Scatter(
            x=pair[pair_a] * 100,
            y=pair[pair_b] * 100,
            mode='markers',
            marker={'size': 7, 'color': '#2563eb', 'opacity': 0.72},
            name='Daily returns',
            text=pair.index.strftime('%Y-%m-%d'),
            hovertemplate='%{text}<br>' + pair_a + ': %{x:.2f}%<br>' + pair_b + ': %{y:.2f}%<extra></extra>',
        )
    )
    x_values = np.array([pair[pair_a].min(), pair[pair_a].max()])
    y_values = regression['intercept'] + regression['beta'] * x_values
    figure.add_trace(
        go.Scatter(x=x_values * 100, y=y_values * 100, mode='lines', name='OLS fit', line={'color': '#dc2626'})
    )
    figure.update_layout(
        template='plotly_white',
        margin=dict(l=55, r=20, t=20, b=55),
        height=360,
        xaxis={'title': f'{pair_a} daily log return (%)'},
        yaxis={'title': f'{pair_b} daily log return (%)'},
    )
    return figure


def _load_grouped_data(history_range, grouping, products, refresh_clicks=0):
    start, end = _history_window(history_range)
    curves = load_forward_curves(start, end, products=products, force=bool(refresh_clicks))
    return curves, group_forward_prices(curves, grouping)


def _stat_chip(label, value, tone='neutral'):
    return html.Div(
        [html.Span(label, className='correlations-stat-label'), html.Strong(value)],
        className=f'correlations-stat-chip correlations-stat-{tone}',
    )


layout = html.Div(
    [
        dcc.Download(id='correlations-download'),
        html.Div(
            [
                html.Div([html.Label('Markets'), dcc.Dropdown(id='correlations-products', options=[{'label': p, 'value': p} for p in PRODUCTS], value=PRODUCTS, multi=True)]),
                html.Div([html.Label('Delivery grouping'), dcc.Dropdown(id='correlations-grouping', options=GROUPING_OPTIONS, value='monthly', clearable=False)]),
                html.Div([html.Label('Delivery period'), dcc.Dropdown(id='correlations-period', clearable=False)]),
                html.Div([html.Label('History'), dcc.RadioItems(id='correlations-history', options=HISTORY_OPTIONS, value='1Y', inline=True)]),
                html.Div([html.Label('Pair X'), dcc.Dropdown(id='correlations-pair-a', clearable=False)]),
                html.Div([html.Label('Pair Y'), dcc.Dropdown(id='correlations-pair-b', clearable=False)]),
                html.Div([html.Label('Rolling window'), dcc.Dropdown(id='correlations-window', options=ROLLING_OPTIONS, value=20, clearable=False)]),
                html.Button('Export Excel', id='correlations-export', className='btn-refresh'),
            ],
            className='correlations-filter-bar',
        ),
        html.Div(
            id='correlations-source-status',
            className='correlations-status',
            role='status',
        ),
        html.Div(id='correlations-status', className='correlations-status'),
        html.Div(
            'Returns use each market\'s native settlement price with no FX or unit conversion. Correlation is scale-invariant, but currency moves remain embedded.',
            className='correlations-info correlations-method-note',
        ),
        html.Div(id='correlations-stat-strip', className='correlations-stat-strip'),
        html.Div(
            [
                html.Div([html.H3('Return Correlation Matrix'), dcc.Graph(id='correlations-matrix', config=CHART_CONFIG, style={'height': '410px'})], className='correlations-card'),
                html.Div([html.H3('Rolling Correlation'), dcc.Graph(id='correlations-rolling', config=CHART_CONFIG, style={'height': '360px'})], className='correlations-card'),
                html.Div([html.H3('Return Scatter and OLS'), dcc.Graph(id='correlations-scatter', config=CHART_CONFIG, style={'height': '360px'})], className='correlations-card'),
            ],
            className='correlations-grid',
        ),
    ],
    className='options-dashboard-container correlations-page',
)


@callback(
    Output('correlations-period', 'options'),
    Output('correlations-period', 'value'),
    Output('correlations-pair-a', 'options'),
    Output('correlations-pair-a', 'value'),
    Output('correlations-pair-b', 'options'),
    Output('correlations-pair-b', 'value'),
    Output('correlations-source-status', 'children'),
    Input('correlations-grouping', 'value'),
    Input('correlations-products', 'value'),
    Input('correlations-history', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    State('correlations-period', 'value'),
    State('correlations-pair-a', 'value'),
    State('correlations-pair-b', 'value'),
)
def update_correlation_controls(grouping, products, history_range, refresh_clicks, current_period, pair_a, pair_b):
    products = products or []
    pair_options = [{'label': product, 'value': product} for product in products]
    resolved_a = pair_a if pair_a in products else (products[0] if products else None)
    remaining = [product for product in products if product != resolved_a]
    resolved_b = pair_b if pair_b in remaining else (remaining[0] if remaining else None)
    if refresh_clicks and triggered_id() in {None, 'refresh-options-data'}:
        clear_forward_curve_cache()
    try:
        _, grouped = _load_grouped_data(history_range, grouping, products, 0)
    except Exception:
        LOGGER.exception('Correlation source loading failed')
        warning = html.Div(
            'Correlation source is unavailable.',
            className='correlations-warning',
        )
        return [], None, pair_options, resolved_a, pair_options, resolved_b, warning
    options = period_options(grouped, products)
    option_values = [option['value'] for option in options]
    resolved_period = current_period if current_period in option_values else (option_values[0] if option_values else None)
    return options, resolved_period, pair_options, resolved_a, pair_options, resolved_b, None


@callback(
    Output('correlations-matrix', 'figure'),
    Output('correlations-rolling', 'figure'),
    Output('correlations-scatter', 'figure'),
    Output('correlations-stat-strip', 'children'),
    Output('correlations-status', 'children'),
    Input('correlations-period', 'value'),
    Input('correlations-products', 'value'),
    Input('correlations-pair-a', 'value'),
    Input('correlations-pair-b', 'value'),
    Input('correlations-window', 'value'),
    Input('correlations-grouping', 'value'),
    Input('correlations-history', 'value'),
)
def update_correlation_analysis(period, products, pair_a, pair_b, rolling_window, grouping, history_range):
    if not period or len(products or []) < 2 or not pair_a or not pair_b or pair_a == pair_b:
        empty = _empty_figure('Select a delivery period and two different markets.')
        return empty, empty, empty, [], html.Div('Correlation selection is incomplete.', className='correlations-warning')
    curves, grouped = _load_grouped_data(history_range, grouping, products, 0)
    analysis = calculate_correlation_analysis(grouped, period, products, pair_a, pair_b, rolling_window)
    pair = analysis['pair']
    regression = analysis['regression']
    latest_cob = curves['trade_date'].max() if not curves.empty else None
    status = make_source_status('at_lng.curve / transformed.enverus.curve', latest_cob)
    observation_tone = 'warning' if len(pair) < 30 else 'success'
    stats = [
        _stat_chip('Period', period),
        _stat_chip('Pair observations', f'{len(pair)}', observation_tone),
        _stat_chip('Correlation', 'n/a' if pd.isna(regression['correlation']) else f"{regression['correlation']:.3f}", observation_tone),
        _stat_chip('Beta', 'n/a' if pd.isna(regression['beta']) else f"{regression['beta']:.3f}"),
        _stat_chip('R²', 'n/a' if pd.isna(regression['r_squared']) else f"{regression['r_squared']:.3f}"),
        _stat_chip('Latest curve COB', status.latest_cob or 'n/a', 'warning' if (status.business_day_age or 0) > 2 else 'neutral'),
    ]
    if len(pair) < MIN_OBSERVATIONS:
        status_component = html.Div(
            f'Insufficient overlap for {pair_a}/{pair_b}: {len(pair)} returns; minimum is {MIN_OBSERVATIONS}.',
            className='correlations-warning',
        )
    else:
        date_range = f"{pair.index.min():%Y-%m-%d} to {pair.index.max():%Y-%m-%d}"
        status_component = html.Div(
            f'Tenor-aligned daily log returns · {date_range} · Source age {status.business_day_age} business days. '
            'Blank cells mean insufficient overlap or zero variance.',
            className='correlations-info',
        )
    return (
        _matrix_figure(analysis),
        _rolling_figure(analysis, pair_a, pair_b, rolling_window),
        _scatter_figure(analysis, pair_a, pair_b),
        stats,
        status_component,
    )


@callback(
    Output('correlations-download', 'data'),
    Input('correlations-export', 'n_clicks'),
    State('correlations-period', 'value'),
    State('correlations-products', 'value'),
    State('correlations-pair-a', 'value'),
    State('correlations-pair-b', 'value'),
    State('correlations-window', 'value'),
    State('correlations-grouping', 'value'),
    State('correlations-history', 'value'),
    prevent_initial_call=True,
)
def export_correlations(n_clicks, period, products, pair_a, pair_b, rolling_window, grouping, history_range):
    if not n_clicks or not period or not pair_a or not pair_b:
        return dash.no_update
    curves, grouped = _load_grouped_data(history_range, grouping, products, 0)
    analysis = calculate_correlation_analysis(grouped, period, products, pair_a, pair_b, rolling_window)
    output = io.BytesIO()
    metadata = pd.DataFrame(
        [
            {'field': 'period', 'value': period},
            {'field': 'grouping', 'value': grouping},
            {'field': 'pair', 'value': f'{pair_a}/{pair_b}'},
            {'field': 'rolling_window', 'value': rolling_window},
            {'field': 'source_latest_cob', 'value': curves['trade_date'].max() if not curves.empty else None},
            {'field': 'return_method', 'value': 'daily log return'},
            {'field': 'minimum_observations', 'value': MIN_OBSERVATIONS},
        ]
    )
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        analysis['correlations'].to_excel(writer, sheet_name='Correlation Matrix')
        analysis['overlap'].to_excel(writer, sheet_name='Overlap Counts')
        analysis['prices'].to_excel(writer, sheet_name='Aligned Prices')
        analysis['returns'].to_excel(writer, sheet_name='Log Returns')
        analysis['pair'].to_excel(writer, sheet_name='Selected Pair')
        metadata.to_excel(writer, sheet_name='Metadata', index=False)
    filename = f"market_correlations_{period.replace('/', '-')}_{pd.Timestamp.now():%Y%m%d}.xlsx"
    return dcc.send_bytes(output.getvalue(), filename)
