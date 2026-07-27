"""Volatility surface dashboard page."""
import io
import threading

from dash import html, dcc, callback, Output, Input, State
import dash
import dash_ag_grid as dag
import plotly.graph_objects as go
import pandas as pd

from dataframe_utils import concat_dataframes
from db_fallback import DB_SCHEMA, read_trino_query, safe_exception_message
from runtime_config import get_database_engine


_DATA_CACHE_LOCK = threading.Lock()


UNIFIED_ATM_COLUMNS = ['cob_date', 'code', 'contract_date', 'year', 'month', 'method', 'volatility']
SURFACE_COLUMNS = [
    'cob_date',
    'code',
    'contract_date',
    'option_expiration_date',
    'delta',
    'delta_abs',
    'put_call',
    'volatility',
    'delta_bucket',
    'delta_sort_key',
    'delta_pct',
]
SURFACE_SOURCE_PRODUCTS = {'BRENT', 'HH', 'JKM', 'TTF', 'NBP'}
SURFACE_PRODUCT_DISPLAY_MAP = {'BRENT': 'Brent'}

SURFACE_POSTGRES_SOURCE_LABEL = f'{DB_SCHEMA}.implied_volatility_surface_from_prices'
SURFACE_SOURCE_LABEL = 'raw.icap.implied_volatility_surface_from_prices'
SURFACE_TRINO_SOURCES = [
    ('raw.icap.implied_volatility_surface_from_prices', 'implied_volatility_surface_from_prices'),
    ('raw.icap.implied_volatility_surface', 'implied_volatility_surface'),
]
SURFACE_POSTGRES_SOURCES = [
    (SURFACE_POSTGRES_SOURCE_LABEL, f'select * from {SURFACE_POSTGRES_SOURCE_LABEL}'),
]


def _empty_unified_atm_df():
    return pd.DataFrame(columns=UNIFIED_ATM_COLUMNS)


def _empty_surface_df():
    return pd.DataFrame(columns=SURFACE_COLUMNS)


def _source_status_template(source_name):
    return {
        'source': source_name,
        'error': None,
        'rows': 0,
        'latest_cob_date': None,
        'fallback_used': False,
    }


atm_dataset = _empty_unified_atm_df()
surface_dataset = _empty_surface_df()
_SURFACE_SNAPSHOT_CACHE = {}
_SURFACE_PIVOT_CACHE = {}
_SURFACE_SNAPSHOT_GENERATION = 0
_SURFACE_SNAPSHOT_CACHE_ATTR = '_surface_snapshot_cache_key'
DATA_CACHE_STATE = {
    'initialized': False,
    'last_refresh_token': None,
    'atm': _source_status_template(SURFACE_SOURCE_LABEL),
    'surface': _source_status_template(SURFACE_SOURCE_LABEL),
}

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


def _select_existing_column(df, candidates):
    for column in candidates:
        if column in df.columns:
            return column
    return None


def load_surface_atm_data(surface_df=None):
    surface_df = load_surface_data()[0] if surface_df is None else surface_df.copy()
    if surface_df.empty:
        return _empty_unified_atm_df()

    surface_df = surface_df.copy()
    surface_df['delta_distance'] = (surface_df['delta_abs'] - 0.5).abs()
    surface_df = surface_df.sort_values(['code', 'cob_date', 'contract_date', 'delta_distance', 'delta_sort_key'])
    surface_df = surface_df.drop_duplicates(['code', 'cob_date', 'contract_date'], keep='first')
    surface_df['year'] = surface_df['contract_date'].dt.year
    surface_df['month'] = surface_df['contract_date'].dt.month
    surface_df['method'] = 'implied_volatility_surface_atm'

    return surface_df[UNIFIED_ATM_COLUMNS]


def _normalize_put_call(value):
    if pd.isna(value):
        return None

    normalized = str(value).strip().lower()
    if normalized in ('p', 'put'):
        return 'put'
    if normalized in ('c', 'call'):
        return 'call'
    return None


def _is_atm_delta(delta_abs):
    return pd.notna(delta_abs) and abs(delta_abs - 0.5) < 1e-8


def _build_delta_bucket(row):
    delta_abs = row['delta_abs']
    put_call = row['put_call']

    if pd.isna(delta_abs):
        return pd.Series([None, None])

    if _is_atm_delta(delta_abs):
        return pd.Series(['ATM', 50.0])

    delta_pct = int(round(delta_abs * 100))

    if put_call == 'put':
        return pd.Series([f'{delta_pct}P', float(delta_pct)])

    if put_call == 'call':
        return pd.Series([f'{delta_pct}C', float(100 - delta_pct)])

    return pd.Series([f'{delta_pct}D', float(delta_pct)])


