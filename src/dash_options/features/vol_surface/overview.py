"""Monthly surface overview: presentation of saved delta nodes, without fitting."""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html
from options.vol_calibration.api import saved_delta_smile_metrics

from dash_options.core.snapshots import SnapshotReferenceError, resolve_snapshot
from dash_options.shared import surfaces
from dash_options.features.vol_surface import selector

METRICS = (
    ('atm', 'ATM', '%'),
    ('rr', 'Risk Reversal', 'vp'),
    ('bf', 'Butterfly', 'vp'),
)
WING_DELTAS = (1, 5, 10, 15, 20, 25, 30, 35, 40, 45)
CURRENT_COLOR = '#111827'
COMPARE_COLOR = '#2563eb'


def controls():
    return html.Div([
        dcc.Store(id='vol-overview-navigation', storage_type='memory'),
        dcc.Store(id='vol-overview-context', data={'months': [], 'fronts': {}}, storage_type='memory'),
        html.Div([
            html.Label('Wing delta', htmlFor='vol-overview-delta'),
            dcc.Dropdown(id='vol-overview-delta', options=[
                {'label': f'{delta}Δ', 'value': delta} for delta in WING_DELTAS
            ], value=25, clearable=False, persistence=True, persistence_type='local'),
        ], className='vol-overview-field vol-overview-delta-field'),
        html.Span(id='vol-overview-window-label', className='vol-overview-window-label'),
        html.Div([
            html.Span([html.I(className='vol-overview-line'), 'Current']),
            html.Span([html.I(className='vol-overview-line comparison'), 'Comparison']),
        ], className='vol-overview-legend'),
    ], className='vol-overview-controls')


def snapshot_frame(reference):
    """Read the requested immutable payload, never another request's active cache."""
    payload = resolve_snapshot(reference, expected_namespace=surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE)
    frame = payload.get('surface_dataset')
    if not isinstance(frame, pd.DataFrame):
        raise SnapshotReferenceError('Volatility snapshot payload is invalid')
    return frame


def settlement_slice(frame, product, date):
    if frame.empty or not date:
        return surfaces._empty_surface_df()
    return frame.loc[
        frame.code.eq(product) & frame.cob_date.dt.normalize().eq(pd.Timestamp(date).normalize())
    ].copy()


def surface_pairs(reference, date, previous_date, products, active_product=None,
                  source='settlement', selection=None, revision=None, comparison=None):
    frame = snapshot_frame(reference)
    pairs = {}
    for product in products or []:
        if product not in surfaces.SURFACE_PRODUCT_DISPLAY_MAP.values() and product not in surfaces.SURFACE_SOURCE_PRODUCTS:
            continue
        pair = {
            'current': settlement_slice(frame, product, date),
            'previous': settlement_slice(frame, product, previous_date),
            'current_label': f'Settlement {date or "unavailable"}',
            'comparison_label': f'Settlement {previous_date or "unavailable"}',
            'origins': {}, 'source': 'settlement', 'error': None,
        }
        if (product == active_product or (selection or {}).get('products') is not None) and source not in (None, 'settlement'):
            # Explicit unavailable/unauthorized replays must not leave settlement curves behind.
            pair.update(current=surfaces._empty_surface_df(), previous=surfaces._empty_surface_df(),
                        source=source, current_label='Selected surface unavailable', comparison_label='Unavailable')
            try:
                if not revision or not comparison:
                    raise selector.SelectionError('Select an available surface revision and comparison.')
                view = selector.resolve_view(selection, product, source, revision, comparison)
                metadata = view['metadata']
                pair.update(current=view['current'].copy(), previous=view['previous'].copy(),
                            current_label=metadata['current_label'], comparison_label=metadata['comparison_label'],
                            origins=metadata['month_origins'])
            except selector.SelectionError as exc:
                pair['error'] = str(exc)
        pairs[product] = pair
    return pairs


def metric_frame(frame, wing_delta=25):
    """Display the analytics API's saved-node diagnostics in IV percent / vol points."""
    return saved_delta_smile_metrics(frame, float(wing_delta) / 100,
                                    delta_grid=surfaces.PUBLISHED_SURFACE_DELTAS)


def front_month(frame):
    """First unexpired delivery in this exact surface, using its own valuation COB."""
    if frame.empty:
        return None
    cob_dates = frame.cob_date.dropna()
    as_of = cob_dates.max().normalize() if not cob_dates.empty else None
    for month, rows in frame.groupby('contract_date', sort=True):
        expiries = rows.option_expiration_date.dropna()
        expiry = expiries.min().normalize() if not expiries.empty else pd.Timestamp(month)
        if as_of is None or expiry >= as_of:
            return pd.Timestamp(month)
    return None


