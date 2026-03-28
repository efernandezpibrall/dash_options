# pages/greeks.py
import configparser
from dash import html, dcc, dash_table, callback, Output, Input, State, Dash, html, dcc, ALL
import dash
import plotly.graph_objects as go
from dash import Dash, html, dcc, dash_table, Input, Output
import pandas as pd
from sqlalchemy import create_engine
import os

UNIFIED_ATM_COLUMNS = ['cob_date', 'code', 'contract_date', 'year', 'month', 'method', 'volatility']
SURFACE_COLUMNS = [
    'cob_date',
    'code',
    'contract_date',
    'delta',
    'delta_abs',
    'put_call',
    'volatility',
    'delta_bucket',
    'delta_sort_key',
    'delta_pct',
]
SURFACE_SOURCE_PRODUCTS = {'JKM', 'TTF', 'NBP'}
LEGACY_ATM_CODES_REPLACED_BY_SURFACE = {'TTF', 'TFM', 'JKM', 'JKM_benchmark', 'NBP'}

#------ code to be able to access config.ini, even having the path in the .virtualenvs is not working without it ------#
try:
    # Get the directory where your script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate to the directory containing config.ini
    # Adjust the number of '..' as needed to reach the correct directory
    config_dir = os.path.abspath(os.path.join(script_dir, '..','..'))  # Go up one level
    CONFIG_FILE_PATH = os.path.join(config_dir, 'config.ini')
except:
    CONFIG_FILE_PATH = 'config.ini'  # Assumes it's in the same directory or the path it is detected

# --- Load Configuration from INI File ---
config_reader = configparser.ConfigParser(interpolation=None)
config_reader.read(CONFIG_FILE_PATH)

# Read values from the ini file sections
DB_CONNECTION_STRING = config_reader.get('DATABASE', 'CONNECTION_STRING', fallback=None)
DB_SCHEMA = config_reader.get('DATABASE', 'SCHEMA', fallback='at_lng')
TRINOS_HOST = config_reader.get('TRINOS', 'HOST', fallback=None)
TRINOS_USERNAME = config_reader.get('TRINOS', 'USERNAME', fallback=None)
TRINOS_TOKEN = config_reader.get('TRINOS', 'TOKEN', fallback=None)
TRINOS_PORT = config_reader.get('TRINOS', 'PORT', fallback=None)


# --- Essential Variable Checks ---
if not DB_CONNECTION_STRING:
    raise ValueError(f"Missing DATABASE CONNECTION_STRING in {CONFIG_FILE_PATH}")

# create engine
engine = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True)
ATM_SOURCE_LABEL = f'{DB_SCHEMA}.options_atm_vol'
POSTGRES_SURFACE_SOURCE_LABEL = f'{DB_SCHEMA}.implied_volatility_surface'
TRINO_SURFACE_SOURCE_LABEL = 'raw.icap.implied_volatility_surface'
SURFACE_SOURCE_LABEL = TRINO_SURFACE_SOURCE_LABEL


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
DATA_CACHE_STATE = {
    'initialized': False,
    'last_refresh_token': None,
    'atm': _source_status_template(ATM_SOURCE_LABEL),
    'surface': _source_status_template(SURFACE_SOURCE_LABEL),
}


def _select_existing_column(df, candidates):
    for column in candidates:
        if column in df.columns:
            return column
    return None


def read_table_conn(conn, query):
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        field_names = [field[0] for field in cursor.description] if cursor.description else []
        return pd.DataFrame(rows, columns=field_names)
    finally:
        cursor.close()


def read_trino_query(query, catalog='raw', schema='icap'):
    try:
        from trino.dbapi import connect
        from trino.auth import JWTAuthentication
    except ImportError as exc:
        raise RuntimeError('trino package is not installed') from exc

    if not TRINOS_HOST or not TRINOS_USERNAME or not TRINOS_TOKEN:
        raise ValueError('TRINOS credentials are missing in config.ini')

    port = int(TRINOS_PORT) if TRINOS_PORT else 443
    conn = connect(
        host=TRINOS_HOST,
        port=port,
        user=TRINOS_USERNAME,
        auth=JWTAuthentication(TRINOS_TOKEN),
        http_scheme='https',
        verify=False,
        catalog=catalog,
        schema=schema,
    )

    try:
        return read_table_conn(conn, query)
    finally:
        conn.close()


