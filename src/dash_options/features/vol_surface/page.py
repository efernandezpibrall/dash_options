"""Volatility surface dashboard page."""
import io
import json

from dash import html, dcc, callback, Output, Input, State, no_update, ALL, ctx
import dash_ag_grid as dag
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import numpy as np
import pandas as pd

from dash_options.core.dataframes import concat_dataframes
from dash_options.core.database_reads import safe_exception_message
from dash_options.core.snapshots import SnapshotReferenceError
import dash_options.shared.surfaces as surface_data
import dash_options.features.vol_surface.delta_history as delta_history_data
import dash_options.features.vol_surface.realized_comparison as realized_comparison_data
from dash_options.features.vol_surface import selector as surface_selector
from dash_options.features.vol_surface import overview


VOL_TRADES_PRODUCTS = {'BRENT', 'HH', 'JKM', 'TTF'}


VOL_CHART_FONT = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
VOL_CHART_GRID = 'rgba(148, 163, 184, 0.18)'
VOL_CHART_AXIS = '#94a3b8'
VOL_CHART_TEXT = '#0f172a'
VOL_CHART_MUTED = '#64748b'
VOL_SELECTED_LINE = '#111827'
VOL_LINE_PALETTE = ['#2563eb', '#0f766e', '#7c3aed', '#d97706', '#be123c']
VOL_SURFACE_PALETTE = ['#111827', '#2563eb', '#0f766e', '#be123c', '#7c3aed']
VOL_ABSOLUTE_SCALE = [
    [0.0, '#f8fafc'],
    [0.22, '#dbeafe'],
    [0.48, '#60a5fa'],
    [0.72, '#0f766e'],
    [1.0, '#0f172a'],
]
VOL_DIVERGING_SCALE = [
    [0.0, '#b42318'],
    [0.42, '#fee2e2'],
    [0.5, '#f8fafc'],
    [0.58, '#d1fae5'],
    [1.0, '#047857'],
]
VOL_GRAPH_CONFIG = {
    'displayModeBar': 'hover',
    'displaylogo': False,
    'responsive': True,
    'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
}


def _vol_axis(title='', tickformat=None, **overrides):
    axis = {
        'title': dict(text=title, font=dict(size=11, color=VOL_CHART_MUTED)),
        'showgrid': True,
        'gridcolor': VOL_CHART_GRID,
        'gridwidth': 1,
        'zeroline': False,
        'linecolor': VOL_CHART_AXIS,
        'linewidth': 1,
        'tickfont': dict(size=10, color=VOL_CHART_MUTED),
        'ticks': 'outside',
        'ticklen': 3,
        'automargin': True,
    }
    if tickformat:
        axis['tickformat'] = tickformat
    axis.update(overrides)
    return axis


def _vol_legend():
    return {
        'orientation': 'h',
        'yanchor': 'top',
        'y': -0.16,
        'xanchor': 'center',
        'x': 0.5,
        'bgcolor': 'rgba(255, 255, 255, 0)',
        'bordercolor': 'rgba(255, 255, 255, 0)',
        'borderwidth': 0,
        'font': dict(size=9, color=VOL_CHART_MUTED),
        'itemsizing': 'constant',
        'itemwidth': 34,
        'tracegroupgap': 4,
    }


def _apply_vol_chart_theme(fig, title=None, margin=None, height=None, hovermode='x unified', showlegend=True):
    fig.update_layout(
        title=dict(
            text=title or '',
            x=0.015,
            y=0.985,
            xanchor='left',
            yanchor='top',
            font=dict(size=14, color=VOL_CHART_TEXT, family=VOL_CHART_FONT),
            pad=dict(t=0, b=6),
        ),
        font=dict(family=VOL_CHART_FONT, size=11, color=VOL_CHART_TEXT),
        plot_bgcolor='#f8fafc',
        paper_bgcolor='white',
        margin=margin or dict(l=46, r=16, t=44, b=72),
        hovermode=hovermode,
        hoverlabel=dict(
            bgcolor='rgba(255, 255, 255, 0.96)',
            bordercolor='rgba(148, 163, 184, 0.45)',
            font=dict(size=11, color=VOL_CHART_TEXT, family=VOL_CHART_FONT),
            align='left',
        ),
        legend=_vol_legend(),
        showlegend=showlegend,
        transition=dict(duration=180, easing='cubic-in-out'),
    )
    if height is not None:
        fig.update_layout(height=height)
    return fig


def _format_vol_date(value):
    if value is None:
        return None
    try:
        return pd.to_datetime(value).strftime('%Y-%m-%d')
    except Exception:
        return str(value)


def _format_vol_mode(value):
    mode_labels = {
        'monthly': 'Monthly',
        'quarterly': 'Quarterly',
        'yearly': 'Yearly',
        'absolute': 'Absolute IV',
        'vs_atm': 'Smile vs ATM',
        'vs_previous': 'Change vs Previous',
        'fixed_expiry': 'Fixed Expiry',
        'rolling_tenor': 'Rolling Tenor',
    }
    return mode_labels.get(value, str(value).replace('_', ' ').title() if value else None)


def _format_surface_expiry_selection_label(value):
    expiry_type, key = surface_data._parse_surface_expiry_selection(value)
    if expiry_type == surface_data.SURFACE_EXPIRY_MONTH:
        return pd.to_datetime(key).strftime("%b'%y")
    if expiry_type in {surface_data.SURFACE_EXPIRY_QUARTER, surface_data.SURFACE_EXPIRY_SEASON}:
        return _format_vol_period_header(key)
    return None


def _build_surface_expiry_options(surface_df):
    if surface_df.empty:
        return []

    expiries = sorted(
        pd.to_datetime(surface_df['contract_date'], errors='coerce')
        .dropna()
        .drop_duplicates()
        .tolist()
    )
    if not expiries:
        return []

    month_options = [
        {
            'label': expiry.strftime("%b'%y"),
            'value': surface_data._surface_expiry_value(surface_data.SURFACE_EXPIRY_MONTH, expiry),
        }
        for expiry in expiries
    ]

    quarter_keys = {surface_data._surface_quarter_key(expiry) for expiry in expiries}
    quarter_keys.discard(None)
    quarter_keys = surface_data._sort_grouped_period_columns(quarter_keys, 'quarterly')
    quarter_options = [
        {
            'label': _format_vol_period_header(key),
            'value': surface_data._surface_expiry_value(surface_data.SURFACE_EXPIRY_QUARTER, key),
        }
        for key in quarter_keys
    ]

    season_keys = {surface_data._surface_season_key(expiry) for expiry in expiries}
    season_keys.discard(None)
    season_keys = surface_data._sort_grouped_period_columns(season_keys, 'season')
    season_options = [
        {
            'label': _format_vol_period_header(key),
            'value': surface_data._surface_expiry_value(surface_data.SURFACE_EXPIRY_SEASON, key),
        }
        for key in season_keys
    ]

    return month_options + quarter_options + season_options


def _build_vol_chart_chip(label, value=None, tone='neutral'):
    if value is None or value == '':
        return None
    return html.Span(
        [
            html.Span(label, className='volatility-chart-chip-label'),
            html.Span(str(value), className='volatility-chart-chip-value'),
        ],
        className=f'volatility-chart-chip volatility-chart-chip-{tone}',
    )


def _build_vol_chart_header(title):
    return html.Div(
        [
            html.Div(
                [html.H5(title, className='volatility-chart-card-title')],
                className='volatility-chart-card-title-group',
            ),
        ],
        className='volatility-chart-card-header',
    )


def _build_vol_chart_card(graph, title, className=None):
    classes = ['volatility-chart-card']
    if className:
        classes.append(className)
    return html.Div(
        [
            _build_vol_chart_header(title),
            graph,
        ],
        className=' '.join(classes),
    )


def _get_delta_bucket_options(surface_df):
    if surface_df.empty:
        return []

    delta_order = surface_data._get_surface_delta_order(surface_df)
    return [
        {'label': bucket, 'value': bucket}
        for bucket in delta_order['delta_bucket'].tolist()
    ]


def _format_surface_table_df(
    pivot_df,
    cob_date=None,
    dte_lookup=None,
    allow_contract_date_fallback=True,
):
    if pivot_df.empty:
        return pd.DataFrame(columns=['expiry', 'dte'])

    formatted = pivot_df.copy().reset_index()
    if cob_date is not None:
        cob_date = pd.to_datetime(cob_date)
        formatted['contract_date'] = pd.to_datetime(formatted['contract_date'], errors='coerce')
        dte_date = formatted['contract_date'] if allow_contract_date_fallback else pd.Series(
            pd.NaT,
            index=formatted.index,
            dtype='datetime64[ns]',
        )
        if dte_lookup is not None and not dte_lookup.empty:
            formatted = formatted.merge(dte_lookup, on='contract_date', how='left')
            verified_expiry = pd.to_datetime(formatted['option_expiration_date'], errors='coerce')
            dte_date = (
                verified_expiry.combine_first(formatted['contract_date'])
                if allow_contract_date_fallback
                else verified_expiry
            )
            formatted = formatted.drop(columns=['option_expiration_date'])
        formatted['dte'] = (dte_date - cob_date).dt.days.astype('Int64')
    else:
        formatted['dte'] = None
    formatted = formatted.rename(columns={'contract_date': 'expiry'})
    formatted['expiry'] = pd.to_datetime(formatted['expiry']).dt.strftime('%Y-%m')
    value_columns = [column for column in formatted.columns if column not in ['expiry', 'dte']]
    return formatted[['expiry', 'dte'] + value_columns]


def _empty_figure(message, title):
    fig = go.Figure()
    _apply_vol_chart_theme(
        fig,
        None,
        margin=dict(l=24, r=20, t=18, b=24),
        hovermode=False,
        showlegend=False,
    )
    fig.update_layout(xaxis=dict(visible=False), yaxis=dict(visible=False))
    fig.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref='paper',
        yref='paper',
        showarrow=False,
        font=dict(size=13, color=VOL_CHART_MUTED, family=VOL_CHART_FONT),
        align='center',
    )
    return fig






def _calculate_symmetric_color_range(values, default_range=0.05):
    valid_values = values.stack().dropna().abs() if isinstance(values, pd.DataFrame) else pd.Series(values).dropna().abs()
    if valid_values.empty:
        return -default_range, default_range

    max_abs = valid_values.quantile(0.95)
    if pd.isna(max_abs) or max_abs <= 0:
        max_abs = valid_values.max()
    if pd.isna(max_abs) or max_abs <= 0:
        max_abs = default_range
    return -float(max_abs), float(max_abs)


def _calculate_heatmap_bounds(values):
    value_range = values.stack().dropna()
    if value_range.empty:
        return 0.0, 1.0

    zmin = float(value_range.quantile(0.05))
    zmax = float(value_range.quantile(0.95))
    if zmin == zmax:
        zmin = float(value_range.min())
        zmax = float(value_range.max())
        if zmin == zmax:
            zmin -= 0.05
            zmax += 0.05
    return zmin, zmax


def _prepare_heatmap_matrix(current_surface, previous_surface, heatmap_mode):
    pivot_df, delta_columns = surface_data._build_surface_pivot(current_surface)
    if pivot_df.empty or not delta_columns:
        return None, None, None, None, None, None

    absolute_values = pivot_df[delta_columns].copy()
    display_values = absolute_values.copy()
    title_suffix = 'Absolute IV'
    colorbar_title = 'Vol'
    colorscale = VOL_ABSOLUTE_SCALE
    zmid = None
    hover_template = 'Expiry %{y}<br>Bucket %{x}<br>Vol %{z:.2%}<extra></extra>'
    zmin, zmax = _calculate_heatmap_bounds(display_values)

    if heatmap_mode == 'vs_atm':
        if 'ATM' not in absolute_values.columns or not absolute_values['ATM'].notna().any():
            return None, None, None, None, None, 'No ATM bucket is available for the selected surface.'

        display_values = absolute_values.sub(absolute_values['ATM'], axis=0)
        title_suffix = 'Smile vs ATM'
        colorbar_title = 'Vol - ATM'
        colorscale = VOL_DIVERGING_SCALE
        zmid = 0.0
        zmin, zmax = _calculate_symmetric_color_range(display_values)
        hover_template = 'Expiry %{y}<br>Bucket %{x}<br>Vol %{customdata:.2%}<br>vs ATM %{z:+.2%}<extra></extra>'

    elif heatmap_mode == 'vs_previous':
        previous_pivot, _ = surface_data._build_surface_pivot(previous_surface)
        if previous_pivot.empty:
            return None, None, None, None, None, 'No previous-date surface data is available for comparison.'

        previous_aligned = previous_pivot.reindex(index=absolute_values.index, columns=delta_columns)
        display_values = absolute_values - previous_aligned
        title_suffix = 'Change vs Previous'
        colorbar_title = 'Vol Change'
        colorscale = VOL_DIVERGING_SCALE
        zmid = 0.0
        zmin, zmax = _calculate_symmetric_color_range(display_values)
        hover_template = 'Expiry %{y}<br>Bucket %{x}<br>Vol %{customdata:.2%}<br>Change %{z:+.2%}<extra></extra>'

    return absolute_values, display_values, delta_columns, title_suffix, (colorscale, colorbar_title, zmid, zmin, zmax), hover_template


