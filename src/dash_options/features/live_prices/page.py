"""Manual Bloomberg snapshot board; no price collection on navigation or timer."""
from datetime import datetime
import time
from zoneinfo import ZoneInfo
import logging

import dash_ag_grid as dag
import plotly.graph_objects as go
from dash import ALL, Input, Output, Patch, State, ctx, dcc, html, no_update
from flask import request
from options.live_prices import period_rows, METHODOLOGY_VERSION

from dash_options.bootstrap import app
import dash_options.features.live_prices.service as data
import dash_options.shared.live_forwards as desk
from dash_options.calibration.auth import Permission, authorize, resolve_request_identity

LOG = logging.getLogger(__name__)
PRODUCTS = [('TTF', 'EUR/MWh'), ('TFU', 'USD/MMBtu'), ('Brent', 'USD/bbl'), ('HH', 'USD/MMBtu')]
GRID_ROW_HEIGHT = 22
GRID_HEADER_HEIGHT = 28
EMPTY = html.Div('Click Refresh to capture Bloomberg prices for all four markets and EUR/USD.',
                 className='live-prices-empty')


def identity(permission=Permission.VIEW):
    current = resolve_request_identity(request.headers, remote_addr=request.remote_addr)
    authorize(current, permission)
    return current.subject


def numeric(field, title, decimals=3, **extra):
    result = {'field': field, 'headerName': title, 'type': 'numericColumn', 'width': 82,
            'valueFormatter': {'function': f"params.value == null ? '—' : d3.format(',.{decimals}f')(params.value)"},
            'enableCellChangeFlash': field not in {'prior'}, **extra}
    if field == 'mid':
        result.update(tooltipField='mark_description',
                      valueFormatter={'function': "params.value == null ? (params.data && params.data.status === 'Expired contract' ? 'Expired' : '—') : "
                                      f"d3.format(',.{decimals}f')(params.value) + "
                                      "(params.data.mark_quality === 'Settlement fallback' ? ' †' : params.data.mark_quality === 'Reconstructed' ? ' ≈' : '')"},
                      cellClassRules={
                          'live-prices-mark-fallback': {'function': "params.data && params.data.mark_quality === 'Settlement fallback'"},
                          'live-prices-mark-estimated': {'function': "params.data && params.data.mark_quality === 'Reconstructed'"}})
    return result


def grid(grid_id, rows, columns, height=600):
    return html.Div(dag.AgGrid(
        id={'type': 'live-prices-grid-values', 'name': grid_id}, rowData=rows, columnDefs=columns,
        getRowId='params.data.id', className='ag-theme-alpine live-prices-grid',
        defaultColDef={'sortable': False, 'resizable': True, 'suppressMovable': True},
        dashGridOptions={'rowHeight': GRID_ROW_HEIGHT, 'headerHeight': GRID_HEADER_HEIGHT, 'animateRows': False,
                         'suppressScrollOnNewData': True,
                         'tooltipShowDelay': 150, 'cellFlashDuration': 700,
                         'suppressCellFocus': False, 'ensureDomOrder': True,
                         'enableBrowserTooltips': True},
        style={'height': height, 'width': '100%'},
    ), id=grid_id)