def load_options_atm_data():
    query = f'''select * from {DB_SCHEMA}.options_atm_vol'''
    atm_df = pd.read_sql(sql=query, con=engine)

    if atm_df.empty:
        return _empty_unified_atm_df()

    atm_df = atm_df.copy()
    atm_df['cob_date'] = pd.to_datetime(atm_df['cob_date'], errors='coerce')
    atm_df['contract_date'] = pd.to_datetime(atm_df['contract_date'], errors='coerce')

    if 'year' not in atm_df.columns:
        atm_df['year'] = atm_df['contract_date'].dt.year
    if 'month' not in atm_df.columns:
        atm_df['month'] = atm_df['contract_date'].dt.month
    if 'method' not in atm_df.columns:
        atm_df['method'] = 'options_atm_vol'

    return atm_df[UNIFIED_ATM_COLUMNS]


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

    surface_df = surface_df.rename(columns=rename_map)
    surface_df['code'] = surface_df['code'].astype(str).str.strip().str.upper()
    surface_df = surface_df[surface_df['code'].isin(SURFACE_SOURCE_PRODUCTS)]
    if surface_df.empty:
        return _empty_surface_df()

    surface_df['cob_date'] = pd.to_datetime(surface_df['cob_date'], errors='coerce')
    surface_df['contract_date'] = pd.to_datetime(surface_df['contract_date'], errors='coerce')
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
    trino_query = 'SELECT * FROM implied_volatility_surface'
    postgres_query = f'''select * from {DB_SCHEMA}.implied_volatility_surface'''
    trino_error = None

    try:
        trino_df = read_trino_query(trino_query, catalog='raw', schema='icap')
        return _normalize_surface_data(trino_df), {
            'source': TRINO_SURFACE_SOURCE_LABEL,
            'error': None,
            'fallback_used': False,
        }
    except Exception as exc:
        trino_error = str(exc)

    try:
        postgres_df = pd.read_sql(sql=postgres_query, con=engine)
        return _normalize_surface_data(postgres_df), {
            'source': POSTGRES_SURFACE_SOURCE_LABEL,
            'error': f'Trino failed, using PostgreSQL fallback: {trino_error}',
            'fallback_used': True,
        }
    except Exception as postgres_exc:
        return _empty_surface_df(), {
            'source': POSTGRES_SURFACE_SOURCE_LABEL,
            'error': f'Both Trino and PostgreSQL surface loads failed. Trino error: {trino_error} | PostgreSQL error: {postgres_exc}',
            'fallback_used': True,
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
    global atm_dataset, surface_dataset, DATA_CACHE_STATE

    should_refresh = (
        force or
        not DATA_CACHE_STATE['initialized'] or
        refresh_token != DATA_CACHE_STATE['last_refresh_token']
    )
    if not should_refresh:
        return

    atm_source_df = _empty_unified_atm_df()
    loaded_surface_df = _empty_surface_df()
    atm_error = None
    surface_meta = _source_status_template(SURFACE_SOURCE_LABEL)

    try:
        loaded_surface_df, surface_loader_meta = load_surface_data()
        surface_meta.update(surface_loader_meta)
    except Exception as exc:
        loaded_surface_df = _empty_surface_df()
        surface_meta.update({
            'source': POSTGRES_SURFACE_SOURCE_LABEL,
            'error': str(exc),
            'fallback_used': False,
        })

    try:
        atm_source_df = load_options_atm_data()
    except Exception as exc:
        atm_error = str(exc)
        atm_source_df = _empty_unified_atm_df()

    try:
        atm_dataset = build_unified_atm_dataset(atm_source_df, loaded_surface_df)
    except Exception as exc:
        if atm_error:
            atm_error = f'{atm_error} | unified ATM build failed: {exc}'
        else:
            atm_error = f'unified ATM build failed: {exc}'
        fallback_atm_df = atm_source_df.copy()
        if not fallback_atm_df.empty:
            fallback_atm_df = fallback_atm_df[~fallback_atm_df['code'].isin(LEGACY_ATM_CODES_REPLACED_BY_SURFACE)].copy()
        atm_dataset = fallback_atm_df if not fallback_atm_df.empty else _empty_unified_atm_df()

    surface_dataset = loaded_surface_df
    DATA_CACHE_STATE['atm'] = _build_source_status(atm_source_df, ATM_SOURCE_LABEL, atm_error)
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

    all_dates = pd.concat(date_series, ignore_index=True).dropna().drop_duplicates()
    return sorted(all_dates.tolist())


def _get_supported_surface_products(selected_products):
    if not selected_products:
        return []

    available_products = set(surface_dataset['code'].unique()) if not surface_dataset.empty else set()
    return [product for product in selected_products if product in available_products]


def _get_surface_snapshot(code, cob_date):
    if surface_dataset.empty or code is None or cob_date is None:
        return _empty_surface_df()

    cob_date = pd.to_datetime(cob_date)
    surface_df = surface_dataset.copy()
    surface_df['cob_date'] = pd.to_datetime(surface_df['cob_date'], errors='coerce')
    surface_df['contract_date'] = pd.to_datetime(surface_df['contract_date'], errors='coerce')

    snapshot = surface_df[
        (surface_df['code'] == code) &
        (surface_df['cob_date'].dt.normalize() == cob_date.normalize())
    ].copy()

    return snapshot.sort_values(['contract_date', 'delta_sort_key'])


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

    delta_order = _get_surface_delta_order(surface_df)
    delta_columns = delta_order['delta_bucket'].tolist()

    pivot = surface_df.pivot_table(
        values='volatility',
        index='contract_date',
        columns='delta_bucket',
        aggfunc='first'
    )
    pivot = pivot.reindex(columns=delta_columns).sort_index()

    return pivot, delta_columns


def _get_delta_bucket_options(surface_df):
    if surface_df.empty:
        return []

    delta_order = _get_surface_delta_order(surface_df)
    return [
        {'label': bucket, 'value': bucket}
        for bucket in delta_order['delta_bucket'].tolist()
    ]


def _format_surface_table_df(pivot_df, cob_date=None):
    if pivot_df.empty:
        return pd.DataFrame(columns=['expiry', 'dte'])

    formatted = pivot_df.copy().reset_index()
    if cob_date is not None:
        cob_date = pd.to_datetime(cob_date)
        formatted['dte'] = (pd.to_datetime(formatted['contract_date']) - cob_date).dt.days
    else:
        formatted['dte'] = None
    formatted = formatted.rename(columns={'contract_date': 'expiry'})
    formatted['expiry'] = pd.to_datetime(formatted['expiry']).dt.strftime('%Y-%m')
    value_columns = [column for column in formatted.columns if column not in ['expiry', 'dte']]
    return formatted[['expiry', 'dte'] + value_columns]


def _empty_figure(message, title):
    fig = go.Figure()
    fig.update_layout(
        title=dict(text=title, x=0.5, font=dict(size=18, color='#1f2937')),
        plot_bgcolor='rgba(248, 249, 250, 0.5)',
        paper_bgcolor='white',
        margin=dict(l=60, r=40, t=70, b=60),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        annotations=[
            dict(
                text=message,
                x=0.5,
                y=0.5,
                xref='paper',
                yref='paper',
                showarrow=False,
                font=dict(size=14, color='#6b7280')
            )
        ]
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
    colorscale = 'Viridis'
    zmid = None
    hover_template = 'Expiry: %{y}<br>Bucket: %{x}<br>Vol: %{z:.4f}<extra></extra>'
    zmin, zmax = _calculate_heatmap_bounds(display_values)

    if heatmap_mode == 'vs_atm':
        if 'ATM' not in absolute_values.columns or not absolute_values['ATM'].notna().any():
            return None, None, None, None, None, 'No ATM bucket is available for the selected surface.'

        display_values = absolute_values.sub(absolute_values['ATM'], axis=0)
        title_suffix = 'Smile vs ATM'
        colorbar_title = 'Vol - ATM'
        colorscale = 'RdBu_r'
        zmid = 0.0
        zmin, zmax = _calculate_symmetric_color_range(display_values)
        hover_template = 'Expiry: %{y}<br>Bucket: %{x}<br>Vol: %{customdata:.4f}<br>vs ATM: %{z:+.4f}<extra></extra>'

    elif heatmap_mode == 'vs_previous':
        previous_pivot, _ = _build_surface_pivot(previous_surface)
        if previous_pivot.empty:
            return None, None, None, None, None, 'No previous-date surface data is available for comparison.'

        previous_aligned = previous_pivot.reindex(index=absolute_values.index, columns=delta_columns)
        display_values = absolute_values - previous_aligned
        title_suffix = 'Change vs Previous'
        colorbar_title = 'Vol Change'
        colorscale = 'RdBu_r'
        zmid = 0.0
        zmin, zmax = _calculate_symmetric_color_range(display_values)
        hover_template = 'Expiry: %{y}<br>Bucket: %{x}<br>Vol: %{customdata:.4f}<br>Change: %{z:+.4f}<extra></extra>'

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
            colorbar=dict(title=colorbar_title),
            hovertemplate=hover_template,
            xgap=1,
            ygap=1
        )
    )

    fig.update_layout(
        title=dict(text=f'{product} {title_suffix}', x=0.5, font=dict(size=18, color='#1f2937')),
        xaxis=dict(title='Delta Bucket', tickangle=0),
        yaxis=dict(title='Expiry', autorange='reversed'),
        plot_bgcolor='rgba(248, 249, 250, 0.5)',
        paper_bgcolor='white',
        margin=dict(l=70, r=60, t=80, b=70),
    )

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
            combined = pd.concat([combined, previous_expiry], ignore_index=True)

    ordered_buckets, x_values, xaxis = _get_smile_axis_config(combined)

    current_date = pd.to_datetime(current_surface['cob_date'].iloc[0]).normalize()
    end_date = pd.to_datetime(end_date).normalize()
    lookback_days = int(lookback_days) if lookback_days is not None else 30
    start_date = end_date - pd.Timedelta(days=lookback_days)

    history_df = surface_dataset.copy()
    if not history_df.empty:
        history_df['cob_date'] = pd.to_datetime(history_df['cob_date'], errors='coerce')
        history_df['contract_date'] = pd.to_datetime(history_df['contract_date'], errors='coerce')
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
        name='Current Date',
        line=dict(color='#2E86C1', width=3),
        marker=dict(size=8),
        customdata=ordered_buckets,
        hovertemplate='Bucket: %{customdata}<br>Vol: %{y:.4f}<extra></extra>',
    ))

    if not previous_expiry.empty:
        previous_series = _build_smile_series(previous_expiry, ordered_buckets)
        fig.add_trace(go.Scatter(
            x=x_values,
            y=previous_series.values,
            mode='lines+markers',
            name='Previous Date',
            line=dict(color='#6b7280', width=1.5, dash='dash'),
            marker=dict(size=7),
            customdata=ordered_buckets,
            hovertemplate='Bucket: %{customdata}<br>Vol: %{y:.4f}<extra></extra>',
        ))

    auxiliary_palette = ['rgba(15, 118, 110, 0.45)', 'rgba(37, 99, 235, 0.35)', 'rgba(124, 58, 237, 0.35)', 'rgba(234, 88, 12, 0.35)']
    for index, cob_date in enumerate(auxiliary_dates):
        date_slice = history_df[history_df['cob_date'].dt.normalize() == cob_date].copy()
        if date_slice.empty:
            continue
        date_series = _build_smile_series(date_slice, ordered_buckets)
        fig.add_trace(go.Scatter(
            x=x_values,
            y=date_series.values,
            mode='lines+markers',
            name=pd.Timestamp(cob_date).strftime('%Y-%m-%d'),
            line=dict(color=auxiliary_palette[index % len(auxiliary_palette)], width=1.25),
            marker=dict(size=5, color=auxiliary_palette[index % len(auxiliary_palette)]),
            customdata=ordered_buckets,
            hovertemplate='Bucket: %{customdata}<br>Vol: %{y:.4f}<extra></extra>',
        ))

    fig.update_layout(
        title=dict(
            text=f"{product} Smile Evolution | {selected_expiry.strftime('%Y-%m')} | Last {lookback_days}D",
            x=0.5,
            font=dict(size=18, color='#1f2937')
        ),
        xaxis=xaxis,
        yaxis=dict(title='Implied Volatility'),
        plot_bgcolor='rgba(248, 249, 250, 0.5)',
        paper_bgcolor='white',
        margin=dict(l=70, r=40, t=80, b=70),
        hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1.0),
    )

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
    return pd.concat(selected_rows, ignore_index=True)


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

    history_df['cob_date'] = pd.to_datetime(history_df['cob_date'], errors='coerce')
    history_df['contract_date'] = pd.to_datetime(history_df['contract_date'], errors='coerce')
    history_df = history_df[
        (history_df['code'] == product) &
        (history_df['delta_bucket'].isin(selected_buckets)) &
        (history_df['cob_date'] >= start_date) &
        (history_df['cob_date'] <= end_date)
    ].copy()

    if history_mode == 'fixed_expiry':
        history_df = history_df[history_df['contract_date'] == selected_expiry].copy()
        title_suffix = selected_expiry.strftime('%Y-%m')
    else:
        tenor_rank = _get_selected_tenor_rank(product, selected_expiry, end_date)
        history_df = _select_rolling_tenor_history(history_df, tenor_rank)
        title_suffix = f'Rolling Tenor #{tenor_rank + 1}'

    if history_df.empty:
        return _empty_figure(
            f'No history is available for the selected buckets over the last {lookback_days} days.',
            'Delta Vol History'
        )

    history_df = history_df.groupby(['cob_date', 'delta_bucket'], as_index=False)['volatility'].mean().sort_values(['delta_bucket', 'cob_date'])

    fig = go.Figure()
    color_palette = ['#C0392B', '#2E86C1', '#117A65', '#7D3C98', '#CA6F1E']
    for index, bucket in enumerate(selected_buckets):
        bucket_df = history_df[history_df['delta_bucket'] == bucket].copy()
        if bucket_df.empty:
            continue

        fig.add_trace(go.Scatter(
            x=bucket_df['cob_date'],
            y=bucket_df['volatility'],
            mode='lines+markers',
            name=bucket,
            line=dict(color=color_palette[index % len(color_palette)], width=2),
            marker=dict(size=7)
        ))

    fig.update_layout(
        title=dict(
            text=f'{product} Delta History | {title_suffix} | Last {lookback_days}D',
            x=0.5,
            font=dict(size=18, color='#1f2937')
        ),
        xaxis=dict(title='COB Date'),
        yaxis=dict(title='Implied Volatility'),
        plot_bgcolor='rgba(248, 249, 250, 0.5)',
        paper_bgcolor='white',
        margin=dict(l=70, r=40, t=80, b=70),
        hovermode='x unified',
        showlegend=True,
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1.0),
    )

    if not fig.data:
        return _empty_figure(
            f'No history is available for the selected buckets over the last {lookback_days} days.',
            'Delta Vol History'
        )

    return fig



