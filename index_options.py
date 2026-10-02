"""Options dashboard shell, page registry and shared navigation callbacks."""
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from urllib.parse import parse_qs

from dash import html, dcc, clientside_callback
from dash.dependencies import Input, Output
from flask import redirect, request
from app import app
from source_status import load_dashboard_source_statuses, summarize_alignment

import pages.greeks
import pages.valuation
import pages.trades
import pages.vol_surface
import pages.brent_vol_history
import pages.ice_chat_quotes
import pages.prices
import pages.pricer
import pages.correlations
import pages.scenarios
import pages.pnl_explain
from vol_calibration.pages import hh_governed, jkm, ttf
from vol_calibration.brent_single_workspace import layout as brent_single_layout
import vol_calibration.session_callbacks  # noqa: F401 - Register Vol Trades table persistence.


server = app.server


@server.route('/brent_vol_history')
def redirect_renamed_vol_trades():
    """Keep existing Vol Trades bookmarks and query parameters usable."""
    query = request.query_string.decode('latin-1')
    target = '/vol_trades' + (f'?{query}' if query else '')
    return redirect(target, code=302)


@server.route('/vol_calibration')
def redirect_retired_vol_calibration():
    """Keep old bookmarks usable after removing the standalone page."""
    return redirect('/vol_trades', code=302)


@server.route('/ice_chat_quotes')
def redirect_retired_ice_quotes():
    """Keep ICE Quotes bookmarks on their new Vol Trades section."""
    return redirect('/vol_trades#ice-quotes', code=302)


@dataclass(frozen=True)
class PageDefinition:
    """One owner for a page's routing, navigation and validation metadata."""

    path: str
    title: str
    layout: object
    nav_id: str
    aliases: tuple = ()
    nav_group: Optional[str] = 'secondary'
    workspace_view: Optional[str] = None
    validation_order: int = 0
    validation_after: tuple = ()


PAGE_REGISTRY = (
    PageDefinition('/greeks', 'Greeks', pages.greeks.layout, 'nav-greeks',
                   aliases=('/',), nav_group='primary', validation_order=0),
    PageDefinition('/valuation', 'Valuation', pages.valuation.layout, 'nav-valuation',
                   workspace_view='valuation', validation_order=1),
    PageDefinition('/trades', 'Trades', pages.trades.layout, 'nav-trades', validation_order=2),
    PageDefinition('/prices', 'Underlying Prices', pages.prices.layout, 'nav-prices', validation_order=5),
    PageDefinition('/vol_surface', 'Volatility Surface', pages.vol_surface.layout, 'nav-vol-surface',
                   validation_order=3, validation_after=(
                       brent_single_layout, hh_governed.layout, jkm.layout, ttf.layout,
                       dcc.Store(id='vol-calibration-session-state', storage_type='session'),
                   )),
    PageDefinition('/vol_trades', 'Vol Trades', pages.brent_vol_history.layout,
                   'nav-brent-vol-history', validation_order=4),
    PageDefinition('/correlations', 'Correlations', pages.correlations.layout, 'nav-correlations',
                   validation_order=7),
    PageDefinition('/scenarios', 'Scenarios', pages.scenarios.layout, 'nav-scenarios', validation_order=8),
    PageDefinition('/pricer', 'Pricer', pages.pricer.layout, 'nav-pricer',
                   nav_group='pricer', validation_order=6),
    PageDefinition('/pnl_explain', 'P&L Explain', pages.pnl_explain.layout, 'nav-valuation',
                   nav_group=None, workspace_view='pnl-explain', validation_order=9),
)
PAGES_BY_PATH = {
    path: page
    for page in PAGE_REGISTRY
    for path in (page.path, *page.aliases)
}
PAGE_TITLES = {path: page.title for path, page in PAGES_BY_PATH.items()}
PAGE_NOT_FOUND_TITLE = 'Page Not Found'
PNL_EXPLAIN_VIEWS = ('pnl-explain', 'pnl_explain')
STATIC_PAGE_LAYOUTS = {
    path: page.layout
    for path, page in PAGES_BY_PATH.items()
    if page.workspace_view is None
}
NAV_LINK_IDS = {path: page.nav_id for path, page in PAGES_BY_PATH.items()}


