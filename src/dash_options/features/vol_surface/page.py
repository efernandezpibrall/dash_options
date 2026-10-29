"""Volatility surface dashboard page."""
import io

from dash import html, dcc, callback, Output, Input, State, no_update
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


def _create_surface_heatmap_figure(product, current_surface, previous_surface, heatmap_mode):
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
    fig.update_yaxes(**_vol_axis('Expiry', showgrid=False, autorange='reversed'))

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


def _create_smile_evolution_figure(product, selected_expiry, current_surface, previous_surface, lookback_days, end_date):
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
        name=f"Current {current_date.strftime('%Y-%m-%d')}",
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
            name=f"Previous {previous_date.strftime('%Y-%m-%d') if previous_date is not None else ''}",
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
            product,contracts,end,days,surface_data.surface_dataset,revision)
        fig = make_subplots(specs=[[{'secondary_y': True}]])
        for month_index,month in enumerate(contracts):
            label = pd.Timestamp(month).strftime('%b-%y')
            for bucket_index,bucket in enumerate(buckets):
                color = VOL_LINE_PALETTE[(month_index*3+bucket_index)%len(VOL_LINE_PALETTE)]
                for frame,origin,dash in ((published,'Published','solid'),(archived,'Archived reconstruction','dot')):
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
        note = 'Published IV and archived reconstructions are separate series; missing dates remain gaps.'
        if meta['errors']:
            note += ' '+ ' | '.join(meta['errors'])
        fig.add_annotation(text=note,xref='paper',yref='paper',x=0,y=1.12,showarrow=False,xanchor='left',font=dict(size=10))
        fig.layout.transition = None
        fig.update_layout(meta={k:v for k,v in meta.items() if k!='sessions'},
                          uirevision=f"{revision}:{product}:{contracts}:{buckets}:{days}")
        return fig
    except Exception as exc:
        return _empty_figure(f'{product} history unavailable: {safe_exception_message(exc)}','Delta Vol History')


def _create_realized_comparison_figure(product, contract, lookback_days, end_date, surface_frame, revision=None):
    try:
        rows,meta=realized_comparison_data.load_comparison(product,contract,end_date,lookback_days,surface_frame,revision)
        if rows.empty:
            return _empty_figure('No trading sessions for this maturity in the selected range.','Realized vs Implied Volatility'), 'No observations in the selected range.'
        label=product+' '+pd.Timestamp(contract).strftime('%b-%y')
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
        iv_dates=rows.loc[rows.volatility.notna(),'cob_date']
        last_iv=iv_dates.max().strftime('%d %b %Y') if not iv_dates.empty else 'unavailable'
        status=(f"{label} · Realized: {rows.realized_volatility.notna().sum()}/{len(rows)} sessions"
                f" · Implied: {rows.volatility.notna().sum()}/{len(rows)} sessions · Last IV: {last_iv}"
                f" · Prices: {meta['price_source']} through {meta['last_price_date']}"
                ' · Missing settlements leave RV gaps; sources are never spliced.'
                ' · Trailing RV and remaining-life IV measure different periods.'
                + (' · Some price sources unavailable.' if meta.get('source_errors') else ''))
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

    history_df = surface_data.surface_dataset.copy()
    if history_df.empty:
        return _empty_figure('No surface history available.', 'Delta Vol History')

    history_df = history_df[
        (history_df['code'] == product) &
        (history_df['delta_bucket'].isin(selected_buckets)) &
        (history_df['cob_date'] >= start_date) &
        (history_df['cob_date'] <= end_date)
    ].copy()

    if history_mode == 'fixed_expiry' or expiry_type != surface_data.SURFACE_EXPIRY_MONTH:
        history_df = surface_data._filter_surface_by_expiry_selection(history_df, selected_expiry)
    else:
        tenor_rank = surface_data._get_selected_tenor_rank(product, selected_expiry, end_date)
        history_df = surface_data._select_rolling_tenor_history(history_df, tenor_rank)

    if history_df.empty:
        return _empty_figure(
            f'No history is available for the selected buckets over the last {lookback_days} days.',
            'Delta Vol History'
        )

    history_df = history_df.groupby(['cob_date', 'delta_bucket'], as_index=False)['volatility'].mean().sort_values(['delta_bucket', 'cob_date'])

    fig = go.Figure()
    color_palette = VOL_SURFACE_PALETTE
    for index, bucket in enumerate(selected_buckets):
        bucket_df = history_df[history_df['delta_bucket'] == bucket].copy()
        if bucket_df.empty:
            continue

        color = color_palette[index % len(color_palette)]
        fig.add_trace(go.Scatter(
            x=bucket_df['cob_date'],
            y=bucket_df['volatility'],
            mode='lines+markers',
            name=bucket,
            line=dict(color=color, width=2),
            marker=dict(size=6, color=color, line=dict(width=1, color='white')),
            hovertemplate='COB %{x|%Y-%m-%d}<br>Bucket %{fullData.name}<br>Vol %{y:.2%}<extra></extra>',
        ))

    _apply_vol_chart_theme(
        fig,
        None,
        margin=dict(l=46, r=16, t=18, b=76),
    )
    fig.update_xaxes(**_vol_axis('COB'))
    fig.update_yaxes(**_vol_axis('IV', tickformat='.0%'))

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
        return ''
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return str(value)
    sign = '+' if signed and number > 0 else ''
    suffix = ' pp' if signed else '%'
    return f'{sign}{number:.2f}{suffix}'


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
        'sortable': True,
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
            'width': _estimate_vol_table_width(dataframe, column, signed=include_diff_colors),
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
                'volatility-positive-cell': f"Number(params.data['{raw_field}']) > 0",
                'volatility-negative-cell': f"Number(params.data['{raw_field}']) < 0",
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
        return ''
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return str(value)
    if signed:
        sign = '+' if number > 0 else ''
        return f'{sign}{number:.2f} pp'
    return f'{number:.2f}%'


