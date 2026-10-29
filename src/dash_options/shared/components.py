"""Shared presentation helpers for portfolio analytics pages."""

import pandas as pd
import plotly.graph_objects as go
from dash import html


CHART_CONFIG = {
    'displaylogo': False,
    'responsive': True,
    'displayModeBar': 'hover',
}


def empty_figure(message):
    figure = go.Figure()
    figure.update_layout(
        template='plotly_white',
        xaxis={'visible': False},
        yaxis={'visible': False},
        margin=dict(l=20, r=20, t=20, b=20),
    )
    figure.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        showarrow=False,
        xref='paper',
        yref='paper',
    )
    return figure


def money(value, currency):
    return f'{float(value or 0):,.2f} {currency}'


def stat(label, value, tone='neutral'):
    return html.Div(
        [html.Span(label, className='analytics-stat-label'), html.Strong(value)],
        className=f'analytics-stat analytics-stat-{tone}',
    )


def grid_payload(aggregated, grouping, value_columns, headers):
    if aggregated.empty:
        return [], []

    display = aggregated.copy()
    display[list(value_columns)] = display[list(value_columns)].apply(
        pd.to_numeric,
        errors='coerce',
    ).round(2)
    column_order = ['currency', grouping, *value_columns]
    resolved_headers = {
        'currency': 'Currency',
        grouping: headers.get(grouping, grouping),
        **headers,
    }
    columns = []
    for column in column_order:
        definition = {
            'field': column,
            'headerName': resolved_headers.get(column, column),
            'sortable': True,
            'filter': True,
            'resizable': True,
            'minWidth': 170 if column == grouping else 118,
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
                            {
                                'condition': 'params.value < 0',
                                'style': {'color': '#b91c1c'},
                            },
                            {
                                'condition': 'params.value > 0',
                                'style': {'color': '#15803d'},
                            },
                        ]
                    },
                }
            )
        columns.append(definition)
    return display[column_order].to_dict('records'), columns