def _valuation_workspace(search=None, *, default_view='valuation'):
    query = parse_qs((search or '').lstrip('?'))
    requested_view = query.get('view', [default_view])[0]
    active_view = (
        'pnl-explain'
        if requested_view in {'pnl-explain', 'pnl_explain'}
        else 'valuation'
    )

    def workspace_link(label, href, view):
        class_name = 'valuation-workspace-tab'
        if active_view == view:
            class_name += ' active'
        return html.A(
            label,
            href=href,
            className=class_name,
            **({'aria-current': 'page'} if active_view == view else {}),
        )

    return html.Div(
        [
            html.Nav(
                [
                    workspace_link('Valuation', '/valuation', 'valuation'),
                    workspace_link(
                        'P&L Explain',
                        '/valuation?view=pnl-explain',
                        'pnl-explain',
                    ),
                ],
                className='valuation-workspace-tabs',
                **{'aria-label': 'Valuation views'},
            ),
            (
                pages.pnl_explain.layout
                if active_view == 'pnl-explain'
                else pages.valuation.layout
            ),
        ],
        className='valuation-workspace',
        **{'data-active-view': active_view},
    )


def _navigation_links(group):
    return [
        dcc.Link(
            page.title,
            href=page.path,
            id=page.nav_id,
            className=(
                'nav-link-primary' if group == 'primary' else 'nav-link-secondary'
            ),
        )
        for page in PAGE_REGISTRY
        if page.nav_group == group
    ]


nav_links = html.Header(
    [
        html.Div(
            [
                html.Nav(
                    [
                        *_navigation_links('primary'),
                        html.Div(_navigation_links('secondary'), className='nav-group-secondary'),
                        html.Div(_navigation_links('pricer'), className='nav-group-pricer'),
                    ],
                    className='main-navigation',
                ),
                html.Div(
                    [html.Button('Refresh Options Data', id='refresh-options-data', className='btn-refresh')],
                    className='top-bar-controls',
                ),
            ],
            className='top-bar-content',
        ),
    ],
    className='top-bar-header',
)

pricer_global_valuation_control = html.Div(
    [
        html.Label('Valuation', className='pricer-field-label'),
        dcc.DatePickerSingle(
            id='pricer-global-valuation-date',
            date=date.today().isoformat(),
            display_format='YYYY-MM-DD',
            clearable=False,
            persistence=False,
            className='pricer-date-picker pricer-global-valuation-picker',
        ),
    ],
    id='pricer-global-valuation-control',
    className=(
        'pricer-field pricer-global-valuation-control '
        'pricer-global-valuation-control-hidden'
    ),
)

app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    dcc.Store(id='dashboard-source-status-store'),
    dcc.Interval(
        id='pricer-valuation-today-ticker',
        interval=60_000,
        n_intervals=0,
    ),
    nav_links,
    html.Div(id='nav-active-sink', style={'display': 'none'}),
    html.Div(
        [
            pricer_global_valuation_control,
            html.Div(
                id='dashboard-source-status-banner',
                className='dashboard-source-status-banner',
            ),
        ],
        id='dashboard-source-status-row',
        className='dashboard-source-status-row',
    ),
    html.Div(id='page-content')
])


@app.callback(
    Output('pricer-global-valuation-control', 'className'),
    Output('pricer-global-valuation-date', 'date'),
    Input('url', 'pathname'),
)
def configure_pricer_global_valuation(pathname):
    base_class = 'pricer-field pricer-global-valuation-control'
    if pathname == '/pricer':
        return f'{base_class} pricer-global-valuation-control-visible', date.today().isoformat()
    return f'{base_class} pricer-global-valuation-control-hidden', date.today().isoformat()


def _compact_source_date(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').strftime('%d %b')
    except (TypeError, ValueError):
        return str(value)


def _build_dashboard_source_status(statuses, *, compact=False):
    alignment = summarize_alignment(statuses)
    chips = []
    accessible_parts = []
    compact_labels = {
        'Vol Surface': 'Vol',
        'Forward Curves': 'Curves',
    }
    for status in statuses:
        if status.get('label') == 'Portfolio':
            continue
        if status.get('error'):
            detail = 'unavailable'
        elif status.get('latest_cob'):
            detail = (
                _compact_source_date(status['latest_cob'])
                if compact
                else status['latest_cob']
            )
        else:
            detail = 'no COB'
        label = (
            compact_labels.get(status['label'], status['label'])
            if compact
            else status['label']
        )
        chips.append(
            html.Span(
                [html.Strong(f'{label}: '), detail],
                className='dashboard-source-status-chip',
                title=(
                    f"{status['label']}: {status.get('latest_cob') or detail} · "
                    f"{status.get('source') or 'source unavailable'}"
                ),
            )
        )
        accessible_parts.append(
            f"{status['label']}: {status.get('latest_cob') or detail}"
        )

    if alignment['error_labels']:
        headline = 'Source unavailable'
    elif alignment['misaligned']:
        headline = (
            'COB mismatch'
            if compact
            else 'COB mismatch — comparisons may mix market dates'
        )
    elif alignment['stale_labels']:
        headline = 'Stale data' if compact else 'Stale market data'
    else:
        headline = 'Sources aligned'
    class_name = (
        'dashboard-source-status-content '
        f"dashboard-source-status-{alignment['tone']}"
    )
    if compact:
        class_name += ' greeks-source-status-content'
    children = [
        html.Span(headline, className='dashboard-source-status-headline'),
        *chips,
    ]
    extra_props = {}
    if compact:
        status_text = ' · '.join([headline, *accessible_parts])
        tone_icon = {
            'success': '✓',
            'warning': '!',
            'danger': '×',
        }[alignment['tone']]
        children.insert(
            0,
            html.Span(
                tone_icon,
                className='greeks-source-status-icon',
                **{'aria-hidden': 'true'},
            ),
        )
        extra_props = {
            'title': status_text,
            'role': 'status',
            'tabIndex': 0,
            'aria-label': status_text,
            'aria-live': 'polite',
        }
    return html.Div(
        children,
        className=class_name,
        **extra_props,
    )


def _coerce_iso_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).split('T', 1)[0]).isoformat()
    except (TypeError, ValueError):
        return None


