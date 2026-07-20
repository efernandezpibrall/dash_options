import dash_ag_grid as dag
from dash import html, dcc, callback, Output, Input, State, ALL, ctx, no_update
import plotly.graph_objects as go
import datetime as dt
from datetime import date, timedelta
import numpy as np

# Import options pricing functions
from options.options_library import (black_76, kirk_model_with_substitution, kirk_spread_greeks)

# Cache for storing calculation results to maintain consistency between results and chart
option_cache = {
    "black76": {"value": None, "params": None},
    "kirk": {"value": None, "params": None}
}

# Define option types
option_types = [
    {"label": "Commodity Options (Black-76)", "value": "black76"},
    {"label": "Spread Options (Kirk)", "value": "kirk"}
]

PRICER_PARAM_ORDER = {
    'black76': ['underlying-price', 'strike-price', 'risk-free-rate', 'volatility'],
    'kirk': [
        'price-asset1',
        'price-asset2',
        'spread-strike',
        'spread-risk-free-rate',
        'volatility-asset1',
        'volatility-asset2',
        'correlation',
    ],
}
PRICER_DATE_PARAM = {
    'black76': 'expiration-date',
    'kirk': 'spread-expiration-date',
}
PRICER_CONTRACT_DATE_PARAM = {
    'black76': 'contract-expiration-date',
    'kirk': 'spread-contract-expiration-date',
}
MAX_PRICER_DECIMALS = 20

# Helper function to parse dates consistently
def parse_date(date_str, default_date=None):
    """Parse date string with consistent error handling"""
    if not date_str:
        return default_date or date.today() + timedelta(days=365)

    if isinstance(date_str, date):
        return date_str

    try:
        return dt.datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        try:
            return dt.datetime.strptime(date_str.split('T')[0], '%Y-%m-%d').date()
        except (ValueError, AttributeError):
            return default_date or date.today() + timedelta(days=365)


def _param_float(values, index, default):
    try:
        value = values[index]
    except (TypeError, IndexError):
        return default

    return _coerce_pricer_float(value, default)


def _ordered_pricer_params(option_type, values, ids=None):
    if not ids:
        return values or []

    by_param = {}
    for value, item_id in zip(values or [], ids or []):
        if not isinstance(item_id, dict):
            continue
        if item_id.get('model') != option_type:
            continue
        by_param[item_id.get('param')] = value

    return [by_param.get(param) for param in PRICER_PARAM_ORDER.get(option_type, [])]


def _ordered_pricer_dates(option_type, dates, ids=None):
    value = _ordered_pricer_date_value(
        option_type,
        dates,
        ids,
        PRICER_DATE_PARAM.get(option_type),
        fallback_index=0,
    )
    return [value] if value else []


def _ordered_pricer_date_value(option_type, dates, ids=None, target_param=None, fallback_index=0):
    if not target_param:
        return None

    if not ids:
        try:
            return (dates or [])[fallback_index]
        except (TypeError, IndexError):
            return None

    for value, item_id in zip(dates or [], ids or []):
        if not isinstance(item_id, dict):
            continue
        if item_id.get('model') == option_type and item_id.get('param') == target_param:
            return value

    return None


def _coerce_pricer_float(value, default=None):
    if value is None or value == '':
        return default

    if isinstance(value, str):
        normalized = _normalize_pricer_number_text(value)
        if normalized is None:
            return default
        value = normalized

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_pricer_number_text(value):
    text = str(value).strip().replace(' ', '')
    if not text or '+' in text or '-' in text:
        return None

    if ',' in text and '.' in text:
        if text.rfind(',') > text.rfind('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    else:
        text = text.replace(',', '.')

    if text.count('.') > 1:
        return None

    integer_part, separator, decimal_part = text.partition('.')
    if not integer_part and not decimal_part:
        return None
    if integer_part and not integer_part.isdigit():
        return None
    if separator and decimal_part and not decimal_part.isdigit():
        return None
    if separator and len(decimal_part) > MAX_PRICER_DECIMALS:
        return None
    if not separator and not integer_part.isdigit():
        return None

    return text


def _parse_pricer_expiration(all_dates, default_date=None):
    if all_dates and all_dates[0]:
        return parse_date(all_dates[0], default_date)
    return default_date or date.today() + timedelta(days=365)


def _parse_pricer_date_value(option_type, all_dates, all_date_ids, target_param, default_date=None, fallback_index=0):
    value = _ordered_pricer_date_value(
        option_type,
        all_dates,
        all_date_ids,
        target_param,
        fallback_index=fallback_index,
    )
    return parse_date(value, default_date) if value else (default_date or date.today() + timedelta(days=365))


def _coerce_pricer_contract_expiration_date(expiration_date, contract_expiration_date):
    return max(expiration_date, contract_expiration_date)


def _sync_pricer_contract_expiration_date(expiration_date_value, contract_expiration_date_value):
    if not expiration_date_value:
        return no_update, no_update

    expiration_date = parse_date(expiration_date_value)
    contract_expiration_date = parse_date(contract_expiration_date_value, expiration_date)
    min_date = expiration_date.strftime('%Y-%m-%d')

    if contract_expiration_date < expiration_date:
        return min_date, min_date

    if not contract_expiration_date_value:
        return min_date, min_date

    return no_update, min_date


def _count_pricer_business_days(start_date, end_date):
    start_date = parse_date(start_date)
    end_date = parse_date(end_date)
    if end_date < start_date:
        return 0

    business_days = 0
    for day_offset in range((end_date - start_date).days + 1):
        if (start_date + timedelta(days=day_offset)).weekday() < 5:
            business_days += 1
    return business_days


def _adjust_pricer_volatility(raw_volatility, option_expiration_date, contract_expiration_date):
    today = date.today()
    option_business_days = _count_pricer_business_days(today, option_expiration_date)
    contract_business_days = _count_pricer_business_days(today, contract_expiration_date)

    if option_business_days <= 0 or contract_business_days <= 0:
        return raw_volatility, 1.0, option_business_days, contract_business_days

    adjustment_factor = float(np.sqrt(option_business_days / contract_business_days))
    return raw_volatility * adjustment_factor, adjustment_factor, option_business_days, contract_business_days


def _parse_black76_model_inputs(all_params, all_dates, all_param_ids=None, all_date_ids=None):
    all_params = _ordered_pricer_params('black76', all_params, all_param_ids)
    S = _param_float(all_params, 0, 100)
    K = _param_float(all_params, 1, 100)
    r = _param_float(all_params, 2, 0.05)
    raw_v = _param_float(all_params, 3, 0.2)
    expiration_date = _parse_pricer_date_value(
        'black76',
        all_dates,
        all_date_ids,
        PRICER_DATE_PARAM['black76'],
        fallback_index=0,
    )
    contract_expiration_date = _parse_pricer_date_value(
        'black76',
        all_dates,
        all_date_ids,
        PRICER_CONTRACT_DATE_PARAM['black76'],
        default_date=expiration_date,
        fallback_index=1,
    )
    contract_expiration_date = _coerce_pricer_contract_expiration_date(
        expiration_date,
        contract_expiration_date,
    )

    if S <= 0:
        S = 100
    if K <= 0:
        K = 100
    if raw_v <= 0:
        raw_v = 0.2

    v, vol_adjustment_factor, option_business_days, contract_business_days = _adjust_pricer_volatility(
        raw_v,
        expiration_date,
        contract_expiration_date,
    )
    if v <= 0:
        v = raw_v

    days_to_expiry = (expiration_date - date.today()).days
    T = max(days_to_expiry / 365.0, 0.001)
    return {
        'S': S,
        'K': K,
        'r': r,
        'raw_v': raw_v,
        'v': v,
        'expiration_date': expiration_date,
        'contract_expiration_date': contract_expiration_date,
        'vol_adjustment_factor': vol_adjustment_factor,
        'option_business_days': option_business_days,
        'contract_business_days': contract_business_days,
        'days_to_expiry': days_to_expiry,
        'T': T,
    }


def _parse_black76_params(all_params, all_dates, all_param_ids=None, all_date_ids=None):
    inputs = _parse_black76_model_inputs(all_params, all_dates, all_param_ids, all_date_ids)
    S = inputs['S']
    K = inputs['K']
    r = inputs['r']
    v = inputs['v']
    expiration_date = inputs['expiration_date']
    days_to_expiry = inputs['days_to_expiry']
    T = inputs['T']
    return S, K, r, v, expiration_date, days_to_expiry, T


def _parse_kirk_model_inputs(all_params, all_dates, all_param_ids=None, all_date_ids=None):
    all_params = _ordered_pricer_params('kirk', all_params, all_param_ids)
    S1 = _param_float(all_params, 0, 100)
    S2 = _param_float(all_params, 1, 90)
    K_spread = _param_float(all_params, 2, 5)
    r_spread = _param_float(all_params, 3, 0.05)
    raw_v1 = _param_float(all_params, 4, 0.2)
    raw_v2 = _param_float(all_params, 5, 0.15)
    rho = _param_float(all_params, 6, 0.5)
    expiration_date = _parse_pricer_date_value(
        'kirk',
        all_dates,
        all_date_ids,
        PRICER_DATE_PARAM['kirk'],
        fallback_index=0,
    )
    contract_expiration_date = _parse_pricer_date_value(
        'kirk',
        all_dates,
        all_date_ids,
        PRICER_CONTRACT_DATE_PARAM['kirk'],
        default_date=expiration_date,
        fallback_index=1,
    )
    contract_expiration_date = _coerce_pricer_contract_expiration_date(
        expiration_date,
        contract_expiration_date,
    )

    if S1 <= 0:
        S1 = 100
    if S2 <= 0:
        S2 = 90
    if raw_v1 <= 0:
        raw_v1 = 0.2
    if raw_v2 <= 0:
        raw_v2 = 0.15
    if rho < -1 or rho > 1:
        rho = 0.5

    v1, vol_adjustment_factor, option_business_days, contract_business_days = _adjust_pricer_volatility(
        raw_v1,
        expiration_date,
        contract_expiration_date,
    )
    v2, _vol_adjustment_factor_2, _option_business_days_2, _contract_business_days_2 = _adjust_pricer_volatility(
        raw_v2,
        expiration_date,
        contract_expiration_date,
    )
    if v1 <= 0:
        v1 = raw_v1
    if v2 <= 0:
        v2 = raw_v2

    days_to_expiry = (expiration_date - date.today()).days
    T_spread = max(days_to_expiry / 365.0, 0.001)
    return {
        'S1': S1,
        'S2': S2,
        'K_spread': K_spread,
        'r_spread': r_spread,
        'raw_v1': raw_v1,
        'raw_v2': raw_v2,
        'v1': v1,
        'v2': v2,
        'rho': rho,
        'expiration_date': expiration_date,
        'contract_expiration_date': contract_expiration_date,
        'vol_adjustment_factor': vol_adjustment_factor,
        'option_business_days': option_business_days,
        'contract_business_days': contract_business_days,
        'days_to_expiry': days_to_expiry,
        'T_spread': T_spread,
    }


def _parse_kirk_params(all_params, all_dates, all_param_ids=None, all_date_ids=None):
    inputs = _parse_kirk_model_inputs(all_params, all_dates, all_param_ids, all_date_ids)
    S1 = inputs['S1']
    S2 = inputs['S2']
    K_spread = inputs['K_spread']
    r_spread = inputs['r_spread']
    v1 = inputs['v1']
    v2 = inputs['v2']
    rho = inputs['rho']
    expiration_date = inputs['expiration_date']
    days_to_expiry = inputs['days_to_expiry']
    T_spread = inputs['T_spread']
    return S1, S2, K_spread, r_spread, v1, v2, rho, expiration_date, days_to_expiry, T_spread


def _get_pricer_triggered_id():
    try:
        return ctx.triggered_id
    except Exception:
        return None


PRICER_CHART_FONT = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
PRICER_CHART_TEXT = '#0f172a'
PRICER_CHART_MUTED = '#64748b'
PRICER_CHART_GRID = 'rgba(148, 163, 184, 0.18)'
PRICER_CHART_AXIS = '#94a3b8'
PRICER_GRAPH_CONFIG = {
    'displayModeBar': 'hover',
    'displaylogo': False,
    'responsive': True,
    'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
}


def _pricer_axis(title='', **overrides):
    axis = {
        'title': dict(text=title, font=dict(size=11, color=PRICER_CHART_MUTED)),
        'showgrid': True,
        'gridcolor': PRICER_CHART_GRID,
        'gridwidth': 1,
        'zeroline': False,
        'linecolor': PRICER_CHART_AXIS,
        'linewidth': 1,
        'tickfont': dict(size=10, color=PRICER_CHART_MUTED),
        'ticks': 'outside',
        'ticklen': 3,
        'automargin': True,
    }
    axis.update(overrides)
    return axis


def _pricer_legend():
    return {
        'orientation': 'h',
        'yanchor': 'top',
        'y': -0.18,
        'xanchor': 'center',
        'x': 0.5,
        'bgcolor': 'rgba(255, 255, 255, 0)',
        'bordercolor': 'rgba(255, 255, 255, 0)',
        'font': dict(size=9, color=PRICER_CHART_MUTED),
        'itemsizing': 'constant',
        'itemwidth': 58,
        'tracegroupgap': 4,
    }


def _style_pricer_figure(fig, height=400):
    fig.update_layout(
        title=dict(text=''),
        font=dict(family=PRICER_CHART_FONT, size=11, color=PRICER_CHART_TEXT),
        plot_bgcolor='#f8fafc',
        paper_bgcolor='white',
        margin=dict(l=52, r=18, t=18, b=88),
        hovermode='x unified',
        hoverlabel=dict(
            bgcolor='rgba(255, 255, 255, 0.96)',
            bordercolor='rgba(148, 163, 184, 0.45)',
            font=dict(size=11, color=PRICER_CHART_TEXT, family=PRICER_CHART_FONT),
            align='left',
        ),
        legend=_pricer_legend(),
        showlegend=True,
        height=height,
        transition=dict(duration=180, easing='cubic-in-out'),
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor=PRICER_CHART_GRID,
        linecolor=PRICER_CHART_AXIS,
        tickfont=dict(size=10, color=PRICER_CHART_MUTED),
        title_font=dict(size=11, color=PRICER_CHART_MUTED),
        automargin=True,
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor=PRICER_CHART_GRID,
        linecolor=PRICER_CHART_AXIS,
        tickfont=dict(size=10, color=PRICER_CHART_MUTED),
        title_font=dict(size=11, color=PRICER_CHART_MUTED),
        automargin=True,
    )
    return fig


def _empty_pricer_figure(message, xaxis_title='', yaxis_title='Option Value'):
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref='paper',
        yref='paper',
        showarrow=False,
        font=dict(size=13, color=PRICER_CHART_MUTED),
    )
    fig.update_layout(
        xaxis=_pricer_axis(xaxis_title),
        yaxis=_pricer_axis(yaxis_title),
    )
    return _style_pricer_figure(fig)