def market_card(product, unit, rows, height=600, *, embedded=False):
    identifier = {'field': 'month', 'headerName': 'Month', 'pinned': 'left', 'width': 90,
                  'valueFormatter': {'function': 'params.data ? params.data.maturity : ""'},
                  'tooltipField': 'security'}
    mid_precision = 4 if product in {'TTF', 'HH'} else 3
    columns = [identifier, numeric('mid', 'TFU Mark' if product == 'TFU' else product + ' Mark', mid_precision), numeric('prior', 'D-1 calc.' if product == 'TFU' else 'D-1'),
               numeric('change', 'Δ D-1', mid_precision, cellClassRules={
                   'live-prices-up': {'function': 'params.value > 0'},
                   'live-prices-down': {'function': 'params.value < 0'}}),
               numeric('bid', 'Bid', tooltipField='bid_source'), numeric('ask', 'Ask', tooltipField='ask_source')]
    if product in {'TTF', 'Brent', 'HH'}:
        columns = [identifier, numeric('mid', product + ' Mark', mid_precision, width=90), numeric('prior', 'D-1', width=78),
                   numeric('live_mid', 'Live Mid', 4, width=90),
                   numeric('quoted_bid', 'Bid', width=76), numeric('quoted_ask', 'Ask', width=76),
                   numeric('change', 'Δ D-1', 4)]
    if product == 'TTF':
        columns.insert(3, numeric('spread', 'Spread', 4, cellClassRules={
            'live-prices-up': {'function': 'params.value > 0'},
            'live-prices-down': {'function': 'params.value < 0'}}))
    if product != 'TFU':
        columns.append(numeric('last', 'Last'))
    if product == 'Brent':
        columns.append(numeric('slope', 'Slope %', 2, width=88))
    if product == 'TFU':
        columns.append({'field': 'fixed', 'headerName': 'Fixed days', 'width': 100})
    if embedded:
        heading_id = f'live-prices-{product.lower()}-monthly-heading'
        for column in columns:
            column.update(flex=1, minWidth=68)
        columns[0].update(flex=0, width=82, minWidth=82)
        columns[1].update(minWidth=100)
        return html.Div([
            html.Div(html.H3('Monthly curve', id=heading_id),
                     className='live-prices-panel-heading'),
            grid(f'live-prices-{product.lower()}-grid', rows, columns, 'var(--live-prices-monthly-height)'),
        ], className='live-prices-market-panel live-prices-monthly-panel',
           **{'aria-labelledby': heading_id})
    available = sum(r.get('mid') is not None for r in rows)
    subtitle = ('Calculated · Bloomberg FX · ICE TFU averaging' if product == 'TFU' else
                'Bloomberg · quality-controlled Mark; raw monthly quotes shown separately' if product == 'TTF' else
                'Bloomberg · bid/ask midpoint or available side')
    return html.Section([
        html.Div([html.Div([html.H2('Henry Hub' if product == 'HH' else product),
                            html.Span(unit, className='live-prices-unit')]),
                  html.Span(f'{available}/{len(rows)} marks', className='live-prices-coverage')],
                 className='live-prices-card-heading'),
        html.P(subtitle, className='live-prices-card-caption'),
        grid(f'live-prices-{product.lower()}-grid', rows, columns, height),
    ], className='live-prices-card')


def period_card(product, rows):
    has_quotes = product != 'TFU'
    precision = 4 if product in {'TTF', 'HH'} else 3
    columns = [{'field': 'period', 'headerName': 'Period', 'pinned': 'left', 'width': 120},
               numeric('mid', 'Mark', precision, tooltipField='status'),
               numeric('prior', 'D-1 calc.' if product == 'TFU' else 'D-1', precision),
               numeric('change', 'Δ D-1', precision)]
    if has_quotes:
        columns.append(numeric('live_mid', 'Live Mid', 4))
    columns.extend([numeric('bid', 'Bid', precision), numeric('ask', 'Ask', precision)])
    for column in columns:
        column.update(flex=1, minWidth=68)
    columns[0].update(flex=0, width=106, minWidth=106)
    groups = []
    kinds = [('calendar', 'Calendars'), ('season', 'Summer / Winter'), ('quarter', 'Quarters')]
    if product == 'Brent':
        kinds.append(('spread', 'December spreads'))
    for kind, title in kinds:
        selected = [r for r in rows if r['kind'] == kind]
        heading_id = f'live-prices-{product.lower()}-{kind}-heading'
        groups.append(html.Div([
            html.Div([html.H4(title, id=heading_id),
                      html.Span(f'{len(selected)} periods', className='live-prices-group-count')],
                     className='live-prices-strip-heading'),
            grid(f'live-prices-{product.lower()}-{kind}-grid', selected, columns,
                 min(370, GRID_HEADER_HEIGHT + 4 + GRID_ROW_HEIGHT * len(selected))) if selected else
            html.P('No periods in this snapshot.', className='live-prices-card-caption'),
        ], className='live-prices-strip-group', **{'aria-labelledby': heading_id}))
    heading_id = f'live-prices-{product.lower()}-strips-heading'
    return html.Div([
        html.Div([html.H3('Period strips', id=heading_id),
                  html.Span('Full delivery periods' if product == 'TTF' else 'Equal monthly lots',
                            className='live-prices-panel-caption')],
                 className='live-prices-panel-heading'), *groups,
    ], className='live-prices-market-panel live-prices-strips-panel',
       **{'aria-labelledby': heading_id})