def _build_surface_tables(current_surface, previous_surface, cob_date):
    current_pivot, current_columns = _build_surface_pivot(current_surface)
    previous_pivot, previous_columns = _build_surface_pivot(previous_surface)

    combined_columns = current_columns[:]
    for column in previous_columns:
        if column not in combined_columns:
            combined_columns.append(column)

    if combined_columns:
        combined_order = (
            pd.concat([
                current_surface[['delta_bucket', 'delta_sort_key']],
                previous_surface[['delta_bucket', 'delta_sort_key']]
            ], ignore_index=True)
            .drop_duplicates()
            .sort_values(['delta_sort_key', 'delta_bucket'])
        )
        combined_columns = [col for col in combined_order['delta_bucket'].tolist() if col in combined_columns]

    current_table_df = _format_surface_table_df(current_pivot.reindex(columns=combined_columns), cob_date=cob_date)

    diff_table_df = pd.DataFrame()
    if not current_pivot.empty and not previous_pivot.empty:
        all_expiries = current_pivot.index.union(previous_pivot.index)
        diff_pivot = current_pivot.reindex(index=all_expiries, columns=combined_columns) - previous_pivot.reindex(index=all_expiries, columns=combined_columns)
        diff_table_df = _format_surface_table_df(diff_pivot, cob_date=cob_date)

    return current_table_df, diff_table_df, combined_columns