def _surface_raw_field(column):
    return f'__raw_surface_{column}'


def _estimate_surface_width(samples, min_width=58, max_width=108, character_px=7):
    content_length = max((len(str(sample)) for sample in samples if sample is not None), default=0)
    return _clamp_vol_width(content_length * character_px + 24, min_width, max_width)


def _prepare_surface_table_display_df(dataframe, delta_columns, include_diff_colors=False):
    display_df = pd.DataFrame(index=dataframe.index)
    display_df['expiry'] = dataframe['expiry'].apply(_format_surface_expiry_label)
    display_df['dte'] = pd.to_numeric(dataframe['dte'], errors='coerce').round().astype('Int64')

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
        width = _estimate_surface_width(
            [column, *display_df[column].dropna().astype(str).tolist()[:200]],
            min_width=64,
            max_width=104 if include_diff_colors else 92,
        )
        column_def = {
            'headerName': str(column),
            'field': str(column),
            'type': 'rightAligned',
            'sortable': False,
            'filter': False,
            'resizable': True,
            'width': width,
            'minWidth': 58,
            'maxWidth': max(width, 108),
            'cellClass': 'mckinsey-ag-grid-cell mckinsey-ag-grid-number-cell volatility-surface-delta-cell',
            'headerClass': 'mckinsey-ag-grid-header volatility-surface-delta-header',
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
                'volatility-positive-cell': f"Number(params.data['{raw_field}']) > 0",
                'volatility-negative-cell': f"Number(params.data['{raw_field}']) < 0",
            })
        column_defs.append(column_def)

    return column_defs


def _column_def_width_sum(column_defs):
    return sum(int(column.get('width') or column.get('minWidth') or 0) for column in column_defs)