def market_workspace(product, unit, view):
    monthly = view['products'][product]
    strips = view['strips'].get(product)
    if strips is None:  # Offline views saved before the strip inventory existed.
        strips = period_rows(view['months'], monthly, product)
    heading_id = f'live-prices-{product.lower()}-heading'
    active_monthly = [r for r in monthly if r.get('status') != 'Expired contract']
    monthly_count = sum(r.get('mid') is not None for r in active_monthly)
    fallback_count = sum(r.get('mark_quality') == 'Settlement fallback' for r in active_monthly)
    strip_count = sum(r.get('mid') is not None for r in strips)
    return html.Section([
        html.Div([
            html.Div([
                html.Div([html.H2('Henry Hub' if product == 'HH' else product, id=heading_id),
                          html.Span(unit, className='live-prices-workspace-unit')],
                         className='live-prices-workspace-title'),
                html.P('Monthly prices and forward strips', className='live-prices-workspace-description'),
            ]),
            html.Div([
                html.Span([html.Strong(f'{monthly_count}/{len(active_monthly)}'), ' active monthly Marks' if len(active_monthly) != len(monthly) else ' monthly Marks']),
                html.Span([html.Strong(f'{strip_count}/{len(strips)}'), ' strip Marks']),
                html.Span(f'{fallback_count} D-1 fallbacks', className='live-prices-workspace-warning') if fallback_count else None,
            ], className='live-prices-workspace-counts',
               id={'type': 'live-prices-section-values', 'name': product + '-counts'}),
        ], className='live-prices-workspace-header'),
        html.Div([
            market_card(product, unit, monthly, embedded=True),
            period_card(product, strips),
        ], className='live-prices-market-section'),
        html.P(('TFU uses the same snapshot’s TTF Marks and Bloomberg EUR/USD under the ICE averaging schedule. '
                'Values are indicative; historical FX uses BGN closes rather than the ICE WMR fixing. ' if product == 'TFU' else
                'Mark is the reconciled curve value. Live Mid, Bid and Ask retain the raw Bloomberg quotes. ') +
               'Every strip includes its complete delivery period, including hidden months. '
               '≈ reconstructed estimate; † previous-settlement fallback. Hover over a Mark for its source. ' +
               ('Unquoted Henry Hub months use historical sensitivity to trusted current anchor moves. '
                'Henry Hub summer: April–October; winter: November–March. ' if product == 'HH' else
                'Summer: April–September; winter: October–March. ') +
               ('Expired monthly contracts have no current trading Mark.' if len(active_monthly) != len(monthly) else ''),
               className='live-prices-workspace-footer',
               id={'type': 'live-prices-section-values', 'name': product + '-footer'}),
    ], id=f'live-prices-{product.lower()}-workspace', className='live-prices-market-workspace',
       **{'aria-labelledby': heading_id})