def _normalize_surface_data(surface_df):
    if surface_df.empty:
        return _empty_surface_df()

    surface_df = surface_df.copy()

    product_col = _select_existing_column(surface_df, ['product', 'commodity', 'code'])
    contract_col = _select_existing_column(surface_df, ['maturity_date', 'expiry', 'contract_date'])
    option_expiration_col = _select_existing_column(surface_df, ['option_expiration_date', 'expiration_date'])
    vol_col = _select_existing_column(surface_df, ['value', 'implied_vol', 'volatility', 'iv'])
    delta_col = _select_existing_column(surface_df, ['delta'])
    put_call_col = _select_existing_column(surface_df, ['put_call', 'option_type', 'option_side'])

    required_columns = [product_col, contract_col, vol_col, delta_col]
    if any(column is None for column in required_columns) or 'cob_date' not in surface_df.columns:
        return _empty_surface_df()

    rename_map = {
        product_col: 'code',
        contract_col: 'contract_date',
        vol_col: 'volatility',
        delta_col: 'delta',
    }
    if put_call_col is not None:
        rename_map[put_call_col] = 'put_call'
    if option_expiration_col is not None:
        rename_map[option_expiration_col] = 'option_expiration_date'

    surface_df = surface_df.rename(columns=rename_map)
    surface_df['code'] = surface_df['code'].astype(str).str.strip().str.upper()
    surface_df = surface_df[surface_df['code'].isin(SURFACE_SOURCE_PRODUCTS)]
    if surface_df.empty:
        return _empty_surface_df()
    surface_df['code'] = surface_df['code'].replace(SURFACE_PRODUCT_DISPLAY_MAP)

    surface_df['cob_date'] = pd.to_datetime(surface_df['cob_date'], errors='coerce')
    surface_df['contract_date'] = pd.to_datetime(surface_df['contract_date'], errors='coerce')
    if 'option_expiration_date' in surface_df.columns:
        surface_df['option_expiration_date'] = pd.to_datetime(surface_df['option_expiration_date'], errors='coerce')
    else:
        surface_df['option_expiration_date'] = pd.NaT
    surface_df['volatility'] = pd.to_numeric(surface_df['volatility'], errors='coerce')
    surface_df['delta'] = pd.to_numeric(surface_df['delta'], errors='coerce')

    if 'put_call' not in surface_df.columns:
        surface_df['put_call'] = None

    surface_df['put_call'] = surface_df['put_call'].apply(_normalize_put_call)
    surface_df['delta_abs'] = surface_df['delta'].abs()
    surface_df.loc[surface_df['delta_abs'] > 1, 'delta_abs'] = surface_df.loc[surface_df['delta_abs'] > 1, 'delta_abs'] / 100.0

    has_signed_delta_convention = surface_df['delta'].lt(0).any()
    if has_signed_delta_convention:
        signed_put_mask = surface_df['put_call'].isna() & (surface_df['delta'] < 0)
        signed_call_mask = surface_df['put_call'].isna() & (surface_df['delta'] > 0)
        surface_df.loc[signed_put_mask, 'put_call'] = 'put'
        surface_df.loc[signed_call_mask, 'put_call'] = 'call'

    if not surface_df['volatility'].dropna().empty and surface_df['volatility'].max() > 5:
        surface_df['volatility'] = surface_df['volatility'] / 100.0

    surface_df = surface_df.dropna(subset=['cob_date', 'contract_date', 'volatility', 'delta_abs'])
    if surface_df.empty:
        return _empty_surface_df()

    surface_df[['delta_bucket', 'delta_sort_key']] = surface_df.apply(_build_delta_bucket, axis=1)
    surface_df['delta_pct'] = surface_df['delta_abs'] * 100.0
    surface_df = surface_df.dropna(subset=['delta_bucket', 'delta_sort_key'])
    surface_df = surface_df.sort_values(['code', 'cob_date', 'contract_date', 'delta_sort_key']).reset_index(drop=True)

    return surface_df[SURFACE_COLUMNS]


def load_surface_data():
    load_errors = []

    for source_label, table_name in SURFACE_TRINO_SOURCES:
        try:
            surface_df = read_trino_query(f'select * from {table_name}', catalog='raw', schema='icap')
            normalized_surface = _normalize_surface_data(surface_df)
            if normalized_surface.empty:
                load_errors.append(f'{source_label}: no usable rows')
                continue
            return normalized_surface, {
                'source': source_label,
                'error': None,
                'fallback_used': False,
            }
        except Exception as exc:
            load_errors.append(f'{source_label}: {safe_exception_message(exc)}')

    for source_label, surface_query in SURFACE_POSTGRES_SOURCES:
        try:
            surface_df = pd.read_sql(sql=surface_query, con=get_database_engine())
            normalized_surface = _normalize_surface_data(surface_df)
            if normalized_surface.empty:
                load_errors.append(f'{source_label}: no usable rows')
                continue
            return normalized_surface, {
                'source': source_label,
                'error': None,
                'fallback_used': True,
            }
        except Exception as exc:
            load_errors.append(f'{source_label}: {safe_exception_message(exc)}')

    attempted_sources = ', '.join(
        [source for source, _ in SURFACE_TRINO_SOURCES] +
        [source for source, _ in SURFACE_POSTGRES_SOURCES]
    )
    return _empty_surface_df(), {
        'source': attempted_sources,
        'error': f'Surface load failed from all sources: {" | ".join(load_errors)}',
        'fallback_used': False,
    }


def _build_source_status(df, source_name, error_message=None, fallback_used=False):
    latest_cob_date = None
    if not df.empty and 'cob_date' in df.columns:
        cob_dates = pd.to_datetime(df['cob_date'], errors='coerce').dropna()
        if not cob_dates.empty:
            latest_cob_date = cob_dates.max()

    return {
        'source': source_name,
        'error': error_message,
        'rows': int(len(df)),
        'latest_cob_date': latest_cob_date,
        'fallback_used': fallback_used,
    }