def _create_surface_datatable(dataframe, delta_columns, table_id, include_diff_colors=False):
    columns = [
        {'name': 'Expiry', 'id': 'expiry'},
        {'name': 'DTE', 'id': 'dte', 'type': 'numeric'}
    ]
    for column in delta_columns:
        columns.append({'name': column, 'id': column, 'type': 'numeric', 'format': {'specifier': '.4f'}})

    style_data_conditional = [
        {
            'if': {'row_index': 'odd'},
            'backgroundColor': 'rgb(248, 248, 248)'
        }
    ]

    for column in delta_columns:
        style_data_conditional.append({
            'if': {'column_id': column, 'filter_query': f'{{{column}}} is nil'},
            'backgroundColor': '#E5E7EB',
            'color': '#9CA3AF',
            'fontStyle': 'italic'
        })

    if include_diff_colors:
        for column in delta_columns:
            style_data_conditional.extend([
                {
                    'if': {'column_id': column, 'filter_query': f'{{{column}}} > 0'},
                    'color': 'green',
                    'fontWeight': 'bold'
                },
                {
                    'if': {'column_id': column, 'filter_query': f'{{{column}}} < 0'},
                    'color': 'red',
                    'fontWeight': 'bold'
                }
            ])

    return dash_table.DataTable(
        id=table_id,
        columns=columns,
        data=dataframe.replace({float('nan'): None}).to_dict('records'),
        style_table={'overflowX': 'auto'},
        style_cell={
            'textAlign': 'center',
            'padding': '4px',
            'fontSize': 12,
            'minWidth': '55px',
            'maxWidth': '110px',
            'color': 'black'
        },
        style_cell_conditional=[
            {
                'if': {'column_id': 'expiry'},
                'textAlign': 'left',
                'fontWeight': 'bold',
                'width': '90px',
                'color': 'black'
            },
            {
                'if': {'column_id': 'dte'},
                'width': '55px',
                'fontWeight': 'bold',
                'color': '#374151'
            }
        ],
        style_header={
            'backgroundColor': 'rgb(230, 230, 230)',
            'fontWeight': 'bold',
            'textAlign': 'center',
            'padding': '4px',
            'fontSize': 12,
            'whiteSpace': 'normal',
            'height': 'auto',
            'color': 'black'
        },
        style_data_conditional=style_data_conditional,
        fixed_rows={'headers': True},
        page_action='none',
        tooltip_delay=0,
        tooltip_duration=None
    )