def curve_card(view, product, unit):
    rows = view['products'][product]
    months = [row['month'] for row in rows]
    key = product.lower()
    title = 'Henry Hub' if product == 'HH' else product
    heading_id = f'live-prices-{key}-curve-heading'
    figure = go.Figure()
    for field, label, color, dash in (('mid', product + ' Mark', '#2e86c1', 'solid'),
                                      ('prior', 'D-1', '#64748b', 'dash')):
        mark = field == 'mid'
        figure.add_trace(go.Scatter(
            x=months, y=[row.get(field) for row in rows], name=label, mode='lines+markers',
            line={'color': color, 'width': 2.5, 'dash': dash},
            marker={'size': 5, 'symbol': [
                'triangle-up-open' if r.get('mark_basis', '').startswith('D-1') else
                'circle' if r.get('mark_basis') == 'Monthly' else 'diamond-open' for r in rows] if mark else 'circle'},
            connectgaps=False,
            customdata=[[r.get('mark_basis', ''), r.get('mark_anchor', ''), r.get('freshness', '')] for r in rows],
            hovertemplate=(f'{label}: %{{y:.4f}} {unit}' +
                           ('<br>%{customdata[0]}<br>%{customdata[1]}<br>%{customdata[2]}' if mark else '') + '<extra></extra>'),
        ))
    figure.update_layout(
        template='plotly_white', autosize=True, hovermode='x unified',
        font={'family': 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
              'size': 11, 'color': '#0f172a'},
        plot_bgcolor='#f8fafc', paper_bgcolor='white', margin={'l': 55, 'r': 18, 't': 65, 'b': 56},
        legend={'orientation': 'h', 'x': 0, 'y': 1.04, 'font': {'size': 11}},
        xaxis={'title': 'Delivery month', 'type': 'date', 'tickformat': '%b %y', 'hoverformat': '%b %Y',
               'showgrid': False, 'automargin': True, 'range': [months[0], months[-1]] if months else None},
        yaxis={'title': unit, 'gridcolor': 'rgba(148,163,184,.18)', 'zeroline': False,
               'automargin': True},
        uirevision=f'{key}-curve:{months[0]}:{months[-1]}' if months else f'{key}-curve:empty',
    )
    if not any(row.get(field) is not None for row in rows for field in ('mid', 'prior')):
        figure.add_annotation(text=f'No {title} prices available in this snapshot', showarrow=False,
                              x=.5, y=.5, xref='paper', yref='paper')
    return html.Section([
        html.H2(f'{title} curve comparison', id=heading_id),
        html.P(f'{product} Mark and D-1 settlement from the same snapshot, in {unit}. '
               'Circles: monthly basis; diamonds: reconstructed; triangles: D-1 interpolation, carry or settlement fallback.',
               className='live-prices-card-caption'),
        html.Div(dcc.Graph(id={'type': 'live-prices-curve-values', 'name': f'live-prices-{key}-curve'}, figure=figure, responsive=True,
                          config={'displayModeBar': 'hover', 'displaylogo': False, 'responsive': True,
                                  'modeBarButtonsToRemove': ['lasso2d', 'select2d']}),
                 id=f'live-prices-{key}-curve', className='live-prices-curve-graph'),
    ], className='live-prices-card live-prices-curve-card',
       **{'aria-labelledby': heading_id})


def render(view):
    captured = datetime.fromisoformat(view['captured_at']).astimezone(ZoneInfo('Asia/Dubai'))
    first, last = view['products']['TTF'][0]['maturity'], view['products']['TTF'][-1]['maturity']
    header = html.Div([
        html.Span(f'Snapshot · {captured:%d %b %Y %H:%M:%S} GST'),
        html.Span(f"{first} – {last} · {len(view['months'])} months"),
        html.Span('Following manually accepted desk curve' if view.get('desk_curve_revision') else 'Captured candidate · fixed until Refresh' if desk.enabled() else 'Prices stay fixed until Refresh'),
    ], className='live-prices-snapshot-meta',
       id={'type': 'live-prices-section-values', 'name': 'snapshot-meta'})
    warnings = view['warnings']
    fallback_notices = [f"{sum(r.get('mark_quality') == 'Settlement fallback' for r in view['products'][product])} "
                        f"{'Henry Hub' if product == 'HH' else product} monthly Marks use previous-trading-day settlement fallback."
                        for product,_ in PRODUCTS if any(r.get('mark_quality') == 'Settlement fallback'
                                                         for r in view['products'][product])]
    historical_count = sum(r.get('mark_basis') == 'Historical spread estimate' for r in view['products'].get('HH', []))
    if historical_count:
        fallback_notices.append(f'{historical_count} Henry Hub monthly Marks use historical sensitivity to trusted current anchor moves. '
                                'These are reconstructed estimates; hover over a Mark for the anchor and historical evidence.')
    warning_panel = html.Details([
        html.Summary(f'{len(warnings)} source notices'),
        html.Ul([html.Li(str(message)) for message in warnings]),
    ], className='live-prices-source-notices') if warnings else None
    fx_columns = [{'field': 'month', 'headerName': 'Month', 'pinned': 'left', 'width': 100,
                   'valueFormatter': {'function': 'params.data ? params.data.maturity : ""'}},
                  numeric('first', '1st · USD/EUR', 6, width=160),
                  numeric('middle', '15th · USD/EUR', 6, width=160),
                  {'field': 'first_fixing_date', 'headerName': '1st fixing date', 'width': 150},
                  {'field': 'first_value_date', 'headerName': '1st value date', 'width': 150},
                  {'field': 'middle_fixing_date', 'headerName': '15th fixing date', 'width': 150},
                  {'field': 'middle_value_date', 'headerName': '15th value date', 'width': 150}]
    return html.Div([
        header, html.Div(warning_panel, id={'type': 'live-prices-section-values', 'name': 'source-notices'}),
        html.Div(html.P(' '.join(fallback_notices), className='live-prices-source-notices', role='note') if fallback_notices else None,
                 id={'type': 'live-prices-section-values', 'name': 'fallback-notices'}),
        *[market_workspace(product, unit, view) for product, unit in PRODUCTS],
        html.Section([html.H2('EUR/USD forward rates'),
                      html.P('Spot plus interpolated forward points. Non-business fixing dates roll forward; value dates include the FX spot lag.',
                             className='live-prices-card-caption'),
                      grid('live-prices-fx-grid', view['fx'], fx_columns, 520)], className='live-prices-card'),
        *[curve_card(view, product, unit) for product, unit in PRODUCTS if product != 'TFU'],
    ])