def delivery_months(pairs, length=24):
    """Align curves from the earliest product front, preserving missing months."""
    starts, ends = [], []
    for pair in pairs.values():
        current = pair['current']
        front = front_month(current)
        if front is not None:
            starts.append(front.to_period('M').to_timestamp())
            ends.append(current.contract_date.max().to_period('M').to_timestamp())
    if not starts:
        return pd.DatetimeIndex([])
    start, end = min(starts), max(ends)
    count = 24 if length not in (12, 24, 'all') else length
    if count != 'all':
        end = min(end, start + pd.DateOffset(months=count - 1))
    return pd.date_range(start, end, freq='MS')


def click_selection(click_data, product, valid_months):
    """Validate graph metadata before using it as a navigation request."""
    try:
        custom = click_data['points'][0]['customdata']
        if not isinstance(custom, (list, tuple)) or len(custom) < 2:
            return None
        if custom[0] == product and custom[1] in valid_months:
            return custom[1]
    except (KeyError, IndexError, TypeError):
        pass
    return None


def format_value(value, unit='', signed=False):
    if pd.isna(value):
        return '—'
    # Suppress negative zero in a displayed change.
    value = 0.0 if abs(value) < .005 else value
    return f'{value:+.2f}{unit}' if signed else f'{value:.2f}{unit}'


def metric_figure(product, metric, label, unit, current, previous, months, front_contract, pair):
    fig = go.Figure()
    if not len(months):
        return fig
    current, previous = current.reindex(months), previous.reindex(months)
    x = [m.strftime('%Y-%m-%d') for m in months]
    custom = [[product, m, pair['origins'].get(m[:7], 'Settlement' if pair['source'] == 'settlement' else 'Unavailable')]
              for m in x]
    for key, values, color, dash in (
        ('comparison_label', previous[metric], COMPARE_COLOR, 'dash'),
        ('current_label', current[metric], CURRENT_COLOR, 'solid'),
    ):
        estimated = (current if key == 'current_label' else previous).attrs.get('interpolated_months', [])
        trace_custom = [
            [*row, 'Interpolated wing IVs' if metric != 'atm' and row[1] in estimated else 'Saved delta nodes']
            for row in (custom if key == 'current_label' else [[product, m, pair['comparison_label']] for m in x])
        ]
        symbols = ['circle-open' if str(c[2]).startswith('Inherited') else 'circle' for c in custom]
        fig.add_trace(go.Scatter(
            x=x, y=[None if pd.isna(v) else float(v) for v in values],
            name=pair[key], customdata=trace_custom, mode='lines+markers', connectgaps=False,
            line={'color': color, 'width': 1.8 if dash == 'solid' else 1.3, 'dash': dash},
            marker={'size': 3.5, 'symbol': symbols if key == 'current_label' else 'circle', 'color': color},
            hovertemplate=f'%{{x|%b %Y}}<br>{label}: %{{y:.2f}} {unit}<br>%{{customdata[3]}}<br>%{{customdata[2]}}<extra>%{{fullData.name}}</extra>',
        ))
    bottom = metric == 'bf'
    step = max(1, int(np.ceil((len(months) - 1) / 3)))
    ticks = list(range(0, len(months), step))
    if len(months) - 1 - ticks[-1] > step / 2:
        ticks.append(len(months) - 1)
    fig.update_layout(
        height=128 if bottom else 115, margin=dict(l=43, r=16, t=8, b=24 if bottom else 9),
        paper_bgcolor='white', plot_bgcolor='#f8fafc', showlegend=False,
        font=dict(family='Inter, Arial, sans-serif', size=11, color='#475569'),
        hovermode='x unified', hoverlabel=dict(font_size=11),
        xaxis=dict(type='date', range=[months[0] - pd.Timedelta(days=8), months[-1] + pd.Timedelta(days=8)],
                   tickvals=[x[i] for i in ticks], ticktext=[months[i].strftime("%b'%y") for i in ticks],
                   showticklabels=bottom,
                   fixedrange=True, gridcolor='#e2e8f0', zeroline=False),
        yaxis=dict(ticksuffix='%' if metric == 'atm' else '', nticks=4, tickfont_size=11,
                   fixedrange=True, gridcolor='#e2e8f0', zeroline=metric != 'atm', zerolinecolor='#94a3b8'),
    )
    if front_contract in x:
        fig.add_vline(x=front_contract, line_color='#93b4d2', line_width=1)
    if not current[metric].notna().any():
        fig.add_annotation(text='Selected metric unavailable', x=.5, y=.5, xref='paper', yref='paper',
                           showarrow=False, font_size=11, bgcolor='rgba(255,255,255,.9)')
    return fig