def _build_pricer_section_header(title, actions=None):
    return html.Div(
        [
            html.Div(
                [html.H3(title, className='section-title-inline pricer-section-title')],
                className='pricer-section-title-row',
            ),
            html.Div(actions or [], className='pricer-section-actions'),
        ],
        className='pricer-section-header',
    )


def _build_pricer_chart_header(title):
    return html.Div(
        html.H5(title, className='pricer-chart-card-title'),
        className='pricer-chart-card-header',
    )


def _build_pricer_chart_card(graph_id, title, empty_message, className=None):
    classes = ['pricer-chart-card']
    if className:
        classes.append(className)
    return html.Div(
        [
            _build_pricer_chart_header(title),
            dcc.Graph(
                id=graph_id,
                className='pricer-chart-graph',
                style={'height': '100%'},
                config=PRICER_GRAPH_CONFIG,
                figure=_empty_pricer_figure(empty_message),
            ),
        ],
        className=' '.join(classes),
    )


def _build_pricer_message(message, tone='neutral'):
    return html.Div(message, className=f'pricer-empty-state pricer-empty-state-{tone}')


def _build_pricer_result_card(label, value, detail=None, tone='neutral'):
    return html.Div(
        [
            html.Div(label, className='pricer-result-card-label'),
            html.Div(value, className='pricer-result-card-value'),
            html.Div(detail, className='pricer-result-card-detail') if detail else None,
        ],
        className=f'pricer-result-card pricer-result-card-{tone}',
    )


def _format_pricer_model_input_value(value):
    if value is None:
        return 'N/A'
    if isinstance(value, dt.datetime):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, np.integer, np.floating)):
        numeric_value = float(value)
        if not np.isfinite(numeric_value):
            return 'N/A'
        formatted = np.format_float_positional(
            numeric_value,
            precision=MAX_PRICER_DECIMALS,
            unique=True,
            fractional=False,
            trim='-',
        )
        if formatted in ('', '-0'):
            return '0'
        return formatted
    return str(value)


def _format_pricer_call_put(value):
    normalized = str(value or '').strip().lower()
    if normalized in ('c', 'call'):
        return 'Call'
    if normalized in ('p', 'put'):
        return 'Put'
    return str(value or 'N/A')


def _build_pricer_model_input_card(label, value, tone='neutral'):
    formatted_value = _format_pricer_model_input_value(value)
    classes = ['pricer-model-input-card']
    if tone:
        classes.append(f'pricer-model-input-card-{tone}')
    return html.Div(
        [
            html.Div(label, className='pricer-model-input-label'),
            html.Div(
                formatted_value,
                className='pricer-model-input-value',
                title=formatted_value,
            ),
        ],
        className=' '.join(classes),
    )


def _build_pricer_model_inputs_strip(items):
    return html.Div(
        [
            _build_pricer_model_input_card(label, value, tone)
            for label, value, tone in items
        ],
        className='pricer-model-inputs-strip',
    )


def _build_pricer_field(label, control, className=None):
    classes = ['pricer-field']
    if className:
        classes.append(className)
    return html.Div(
        [
            html.Label(label, className='pricer-field-label'),
            control,
        ],
        className=' '.join(classes),
    )


def _build_pricer_number_input(input_id, value, min_value=None, max_value=None, step=None):
    return dcc.Input(
        id=input_id,
        type='text',
        value=str(value),
        inputMode='decimal',
        pattern=rf'[0-9]*([.,][0-9]{{0,{MAX_PRICER_DECIMALS}}})?',
        autoComplete='off',
        spellCheck=False,
        className='pricer-number-input',
    )


def _build_pricer_date_picker(picker_id, default_date):
    return html.Div(
        dcc.DatePickerSingle(
            id=picker_id,
            min_date_allowed=date.today(),
            initial_visible_month=date.today(),
            date=default_date,
            display_format='YYYY-MM-DD',
            className='pricer-date-picker',
        ),
        className='pricer-date-control',
    )


def _format_pricer_number(value, decimals=4):
    if value is None or value == '':
        return None, None
    parsed_value = _coerce_pricer_float(value)
    if parsed_value is None:
        return None, None
    return parsed_value, f"{parsed_value:,.{decimals}f}"