def build_unified_atm_dataset(atm_df=None, surface_df=None):
    atm_df = load_options_atm_data() if atm_df is None else atm_df.copy()
    surface_atm_df = load_surface_atm_data(surface_df=surface_df)

    if atm_df.empty:
        return surface_atm_df
    if surface_atm_df.empty:
        return atm_df[~atm_df['code'].isin(LEGACY_ATM_CODES_REPLACED_BY_SURFACE)].copy()

    non_surface_atm_df = atm_df[~atm_df['code'].isin(LEGACY_ATM_CODES_REPLACED_BY_SURFACE)].copy()
    unified_df = pd.concat([non_surface_atm_df[UNIFIED_ATM_COLUMNS], surface_atm_df[UNIFIED_ATM_COLUMNS]], ignore_index=True)
    unified_df['cob_date'] = pd.to_datetime(unified_df['cob_date'], errors='coerce')
    unified_df['contract_date'] = pd.to_datetime(unified_df['contract_date'], errors='coerce')
    unified_df['volatility'] = pd.to_numeric(unified_df['volatility'], errors='coerce')
    unified_df = unified_df.dropna(subset=['cob_date', 'contract_date', 'volatility', 'code'])
    unified_df = unified_df.sort_values(['code', 'cob_date', 'contract_date']).reset_index(drop=True)

    return unified_df[UNIFIED_ATM_COLUMNS]