def _create_surface_heatmap_figure(product, current_surface, previous_surface, heatmap_mode, selected_expiry=None):
    if current_surface.empty:
        return _empty_figure(f'No surface data available for {product} on the selected date.', f'{product} Surface Heatmap')

    matrix = _prepare_heatmap_matrix(current_surface, previous_surface, heatmap_mode)
    absolute_values, display_values, delta_columns, title_suffix, layout_values, hover_template = matrix
    if absolute_values is None or display_values is None or not delta_columns:
        message = hover_template if layout_values is None and isinstance(hover_template, str) else f'No surface data available for {product} on the selected date.'
        return _empty_figure(message, f'{product} Surface Heatmap')

    colorscale, colorbar_title, zmid, zmin, zmax = layout_values
    y_labels = [pd.to_datetime(expiry).strftime('%Y-%m') for expiry in absolute_values.index]

    fig = go.Figure(
        data=go.Heatmap(
            z=display_values[delta_columns].to_numpy(),
            customdata=absolute_values[delta_columns].to_numpy(),
            x=delta_columns,
            y=y_labels,
            colorscale=colorscale,
            zmid=zmid,
            zmin=zmin,
            zmax=zmax,
            colorbar=dict(
                title=dict(text=colorbar_title, font=dict(size=10, color=VOL_CHART_MUTED)),
                thickness=10,
                len=0.78,
                x=1.015,
                tickfont=dict(size=9, color=VOL_CHART_MUTED),
                tickformat='.1%',
                outlinewidth=0,
            ),
            hovertemplate=hover_template,
            xgap=2,
            ygap=2
        )
    )

    _apply_vol_chart_theme(
        fig,
        None,
        margin=dict(l=52, r=46, t=18, b=34),
        hovermode='closest',
        showlegend=False,
    )
    fig.update_xaxes(**_vol_axis('Delta', showgrid=False, tickangle=0))
    fig.update_yaxes(**_vol_axis('Expiry', type='category', showgrid=False, autorange='reversed',
                                   tickmode='array', tickvals=y_labels[::6],
                                   ticktext=[pd.Timestamp(m).strftime('%b %Y') for m in y_labels[::6]]))
    return fig


def _get_smile_axis_config(expiry_df):
    ordered_df = expiry_df.drop_duplicates('delta_bucket').sort_values(['delta_sort_key', 'delta_bucket']).copy()
    ordered_buckets = ordered_df['delta_bucket'].tolist()
    return ordered_buckets, ordered_buckets, dict(title='Delta Bucket')


def _build_smile_series(surface_slice, ordered_buckets):
    if surface_slice.empty:
        return pd.Series(index=ordered_buckets, dtype='float64')

    deduped_slice = (
        surface_slice
        .groupby('delta_bucket', as_index=False)
        .agg({'volatility': 'mean', 'delta_sort_key': 'min'})
        .sort_values(['delta_sort_key', 'delta_bucket'])
    )
    return deduped_slice.set_index('delta_bucket')['volatility'].reindex(ordered_buckets)


def _create_smile_evolution_figure(product, selected_expiry, current_surface, previous_surface, lookback_days, end_date,
                                 *, current_label=None, comparison_label=None):
    if current_surface.empty or selected_expiry is None:
        return _empty_figure('Select an expiry with available surface data to see the smile.', f'{product} Smile Evolution')

    expiry_label = _format_surface_expiry_selection_label(selected_expiry) or 'selected expiry'
    current_expiry = surface_data._filter_surface_by_expiry_selection(current_surface, selected_expiry)

    if current_expiry.empty:
        return _empty_figure(f'No current-date smile data available for {expiry_label}.', f'{product} Smile Evolution')

    combined = current_expiry.copy()
    previous_expiry = pd.DataFrame()
    if not previous_surface.empty:
        previous_expiry = surface_data._filter_surface_by_expiry_selection(previous_surface, selected_expiry)
        if not previous_expiry.empty:
            combined = concat_dataframes([combined, previous_expiry], ignore_index=True)

    ordered_buckets, x_values, xaxis = _get_smile_axis_config(combined)

    current_date = pd.to_datetime(current_surface['cob_date'].iloc[0]).normalize()
    end_date = pd.to_datetime(end_date).normalize()
    lookback_days = int(lookback_days) if lookback_days is not None else 30
    start_date = end_date - pd.Timedelta(days=lookback_days)

    history_df = surface_data.surface_dataset.copy()
    if not history_df.empty:
        history_df = history_df[
            (history_df['code'] == product) &
            (history_df['cob_date'].dt.normalize() >= start_date) &
            (history_df['cob_date'].dt.normalize() <= end_date)
        ].copy()
        history_df = surface_data._filter_surface_by_expiry_selection(history_df, selected_expiry)

    previous_date = None
    if not previous_expiry.empty:
        previous_date = pd.to_datetime(previous_expiry['cob_date'].iloc[0]).normalize()

    excluded_dates = {current_date}
    if previous_date is not None:
        excluded_dates.add(previous_date)

    auxiliary_dates = []
    if not history_df.empty:
        unique_dates = sorted(history_df['cob_date'].dt.normalize().dropna().drop_duplicates(), reverse=True)
        auxiliary_dates = [date for date in unique_dates if date not in excluded_dates][:4]

    fig = go.Figure()
    current_series = _build_smile_series(current_expiry, ordered_buckets)
    fig.add_trace(go.Scatter(
        x=x_values,
        y=current_series.values,
        mode='lines+markers',
        name=current_label or f"Current {current_date.strftime('%Y-%m-%d')}",
        line=dict(color=VOL_SELECTED_LINE, width=2.6),
        marker=dict(size=7, color=VOL_SELECTED_LINE, line=dict(width=1.2, color='white')),
        customdata=ordered_buckets,
        hovertemplate='Bucket %{customdata}<br>Vol %{y:.2%}<extra></extra>',
    ))

    if not previous_expiry.empty:
        previous_series = _build_smile_series(previous_expiry, ordered_buckets)
        fig.add_trace(go.Scatter(
            x=x_values,
            y=previous_series.values,
            mode='lines+markers',
            name=comparison_label or f"Previous {previous_date.strftime('%Y-%m-%d') if previous_date is not None else ''}",
            line=dict(color=VOL_CHART_MUTED, width=1.5, dash='dash'),
            marker=dict(size=6, color='white', line=dict(width=1.2, color=VOL_CHART_MUTED)),
            customdata=ordered_buckets,
            hovertemplate='Bucket %{customdata}<br>Vol %{y:.2%}<extra></extra>',
        ))

    auxiliary_palette = ['#2563eb', '#0f766e', '#7c3aed', '#d97706']
    for index, cob_date in enumerate(auxiliary_dates):
        date_slice = history_df[history_df['cob_date'].dt.normalize() == cob_date].copy()
        if date_slice.empty:
            continue
        date_series = _build_smile_series(date_slice, ordered_buckets)
        color = auxiliary_palette[index % len(auxiliary_palette)]
        fig.add_trace(go.Scatter(
            x=x_values,
            y=date_series.values,
            mode='lines+markers',
            name=pd.Timestamp(cob_date).strftime('%Y-%m-%d'),
            line=dict(color=color, width=1.15),
            marker=dict(size=4.5, color=color),
            opacity=0.50,
            customdata=ordered_buckets,
            hovertemplate='Bucket %{customdata}<br>Vol %{y:.2%}<extra></extra>',
        ))

    _apply_vol_chart_theme(
        fig,
        None,
        margin=dict(l=46, r=14, t=18, b=76),
    )
    fig.update_xaxes(**_vol_axis('', **{key: value for key, value in xaxis.items() if key != 'title'}))
    fig.update_yaxes(**_vol_axis('IV', tickformat='.0%'))

    return fig


def _create_shared_delta_history_figure(product, contracts, buckets, days, end, revision=None):
    if not contracts:
        return _empty_figure('Select one or more history contracts.', 'Delta Vol History')
    try:
        published, archived, prices, meta = delta_history_data.load_history(
            product,contracts,end,days,
            overview.snapshot_frame(revision) if isinstance(revision, dict) else surface_data.surface_dataset,revision)
        fig = make_subplots(specs=[[{'secondary_y': True}]])
        for month_index,month in enumerate(contracts):
            label = pd.Timestamp(month).strftime('%b-%y')
            for bucket_index,bucket in enumerate(buckets):
                color = VOL_LINE_PALETTE[(month_index*3+bucket_index)%len(VOL_LINE_PALETTE)]
                for frame,origin,dash in ((published,'Settlement','solid'),(archived,'Archived reconstruction','dot')):
                    series = frame.loc[frame.contract_date.eq(pd.Timestamp(month))&frame.delta_bucket.eq(bucket)]
                    if series.empty:
                        continue
                    # A trading-date grid exposes missing/rejected sessions instead of bridging them.
                    dates = meta['sessions'].union(pd.DatetimeIndex(series.cob_date)).sort_values()
                    series = series.set_index('cob_date').reindex(dates)
                    fig.add_trace(go.Scatter(x=series.index,y=series.volatility,mode='lines+markers',
                        name=f'{label} {bucket} · {origin}',line=dict(color=color,width=2,dash=dash),
                        marker=dict(size=3),connectgaps=False,
                        hovertemplate='COB %{x|%Y-%m-%d}<br>%{fullData.name}<br>IV %{y:.2%}<extra></extra>'),secondary_y=False)
            selected = prices.loc[prices.contract_date.eq(pd.Timestamp(month))]
            if selected.settlement_price.notna().any():
                fig.add_trace(go.Scatter(x=selected.cob_date,y=selected.settlement_price,
                    mode='lines',name=f'{label} settlement',connectgaps=False,
                    line=dict(color=VOL_SURFACE_PALETTE[month_index%len(VOL_SURFACE_PALETTE)],width=1.5,dash='dash'),
                    customdata=selected.source_security,
                    hovertemplate='COB %{x|%Y-%m-%d}<br>%{fullData.name}<br>Price %{y:.3f}<br>%{customdata}<extra></extra>'),secondary_y=True)
        _apply_vol_chart_theme(fig,None,margin=dict(l=48,r=60,t=40,b=108))
        fig.update_layout(legend={**_vol_legend(),'y':-.25})
        fig.update_xaxes(**_vol_axis('COB',tickformat='%d %b',range=[meta['requested_start'],meta['requested_end']]))
        fig.update_yaxes(**_vol_axis('Implied volatility (%)',tickformat='.0%'),secondary_y=False)
        fig.update_yaxes(**_vol_axis('Settlement ('+meta['price_unit']+')',tickformat='.2f',showgrid=False),secondary_y=True)
        note = 'Settlement IV · missing sessions remain gaps.'
        if not archived.empty:
            note += ' Dotted lines: archived reconstruction.'
        if meta['errors']:
            note += ' '+ ' | '.join(meta['errors'])
        fig.add_annotation(text=note,xref='paper',yref='paper',x=0,y=1.12,showarrow=False,xanchor='left',font=dict(size=10))
        fig.layout.transition = None
        fig.update_layout(meta={k:v for k,v in meta.items() if k!='sessions'},
                          uirevision=f"{revision}:{product}:{contracts}:{buckets}:{days}")
        return fig
    except Exception as exc:
        return _empty_figure(f'{product} history unavailable: {safe_exception_message(exc)}','Delta Vol History')


def _add_selected_history_points(fig, frame, metadata, contracts, buckets, *, rv=False):
    """Overlay a selected saved revision without rewriting settlement history."""
    if not metadata or frame.empty or not contracts:
        return
    date = pd.Timestamp(metadata['business_date'])
    window = fig.layout.meta or {}
    if (window.get('requested_start') is not None and
            not pd.Timestamp(window['requested_start']) <= date <= pd.Timestamp(window['requested_end'])):
        return
    points = frame.loc[frame.contract_date.isin(pd.to_datetime(contracts)) & frame.delta_bucket.isin(buckets)]
    for (month, bucket), rows in points.groupby(['contract_date','delta_bucket']):
        if bucket == 'ATM':
            rows = rows.loc[rows.put_call.eq('call')]
        if len(rows) != 1:
            continue
        trace = go.Scatter(x=[date], y=[float(rows.volatility.iloc[0])], mode='markers',
            marker=dict(symbol='diamond', size=10, color='#d97706', line=dict(color='white',width=1)),
            name=f"{pd.Timestamp(month):%b-%y} {bucket} · {metadata['current_label']}",
            hovertemplate='%{x|%Y-%m-%d}<br>%{fullData.name}<br>IV %{y:.2%}<extra></extra>')
        if rv:
            fig.add_trace(trace,row=1,col=1)
        else:
            fig.add_trace(trace,secondary_y=False)
    fig.update_layout(meta={**window, 'selected_surface':metadata})