def _build_pricer_greeks_grid(grid_id, rows, columns):
    numeric_fields = [col['id'] for col in columns if col['id'] != 'greek']
    row_data = []
    for row in rows:
        clean_row = {}
        for key, value in row.items():
            if key in numeric_fields:
                raw_value, display_value = _format_pricer_number(value, decimals=6 if abs(value or 0) < 0.01 else 4)
                clean_row[key] = display_value
                clean_row[f'__{key}_raw'] = raw_value
            else:
                clean_row[key] = value
        row_data.append(clean_row)

    column_defs = []
    for col in columns:
        field = col['id']
        header = col['name']
        column_def = {
            'headerName': header,
            'field': field,
            'sortable': False,
            'filter': False,
            'resizable': True,
            'width': 128 if field == 'greek' else 112,
            'minWidth': 104 if field == 'greek' else 90,
            'maxWidth': 170,
            'tooltipField': field,
            'headerTooltip': header,
            'cellClass': 'pricer-table-text-cell',
            'headerClass': 'pricer-table-text-header',
        }
        if field == 'greek':
            column_def.update({'pinned': 'left', 'lockPinned': True})
        else:
            raw_key = f'__{field}_raw'
            raw_expr = f"Number(params.data && params.data['{raw_key}'])"
            column_def.update({
                'type': 'rightAligned',
                'cellClass': 'pricer-table-number-cell',
                'headerClass': 'pricer-table-number-header',
                'cellClassRules': {
                    'pricer-positive-cell': f"{raw_expr} > 0",
                    'pricer-negative-cell': f"{raw_expr} < 0",
                    'pricer-missing-cell': (
                        f"params.data === null || params.data === undefined "
                        f"|| params.data['{raw_key}'] === null || params.data['{raw_key}'] === undefined "
                        f"|| isNaN(Number(params.data['{raw_key}']))"
                    ),
                },
            })
        column_defs.append(column_def)

    return dag.AgGrid(
        id=grid_id,
        rowData=row_data,
        columnDefs=column_defs,
        defaultColDef={
            'sortable': False,
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
        className='ag-theme-alpine mckinsey-ag-grid pricer-data-grid',
        style={'width': '100%'},
        dangerously_allow_code=True,
    )


def _build_pricer_filter_bar():
    return html.Div(
        [
            html.Div(
                [
                    html.Span('Option Type', className='filter-group-header'),
                    dcc.Dropdown(
                        id='option-type',
                        options=option_types,
                        value='black76',
                        clearable=False,
                        className='pricer-filter-dropdown pricer-option-type-dropdown',
                    ),
                ],
                className='filter-group pricer-sticky-filter-group pricer-option-type-group',
            )
        ],
        className='professional-section-header pricer-sticky-filter-bar',
    )


# Create app layout
layout = html.Div([
    _build_pricer_filter_bar(),

    html.Div([
        _build_pricer_section_header('Option Configuration'),
        html.Div([
            html.Div([
                html.Div([
                    html.Span('Call / Put', className='pricer-control-label'),
                    dcc.RadioItems(
                        id='option-call-put',
                        options=[
                            {'label': 'Call', 'value': 'C'},
                            {'label': 'Put', 'value': 'P'},
                        ],
                        value='C',
                        inline=True,
                        className='pricer-segmented-selector pricer-call-put-selector',
                        inputStyle={'display': 'none'},
                        labelStyle={'marginRight': '0'},
                    ),
                ], className='pricer-config-control'),
                html.Button('Calculate', id='calculate-button', className='custom-export-btn pricer-calculate-button'),
            ], className='pricer-config-toolbar'),
            html.Div(id='parameters-container', className='pricer-parameters-container'),
            html.Div(
                id='model-inputs-used-container',
                children=_build_pricer_message('Calculate to confirm model inputs.'),
                className='pricer-model-inputs-used-container',
            ),
        ], className='pricer-section-body pricer-config-body'),
    ], className='pricer-section pricer-config-section'),

    html.Div([
        _build_pricer_section_header('Pricing Output'),
        html.Div([
            html.Div(
                [
                    html.Div(id='results-container', children=_build_pricer_message('Click Calculate to see results.')),
                    html.Div(id='time-info', children=_build_pricer_message('Time information will appear here.')),
                ],
                className='pricer-output-summary',
            ),
            html.Div(
                [
                    html.H4('Greeks', className='pricer-output-panel-title'),
                    html.Div(id='greeks-container', children=_build_pricer_message('Greeks will appear here.')),
                ],
                className='pricer-output-panel pricer-greeks-panel',
            ),
        ], className='pricer-section-body pricer-output-body'),
    ], className='pricer-section pricer-output-section'),

    html.Div([
        _build_pricer_section_header('Payoff Analysis'),
        html.Div([
            html.Div([
                _build_pricer_field(
                    'Valuation Date',
                    html.Div(
                        dcc.DatePickerSingle(
                            id='valuation-date',
                            min_date_allowed=date.today(),
                            initial_visible_month=date.today(),
                            date=None,
                            display_format='YYYY-MM-DD',
                            placeholder='At Expiration',
                            className='pricer-date-picker',
                        ),
                        className='pricer-date-control',
                    ),
                    className='pricer-payoff-date-field',
                ),
                _build_pricer_field(
                    'Price Range (%)',
                    dcc.Slider(
                        id='price-range-slider',
                        min=10,
                        max=100,
                        step=5,
                        value=50,
                        marks={
                            10: '10%',
                            25: '25%',
                            50: '50%',
                            75: '75%',
                            100: '100%',
                        },
                        className='pricer-slider',
                    ),
                    className='pricer-payoff-slider-field',
                ),
            ], className='pricer-payoff-controls'),
            _build_pricer_chart_card('payoff-chart', 'Option Payoff', 'Click Calculate to see payoff chart.', className='pricer-wide-chart'),
        ], className='pricer-section-body pricer-payoff-body'),
    ], className='pricer-section pricer-payoff-section'),

    html.Div([
        _build_pricer_section_header('Sensitivity Charts'),
        html.Div([
            _build_pricer_chart_card('volatility-chart', 'Volatility Sensitivity', 'Click Calculate to see volatility chart.'),
            _build_pricer_chart_card('rate-chart', 'Risk-Free Rate Sensitivity', 'Click Calculate to see rate chart.'),
            _build_pricer_chart_card('correlation-chart', 'Correlation Sensitivity', 'Click Calculate to see correlation chart.'),
            _build_pricer_chart_card('extension-chart', 'Expiration Extension', 'Click Calculate to see extension chart.'),
            _build_pricer_chart_card('time-chart', 'Time Decay', 'Click Calculate to see time decay chart.', className='pricer-wide-chart'),
        ], className='pricer-section-body pricer-chart-grid'),
    ], className='pricer-section pricer-sensitivity-section'),
], className='options-dashboard-container pricer-page')


# Callback to update parameters based on option type
@callback(
    Output("parameters-container", "children"),
    Input("option-type", "value")
)
def update_parameters(option_type):
    """Update parameter form based on selected option type"""

    if option_type == "black76":
        params = [
            _build_pricer_field(
                "Underlying Price (S)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'black76', 'param': 'underlying-price'},
                    100,
                    min_value=0.01,
                ),
            ),
            _build_pricer_field(
                "Strike Price (K)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'black76', 'param': 'strike-price'},
                    100,
                    min_value=0.01,
                ),
            ),
            _build_pricer_field(
                "Expiration Date",
                _build_pricer_date_picker(
                    {'type': 'param-date', 'model': 'black76', 'param': 'expiration-date'},
                    date.today() + timedelta(days=30),
                ),
            ),
            _build_pricer_field(
                "Expiration Contract Date",
                _build_pricer_date_picker(
                    {'type': 'param-date', 'model': 'black76', 'param': 'contract-expiration-date'},
                    date.today() + timedelta(days=30),
                ),
            ),
            _build_pricer_field(
                "Risk-Free Rate (r)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'black76', 'param': 'risk-free-rate'},
                    0.05,
                    min_value=-1,
                    max_value=2,
                    step=0.000001,
                ),
            ),
            _build_pricer_field(
                "Volatility (σ)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'black76', 'param': 'volatility'},
                    0.2,
                    min_value=0.005,
                    max_value=2,
                    step=0.0000000001,
                ),
            ),
        ]

    elif option_type == "kirk":
        params = [
            _build_pricer_field(
                "Price of Asset 1 (S1)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'kirk', 'param': 'price-asset1'},
                    100,
                    min_value=0.01,
                ),
            ),
            _build_pricer_field(
                "Price of Asset 2 (S2)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'kirk', 'param': 'price-asset2'},
                    90,
                    min_value=0.01,
                ),
            ),
            _build_pricer_field(
                "Strike Price (K)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'kirk', 'param': 'spread-strike'},
                    5,
                ),
            ),
            _build_pricer_field(
                "Expiration Date",
                _build_pricer_date_picker(
                    {'type': 'param-date', 'model': 'kirk', 'param': 'spread-expiration-date'},
                    date.today() + timedelta(days=30),
                ),
            ),
            _build_pricer_field(
                "Expiration Contract Date",
                _build_pricer_date_picker(
                    {'type': 'param-date', 'model': 'kirk', 'param': 'spread-contract-expiration-date'},
                    date.today() + timedelta(days=30),
                ),
            ),
            _build_pricer_field(
                "Risk-Free Rate (r)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'kirk', 'param': 'spread-risk-free-rate'},
                    0.05,
                    min_value=-1,
                    max_value=2,
                    step=0.00001,
                ),
            ),
            _build_pricer_field(
                "Volatility of Asset 1 (σ1)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'kirk', 'param': 'volatility-asset1'},
                    0.2,
                    min_value=0.005,
                    max_value=2,
                    step=0.0000000001,
                ),
            ),
            _build_pricer_field(
                "Volatility of Asset 2 (σ2)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'kirk', 'param': 'volatility-asset2'},
                    0.15,
                    min_value=0.005,
                    max_value=2,
                    step=0.0000000001,
                ),
            ),
            _build_pricer_field(
                "Correlation (ρ)",
                _build_pricer_number_input(
                    {'type': 'param', 'model': 'kirk', 'param': 'correlation'},
                    0.5,
                    min_value=-1,
                    max_value=1,
                    step=0.00001,
                ),
            ),
        ]

    else:
        # Default to Black-76 if an unknown option type is selected
        return update_parameters("black76")

    return html.Div(params, className='pricer-parameter-grid')


@callback(
    [
        Output({'type': 'param-date', 'model': 'black76', 'param': 'contract-expiration-date'}, 'date'),
        Output({'type': 'param-date', 'model': 'black76', 'param': 'contract-expiration-date'}, 'min_date_allowed'),
    ],
    [
        Input({'type': 'param-date', 'model': 'black76', 'param': 'expiration-date'}, 'date'),
        Input({'type': 'param-date', 'model': 'black76', 'param': 'contract-expiration-date'}, 'date'),
    ],
    prevent_initial_call=True,
)
def sync_black76_contract_expiration_date(expiration_date_value, contract_expiration_date_value):
    return _sync_pricer_contract_expiration_date(expiration_date_value, contract_expiration_date_value)


@callback(
    [
        Output({'type': 'param-date', 'model': 'kirk', 'param': 'spread-contract-expiration-date'}, 'date'),
        Output({'type': 'param-date', 'model': 'kirk', 'param': 'spread-contract-expiration-date'}, 'min_date_allowed'),
    ],
    [
        Input({'type': 'param-date', 'model': 'kirk', 'param': 'spread-expiration-date'}, 'date'),
        Input({'type': 'param-date', 'model': 'kirk', 'param': 'spread-contract-expiration-date'}, 'date'),
    ],
    prevent_initial_call=True,
)
def sync_kirk_contract_expiration_date(expiration_date_value, contract_expiration_date_value):
    return _sync_pricer_contract_expiration_date(expiration_date_value, contract_expiration_date_value)