layout = html.Div([
    # Download component for table export
    dcc.Download(id="download-volatility-table"),
    dcc.Download(id="download-surface-table"),
    
    # Top section with controls following greeks.py pattern
    html.Div([
        # Controls section - matching greeks.py inline layout
        html.Div([
            # First row - Main controls
            html.Div([
                html.Label('Products:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='table-product-dropdown',
                    options=[],  # Will be populated in callback
                    value=[],
                    multi=True,
                    placeholder='Select products...',
                    className="inline-dropdown-multi"
                ),
                html.Label('Current Date:', className="inline-filter-label"),
                dcc.DatePickerSingle(
                    id='table-date-picker',
                    display_format='YYYY-MM-DD',
                    with_portal=True,
                    className="inline-date-picker"
                ),
                html.Label('Previous Date:', className="inline-filter-label"),
                dcc.DatePickerSingle(
                    id='table-prev-date-picker',
                    display_format='YYYY-MM-DD',
                    with_portal=True,
                    className="inline-date-picker"
                )
            ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap', 'margin-bottom': '8px'}),
            
            # Second row - Grouping only
            html.Div([
                html.Label('Group By:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='table-grouping-dropdown',
                    options=[
                        {'label': 'Monthly', 'value': 'monthly'},
                        {'label': 'Quarterly', 'value': 'quarterly'},
                        {'label': 'Season', 'value': 'season'},
                        {'label': 'Calendar', 'value': 'calendar'}
                    ],
                    value='monthly',  # Default selection
                    clearable=False,
                    className="inline-dropdown-aggregation"
                )
            ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap'})
        ], className="inline-section-header")
    ], style={'margin-bottom': '8px'}),
    html.Div(id='options-refresh-message', style={'margin-bottom': '16px'}),

    # Charts section header and content
    html.H3('ATM Volatility', className="greeks-title"),
    html.Div(
        id='atm-status-line',
        style={'margin-bottom': '12px', 'padding': '8px 10px', 'backgroundColor': '#F3F4F6', 'borderRadius': '4px', 'fontSize': '12px', 'color': '#4B5563'}
    ),
    html.Div(
        id='graphs-container',
        style={'margin-bottom': '24px'}
    ),
    
    # Tables section
    html.Div(
        id='tables-container',
        style={'width': '100%'}
    ),

    # Full volatility surface section
    html.Div([
        html.Div([
            html.H3('Volatility Surface', className="greeks-title", style={'margin': '0', 'display': 'inline-block'}),
            html.Button(
                'Export Surface',
                id='export-surface-table-btn',
                className='custom-export-btn',
                style={
                    'margin-left': '12px',
                    'font-size': '11px',
                    'padding': '4px 8px',
                    'background-color': '#117A65',
                    'color': 'white',
                    'border': 'none',
                    'border-radius': '3px',
                    'cursor': 'pointer',
                    'font-weight': '500'
                }
            )
        ], style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '12px'}),
        html.Div(
            id='surface-status-line',
            style={'margin-bottom': '12px', 'padding': '8px 10px', 'backgroundColor': '#ECFDF5', 'borderRadius': '4px', 'fontSize': '12px', 'color': '#065F46'}
        ),
        html.Div(
            id='surface-empty-message',
            style={'margin-bottom': '12px', 'color': '#6b7280'}
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
                dcc.Graph(
                    id='surface-heatmap-graph',
                    config={'displayModeBar': True, 'responsive': True, 'modeBarButtonsToRemove': ['lasso2d', 'select2d']},
                    style={'height': '450px'}
                ),
                dcc.Graph(
                    id='surface-smile-graph',
                    config={'displayModeBar': True, 'responsive': True, 'modeBarButtonsToRemove': ['lasso2d', 'select2d']},
                    style={'height': '450px'}
                )
            ], style={'display': 'grid', 'gridTemplateColumns': '1fr 1fr', 'gap': '16px', 'marginBottom': '20px'}),
            dcc.Graph(
                id='surface-history-graph',
                config={'displayModeBar': True, 'responsive': True, 'modeBarButtonsToRemove': ['lasso2d', 'select2d']},
                style={'height': '420px', 'marginBottom': '20px'}
            ),
            html.Div(id='surface-table-container', style={'width': '100%'})
        ], id='surface-content-wrapper', style={'display': 'none'})
    ], id='surface-section-container', style={'width': '100%', 'marginTop': '32px'})
])


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

    if atm_error and atm_dataset.empty:
        return _build_message_span(f'ATM source unavailable: {atm_error}', tone='error')

    current_pivot, _, sorted_date_cols = _build_atm_table_frames(selected_date, None, selected_products, grouping_mode)
    visible_products = int(current_pivot['product'].nunique()) if not current_pivot.empty else 0
    period_count = len(sorted_date_cols)
    missing_cells = int(current_pivot[sorted_date_cols].isna().sum().sum()) if sorted_date_cols and not current_pivot.empty else 0
    selected_date_text = pd.to_datetime(selected_date).strftime('%Y-%m-%d') if selected_date else 'n/a'

    message = (
        f'ATM sources: {ATM_SOURCE_LABEL} for ATM-only markets; derived ATM from {surface_source} for TTF/JKM/NBP. '
        f'Selected date: {selected_date_text} | Products shown: {visible_products}/{len(selected_products)} | '
        f'Grouped tenors: {period_count} | Missing cells: {missing_cells}'
    )

    if surface_error:
        message = f'{message} | Surface-derived ATM warning: {surface_error}'
        return _build_message_span(message, tone='warning')
    return _build_message_span(message, tone='neutral')


