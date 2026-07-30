import io

import dash
import dash_ag_grid as dag
from dash import Input, Output, State, callback, dcc, html
import pandas as pd
import plotly.graph_objects as go

from valuation_analytics import (
    GROUPING_LABELS,
    ValuationDataError,
    aggregate_pnl_explain,
    available_valuation_dates,
    calculate_pnl_explain,
    clear_valuation_analytics_cache,
    load_valuation_snapshot,
    select_currency,
)


CHART_CONFIG = {'displaylogo': False, 'responsive': True, 'displayModeBar': 'hover'}
EXPLAIN_COLUMNS = [
    'actual_pnl',
    'delta_pnl',
    'gamma_pnl',
    'vega_pnl',
    'correlation_pnl',
    'theta_pnl',
    'trade_pnl',
    'explained_pnl',
    'unexplained_pnl',
]


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


def _money(value, currency):
    value = float(value or 0)
    return f'{value:,.2f} {currency}'


def _stat(label, value, tone='neutral'):
    return html.Div(
        [html.Span(label, className='analytics-stat-label'), html.Strong(value)],
        className=f'analytics-stat analytics-stat-{tone}',
    )


def _pnl_waterfall(explain, currency):
    if explain.empty:
        return _empty_figure('No positions are available for attribution.')
    totals = explain.sum(numeric_only=True)
    labels = ['Delta', 'Gamma', 'Vega', 'Correlation', 'Theta', 'Trades / quantity', 'Unexplained']
    columns = ['delta_pnl', 'gamma_pnl', 'vega_pnl', 'correlation_pnl', 'theta_pnl', 'trade_pnl', 'unexplained_pnl']
    values = [float(totals.get(column, 0)) for column in columns]
    figure = go.Figure(
        go.Waterfall(
            x=labels + ['Actual value change'],
            y=values + [float(totals.get('actual_pnl', 0))],
            measure=['relative'] * len(values) + ['total'],
            connector={'line': {'color': '#94a3b8'}},
            increasing={'marker': {'color': '#16a34a'}},
            decreasing={'marker': {'color': '#dc2626'}},
            totals={'marker': {'color': '#2563eb'}},
            hovertemplate=(
                f'%{{x}}<br>%{{y:,.2f}} {currency}<extra></extra>'
            ),
        )
    )
    figure.update_layout(
        template='plotly_white',
        height=400,
        margin=dict(l=60, r=20, t=20, b=80),
        yaxis={'title': f'Value change ({currency})'},
        showlegend=False,
    )
    return figure


def _group_chart(aggregated, grouping, currency):
    if aggregated.empty:
        return _empty_figure('No P&L Explain results to group.')
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=aggregated[grouping],
            y=aggregated['actual_pnl'],
            name='Actual',
            marker_color='#2563eb',
            hovertemplate=(
                f'%{{x}}<br>Actual: %{{y:,.2f}} '
                f'{currency}<extra></extra>'
            ),
        )
    )
    figure.add_trace(
        go.Bar(
            x=aggregated[grouping],
            y=aggregated['explained_pnl'],
            name='Explained',
            marker_color='#16a34a',
            hovertemplate=(
                f'%{{x}}<br>Explained: %{{y:,.2f}} '
                f'{currency}<extra></extra>'
            ),
        )
    )
    figure.update_layout(
        template='plotly_white',
        barmode='group',
        height=400,
        margin=dict(l=60, r=20, t=20, b=90),
        yaxis={'title': f'Value change ({currency})'},
        xaxis={'title': GROUPING_LABELS.get(grouping, grouping), 'tickangle': -25},
        legend={'orientation': 'h', 'y': 1.08},
    )
    return figure