# Callback to calculate option prices and greeks
@callback(
    [
        Output("results-container", "children"),
        Output("greeks-container", "children"),
        Output("time-info", "children"),
        Output("model-inputs-used-container", "children"),
    ],
    [
        Input("calculate-button", "n_clicks"),
        Input("option-type", "value"),
    ],
    [
        State("option-call-put", "value"),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date'),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'id'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'id')
    ],
    prevent_initial_call=True
)
def calculate_option(n_clicks, option_type, call_put, all_params, all_dates, all_param_ids=None, all_date_ids=None):
    """Calculate option price and greeks using either Black-76 or Kirk."""

    if _get_pricer_triggered_id() == 'option-type':
        return (
            _build_pricer_message("Click Calculate to see results."),
            _build_pricer_message("Greeks will appear here."),
            _build_pricer_message("Time information will appear here."),
            _build_pricer_message("Calculate to confirm model inputs."),
        )

    if not n_clicks:
        return (
            _build_pricer_message("No calculation performed."),
            _build_pricer_message("Greeks will appear here."),
            _build_pricer_message("Time information will appear here."),
            _build_pricer_message("Calculate to confirm model inputs."),
        )

    try:
        # --------------------------------------------------
        # 1) Pull out parameter values by index
        #    based on the known order of fields in the layout
        # --------------------------------------------------

        if option_type == "black76":
            black76_inputs = _parse_black76_model_inputs(
                all_params, all_dates, all_param_ids, all_date_ids
            )
            S = black76_inputs['S']
            K = black76_inputs['K']
            r = black76_inputs['r']
            raw_v = black76_inputs['raw_v']
            v = black76_inputs['v']
            expiration_date = black76_inputs['expiration_date']
            contract_expiration_date = black76_inputs['contract_expiration_date']
            vol_adjustment_factor = black76_inputs['vol_adjustment_factor']
            option_business_days = black76_inputs['option_business_days']
            contract_business_days = black76_inputs['contract_business_days']
            days_to_expiry = black76_inputs['days_to_expiry']
            T = black76_inputs['T']
            results = black_76(call_put, S, K, T, r, v)
            option_value, delta, gamma, theta, vega, rho_greek = results

            # Store for payoff chart
            option_cache["black76"]["value"] = option_value
            option_cache["black76"]["params"] = {
                "S": S,
                "K": K,
                "T": T,
                "r": r,
                "raw_v": raw_v,
                "v": v,
                "vol_adjustment_factor": vol_adjustment_factor,
                "option_business_days": option_business_days,
                "contract_business_days": contract_business_days,
                "call_put": call_put,
                "expiration_date": expiration_date.strftime('%Y-%m-%d'),
                "contract_expiration_date": contract_expiration_date.strftime('%Y-%m-%d')
            }

            results_div = _build_pricer_result_card(
                "Option Value",
                f"{option_value:,.4f}",
                "Black-76 (Commodities)",
                tone='primary',
            )

            # Greeks are already in trader convention from black_76():
            # - theta: per day
            # - vega: per 1% vol change
            # - rho: per 1% rate change
            greeks_div = _build_pricer_greeks_grid(
                'pricer-black76-greeks-grid',
                [
                    {'greek': 'Delta', 'value': delta},
                    {'greek': 'Gamma', 'value': gamma},
                    {'greek': 'Theta', 'value': theta},
                    {'greek': 'Vega', 'value': vega},
                    {'greek': 'Rho', 'value': rho_greek},
                ],
                [
                    {'name': 'Greek', 'id': 'greek'},
                    {'name': 'Value', 'id': 'value'},
                ],
            )

            time_info = _build_pricer_result_card(
                "Time to Expiration",
                f"{T:.4f} years",
                f"{days_to_expiry} days",
            )

            model_inputs_used = _build_pricer_model_inputs_strip(
                [
                    ('Model', 'Black-76', 'primary'),
                    ('Call/Put', _format_pricer_call_put(call_put), 'primary'),
                    ('Underlying Price', S, 'neutral'),
                    ('Strike', K, 'neutral'),
                    ('Risk-Free Rate', r, 'neutral'),
                    ('Input Volatility', raw_v, 'neutral'),
                    ('Vol Adj Factor', vol_adjustment_factor, 'neutral'),
                    ('Volatility Used', v, 'primary' if contract_expiration_date != expiration_date else 'neutral'),
                    ('Expiration', expiration_date, 'neutral'),
                    ('Contract Expiration', contract_expiration_date, 'neutral'),
                    ('Option BDays', option_business_days, 'neutral'),
                    ('Contract BDays', contract_business_days, 'neutral'),
                    ('T', T, 'neutral'),
                ]
            )

            return results_div, greeks_div, time_info, model_inputs_used

        elif option_type == "kirk":
            kirk_inputs = _parse_kirk_model_inputs(
                all_params, all_dates, all_param_ids, all_date_ids
            )
            S1 = kirk_inputs['S1']
            S2 = kirk_inputs['S2']
            K_spread = kirk_inputs['K_spread']
            r_spread = kirk_inputs['r_spread']
            raw_v1 = kirk_inputs['raw_v1']
            raw_v2 = kirk_inputs['raw_v2']
            v1 = kirk_inputs['v1']
            v2 = kirk_inputs['v2']
            rho = kirk_inputs['rho']
            spread_expiration_date = kirk_inputs['expiration_date']
            spread_contract_expiration_date = kirk_inputs['contract_expiration_date']
            vol_adjustment_factor = kirk_inputs['vol_adjustment_factor']
            option_business_days = kirk_inputs['option_business_days']
            contract_business_days = kirk_inputs['contract_business_days']
            days_to_expiry = kirk_inputs['days_to_expiry']
            T_spread = kirk_inputs['T_spread']
            call_put_expanded = "call" if call_put == "C" else "put"

            # --------------------------------------------------
            # 3) Calculate using Kirk model
            # --------------------------------------------------
            option_value = kirk_model_with_substitution(
                S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
            )
            option_cache["kirk"]["value"] = option_value
            option_cache["kirk"]["params"] = {
                "S1": S1,
                "S2": S2,
                "K_spread": K_spread,
                "T_spread": T_spread,
                "r_spread": r_spread,
                "raw_v1": raw_v1,
                "raw_v2": raw_v2,
                "v1": v1,
                "v2": v2,
                "vol_adjustment_factor": vol_adjustment_factor,
                "option_business_days": option_business_days,
                "contract_business_days": contract_business_days,
                "rho": rho,
                "call_put": call_put_expanded,
                "expiration_date": spread_expiration_date.strftime('%Y-%m-%d'),
                "contract_expiration_date": spread_contract_expiration_date.strftime('%Y-%m-%d')
            }

            # Get greeks
            greeks = kirk_spread_greeks(
                S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
            )
            delta_S1 = greeks.get('delta_S1', 0)
            delta_S2 = greeks.get('delta_S2', 0)
            gamma_S1 = greeks.get('gamma_S1', 0)
            gamma_S2 = greeks.get('gamma_S2', 0)
            gamma_S1S2 = greeks.get('gamma_S1S2', 0)
            vega_sigma1 = greeks.get('vega_sigma1', 0)
            vega_sigma2 = greeks.get('vega_sigma2', 0)
            corr_sensitivity = greeks.get('corr_sensitivity', 0)
            theta = greeks.get('theta', 0)
            vega_equiv = greeks.get('vega_equiv', 0)

            results_div = _build_pricer_result_card(
                "Option Value",
                f"{option_value:,.4f}",
                "Kirk Spread Option Model",
                tone='primary',
            )

            greeks_div = _build_pricer_greeks_grid(
                'pricer-kirk-greeks-grid',
                [
                    {'greek': 'Delta', 'asset_1': delta_S1, 'asset_2': delta_S2, 'cross': None},
                    {'greek': 'Gamma', 'asset_1': gamma_S1, 'asset_2': gamma_S2, 'cross': gamma_S1S2},
                    {'greek': 'Vega', 'asset_1': vega_sigma1, 'asset_2': vega_sigma2, 'cross': None},
                    {'greek': 'Theta', 'asset_1': theta, 'asset_2': None, 'cross': None},
                    {'greek': 'Corr Sensitivity', 'asset_1': corr_sensitivity, 'asset_2': None, 'cross': None},
                    {'greek': 'Vega Equiv', 'asset_1': vega_equiv, 'asset_2': None, 'cross': None},
                ],
                [
                    {'name': 'Greek', 'id': 'greek'},
                    {'name': 'Asset 1', 'id': 'asset_1'},
                    {'name': 'Asset 2', 'id': 'asset_2'},
                    {'name': 'Cross', 'id': 'cross'},
                ],
            )

            time_info = _build_pricer_result_card(
                "Time to Expiration",
                f"{T_spread:.4f} years",
                f"{days_to_expiry} days",
            )

            model_inputs_used = _build_pricer_model_inputs_strip(
                [
                    ('Model', 'Kirk', 'primary'),
                    ('Call/Put', _format_pricer_call_put(call_put_expanded), 'primary'),
                    ('Asset 1', S1, 'neutral'),
                    ('Asset 2', S2, 'neutral'),
                    ('Spread Strike', K_spread, 'neutral'),
                    ('Risk-Free Rate', r_spread, 'neutral'),
                    ('Input Vol Asset 1', raw_v1, 'neutral'),
                    ('Input Vol Asset 2', raw_v2, 'neutral'),
                    ('Vol Adj Factor', vol_adjustment_factor, 'neutral'),
                    ('Vol Asset 1 Used', v1, 'primary' if spread_contract_expiration_date != spread_expiration_date else 'neutral'),
                    ('Vol Asset 2 Used', v2, 'primary' if spread_contract_expiration_date != spread_expiration_date else 'neutral'),
                    ('Correlation', rho, 'neutral'),
                    ('Expiration', spread_expiration_date, 'neutral'),
                    ('Contract Expiration', spread_contract_expiration_date, 'neutral'),
                    ('Option BDays', option_business_days, 'neutral'),
                    ('Contract BDays', contract_business_days, 'neutral'),
                    ('T', T_spread, 'neutral'),
                ]
            )

            return results_div, greeks_div, time_info, model_inputs_used

        else:
            # Unknown option type
            return (
                _build_pricer_message(f"Unknown option type: {option_type}", tone='danger'),
                _build_pricer_message("Greeks unavailable.", tone='warning'),
                _build_pricer_message("Time unavailable.", tone='warning'),
                _build_pricer_message("Model inputs unavailable.", tone='warning'),
            )

    except Exception as e:
        error_message = f"Error in calculation: {str(e)}"
        return (
            _build_pricer_message(error_message, tone='danger'),
            _build_pricer_message("Greeks unavailable.", tone='warning'),
            _build_pricer_message("Calculation error.", tone='danger'),
            _build_pricer_message("Model inputs unavailable due to calculation error.", tone='danger'),
        )