def product_card(product, pair, months, active_product, wing_delta=25):
    current, previous = metric_frame(pair['current'], wing_delta), metric_frame(pair['previous'], wing_delta)
    stamp = front_month(pair['current'])
    contract = stamp.strftime("%b'%y") if stamp is not None else '—'
    children = [html.Div(html.Strong(product), className='vol-overview-card-header')]
    if pair['error']:
        children.append(html.Div(pair['error'], role='status', className='vol-overview-error'))
    for metric, label, unit in METRICS:
        label = f'{wing_delta}Δ {label}' if metric != 'atm' else label
        a = current.loc[stamp, metric] if stamp in current.index else np.nan
        b = previous.loc[stamp, metric] if stamp in previous.index else np.nan
        month = stamp.strftime('%Y-%m-%d') if stamp is not None else None
        indicative = metric != 'atm' and month in current.attrs.get('interpolated_months', []) and pd.notna(a)
        comparison_indicative = metric != 'atm' and month in previous.attrs.get('interpolated_months', []) and pd.notna(b)
        children.append(html.Div([
            html.Div([html.Span(label, className='vol-overview-metric-name'),
                      html.Span([html.Span(contract, className='vol-overview-contract',
                                           title='Front contract' if stamp is not None else 'Front contract unavailable'),
                                 html.Strong(('≈ ' if indicative else '') + format_value(a, '%' if unit == '%' else ' vp')),
                                 html.Span('Δ ' + format_value(a - b, ' vp', signed=True))],
                                title='Comparison: ' + ('≈ ' if comparison_indicative else '') + format_value(b, '%' if unit == '%' else ' vp'),
                                className='vol-overview-readout')], className='vol-overview-metric-header'),
            dcc.Graph(id={'type': 'vol-overview-chart', 'product': product, 'metric': metric},
                      figure=metric_figure(product, metric, label, unit, current, previous, months, month, pair),
                      config={'displayModeBar': False, 'displaylogo': False, 'responsive': True},
                      className='vol-overview-graph'),
        ], className='vol-overview-metric'))
    children.append(html.Div([
        html.A('Inspect smile ↓', href='#surface-section-container',
               id={'type': 'vol-overview-inspect', 'product': product}, n_clicks=0),
    ], className='vol-overview-card-footer'))
    return html.Div(children, className='volatility-chart-card vol-overview-card' +
                    (' vol-overview-card-active' if product == active_product else ''))


def atm_frames(reference, date, previous_date, products, source='settlement', selection=None, revision=None, comparison=None):
    if not date or not products:
        return pd.DataFrame(columns=['code','cob_date','contract_date','volatility'])
    pairs = surface_pairs(reference, date, previous_date, products, None, source, selection, revision, comparison)
    frames = []
    for product, pair in pairs.items():
        for side, selected_date in [('current', date), ('previous', previous_date)]:
            frame = pair[side]
            if frame.empty or not selected_date:
                continue
            # Project the exact same saved ATM node used by overview cards.
            metrics = metric_frame(frame).reset_index()
            if 'contract_date' not in metrics or 'atm' not in metrics:
                continue
            nodes = metrics[['contract_date', 'atm']].rename(columns={'atm':'volatility'})
            nodes['volatility'] /= 100
            nodes['code'], nodes['cob_date'] = product, pd.Timestamp(selected_date)
            frames.append(nodes)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=['code','cob_date','contract_date','volatility'])


def filter_delivery(frame, context):
    """Apply the shared monthly display scope without touching historical warm-up."""
    if context is None:
        return frame.copy()
    months = pd.to_datetime(context.get('months', []))
    return frame.loc[frame.contract_date.dt.to_period('M').dt.to_timestamp().isin(months)].copy()


def complete_bucket_frames(frame, context, grouping):
    """Exclude boundary buckets and missing-node aggregates, never average partial strips."""
    frame = filter_delivery(frame, context)
    if context is None or grouping == 'monthly' or frame.empty:
        return frame
    months = pd.DatetimeIndex(pd.to_datetime(context.get('months', [])))
    def bucket(month):
        if grouping == 'quarterly':
            start = month.to_period('Q').start_time
            return pd.date_range(start, periods=3, freq='MS')
        if grouping == 'calendar':
            return pd.date_range(f'{month.year}-01-01', periods=12, freq='MS')
        if grouping == 'season':
            start = pd.Timestamp(month.year if month.month >= 4 else month.year-1,
                                 4 if 4 <= month.month <= 9 else 10, 1)
            return pd.date_range(start, periods=6, freq='MS')
        return pd.DatetimeIndex([month])
    scope = set(months)
    allowed = {m for m in months if set(bucket(m)) <= scope}
    frame = frame.loc[frame.contract_date.isin(allowed)].copy()
    # Each product/date needs all the nodes belonging to its displayed bucket.
    keep = []
    for _, rows in frame.groupby(['code', 'cob_date']):
        available = set(rows.loc[rows.volatility.notna(), 'contract_date'])
        keep.extend(rows.index[rows.contract_date.map(lambda m: set(bucket(m)) <= available)])
    return frame.loc[keep].copy()