def _build_pricer_valuation_warning(statuses, valuation_date):
    today_iso = date.today().isoformat()
    selected_iso = _coerce_iso_date(valuation_date)
    selected_label = selected_iso or str(valuation_date or 'not selected')
    warning_text = (
        f'Warning: valuation date {selected_label} is not today. '
        f'Today is {today_iso}.'
    )
    source_children = []
    accessible_parts = [warning_text]
    if statuses:
        source_status = _build_dashboard_source_status(statuses)
        source_children.append(
            html.Span(
                source_status.children[0].children,
                className=(
                    'dashboard-source-status-chip '
                    'dashboard-source-status-context'
                ),
            )
        )
        source_children.extend(source_status.children[1:])
        accessible_parts.append(source_status.children[0].children)
        for status in statuses:
            if status.get('label') == 'Portfolio':
                continue
            detail = (
                'unavailable'
                if status.get('error')
                else status.get('latest_cob') or 'no COB'
            )
            accessible_parts.append(f"{status['label']}: {detail}")

    return html.Div(
        [
            html.Span(
                '!',
                className='dashboard-source-status-alert-icon',
                **{'aria-hidden': 'true'},
            ),
            html.Span(
                'WARNING — VALUATION DATE IS NOT TODAY',
                className='dashboard-source-status-headline',
            ),
            html.Span(
                f'Selected: {selected_label} · Today: {today_iso}',
                className='dashboard-source-status-alert-detail',
            ),
            *source_children,
        ],
        className=(
            'dashboard-source-status-content dashboard-source-status-danger '
            'dashboard-source-status-valuation-warning'
        ),
        title=warning_text,
        role='alert',
        tabIndex=0,
        **{
            'aria-label': ' · '.join(accessible_parts),
            'aria-live': 'assertive',
            'aria-atomic': 'true',
        },
    )


@app.callback(
    Output('dashboard-source-status-store', 'data'),
    Input('refresh-options-data', 'n_clicks'),
)
def update_dashboard_source_status_store(refresh_clicks):
    return load_dashboard_source_statuses(force=bool(refresh_clicks))


@app.callback(
    Output('dashboard-source-status-banner', 'children'),
    Output('dashboard-source-status-banner', 'className'),
    Output('dashboard-source-status-row', 'className'),
    Input('dashboard-source-status-store', 'data'),
    Input('url', 'pathname'),
    Input('url', 'search'),
    Input('pricer-global-valuation-date', 'date'),
    Input('pricer-valuation-today-ticker', 'n_intervals'),
)
def render_dashboard_source_status(
    statuses,
    pathname,
    search=None,
    valuation_date=None,
    _today_tick=None,
):
    base_class = 'dashboard-source-status-banner'
    row_class = 'dashboard-source-status-row'
    is_pricer = pathname == '/pricer'
    valuation_date_is_not_today = (
        is_pricer
        and _coerce_iso_date(valuation_date) != date.today().isoformat()
    )
    if is_pricer:
        row_class += ' dashboard-source-status-row-pricer'
    if valuation_date_is_not_today:
        row_class += ' dashboard-source-status-row-pricer-valuation-warning'
    valuation_view = parse_qs((search or '').lstrip('?')).get(
        'view',
        ['valuation'],
    )[0]
    status_is_inline = (
        pathname in (
            None,
            '/',
            '/greeks',
            '/trades',
            '/vol_trades',
        )
        or (
            pathname == '/valuation'
            and valuation_view not in {'pnl-explain', 'pnl_explain'}
        )
    )
    if status_is_inline:
        return (
            None,
            f'{base_class} dashboard-source-status-banner-hidden',
            row_class,
        )
    if valuation_date_is_not_today:
        return (
            _build_pricer_valuation_warning(statuses, valuation_date),
            base_class,
            row_class,
        )
    if not statuses:
        return None, base_class, row_class
    return _build_dashboard_source_status(statuses), base_class, row_class