def _build_surface_status_line(selected_date, prev_selected_date, active_product):
    surface_error = DATA_CACHE_STATE['surface']['error']
    surface_source = DATA_CACHE_STATE['surface']['source']
    if surface_error and surface_dataset.empty:
        return _build_message_span(f'Surface source unavailable: {surface_error}', tone='error')

    if not active_product or selected_date is None:
        return _build_message_span(
            f'Source: {surface_source}. Select TTF, JKM, or NBP to inspect the full volatility surface.',
            tone='success'
        )

    current_surface = _get_surface_snapshot(active_product, selected_date)
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
        f'Data refreshed from {ATM_SOURCE_LABEL} and {surface_source}.',
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
    product_codes = sorted([code for code in atm_dataset['code'].unique() if code != 'TTF_benchmark']) if not atm_dataset.empty else []
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
    ttf_benchmark_df = atm_df[atm_df['code'] == 'TTF_benchmark'].copy()

    latest_ttf_benchmark_data = pd.DataFrame()
    latest_ttf_benchmark_date_str = None
    if not ttf_benchmark_df.empty:
        latest_ttf_benchmark_date = pd.Timestamp(ttf_benchmark_df['cob_date'].max())
        latest_ttf_benchmark_data = ttf_benchmark_df[ttf_benchmark_df['cob_date'] == latest_ttf_benchmark_date].copy()
        latest_ttf_benchmark_date_str = latest_ttf_benchmark_date.strftime('%Y-%m-%d')

    graphs = []
    row = []
    product_codes = selected_products if selected_products else [code for code in atm_df['code'].unique() if code != 'TTF_benchmark']

    for index, code in enumerate(product_codes):
        code_df = atm_df[atm_df['code'] == code].copy()
        if code_df.empty:
            continue

        fig = go.Figure()
        recent_dates = code_df['cob_date'].drop_duplicates().nlargest(5)
        has_selected_or_closest = False

        for cob_date in recent_dates:
            cob_date = pd.Timestamp(cob_date)
            cob_data = code_df[code_df['cob_date'] == cob_date].sort_values('contract_date').copy()
            if cob_data.empty:
                continue

            is_selected_date = cob_date.date() == selected_date.date()
            if is_selected_date:
                has_selected_or_closest = True

            line_style = dict(width=1.5, color='blue', dash='dash') if is_selected_date else dict(width=1)
            legend_name = f'★ {cob_date.strftime("%Y-%m-%d")} (SELECTED) ★' if is_selected_date else cob_date.strftime('%Y-%m-%d')

            if grouping_mode != 'monthly':
                grouped_data = group_data_by_period(cob_data, grouping_mode).sort_values('period')
                x_values = grouped_data['period'] if 'period' in grouped_data.columns else cob_data['contract_date']
                y_values = grouped_data['volatility'] if 'period' in grouped_data.columns else cob_data['volatility']
                mode = 'lines+markers' if 'period' in grouped_data.columns else 'lines'
            else:
                x_values = cob_data['contract_date']
                y_values = cob_data['volatility']
                mode = 'lines'

            fig.add_trace(go.Scatter(x=x_values, y=y_values, mode=mode, name=legend_name, line=line_style))

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
                        name=f'⚠ {candidate_date.strftime("%Y-%m-%d")} - CLOSEST AVAILABLE',
                        line=dict(width=1, color='orange', dash='dot')
                    ))

        if code.upper() in ['JKM', 'HH', 'TTF'] and not latest_ttf_benchmark_data.empty:
            benchmark_data = latest_ttf_benchmark_data.sort_values('contract_date').copy()
            if grouping_mode != 'monthly':
                benchmark_grouped = group_data_by_period(benchmark_data, grouping_mode).sort_values('period')
                x_values = benchmark_grouped['period'] if 'period' in benchmark_grouped.columns else benchmark_data['contract_date']
                y_values = benchmark_grouped['volatility'] if 'period' in benchmark_grouped.columns else benchmark_data['volatility']
                mode = 'lines+markers' if 'period' in benchmark_grouped.columns else 'lines'
            else:
                x_values = benchmark_data['contract_date']
                y_values = benchmark_data['volatility']
                mode = 'lines'

            fig.add_trace(go.Scatter(
                x=x_values,
                y=y_values,
                mode=mode,
                name=f'TTF_benchmark ({latest_ttf_benchmark_date_str})',
                line=dict(dash='dash', color='black', width=1)
            ))

        fig.update_layout(
            title=dict(
                text=f'{code} Volatility',
                font=dict(size=18, color='#1f2937', family='Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif'),
                x=0.5,
                y=0.96,
                pad=dict(b=20)
            ),
            xaxis=dict(
                title=dict(text='', font=dict(size=12, color='#374151')),
                tickangle=0,
                showgrid=True,
                gridcolor='rgba(200, 200, 200, 0.3)',
                gridwidth=0.5,
                linecolor='#CCCCCC',
                linewidth=1,
                tickfont=dict(size=10, color='#6b7280'),
                tickmode='auto'
            ),
            yaxis=dict(
                title=dict(text='Volatility', font=dict(size=12, color='#374151')),
                showgrid=True,
                gridcolor='rgba(200, 200, 200, 0.3)',
                gridwidth=0.5,
                linecolor='#CCCCCC',
                linewidth=1,
                tickfont=dict(size=10, color='#6b7280'),
                zeroline=True,
                zerolinecolor='rgba(150, 150, 150, 0.4)',
                zerolinewidth=1
            ),
            legend=dict(
                orientation='h',
                yanchor='top',
                y=-0.08,
                xanchor='center',
                x=0.5,
                bgcolor='rgba(255, 255, 255, 0)',
                bordercolor='rgba(255, 255, 255, 0)',
                borderwidth=0,
                font=dict(size=10, color='#4A4A4A'),
                itemsizing='constant',
                itemwidth=30
            ),
            plot_bgcolor='rgba(248, 249, 250, 0.5)',
            paper_bgcolor='white',
            margin=dict(l=70, r=70, t=80, b=100),
            hovermode='x unified',
            hoverlabel=dict(
                bgcolor='rgba(255, 255, 255, 0.95)',
                bordercolor='rgba(200, 200, 200, 0.8)',
                font=dict(size=11, color='#2C3E50'),
                align='left'
            ),
            transition=dict(duration=300, easing='cubic-in-out')
        )

        row.append(html.Div(
            dcc.Graph(
                figure=fig,
                config={'displayModeBar': True, 'responsive': True, 'modeBarButtonsToRemove': ['lasso2d', 'select2d']},
                style={'height': '420px'}
            ),
            style={'display': 'inline-block', 'width': '33%', 'padding': '5px', 'marginBottom': '10px'}
        ))

        if (index + 1) % 3 == 0:
            graphs.append(html.Div(row, style={'display': 'flex'}))
            row = []

    if row:
        graphs.append(html.Div(row, style={'display': 'flex'}))

    return graphs


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

    columns = [{'name': 'Product', 'id': 'product'}]
    for column in sorted_date_cols:
        columns.append({'name': column, 'id': column, 'type': 'numeric', 'format': {'specifier': '.4f'}})

    current_table = html.Div([
        dash_table.DataTable(
            id='volatility-table',
            columns=columns,
            data=current_pivot.replace({float('nan'): None}).to_dict('records'),
            style_table={'overflowX': 'auto'},
            style_cell={'textAlign': 'center', 'padding': '4px', 'fontSize': 12, 'minWidth': '40px', 'maxWidth': '100px', 'color': 'black'},
            style_cell_conditional=[{'if': {'column_id': 'product'}, 'textAlign': 'left', 'fontWeight': 'bold', 'width': '80px', 'color': 'black'}],
            style_header={'backgroundColor': 'rgb(230, 230, 230)', 'fontWeight': 'bold', 'textAlign': 'center', 'padding': '4px', 'fontSize': 12, 'whiteSpace': 'normal', 'height': 'auto', 'color': 'black'},
            style_data_conditional=[{'if': {'row_index': 'odd'}, 'backgroundColor': 'rgb(248, 248, 248)'}],
            fixed_rows={'headers': True},
            page_action='none',
            tooltip_delay=0,
            tooltip_duration=None
        )
    ], style={'marginBottom': '15px', 'width': '100%'})

    changes_table = html.Div()
    if not changes_pivot.empty:
        conditional_styling = [{'if': {'row_index': 'odd'}, 'backgroundColor': 'rgb(248, 248, 248)'}]
        for column in sorted_date_cols:
            conditional_styling.extend([
                {'if': {'column_id': column, 'filter_query': f'{{{column}}} > 0'}, 'color': 'green', 'fontWeight': 'bold'},
                {'if': {'column_id': column, 'filter_query': f'{{{column}}} < 0'}, 'color': 'red', 'fontWeight': 'bold'}
            ])

        changes_table = html.Div([
            html.H3('Difference vs. Previous Date', className='greeks-title', style={'margin-bottom': '12px', 'margin-top': '24px'}),
            dash_table.DataTable(
                id='changes-table',
                columns=columns,
                data=changes_pivot.replace({float('nan'): None}).to_dict('records'),
                style_table={'overflowX': 'auto'},
                style_cell={'textAlign': 'center', 'padding': '4px', 'fontSize': 12, 'minWidth': '40px', 'maxWidth': '100px', 'color': 'black'},
                style_cell_conditional=[{'if': {'column_id': 'product'}, 'textAlign': 'left', 'fontWeight': 'bold', 'width': '80px', 'color': 'black'}],
                style_header={'backgroundColor': 'rgb(230, 230, 230)', 'fontWeight': 'bold', 'textAlign': 'center', 'padding': '4px', 'fontSize': 12, 'whiteSpace': 'normal', 'height': 'auto', 'color': 'black'},
                style_data_conditional=conditional_styling,
                fixed_rows={'headers': True},
                page_action='none',
                tooltip_delay=0,
                tooltip_duration=None
            )
        ], style={'width': '100%'})

    table_header = html.Div([
        html.H3('Volatility Data', className='greeks-title', style={'margin': '0', 'display': 'inline-block'}),
        html.Button(
            'Export',
            id='export-volatility-table-btn',
            className='custom-export-btn',
            style={'margin-left': '12px', 'font-size': '11px', 'padding': '4px 8px', 'background-color': '#2E86C1', 'color': 'white', 'border': 'none', 'border-radius': '3px', 'cursor': 'pointer', 'font-weight': '500'}
        )
    ], style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '12px'})

    return html.Div([table_header, current_table, changes_table])