def _refresh_cached_data(refresh_token=None, force=False):
    global atm_dataset, surface_dataset, DATA_CACHE_STATE, _SURFACE_SNAPSHOT_GENERATION

    with _DATA_CACHE_LOCK:
        should_refresh = (
            force or
            not DATA_CACHE_STATE['initialized'] or
            refresh_token != DATA_CACHE_STATE['last_refresh_token']
        )
        if not should_refresh:
            return

        loaded_surface_df = _empty_surface_df()
        atm_error = None
        surface_meta = _source_status_template(SURFACE_SOURCE_LABEL)

        try:
            loaded_surface_df, surface_loader_meta = load_surface_data()
            surface_meta.update(surface_loader_meta)
        except Exception as exc:
            loaded_surface_df = _empty_surface_df()
            surface_meta.update({
                'source': SURFACE_SOURCE_LABEL,
                'error': safe_exception_message(exc),
                'fallback_used': False,
            })

        try:
            atm_dataset = load_surface_atm_data(loaded_surface_df)
        except Exception as exc:
            atm_error = f'surface-derived ATM build failed: {exc}'
            atm_dataset = _empty_unified_atm_df()

        surface_dataset = loaded_surface_df
        _SURFACE_SNAPSHOT_CACHE.clear()
        _SURFACE_PIVOT_CACHE.clear()
        _SURFACE_SNAPSHOT_GENERATION += 1
        DATA_CACHE_STATE['atm'] = _build_source_status(
            atm_dataset,
            surface_meta['source'],
            atm_error,
            fallback_used=surface_meta.get('fallback_used', False),
        )
        DATA_CACHE_STATE['surface'] = _build_source_status(
            loaded_surface_df,
            surface_meta['source'],
            surface_meta['error'],
            fallback_used=surface_meta.get('fallback_used', False)
        )
        DATA_CACHE_STATE['initialized'] = True
        DATA_CACHE_STATE['last_refresh_token'] = refresh_token


def _ensure_cached_data(refresh_token=None):
    _refresh_cached_data(refresh_token=refresh_token, force=False)


def _get_all_available_dates():
    date_series = []
    if not atm_dataset.empty and 'cob_date' in atm_dataset.columns:
        date_series.append(pd.to_datetime(atm_dataset['cob_date'], errors='coerce'))
    if not surface_dataset.empty and 'cob_date' in surface_dataset.columns:
        date_series.append(pd.to_datetime(surface_dataset['cob_date'], errors='coerce'))

    if not date_series:
        return []

    all_dates = concat_dataframes(date_series, ignore_index=True).dropna().drop_duplicates()
    return sorted(all_dates.tolist())


def _get_supported_surface_products(selected_products, selected_date=None):
    if not selected_products:
        return []

    available_products = set(surface_dataset['code'].unique()) if not surface_dataset.empty else set()
    supported_products = [product for product in selected_products if product in available_products]

    if selected_date is None or surface_dataset.empty:
        return supported_products

    selected_date = pd.to_datetime(selected_date).normalize()
    current_products = set(
        surface_dataset.loc[
            surface_dataset['cob_date'].dt.normalize() == selected_date,
            'code',
        ].dropna().unique()
    )
    current_supported = [product for product in supported_products if product in current_products]
    other_supported = [product for product in supported_products if product not in current_products]
    return current_supported + other_supported


def _get_surface_snapshot(code, cob_date):
    if surface_dataset.empty or code is None or cob_date is None:
        return _empty_surface_df()

    cob_date = pd.to_datetime(cob_date)
    cache_key = (_SURFACE_SNAPSHOT_GENERATION, str(code), cob_date.normalize().strftime('%Y-%m-%d'))
    if cache_key in _SURFACE_SNAPSHOT_CACHE:
        snapshot = _SURFACE_SNAPSHOT_CACHE[cache_key].copy()
        snapshot.attrs[_SURFACE_SNAPSHOT_CACHE_ATTR] = cache_key
        return snapshot

    surface_df = surface_dataset

    snapshot = surface_df[
        (surface_df['code'] == code) &
        (surface_df['cob_date'].dt.normalize() == cob_date.normalize())
    ].copy()

    snapshot = snapshot.sort_values(['contract_date', 'delta_sort_key'])
    _SURFACE_SNAPSHOT_CACHE[cache_key] = snapshot
    snapshot = snapshot.copy()
    snapshot.attrs[_SURFACE_SNAPSHOT_CACHE_ATTR] = cache_key
    return snapshot


def _get_surface_delta_order(surface_df):
    if surface_df.empty:
        return pd.DataFrame(columns=['delta_bucket', 'delta_sort_key'])

    return (
        surface_df[['delta_bucket', 'delta_sort_key']]
        .drop_duplicates()
        .sort_values(['delta_sort_key', 'delta_bucket'])
    )