@app.callback(
    Output('greeks-source-status-inline', 'children'),
    Input('dashboard-source-status-store', 'data'),
    Input('greeks-source-status-mount', 'data'),
)
def render_greeks_source_status(statuses, _mounted):
    if not statuses:
        return None
    return _build_dashboard_source_status(statuses, compact=True)


@app.callback(
    Output('valuation-source-status-inline', 'children'),
    Input('dashboard-source-status-store', 'data'),
    Input('valuation-source-status-mount', 'data'),
    Input('valuation-aspect-source-status', 'data'),
)
def render_valuation_source_status(statuses, _mounted, aspect_status=None):
    if not statuses:
        return None
    valuation_statuses = list(statuses)
    if aspect_status:
        valuation_statuses.append(aspect_status)
    return _build_dashboard_source_status(valuation_statuses, compact=True)


@app.callback(
    Output('trades-dashboard-source-status-inline', 'children'),
    Input('dashboard-source-status-store', 'data'),
    Input('trades-source-status-mount', 'data'),
)
def render_trades_dashboard_source_status(statuses, _mounted):
    if not statuses:
        return None
    return _build_dashboard_source_status(statuses, compact=True)


@app.callback(
    Output('brent-vol-history-market-data-status', 'children'),
    Input('dashboard-source-status-store', 'data'),
    Input('brent-vol-history-source-status-mount', 'data'),
)
def render_brent_vol_history_source_status(statuses, _mounted):
    if not statuses:
        return None
    return _build_dashboard_source_status(statuses, compact=True)

# Callback to handle page routing
@app.callback(
    Output('page-content', 'children'),
    Input('url', 'pathname'),
    Input('url', 'search'),
)
def display_page(pathname, search):
    page = PAGES_BY_PATH.get(pathname)
    if page is not None and page.workspace_view is not None:
        return _valuation_workspace(search, default_view=page.workspace_view)
    if pathname == '/brent_vol_history':
        return dcc.Location(
            id='renamed-vol-trades-redirect',
            pathname='/vol_trades',
            search=search or '',
        )
    if pathname == '/vol_calibration':
        return dcc.Location(
            id='retired-vol-calibration-redirect',
            pathname='/vol_trades',
        )
    if pathname == '/ice_chat_quotes':
        return dcc.Location(
            id='retired-ice-chat-quotes-redirect',
            pathname='/vol_trades',
            hash='#ice-quotes',
        )
    return STATIC_PAGE_LAYOUTS.get(pathname, '404 - Page not found')

# Clientside callback for active navigation states
clientside_callback(
    f"""
    function(pathname, search) {{
        var currentPath = pathname || '/';
        var pageTitles = {json.dumps(PAGE_TITLES, sort_keys=True)};
        var pageTitle = pageTitles[currentPath] || {json.dumps(PAGE_NOT_FOUND_TITLE)};
        if (currentPath === '/valuation') {{
            var valuationView = new URLSearchParams(search || '').get('view');
            if ({json.dumps(PNL_EXPLAIN_VIEWS)}.indexOf(valuationView) !== -1) {{
                pageTitle = 'P&L Explain';
            }}
        }}
        document.title = pageTitle;

        // Remove active class from all nav links
        var allLinks = document.querySelectorAll('.nav-link-primary, .nav-link-secondary');
        allLinks.forEach(function(link) {{
            link.classList.remove('active');
        }});
        
        // Add active class based on current pathname
        var linkMap = {json.dumps(NAV_LINK_IDS, sort_keys=True)};
        var activeLink = linkMap[currentPath]
            ? document.getElementById(linkMap[currentPath])
            : null;
        
        if (activeLink) {{
            activeLink.classList.add('active');
        }}
        
        return '';
    }}
    """,
    Output('nav-active-sink', 'children'),
    [Input('url', 'pathname'), Input('url', 'search')]
)

app.validation_layout = html.Div([
    app.layout,
    *[
        component
        for page in sorted(PAGE_REGISTRY, key=lambda page: page.validation_order)
        for component in (page.layout, *page.validation_after)
    ],
])

if __name__ == '__main__':
    debug_enabled = os.getenv('DASH_DEBUG', '').lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug_enabled, port=int(os.getenv('DASH_PORT', '8071')))