# Callback to update the payoff chart
# Also update the payoff chart callback to use the cache
@callback(
    Output("payoff-chart", "figure"),
    [Input("calculate-button", "n_clicks"),
     Input("valuation-date", "date"),
     Input("price-range-slider", "value")],
    [State("option-type", "value")],
    prevent_initial_call=True
)
def update_payoff_chart(n_clicks, valuation_date, price_range, option_type):

    """Update the payoff chart based on option parameters and valuation date"""
    # Check if calculation has been triggered
    if n_clicks is None:
        return _empty_pricer_figure("Calculate option price first.", "Underlying Price", "Option Value")

    try:
        if option_type == "black76":
            # Check if we have cached values
            if option_cache["black76"]["value"] is None or option_cache["black76"]["params"] is None:
                return _empty_pricer_figure("Calculate option price first.", "Underlying Price", "Option Value")

            # Get parameters from cache
            cached_params = option_cache["black76"]["params"]
            S = cached_params["S"]
            K = cached_params["K"]
            r = cached_params["r"]
            v = cached_params["v"]
            call_put = cached_params["call_put"]

            # Parse expiration date from cache
            if "expiration_date" in cached_params and cached_params["expiration_date"]:
                exp_date = parse_date(cached_params["expiration_date"])
            else:
                # Use T to calculate expiration date if not directly available
                today = date.today()
                days_to_expiry = int(cached_params["T"] * 365)  # Approximate
                exp_date = today + timedelta(days=days_to_expiry)

            # Parse valuation date if provided
            if valuation_date is None:
                # If no valuation date, use expiration date
                val_date = exp_date
                time_to_expiry = 0.001  # Almost at expiration
            else:
                val_date = parse_date(valuation_date)
                # If valuation date is after expiration, use expiration date
                if val_date > exp_date:
                    val_date = exp_date
                    time_to_expiry = 0.001
                else:
                    days_to_expiry = (exp_date - val_date).days
                    time_to_expiry = max(days_to_expiry / 365.0, 0.001)

            # Calculate price range
            price_min = max(0.01, S * (1 - price_range / 100))
            price_max = S * (1 + price_range / 100)
            price_step = (price_max - price_min) / 100
            prices = np.arange(price_min, price_max + price_step, price_step)

            option_values = []
            intrinsic_values = []

            # At expiration, option value equals intrinsic value
            if time_to_expiry <= 0.001:
                for price in prices:
                    if call_put == "C":
                        intrinsic = max(0, price - K)
                    else:
                        intrinsic = max(0, K - price)

                    intrinsic_values.append(intrinsic)
                    option_values.append(intrinsic)
            else:
                # Calculate option values across price range
                for price in prices:
                    try:
                        results = black_76(call_put, price, K, time_to_expiry, r, v)
                        option_values.append(results[0])

                        # Calculate intrinsic value
                        if call_put == "C":
                            intrinsic = max(0, price - K)
                        else:
                            intrinsic = max(0, K - price)
                        intrinsic_values.append(intrinsic)
                    except Exception:
                        # If error, use intrinsic value
                        if call_put == "C":
                            intrinsic = max(0, price - K)
                        else:
                            intrinsic = max(0, K - price)

                        option_values.append(intrinsic)
                        intrinsic_values.append(intrinsic)

            # Create figure
            fig = go.Figure()

            # Add option value line
            fig.add_trace(
                go.Scatter(
                    x=prices,
                    y=option_values,
                    mode='lines',
                    name='Option Value',
                    line=dict(color='blue', width=2)
                )
            )

            # Add intrinsic value line
            fig.add_trace(
                go.Scatter(
                    x=prices,
                    y=intrinsic_values,
                    mode='lines',
                    name='Intrinsic Value',
                    line=dict(color='red', width=2, dash='dash')
                )
            )

            # Add marker for originally calculated value
            fig.add_trace(
                go.Scatter(
                    x=[S],
                    y=[option_cache["black76"]["value"]],
                    mode='markers',
                    name='Calculated Value',
                    marker=dict(color='green', size=10, symbol='star')
                )
            )

            # Add current price line
            fig.add_vline(
                x=S,
                line_width=1,
                line_dash="dash",
                line_color="green",
                annotation_text="Current Price",
                annotation_position="top"
            )

            # Add strike price line
            fig.add_vline(
                x=K,
                line_width=1,
                line_dash="dash",
                line_color="orange",
                annotation_text="Strike Price",
                annotation_position="bottom"
            )

            # Update layout
            title_text = f"{'Call' if call_put == 'C' else 'Put'} Option Payoff"
            if valuation_date is None:
                title_text += " at Expiration"
            else:
                title_text += f" on {val_date.strftime('%Y-%m-%d')}"

            fig.update_layout(
                title=title_text,
                xaxis_title="Underlying Price",
                yaxis_title="Option Value",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            return _style_pricer_figure(fig)

        elif option_type == "kirk":
            # Check if we have cached values
            if option_cache["kirk"]["value"] is None or option_cache["kirk"]["params"] is None:
                return _empty_pricer_figure("Calculate option price first.", "Price", "Option Value")

            # Get parameters from cache
            cached_params = option_cache["kirk"]["params"]
            S1 = cached_params["S1"]
            S2 = cached_params["S2"]
            K_spread = cached_params["K_spread"]
            v1 = cached_params["v1"]
            v2 = cached_params["v2"]
            rho = cached_params["rho"]
            call_put_expanded = cached_params["call_put"]
            call_put = "C" if call_put_expanded == "call" else "P"

            # Parse expiration date from cache
            if "expiration_date" in cached_params and cached_params["expiration_date"]:
                exp_date = parse_date(cached_params["expiration_date"])
            else:
                # Use T_spread to calculate expiration date if not directly available
                today = date.today()
                days_to_expiry = int(cached_params["T_spread"] * 365)  # Approximate
                exp_date = today + timedelta(days=days_to_expiry)

            # Parse valuation date if provided
            if valuation_date is None:
                # If no valuation date, use expiration date
                val_date = exp_date
                time_to_expiry = 0.001  # Almost at expiration
            else:
                val_date = parse_date(valuation_date)
                # If valuation date is after expiration, use expiration date
                if val_date > exp_date:
                    val_date = exp_date
                    time_to_expiry = 0.001
                else:
                    days_to_expiry = (exp_date - val_date).days
                    time_to_expiry = max(days_to_expiry / 365.0, 0.001)

            # Calculate price range for asset 1
            price_min = max(0.01, S1 * (1 - price_range / 100))
            price_max = S1 * (1 + price_range / 100)
            price_step = (price_max - price_min) / 100
            prices = np.arange(price_min, price_max + price_step, price_step)

            option_values = []
            intrinsic_values = []

            # At expiration, option value equals intrinsic value
            if time_to_expiry <= 0.001:
                for price in prices:
                    if call_put == "C":
                        intrinsic = max(0, price - S2 - K_spread)
                    else:
                        intrinsic = max(0, S2 + K_spread - price)

                    intrinsic_values.append(intrinsic)
                    option_values.append(intrinsic)
            else:
                # Calculate option values across price range
                for price in prices:
                    try:
                        option_value = kirk_model_with_substitution(
                            price, S2, K_spread, v1, v2, rho, time_to_expiry, call_put_expanded
                        )
                        option_values.append(option_value)

                        # Calculate intrinsic value
                        if call_put == "C":
                            intrinsic = max(0, price - S2 - K_spread)
                        else:
                            intrinsic = max(0, S2 + K_spread - price)
                        intrinsic_values.append(intrinsic)
                    except Exception:
                        # If error, use intrinsic value
                        if call_put == "C":
                            intrinsic = max(0, price - S2 - K_spread)
                        else:
                            intrinsic = max(0, S2 + K_spread - price)

                        option_values.append(intrinsic)
                        intrinsic_values.append(intrinsic)

            # Create figure
            fig = go.Figure()

            # Add option value line
            fig.add_trace(
                go.Scatter(
                    x=prices,
                    y=option_values,
                    mode='lines',
                    name='Option Value',
                    line=dict(color='blue', width=2)
                )
            )

            # Add intrinsic value line
            fig.add_trace(
                go.Scatter(
                    x=prices,
                    y=intrinsic_values,
                    mode='lines',
                    name='Intrinsic Value',
                    line=dict(color='red', width=2, dash='dash')
                )
            )

            # Add marker for originally calculated value
            fig.add_trace(
                go.Scatter(
                    x=[S1],
                    y=[option_cache["kirk"]["value"]],
                    mode='markers',
                    name='Calculated Value',
                    marker=dict(color='green', size=10, symbol='star')
                )
            )

            # Add current price line for asset 1
            fig.add_vline(
                x=S1,
                line_width=1,
                line_dash="dash",
                line_color="green",
                annotation_text="Current Asset 1",
                annotation_position="top"
            )

            # Add breakeven price line (S2 + K_spread)
            fig.add_vline(
                x=S2 + K_spread,
                line_width=1,
                line_dash="dash",
                line_color="orange",
                annotation_text="S2 + Strike",
                annotation_position="bottom"
            )

            # Update layout
            title_text = f"{'Call' if call_put == 'C' else 'Put'} Spread Option Payoff (Asset 1 varying)"
            if valuation_date is None:
                title_text += " at Expiration"
            else:
                title_text += f" on {val_date.strftime('%Y-%m-%d')}"

            fig.update_layout(
                title=title_text,
                xaxis_title="Asset 1 Price",
                yaxis_title="Option Value",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            return _style_pricer_figure(fig)

        else:
            # Unknown option type
            return _empty_pricer_figure(f"Unknown option type: {option_type}", "Price", "Option Value")

    except Exception as e:
        # Return error figure
        return _empty_pricer_figure(f"Error creating chart: {str(e)}", "Price", "Option Value")


# Add a new callback for the volatility chart
@callback(
    Output("volatility-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [State("option-type", "value"),
     State("option-call-put", "value"),
     # Use pattern-matching selectors for all params
     State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
     State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date'),
     State({'type': 'param', 'model': ALL, 'param': ALL}, 'id'),
     State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'id')],
    prevent_initial_call=True
)
def update_volatility_chart(n_clicks, option_type, call_put, all_params, all_dates, all_param_ids=None, all_date_ids=None):
    """Create a chart showing option price vs volatility for Kirk spread options"""
    # Check if calculation has been performed
    if n_clicks is None:
        return _empty_pricer_figure("Calculate option price first.", "Volatility", "Option Price")

    try:
        # Processing parameters directly as in the calculate_option function

        # Generate range of volatilities to simulate (from 0.05 to 1.00)
        vol_range = np.linspace(0.05, 1.00, 40)

        # Arrays to store results
        option_prices = []
        volatilities = []

        if option_type == "black76":
            S, K, r, v, _exp_date, _days_to_expiry, T = _parse_black76_params(
                all_params, all_dates, all_param_ids, all_date_ids
            )

            # Calculate option price for each volatility value
            for vol in vol_range:
                volatilities.append(vol)

                try:
                    results = black_76(call_put, S, K, T, r, vol)
                    option_prices.append(results[0])  # First element is option price
                except Exception:
                    # If calculation fails, use None to create a gap in the chart
                    option_prices.append(None)

            # Create figure
            fig = go.Figure()

            # Add option price vs volatility line
            fig.add_trace(
                go.Scatter(
                    x=volatilities,
                    y=option_prices,
                    mode='lines+markers',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the current volatility and price
            current_vol = v
            current_price = option_cache["black76"]["value"] if option_cache["black76"]["value"] is not None else \
            black_76(call_put, S, K, T, r, v)[0]

            fig.add_trace(
                go.Scatter(
                    x=[current_vol],
                    y=[current_price],
                    mode='markers',
                    name='Current Parameters',
                    marker=dict(color='red', size=10, symbol='star')
                )
            )

            # Update layout
            fig.update_layout(
                title="Black-76 Option Price vs Volatility",
                xaxis_title="Volatility (σ)",
                yaxis_title="Option Price",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add annotation for current volatility
            fig.add_annotation(
                x=current_vol,
                y=current_price,
                text=f"Current: σ={current_vol:.3f}, Price={current_price:.4f}",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                ax=80,
                ay=-40
            )

        elif option_type == "kirk":
            S1, S2, K_spread, _r_spread, v1, v2, rho, _exp_date, _days_to_expiry, T_spread = (
                _parse_kirk_params(all_params, all_dates, all_param_ids, all_date_ids)
            )

            # Convert C/P to call/put for kirk_model_with_substitution
            call_put_expanded = "call" if call_put == "C" else "put"

            # Arrays to store results
            option_prices = []
            implied_vols = []

            # Calculate option price for each volatility combination
            # We'll vary both volatilities together proportionally
            base_v1 = v1
            base_v2 = v2
            ratio = base_v2 / base_v1 if base_v1 > 0 else 1

            for vol_factor in vol_range:
                # Create test volatility for asset 1
                test_v1 = vol_factor
                # Scale volatility for asset 2 to maintain original ratio
                test_v2 = test_v1 * ratio

                # Calculate equivalent volatility
                # Simplified formula: sqrt(v1^2 + v2^2 - 2*rho*v1*v2)
                equiv_vol = np.sqrt(test_v1 ** 2 + test_v2 ** 2 - 2 * rho * test_v1 * test_v2)
                implied_vols.append(equiv_vol)

                # Calculate option price with these volatilities
                try:
                    option_value = kirk_model_with_substitution(
                        S1, S2, K_spread, test_v1, test_v2, rho, T_spread, call_put_expanded
                    )
                    option_prices.append(option_value)
                except Exception:
                    # If calculation fails, use None to create a gap in the chart
                    option_prices.append(None)

            # Use implied_vols as our x-axis
            volatilities = implied_vols

            # Create figure
            fig = go.Figure()

            # Add option price vs implied volatility line
            fig.add_trace(
                go.Scatter(
                    x=volatilities,
                    y=option_prices,
                    mode='lines+markers',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the current volatility and price
            current_equiv_vol = np.sqrt(v1 ** 2 + v2 ** 2 - 2 * rho * v1 * v2)
            current_price = option_cache["kirk"]["value"] if option_cache["kirk"][
                "value"] else kirk_model_with_substitution(
                S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
            )

            fig.add_trace(
                go.Scatter(
                    x=[current_equiv_vol],
                    y=[current_price],
                    mode='markers',
                    name='Current Parameters',
                    marker=dict(color='red', size=10, symbol='star')
                )
            )

            # Update layout
            fig.update_layout(
                title="Kirk Spread Option Price vs Equivalent Volatility",
                xaxis_title="Equivalent Volatility (σ_eq)",
                yaxis_title="Option Price",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add annotation for current volatility
            fig.add_annotation(
                x=current_equiv_vol,
                y=current_price,
                text=f"Current: σ_eq={current_equiv_vol:.3f}, Price={current_price:.4f}",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                ax=80,
                ay=-40
            )

        else:
            # Unknown option type
            return _empty_pricer_figure(f"Unknown option type: {option_type}", "Volatility", "Option Price")

        return _style_pricer_figure(fig)

    except Exception as e:
        # Return error figure
        return _empty_pricer_figure(f"Error creating volatility chart: {str(e)}", "Volatility", "Option Price")


# Callback for the risk-free rate chart
@callback(
    Output("rate-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [State("option-type", "value"),
     State("option-call-put", "value"),
     State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
     State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date'),
     State({'type': 'param', 'model': ALL, 'param': ALL}, 'id'),
     State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'id')],
    prevent_initial_call=True
)
def update_rate_chart(n_clicks, option_type, call_put, all_params, all_dates, all_param_ids=None, all_date_ids=None):
    """Create a chart showing option price vs risk-free rate"""
    # Check if calculation has been performed
    if n_clicks is None:
        return _empty_pricer_figure("Calculate option price first.", "Risk-Free Rate (%)", "Option Price")

    try:
        # Processing parameters directly as in the calculate_option function

        # Generate range of risk-free rates to simulate (from -0.02 to 0.15)
        rate_range = np.linspace(-0.02, 0.15, 40)

        # Arrays to store results
        option_prices = []
        rates = []

        if option_type == "black76":
            S, K, r, v, _exp_date, _days_to_expiry, T = _parse_black76_params(
                all_params, all_dates, all_param_ids, all_date_ids
            )

            # Calculate option price for each risk-free rate value
            for rate in rate_range:
                rates.append(rate)

                try:
                    results = black_76(call_put, S, K, T, rate, v)
                    option_prices.append(results[0])  # First element is option price
                except Exception:
                    # If calculation fails, use None to create a gap in the chart
                    option_prices.append(None)

            # Create figure
            fig = go.Figure()

            # Add option price vs risk-free rate line
            fig.add_trace(
                go.Scatter(
                    x=rates,
                    y=option_prices,
                    mode='lines+markers',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the current risk-free rate and price
            current_rate = r
            current_price = option_cache["black76"]["value"] if option_cache["black76"]["value"] is not None else \
            black_76(call_put, S, K, T, r, v)[0]

            fig.add_trace(
                go.Scatter(
                    x=[current_rate],
                    y=[current_price],
                    mode='markers',
                    name='Current Parameters',
                    marker=dict(color='red', size=10, symbol='star')
                )
            )

            # Update layout
            fig.update_layout(
                title="Black-76 Option Price vs Risk-Free Rate",
                xaxis_title="Risk-Free Rate (r)",
                yaxis_title="Option Price",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add annotation for current risk-free rate
            fig.add_annotation(
                x=current_rate,
                y=current_price,
                text=f"Current: r={current_rate:.2%}, Price={current_price:.4f}",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                ax=80,
                ay=-40
            )

        elif option_type == "kirk":
            S1, S2, K_spread, r_spread, v1, v2, rho, _exp_date, _days_to_expiry, T_spread = (
                _parse_kirk_params(all_params, all_dates, all_param_ids, all_date_ids)
            )
            call_put_expanded = "call" if call_put == "C" else "put"

            rates = list(rate_range)
            try:
                option_value = kirk_model_with_substitution(
                    S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
                )
                option_prices = [option_value] * len(rate_range)
            except Exception:
                option_prices = [None] * len(rate_range)

            # Create figure
            fig = go.Figure()

            # Add option price vs risk-free rate line
            fig.add_trace(
                go.Scatter(
                    x=rates,
                    y=option_prices,
                    mode='lines+markers',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the current risk-free rate and price
            current_rate = r_spread
            current_price = option_cache["kirk"]["value"] if option_cache["kirk"][
                                                                 "value"] is not None else kirk_model_with_substitution(
                S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
            )

            fig.add_trace(
                go.Scatter(
                    x=[current_rate],
                    y=[current_price],
                    mode='markers',
                    name='Current Parameters',
                    marker=dict(color='red', size=10, symbol='star')
                )
            )

            # Update layout
            fig.update_layout(
                title="Kirk Spread Option Price vs Risk-Free Rate",
                xaxis_title="Risk-Free Rate (r)",
                yaxis_title="Option Price",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add annotation for current risk-free rate
            fig.add_annotation(
                x=current_rate,
                y=current_price,
                text=f"Current: r={current_rate:.2%}, Price={current_price:.4f}",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                ax=80,
                ay=-40
            )

        else:
            # Unknown option type
            return _empty_pricer_figure(f"Unknown option type: {option_type}", "Risk-Free Rate", "Option Price")

        return _style_pricer_figure(fig)

    except Exception as e:
        # Return error figure
        return _empty_pricer_figure(f"Error creating risk-free rate chart: {str(e)}", "Risk-Free Rate", "Option Price")


@callback(
    Output("time-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [
        State("option-type", "value"),
        State("option-call-put", "value"),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date'),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'id'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'id')
    ],
    prevent_initial_call=True
)
def update_time_chart(n_clicks, option_type, call_put, all_params, all_dates, all_param_ids=None, all_date_ids=None):
    """
    Create a chart showing option price vs time to expiration.
    Fixed to ensure dates display in correct order.
    """
    if n_clicks is None:
        return _empty_pricer_figure("Calculate option price first.", "Date", "Option Price")

    try:
        # ---------------------------------------------------------------
        # STEP 1: Get today and tomorrow as reference points
        # ---------------------------------------------------------------
        today = date.today()

        # ---------------------------------------------------------------
        # STEP 2: Get expiration date (with careful type handling)
        # ---------------------------------------------------------------
        expiration_date = _parse_pricer_expiration(
            _ordered_pricer_dates(option_type, all_dates, all_date_ids)
        )

        # ---------------------------------------------------------------
        # STEP 3: Generate date points for x-axis (with careful type handling)
        # ---------------------------------------------------------------
        try:
            # Create date points from tomorrow up to 4 months after expiration
            date_points = []

            # Start with tomorrow
            date_points.append(today)

            # Add some intermediate points before expiration
            days_to_expiry = (expiration_date - today).days

            # # Calculate intervals for date points
            for i in range(1, days_to_expiry):
                point_date = today + timedelta(days=i )
                if point_date < expiration_date:
                    date_points.append(point_date)

            # Add expiration date
            if expiration_date not in date_points:
                date_points.append(expiration_date)

            # Sort all dates
            date_points.sort()

            # Create formatted dates for x-axis in ISO format for proper sorting
            dates_formatted = [d.strftime('%Y-%m-%d') for d in date_points]

        except Exception:
            pass

        # ---------------------------------------------------------------
        # STEP 4: Calculate option values for each date
        # ---------------------------------------------------------------
        option_values = []

        # BLACK-76 OPTION CALCULATION
        if option_type == "black76":
            try:
                S, K, r, v, _exp_date, _days_to_expiry, _T = _parse_black76_params(
                    all_params, all_dates, all_param_ids, all_date_ids
                )

                # Calculate option value for each date
                for eval_date in date_points:
                    # Need to compute time to expiry in years
                    days_diff = (expiration_date - eval_date).days

                    if days_diff < 0:
                        # After expiration - option value is intrinsic value
                        if call_put == "C":
                            value = max(0, S - K)
                        else:
                            value = max(0, K - S)
                    else:
                        # Before expiration - use Black76
                        T = max(days_diff / 365.0, 0.001)  # Time in years

                        try:
                            option_results = black_76(call_put, S, K, T, r, v)
                            value = option_results[0]  # First return value is option price
                        except Exception:
                            # Fallback to intrinsic value
                            if call_put == "C":
                                value = max(0, S - K)
                            else:
                                value = max(0, K - S)

                    option_values.append(value)

                # Create the data dictionary for the figure
                chart_data = []
                for i in range(len(dates_formatted)):
                    chart_data.append({
                        'date': dates_formatted[i],
                        'value': option_values[i]
                    })

                # Sort the data by date
                chart_data.sort(key=lambda x: x['date'])

                # Extract the sorted data back into separate lists
                sorted_dates = [item['date'] for item in chart_data]
                sorted_values = [item['value'] for item in chart_data]

                # Create figure
                fig = go.Figure()

                # Add option price line with sorted data
                fig.add_trace(
                    go.Scatter(
                        x=sorted_dates,
                        y=sorted_values,
                        mode='lines',
                        name='Option Price',
                        line=dict(color='blue', width=2)
                    )
                )

                # Mark today's price
                try:
                    days_to_expiry_today = (expiration_date - today).days
                    if days_to_expiry_today >= 0:
                        today_T = max(days_to_expiry_today / 365.0, 0.001)
                        today_price = black_76(call_put, S, K, today_T, r, v)[0]

                        fig.add_trace(
                            go.Scatter(
                                x=[today.strftime('%Y-%m-%d')],
                                y=[today_price],
                                mode='markers',
                                name='Today',
                                marker=dict(color='red', size=10, symbol='star')
                            )
                        )
                except Exception:
                    pass

                # Create layout with proper date axis type
                option_type_label = "Call" if call_put == "C" else "Put"
                fig.update_layout(
                    title=f"Black-76 {option_type_label} Option Price Over Time",
                    xaxis_title="Date",
                    yaxis_title="Option Price",
                    height=400,
                    xaxis=dict(
                        tickangle=-45,
                        type='category',  # Keep as category but ensure data is pre-sorted
                        categoryorder='array',
                        categoryarray=sorted_dates  # Explicitly set the order
                    ),
                    legend=dict(
                        yanchor="top",
                        y=0.99,
                        xanchor="left",
                        x=0.01
                    )
                )

                # Add vertical line at expiration using shapes
                exp_date_str = expiration_date.strftime('%Y-%m-%d')
                fig.update_layout(
                    shapes=[
                        dict(
                            type="line",
                            xref="x",
                            yref="paper",
                            x0=exp_date_str,
                            y0=0,
                            x1=exp_date_str,
                            y1=1,
                            line=dict(
                                color="red",
                                width=1,
                                dash="dash",
                            )
                        )
                    ],
                    annotations=[
                        dict(
                            x=exp_date_str,
                            y=1,
                            xref="x",
                            yref="paper",
                            text="Expiration Date",
                            showarrow=False,
                            xanchor="center",
                            yanchor="bottom"
                        )
                    ]
                )

                return _style_pricer_figure(fig)

            except Exception as e:
                return _empty_pricer_figure(f"Error in Black76 calculation: {str(e)}", "Date", "Option Price")

        # KIRK SPREAD OPTION CALCULATION
        elif option_type == "kirk":
            try:
                S1, S2, K_spread, _r_spread, v1, v2, rho, _exp_date, _days_to_expiry, _T_spread = (
                    _parse_kirk_params(all_params, all_dates, all_param_ids, all_date_ids)
                )

                # Convert C/P to call/put string
                call_put_expanded = "call" if call_put == "C" else "put"

                # Calculate option value for each date
                for eval_date in date_points:
                    # Need to compute time to expiry in years
                    days_diff = (expiration_date - eval_date).days

                    if days_diff < 0:
                        # After expiration - option value is intrinsic value
                        if call_put == "C":
                            value = max(0, S1 - S2 - K_spread)
                        else:
                            value = max(0, S2 + K_spread - S1)
                    else:
                        # Before expiration - use Kirk model
                        T = max(days_diff / 365.0, 0.001)  # Time in years

                        try:
                            value = kirk_model_with_substitution(
                                S1, S2, K_spread, v1, v2, rho, T, call_put_expanded
                            )
                        except Exception:
                            # Fallback to intrinsic value
                            if call_put == "C":
                                value = max(0, S1 - S2 - K_spread)
                            else:
                                value = max(0, S2 + K_spread - S1)

                    option_values.append(value)

                # Create the data dictionary for the figure
                chart_data = []
                for i in range(len(dates_formatted)):
                    chart_data.append({
                        'date': dates_formatted[i],
                        'value': option_values[i]
                    })

                # Sort the data by date
                chart_data.sort(key=lambda x: x['date'])

                # Extract the sorted data back into separate lists
                sorted_dates = [item['date'] for item in chart_data]
                sorted_values = [item['value'] for item in chart_data]

                # Create figure
                fig = go.Figure()

                # Add option price line with sorted data
                fig.add_trace(
                    go.Scatter(
                        x=sorted_dates,
                        y=sorted_values,
                        mode='lines',
                        name='Option Price',
                        line=dict(color='blue', width=2)
                    )
                )

                # Mark today's price
                try:
                    days_to_expiry_today = (expiration_date - today).days
                    if days_to_expiry_today >= 0:
                        today_T = max(days_to_expiry_today / 365.0, 0.001)
                        today_price = kirk_model_with_substitution(
                            S1, S2, K_spread, v1, v2, rho, today_T, call_put_expanded
                        )

                        fig.add_trace(
                            go.Scatter(
                                x=[today.strftime('%Y-%m-%d')],
                                y=[today_price],
                                mode='markers',
                                name='Today',
                                marker=dict(color='red', size=10, symbol='star')
                            )
                        )
                except Exception:
                    pass

                # Create layout with proper date axis type
                option_type_label = "Call" if call_put == "C" else "Put"
                fig.update_layout(
                    title=f"Kirk Spread {option_type_label} Option Price Over Time",
                    xaxis_title="Date",
                    yaxis_title="Option Price",
                    height=400,
                    xaxis=dict(
                        tickangle=-45,
                        type='category',  # Keep as category but ensure data is pre-sorted
                        categoryorder='array',
                        categoryarray=sorted_dates  # Explicitly set the order
                    ),
                    legend=dict(
                        yanchor="top",
                        y=0.99,
                        xanchor="left",
                        x=0.01
                    )
                )

                # Add vertical line at expiration using shapes
                exp_date_str = expiration_date.strftime('%Y-%m-%d')
                fig.update_layout(
                    shapes=[
                        dict(
                            type="line",
                            xref="x",
                            yref="paper",
                            x0=exp_date_str,
                            y0=0,
                            x1=exp_date_str,
                            y1=1,
                            line=dict(
                                color="red",
                                width=1,
                                dash="dash",
                            )
                        )
                    ],
                    annotations=[
                        dict(
                            x=exp_date_str,
                            y=1,
                            xref="x",
                            yref="paper",
                            text="Expiration Date",
                            showarrow=False,
                            xanchor="center",
                            yanchor="bottom"
                        )
                    ]
                )

                return _style_pricer_figure(fig)

            except Exception as e:
                return _empty_pricer_figure(f"Error in Kirk calculation: {str(e)}", "Date", "Option Price")

        # Unknown option type
        else:
            return _empty_pricer_figure(f"Unknown option type: {option_type}", "Date", "Option Price")

    except Exception as e:

        return _empty_pricer_figure(f"Error creating time chart: {str(e)}", "Date", "Option Price")


@callback(
    Output("extension-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [
        State("option-type", "value"),
        State("option-call-put", "value"),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date'),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'id'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'id')
    ],
    prevent_initial_call=True
)
def update_extension_chart(n_clicks, option_type, call_put, all_params, all_dates, all_param_ids=None, all_date_ids=None):
    """
    Create a chart showing option price vs different expiration dates.
    This shows how extending the expiration date affects option value.
    """
    if n_clicks is None:
        return _empty_pricer_figure("Calculate option price first.", "Expiration Date", "Option Price")

    try:
        # ---------------------------------------------------------------
        # STEP 1: Get today's date as reference point
        # ---------------------------------------------------------------
        today = date.today()

        # ---------------------------------------------------------------
        # STEP 2: Get base expiration date from Option Configuration
        # ---------------------------------------------------------------
        # Default expiration is 1 year from today
        default_expiration = today + timedelta(days=365)
        base_expiration_date = _parse_pricer_expiration(
            _ordered_pricer_dates(option_type, all_dates, all_date_ids),
            default_expiration,
        )


        # ---------------------------------------------------------------
        # STEP 3: Generate range of expiration dates to evaluate
        # ---------------------------------------------------------------
        try:
            # Range: from today to 2 months after the base expiration date
            extended_date = base_expiration_date + timedelta(days=60)  # Base + 2 months

            # Create 40 evenly spaced dates from today to the extended date
            total_days = (extended_date - today).days

            # Safety check for negative or very small total_days
            if total_days <= 10:
                # If expiration is in the past or very close, use 1 year from today
                extended_date = today + timedelta(days=365 + 60)
                total_days = (extended_date - today).days

            # Generate evenly spaced dates
            num_points = 40
            days_step = max(1, total_days // (num_points - 1))

            expiration_dates = []
            for i in range(num_points):
                exp_date = today + timedelta(days=i * days_step)
                expiration_dates.append(exp_date)

            # Ensure base expiration date is in the list
            if base_expiration_date not in expiration_dates:
                expiration_dates.append(base_expiration_date)
                expiration_dates.sort()  # Re-sort after adding

            # Format dates for x-axis
            dates_formatted = [d.strftime('%Y-%m-%d') for d in expiration_dates]

        except Exception:
            # Fallback to basic date range
            expiration_dates = [
                today + timedelta(days=7),  # 1 week
                today + timedelta(days=30),  # 1 month
                today + timedelta(days=60),  # 2 months
                today + timedelta(days=90),  # 3 months
                today + timedelta(days=180),  # 6 months
                today + timedelta(days=270),  # 9 months
                today + timedelta(days=365),  # 1 year
                today + timedelta(days=365 + 60)  # 1 year + 2 months
            ]
            # Ensure base expiration date is in the list
            if base_expiration_date not in expiration_dates:
                expiration_dates.append(base_expiration_date)
                expiration_dates.sort()
            dates_formatted = [d.strftime('%Y-%m-%d') for d in expiration_dates]

        # ---------------------------------------------------------------
        # STEP 4: Calculate option values for each expiration date
        # ---------------------------------------------------------------
        option_values = []

        # BLACK-76 OPTION CALCULATION
        if option_type == "black76":
            try:
                S, K, r, v, _exp_date, _days_to_expiry, _T = _parse_black76_params(
                    all_params, all_dates, all_param_ids, all_date_ids
                )

                # For each potential expiration date, calculate option value
                for exp_date in expiration_dates:
                    # Calculate time to expiry in years (from today to this expiration date)
                    days_to_expiry = (exp_date - today).days

                    # Handle dates in the past
                    if days_to_expiry <= 0:
                        # For expiration dates in the past, option is just intrinsic value
                        if call_put == "C":
                            value = max(0, S - K)
                        else:
                            value = max(0, K - S)
                    else:
                        # Valid future expiration date - calculate Black-76 price
                        T = max(days_to_expiry / 365.0, 0.001)  # Time in years

                        try:
                            option_results = black_76(call_put, S, K, T, r, v)
                            value = option_results[0]  # First return value is option price
                        except Exception:
                            # Fallback to intrinsic value
                            if call_put == "C":
                                value = max(0, S - K)
                            else:
                                value = max(0, K - S)

                    option_values.append(value)

                # Create the data dictionary for the figure
                chart_data = []
                for i in range(len(dates_formatted)):
                    chart_data.append({
                        'date': dates_formatted[i],
                        'value': option_values[i]
                    })

                # Sort the data by date
                chart_data.sort(key=lambda x: x['date'])

                # Extract the sorted data back into separate lists
                sorted_dates = [item['date'] for item in chart_data]
                sorted_values = [item['value'] for item in chart_data]

                # Create figure
                fig = go.Figure()

                # Add option price line with sorted data
                fig.add_trace(
                    go.Scatter(
                        x=sorted_dates,
                        y=sorted_values,
                        mode='lines',
                        name='Option Price',
                        line=dict(color='blue', width=2)
                    )
                )

                # Mark the base expiration date that was selected in the UI
                try:
                    base_exp_str = base_expiration_date.strftime('%Y-%m-%d')
                    if base_exp_str in sorted_dates:
                        base_index = sorted_dates.index(base_exp_str)
                        base_value = sorted_values[base_index]

                        fig.add_trace(
                            go.Scatter(
                                x=[base_exp_str],
                                y=[base_value],
                                mode='markers',
                                name='Base Expiration',
                                marker=dict(color='red', size=10, symbol='star')
                            )
                        )
                except Exception:
                    pass

                # Create layout with proper date axis ordering
                option_type_label = "Call" if call_put == "C" else "Put"
                fig.update_layout(
                    title=f"Black-76 {option_type_label} Option Value vs Expiration Date",
                    xaxis_title="Expiration Date",
                    yaxis_title="Option Value",
                    height=400,
                    xaxis=dict(
                        tickangle=-45,
                        type='category',  # Keep as category but ensure data is pre-sorted
                        categoryorder='array',
                        categoryarray=sorted_dates  # Explicitly set the order
                    ),
                    legend=dict(
                        yanchor="top",
                        y=0.99,
                        xanchor="left",
                        x=0.01
                    )
                )

                # Add vertical line at the base expiration date
                base_exp_str = base_expiration_date.strftime('%Y-%m-%d')
                fig.update_layout(
                    shapes=[
                        dict(
                            type="line",
                            xref="x",
                            yref="paper",
                            x0=base_exp_str,
                            y0=0,
                            x1=base_exp_str,
                            y1=1,
                            line=dict(
                                color="red",
                                width=1,
                                dash="dash",
                            )
                        )
                    ],
                    annotations=[
                        dict(
                            x=base_exp_str,
                            y=1,
                            xref="x",
                            yref="paper",
                            text="Selected Expiration",
                            showarrow=False,
                            xanchor="center",
                            yanchor="bottom"
                        )
                    ]
                )

                return _style_pricer_figure(fig)

            except Exception as e:
                return _empty_pricer_figure(
                    f"Error in Black76 calculation: {str(e)}",
                    "Expiration Date",
                    "Option Price",
                )

        # KIRK SPREAD OPTION CALCULATION
        elif option_type == "kirk":
            try:
                S1, S2, K_spread, _r_spread, v1, v2, rho, _exp_date, _days_to_expiry, _T_spread = (
                    _parse_kirk_params(all_params, all_dates, all_param_ids, all_date_ids)
                )

                # Convert C/P to call/put string
                call_put_expanded = "call" if call_put == "C" else "put"

                # For each potential expiration date, calculate option value
                for exp_date in expiration_dates:
                    # Calculate time to expiry in years (from today to this expiration date)
                    days_to_expiry = (exp_date - today).days

                    # Handle dates in the past
                    if days_to_expiry <= 0:
                        # For expiration dates in the past, option is just intrinsic value
                        if call_put == "C":
                            value = max(0, S1 - S2 - K_spread)
                        else:
                            value = max(0, S2 + K_spread - S1)
                    else:
                        # Valid future expiration date - calculate Kirk price
                        T = max(days_to_expiry / 365.0, 0.001)  # Time in years

                        try:
                            value = kirk_model_with_substitution(
                                S1, S2, K_spread, v1, v2, rho, T, call_put_expanded
                            )
                        except Exception:
                            # Fallback to intrinsic value
                            if call_put == "C":
                                value = max(0, S1 - S2 - K_spread)
                            else:
                                value = max(0, S2 + K_spread - S1)

                    option_values.append(value)

                # Create the data dictionary for the figure
                chart_data = []
                for i in range(len(dates_formatted)):
                    chart_data.append({
                        'date': dates_formatted[i],
                        'value': option_values[i]
                    })

                # Sort the data by date
                chart_data.sort(key=lambda x: x['date'])

                # Extract the sorted data back into separate lists
                sorted_dates = [item['date'] for item in chart_data]
                sorted_values = [item['value'] for item in chart_data]

                # Create figure
                fig = go.Figure()

                # Add option price line with sorted data
                fig.add_trace(
                    go.Scatter(
                        x=sorted_dates,
                        y=sorted_values,
                        mode='lines',
                        name='Option Price',
                        line=dict(color='blue', width=2)
                    )
                )

                # Mark the base expiration date that was selected in the UI
                try:
                    base_exp_str = base_expiration_date.strftime('%Y-%m-%d')
                    if base_exp_str in sorted_dates:
                        base_index = sorted_dates.index(base_exp_str)
                        base_value = sorted_values[base_index]

                        fig.add_trace(
                            go.Scatter(
                                x=[base_exp_str],
                                y=[base_value],
                                mode='markers',
                                name='Base Expiration',
                                marker=dict(color='red', size=10, symbol='star')
                            )
                        )
                except Exception:
                    pass

                # Create layout with proper date axis ordering
                option_type_label = "Call" if call_put == "C" else "Put"
                fig.update_layout(
                    title=f"Kirk Spread {option_type_label} Option Value vs Expiration Date",
                    xaxis_title="Expiration Date",
                    yaxis_title="Option Value",
                    height=400,
                    xaxis=dict(
                        tickangle=-45,
                        type='category',  # Keep as category but ensure data is pre-sorted
                        categoryorder='array',
                        categoryarray=sorted_dates  # Explicitly set the order
                    ),
                    legend=dict(
                        yanchor="top",
                        y=0.99,
                        xanchor="left",
                        x=0.01
                    )
                )

                # Add vertical line at the base expiration date
                base_exp_str = base_expiration_date.strftime('%Y-%m-%d')
                fig.update_layout(
                    shapes=[
                        dict(
                            type="line",
                            xref="x",
                            yref="paper",
                            x0=base_exp_str,
                            y0=0,
                            x1=base_exp_str,
                            y1=1,
                            line=dict(
                                color="red",
                                width=1,
                                dash="dash",
                            )
                        )
                    ],
                    annotations=[
                        dict(
                            x=base_exp_str,
                            y=1,
                            xref="x",
                            yref="paper",
                            text="Selected Expiration",
                            showarrow=False,
                            xanchor="center",
                            yanchor="bottom"
                        )
                    ]
                )

                return _style_pricer_figure(fig)

            except Exception as e:
                return _empty_pricer_figure(
                    f"Error in Kirk calculation: {str(e)}",
                    "Expiration Date",
                    "Option Price",
                )

        # Unknown option type
        else:
            return _empty_pricer_figure(f"Unknown option type: {option_type}", "Expiration Date", "Option Price")

    except Exception as e:

        return _empty_pricer_figure(f"Error creating chart: {str(e)}", "Expiration Date", "Option Price")


@callback(
    Output("correlation-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [
        State("option-type", "value"),
        State("option-call-put", "value"),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date'),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'id'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'id')
    ],
    prevent_initial_call=True
)
def update_correlation_chart(n_clicks, option_type, call_put, all_params, all_dates, all_param_ids=None, all_date_ids=None):
    """
    Create a chart showing how correlation affects Kirk spread option price.
    Only applicable for Kirk model - returns empty chart for Black-76.
    """
    if n_clicks is None:
        return _empty_pricer_figure("Calculate option price first.", "Correlation (ρ)", "Option Price")

    # If Black-76 is selected, return a message indicating chart not applicable
    if option_type != "kirk":
        return _empty_pricer_figure(
            "Correlation sensitivity is only available for Kirk spread options.",
            "Correlation (ρ)",
            "Option Price",
        )

    try:
        # ---------------------------------------------------------------
        # STEP 1: Get today's date as reference point
        # ---------------------------------------------------------------
        today = date.today()

        # ---------------------------------------------------------------
        # STEP 2: Get expiration date (with careful type handling)
        # ---------------------------------------------------------------
        # Default expiration is 1 year from today
        default_expiration = today + timedelta(days=365)
        expiration_date = _parse_pricer_expiration(
            _ordered_pricer_dates(option_type, all_dates, all_date_ids),
            default_expiration,
        )


        # ---------------------------------------------------------------
        # STEP 3: Generate correlation values to test
        # ---------------------------------------------------------------
        # Create range of correlation values from -1 to 1 with step 0.05
        correlations = np.arange(-1.0, 1.01, 0.05)

        # ---------------------------------------------------------------
        # STEP 4: Parse other Kirk parameters
        # ---------------------------------------------------------------
        try:
            S1, S2, K_spread, _r_spread, v1, v2, base_rho, _exp_date, _days_to_expiry, _T_spread = (
                _parse_kirk_params(all_params, all_dates, all_param_ids, all_date_ids)
            )

            # Convert C/P to call/put string
            call_put_expanded = "call" if call_put == "C" else "put"

            # Calculate time to expiration in years
            days_to_expiry = (expiration_date - today).days
            T = max(days_to_expiry / 365.0, 0.001)  # Time in years

            # ---------------------------------------------------------------
            # STEP 5: Calculate option values for each correlation
            # ---------------------------------------------------------------
            option_values = []
            valid_correlations = []

            for rho in correlations:
                try:
                    # Calculate option value with this correlation
                    value = kirk_model_with_substitution(
                        S1, S2, K_spread, v1, v2, rho, T, call_put_expanded
                    )
                    option_values.append(value)
                    valid_correlations.append(rho)
                except Exception:
                    # Skip this correlation value if calculation fails
                    continue

            # ---------------------------------------------------------------
            # STEP 6: Create correlation chart
            # ---------------------------------------------------------------
            fig = go.Figure()

            # Add option price line
            fig.add_trace(
                go.Scatter(
                    x=valid_correlations,
                    y=option_values,
                    mode='lines',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the base correlation from UI input
            try:
                # Find the nearest correlation value to the base correlation
                nearest_idx = min(range(len(valid_correlations)),
                                  key=lambda i: abs(valid_correlations[i] - base_rho))

                fig.add_trace(
                    go.Scatter(
                        x=[valid_correlations[nearest_idx]],
                        y=[option_values[nearest_idx]],
                        mode='markers',
                        name='Base Correlation',
                        marker=dict(color='red', size=10, symbol='star')
                    )
                )
            except Exception:
                pass

            # Create layout
            option_type_label = "Call" if call_put == "C" else "Put"
            fig.update_layout(
                title=f"Kirk Spread {option_type_label} Option Value vs Correlation",
                xaxis_title="Correlation (ρ)",
                yaxis_title="Option Value",
                height=400,
                xaxis=dict(
                    tickformat='.2f'
                ),
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add vertical line at the base correlation
            fig.update_layout(
                shapes=[
                    dict(
                        type="line",
                        xref="x",
                        yref="paper",
                        x0=base_rho,
                        y0=0,
                        x1=base_rho,
                        y1=1,
                        line=dict(
                            color="red",
                            width=1,
                            dash="dash",
                        )
                    )
                ],
                annotations=[
                    dict(
                        x=base_rho,
                        y=1,
                        xref="x",
                        yref="paper",
                        text=f"ρ = {base_rho:.2f}",
                        showarrow=False,
                        xanchor="center",
                        yanchor="bottom"
                    )
                ]
            )

            return _style_pricer_figure(fig)

        except Exception as e:
            return _empty_pricer_figure(f"Error creating correlation chart: {str(e)}", "Correlation (ρ)", "Option Price")

    except Exception as e:

        return _empty_pricer_figure(f"Error creating correlation chart: {str(e)}", "Correlation (ρ)", "Option Price")