def _create_surface_ag_grid(dataframe, delta_columns, table_id, include_diff_colors=False):
    if dataframe.empty or not delta_columns:
        return html.Div('No surface data for the current selection.', className='volatility-table-empty-state')

    display_df = _prepare_surface_table_display_df(dataframe, delta_columns, include_diff_colors=include_diff_colors)
    column_defs = _build_surface_grid_column_defs(display_df, delta_columns, include_diff_colors=include_diff_colors)
    content_width = _column_def_width_sum(column_defs) + 30
    return dag.AgGrid(
        id=table_id,
        rowData=_clean_vol_table_records(display_df),
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
        },
        className=(
            'ag-theme-alpine mckinsey-ag-grid supply-dest-summary-grid volatility-surface-grid'
            + (' volatility-surface-change-grid' if include_diff_colors else '')
        ),
        style={'width': f'{content_width}px', 'maxWidth': '100%', 'height': 'auto'},
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
                            html.Span('Current', className='volatility-surface-date-label'),
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
                    html.Div(
                        [
                            html.Span('Previous', className='volatility-surface-date-label'),
                            dcc.DatePickerSingle(
                                id='table-prev-date-picker',
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

    _build_vol_surface_filter_bar(),
    html.Div(id='options-refresh-message', className='volatility-refresh-message'),
    html.Div(id='vol-surface-publication-status', role='status', **{'aria-live': 'polite'}),

    html.Div([
        _build_volatility_section_header('ATM Volatility'),
        html.Div(id='atm-status-line', className='volatility-status-line volatility-status-neutral'),
        html.Div(id='graphs-container', className='volatility-section-body volatility-atm-body'),
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
            dcc.Tabs(id='surface-product-tabs', value=None, children=[]),
            html.Div([
                html.Label('Smile Expiry:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='surface-expiry-dropdown',
                    options=[],
                    value=None,
                    clearable=False,
                    className="inline-dropdown-aggregation"
                ),
                html.Label('Delta Buckets:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='surface-history-buckets-dropdown',
                    options=[],
                    value=[],
                    multi=True,
                    placeholder='Select 1-3 buckets...',
                    className="inline-dropdown-multi"
                ),
                html.Label('Lookback:', className="inline-filter-label"),
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
                html.Label('History Mode:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='surface-history-mode-dropdown',
                    options=[
                        {'label': 'Fixed Expiry', 'value': 'fixed_expiry'},
                        {'label': 'Rolling Tenor', 'value': 'rolling_tenor'}
                    ],
                    value='fixed_expiry',
                    clearable=False,
                    className="inline-dropdown-aggregation"
                )
            ], id='surface-expiry-controls', style={'display': 'none', 'align-items': 'center', 'gap': '16px', 'margin': '16px 0', 'flex-wrap': 'wrap'}),
            html.Div([
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
                _build_vol_chart_card(
                    dcc.Graph(
                        id='surface-smile-graph',
                        config=VOL_GRAPH_CONFIG,
                        className='volatility-chart-graph',
                        style={'height': '388px'}
                    ),
                    'Smile Evolution',
                    className='volatility-surface-chart-card'
                )
            ], className='volatility-surface-chart-grid'),
            html.Div([
                html.Label('History contracts:',className='inline-filter-label',htmlFor='surface-history-contracts-dropdown'),
                dcc.Dropdown(id='surface-history-contracts-dropdown',
                    options=[],value=[],multi=True,clearable=True,
                    className='inline-dropdown-multi'),
            ],id='surface-history-contracts-controls',style={'display':'none'},
                className='volatility-history-contracts-controls'),
            _build_vol_chart_card(
                dcc.Graph(
                    id='surface-history-graph',
                    config=VOL_GRAPH_CONFIG,
                    className='volatility-chart-graph',
                    style={'height': '368px'}
                ),
                'Delta Vol History',
                className='volatility-history-chart-card'
            ),
            html.Div([
                html.Div([
                    html.Label('Comparison contract:',className='inline-filter-label',htmlFor='surface-rv-contract-dropdown'),
                    dcc.Dropdown(id='surface-rv-contract-dropdown',options=[],value='2027-01-01',clearable=False,
                                 className='inline-dropdown-aggregation'),
                    html.Span('Rolling window: 30 calendar days',className='volatility-rv-window-label'),
                ],className='volatility-rv-controls'),
                _build_vol_chart_card(
                    dcc.Graph(id='surface-rv-comparison-graph',config=VOL_GRAPH_CONFIG,
                              className='volatility-chart-graph',style={'height':'480px'}),
                    'Realized vs Implied Volatility',className='volatility-rv-chart-card'),
                html.Div(id='surface-rv-comparison-status',className='volatility-rv-comparison-status'),
            ],id='surface-rv-comparison-section',style={'display':'none'}),
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
    notices = status_notices(status)
    if status.get('latest_cob') and status['latest_cob'] > selected_date:
        notices.insert(0, {'message':f"Settlements are available through {status['latest_cob']}; selected date is {selected_date}.",
                           'tone':'warning'})
    changed = (isinstance(previous, dict) and previous.get('cob') == selected_date
               and previous.get('revision') != status['revision'])
    if not previous and isinstance(snapshot_reference, dict):
        from dash_options.core.snapshots import resolve_snapshot
        try:
            snapshot = resolve_snapshot(snapshot_reference, expected_namespace=surface_data.VOL_SURFACE_SNAPSHOT_NAMESPACE)
            cached = snapshot['data_cache_state']['surface'].get('publications', {})
            changed = any(row['commodity'] in surface_data.PUBLISHED_SURFACE_PRODUCTS
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


def _build_atm_status_line(selected_date, selected_products, grouping_mode):
    atm_error = surface_data.DATA_CACHE_STATE['atm']['error']
    surface_error = surface_data.DATA_CACHE_STATE['surface']['error']
    surface_source = surface_data.DATA_CACHE_STATE['surface']['source']
    selected_products = selected_products or []

    if surface_data.atm_dataset.empty and (atm_error or surface_error):
        return _build_message_span(f'ATM source unavailable: {atm_error or surface_error}', tone='error')

    current_pivot, _, sorted_date_cols = surface_data._build_atm_table_frames(selected_date, None, selected_products, grouping_mode)
    visible_products = int(current_pivot['product'].nunique()) if not current_pivot.empty else 0
    period_count = len(sorted_date_cols)
    missing_cells = int(current_pivot[sorted_date_cols].isna().sum().sum()) if sorted_date_cols and not current_pivot.empty else 0
    selected_date_text = pd.to_datetime(selected_date).strftime('%Y-%m-%d') if selected_date else 'n/a'

    message = (
        f'ATM source: 50-delta rows derived from {surface_source} for Brent/HH/TTF/JKM/NBP. '
        f'Selected date: {selected_date_text} | Products shown: {visible_products}/{len(selected_products)} | '
        f'Grouped tenors: {period_count} | Missing cells: {missing_cells}'
    )

    if surface_error:
        message = f'{message} | Surface-derived ATM warning: {surface_error}'
        return _build_message_span(message, tone='warning')
    return _build_message_span(message, tone='neutral')


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


def update_atm_status_line(n_clicks, selected_date, selected_products, grouping_mode):
    return _build_atm_status_line(selected_date, selected_products, grouping_mode or 'monthly')


def update_graphs(n_clicks, selected_date, selected_products, grouping_mode):
    if selected_date is None or not selected_products or surface_data.atm_dataset.empty:
        return html.Div()

    selected_date = pd.to_datetime(selected_date)
    atm_df = surface_data.atm_dataset.copy()
    atm_df['cob_date'] = pd.to_datetime(atm_df['cob_date'], errors='coerce')
    atm_df['contract_date'] = pd.to_datetime(atm_df['contract_date'], errors='coerce')

    chart_cards = []
    product_codes = list(selected_products)

    for code in product_codes:
        code_df = atm_df[atm_df['code'] == code].copy()
        if code_df.empty:
            continue

        fig = go.Figure()
        recent_dates = code_df['cob_date'].drop_duplicates().nlargest(5)
        has_selected_or_closest = False

        for date_index, cob_date in enumerate(recent_dates):
            cob_date = pd.Timestamp(cob_date)
            cob_data = code_df[code_df['cob_date'] == cob_date].sort_values('contract_date').copy()
            if cob_data.empty:
                continue

            is_selected_date = cob_date.date() == selected_date.date()
            if is_selected_date:
                has_selected_or_closest = True

            palette_color = VOL_LINE_PALETTE[date_index % len(VOL_LINE_PALETTE)]
            line_style = (
                dict(width=2.6, color=VOL_SELECTED_LINE)
                if is_selected_date else
                dict(width=1.15, color=palette_color)
            )
            legend_name = (
                f'Selected {cob_date.strftime("%Y-%m-%d")}'
                if is_selected_date else
                cob_date.strftime('%Y-%m-%d')
            )

            if grouping_mode != 'monthly':
                grouped_data = surface_data.group_data_by_period(cob_data, grouping_mode).sort_values('period')
                x_values = grouped_data['period'] if 'period' in grouped_data.columns else cob_data['contract_date']
                y_values = grouped_data['volatility'] if 'period' in grouped_data.columns else cob_data['volatility']
                mode = 'lines+markers' if 'period' in grouped_data.columns else 'lines'
            else:
                x_values = cob_data['contract_date']
                y_values = cob_data['volatility']
                mode = 'lines'

            fig.add_trace(go.Scatter(
                x=x_values,
                y=y_values,
                mode=mode,
                name=legend_name,
                line=line_style,
                marker=dict(
                    size=5 if is_selected_date else 3.5,
                    color=line_style['color'],
                    line=dict(width=1, color='white') if is_selected_date else dict(width=0),
                ),
                opacity=1.0 if is_selected_date else 0.52,
                connectgaps=True,
                hovertemplate='Tenor %{x}<br>Vol %{y:.2%}<extra>%{fullData.name}</extra>',
            ))

        if not has_selected_or_closest:
            prior_dates = code_df[code_df['cob_date'] < selected_date]['cob_date']
            candidate_date = None
            if not prior_dates.empty:
                candidate_date = pd.Timestamp(prior_dates.iloc[(prior_dates - selected_date).abs().argsort()[:1]].values[0])
            elif not code_df.empty:
                candidate_date = pd.Timestamp(code_df['cob_date'].iloc[(code_df['cob_date'] - selected_date).abs().argsort()[:1]].values[0])

            if candidate_date is not None:
                cob_data = code_df[code_df['cob_date'] == candidate_date].sort_values('contract_date').copy()
                if not cob_data.empty:
                    if grouping_mode != 'monthly':
                        grouped_data = surface_data.group_data_by_period(cob_data, grouping_mode).sort_values('period')
                        x_values = grouped_data['period'] if 'period' in grouped_data.columns else cob_data['contract_date']
                        y_values = grouped_data['volatility'] if 'period' in grouped_data.columns else cob_data['volatility']
                        mode = 'lines+markers' if 'period' in grouped_data.columns else 'lines'
                    else:
                        x_values = cob_data['contract_date']
                        y_values = cob_data['volatility']
                        mode = 'lines'

                    fig.add_trace(go.Scatter(
                        x=x_values,
                        y=y_values,
                        mode=mode,
                        name=f'Closest {candidate_date.strftime("%Y-%m-%d")}',
                        line=dict(width=1.5, color='#d97706', dash='dot'),
                        marker=dict(size=4, color='#d97706'),
                        hovertemplate='Tenor %{x}<br>Vol %{y:.2%}<extra>%{fullData.name}</extra>',
                    ))

        _apply_vol_chart_theme(
            fig,
            None,
            margin=dict(l=44, r=12, t=18, b=74),
            height=303,
        )
        fig.update_xaxes(**_vol_axis('', tickangle=0))
        fig.update_yaxes(**_vol_axis('IV', tickformat='.0%'))

        chart_cards.append(_build_vol_chart_card(
            dcc.Graph(
                figure=fig,
                config=VOL_GRAPH_CONFIG,
                className='volatility-chart-graph',
                style={'height': '303px'}
            ),
            f'{code} ATM Volatility',
            className='volatility-atm-chart-card'
        ))

    return html.Div(chart_cards, className='volatility-atm-chart-grid')


def update_tables(n_clicks, selected_date, prev_selected_date, selected_products, grouping_mode):
    if selected_date is None or not selected_products:
        return html.Div()

    current_pivot, changes_pivot, sorted_date_cols = surface_data._build_atm_table_frames(selected_date, prev_selected_date, selected_products, grouping_mode)

    current_table = html.Div([
        _build_vol_table_panel_header(
            'Current ATM IV',
            chips=[
                _build_vol_chart_chip('COB', _format_vol_date(selected_date), tone='primary'),
                _build_vol_chart_chip('Products', current_pivot['product'].nunique() if not current_pivot.empty else 0),
                _build_vol_chart_chip('Tenors', len(sorted_date_cols)),
                _build_vol_chart_chip('Mode', _format_vol_mode(grouping_mode or 'monthly')),
            ],
        ),
        _create_volatility_ag_grid(
            table_id='volatility-table',
            dataframe=current_pivot,
            period_columns=sorted_date_cols,
        )
    ], className='volatility-table-panel volatility-current-table-panel')

    changes_table = html.Div()
    if not changes_pivot.empty:
        changes_table = html.Div([
            _build_vol_table_panel_header(
                'Change vs Previous COB',
                chips=[
                    _build_vol_chart_chip('COB', _format_vol_date(selected_date), tone='primary'),
                    _build_vol_chart_chip('Prev', _format_vol_date(prev_selected_date)),
                    _build_vol_chart_chip('Tenors', len(sorted_date_cols)),
                ],
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
    Output('atm-status-line', 'children'),
    Output('graphs-container', 'children'),
    Output('tables-container', 'children'),
    Input('vol-surface-snapshot-ref', 'data'),
    Input('table-date-picker', 'date'),
    Input('table-prev-date-picker', 'date'),
    Input('table-product-dropdown', 'value'),
    Input('table-grouping-dropdown', 'value'),
    prevent_initial_call=True,
)
def render_atm_section(
    snapshot_reference,
    selected_date,
    prev_selected_date,
    selected_products,
    grouping_mode,
):
    surface_data._ensure_cached_data(snapshot_reference)
    return (
        update_atm_status_line(None, selected_date, selected_products, grouping_mode),
        update_graphs(None, selected_date, selected_products, grouping_mode),
        update_tables(
            None,
            selected_date,
            prev_selected_date,
            selected_products,
            grouping_mode,
        ),
    )


@callback(
    [Output('surface-product-tabs', 'children'),
     Output('surface-product-tabs', 'value'),
    Output('surface-content-wrapper', 'style'),
     Output('surface-empty-message', 'children')],
    [Input('vol-surface-snapshot-ref', 'data'),
     Input('table-product-dropdown', 'value'),
     Input('table-date-picker', 'date')],
    State('surface-product-tabs', 'value'),
    prevent_initial_call=True
)
def update_surface_tabs(snapshot_reference, selected_products, selected_date, active_tab):
    surface_data._ensure_cached_data(snapshot_reference)
    supported_products = surface_data._get_supported_surface_products(selected_products, selected_date)

    if not supported_products:
        return [], None, {'display': 'none'}, 'Full surface data is available only for Brent, HH, TTF, JKM, and NBP.'

    tabs = [dcc.Tab(label=product, value=product) for product in supported_products]
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
     Input('surface-product-tabs', 'value')],
    State('surface-expiry-dropdown', 'value'),
    prevent_initial_call=True
)
def update_surface_expiry_dropdown(snapshot_reference, selected_date, active_product, current_expiry):
    surface_data._ensure_cached_data(snapshot_reference)
    hidden_style = {'display': 'none', 'align-items': 'center', 'gap': '16px', 'margin': '16px 0', 'flex-wrap': 'wrap'}
    visible_style = {'display': 'flex', 'align-items': 'center', 'gap': '16px', 'margin': '16px 0', 'flex-wrap': 'wrap'}

    if selected_date is None or not active_product:
        return [], None, hidden_style

    current_surface = surface_data._get_surface_snapshot(active_product, selected_date)
    if current_surface.empty:
        return [], None, hidden_style

    expiry_options = _build_surface_expiry_options(current_surface)
    if not expiry_options:
        return [], None, hidden_style

    valid_expiries = {option['value'] for option in expiry_options}
    normalized_current_expiry = surface_data._normalize_surface_expiry_selection(current_expiry)
    selected_expiry = (
        normalized_current_expiry
        if normalized_current_expiry in valid_expiries
        else expiry_options[0]['value']
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


@callback(
    [Output('surface-history-buckets-dropdown', 'options'),
     Output('surface-history-buckets-dropdown', 'value')],
    [Input('vol-surface-snapshot-ref', 'data'),
     Input('table-date-picker', 'date'),
     Input('surface-product-tabs', 'value'),
     Input('surface-expiry-dropdown', 'value')],
    State('surface-history-buckets-dropdown', 'value'),
    prevent_initial_call=True
)
def update_surface_delta_dropdown(snapshot_reference, selected_date, active_product, selected_expiry, current_history_buckets):
    surface_data._ensure_cached_data(snapshot_reference)
    if selected_date is None or not active_product or not selected_expiry:
        return [], []

    current_surface = surface_data._get_surface_snapshot(active_product, selected_date)
    if current_surface.empty:
        return [], []

    expiry_surface = surface_data._filter_surface_by_expiry_selection(current_surface, selected_expiry)
    delta_options = _get_delta_bucket_options(expiry_surface)
    valid_deltas = {option['value'] for option in delta_options}
    history_buckets = [bucket for bucket in (current_history_buckets or []) if bucket in valid_deltas]
    preferred_delta = 'ATM' if 'ATM' in valid_deltas else (delta_options[0]['value'] if delta_options else None)
    if not history_buckets and preferred_delta:
        history_buckets = [preferred_delta]
    history_buckets = history_buckets[:3]

    return delta_options, history_buckets


@callback(Output('surface-rv-comparison-section','style'),
          Output('surface-rv-contract-dropdown','options'),Output('surface-rv-contract-dropdown','value'),
          Input('vol-surface-snapshot-ref','data'),Input('table-date-picker','date'),Input('surface-product-tabs','value'),
          State('surface-rv-contract-dropdown','value'),prevent_initial_call=True)
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
          Input('surface-rv-contract-dropdown','value'),Input('surface-lookback-dropdown','value'),prevent_initial_call=True)
def update_realized_comparison(snapshot_reference,selected_date,product,contract,lookback):
    if product not in realized_comparison_data.CALENDARS or not selected_date or not contract:
        return _empty_figure('Select a product and maturity.','Realized vs Implied Volatility'),''
    surface_data._ensure_cached_data(snapshot_reference)
    fig,status=_create_realized_comparison_figure(product,contract,lookback,selected_date,surface_data.surface_dataset, snapshot_reference)
    if fig.layout.meta:
        fig.update_layout(meta={**fig.layout.meta,'iv_snapshot_reference':snapshot_reference})
    return fig,status


@callback(Output('surface-history-contracts-controls','style'),
          Input('surface-product-tabs','value'),Input('surface-history-mode-dropdown','value'))
def show_history_contracts(product,mode):
    return {'display':'flex' if product in delta_history_data.CALENDARS and mode == 'fixed_expiry' else 'none',
            'alignItems':'center','gap':'12px','flexWrap':'wrap','marginBottom':'12px'}


@callback(Output('surface-history-contracts-dropdown','options'),
          Output('surface-history-contracts-dropdown','value'),
          Input('vol-surface-snapshot-ref','data'),Input('table-date-picker','date'),
          Input('surface-product-tabs','value'),Input('surface-lookback-dropdown','value'),
          State('surface-history-contracts-dropdown','value'))
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


@callback(Output('surface-lookback-dropdown','value'),Input('surface-product-tabs','value'),
          State('surface-lookback-dropdown','value'))
def default_brent_history_lookback(product,current):
    return 90 if product == 'Brent' else (current or 30)


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
     Input('surface-history-contracts-dropdown','value')],
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
    history_contracts=None
):
    surface_data._ensure_cached_data(snapshot_reference)

    if selected_date is None or not active_product:
        status_line = _build_surface_status_line(selected_date, prev_selected_date, active_product)
        empty_heatmap = _empty_figure('Select a supported product to view the surface.', 'Surface Heatmap')
        empty_smile = _empty_figure('Select a supported product to view smile details.', 'Smile Evolution')
        empty_history = _empty_figure('Select a supported product, expiry, and delta bucket to view history.', 'Delta Vol History')
        return empty_heatmap, empty_smile, empty_history, html.Div(), status_line

    current_surface = surface_data._get_surface_snapshot(active_product, selected_date)
    previous_surface = surface_data._get_surface_snapshot(active_product, prev_selected_date) if prev_selected_date else surface_data._empty_surface_df()
    status_line = _build_surface_status_line(
        selected_date,
        prev_selected_date,
        active_product,
        current_surface=current_surface,
        previous_surface=previous_surface,
    )

    if current_surface.empty:
        no_data_message = html.Div(
            f'No surface data available for {active_product} on {pd.to_datetime(selected_date).strftime("%Y-%m-%d")}.',
            style={'padding': '12px 0', 'color': '#6b7280'}
        )
        return (
            _create_surface_heatmap_figure(active_product, current_surface, previous_surface, heatmap_mode or 'vs_atm'),
            _create_smile_evolution_figure(active_product, None, current_surface, previous_surface, lookback_days, selected_date),
            _create_delta_history_figure(active_product, None, None, lookback_days, selected_date, history_mode or 'fixed_expiry'),
            no_data_message,
            status_line
        )

    if selected_expiry is None:
        selected_expiry = surface_data._surface_expiry_value(
            surface_data.SURFACE_EXPIRY_MONTH,
            pd.to_datetime(current_surface['contract_date'].min()),
        )

    expiry_surface = surface_data._filter_surface_by_expiry_selection(current_surface, selected_expiry)
    delta_options = _get_delta_bucket_options(expiry_surface)
    valid_deltas = {option['value'] for option in delta_options}
    selected_history_buckets = [bucket for bucket in (selected_history_buckets or []) if bucket in valid_deltas]
    preferred_bucket = 'ATM' if 'ATM' in valid_deltas else (delta_options[0]['value'] if delta_options else None)
    if not selected_history_buckets and preferred_bucket:
        selected_history_buckets = [preferred_bucket]
    selected_history_buckets = selected_history_buckets[:3]

    heatmap_figure = _create_surface_heatmap_figure(active_product, current_surface, previous_surface, heatmap_mode or 'vs_atm')
    smile_figure = _create_smile_evolution_figure(active_product, selected_expiry, current_surface, previous_surface, lookback_days, selected_date)
    history_figure = _create_delta_history_figure(active_product, selected_expiry, selected_history_buckets, lookback_days, selected_date, history_mode or 'fixed_expiry',history_contracts=history_contracts,snapshot_reference=snapshot_reference)
    current_table_df, diff_table_df, delta_columns = _build_surface_tables(current_surface, previous_surface, selected_date)

    current_table = _build_surface_table_panel(
        'Surface Matrix',
        _create_surface_ag_grid(current_table_df, delta_columns, 'surface-current-table'),
        chips=[
            _build_vol_chart_chip('Rows', len(current_table_df), 'primary'),
            _build_vol_chart_chip('Deltas', len(delta_columns)),
            _build_vol_chart_chip('Format', 'Vol %'),
        ],
        className='volatility-surface-current-panel',
    )

    if not diff_table_df.empty:
        diff_table = _build_surface_table_panel(
            'Difference vs. Previous Date',
            _create_surface_ag_grid(diff_table_df, delta_columns, 'surface-diff-table', include_diff_colors=True),
            chips=[
                _build_vol_chart_chip('Rows', len(diff_table_df), 'primary'),
                _build_vol_chart_chip('Deltas', len(delta_columns)),
                _build_vol_chart_chip('Format', 'pp'),
            ],
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
     State('vol-surface-snapshot-ref', 'data')],
    prevent_initial_call=True
)
def export_volatility_table(
    n_clicks,
    selected_date,
    selected_products,
    grouping_mode,
    snapshot_reference=None,
):
    if not n_clicks or not selected_date or not selected_products:
        return None

    try:
        surface_data._ensure_cached_data(snapshot_reference)

        current_pivot, _, sorted_date_cols = surface_data._build_atm_table_frames(selected_date, None, selected_products, grouping_mode)
        if current_pivot.empty:
            return None

        export_df = current_pivot[['product'] + sorted_date_cols]
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            export_df.to_excel(writer, sheet_name='Volatility Data', index=False)
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
     State('vol-surface-snapshot-ref', 'data')],
    prevent_initial_call=True
)
def export_surface_table(
    n_clicks,
    selected_date,
    prev_selected_date,
    active_product,
    selected_expiry,
    snapshot_reference=None,
):
    if not n_clicks or not selected_date or not active_product:
        return None

    try:
        surface_data._ensure_cached_data(snapshot_reference)

        current_surface = surface_data._get_surface_snapshot(active_product, selected_date)
        previous_surface = surface_data._get_surface_snapshot(active_product, prev_selected_date) if prev_selected_date else surface_data._empty_surface_df()
        if current_surface.empty:
            return None

        current_table_df, diff_table_df, _ = _build_surface_tables(current_surface, previous_surface, selected_date)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            current_table_df.to_excel(writer, sheet_name=f'{active_product} Surface', index=False)
            if not diff_table_df.empty:
                diff_table_df.to_excel(writer, sheet_name=f'{active_product} Change', index=False)
            if str(active_product).upper() == 'TTF' and selected_expiry:
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

        selected_date_str = pd.to_datetime(selected_date).strftime('%Y%m%d')
        return dcc.send_bytes(output.getvalue(), f'{active_product.lower()}_surface_{selected_date_str}.xlsx')
    except Exception:
        return None
 