def _create_realized_comparison_figure(product, contract, lookback_days, end_date, surface_frame, revision=None, history_mode="fixed_expiry"):
    try:
        loader = (delta_history_data.load_rolling_comparison if history_mode == 'rolling_tenor'
                  else realized_comparison_data.load_comparison)
        rows,meta=loader(product,contract,end_date,lookback_days,surface_frame,revision)
        if rows.empty:
            return _empty_figure('No trading sessions for this maturity in the selected range.','Realized vs Implied Volatility'), 'No observations in the selected range.'
        label=product+' '+pd.Timestamp(contract).strftime('%b-%y')
        if meta.get('tenor_rank'):
            label = f"{product} M{meta['tenor_rank']} · expiry-based roll"
        fig=make_subplots(rows=2,cols=1,shared_xaxes=True,row_heights=[.72,.28],vertical_spacing=.10)
        common=np.column_stack([rows.window_start.dt.strftime('%Y-%m-%d'),rows.window_end.dt.strftime('%Y-%m-%d'),
                                rows.return_count,rows.days_to_expiry.astype('Int64').astype(str).replace('<NA>',''),rows.iv_source.fillna('Unavailable'),rows.source_security])
        fig.add_trace(go.Scatter(x=rows.cob_date,y=rows.volatility,mode='lines+markers',name='Implied · 50Δ call',
            line=dict(color=VOL_SELECTED_LINE,width=2),marker=dict(size=4),connectgaps=False,customdata=common,
            hovertemplate='COB %{x|%Y-%m-%d}<br>50Δ call IV %{y:.2%}<br>Option DTE %{customdata[3]}'
                          '<br>%{customdata[4]}<extra></extra>'),row=1,col=1)
        fig.add_trace(go.Scatter(x=rows.cob_date,y=rows.realized_volatility,mode='lines',name='Realized · 30 calendar days',
            line=dict(color='#0f766e',width=2,dash='dash'),connectgaps=False,customdata=common,
            hovertemplate='COB %{x|%Y-%m-%d}<br>Realized vol %{y:.2%}<br>Window (%{customdata[0]}, %{customdata[1]}]'
                          '<br>%{customdata[2]} daily log returns · annualized √252'
                          '<br>%{customdata[5]}<extra></extra>'),row=1,col=1)
        fig.add_trace(go.Scatter(x=rows.cob_date,y=rows.spread*100,mode='lines+markers',name='IV minus trailing RV',showlegend=False,
            line=dict(color='#7c3aed',width=1.6),marker=dict(size=3),connectgaps=False,
            hovertemplate='COB %{x|%Y-%m-%d}<br>IV − trailing RV %{y:.2f} vol points<extra></extra>'),row=2,col=1)
        fig.add_hline(y=0,line_width=1,line_color=VOL_CHART_AXIS,row=2,col=1)
        _apply_vol_chart_theme(fig,None,margin=dict(l=54,r=18,t=18,b=80))
        fig.update_layout(legend={**_vol_legend(),'y':-.20},hovermode='x unified')
        fig.update_yaxes(**_vol_axis('Annualized volatility (%)',tickformat='.0%'),row=1,col=1)
        fig.update_yaxes(**_vol_axis('IV − RV (vol points)',tickformat='.1f'),row=2,col=1)
        fig.update_xaxes(**_vol_axis('',range=[meta['requested_start'],meta['requested_end']],showticklabels=False),row=1,col=1)
        fig.update_xaxes(**_vol_axis('COB',tickformat='%d %b',range=[meta['requested_start'],meta['requested_end']]),row=2,col=1)
        fig.layout.transition=None
        fig.update_layout(meta={**meta,'iv_observations':int(rows.volatility.notna().sum()),
                                'rv_observations':int(rows.realized_volatility.notna().sum()),
                                'rolling_window_calendar_days':30,'annualization_trading_days':252,'standard_deviation_ddof':1},
                          uirevision=f"{meta['input_fingerprint']}:{contract}:{lookback_days}")
        coverage = meta.get('coverage', {})
        expected = coverage.get('iv_expected', len(rows))
        status = (f"{label} · IV {rows.volatility.notna().sum()}/{expected} · RV {rows.realized_volatility.notna().sum()}/{len(rows)}"
                  f" · {meta['price_source']} through {meta['last_price_date']}"
                  ' · IV − trailing RV compares different periods')
        return fig,status
    except Exception as exc:
        message=f'{product} comparison unavailable: {safe_exception_message(exc)}'
        return _empty_figure(message,'Realized vs Implied Volatility'),message


def _create_delta_history_figure(product, selected_expiry, selected_buckets, lookback_days, end_date, history_mode,
                               history_contracts=None, snapshot_reference=None):
    if history_mode == 'fixed_expiry' and history_contracts is not None and selected_buckets and end_date:
        return _create_shared_delta_history_figure(product,history_contracts,selected_buckets,lookback_days,end_date,snapshot_reference)
    if product is None or selected_expiry is None or not selected_buckets or end_date is None:
        return _empty_figure('Select a product, expiry, and delta bucket to view history.', 'Delta Vol History')

    end_date = pd.to_datetime(end_date)
    expiry_type, expiry_key = surface_data._parse_surface_expiry_selection(selected_expiry)
    if expiry_type is None:
        return _empty_figure('Select a valid expiry, quarter, or season to view history.', 'Delta Vol History')
    lookback_days = int(lookback_days) if lookback_days is not None else 30
    start_date = end_date - pd.Timedelta(days=lookback_days)

    history_df = (overview.snapshot_frame(snapshot_reference) if isinstance(snapshot_reference, dict)
                  else surface_data.surface_dataset).copy()
    if history_df.empty:
        return _empty_figure('No surface history available.', 'Delta Vol History')

    history_df = history_df[
        (history_df['code'] == product) &
        (history_df['cob_date'] >= start_date) &
        (history_df['cob_date'] <= end_date)
    ].copy()

    if history_mode == 'fixed_expiry' or expiry_type != surface_data.SURFACE_EXPIRY_MONTH:
        history_df = surface_data._filter_surface_by_expiry_selection(history_df, selected_expiry)
    else:
        full = overview.snapshot_frame(snapshot_reference) if isinstance(snapshot_reference, dict) else surface_data.surface_dataset
        schedule, tenor_rank = delta_history_data.rolling_schedule(full, product, expiry_key, end_date, lookback_days)
        history_df = history_df.merge(schedule, on=['cob_date', 'contract_date'], how='inner', validate='many_to_one')

    history_df = history_df.loc[history_df.delta_bucket.isin(selected_buckets)]

    if history_df.empty:
        return _empty_figure(
            f'No history is available for the selected buckets over the last {lookback_days} days.',
            'Delta Vol History'
        )

    # Preserve the underlying contract behind a rolling point. No substitution
    # or connecting lines across absent settlement sessions.
    history_df = history_df.groupby(['cob_date', 'delta_bucket'], as_index=False).agg(
        volatility=('volatility', 'mean'), contract_date=('contract_date', 'first'))
    start_date, end_date, sessions, _ = delta_history_data.comparison_calendar(product, end_date, lookback_days)
    sessions = sessions[sessions >= start_date]

    fig = go.Figure()
    color_palette = VOL_SURFACE_PALETTE
    for index, bucket in enumerate(selected_buckets):
        bucket_df = history_df[history_df['delta_bucket'] == bucket].copy()
        if bucket_df.empty:
            continue
        bucket_df = bucket_df.set_index('cob_date').reindex(sessions).rename_axis('cob_date').reset_index()

        color = color_palette[index % len(color_palette)]
        fig.add_trace(go.Scatter(
            x=bucket_df['cob_date'],
            y=bucket_df['volatility'],
            mode='lines+markers',
            name=bucket,
            connectgaps=False,
            customdata=bucket_df.contract_date.dt.strftime('%b-%y').fillna('Unavailable'),
            line=dict(color=color, width=2),
            marker=dict(size=6, color=color, line=dict(width=1, color='white')),
            hovertemplate='COB %{x|%Y-%m-%d}<br>Contract %{customdata}<br>Bucket %{fullData.name}<br>Vol %{y:.2%}<extra></extra>',
        ))

    _apply_vol_chart_theme(
        fig,
        None,
        margin=dict(l=46, r=16, t=18, b=76),
    )
    fig.update_xaxes(**_vol_axis('COB', range=[start_date, end_date]))
    fig.update_yaxes(**_vol_axis('IV', tickformat='.0%'))
    if history_mode == 'rolling_tenor':
        changes = schedule.loc[schedule.contract_date.ne(schedule.contract_date.shift()) & schedule.contract_date.notna()]
        for row in changes.iloc[1:].itertuples():
            fig.add_vline(x=row.cob_date, line_dash='dot', line_width=1, line_color=VOL_CHART_MUTED)
        fig.add_annotation(text=f'M{tenor_rank} · expiry-based roll · hover for contract', xref='paper', yref='paper',
                           x=0, y=1.1, showarrow=False, xanchor='left', font=dict(size=10))
        fig.update_layout(meta={'history_mode':'rolling_tenor', 'tenor_rank':tenor_rank,
                               'iv_snapshot_reference':snapshot_reference})


    if not fig.data:
        return _empty_figure(
            f'No history is available for the selected buckets over the last {lookback_days} days.',
            'Delta Vol History'
        )

    return fig


def _build_surface_tables(current_surface, previous_surface, cob_date):
    current_pivot, current_columns = surface_data._build_surface_pivot(current_surface)
    previous_pivot, previous_columns = surface_data._build_surface_pivot(previous_surface)
    dte_lookup = surface_data._build_surface_dte_lookup(
        concat_dataframes([current_surface, previous_surface], ignore_index=True)
    )
    is_brent_surface = 'Brent' in set(current_surface.get('code', pd.Series(dtype=str)).dropna())

    combined_columns = current_columns[:]
    for column in previous_columns:
        if column not in combined_columns:
            combined_columns.append(column)

    if combined_columns:
        combined_order = (
            concat_dataframes([
                current_surface[['delta_bucket', 'delta_sort_key']],
                previous_surface[['delta_bucket', 'delta_sort_key']]
            ], ignore_index=True)
            .drop_duplicates()
            .sort_values(['delta_sort_key', 'delta_bucket'])
        )
        combined_columns = [col for col in combined_order['delta_bucket'].tolist() if col in combined_columns]

    current_table_df = _format_surface_table_df(
        current_pivot.reindex(columns=combined_columns),
        cob_date=cob_date,
        dte_lookup=dte_lookup,
        allow_contract_date_fallback=not is_brent_surface,
    )

    diff_table_df = pd.DataFrame()
    if not current_pivot.empty and not previous_pivot.empty:
        all_expiries = current_pivot.index.union(previous_pivot.index)
        diff_pivot = current_pivot.reindex(index=all_expiries, columns=combined_columns) - previous_pivot.reindex(index=all_expiries, columns=combined_columns)
        diff_table_df = _format_surface_table_df(
            diff_pivot,
            cob_date=cob_date,
            dte_lookup=dte_lookup,
            allow_contract_date_fallback=not is_brent_surface,
        )

    return current_table_df, diff_table_df, combined_columns


def _selected_surface_pair(product, date, previous_date, source='settlement', selection=None,
                           revision=None, comparison=None):
    if source == 'settlement':
        return (surface_data._get_surface_snapshot(product, date),
                surface_data._get_surface_snapshot(product, previous_date) if previous_date else surface_data._empty_surface_df(),
                None)
    if not revision or not comparison:
        raise surface_selector.SelectionError('Select an available surface revision and comparison.')
    view = surface_selector.resolve_view(selection, product, source, revision, comparison)
    return view['current'].copy(), view['previous'].copy(), view['metadata']


def _add_surface_origins(current_table, difference_table, metadata):
    if metadata:
        for frame in (current_table, difference_table):
            if not frame.empty:
                frame['origin'] = frame['expiry'].map(metadata['month_origins']).fillna('Unavailable in selected revision')


def _selected_surface_status(product, source, metadata, current, previous):
    months = current.contract_date.nunique()
    updated = len(metadata['changed_months'])
    message = (
        f"Source: {metadata['current_label']} | Product: {product} | "
        f"Valuation date: {metadata['business_date']} | Compare: {metadata['comparison_label']} | "
        f"Anchor: {metadata['settlement_cob']} ({metadata['base_publication_id']}) | "
        f"Months updated: {updated}/{months}; inherited: {months-updated} | "
        f"Comparison: {'available' if not previous.empty else 'unavailable'}. "
        "Overview and tables use the page selection. History retains settlement observations and labelled revision points."
    )
    return _build_message_span(message, tone='warning' if source == 'draft' else 'success')


def _clean_vol_table_records(dataframe):
    records = []
    for row in dataframe.to_dict('records'):
        clean_row = {}
        for key, value in row.items():
            clean_row[key] = None if pd.isna(value) else value
        records.append(clean_row)
    return records


def _format_vol_table_sample(value, signed=False):
    if value is None or pd.isna(value):
        return '—'
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return str(value)
    number = 0.0 if abs(number) < .005 else number
    sign = '+' if signed and number > 0 else ''
    return f'{sign}{number:.2f}'


def _clamp_vol_width(value, min_width, max_width):
    return max(min_width, min(int(round(value)), max_width))


def _estimate_vol_table_width(dataframe, column, signed=False):
    samples = [_format_vol_period_header(column)]
    if column in dataframe.columns:
        samples.extend(
            _format_vol_table_sample(value, signed=signed)
            for value in dataframe[column].dropna().tolist()[:200]
        )
    content_length = max((len(sample) for sample in samples if sample), default=len(str(column)))
    return _clamp_vol_width(content_length * 7 + 24, 68, 104)


def _vol_raw_field(column):
    return f'__raw_{column}'


def _prepare_vol_table_display_df(dataframe, period_columns, include_diff_colors=False):
    base_df = dataframe[['product'] + period_columns].copy()
    display_columns = {}
    raw_columns = {}
    for column in period_columns:
        raw_field = _vol_raw_field(column)
        raw_series = pd.to_numeric(base_df[column], errors='coerce')
        raw_columns[raw_field] = raw_series
        display_columns[column] = raw_series.apply(
            lambda value: _format_vol_table_sample(value, signed=include_diff_colors)
        )
    return concat_dataframes(
        [
            base_df[['product']],
            pd.DataFrame(display_columns, index=base_df.index),
            pd.DataFrame(raw_columns, index=base_df.index),
        ],
        axis=1,
    )


def _vol_period_group(column):
    label = str(column)
    parts = label.split('-')
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        year = f'20{parts[1]}' if len(parts[1]) == 2 else parts[1]
        return year, 'month'
    if len(label) == 4 and label.isdigit():
        return 'Calendar Years', 'year'
    if '-Q' in label:
        return label.split('-Q', 1)[0], 'quarter'
    if '-Summer' in label or '-Winter' in label:
        return label.split('-', 1)[0], 'season'
    return 'Tenors', 'period'


def _format_vol_period_header(column):
    label = str(column)
    parts = label.split('-')
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        try:
            return pd.to_datetime(label, format='%m-%y').strftime("%b'%y")
        except (TypeError, ValueError):
            return label
    if '-Q' in label:
        year, quarter = label.split('-Q', 1)
        return f"Q{quarter}'{year[-2:]}"
    if '-Summer' in label or '-Winter' in label:
        year, season = label.split('-', 1)
        if season == 'Winter':
            try:
                next_year = (int(year) + 1) % 100
                return f"{season}'{year[-2:]}/{next_year:02d}"
            except (TypeError, ValueError):
                return label
        return f"{season}'{year[-2:]}"
    return label