def _build_surface_pivot(surface_df):
    if surface_df.empty:
        return pd.DataFrame(), []

    snapshot_cache_key = surface_df.attrs.get(_SURFACE_SNAPSHOT_CACHE_ATTR)
    pivot_cache_key = None
    if snapshot_cache_key is not None:
        pivot_cache_key = (snapshot_cache_key, len(surface_df))
        cached_pivot = _SURFACE_PIVOT_CACHE.get(pivot_cache_key)
        if cached_pivot is not None:
            pivot, delta_columns = cached_pivot
            return pivot.copy(), list(delta_columns)

    delta_order = _get_surface_delta_order(surface_df)
    delta_columns = delta_order['delta_bucket'].tolist()

    pivot = surface_df.pivot_table(
        values='volatility',
        index='contract_date',
        columns='delta_bucket',
        aggfunc='first'
    )
    pivot = pivot.reindex(columns=delta_columns).sort_index()

    if pivot_cache_key is not None:
        _SURFACE_PIVOT_CACHE[pivot_cache_key] = (pivot.copy(), tuple(delta_columns))

    return pivot, delta_columns


def _get_delta_bucket_options(surface_df):
    if surface_df.empty:
        return []

    delta_order = _get_surface_delta_order(surface_df)
    return [
        {'label': bucket, 'value': bucket}
        for bucket in delta_order['delta_bucket'].tolist()
    ]


def _build_surface_dte_lookup(surface_df):
    if surface_df.empty or 'option_expiration_date' not in surface_df.columns:
        return pd.DataFrame(columns=['contract_date', 'option_expiration_date'])

    lookup = surface_df[['contract_date', 'option_expiration_date']].copy()
    lookup['contract_date'] = pd.to_datetime(lookup['contract_date'], errors='coerce')
    lookup['option_expiration_date'] = pd.to_datetime(lookup['option_expiration_date'], errors='coerce')
    lookup = lookup.dropna(subset=['contract_date', 'option_expiration_date'])
    if lookup.empty:
        return pd.DataFrame(columns=['contract_date', 'option_expiration_date'])

    return (
        lookup
        .drop_duplicates(['contract_date', 'option_expiration_date'])
        .sort_values(['contract_date', 'option_expiration_date'])
        .drop_duplicates('contract_date', keep='first')
        .reset_index(drop=True)
    )


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
    pivot_df, delta_columns = _build_surface_pivot(current_surface)
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
        previous_pivot, _ = _build_surface_pivot(previous_surface)
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
        .sort_values(['delta_sort_key', 'delta_bucket'])
        .drop_duplicates('delta_bucket', keep='first')
    )
    return deduped_slice.set_index('delta_bucket')['volatility'].reindex(ordered_buckets)


def _create_smile_evolution_figure(product, selected_expiry, current_surface, previous_surface, lookback_days, end_date):
    if current_surface.empty or selected_expiry is None:
        return _empty_figure('Select an expiry with available surface data to see the smile.', f'{product} Smile Evolution')

    selected_expiry = pd.to_datetime(selected_expiry)
    current_expiry = current_surface[current_surface['contract_date'] == selected_expiry].copy()

    if current_expiry.empty:
        return _empty_figure('No current-date smile data available for the selected expiry.', f'{product} Smile Evolution')

    combined = current_expiry.copy()
    previous_expiry = pd.DataFrame()
    if not previous_surface.empty:
        previous_expiry = previous_surface[previous_surface['contract_date'] == selected_expiry].copy()
        if not previous_expiry.empty:
            combined = concat_dataframes([combined, previous_expiry], ignore_index=True)

    ordered_buckets, x_values, xaxis = _get_smile_axis_config(combined)

    current_date = pd.to_datetime(current_surface['cob_date'].iloc[0]).normalize()
    end_date = pd.to_datetime(end_date).normalize()
    lookback_days = int(lookback_days) if lookback_days is not None else 30
    start_date = end_date - pd.Timedelta(days=lookback_days)

    history_df = surface_dataset.copy()
    if not history_df.empty:
        history_df = history_df[
            (history_df['code'] == product) &
            (history_df['contract_date'] == selected_expiry) &
            (history_df['cob_date'].dt.normalize() >= start_date) &
            (history_df['cob_date'].dt.normalize() <= end_date)
        ].copy()

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


def _get_selected_tenor_rank(product, selected_expiry, end_date):
    current_surface = _get_surface_snapshot(product, end_date)
    if current_surface.empty:
        return 0

    expiries = sorted(pd.to_datetime(current_surface['contract_date']).drop_duplicates())
    if not expiries:
        return 0

    selected_expiry = pd.to_datetime(selected_expiry)
    return expiries.index(selected_expiry) if selected_expiry in expiries else 0


def _select_rolling_tenor_history(history_df, tenor_rank):
    if history_df.empty:
        return history_df

    selected_rows = []
    for cob_date, cob_slice in history_df.groupby('cob_date'):
        expiries = sorted(pd.to_datetime(cob_slice['contract_date']).drop_duplicates())
        if not expiries:
            continue
        selected_expiry = expiries[min(tenor_rank, len(expiries) - 1)]
        selected_rows.append(cob_slice[cob_slice['contract_date'] == selected_expiry])

    if not selected_rows:
        return history_df.iloc[0:0].copy()
    return concat_dataframes(selected_rows, ignore_index=True)