def render_parts(view):
    """One presentation path supplies initial components and direct value updates."""
    board = render(view)
    values = {'live-prices-grid-values': {}, 'live-prices-curve-values': {}, 'live-prices-section-values': {}}

    def walk(node):
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
        elif hasattr(node, 'to_plotly_json'):
            identifier = getattr(node, 'id', None)
            if isinstance(identifier, dict) and identifier.get('type') in values:
                kind, name = identifier['type'], identifier['name']
                if kind == 'live-prices-grid-values':
                    value = node.rowData
                elif kind == 'live-prices-curve-values':
                    value = Patch()
                    value['data'] = node.figure.to_plotly_json()['data']
                else:
                    value = node.children
                values[kind][name] = value
            walk(getattr(node, 'children', None))

    walk(board)
    return board, values


layout = html.Main([
    dcc.Store(id='live-prices-tab', storage_type='session'),
    dcc.Store(id='live-prices-ticket', storage_type='session'),
    dcc.Store(id='live-prices-reference', storage_type='session'),
    dcc.Store(id='live-prices-render-reference'),
    dcc.Store(id='live-prices-desk-head'),
    dcc.Interval(id='live-prices-desk-poll', interval=15000, disabled=not desk.enabled()),
    dcc.Interval(id='live-prices-job-poll', interval=300, disabled=True),
    html.Div([
        html.Div([html.H1('Live Prices'), html.P('Bloomberg · manual snapshot', className='live-prices-subtitle')]),
        html.Button([html.Span('↻', **{'aria-hidden': 'true'}), html.Span('Refresh')],
                    id='live-prices-refresh', n_clicks=0, className='btn-refresh'),
    ], className='live-prices-toolbar'),
    html.Div([html.Strong('Bloomberg commodity quotes may be delayed by 10 minutes. '),
              ('Refresh captures a candidate. Use captured curve for desk accepts it manually; other pages then follow that revision. ' if desk.enabled() else 'Prices update only when you click Refresh. '),
              'Marks may be reconstructed; any use of D-1 settlement fallback is flagged below.'],
             id='live-prices-delay-warning', className='live-prices-source-notices', role='note'),
    html.Div(id='live-prices-progress', role='status', **{'aria-live': 'polite'}),
    html.Div([
        html.Button('Show desk curve', id='live-prices-show-desk', n_clicks=0),
        html.Button('Use captured curve for desk', id='live-prices-accept-desk', n_clicks=0),
        html.Span(id='live-prices-desk-status', role='status'),
    ], className='live-prices-source-notices', style={} if desk.enabled() else {'display': 'none'}),
    html.Div(EMPTY, id='live-prices-board'),
    html.Details([
        html.Summary('Price and FX methodology'),
        html.P('TTF, Brent and Henry Hub Marks use a shared quality-controlled monthly curve. '
               'TTF Live Mid requires both raw monthly sides; Bid and Ask retain original quotes. '
               'Quote eligibility uses product-specific currency, units, contract size, venue, side freshness, width, size and consistency checks. '
               'Accepted monthly, quarter, season, calendar and independently quoted spread markets are reconciled on one monthly curve. '
               'Spread is the preceding month’s TTF Mark minus this month’s Mark, with the first month blank. '
               'Reconstruction preserves delivery-hour-weighted D-1 shape; unsafe ratios use additive shape. '
               'Brent gaps bounded by reliable quotes preserve interpolation between both daily moves. '
               'Other unsupported months use historical responses selected from prior settlement changes; '
               'nearest and same-delivery-month anchors compete on preceding forecast errors. '
               'HH estimates use the target’s D-1 settlement plus its historical sensitivity to current original anchor moves. '
               'Quote freshness allows the provider-declared 10-minute delay; additional quote age is checked separately. '
               'Insufficient current history evidence blocks a new snapshot; older replays retain their limited-history policy. '
               'D-1 shape dates must align and periods must be complete. One-sided markets never establish a quoted central TTF Mark. '
               'Model Marks do not manufacture executable monthly Bid/Ask. '
               'Source, rejection reason and historical replay limitations remain visible. '
               'Crossed quotes have no mid. Δ D-1 compares mid with the previous trading-day settlement. '
               'The four-year ladder completes its final calendar year and every final season; failed or incomplete refreshes retain the last complete snapshot and its original timestamp. '
               'The snapshot timestamp and source notices apply to every section.'),
        html.P('TFU is calculated from TTF under the ICE TFU front-month averaging schedule. '
               'Fixed days retain their TTF settlement and Bloomberg EURUSD BGN closing rate. '
               'Unfixed days use the current TTF Mark and the interpolated EURUSD forward for that fixing’s value date. '
               'Bloomberg EURUSD closes are the selected workbook FX basis; ICE uses WMR 4 p.m. London fixings. '
               'TFU D-1 is reconstructed from prior-day TTF settlements and the prior-day EURUSD curve. '
               'Calculated TFU values and bid/ask sides are indicative, not exchange TFU quotes.'),
        html.P('Forward points use Bloomberg’s scale and actual tenor value dates. '
               'Interpolation is linear in points over actual calendar days, without extrapolation. '
               'Prior-day calculations use prior-day inputs only. Brent slope is 100 × TFU mid / same-month Brent mid. '
               'Summer is April–September and winter October–March. TTF period Marks are delivery-hour-weighted averages of all constituent monthly Marks, including hidden months. '
               'Complete monthly Mark coverage is required. Period D-1 retains its own Bloomberg settlement; Curve D-1 shows the aligned weighted monthly settlement average when available. '
               'Brent, TFU and Henry Hub strips use equal monthly contract lots and the full internal horizon. Henry Hub summer is April–October and winter November–March. '
               'December spreads are earlier December minus later December.'),
    ], className='live-prices-methodology'),
], className='live-prices-page')