def _build_vol_table_column_defs(dataframe, period_columns, include_diff_colors=False):
    product_values = dataframe['product'].dropna().astype(str).tolist() if 'product' in dataframe.columns else []
    product_width = _clamp_vol_width(max([len('Product'), *[len(value) for value in product_values]], default=7) * 8 + 34, 94, 140)
    column_defs = [{
        'headerName': 'Product',
        'field': 'product',
        'pinned': 'left',
        'lockPinned': True,
        'suppressMovable': True,
        'sortable': False,
        'filter': False,
        'resizable': True,
        'width': product_width,
        'minWidth': product_width,
        'maxWidth': max(product_width, 140),
        'cellClass': 'mckinsey-ag-grid-cell mckinsey-ag-grid-text-cell volatility-table-product-cell',
        'headerClass': 'mckinsey-ag-grid-header volatility-table-product-header',
        'tooltipField': 'product',
    }]

    for index, column in enumerate(period_columns):
        group_label, family = _vol_period_group(column)
        raw_field = _vol_raw_field(column)
        column_def = {
            'headerName': _format_vol_period_header(column),
            'field': str(column),
            'type': 'rightAligned',
            'sortable': False,
            'filter': False,
            'resizable': True,
            'width': 78,
            'minWidth': 64,
            'maxWidth': 110,
            'headerTooltip': str(column),
            'cellClass': 'mckinsey-ag-grid-cell mckinsey-ag-grid-number-cell volatility-table-number-cell',
            'headerClass': f'mckinsey-ag-grid-header volatility-table-period-header volatility-table-period-{family}',
            'cellClassRules': {
                'volatility-missing-cell': (
                    f"params.data['{raw_field}'] === null || params.data['{raw_field}'] === undefined "
                    f"|| params.data['{raw_field}'] === '' || isNaN(Number(params.data['{raw_field}']))"
                ),
            },
        }
        if include_diff_colors:
            column_def['cellClass'] += ' volatility-table-change-cell'
            column_def['cellClassRules'].update({
                'volatility-positive-cell': f"Number(params.data['{raw_field}']) >= 0.00005",
                'volatility-negative-cell': f"Number(params.data['{raw_field}']) <= -0.00005",
            })

        if index == 0 or _vol_period_group(period_columns[index - 1])[0] != group_label:
            column_def['headerClass'] += ' volatility-table-period-group-start'
        column_defs.append(column_def)

    return column_defs


def _create_volatility_ag_grid(table_id, dataframe, period_columns, include_diff_colors=False):
    if dataframe.empty or not period_columns:
        return html.Div('No volatility data for the current selection.', className='volatility-table-empty-state')

    display_df = _prepare_vol_table_display_df(dataframe, period_columns, include_diff_colors=include_diff_colors)
    return dag.AgGrid(
        id=table_id,
        rowData=_clean_vol_table_records(display_df),
        columnDefs=_build_vol_table_column_defs(display_df, period_columns, include_diff_colors=include_diff_colors),
        defaultColDef={
            'wrapHeaderText': False,
            'autoHeaderHeight': False,
            'suppressHeaderMenuButton': True,
            'suppressHeaderFilterButton': True,
            'resizable': True,
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
            'alwaysShowHorizontalScroll': True,
            'alignedGrids': ['changes-table' if table_id == 'volatility-table' else 'volatility-table'],
        },
        className=(
            'ag-theme-alpine mckinsey-ag-grid supply-dest-summary-grid volatility-data-grid'
            + (' volatility-change-grid' if include_diff_colors else '')
        ),
        style={'width': '100%', 'height': 'auto'},
        dangerously_allow_code=True,
    )


def _build_vol_table_panel_header(title, chips=None):
    return html.Div(
        [
            html.H4(title, className='volatility-table-panel-title'),
            html.Div([chip for chip in (chips or []) if chip is not None], className='volatility-table-panel-chips'),
        ],
        className='volatility-table-panel-header',
    )


def _format_surface_expiry_label(value):
    if value is None or pd.isna(value):
        return ''
    try:
        return pd.to_datetime(str(value) + '-01', format='%Y-%m-%d').strftime("%b'%y")
    except (TypeError, ValueError):
        return str(value)


def _format_surface_cell(value, signed=False):
    if value is None or pd.isna(value):
        return '—'
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return str(value)
    number = 0.0 if abs(number) < .005 else number
    sign = '+' if signed and number > 0 else ''
    return f'{sign}{number:.2f}'


def _surface_raw_field(column):
    return f'__raw_surface_{column}'


def _estimate_surface_width(samples, min_width=58, max_width=108, character_px=7):
    content_length = max((len(str(sample)) for sample in samples if sample is not None), default=0)
    return _clamp_vol_width(content_length * character_px + 24, min_width, max_width)


def _prepare_surface_table_display_df(dataframe, delta_columns, include_diff_colors=False):
    display_df = pd.DataFrame(index=dataframe.index)
    display_df['expiry'] = dataframe['expiry'].apply(_format_surface_expiry_label)
    display_df['__contract'] = dataframe['expiry'].astype(str).str[:7]
    display_df['dte'] = pd.to_numeric(dataframe['dte'], errors='coerce').round().astype('Int64')
    if 'origin' in dataframe.columns:
        display_df['origin'] = dataframe['origin']

    raw_columns = {'__raw_dte': display_df['dte'].astype('float')}
    for column in delta_columns:
        raw_field = _surface_raw_field(column)
        raw_series = pd.to_numeric(dataframe[column], errors='coerce') if column in dataframe.columns else pd.Series(index=dataframe.index, dtype='float64')
        raw_columns[raw_field] = raw_series
        display_df[column] = raw_series.apply(lambda value: _format_surface_cell(value, signed=include_diff_colors))

    return concat_dataframes([display_df, pd.DataFrame(raw_columns, index=dataframe.index)], axis=1)


def _build_surface_grid_column_defs(display_df, delta_columns, include_diff_colors=False):
    expiry_width = _estimate_surface_width(['Expiry', *display_df['expiry'].dropna().astype(str).tolist()], min_width=72, max_width=96, character_px=8)
    dte_width = _estimate_surface_width(['DTE', *display_df['dte'].dropna().astype(str).tolist()], min_width=56, max_width=72, character_px=8)
    column_defs = [
        {
            'headerName': 'Expiry',
            'field': 'expiry',
            'pinned': 'left',
            'lockPinned': True,
            'suppressMovable': True,
            'sortable': True,
            'filter': False,
            'resizable': True,
            'width': expiry_width,
            'minWidth': expiry_width,
            'maxWidth': max(expiry_width, 104),
            'cellClass': 'mckinsey-ag-grid-cell volatility-surface-expiry-cell',
            'headerClass': 'mckinsey-ag-grid-header volatility-surface-expiry-header',
            'tooltipField': 'expiry',
            'headerTooltip': 'Expiry',
        },
        {
            'headerName': 'DTE',
            'field': 'dte',
            'pinned': 'left',
            'lockPinned': True,
            'sortable': True,
            'filter': False,
            'resizable': True,
            'width': dte_width,
            'minWidth': dte_width,
            'maxWidth': max(dte_width, 78),
            'type': 'rightAligned',
            'cellClass': 'mckinsey-ag-grid-cell mckinsey-ag-grid-number-cell volatility-surface-dte-cell',
            'headerClass': 'mckinsey-ag-grid-header volatility-surface-dte-header',
            'cellClassRules': {
                'volatility-missing-cell': (
                    "params.data['__raw_dte'] === null || params.data['__raw_dte'] === undefined "
                    "|| isNaN(Number(params.data['__raw_dte']))"
                ),
            },
            'headerTooltip': 'Days to expiry',
        },
    ]

    for column in delta_columns:
        raw_field = _surface_raw_field(column)
        column_def = {
            'headerName': str(column),
            'field': str(column),
            'type': 'rightAligned',
            'sortable': False,
            'filter': False,
            'resizable': True,
            'width': 70,
            'minWidth': 64,
            'flex': 1,
            'cellClass': 'mckinsey-ag-grid-cell mckinsey-ag-grid-number-cell volatility-surface-delta-cell',
            'headerClass': 'mckinsey-ag-grid-header volatility-surface-delta-header' + (' volatility-surface-atm-header' if column == 'ATM' else ''),
            'headerTooltip': str(column),
            'cellClassRules': {
                'volatility-missing-cell': (
                    f"params.data['{raw_field}'] === null || params.data['{raw_field}'] === undefined "
                    f"|| params.data['{raw_field}'] === '' || isNaN(Number(params.data['{raw_field}']))"
                ),
            },
        }
        if include_diff_colors:
            column_def['cellClass'] += ' volatility-surface-change-cell'
            column_def['cellClassRules'].update({
                'volatility-positive-cell': f"Number(params.data['{raw_field}']) >= 0.00005",
                'volatility-negative-cell': f"Number(params.data['{raw_field}']) <= -0.00005",
            })
        column_defs.append(column_def)

    if 'origin' in display_df.columns:
        column_defs.append({
            'headerName': 'Origin', 'field': 'origin', 'width': 152, 'minWidth': 140,
            'sortable': True, 'filter': False, 'resizable': True, 'tooltipField': 'origin',
            'headerTooltip': 'Updated months or the original settlement date inherited by this revision',
            'cellClass': 'mckinsey-ag-grid-cell volatility-surface-origin-cell',
            'headerClass': 'mckinsey-ag-grid-header',
        })
    return column_defs


def _column_def_width_sum(column_defs):
    return sum(int(column.get('width') or column.get('minWidth') or 0) for column in column_defs)


def _create_surface_ag_grid(dataframe, delta_columns, table_id, include_diff_colors=False, selected_expiry=None, aligned_table_id=None):
    if dataframe.empty or not delta_columns:
        return html.Div('No surface data for the current selection.', className='volatility-table-empty-state')

    display_df = _prepare_surface_table_display_df(dataframe, delta_columns, include_diff_colors=include_diff_colors)
    column_defs = _build_surface_grid_column_defs(display_df, delta_columns, include_diff_colors=include_diff_colors)
    kind, month = surface_data._parse_surface_expiry_selection(selected_expiry)
    selected_month = month[:7] if kind == surface_data.SURFACE_EXPIRY_MONTH else ''
    return dag.AgGrid(
        id=table_id,
        rowData=_clean_vol_table_records(display_df),
        getRowStyle={'styleConditions': [{'condition': f"params.data.__contract === '{selected_month}'",
                     'style': {'backgroundColor': '#f1f5f9', 'borderTop': '1px solid #cbd5e1',
                               'borderBottom': '1px solid #cbd5e1'}}]},
        columnDefs=column_defs,
        defaultColDef={
            'wrapHeaderText': False,
            'autoHeaderHeight': False,
            'suppressHeaderMenuButton': True,
            'suppressHeaderFilterButton': True,
            'resizable': True,
        },
        dashGridOptions={
            'domLayout': 'autoHeight',
            'rowHeight': 28,
            'headerHeight': 34,
            'pagination': False,
            'suppressPaginationPanel': True,
            'enableCellTextSelection': True,
            'ensureDomOrder': True,
            'animateRows': False,
            'alwaysShowHorizontalScroll': False,
            'alwaysShowVerticalScroll': False,
            **({'alignedGrids': [aligned_table_id]} if aligned_table_id else {}),
        },
        className=(
            'ag-theme-alpine mckinsey-ag-grid supply-dest-summary-grid volatility-surface-grid'
            + (' volatility-surface-change-grid' if include_diff_colors else '')
        ),
        style={'width': '100%', 'height': 'auto'},
        dangerously_allow_code=True,
    )


def _build_surface_table_panel(title, grid, chips=None, className=None):
    classes = ['volatility-table-panel', 'volatility-surface-table-panel']
    if className:
        classes.append(className)
    return html.Div(
        [
            _build_vol_table_panel_header(title, chips=chips),
            grid,
        ],
        className=' '.join(classes),
    )


def _build_vol_surface_filter_bar():
    return html.Div(
        [
            html.Div(
                [
                    html.Div('Products', className='filter-group-header'),
                    dcc.Dropdown(
                        id='table-product-dropdown',
                        options=[],
                        value=[],
                        multi=True,
                        persistence=True,
                        persistence_type='session',
                        placeholder='Select products...',
                        className=(
                            'filter-dropdown volatility-surface-filter-dropdown '
                            'volatility-surface-product-dropdown'
                        ),
                    ),
                ],
                className=(
                    'filter-group volatility-surface-sticky-filter-group '
                    'volatility-surface-products-group'
                ),
            ),
            html.Div(
                [
                    html.Div('COB', className='filter-group-header'),
                    html.Div(
                        [
                            dcc.DatePickerSingle(
                                id='table-date-picker',
                                display_format='YYYY-MM-DD',
                                with_portal=True,
                                persistence=True,
                                persistence_type='session',
                                className='volatility-surface-date-picker',
                            ),
                        ],
                        className='volatility-surface-date-control',
                    ),
                ],
                className=(
                    'filter-group volatility-surface-sticky-filter-group '
                    'volatility-surface-date-group'
                ),
            ),
            surface_selector.layout(),
            html.Div([
                html.Label('Delivery', htmlFor='vol-overview-window', className='filter-group-header'),
                dcc.Dropdown(id='vol-overview-window', options=[
                    {'label':'12 months', 'value':12}, {'label':'24 months', 'value':24},
                    {'label':'All', 'value':'all'}], value=24, clearable=False,
                    persistence=True, persistence_type='session'),
            ], className='filter-group volatility-surface-sticky-filter-group volatility-delivery-group'),
            html.Div(
                [
                    html.Div('Group By', className='filter-group-header'),
                    dcc.RadioItems(
                        id='table-grouping-dropdown',
                        options=[
                            {'label': 'Monthly', 'value': 'monthly'},
                            {'label': 'Quarterly', 'value': 'quarterly'},
                            {'label': 'Season', 'value': 'season'},
                            {'label': 'Calendar', 'value': 'calendar'},
                        ],
                        value='monthly',
                        persistence=True,
                        persistence_type='session',
                        inline=True,
                        className='volatility-surface-grouping-selector',
                        inputStyle={'display': 'none'},
                        labelStyle={'marginRight': '0'},
                    ),
                ],
                className=(
                    'filter-group volatility-surface-sticky-filter-group '
                    'volatility-surface-grouping-group'
                ),
            ),
            html.Div(id='surface-toolbar-source-cards', className='surface-toolbar-source-cards'),
        ],
        className='professional-section-header volatility-surface-sticky-filter-bar',
    )