def _create_delta_history_figure(product, selected_expiry, selected_buckets, lookback_days, end_date, history_mode):
    if product is None or selected_expiry is None or not selected_buckets or end_date is None:
        return _empty_figure('Select a product, expiry, and delta bucket to view history.', 'Delta Vol History')

    end_date = pd.to_datetime(end_date)
    selected_expiry = pd.to_datetime(selected_expiry)
    lookback_days = int(lookback_days) if lookback_days is not None else 30
    start_date = end_date - pd.Timedelta(days=lookback_days)

    history_df = surface_dataset.copy()
    if history_df.empty:
        return _empty_figure('No surface history available.', 'Delta Vol History')

    history_df = history_df[
        (history_df['code'] == product) &
        (history_df['delta_bucket'].isin(selected_buckets)) &
        (history_df['cob_date'] >= start_date) &
        (history_df['cob_date'] <= end_date)
    ].copy()

    if history_mode == 'fixed_expiry':
        history_df = history_df[history_df['contract_date'] == selected_expiry].copy()
    else:
        tenor_rank = _get_selected_tenor_rank(product, selected_expiry, end_date)
        history_df = _select_rolling_tenor_history(history_df, tenor_rank)

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
    current_pivot, current_columns = _build_surface_pivot(current_surface)
    previous_pivot, previous_columns = _build_surface_pivot(previous_surface)
    dte_lookup = _build_surface_dte_lookup(
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
    # Download component for table export
    dcc.Download(id="download-volatility-table"),
    dcc.Download(id="download-surface-table"),

    _build_vol_surface_filter_bar(),
    html.Div(id='options-refresh-message', className='volatility-refresh-message'),

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


def group_data_by_period(data, grouping_mode):
    data = data.copy()
    data['contract_date'] = pd.to_datetime(data['contract_date'])

    if grouping_mode == 'monthly':
        data['period'] = data['contract_date'].dt.strftime('%m-%y')
        return data

    if grouping_mode == 'quarterly':
        data['quarter'] = data['contract_date'].dt.quarter
        data['year'] = data['contract_date'].dt.year
        data['period'] = data.apply(lambda row: f"{row['year']}-Q{row['quarter']}", axis=1)
        return data.groupby(['code', 'cob_date', 'period']).agg({'volatility': 'mean'}).reset_index()

    if grouping_mode == 'season':
        def get_season(date):
            if 5 <= date.month <= 9:
                return f'{date.year}-Summer'
            return f'{date.year}-Winter'

        data['period'] = data['contract_date'].apply(get_season)
        return data.groupby(['code', 'cob_date', 'period']).agg({'volatility': 'mean'}).reset_index()

    if grouping_mode == 'calendar':
        data['period'] = data['contract_date'].dt.year.astype(str)
        return data.groupby(['code', 'cob_date', 'period']).agg({'volatility': 'mean'}).reset_index()

    data['period'] = data['contract_date'].dt.strftime('%m-%y')
    return data


def _sort_grouped_period_columns(date_cols, grouping_mode):
    if grouping_mode == 'monthly':
        def month_year_to_date(month_year):
            try:
                month, year = month_year.split('-')
                return pd.to_datetime(f'20{year}-{month}-01')
            except Exception:
                return pd.to_datetime('2100-01-01')

        return sorted(date_cols, key=month_year_to_date)

    if grouping_mode == 'quarterly':
        def quarter_key(value):
            try:
                year, quarter = value.split('-')
                return int(year), int(quarter[1])
            except Exception:
                return 9999, 0

        return sorted(date_cols, key=quarter_key)

    if grouping_mode == 'season':
        def season_key(value):
            try:
                year, season = value.split('-')
                return int(year), 0 if season == 'Winter' else 1
            except Exception:
                return 9999, 0

        return sorted(date_cols, key=season_key)

    if grouping_mode == 'calendar':
        return sorted(date_cols, key=lambda value: int(value) if str(value).isdigit() else 9999)

    return list(date_cols)


def _build_atm_table_frames(selected_date, prev_selected_date, selected_products, grouping_mode):
    if selected_date is None or not selected_products or atm_dataset.empty:
        return pd.DataFrame(columns=['product']), pd.DataFrame(columns=['product']), []

    selected_date = pd.to_datetime(selected_date)
    prev_date = pd.to_datetime(prev_selected_date) if prev_selected_date else None

    atm_df = atm_dataset.copy()
    atm_df['cob_date'] = pd.to_datetime(atm_df['cob_date'], errors='coerce')
    atm_df['contract_date'] = pd.to_datetime(atm_df['contract_date'], errors='coerce')

    product_df = atm_df[atm_df['code'].isin(selected_products)].copy()
    if product_df.empty:
        return pd.DataFrame(columns=['product']), pd.DataFrame(columns=['product']), []

    current_data = product_df[product_df['cob_date'].dt.normalize() == selected_date.normalize()].copy()
    current_pivot = pd.DataFrame(columns=['product'])
    sorted_date_cols = []

    if not current_data.empty:
        current_grouped = group_data_by_period(current_data, grouping_mode)
        current_grouped['product'] = current_grouped['code']
        current_pivot = current_grouped.pivot_table(
            values='volatility',
            index='product',
            columns='period',
            aggfunc='first'
        ).reset_index()

        date_cols = [column for column in current_pivot.columns if column != 'product']
        sorted_date_cols = _sort_grouped_period_columns(date_cols, grouping_mode)
        current_pivot = current_pivot[['product'] + sorted_date_cols]

    changes_pivot = pd.DataFrame(columns=['product'])
    if prev_date is not None and not current_data.empty:
        prev_data = product_df[product_df['cob_date'].dt.normalize() == prev_date.normalize()].copy()
        if not prev_data.empty:
            prev_grouped = group_data_by_period(prev_data, grouping_mode)
            prev_grouped['product'] = prev_grouped['code']
            prev_pivot = prev_grouped.pivot_table(
                values='volatility',
                index='product',
                columns='period',
                aggfunc='first'
            )

            current_indexed = current_pivot.set_index('product') if not current_pivot.empty else pd.DataFrame()
            all_products = sorted(set(current_indexed.index.tolist()) | set(prev_pivot.index.tolist()))
            if all_products:
                current_aligned = current_indexed.reindex(all_products)
                prev_aligned = prev_pivot.reindex(all_products).reindex(columns=sorted_date_cols)
                changes_pivot = (current_aligned[sorted_date_cols] - prev_aligned).reset_index().rename(columns={'index': 'product'})

    return current_pivot, changes_pivot, sorted_date_cols


def _build_atm_status_line(selected_date, selected_products, grouping_mode):
    atm_error = DATA_CACHE_STATE['atm']['error']
    surface_error = DATA_CACHE_STATE['surface']['error']
    surface_source = DATA_CACHE_STATE['surface']['source']
    selected_products = selected_products or []

    if atm_dataset.empty and (atm_error or surface_error):
        return _build_message_span(f'ATM source unavailable: {atm_error or surface_error}', tone='error')

    current_pivot, _, sorted_date_cols = _build_atm_table_frames(selected_date, None, selected_products, grouping_mode)
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
    surface_error = DATA_CACHE_STATE['surface']['error']
    surface_source = DATA_CACHE_STATE['surface']['source']
    if surface_error and surface_dataset.empty:
        return _build_message_span(f'Surface source unavailable: {surface_error}', tone='error')

    if not active_product or selected_date is None:
        return _build_message_span(
            f'Source: {surface_source}. Select Brent, HH, TTF, JKM, or NBP to inspect the full volatility surface.',
            tone='success'
        )

    if current_surface is None:
        current_surface = _get_surface_snapshot(active_product, selected_date)
    if previous_surface is None:
        previous_surface = _get_surface_snapshot(active_product, prev_selected_date) if prev_selected_date else _empty_surface_df()

    if current_surface.empty:
        selected_date_text = pd.to_datetime(selected_date).strftime('%Y-%m-%d')
        return _build_message_span(
            f'Source: {surface_source}. No {active_product} surface is available on {selected_date_text}.',
            tone='warning'
        )

    pivot_df, delta_columns = _build_surface_pivot(current_surface)
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


@callback(
    Output('options-refresh-message', 'children'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=False
)
def refresh_data_feedback(n_clicks):
    refresh_token = n_clicks or 0
    _ensure_cached_data(refresh_token)

    if n_clicks is None:
        return ''

    errors = [status['error'] for status in DATA_CACHE_STATE.values() if isinstance(status, dict) and status.get('error')]
    if errors:
        return _build_message_span(f'Data refresh completed with warnings: {" | ".join(errors)}', tone='warning')

    surface_source = DATA_CACHE_STATE['surface']['source']
    return _build_message_span(
        f'Data refreshed from {surface_source}.',
        tone='success'
    )


@callback(
    [Output('table-date-picker', 'date'),
     Output('table-prev-date-picker', 'date')],
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=False
)
def init_date_pickers(n_clicks):
    _ensure_cached_data(n_clicks or 0)
    all_dates = _get_all_available_dates()
    if not all_dates:
        return None, None

    latest_date = pd.Timestamp(all_dates[-1])
    prev_date = pd.Timestamp(all_dates[-2]) if len(all_dates) > 1 else latest_date
    return latest_date.strftime('%Y-%m-%d'), prev_date.strftime('%Y-%m-%d')


@callback(
    Output('table-prev-date-picker', 'date', allow_duplicate=True),
    [Input('refresh-options-data', 'n_clicks'),
     State('table-date-picker', 'date')],
    prevent_initial_call=True
)
def set_prev_date(n_clicks, current_date):
    if current_date is None:
        raise dash.exceptions.PreventUpdate

    _ensure_cached_data(n_clicks or 0)
    current_date_dt = pd.to_datetime(current_date)
    all_dates = _get_all_available_dates()

    for date in reversed(all_dates):
        if pd.Timestamp(date) < current_date_dt:
            return pd.Timestamp(date).strftime('%Y-%m-%d')

    return current_date


@callback(
    [Output('table-product-dropdown', 'options'),
     Output('table-product-dropdown', 'value')],
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=False
)
def init_products(n_clicks):
    _ensure_cached_data(n_clicks or 0)
    product_codes = sorted(atm_dataset['code'].unique()) if not atm_dataset.empty else []
    dropdown_options = [{'label': code, 'value': code} for code in product_codes]
    return dropdown_options, product_codes


@callback(
    Output('atm-status-line', 'children'),
    [Input('refresh-options-data', 'n_clicks'),
     Input('table-date-picker', 'date'),
     Input('table-product-dropdown', 'value'),
     Input('table-grouping-dropdown', 'value')],
    prevent_initial_call=False
)
def update_atm_status_line(n_clicks, selected_date, selected_products, grouping_mode):
    _ensure_cached_data(n_clicks or 0)
    return _build_atm_status_line(selected_date, selected_products, grouping_mode or 'monthly')


@callback(
    Output('graphs-container', 'children'),
    [Input('refresh-options-data', 'n_clicks'),
     Input('table-date-picker', 'date'),
     Input('table-product-dropdown', 'value'),
     Input('table-grouping-dropdown', 'value')],
    prevent_initial_call=False
)
def update_graphs(n_clicks, selected_date, selected_products, grouping_mode):
    _ensure_cached_data(n_clicks or 0)

    if selected_date is None or not selected_products or atm_dataset.empty:
        return html.Div()

    selected_date = pd.to_datetime(selected_date)
    atm_df = atm_dataset.copy()
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
                grouped_data = group_data_by_period(cob_data, grouping_mode).sort_values('period')
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
                        grouped_data = group_data_by_period(cob_data, grouping_mode).sort_values('period')
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


@callback(
    Output('tables-container', 'children'),
    [Input('refresh-options-data', 'n_clicks'),
     Input('table-date-picker', 'date'),
     Input('table-prev-date-picker', 'date'),
     Input('table-product-dropdown', 'value'),
     Input('table-grouping-dropdown', 'value')],
    prevent_initial_call=False
)
def update_tables(n_clicks, selected_date, prev_selected_date, selected_products, grouping_mode):
    _ensure_cached_data(n_clicks or 0)
    if selected_date is None or not selected_products:
        return html.Div()

    current_pivot, changes_pivot, sorted_date_cols = _build_atm_table_frames(selected_date, prev_selected_date, selected_products, grouping_mode)

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
    [Output('surface-product-tabs', 'children'),
     Output('surface-product-tabs', 'value'),
    Output('surface-content-wrapper', 'style'),
     Output('surface-empty-message', 'children')],
    [Input('refresh-options-data', 'n_clicks'),
     Input('table-product-dropdown', 'value'),
     Input('table-date-picker', 'date')],
    State('surface-product-tabs', 'value'),
    prevent_initial_call=False
)
def update_surface_tabs(n_clicks, selected_products, selected_date, active_tab):
    _ensure_cached_data(n_clicks or 0)
    supported_products = _get_supported_surface_products(selected_products, selected_date)

    if not supported_products:
        return [], None, {'display': 'none'}, 'Full surface data is available only for Brent, HH, TTF, JKM, and NBP.'

    tabs = [dcc.Tab(label=product, value=product) for product in supported_products]
    current_date_products = supported_products
    if selected_date is not None and not surface_dataset.empty:
        selected_date_dt = pd.to_datetime(selected_date).normalize()
        available_on_date = set(
            surface_dataset.loc[
                surface_dataset['cob_date'].dt.normalize() == selected_date_dt,
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
    [Input('refresh-options-data', 'n_clicks'),
     Input('table-date-picker', 'date'),
     Input('surface-product-tabs', 'value')],
    State('surface-expiry-dropdown', 'value'),
    prevent_initial_call=False
)
def update_surface_expiry_dropdown(n_clicks, selected_date, active_product, current_expiry):
    _ensure_cached_data(n_clicks or 0)
    hidden_style = {'display': 'none', 'align-items': 'center', 'gap': '16px', 'margin': '16px 0', 'flex-wrap': 'wrap'}
    visible_style = {'display': 'flex', 'align-items': 'center', 'gap': '16px', 'margin': '16px 0', 'flex-wrap': 'wrap'}

    if selected_date is None or not active_product:
        return [], None, hidden_style

    current_surface = _get_surface_snapshot(active_product, selected_date)
    if current_surface.empty:
        return [], None, hidden_style

    expiries = sorted(pd.to_datetime(current_surface['contract_date']).drop_duplicates())
    expiry_options = [{'label': expiry.strftime('%Y-%m'), 'value': expiry.strftime('%Y-%m-%d')} for expiry in expiries]
    valid_expiries = {option['value'] for option in expiry_options}
    selected_expiry = current_expiry if current_expiry in valid_expiries else expiry_options[0]['value']
    return expiry_options, selected_expiry, visible_style


@callback(
    [Output('surface-history-buckets-dropdown', 'options'),
     Output('surface-history-buckets-dropdown', 'value')],
    [Input('refresh-options-data', 'n_clicks'),
     Input('table-date-picker', 'date'),
     Input('surface-product-tabs', 'value'),
     Input('surface-expiry-dropdown', 'value')],
    State('surface-history-buckets-dropdown', 'value'),
    prevent_initial_call=False
)
def update_surface_delta_dropdown(n_clicks, selected_date, active_product, selected_expiry, current_history_buckets):
    _ensure_cached_data(n_clicks or 0)
    if selected_date is None or not active_product or not selected_expiry:
        return [], []

    current_surface = _get_surface_snapshot(active_product, selected_date)
    if current_surface.empty:
        return [], []

    expiry_surface = current_surface[current_surface['contract_date'] == pd.to_datetime(selected_expiry)].copy()
    delta_options = _get_delta_bucket_options(expiry_surface)
    valid_deltas = {option['value'] for option in delta_options}
    history_buckets = [bucket for bucket in (current_history_buckets or []) if bucket in valid_deltas]
    preferred_delta = 'ATM' if 'ATM' in valid_deltas else (delta_options[0]['value'] if delta_options else None)
    if not history_buckets and preferred_delta:
        history_buckets = [preferred_delta]
    history_buckets = history_buckets[:3]

    return delta_options, history_buckets


@callback(
    [Output('surface-heatmap-graph', 'figure'),
     Output('surface-smile-graph', 'figure'),
     Output('surface-history-graph', 'figure'),
     Output('surface-table-container', 'children'),
     Output('surface-status-line', 'children')],
    [Input('refresh-options-data', 'n_clicks'),
     Input('table-date-picker', 'date'),
     Input('table-prev-date-picker', 'date'),
     Input('surface-product-tabs', 'value'),
     Input('surface-expiry-dropdown', 'value'),
     Input('surface-history-buckets-dropdown', 'value'),
     Input('surface-lookback-dropdown', 'value'),
     Input('surface-heatmap-mode-dropdown', 'value'),
     Input('surface-history-mode-dropdown', 'value')],
    prevent_initial_call=False
)
def update_surface_section(
    n_clicks,
    selected_date,
    prev_selected_date,
    active_product,
    selected_expiry,
    selected_history_buckets,
    lookback_days,
    heatmap_mode,
    history_mode
):
    _ensure_cached_data(n_clicks or 0)

    if selected_date is None or not active_product:
        status_line = _build_surface_status_line(selected_date, prev_selected_date, active_product)
        empty_heatmap = _empty_figure('Select a supported product to view the surface.', 'Surface Heatmap')
        empty_smile = _empty_figure('Select a supported product to view smile details.', 'Smile Evolution')
        empty_history = _empty_figure('Select a supported product, expiry, and delta bucket to view history.', 'Delta Vol History')
        return empty_heatmap, empty_smile, empty_history, html.Div(), status_line

    current_surface = _get_surface_snapshot(active_product, selected_date)
    previous_surface = _get_surface_snapshot(active_product, prev_selected_date) if prev_selected_date else _empty_surface_df()
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
        selected_expiry = pd.to_datetime(current_surface['contract_date'].min()).strftime('%Y-%m-%d')

    expiry_surface = current_surface[current_surface['contract_date'] == pd.to_datetime(selected_expiry)].copy()
    delta_options = _get_delta_bucket_options(expiry_surface)
    valid_deltas = {option['value'] for option in delta_options}
    selected_history_buckets = [bucket for bucket in (selected_history_buckets or []) if bucket in valid_deltas]
    preferred_bucket = 'ATM' if 'ATM' in valid_deltas else (delta_options[0]['value'] if delta_options else None)
    if not selected_history_buckets and preferred_bucket:
        selected_history_buckets = [preferred_bucket]
    selected_history_buckets = selected_history_buckets[:3]

    heatmap_figure = _create_surface_heatmap_figure(active_product, current_surface, previous_surface, heatmap_mode or 'vs_atm')
    smile_figure = _create_smile_evolution_figure(active_product, selected_expiry, current_surface, previous_surface, lookback_days, selected_date)
    history_figure = _create_delta_history_figure(active_product, selected_expiry, selected_history_buckets, lookback_days, selected_date, history_mode or 'fixed_expiry')
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
    Output('table-grouping-dropdown', 'value'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=True
)
def reset_grouping(n_clicks):
    return 'monthly'


@callback(
    Output("download-volatility-table", "data"),
    Input("export-volatility-table-btn", "n_clicks"),
    [State('table-date-picker', 'date'),
     State('table-product-dropdown', 'value'),
     State('table-grouping-dropdown', 'value')],
    prevent_initial_call=True
)
def export_volatility_table(n_clicks, selected_date, selected_products, grouping_mode):
    if not n_clicks or not selected_date or not selected_products:
        return None

    try:
        _ensure_cached_data()

        current_pivot, _, sorted_date_cols = _build_atm_table_frames(selected_date, None, selected_products, grouping_mode)
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
     State('surface-product-tabs', 'value')],
    prevent_initial_call=True
)
def export_surface_table(n_clicks, selected_date, prev_selected_date, active_product):
    if not n_clicks or not selected_date or not active_product:
        return None

    try:
        _ensure_cached_data()

        current_surface = _get_surface_snapshot(active_product, selected_date)
        previous_surface = _get_surface_snapshot(active_product, prev_selected_date) if prev_selected_date else _empty_surface_df()
        if current_surface.empty:
            return None

        current_table_df, diff_table_df, _ = _build_surface_tables(current_surface, previous_surface, selected_date)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            current_table_df.to_excel(writer, sheet_name=f'{active_product} Surface', index=False)
            if not diff_table_df.empty:
                diff_table_df.to_excel(writer, sheet_name=f'{active_product} Change', index=False)
        output.seek(0)

        selected_date_str = pd.to_datetime(selected_date).strftime('%Y%m%d')
        return dcc.send_bytes(output.getvalue(), f'{active_product.lower()}_surface_{selected_date_str}.xlsx')
    except Exception:
        return None
 