@callback(
    [Output('surface-product-tabs', 'children'),
     Output('surface-product-tabs', 'value'),
     Output('surface-content-wrapper', 'style'),
     Output('surface-empty-message', 'children')],
    [Input('refresh-options-data', 'n_clicks'),
     Input('table-product-dropdown', 'value')],
    State('surface-product-tabs', 'value'),
    prevent_initial_call=False
)
def update_surface_tabs(n_clicks, selected_products, active_tab):
    _ensure_cached_data(n_clicks or 0)
    supported_products = _get_supported_surface_products(selected_products)

    if not supported_products:
        return [], None, {'display': 'none'}, 'Full surface data is available only for TTF, JKM, and NBP.'

    tabs = [dcc.Tab(label=product, value=product) for product in supported_products]
    active_value = active_tab if active_tab in supported_products else supported_products[0]
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

    status_line = _build_surface_status_line(selected_date, prev_selected_date, active_product)
    if selected_date is None or not active_product:
        empty_heatmap = _empty_figure('Select a supported product to view the surface.', 'Surface Heatmap')
        empty_smile = _empty_figure('Select a supported product to view smile details.', 'Smile Evolution')
        empty_history = _empty_figure('Select a supported product, expiry, and delta bucket to view history.', 'Delta Vol History')
        return empty_heatmap, empty_smile, empty_history, html.Div(), status_line

    current_surface = _get_surface_snapshot(active_product, selected_date)
    previous_surface = _get_surface_snapshot(active_product, prev_selected_date) if prev_selected_date else _empty_surface_df()

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

    current_table = html.Div([
        html.H3('Surface Matrix', className='greeks-title', style={'margin-bottom': '12px'}),
        _create_surface_datatable(current_table_df, delta_columns, 'surface-current-table')
    ], style={'marginBottom': '20px'})

    if not diff_table_df.empty:
        diff_table = html.Div([
            html.H3('Difference vs. Previous Date', className='greeks-title', style={'margin-bottom': '12px'}),
            _create_surface_datatable(diff_table_df, delta_columns, 'surface-diff-table', include_diff_colors=True)
        ])
    else:
        diff_table = html.Div('No previous-date surface data available for comparison.', style={'padding': '8px 0 0 0', 'color': '#6b7280'})

    return heatmap_figure, smile_figure, history_figure, html.Div([current_table, diff_table]), status_line


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
        import io
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
    except Exception as exc:
        print(f'Export error: {exc}')
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
        import io
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
    except Exception as exc:
        print(f'Surface export error: {exc}')
        return None
 