def _build_volatility_section_header(title, actions=None):
    return html.Div(
        [
            html.Div(
                [html.H3(title, className='section-title-inline volatility-section-title')],
                className='volatility-section-title-row',
            ),
            html.Div(actions or [], className='volatility-section-actions'),
        ],
        className='volatility-section-header',
    )


layout = html.Div([
    dcc.Store(id='vol-surface-snapshot-ref', storage_type='session'),
    dcc.Store(id='vol-surface-publication-state', storage_type='session'),
    dcc.Interval(id='vol-surface-publication-poll', interval=30000),
    # Download component for table export
    dcc.Download(id="download-volatility-table"),
    dcc.Download(id="download-surface-table"),
    dcc.Download(id='download-surface-history'),

    _build_vol_surface_filter_bar(),
    html.Div(id='options-refresh-message', className='volatility-refresh-message'),
    html.Div(id='vol-surface-publication-status', role='status', **{'aria-live': 'polite'}),

    html.Div([
        _build_volatility_section_header('Volatility Overview', actions=[
            overview.controls(),
            html.Div(id='atm-status-line', role='status', className='volatility-status-line volatility-status-neutral'),
        ]),
        html.Div(id='graphs-container', className='volatility-section-body volatility-atm-body'),
        html.Div('Monthly delivery · ATM = 50C; RR = selected-delta call − put; BF = average wings − ATM. '
                 '≈ linear IV interpolation between saved deltas; no extrapolation. Group By applies to the Volatility Data tables.',
                 className='vol-overview-footnote'),
    ], className='volatility-section volatility-atm-section'),

    # Tables section
    html.Div(
        id='tables-container'
    ),

    # Full volatility surface section
    html.Div([
        _build_volatility_section_header(
            'Volatility Surface',
            actions=[
                dcc.Tabs(id='surface-product-tabs', value=None, children=[],
                         className='surface-header-product-tabs',
                         parent_className='surface-header-product-selector'),
                dcc.Link(
                    'Open Vol Trades',
                    id='open-vol-trades-link',
                    href='/vol_trades',
                    className='custom-export-btn volatility-export-button',
                    style={'display': 'none'},
                ),
                html.Button(
                    'Export Surface',
                    id='export-surface-table-btn',
                    className='custom-export-btn volatility-export-button',
                )
            ],
        ),
        html.Div(id='surface-status-line', className='volatility-status-line volatility-status-success'),
        html.Div(
            id='surface-empty-message',
            className='volatility-empty-message'
        ),
        html.Div([
            html.Div([
                html.Label('Contract:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='surface-expiry-dropdown',
                    options=[],
                    value=None,
                    clearable=False,
                    className="inline-dropdown-aggregation"
                ),
                html.Label('Smile history:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='surface-lookback-dropdown',
                    options=[
                        {'label': '10D', 'value': 10},
                        {'label': '30D', 'value': 30},
                        {'label': '60D', 'value': 60},
                        {'label': '90D', 'value': 90}
                    ],
                    value=30,
                    clearable=False,
                    className="inline-dropdown-aggregation"
                ),
                html.Label('Heatmap Mode:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='surface-heatmap-mode-dropdown',
                    options=[
                        {'label': 'Absolute IV', 'value': 'absolute'},
                        {'label': 'Smile vs ATM', 'value': 'vs_atm'},
                        {'label': 'Change vs Previous', 'value': 'vs_previous'}
                    ],
                    value='vs_atm',
                    clearable=False,
                    className="inline-dropdown-aggregation"
                ),
            ], id='surface-expiry-controls', style={'display': 'none', 'align-items': 'center', 'gap': '16px', 'margin': '16px 0', 'flex-wrap': 'wrap'}),
            html.Div([
                _build_vol_chart_card(
                    dcc.Graph(
                        id='surface-smile-graph',
                        config=VOL_GRAPH_CONFIG,
                        className='volatility-chart-graph',
                        style={'height': '388px'}
                    ),
                    'Smile Evolution',
                    className='volatility-surface-chart-card'
                ),
                _build_vol_chart_card(
                    dcc.Graph(
                        id='surface-heatmap-graph',
                        config=VOL_GRAPH_CONFIG,
                        className='volatility-chart-graph',
                        style={'height': '388px'}
                    ),
                    'Surface Heatmap',
                    className='volatility-surface-chart-card'
                ),
            ], className='volatility-surface-chart-grid'),
            dcc.Store(id='surface-history-context'),
            dcc.Store(id='surface-history-contracts-dropdown', data=[]),
            html.Div([
                html.Div([html.Label('Contract', htmlFor='surface-rv-contract-dropdown'),
                    dcc.Dropdown(id='surface-rv-contract-dropdown', options=[], value=None, clearable=False,
                                 className='history-contract-select')], style={'display': 'none'}),
                html.Strong(id='surface-history-selection-label'),
                html.Div([html.Label('Compare', htmlFor='surface-history-compare'),
                    dcc.Dropdown(id='surface-history-compare', options=[], value=None, clearable=True,
                                 placeholder='Optional', className='history-contract-select')], className='history-control'),
                html.Div([html.Label('Range', htmlFor='surface-history-range'),
                    dcc.Dropdown(id='surface-history-range', options=[{'label':f'{d}D','value':d} for d in (10,30,60,90)],
                                 value=90, clearable=False, className='history-small-select')], className='history-control'),
                html.Div([html.Label('Deltas', htmlFor='surface-history-buckets-dropdown'),
                    dcc.Dropdown(id='surface-history-buckets-dropdown', options=[], value=[], multi=True,
                                 placeholder='Select buckets', className='history-buckets-select')], className='history-control'),
                html.Div([html.Label('Mode', htmlFor='surface-history-mode-dropdown'),
                    dcc.Dropdown(id='surface-history-mode-dropdown', options=[
                        {'label':'Fixed expiry','value':'fixed_expiry'},
                        {'label':'Rolling tenor','value':'rolling_tenor'}], value='fixed_expiry', clearable=False,
                        className='history-mode-select')], className='history-control'),
                html.Button('Export history', id='export-surface-history', className='custom-export-btn'),
            ], id='surface-history-contracts-controls', className='volatility-history-toolbar'),
            html.Div('Settlement history · first contract also drives realized vs implied', className='volatility-history-caption'),
            html.Div([html.Div([
            _build_vol_chart_card(
                dcc.Graph(
                    id='surface-history-graph',
                    config=VOL_GRAPH_CONFIG,
                    className='volatility-chart-graph',
                    style={'height': '480px'}
                ),
                'Delta Vol History',
                className='volatility-history-chart-card'
            ),
            html.Div(id='surface-history-coverage'),
            ]),
            html.Div([
                _build_vol_chart_card(
                    dcc.Graph(id='surface-rv-comparison-graph',config=VOL_GRAPH_CONFIG,
                              className='volatility-chart-graph',style={'height':'480px'}),
                    'Realized vs Implied Volatility',className='volatility-rv-chart-card'),
                html.Div(id='surface-rv-comparison-status',className='volatility-rv-comparison-status'),
            ],id='surface-rv-comparison-section',style={'display':'none'}),
            ], className='volatility-linked-history-grid'),
            html.Div(id='surface-table-container', style={'width': '100%'})
        ], id='surface-content-wrapper', style={'display': 'none'})
    ], id='surface-section-container', className='volatility-section volatility-surface-section')
], className='options-dashboard-container volatility-surface-page')


# ===== UPDATED CALLBACKS FOR UNIFIED INTERFACE =====


def _build_message_span(message, tone='neutral'):
    colors = {
        'neutral': '#4B5563',
        'success': '#166534',
        'warning': '#92400E',
        'error': '#991B1B',
    }
    return html.Span(message, style={'color': colors.get(tone, colors['neutral'])})


@callback(
    Output('vol-surface-publication-state', 'data'),
    Output('vol-surface-publication-status', 'children'),
    Output('refresh-options-data', 'n_clicks', allow_duplicate=True),
    Input('vol-surface-publication-poll', 'n_intervals'),
    State('table-date-picker', 'date'),
    State('vol-surface-publication-state', 'data'),
    State('refresh-options-data', 'n_clicks'),
    State('vol-surface-snapshot-ref', 'data'),
    prevent_initial_call=True,
)
def poll_surface_publications(_tick, selected_date, previous, refresh_count, snapshot_reference=None):
    if not selected_date:
        return no_update, no_update, no_update
    from dash_options.core.config import get_database_engine
    from dash_options.calibration.publication_status import load_publication_status, status_notices
    try:
        from flask import has_request_context, request
        from dash_options.calibration.auth import resolve_request_identity, authorize, Permission
        owner = None
        if has_request_context():
            try:
                identity = resolve_request_identity(request.headers, remote_addr=request.remote_addr)
                authorize(identity, Permission.VIEW)
                owner = identity.subject
            except PermissionError:
                pass
        status = load_publication_status(get_database_engine(), selected_date, owner=owner)
    except Exception:
        return no_update, _build_message_span('Publication status is temporarily unavailable.', 'warning'), no_update
    display_status = {**status, 'jobs': [row for row in status.get('jobs', [])
                                       if row.get('stage') != 'verified' or row.get('status') == 'failed']}
    notices = status_notices(display_status)
    if status.get('latest_cob') and status['latest_cob'] > selected_date:
        notices.insert(0, {'message':f"Settlements are available through {status['latest_cob']}; selected date is {selected_date}.",
                           'tone':'warning'})
    changed = (isinstance(previous, dict) and previous.get('cob') == selected_date
               and previous.get('revision') != status['revision'])
    if isinstance(snapshot_reference, dict):
        from dash_options.core.snapshots import resolve_snapshot
        try:
            snapshot = resolve_snapshot(snapshot_reference, expected_namespace=surface_data.VOL_SURFACE_SNAPSHOT_NAMESPACE)
            cached = snapshot['data_cache_state']['surface'].get('publications', {})
            changed = changed or any(row['commodity'] in surface_data.PUBLISHED_SURFACE_PRODUCTS
                          and cached.get(f"{row['commodity']}:{selected_date}", {}).get('publication_id') != row['publication_id']
                          for row in status['publications'])
            cached_latest = max((key.rsplit(':', 1)[-1] for key in cached), default='')
            changed = changed or bool(status.get('latest_cob') and status['latest_cob'] > cached_latest)
        except SnapshotReferenceError:
            changed = True
    state = {'cob': selected_date, 'revision': status['revision']}
    content = [html.Div(_build_message_span(notice['message'], notice['tone']),
                        title=notice.get('publication_id') or '') for notice in notices]
    return state, content, (int(refresh_count or 0) + 1 if changed else no_update)


def _build_surface_status_line(
    selected_date,
    prev_selected_date,
    active_product,
    current_surface=None,
    previous_surface=None,
):
    surface_error = surface_data.DATA_CACHE_STATE['surface']['error']
    surface_source = surface_data.surface_source_for(active_product, selected_date)
    if surface_error and surface_data.surface_dataset.empty:
        return _build_message_span(f'Surface source unavailable: {surface_error}', tone='error')

    if not active_product or selected_date is None:
        return _build_message_span(
            f'Source: {surface_source}. Select Brent, HH, TTF, JKM, or NBP to inspect the full volatility surface.',
            tone='success'
        )

    if current_surface is None:
        current_surface = surface_data._get_surface_snapshot(active_product, selected_date)
    if previous_surface is None:
        previous_surface = surface_data._get_surface_snapshot(active_product, prev_selected_date) if prev_selected_date else surface_data._empty_surface_df()

    if current_surface.empty:
        selected_date_text = pd.to_datetime(selected_date).strftime('%Y-%m-%d')
        return _build_message_span(
            f'Source: {surface_source}. No {active_product} surface is available on {selected_date_text}.',
            tone='warning'
        )

    pivot_df, delta_columns = surface_data._build_surface_pivot(current_surface)
    expiry_count = int(len(pivot_df.index))
    bucket_count = len(delta_columns)
    filled_cells = int(pivot_df.notna().sum().sum()) if not pivot_df.empty else 0
    missing_cells = max((expiry_count * bucket_count) - filled_cells, 0)
    previous_text = pd.to_datetime(prev_selected_date).strftime('%Y-%m-%d') if prev_selected_date else 'n/a'
    comparison_status = 'available' if not previous_surface.empty else 'missing'

    message = (
        f'Source: {surface_source} | Product: {active_product} | Current COB: {pd.to_datetime(selected_date).strftime("%Y-%m-%d")} | '
        f'Previous COB: {previous_text} ({comparison_status}) | Expiries: {expiry_count} | Buckets: {bucket_count} | Missing cells: {missing_cells}'
    )
    if active_product == 'Brent':
        expiry_metadata = current_surface[['contract_date', 'option_expiration_date']].copy()
        expiry_metadata['option_expiration_date'] = pd.to_datetime(
            expiry_metadata['option_expiration_date'], errors='coerce'
        )
        verified_expiry_count = int(
            expiry_metadata.dropna(subset=['option_expiration_date'])['contract_date'].nunique()
        )
        conflicting_expiry_count = int(
            (
                expiry_metadata.dropna(subset=['option_expiration_date'])
                .groupby('contract_date')['option_expiration_date']
                .nunique()
                > 1
            ).sum()
        )
        missing_expiry_count = max(expiry_count - verified_expiry_count, 0)
        message = (
            f'{message} | Verified ICE option expiries: {verified_expiry_count}/{expiry_count}'
        )
        if missing_expiry_count or conflicting_expiry_count:
            quality_message = (
                f'{message} | Expiry metadata issue: missing={missing_expiry_count}, '
                f'conflicting={conflicting_expiry_count}. DTE is intentionally blank where unverified.'
            )
            return _build_message_span(quality_message, tone='warning')
    if surface_error:
        return _build_message_span(f'{message} | Load warning: {surface_error}', tone='warning')
    return _build_message_span(message, tone='success')