@app.callback(
    Output('live-prices-reference', 'data', allow_duplicate=True),
    Output('live-prices-desk-head', 'data'), Output('live-prices-desk-status', 'children'),
    Input('live-prices-desk-poll', 'n_intervals'), Input('live-prices-show-desk', 'n_clicks'),
    Input('live-prices-accept-desk', 'n_clicks'), State('live-prices-reference', 'data'),
    State('live-prices-desk-head', 'data'), prevent_initial_call='initial_duplicate',
)
def manage_desk_curve(_tick, _show, _accept, reference, reviewed_head):
    if not desk.enabled():
        return no_update, no_update, 'Shared desk curves disabled'
    try:
        actor = identity(Permission.PUBLISH if ctx.triggered_id == 'live-prices-accept-desk' else Permission.VIEW)
        repo = desk.repository()
        if ctx.triggered_id == 'live-prices-accept-desk':
            if not reference or reference.get('kind') == 'desk_curve':
                raise ValueError('Capture and review Bloomberg prices before accepting a desk curve')
            view = data.resolve(reference, actor)
            from dash_options.core.source_identity import source_config_fingerprint
            candidate = repo.stage(view, actor=actor, source_fingerprint=source_config_fingerprint())
            curve = repo.activate(candidate.revision, actor=actor, expected_revision=reviewed_head)
        else:
            curve = repo.resolve()
        following = not reference or reference.get('kind') == 'desk_curve'
        use_desk = following or ctx.triggered_id in {'live-prices-show-desk', 'live-prices-accept-desk'}
        target = {'kind': 'desk_curve', 'revision': curve.revision}
        status = desk.describe(curve)
        if not use_desk:
            status += ' · Viewing your captured candidate; desk is unchanged'
        return (target if use_desk and reference != target else no_update), (curve.revision if use_desk else no_update), status
    except (PermissionError, ValueError) as exc:
        return no_update, no_update, str(exc)
    except Exception:
        LOG.exception('Shared desk curve unavailable')
        return no_update, no_update, 'Desk curve storage unavailable; existing marks retained'


