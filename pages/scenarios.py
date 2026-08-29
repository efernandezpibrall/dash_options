import io

import dash
import dash_ag_grid as dag
from dash import Input, Output, State, callback, dcc, html
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from analytics_components import (
    CHART_CONFIG,
    empty_figure as _empty_figure,
    grid_payload,
    money as _money,
    stat as _stat,
)
from valuation_analytics import (
    GROUPING_LABELS,
    ValuationDataError,
    aggregate_scenario,
    available_valuation_dates,
    clear_valuation_analytics_cache,
    load_valuation_snapshot,
    resolve_currency,
    run_portfolio_scenario,
    select_currency,
)


PNL_COLUMNS = [
    'base_value',
    'shocked_value',
    'exact_pnl',
    'delta_pnl',
    'gamma_pnl',
    'vega_pnl',
    'correlation_pnl',
    'theta_pnl',
    'rate_pnl',
    'interaction_residual',
]


def _component_waterfall(results, currency):
    if results.empty:
        return _empty_figure('No valid positions for this scenario.')
    totals = results.sum(numeric_only=True)
    labels = ['Delta', 'Gamma', 'Vega', 'Correlation', 'Theta', 'Rate', 'Non-linear / cross']
    columns = [
        'delta_pnl',
        'gamma_pnl',
        'vega_pnl',
        'correlation_pnl',
        'theta_pnl',
        'rate_pnl',
        'interaction_residual',
    ]
    values = [float(totals.get(column, 0)) for column in columns]
    figure = go.Figure(
        go.Waterfall(
            x=labels + ['Exact scenario P&L'],
            y=values + [float(totals.get('exact_pnl', 0))],
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
        height=390,
        margin=dict(l=60, r=20, t=20, b=70),
        yaxis={'title': f'Portfolio P&L ({currency})'},
        showlegend=False,
    )
    return figure


def _group_figure(aggregated, grouping, currency):
    if aggregated.empty:
        return _empty_figure('No scenario results to group.')
    figure = go.Figure(
        go.Bar(
            x=aggregated[grouping],
            y=aggregated['exact_pnl'],
            marker_color=np.where(aggregated['exact_pnl'] >= 0, '#16a34a', '#dc2626'),
            text=aggregated['exact_pnl'].map(lambda value: f'{value:,.2f}'),
            textposition='outside',
            hovertemplate=(
                f'%{{x}}<br>Exact P&L: %{{y:,.2f}} '
                f'{currency}<extra></extra>'
            ),
        )
    )
    figure.update_layout(
        template='plotly_white',
        height=390,
        margin=dict(l=60, r=20, t=20, b=90),
        yaxis={'title': f'Exact scenario P&L ({currency})'},
        xaxis={'title': GROUPING_LABELS.get(grouping, grouping), 'tickangle': -25},
    )
    return figure


def _grid_payload(aggregated, grouping):
    headers = {
        grouping: GROUPING_LABELS.get(grouping, grouping),
        'base_value': 'Base value',
        'shocked_value': 'Shocked value',
        'exact_pnl': 'Exact P&L',
        'delta_pnl': 'Delta',
        'gamma_pnl': 'Gamma',
        'vega_pnl': 'Vega',
        'correlation_pnl': 'Correlation',
        'theta_pnl': 'Theta',
        'rate_pnl': 'Rate',
        'interaction_residual': 'Non-linear / cross',
    }
    return grid_payload(aggregated, grouping, PNL_COLUMNS, headers)


def _scenario_frame(cob_date, strategies, currency, shocks):
    snapshot = load_valuation_snapshot(cob_date)
    snapshot = select_currency(snapshot, currency)
    if strategies:
        snapshot = snapshot[snapshot['substrategy'].isin(strategies)].copy()
    return run_portfolio_scenario(snapshot, **shocks)


layout = html.Div(
    [
        dcc.Download(id='scenario-download'),
        html.Div(
            [
                html.Div([html.Label('Valuation COB'), dcc.Dropdown(id='scenario-cob', clearable=False)]),
                html.Div([html.Label('Strategies'), dcc.Dropdown(id='scenario-strategies', multi=True)]),
                html.Div(
                    [
                        html.Label('Currency'),
                        dcc.Dropdown(
                            id='scenario-currency',
                            clearable=False,
                        ),
                    ]
                ),
                html.Div(
                    [
                        html.Label('Group result by'),
                        dcc.Dropdown(
                            id='scenario-grouping',
                            options=[{'label': label, 'value': value} for value, label in GROUPING_LABELS.items()],
                            value='substrategy',
                            clearable=False,
                        ),
                    ]
                ),
                html.Div([html.Label('Asset A move (%)'), dcc.Input(id='scenario-price-a', type='number', value=0, step=0.5)]),
                html.Div([html.Label('Asset B move (%)'), dcc.Input(id='scenario-price-b', type='number', value=0, step=0.5)]),
                html.Div([html.Label('Asset A vol (points)'), dcc.Input(id='scenario-vol-a', type='number', value=0, step=1)]),
                html.Div([html.Label('Asset B vol (points)'), dcc.Input(id='scenario-vol-b', type='number', value=0, step=1)]),
                html.Div([html.Label('Correlation (points)'), dcc.Input(id='scenario-correlation', type='number', value=0, step=1)]),
                html.Div([html.Label('Zero-base rate (bps)'), dcc.Input(id='scenario-rate', type='number', value=0, step=25)]),
                html.Div([html.Label('Time forward (business days)'), dcc.Input(id='scenario-days', type='number', value=0, min=0, step=1)]),
                html.Button('Run Scenario', id='scenario-run', className='btn-refresh'),
                html.Button('Export Excel', id='scenario-export', className='btn-refresh'),
            ],
            className='analytics-filter-bar',
        ),
        html.Div(
            'Full Kirk revaluation in one selected native currency. No FX '
            'conversion or cross-currency aggregation is performed. Rate is a '
            'separate discount overlay because the production Kirk valuation '
            'is undiscounted.',
            className='analytics-model-note',
        ),
        html.Div(id='scenario-status', className='analytics-status'),
        html.Div(id='scenario-stats', className='analytics-stat-strip'),
        html.Div(
            [
                html.Div([html.H3('Scenario P&L Attribution'), dcc.Graph(id='scenario-waterfall', config=CHART_CONFIG, style={'height': '390px'})], className='analytics-card'),
                html.Div([html.H3('P&L by Selected Group'), dcc.Graph(id='scenario-groups-chart', config=CHART_CONFIG, style={'height': '390px'})], className='analytics-card'),
            ],
            className='analytics-two-column',
        ),
        html.Div(
            [
                html.H3('Scenario Results'),
                dag.AgGrid(
                    id='scenario-grid',
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
    Output('scenario-cob', 'options'),
    Output('scenario-cob', 'value'),
    Output('scenario-strategies', 'options'),
    Output('scenario-strategies', 'value'),
    Output('scenario-currency', 'options'),
    Output('scenario-currency', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    State('scenario-cob', 'value'),
    State('scenario-strategies', 'value'),
    State('scenario-currency', 'value'),
)
def update_scenario_selectors(
    refresh_clicks,
    current_cob,
    current_strategies,
    current_currency,
):
    try:
        if refresh_clicks:
            clear_valuation_analytics_cache()
        dates = available_valuation_dates()
        resolved_cob = current_cob if current_cob in dates else (dates[0] if dates else None)
        snapshot = load_valuation_snapshot(resolved_cob) if resolved_cob else pd.DataFrame()
        strategies = sorted(snapshot['substrategy'].dropna().unique()) if not snapshot.empty else []
        selected = [value for value in (current_strategies or strategies) if value in strategies]
        currencies = sorted(
            snapshot['currency'].dropna().astype(str).unique()
        )
        currency = resolve_currency(snapshot, current_currency)
        return (
            [{'label': date, 'value': date} for date in dates],
            resolved_cob,
            [{'label': value, 'value': value} for value in strategies],
            selected,
            [{'label': value, 'value': value} for value in currencies],
            currency,
        )
    except ValuationDataError:
        return [], None, [], [], [], None


@callback(
    Output('scenario-waterfall', 'figure'),
    Output('scenario-groups-chart', 'figure'),
    Output('scenario-grid', 'rowData'),
    Output('scenario-grid', 'columnDefs'),
    Output('scenario-stats', 'children'),
    Output('scenario-status', 'children'),
    Input('scenario-run', 'n_clicks'),
    Input('scenario-cob', 'value'),
    Input('scenario-currency', 'value'),
    State('scenario-strategies', 'value'),
    State('scenario-grouping', 'value'),
    State('scenario-price-a', 'value'),
    State('scenario-price-b', 'value'),
    State('scenario-vol-a', 'value'),
    State('scenario-vol-b', 'value'),
    State('scenario-correlation', 'value'),
    State('scenario-rate', 'value'),
    State('scenario-days', 'value'),
)
def update_scenario_results(
    _run_clicks,
    cob_date,
    currency,
    strategies,
    grouping,
    price_a,
    price_b,
    vol_a,
    vol_b,
    correlation,
    rate,
    days,
):
    if not cob_date or not currency:
        empty = _empty_figure('No valuation snapshot selected.')
        return empty, empty, [], [], [], html.Div('No valuation snapshot is available.', className='analytics-warning')
    shocks = {
        'price_a_pct': price_a,
        'price_b_pct': price_b,
        'vol_a_points': vol_a,
        'vol_b_points': vol_b,
        'correlation_points': correlation,
        'rate_bps': rate,
        'business_days_forward': days,
    }
    try:
        results, invalid = _scenario_frame(
            cob_date,
            strategies,
            currency,
            shocks,
        )
    except (ValuationDataError, ValueError, FloatingPointError) as exc:
        empty = _empty_figure('Scenario calculation failed validation.')
        return empty, empty, [], [], [], html.Div(str(exc), className='analytics-warning')
    aggregated = aggregate_scenario(results, grouping)
    rows, columns = _grid_payload(aggregated, grouping)
    totals = results.sum(numeric_only=True) if not results.empty else pd.Series(dtype=float)
    exact = float(totals.get('exact_pnl', 0))
    residual = float(totals.get('interaction_residual', 0))
    stats = [
        _stat('Positions revalued', f'{len(results):,}', 'success' if len(results) else 'warning'),
        _stat('Positions excluded', f'{len(invalid):,}', 'warning' if len(invalid) else 'neutral'),
        _stat('Base value', _money(totals.get('base_value', 0), currency)),
        _stat('Shocked value', _money(totals.get('shocked_value', 0), currency)),
        _stat('Exact P&L', _money(exact, currency), 'success' if exact >= 0 else 'warning'),
        _stat('Non-linear / cross', _money(residual, currency), 'warning' if abs(residual) > max(abs(exact) * 0.1, 1) else 'neutral'),
    ]
    status = html.Div(
        f'{cob_date} valuation snapshot · {currency} only · '
        f'{len(results)} Kirk spread positions · '
        f'{len(invalid)} excluded with explicit validation errors. Exact P&L is the full revaluation; component P&L is a local approximation.',
        className='analytics-info',
    )
    return (
        _component_waterfall(results, currency),
        _group_figure(aggregated, grouping, currency),
        rows,
        columns,
        stats,
        status,
    )


@callback(
    Output('scenario-download', 'data'),
    Input('scenario-export', 'n_clicks'),
    State('scenario-cob', 'value'),
    State('scenario-strategies', 'value'),
    State('scenario-currency', 'value'),
    State('scenario-grouping', 'value'),
    State('scenario-price-a', 'value'),
    State('scenario-price-b', 'value'),
    State('scenario-vol-a', 'value'),
    State('scenario-vol-b', 'value'),
    State('scenario-correlation', 'value'),
    State('scenario-rate', 'value'),
    State('scenario-days', 'value'),
    prevent_initial_call=True,
)
def export_scenario(
    n_clicks,
    cob_date,
    strategies,
    currency,
    grouping,
    price_a,
    price_b,
    vol_a,
    vol_b,
    correlation,
    rate,
    days,
):
    if not n_clicks or not cob_date or not currency:
        return dash.no_update
    shocks = {
        'price_a_pct': price_a,
        'price_b_pct': price_b,
        'vol_a_points': vol_a,
        'vol_b_points': vol_b,
        'correlation_points': correlation,
        'rate_bps': rate,
        'business_days_forward': days,
    }
    try:
        results, invalid = _scenario_frame(
            cob_date,
            strategies,
            currency,
            shocks,
        )
    except ValuationDataError:
        return dash.no_update
    output = io.BytesIO()
    assumptions = pd.DataFrame(
        [
            {'assumption': key, 'value': value}
            for key, value in {
                'cob_date': cob_date,
                'currency': currency,
                'grouping': grouping,
                **shocks,
            }.items()
        ]
    )
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        aggregate_scenario(results, grouping).to_excel(writer, sheet_name='Summary', index=False)
        results.to_excel(writer, sheet_name='Position Detail', index=False)
        invalid.to_excel(writer, sheet_name='Excluded Positions', index=False)
        assumptions.to_excel(writer, sheet_name='Assumptions', index=False)
    return dcc.send_bytes(output.getvalue(), f'portfolio_scenario_{cob_date}.xlsx')