def refresh_data_feedback(n_clicks):
    refresh_token = n_clicks or 0
    surface_data._ensure_cached_data(refresh_token)

    if n_clicks is None:
        return ''

    errors = [status['error'] for status in surface_data.DATA_CACHE_STATE.values() if isinstance(status, dict) and status.get('error')]
    if errors:
        return _build_message_span(f'Data refresh completed with warnings: {" | ".join(errors)}', tone='warning')

    surface_source = surface_data.DATA_CACHE_STATE['surface']['source']
    return _build_message_span(
        f'Data refreshed from {surface_source}.',
        tone='success'
    )


@callback(
    Output('vol-surface-snapshot-ref', 'data'),
    Output('options-refresh-message', 'children'),
    Output('table-date-picker', 'date'),
    Output('table-prev-date-picker', 'date'),
    Output('table-product-dropdown', 'options'),
    Output('table-product-dropdown', 'value'),
    Output('table-grouping-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    State('vol-surface-snapshot-ref', 'data'),
    State('table-date-picker', 'date'),
    State('table-prev-date-picker', 'date'),
    State('table-product-dropdown', 'value'),
    State('table-grouping-dropdown', 'value'),
)
def initialize_vol_surface_page(
    n_clicks,
    current_reference,
    current_date,
    previous_date,
    selected_products,
    grouping_mode,
):
    refresh_count = int(n_clicks or 0)
    previous_refresh_count = (
        current_reference.get('refresh_count')
        if isinstance(current_reference, dict)
        else None
    )
    force = refresh_count > 0 and previous_refresh_count != refresh_count

    reference = None
    if not force and isinstance(current_reference, dict):
        try:
            reference = surface_data._activate_surface_snapshot(current_reference)
        except SnapshotReferenceError:
            reference = None
    if reference is None:
        reference = surface_data.prepare_vol_surface_snapshot(
            force=force,
            refresh_token=refresh_count,
        )

    browser_reference = dict(reference)
    browser_reference['refresh_count'] = refresh_count
    all_dates = surface_data._get_all_available_dates()
    date_values = [pd.Timestamp(value).strftime('%Y-%m-%d') for value in all_dates]
    resolved_date = current_date if current_date in date_values else (
        date_values[-1] if date_values else None
    )
    resolved_previous = previous_date if previous_date in date_values else None
    if resolved_previous is None and date_values:
        earlier_dates = [value for value in date_values if value < resolved_date]
        resolved_previous = earlier_dates[-1] if earlier_dates else resolved_date

    product_codes = sorted(surface_data.atm_dataset['code'].unique()) if not surface_data.atm_dataset.empty else []
    product_options = [{'label': code, 'value': code} for code in product_codes]
    had_reference = isinstance(current_reference, dict)
    if had_reference and selected_products is not None:
        resolved_products = [
            product for product in selected_products if product in product_codes
        ]
    else:
        resolved_products = product_codes

    resolved_grouping = (
        grouping_mode
        if grouping_mode in {'monthly', 'quarterly', 'season', 'calendar'}
        else 'monthly'
    )
    message = '' if n_clicks is None else refresh_data_feedback(n_clicks)
    return (
        browser_reference,
        message,
        resolved_date,
        resolved_previous,
        product_options,
        resolved_products,
        resolved_grouping,
    )


def update_tables(n_clicks, selected_date, prev_selected_date, selected_products, grouping_mode, frames=None, source='settlement', delivery_context=None):
    if selected_date is None or not selected_products:
        return html.Div()

    current_pivot, changes_pivot, sorted_date_cols = surface_data._build_atm_table_frames(selected_date, prev_selected_date, selected_products, grouping_mode, frames=frames)

    if delivery_context is not None and grouping_mode == 'monthly':
        sorted_date_cols = [pd.Timestamp(m).strftime('%m-%y') for m in delivery_context.get('months', [])]
    current_pivot = current_pivot.set_index('product').reindex(selected_products).reindex(columns=sorted_date_cols).reset_index()

    current_table = html.Div([
        _build_vol_table_panel_header(
            'ATM volatility (%)',
        ),
        _create_volatility_ag_grid(
            table_id='volatility-table',
            dataframe=current_pivot,
            period_columns=sorted_date_cols,
        )
    ], className='volatility-table-panel volatility-current-table-panel')

    changes_pivot = changes_pivot.set_index('product').reindex(current_pivot['product']).reindex(columns=sorted_date_cols).reset_index()
    changes_table = html.Div()
    if not current_pivot.empty:
        changes_table = html.Div([
            _build_vol_table_panel_header(
                'Change vs comparison (vol pts)',
            ),
            _create_volatility_ag_grid(
                table_id='changes-table',
                dataframe=changes_pivot,
                period_columns=sorted_date_cols,
                include_diff_colors=True,
            )
        ], className='volatility-table-panel volatility-change-table-panel')

    table_header = _build_volatility_section_header(
        'Volatility Data',
        actions=[
            html.Button(
                'Export',
                id='export-volatility-table-btn',
                className='custom-export-btn volatility-export-button volatility-export-button-primary',
            )
        ],
    )

    return html.Div(
        [
            table_header,
            html.Div([current_table, changes_table], className='volatility-section-body volatility-table-body'),
        ],
        className='volatility-section volatility-data-section',
    )


@callback(
    Output('tables-container', 'children'),
    Input('vol-surface-snapshot-ref', 'data'),
    Input('table-date-picker', 'date'),
    Input('table-prev-date-picker', 'date'),
    Input('table-product-dropdown', 'value'),
    Input('table-grouping-dropdown', 'value'),
    Input('surface-selection-ref', 'data'), Input('surface-source-dropdown', 'value'),
    Input('surface-revision-dropdown', 'value'), Input('surface-comparison-dropdown', 'value'),
    Input('vol-overview-context', 'data'),
    prevent_initial_call=True,
)
def render_volatility_tables(
    snapshot_reference,
    selected_date,
    prev_selected_date,
    selected_products,
    grouping_mode, selection=None, source='settlement', revision=None, comparison=None, delivery_context=None):
    surface_data._ensure_cached_data(snapshot_reference)
    frames = overview.atm_frames(snapshot_reference, selected_date, prev_selected_date, selected_products,
                                 source, selection, revision, comparison)
    frames = overview.complete_bucket_frames(frames, delivery_context, grouping_mode)
    return update_tables(None, selected_date, prev_selected_date, selected_products, grouping_mode, frames, source, delivery_context)



@callback(
    [Output('surface-product-tabs', 'children'),
     Output('surface-product-tabs', 'value'),
    Output('surface-content-wrapper', 'style'),
     Output('surface-empty-message', 'children')],
    [Input('vol-surface-snapshot-ref', 'data'),
     Input('table-product-dropdown', 'value'),
     Input('table-date-picker', 'date'),
     Input('vol-overview-navigation', 'data')],
    State('surface-product-tabs', 'value'),
    prevent_initial_call=True
)
def update_surface_tabs(snapshot_reference, selected_products, selected_date, navigation, active_tab):
    surface_data._ensure_cached_data(snapshot_reference)
    supported_products = surface_data._get_supported_surface_products(selected_products, selected_date)

    if not supported_products:
        return [], None, {'display': 'none'}, 'Full surface data is available only for Brent, HH, TTF, JKM, and NBP.'

    tabs = [dcc.Tab(label=product, value=product, className='surface-header-product-tab',
                    selected_className='surface-header-product-tab-selected') for product in supported_products]
    current_date_products = supported_products
    if selected_date is not None and not surface_data.surface_dataset.empty:
        selected_date_dt = pd.to_datetime(selected_date).normalize()
        available_on_date = set(
            surface_data.surface_dataset.loc[
                surface_data.surface_dataset['cob_date'].dt.normalize() == selected_date_dt,
                'code',
            ].dropna().unique()
        )
        current_date_products = [product for product in supported_products if product in available_on_date]

    navigation_triggered = (ctx.triggered_id == 'vol-overview-navigation'
                            or 'vol-overview-navigation.data' in getattr(ctx, 'triggered_prop_ids', {}))
    if (navigation_triggered and isinstance(navigation, dict)
            and navigation.get('product') in current_date_products):
        active_tab = navigation['product']
    if active_tab in current_date_products:
        active_value = active_tab
    else:
        active_value = current_date_products[0] if current_date_products else supported_products[0]
    return tabs, active_value, {'display': 'block'}, ''


@callback(
    [Output('surface-expiry-dropdown', 'options'),
     Output('surface-expiry-dropdown', 'value'),
     Output('surface-expiry-controls', 'style')],
    [Input('vol-surface-snapshot-ref', 'data'),
     Input('table-date-picker', 'date'),
     Input('surface-product-tabs', 'value'),
     Input('surface-selection-ref', 'data'),
     Input('surface-source-dropdown', 'value'),
     Input('surface-revision-dropdown', 'value'),
     Input('surface-comparison-dropdown', 'value'),
     Input('vol-overview-navigation', 'data')],
    Input('vol-overview-context', 'data'),
    State('surface-expiry-dropdown', 'value'),
    prevent_initial_call=True
)
def update_surface_expiry_dropdown(snapshot_reference, selected_date, active_product, selection,
                                   source, revision, comparison, navigation, delivery_context, current_expiry):
    surface_data._ensure_cached_data(snapshot_reference)
    hidden_style = {'display': 'none', 'align-items': 'center', 'gap': '16px', 'margin': '16px 0', 'flex-wrap': 'wrap'}
    visible_style = {'display': 'flex', 'align-items': 'center', 'gap': '16px', 'margin': '16px 0', 'flex-wrap': 'wrap'}

    if selected_date is None or not active_product:
        return [], None, hidden_style

    try:
        current_surface, _, _ = _selected_surface_pair(
            active_product, selected_date, None, source, selection, revision, comparison,
        )
    except surface_selector.SelectionError:
        return [], None, hidden_style
    if current_surface.empty:
        return [], None, hidden_style

    current_surface = overview.filter_delivery(current_surface, delivery_context)
    expiry_options = [o for o in _build_surface_expiry_options(current_surface) if o['value'].startswith('month:')]
    if not expiry_options:
        return [], None, hidden_style

    valid_expiries = {option['value'] for option in expiry_options}
    requested_expiry = current_expiry
    navigation_triggered = (ctx.triggered_id in {'vol-overview-navigation', 'surface-product-tabs'}
                            or bool({'vol-overview-navigation.data', 'surface-product-tabs.value'}
                                    & set(getattr(ctx, 'triggered_prop_ids', {}))))
    if (navigation_triggered
            and isinstance(navigation, dict) and navigation.get('product') == active_product):
        requested_expiry = navigation.get('month')
    normalized_current_expiry = surface_data._normalize_surface_expiry_selection(requested_expiry)
    selected_expiry = (
        normalized_current_expiry
        if normalized_current_expiry in valid_expiries
        else (None if requested_expiry or navigation_triggered and isinstance(navigation, dict) and navigation.get('product') == active_product else expiry_options[0]['value'])
    )
    return expiry_options, selected_expiry, visible_style


def vol_trades_link_style(active_product):
    """Show the market workspace for products supported by Vol Trades."""
    product = str(active_product or '').upper()
    return {'display': 'inline-flex' if product in VOL_TRADES_PRODUCTS else 'none'}


@callback(
    Output('open-vol-trades-link', 'style'),
    Input('surface-product-tabs', 'value'),
)
def update_vol_trades_link(active_product):
    return vol_trades_link_style(active_product)


@callback(Output('surface-history-buckets-dropdown','options'), Output('surface-history-buckets-dropdown','value'),
          Input('vol-surface-snapshot-ref','data'), Input('surface-product-tabs','value'),
          Input('surface-rv-contract-dropdown','value'), State('surface-history-buckets-dropdown','value'))
def update_history_buckets(reference, product, contract, current):
    if not product or not contract:
        return [], []
    frame = overview.snapshot_frame(reference)
    selected = frame.loc[frame.code.eq(product) & frame.contract_date.eq(pd.Timestamp(contract))]
    options = _get_delta_bucket_options(selected)
    valid = {o['value'] for o in options}
    values = [v for v in (current or []) if v in valid][:3]
    return options, values or (['ATM'] if 'ATM' in valid else sorted(valid)[:1])



def update_comparison_contracts(snapshot_reference,selected_date,product,current):
    if product not in realized_comparison_data.CALENDARS or not selected_date:
        return {'display':'none'},[],current
    surface_data._ensure_cached_data(snapshot_reference)
    current_surface=surface_data._get_surface_snapshot(product,selected_date)
    months=sorted(set(current_surface.contract_date.dropna().dt.strftime('%Y-%m-%d')))
    options=[{'label':pd.Timestamp(m).strftime('%b-%y'),'value':m} for m in months]
    return {'display':'block'},options,current if current in months else (months[0] if months else None)


@callback(Output('surface-rv-comparison-graph','figure'),Output('surface-rv-comparison-status','children'),
          Input('vol-surface-snapshot-ref','data'),Input('table-date-picker','date'),Input('surface-product-tabs','value'),
          Input('surface-rv-contract-dropdown','value'),Input('surface-history-range','value'),
          Input('surface-history-mode-dropdown','value'),
          Input('surface-selection-ref','data'), Input('surface-source-dropdown','value'),
          Input('surface-revision-dropdown','value'), Input('surface-comparison-dropdown','value'), Input('vol-overview-context','data'),prevent_initial_call=True)
def update_realized_comparison(snapshot_reference,selected_date,product,contract,lookback,history_mode="fixed_expiry",
                               selection=None,source='settlement',revision=None,comparison=None,delivery_context=None):
    if delivery_context is not None and contract not in delivery_context.get('months', []):
        contract = None
    if product not in realized_comparison_data.CALENDARS or not selected_date or not contract:
        return _empty_figure('Select a product and maturity.','Realized vs Implied Volatility'),''
    surface_data._ensure_cached_data(snapshot_reference)
    frame = overview.snapshot_frame(snapshot_reference) if isinstance(snapshot_reference, dict) else surface_data.surface_dataset
    fig,status=_create_realized_comparison_figure(product,contract,lookback,selected_date,frame, snapshot_reference, history_mode)
    if fig.layout.meta:
        fig.update_layout(meta={**fig.layout.meta,'iv_snapshot_reference':snapshot_reference})
    if source not in (None, 'settlement') and history_mode == 'fixed_expiry' and fig.data:
        try:
            current, _, metadata = _selected_surface_pair(product,selected_date,None,source,selection,revision,comparison)
            _add_selected_history_points(fig,current,metadata,[contract],['ATM'],rv=True)
        except surface_selector.SelectionError as exc:
            return fig, str(exc)

    return fig, (status if history_mode == 'rolling_tenor' or not fig.data else
                 'RV: 30 calendar days · IV − trailing RV compares different periods')


def show_history_contracts(product,mode):
    return {'display':'flex' if product in delta_history_data.CALENDARS and mode == 'fixed_expiry' else 'none',
            'alignItems':'center','gap':'12px','flexWrap':'wrap','marginBottom':'12px'}


def update_history_contracts(snapshot_reference,end,product,lookback,current):
    if product not in delta_history_data.CALENDARS or not end:
        return [],[]
    surface_data._ensure_cached_data(snapshot_reference)
    months = delta_history_data.available_contracts(product,end,lookback,surface_data.surface_dataset)
    options = [{'label':pd.Timestamp(m).strftime('%b-%y'),'value':m} for m in months]
    if current:
        valid = [m for m in current if m in months]
        if valid:
            return options,valid
    current_months = surface_data._get_surface_snapshot(product,end).contract_date.dropna().dt.strftime('%Y-%m-%d')
    defaults = [m for m in months if m in set(current_months)]
    return options,(defaults or months)[:2]





@callback(Output('surface-rv-comparison-section','style'),
          Output('surface-rv-contract-dropdown','options'), Output('surface-rv-contract-dropdown','value'),
          Output('surface-history-compare','options'), Output('surface-history-compare','value'),
          Output('surface-history-context','data'),
          Input('vol-surface-snapshot-ref','data'), Input('table-date-picker','date'),
          Input('surface-product-tabs','value'), Input('surface-history-range','value'),
          Input('surface-expiry-dropdown', 'value'),
          Input('vol-overview-context', 'data'),
          State('surface-rv-contract-dropdown','value'), State('surface-history-compare','value'),
          State('surface-history-context','data'),
)
def select_linked_history_contracts(reference, end, product, lookback, selected_expiry, delivery_context, primary, comparison, previous):
    return select_history_contracts(reference, end, product, lookback, primary, comparison, previous, selected_expiry, delivery_context)


def select_history_contracts(reference, end, product, lookback, primary, comparison, previous, selected_expiry='legacy', delivery_context=None):
    if not reference or not end or product not in delta_history_data.CALENDARS:
        return {'display':'none'}, [], None, [], None, {'product':product}
    frame = overview.snapshot_frame(reference)
    months = delta_history_data.available_contracts(product, end, lookback, frame)
    current = overview.settlement_slice(frame, product, end)
    front = overview.front_month(current)
    default = front.strftime('%Y-%m-%d') if front is not None else (months[0] if months else None)
    if not isinstance(previous, dict) or previous.get('product') != product:
        primary, comparison = default, None
    elif primary not in months:
        primary = default
    if selected_expiry != 'legacy':
        kind, month = surface_data._parse_surface_expiry_selection(selected_expiry)
        primary = month if kind == surface_data.SURFACE_EXPIRY_MONTH else None
    if primary and primary not in months:
        months = sorted([*months, primary])
    if comparison not in months or comparison == primary:
        comparison = None
    if delivery_context is not None:
        allowed = set(delivery_context.get('months', []))
        months = [m for m in months if m in allowed]
        primary = primary if primary in allowed else None
        comparison = comparison if comparison in allowed else None
    options = [{'label':pd.Timestamp(m).strftime('%b-%y'), 'value':m} for m in months]
    return {'display':'block'}, options, primary, options, comparison, {'product':product}


@callback(Output('surface-history-contracts-dropdown','data'),
          Input('surface-rv-contract-dropdown','value'), Input('surface-history-compare','value'))
def linked_history_contracts(primary, comparison):
    return list(dict.fromkeys(m for m in (primary, comparison) if m))


@callback(Output('surface-history-compare', 'disabled'),
          Input('surface-history-mode-dropdown', 'value'))
def disable_rolling_contract_comparison(mode):
    return mode == 'rolling_tenor'


@callback(Output('surface-history-coverage','children'), Input('surface-history-graph','figure'))
def history_coverage(figure):
    meta = (figure or {}).get('layout', {}).get('meta') or {}
    items = []
    for month, source in meta.get('prices', {}).items():
        coverage = source.get('coverage', {})
        if not coverage:
            continue
        label = pd.Timestamp(month).strftime('%b-%y')
        summary = f"{label} · IV {coverage['iv_observed']}/{coverage['iv_expected']} · RV {coverage['rv_observed']}/{coverage['sessions']} · Prices through {source['last_price_date']}"
        missing = coverage.get('missing_iv_dates', [])
        prices = coverage.get('missing_price_dates', [])
        items.append(html.Details([html.Summary(summary),
            html.Div(f"Price source: {source['price_source']} · Calendar: {source['calendar']} · RV: 30 calendar days, sample standard deviation × √252"),
            html.Div('Missing IV: ' + (', '.join(missing) or 'none')),
            html.Div('Missing/invalid settlements: ' + (', '.join(prices) or 'none')),
            html.Div('Incomplete RV windows: ' + (', '.join(coverage.get('incomplete_rv_dates', [])) or 'none')),
            html.Div('Archive is a separate reconstruction, never substituted for settlement IV.'),
        ], className='volatility-history-quality'))
    return items


@callback(Output('download-surface-history','data'), Input('export-surface-history','n_clicks'),
          State('vol-surface-snapshot-ref','data'), State('table-date-picker','date'),
          State('surface-product-tabs','value'), State('surface-history-contracts-dropdown','data'),
          State('surface-history-range','value'), State('surface-history-mode-dropdown','value'),
          State('surface-selection-ref','data'), State('surface-source-dropdown','value'),
          State('surface-revision-dropdown','value'), State('surface-comparison-dropdown','value'),
          State('vol-overview-context','data'),
          prevent_initial_call=True)
def export_surface_history(clicks, reference, end, product, contracts, lookback, mode,
                           selection=None, source='settlement', revision=None, comparison=None, delivery_context=None):
    if delivery_context is not None:
        contracts = [m for m in (contracts or []) if m in delivery_context.get('months', [])]
    if not clicks or not contracts:
        return no_update
    frame = overview.snapshot_frame(reference)
    primary = contracts[0]
    if mode == 'rolling_tenor':
        schedule, _ = delta_history_data.rolling_schedule(frame, product, primary, end, lookback)
        exported_contracts = schedule.contract_date.dropna().dt.strftime('%Y-%m-%d').unique().tolist()
        rv, rv_meta = delta_history_data.load_rolling_comparison(product, primary, end, lookback, frame, reference)
    else:
        exported_contracts = contracts
        rv, rv_meta = realized_comparison_data.load_comparison(product, primary, end, lookback, frame, reference)
    settlement, archive, prices, meta = delta_history_data.load_history(product, exported_contracts, end, lookback, frame, reference)
    if mode == 'rolling_tenor':
        settlement = settlement.merge(schedule, on=['cob_date','contract_date'], validate='many_to_one')
        archive = archive.merge(schedule, on=['cob_date','contract_date'], validate='many_to_one')
        prices = prices.merge(schedule, on=['cob_date','contract_date'], validate='one_to_one')
    buffer = io.BytesIO()
    selected = None
    if source not in (None, 'settlement') and mode == 'fixed_expiry':
        selected, _, selected_meta = _selected_surface_pair(product,end,None,source,selection,revision,comparison)
        selected = selected.loc[selected.contract_date.isin(pd.to_datetime(contracts))]
        meta['selected_surface'] = selected_meta
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        if selected is not None:
            selected.to_excel(writer, sheet_name='Selected revision', index=False)
        settlement.to_excel(writer, sheet_name='Settlement IV', index=False)
        archive.to_excel(writer, sheet_name='Reconstruction', index=False)
        prices.to_excel(writer, sheet_name='Settlements', index=False)
        rv.to_excel(writer, sheet_name='Realized vs implied', index=False)
        pd.DataFrame([{'field':k, 'value':json.dumps(v, default=str)} for k,v in
                      {**meta, 'rv_metadata':rv_meta, 'history_mode':mode}.items() if k!='sessions']).to_excel(
                          writer, sheet_name='Provenance', index=False)
    return dcc.send_bytes(buffer.getvalue(), f'{product.lower()}_volatility_history_{end}.xlsx')


@callback(
    [Output('surface-heatmap-graph', 'figure'),
     Output('surface-smile-graph', 'figure'),
     Output('surface-history-graph', 'figure'),
     Output('surface-table-container', 'children'),
     Output('surface-status-line', 'children')],
    [Input('vol-surface-snapshot-ref', 'data'),
     Input('table-date-picker', 'date'),
     Input('table-prev-date-picker', 'date'),
     Input('surface-product-tabs', 'value'),
     Input('surface-expiry-dropdown', 'value'),
     Input('surface-history-buckets-dropdown', 'value'),
     Input('surface-lookback-dropdown', 'value'),
     Input('surface-heatmap-mode-dropdown', 'value'),
     Input('surface-history-mode-dropdown', 'value'),
     Input('surface-history-contracts-dropdown','data'),
     Input('surface-selection-ref', 'data'),
     Input('surface-source-dropdown', 'value'),
     Input('surface-revision-dropdown', 'value'),
     Input('surface-comparison-dropdown', 'value'),
     Input('surface-history-range', 'value')],
    Input('vol-overview-context', 'data'),
    prevent_initial_call=True
)
def update_surface_section(
    snapshot_reference,
    selected_date,
    prev_selected_date,
    active_product,
    selected_expiry,
    selected_history_buckets,
    lookback_days,
    heatmap_mode,
    history_mode,
    history_contracts=None,
    selection=None,
    source='settlement',
    revision=None,
    comparison=None,
    history_range=None, delivery_context=None):
    surface_data._ensure_cached_data(snapshot_reference)
    if delivery_context is not None:
        allowed = set(delivery_context.get('months', []))
        kind, month = surface_data._parse_surface_expiry_selection(selected_expiry)
        if month not in allowed:
            selected_expiry = None
        history_contracts = [m for m in (history_contracts or []) if m in allowed]
    history_range = history_range or lookback_days
    history_expiry = 'month:' + history_contracts[0] if history_contracts else selected_expiry

    if selected_date is None or not active_product:
        status_line = ''
        empty_heatmap = _empty_figure('Select a supported product to view the surface.', 'Surface Heatmap')
        empty_smile = _empty_figure('Select a supported product to view smile details.', 'Smile Evolution')
        empty_history = _empty_figure('Select a supported product, expiry, and delta bucket to view history.', 'Delta Vol History')
        return empty_heatmap, empty_smile, empty_history, html.Div(), status_line

    try:
        current_surface, previous_surface, selected_metadata = _selected_surface_pair(
            active_product, selected_date, prev_selected_date, source, selection, revision, comparison,
        )
    except surface_selector.SelectionError as exc:
        message = str(exc)
        return (_empty_figure(message, 'Surface Heatmap'), _empty_figure(message, 'Smile Evolution'),
                _create_delta_history_figure(active_product, history_expiry, selected_history_buckets,
                    history_range, selected_date, history_mode or 'fixed_expiry',
                    history_contracts=history_contracts, snapshot_reference=snapshot_reference),
                html.Div(message, className='volatility-table-empty-state'),
                _build_message_span(message, tone='warning'))
    current_surface = overview.filter_delivery(current_surface, delivery_context)
    previous_surface = overview.filter_delivery(previous_surface, delivery_context)
    # Routine source identity is already controlled in the page toolbar and exports.
    status_line = ''

    if current_surface.empty:
        no_data_message = html.Div(
            f'No surface data available for {active_product} on {pd.to_datetime(selected_date).strftime("%Y-%m-%d")}.',
            style={'padding': '12px 0', 'color': '#6b7280'}
        )
        return (
            _create_surface_heatmap_figure(active_product, current_surface, previous_surface, heatmap_mode or 'vs_atm'),
            _create_smile_evolution_figure(active_product, None, current_surface, previous_surface, lookback_days, selected_date),
            _create_delta_history_figure(active_product, None, None, history_range, selected_date, history_mode or 'fixed_expiry'),
            no_data_message,
            status_line
        )

    # The shared contract selector owns selection; never substitute another maturity.

    selected_history_buckets = (selected_history_buckets or ['ATM'])[:3]

    heatmap_figure = _create_surface_heatmap_figure(active_product, current_surface, previous_surface, heatmap_mode or 'vs_atm', selected_expiry)
    smile_figure = _create_smile_evolution_figure(
        active_product, selected_expiry, current_surface, previous_surface, lookback_days,
        (selected_metadata or {}).get('business_date', selected_date),
        current_label=(selected_metadata or {}).get('current_label'),
        comparison_label=(selected_metadata or {}).get('comparison_label'),
    )
    history_figure = _create_delta_history_figure(active_product, history_expiry, selected_history_buckets, history_range, selected_date, history_mode or 'fixed_expiry',history_contracts=history_contracts,snapshot_reference=snapshot_reference)
    if selected_metadata and history_mode in (None,'fixed_expiry') and history_figure.data:
        _add_selected_history_points(history_figure,current_surface,selected_metadata,history_contracts,selected_history_buckets)

    current_table_df, diff_table_df, delta_columns = _build_surface_tables(
        current_surface, previous_surface, (selected_metadata or {}).get('business_date', selected_date),
    )
    _add_surface_origins(current_table_df, diff_table_df, selected_metadata)
    if selected_metadata:
        for figure in (heatmap_figure, smile_figure):
            figure.update_layout(meta={'surface_selection': {
                'source': source, 'revision': revision, 'comparison': comparison,
                'anchor': selected_metadata['base_publication_id'],
            }})

    current_table = _build_surface_table_panel(
        'Surface matrix (%)',
        _create_surface_ag_grid(current_table_df, delta_columns, 'surface-current-table',
                                selected_expiry=selected_expiry,
                                aligned_table_id='surface-diff-table' if not diff_table_df.empty else None),
        className='volatility-surface-current-panel',
    )

    if not diff_table_df.empty:
        diff_table = _build_surface_table_panel(
            'Change vs comparison (vol pts)',
            _create_surface_ag_grid(diff_table_df, delta_columns, 'surface-diff-table',
                                    include_diff_colors=True, selected_expiry=selected_expiry,
                                    aligned_table_id='surface-current-table'),
            className='volatility-surface-diff-panel',
        )
    else:
        diff_table = html.Div(
            'No previous-date surface data available for comparison.',
            className='volatility-table-empty-state volatility-surface-empty-state',
        )

    return (
        heatmap_figure,
        smile_figure,
        history_figure,
        html.Div([current_table, diff_table], className='volatility-surface-table-stack'),
        status_line,
    )




@callback(
    Output("download-volatility-table", "data"),
    Input("export-volatility-table-btn", "n_clicks"),
    [State('table-date-picker', 'date'),
     State('table-product-dropdown', 'value'),
     State('table-grouping-dropdown', 'value'),
     State('vol-surface-snapshot-ref', 'data'),
     State('surface-selection-ref','data'), State('surface-source-dropdown','value'),
     State('surface-revision-dropdown','value'), State('surface-comparison-dropdown','value')],
    State('vol-overview-context', 'data'),
    State('table-prev-date-picker', 'date'),
    prevent_initial_call=True
)
def export_volatility_table(
    n_clicks,
    selected_date,
    selected_products,
    grouping_mode,
    snapshot_reference=None, selection=None, source='settlement', revision=None, comparison=None, delivery_context=None, previous_date=None):
    if not n_clicks or not selected_date or not selected_products:
        return None

    try:
        surface_data._ensure_cached_data(snapshot_reference)

        frames = overview.atm_frames(snapshot_reference, selected_date, previous_date, selected_products, source, selection, revision, comparison)
        frames = overview.complete_bucket_frames(frames, delivery_context, grouping_mode)
        current_pivot, changes_pivot, sorted_date_cols = surface_data._build_atm_table_frames(selected_date, previous_date, selected_products, grouping_mode, frames=frames)
        if delivery_context is not None and grouping_mode == 'monthly':
            sorted_date_cols = [pd.Timestamp(m).strftime('%m-%y') for m in delivery_context.get('months', [])]
        current_pivot = current_pivot.set_index('product').reindex(selected_products).reindex(columns=sorted_date_cols).reset_index()
        if current_pivot.empty:
            return None

        export_df = current_pivot[['product'] + sorted_date_cols]
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            export_df.to_excel(writer, sheet_name='Volatility Data', index=False)
            changes_pivot = changes_pivot.set_index('product').reindex(current_pivot['product']).reindex(columns=sorted_date_cols).reset_index()
            changes_pivot.to_excel(writer, sheet_name='Change vs comparison', index=False)
            pd.DataFrame({'delivery_month': (delivery_context or {}).get('months', [])}).to_excel(writer, sheet_name='Delivery scope', index=False)
            pairs = overview.surface_pairs(snapshot_reference, selected_date, previous_date, selected_products,
                                           None, source, selection, revision, comparison)
            pd.DataFrame([{'product':p, 'source':pair['source'], 'revision':pair['current_label'],
                           'comparison':pair['comparison_label'], 'revision_id':((selection or {}).get('products',{}).get(p.upper()) or {}).get('revision'), 'comparison_id':comparison if str(comparison).startswith('settlement:') else ((selection or {}).get('products',{}).get(p.upper()) or {}).get('comparison'), 'status':pair['error'] or 'available'}
                          for p,pair in pairs.items()]).to_excel(writer, sheet_name='Provenance', index=False)
        output.seek(0)

        selected_date_str = pd.to_datetime(selected_date).strftime('%Y%m%d')
        return dcc.send_bytes(output.getvalue(), f'volatility_data_{grouping_mode}_{selected_date_str}.xlsx')
    except Exception:
        return None


@callback(
    Output("download-surface-table", "data"),
    Input("export-surface-table-btn", "n_clicks"),
    [State('table-date-picker', 'date'),
     State('table-prev-date-picker', 'date'),
     State('surface-product-tabs', 'value'),
     State('surface-expiry-dropdown', 'value'),
     State('vol-surface-snapshot-ref', 'data'),
     State('surface-selection-ref', 'data'),
     State('surface-source-dropdown', 'value'),
     State('surface-revision-dropdown', 'value'),
     State('surface-comparison-dropdown', 'value')],
    State('vol-overview-context', 'data'),
    prevent_initial_call=True
)
def export_surface_table(
    n_clicks,
    selected_date,
    prev_selected_date,
    active_product,
    selected_expiry,
    snapshot_reference=None,
    selection=None,
    source='settlement',
    revision=None,
    comparison=None, delivery_context=None):
    if not n_clicks or not selected_date or not active_product:
        return None

    try:
        surface_data._ensure_cached_data(snapshot_reference)

        current_surface, previous_surface, selected_metadata = _selected_surface_pair(
            active_product, selected_date, prev_selected_date, source, selection, revision, comparison,
        )
        current_surface = overview.filter_delivery(current_surface, delivery_context)
        previous_surface = overview.filter_delivery(previous_surface, delivery_context)
        if current_surface.empty:
            return None

        current_table_df, diff_table_df, _ = _build_surface_tables(
            current_surface, previous_surface, (selected_metadata or {}).get('business_date', selected_date),
        )
        _add_surface_origins(current_table_df, diff_table_df, selected_metadata)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            current_table_df.to_excel(writer, sheet_name=f'{active_product} Surface', index=False)
            if not diff_table_df.empty:
                diff_table_df.to_excel(writer, sheet_name=f'{active_product} Change', index=False)
            if selected_metadata:
                provenance = {
                    'Product': active_product, 'Source': source, 'Revision': selected_metadata.get('revision_id', revision),
                    'Selected': selected_metadata['current_label'],
                    'Valuation date': selected_metadata['business_date'],
                    'Evidence cutoff': selected_metadata['cutoff'],
                    'Comparison': selected_metadata['comparison_label'],
                    'Comparison publication': selected_metadata['comparison_id'],
                    'Comparison snapshot': json.dumps(selected_metadata.get('comparison_snapshot_reference')),
                    'Settlement anchor': selected_metadata['base_publication_id'],
                    'Settlement COB': selected_metadata['settlement_cob'],
                    'Changed months': ', '.join(selected_metadata['changed_months']),
                    'Units': 'Decimal IV; Change is decimal IV difference (multiply by 100 for vol points)',
                }
                pd.DataFrame(provenance.items(), columns=['Field', 'Value']).to_excel(
                    writer, sheet_name='Surface Selection', index=False,
                )
            if source == 'settlement' and str(active_product).upper() == 'TTF' and selected_expiry:
                try:
                    comparison_curves, comparison_trades = (
                        surface_data._load_ttf_source_comparison(
                            selected_date,
                            selected_expiry,
                        )
                    )
                except Exception:
                    comparison_curves = pd.DataFrame()
                    comparison_trades = pd.DataFrame()
                if not comparison_curves.empty:
                    comparison_curves.to_excel(
                        writer,
                        sheet_name='TTF ICAP vs ICE',
                        index=False,
                    )
                if not comparison_trades.empty:
                    comparison_trades.to_excel(
                        writer,
                        sheet_name='TTF Trade Marks',
                        index=False,
                    )
        output.seek(0)

        selected_date_str = pd.to_datetime((selected_metadata or {}).get('business_date', selected_date)).strftime('%Y%m%d')
        suffix = f'_{source}_{revision[:8]}' if selected_metadata else ''
        return dcc.send_bytes(output.getvalue(), f'{active_product.lower()}_surface_{selected_date_str}{suffix}.xlsx')
    except Exception:
        return None
 


@callback(
    Output('vol-overview-navigation', 'data'),
    Input({'type': 'vol-overview-chart', 'product': ALL, 'metric': ALL}, 'clickData'),
    Input({'type': 'vol-overview-inspect', 'product': ALL}, 'n_clicks'),
    Input('surface-heatmap-graph', 'clickData'),
    Input('volatility-table', 'cellClicked', allow_optional=True),
    Input('changes-table', 'cellClicked', allow_optional=True),
    Input('surface-current-table', 'cellClicked', allow_optional=True),
    Input('surface-diff-table', 'cellClicked', allow_optional=True),
    State('surface-product-tabs', 'value'),
    State('table-grouping-dropdown', 'value'),
    State({'type': 'vol-overview-chart', 'product': ALL, 'metric': ALL}, 'id'),
    State({'type': 'vol-overview-inspect', 'product': ALL}, 'id'),
    State('vol-overview-context', 'data'),
    State('vol-overview-navigation', 'data'),
    prevent_initial_call=True,
)
def navigate_workspace(clicks, inspections, heat_click, table_click, changes_click,
                       matrix_click, diff_click, active_product, grouping, chart_ids, link_ids, context, previous):
    return navigate_overview(clicks, inspections, chart_ids, link_ids, context, previous,
        heat_click, table_click, changes_click, matrix_click, diff_click, active_product, grouping)


def navigate_overview(clicks, inspections, chart_ids, link_ids, context, previous,
                      heat_click=None, table_click=None, changes_click=None,
                      matrix_click=None, diff_click=None, active_product=None, grouping='monthly'):
    valid = (context or {}).get('months', [])
    trigger = ctx.triggered_id
    if isinstance(trigger, str):
        events = {'surface-heatmap-graph': heat_click,
                  'volatility-table': table_click, 'changes-table': changes_click,
                  'surface-current-table': matrix_click, 'surface-diff-table': diff_click}
        event = events.get(trigger)
        picked = detail_click_selection(trigger, event, active_product, grouping)
        if picked is None:
            return no_update
        product, chosen = picked
        return {'product': product, 'month': chosen, 'sequence': (previous or {}).get('sequence', 0) + 1}
    if not valid or not isinstance(trigger, dict):
        return no_update
    product = trigger.get('product')
    if trigger.get('type') == 'vol-overview-chart':
        if trigger not in (chart_ids or []):
            return no_update
        chosen = overview.click_selection(clicks[chart_ids.index(trigger)], product, valid)
    elif trigger.get('type') == 'vol-overview-inspect':
        if trigger not in (link_ids or []) or not inspections[link_ids.index(trigger)]:
            return no_update
        chosen = (context or {}).get('fronts', {}).get(product)
    else:
        return no_update
    if chosen not in valid:
        return no_update
    # A repeated Inspect click must reopen the front even after a manual smile change.
    sequence = (previous or {}).get('sequence', 0)
    sequence = sequence if isinstance(sequence, int) and sequence >= 0 else 0
    return {'product': product, 'month': chosen, 'sequence': sequence + 1}


@callback(
    Output('atm-status-line', 'children'),
    Output('graphs-container', 'children'),
    Output('vol-overview-context', 'data'),
    Output('vol-overview-window-label', 'children'),
    Input('vol-surface-snapshot-ref', 'data'),
    Input('table-date-picker', 'date'),
    Input('table-prev-date-picker', 'date'),
    Input('table-product-dropdown', 'value'),
    Input('vol-overview-window', 'value'),
    Input('vol-overview-delta', 'value'),
    Input('surface-product-tabs', 'value'),
    Input('surface-selection-ref', 'data'),
    Input('surface-source-dropdown', 'value'),
    Input('surface-revision-dropdown', 'value'),
    Input('surface-comparison-dropdown', 'value'),
    prevent_initial_call=True,
)
def render_overview(reference, date, previous_date, products, window, wing_delta, active_product,
                    selection, source, revision, comparison):
    if wing_delta not in overview.WING_DELTAS:
        return _build_message_span('Choose an available wing delta.', tone='error'), no_update, no_update, no_update
    try:
        pairs = overview.surface_pairs(reference, date, previous_date, products, active_product,
                                       source, selection, revision, comparison)
    except SnapshotReferenceError:
        return (_build_message_span('Surface snapshot unavailable; refresh Options Data.', tone='error'),
                html.Div(), {'months': [], 'fronts': {}}, 'Surface snapshot unavailable')
    months = overview.delivery_months(pairs, window)
    fronts = {product: overview.front_month(pair['current']) for product, pair in pairs.items()}
    context = {'months': [m.strftime('%Y-%m-%d') for m in months],
               'fronts': {product: front.strftime('%Y-%m-%d') for product, front in fronts.items() if front is not None}}
    label = f"{months[0]:%b %Y} – {months[-1]:%b %Y} · monthly" if len(months) else 'No unexpired maturities'
    cards = [overview.product_card(product, pair, months, active_product, wing_delta) for product, pair in pairs.items()]
    return '', html.Div(cards, className='vol-overview-grid'), context, label


def detail_click_selection(trigger, event, product, grouping='monthly'):
    """Decode only real monthly cells; aggregates never become a single option."""
    if not isinstance(event, dict):
        return None
    try:
        if trigger == 'surface-heatmap-graph':
            point = event['points'][0]
            value = point['y']
            month = pd.Timestamp(value).to_period('M').to_timestamp()
        elif trigger in {'volatility-table', 'changes-table'}:
            if grouping != 'monthly':
                return None
            product = event['data']['product']
            month = pd.to_datetime(event['colId'], format='%m-%y')
        else:
            month = pd.to_datetime(event['data']['expiry'], format="%b'%y")
        if pd.isna(month) or not product:
            return None
        return product, month.strftime('%Y-%m-%d')
    except (KeyError, IndexError, TypeError, ValueError):
        return None


@callback(Output('surface-history-selection-label', 'children'),
          Input('surface-product-tabs', 'value'), Input('surface-expiry-dropdown', 'value'))
def linked_history_label(product, expiry):
    return f"{product or 'Select product'} · {_format_surface_expiry_selection_label(expiry) or 'Select contract'}"