def _grid_payload(aggregated, grouping):
    if aggregated.empty:
        return [], []
    display = aggregated.copy()
    for column in EXPLAIN_COLUMNS:
        display[column] = pd.to_numeric(
            display[column],
            errors='coerce',
        ).round(2)
    headers = {
        grouping: GROUPING_LABELS.get(grouping, grouping),
        'actual_pnl': 'Actual P&L change',
        'delta_pnl': 'Delta',
        'gamma_pnl': 'Gamma',
        'vega_pnl': 'Vega',
        'correlation_pnl': 'Correlation',
        'theta_pnl': 'Theta',
        'trade_pnl': 'Trades / quantity',
        'explained_pnl': 'Explained',
        'unexplained_pnl': 'Unexplained',
    }
    headers['currency'] = 'Currency'
    columns = []
    for column in ['currency', grouping, *EXPLAIN_COLUMNS]:
        definition = {
            'field': column,
            'headerName': headers.get(column, column),
            'sortable': True,
            'filter': True,
            'resizable': True,
            'minWidth': 118 if column != grouping else 170,
        }
        if column not in {grouping, 'currency'}:
            definition.update(
                {
                    'type': 'numericColumn',
                    'valueFormatter': {
                        'function': "d3.format(',.2f')(params.value)"
                    },
                    'cellStyle': {
                        'styleConditions': [
                            {'condition': 'params.value < 0', 'style': {'color': '#b91c1c'}},
                            {'condition': 'params.value > 0', 'style': {'color': '#15803d'}},
                        ]
                    },
                }
            )
        columns.append(definition)
    return (
        display[['currency', grouping, *EXPLAIN_COLUMNS]].to_dict('records'),
        columns,
    )


def _build_explain(previous_date, current_date, strategies, currency):
    previous = load_valuation_snapshot(previous_date)
    current = load_valuation_snapshot(current_date)
    previous = select_currency(previous, currency)
    current = select_currency(current, currency)
    if strategies:
        previous = previous[previous['substrategy'].isin(strategies)].copy()
        current = current[current['substrategy'].isin(strategies)].copy()
    return calculate_pnl_explain(previous, current)


layout = html.Div(
    [
        dcc.Download(id='pnl-explain-download'),
        html.Div(
            [
                html.Div([html.Label('Previous valuation COB'), dcc.Dropdown(id='pnl-explain-previous', clearable=False)]),
                html.Div([html.Label('Current valuation COB'), dcc.Dropdown(id='pnl-explain-current', clearable=False)]),
                html.Div([html.Label('Strategies'), dcc.Dropdown(id='pnl-explain-strategies', multi=True)]),
                html.Div(
                    [
                        html.Label('Currency'),
                        dcc.Dropdown(
                            id='pnl-explain-currency',
                            clearable=False,
                        ),
                    ]
                ),
                html.Div(
                    [
                        html.Label('Group result by'),
                        dcc.Dropdown(
                            id='pnl-explain-grouping',
                            options=[{'label': label, 'value': value} for value, label in GROUPING_LABELS.items()],
                            value='substrategy',
                            clearable=False,
                        ),
                    ]
                ),
                html.Button('Export Excel', id='pnl-explain-export', className='btn-refresh'),
            ],
            className='analytics-filter-bar analytics-filter-bar-compact',
        ),
        html.Div(
            'Attribution uses one selected native currency and the market inputs '
            'embedded in each persisted valuation snapshot. No FX conversion or '
            'cross-currency aggregation is performed.',
            className='analytics-model-note',
        ),
        html.Div(id='pnl-explain-status', className='analytics-status'),
        html.Div(id='pnl-explain-stats', className='analytics-stat-strip'),
        html.Div(
            [
                html.Div([html.H3('Portfolio P&L Explain'), dcc.Graph(id='pnl-explain-waterfall', config=CHART_CONFIG, style={'height': '400px'})], className='analytics-card'),
                html.Div([html.H3('Actual versus Explained'), dcc.Graph(id='pnl-explain-groups-chart', config=CHART_CONFIG, style={'height': '400px'})], className='analytics-card'),
            ],
            className='analytics-two-column',
        ),
        html.Div(
            [
                html.H3('P&L Explain Results'),
                dag.AgGrid(
                    id='pnl-explain-grid',
                    rowData=[],
                    columnDefs=[],
                    defaultColDef={'sortable': True, 'resizable': True},
                    dashGridOptions={'domLayout': 'autoHeight', 'animateRows': False},
                    className='ag-theme-alpine',
                ),
            ],
            className='analytics-card analytics-grid-card',
        ),
    ],
    className='options-dashboard-container analytics-page',
)