@app.callback(
    Output('live-prices-ticket', 'data'), Output('live-prices-tab', 'data'),
    Output('live-prices-reference', 'data'), Output('live-prices-job-poll', 'disabled'),
    Output('live-prices-refresh', 'disabled'), Output('live-prices-progress', 'children'),
    Input('live-prices-refresh', 'n_clicks'), Input('live-prices-job-poll', 'n_intervals'),
    State('live-prices-ticket', 'data'), State('live-prices-tab', 'data'),
    prevent_initial_call=True,
)
def refresh_prices(clicks, intervals, ticket, tab):
    try:
        actor = identity(Permission.REFRESH_BLOOMBERG)
        if ctx.triggered_id == 'live-prices-refresh':
            if not clicks:
                return (no_update,) * 6
            ticket = data.submit(actor, tab)
            return ticket, ticket['tab'], no_update, False, True, 'Fetching all Bloomberg markets and EUR/USD…'
        if not ticket:
            return no_update, no_update, no_update, True, False, ''
        state = data.poll(ticket, actor)
        if state['status'] == 'complete':
            return None, no_update, state['ref'], True, False, 'All sections updated from the captured snapshot.'
        if state['status'] == 'failed':
            return None, no_update, state.get('retained_ref', no_update), True, False, state['error']
        return no_update, no_update, no_update, False, True, state['stage'] + '…'
    except (PermissionError, ValueError) as exc:
        return None, no_update, no_update, True, False, str(exc)
    except Exception:
        LOG.exception('Live Prices refresh callback failed')
        return None, no_update, no_update, True, False, 'Price refresh unavailable. Previous snapshot retained.'


@app.callback(Output('live-prices-board', 'children'), Output('live-prices-render-reference', 'data'),
              Output({'type': 'live-prices-grid-values', 'name': ALL}, 'rowData'),
              Output({'type': 'live-prices-curve-values', 'name': ALL}, 'figure'),
              Output({'type': 'live-prices-section-values', 'name': ALL}, 'children'),
              Input('live-prices-reference', 'data'), State('live-prices-render-reference', 'data'),
              State({'type': 'live-prices-grid-values', 'name': ALL}, 'id'),
              State({'type': 'live-prices-curve-values', 'name': ALL}, 'id'),
              State({'type': 'live-prices-section-values', 'name': ALL}, 'id'))
def show_snapshot(reference, rendered_reference=None, grid_ids=(), curve_ids=(), section_ids=()):
    # Dash validates each ALL output against the mounted list, including zero.
    unchanged = tuple([no_update] * len(ids or ()) for ids in (grid_ids, curve_ids, section_ids))
    if not reference:
        return EMPTY, None, *unchanged
    try:
        started = time.monotonic()
        actor = identity()
        if reference == rendered_reference:
            return no_update, no_update, *unchanged
        view = data.resolve(reference, actor)
        if view.get('methodology_version') != METHODOLOGY_VERSION:
            return html.Div('The marking methodology has changed. Click Refresh to capture Bloomberg quotes and build the new market curves.',
                            className='live-prices-empty', role='status'), None, *unchanged
        previous = None
        if rendered_reference:
            try:
                previous = data.resolve(rendered_reference, actor)
            except Exception:
                pass  # A missing old reference requires a full mount of the exact new reference.
        board, values = render_parts(view)
        groups = (grid_ids, curve_ids, section_ids)
        kinds = ('live-prices-grid-values', 'live-prices-curve-values', 'live-prices-section-values')
        mounted = (previous and previous['months'] == view['months'] and
                   all({item['name'] for item in identifiers} == set(values[kind])
                       for kind, identifiers in zip(kinds, groups)))
        LOG.info('Live Prices board render seconds=%.4f direct=%s', time.monotonic() - started, bool(mounted))
        if mounted:
            return (no_update, reference,
                    *[[values[kind][item['name']] for item in identifiers] for kind, identifiers in zip(kinds, groups)])
        return board, reference, *unchanged
    except Exception:
        LOG.exception('Live Prices snapshot could not be resolved')
        return html.Div('This snapshot is unavailable or expired. Click Refresh to capture a new one.',
                        className='live-prices-empty', role='alert'), None, *unchanged