@callback(
    Output('pnl-explain-previous', 'options'),
    Output('pnl-explain-previous', 'value'),
    Output('pnl-explain-current', 'options'),
    Output('pnl-explain-current', 'value'),
    Output('pnl-explain-strategies', 'options'),
    Output('pnl-explain-strategies', 'value'),
    Output('pnl-explain-currency', 'options'),
    Output('pnl-explain-currency', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    State('pnl-explain-previous', 'value'),
    State('pnl-explain-current', 'value'),
    State('pnl-explain-strategies', 'value'),
    State('pnl-explain-currency', 'value'),
)
def update_pnl_explain_selectors(
    refresh_clicks,
    previous_date,
    current_date,
    selected_strategies,
    selected_currency,
):
    try:
        if refresh_clicks:
            clear_valuation_analytics_cache()
        dates = available_valuation_dates()
        current = current_date if current_date in dates else (dates[0] if dates else None)
        eligible_previous = tuple(date for date in dates if current and pd.Timestamp(date) < pd.Timestamp(current))
        previous = previous_date if previous_date in eligible_previous else (eligible_previous[0] if eligible_previous else None)
        snapshot = load_valuation_snapshot(current) if current else pd.DataFrame()
        previous_snapshot = (
            load_valuation_snapshot(previous)
            if previous
            else pd.DataFrame()
        )
        strategies = sorted(snapshot['substrategy'].dropna().unique()) if not snapshot.empty else []
        selected = [value for value in (selected_strategies or strategies) if value in strategies]
        current_currencies = set(
            snapshot['currency'].dropna().astype(str)
        )
        previous_currencies = set(
            previous_snapshot['currency'].dropna().astype(str)
        )
        currencies = sorted(current_currencies & previous_currencies)
        currency = (
            selected_currency
            if selected_currency in currencies
            else ('USD' if 'USD' in currencies else (currencies[0] if currencies else None))
        )
        date_options = [{'label': date, 'value': date} for date in dates]
        previous_options = [{'label': date, 'value': date} for date in eligible_previous]
        return (
            previous_options,
            previous,
            date_options,
            current,
            [{'label': value, 'value': value} for value in strategies],
            selected,
            [{'label': value, 'value': value} for value in currencies],
            currency,
        )
    except ValuationDataError:
        return [], None, [], None, [], [], [], None


@callback(
    Output('pnl-explain-waterfall', 'figure'),
    Output('pnl-explain-groups-chart', 'figure'),
    Output('pnl-explain-grid', 'rowData'),
    Output('pnl-explain-grid', 'columnDefs'),
    Output('pnl-explain-stats', 'children'),
    Output('pnl-explain-status', 'children'),
    Input('pnl-explain-previous', 'value'),
    Input('pnl-explain-current', 'value'),
    Input('pnl-explain-strategies', 'value'),
    Input('pnl-explain-currency', 'value'),
    Input('pnl-explain-grouping', 'value'),
)
def update_pnl_explain(
    previous_date,
    current_date,
    strategies,
    currency,
    grouping,
):
    if (
        not previous_date
        or not current_date
        or not currency
        or pd.Timestamp(previous_date) >= pd.Timestamp(current_date)
    ):
        empty = _empty_figure('Select two ordered valuation snapshots.')
        return empty, empty, [], [], [], html.Div('Previous COB must be earlier than current COB.', className='analytics-warning')
    try:
        explain = _build_explain(
            previous_date,
            current_date,
            strategies,
            currency,
        )
    except (ValuationDataError, ValueError) as exc:
        empty = _empty_figure('P&L Explain calculation failed validation.')
        return empty, empty, [], [], [], html.Div(str(exc), className='analytics-warning')
    aggregated = aggregate_pnl_explain(explain, grouping)
    rows, columns = _grid_payload(aggregated, grouping)
    totals = explain.sum(numeric_only=True) if not explain.empty else pd.Series(dtype=float)
    actual = float(totals.get('actual_pnl', 0))
    unexplained = float(totals.get('unexplained_pnl', 0))
    unexplained_ratio = abs(unexplained) / max(abs(actual), 1)
    status_counts = explain['position_status'].value_counts().to_dict() if not explain.empty else {}
    matched_count = int(status_counts.get('Matched', 0))
    price_marks_changed = int(explain['price_mark_changed'].sum()) if not explain.empty else 0
    vol_marks_changed = int(explain['vol_mark_changed'].sum()) if not explain.empty else 0
    correlation_marks_changed = int(explain['correlation_mark_changed'].sum()) if not explain.empty else 0
    stats = [
        _stat('Positions', f'{len(explain):,}'),
        _stat('Matched', f"{status_counts.get('Matched', 0):,}", 'success'),
        _stat(
            'Price marks changed',
            f'{price_marks_changed:,}/{matched_count:,}',
            'warning' if matched_count and price_marks_changed == 0 else 'neutral',
        ),
        _stat('New / closed', f"{status_counts.get('New', 0) + status_counts.get('Closed', 0):,}", 'warning' if status_counts.get('New', 0) + status_counts.get('Closed', 0) else 'neutral'),
        _stat('Actual change', _money(actual, currency), 'success' if actual >= 0 else 'warning'),
        _stat('Explained', _money(totals.get('explained_pnl', 0), currency)),
        _stat('Unexplained', f'{_money(unexplained, currency)} · {unexplained_ratio:.1%}', 'warning' if unexplained_ratio > 0.05 else 'success'),
    ]
    business_days = int(explain['business_days'].iloc[0]) if not explain.empty else 0
    unchanged_price_warning = matched_count and price_marks_changed == 0 and business_days > 1
    status = html.Div(
        f'{previous_date} → {current_date} · {currency} only · '
        f'{business_days} business days · '
        f'{status_counts.get("Matched", 0)} matched, {status_counts.get("New", 0)} new, {status_counts.get("Closed", 0)} closed. '
        f'Changed marks: price {price_marks_changed}/{matched_count}, vol {vol_marks_changed}/{matched_count}, '
        f'correlation {correlation_marks_changed}/{matched_count}. '
        'Delta/gamma/vega/correlation/theta use prior-COB Greeks; unexplained captures higher-order and mark/model effects. '
        + ('Underlying price marks are unchanged across the interval, so delta and gamma attribution are zero; check the stale curve-source warning above.' if unchanged_price_warning else ''),
        className='analytics-warning' if unchanged_price_warning else 'analytics-info',
    )
    return (
        _pnl_waterfall(explain, currency),
        _group_chart(aggregated, grouping, currency),
        rows,
        columns,
        stats,
        status,
    )


@callback(
    Output('pnl-explain-download', 'data'),
    Input('pnl-explain-export', 'n_clicks'),
    State('pnl-explain-previous', 'value'),
    State('pnl-explain-current', 'value'),
    State('pnl-explain-strategies', 'value'),
    State('pnl-explain-currency', 'value'),
    State('pnl-explain-grouping', 'value'),
    prevent_initial_call=True,
)
def export_pnl_explain(
    n_clicks,
    previous_date,
    current_date,
    strategies,
    currency,
    grouping,
):
    if (
        not n_clicks
        or not previous_date
        or not current_date
        or not currency
    ):
        return dash.no_update
    try:
        explain = _build_explain(
            previous_date,
            current_date,
            strategies,
            currency,
        )
    except ValuationDataError:
        return dash.no_update
    output = io.BytesIO()
    metadata = pd.DataFrame(
        [
            {'field': 'previous_cob', 'value': previous_date},
            {'field': 'current_cob', 'value': current_date},
            {'field': 'currency', 'value': currency},
            {'field': 'grouping', 'value': grouping},
            {'field': 'method', 'value': 'prior-COB Greeks with exact stored value-change reconciliation'},
            {'field': 'market_input_source', 'value': 'persisted trades_options_valuation snapshots'},
        ]
    )
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        aggregate_pnl_explain(explain, grouping).to_excel(writer, sheet_name='Summary', index=False)
        explain.to_excel(writer, sheet_name='Position Detail', index=False)
        metadata.to_excel(writer, sheet_name='Metadata', index=False)
    return dcc.send_bytes(output.getvalue(), f'pnl_explain_{previous_date}_to_{current_date}.xlsx')
