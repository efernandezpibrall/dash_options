# index.py
import os
from datetime import datetime
from urllib.parse import parse_qs

from dash import html, dcc, clientside_callback
from dash.dependencies import Input, Output
from app import app
from source_status import load_dashboard_source_statuses, summarize_alignment

import pages.greeks
import pages.valuation
import pages.trades
import pages.vol_surface
import pages.vol_calibration
import pages.brent_vol_history
import pages.ice_chat_quotes
import pages.prices
import pages.pricer
import pages.correlations
import pages.scenarios
import pages.pnl_explain


server = app.server


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


# Professional Navigation Bar - Options Dashboard
nav_links = html.Header([
    html.Div([
        # Main Navigation Section
        html.Nav([
            # Primary navigation - Greeks as focal point
            dcc.Link('Greeks', href='/greeks', 
                    id='nav-greeks', className='nav-link-primary'),
            
            # Secondary navigation group
            html.Div([
                dcc.Link(
                    'Valuation',
                    href='/valuation',
                    id='nav-valuation',
                    className='nav-link-secondary',
                ),
                dcc.Link(
                    'Trades',
                    href='/trades',
                    id='nav-trades',
                    className='nav-link-secondary',
                ),
                dcc.Link(
                    'Underlying Prices',
                    href='/prices',
                    id='nav-prices',
                    className='nav-link-secondary',
                ),
                dcc.Link(
                    'Volatility Surface',
                    href='/vol_surface',
                    id='nav-vol-surface',
                    className='nav-link-secondary',
                ),
                dcc.Link(
                    'Vol Calibration',
                    href='/vol_calibration?product=ttf',
                    id='nav-vol-calibration',
                    className='nav-link-secondary',
                ) if pages.vol_calibration.calibration_enabled() else None,
                dcc.Link(
                    'Vol Trades',
                    href='/brent_vol_history',
                    id='nav-brent-vol-history',
                    className='nav-link-secondary',
                ),
                dcc.Link(
                    'ICE Quotes',
                    href='/ice_chat_quotes',
                    id='nav-ice-chat-quotes',
                    className='nav-link-secondary',
                ),
                dcc.Link(
                    'Correlations',
                    href='/correlations',
                    id='nav-correlations',
                    className='nav-link-secondary',
                ),
                dcc.Link(
                    'Scenarios',
                    href='/scenarios',
                    id='nav-scenarios',
                    className='nav-link-secondary',
                ),
            ], className='nav-group-secondary'),

            # Terminal workflow - visually separated and always last
            html.Div(
                dcc.Link(
                    'Pricer',
                    href='/pricer',
                    id='nav-pricer',
                    className='nav-link-secondary',
                ),
                className='nav-group-pricer',
            ),
        ], className='main-navigation'),
        
        # Professional Controls Section - Options Dashboard specific
        html.Div([
            html.Button('Refresh Options Data', id='refresh-options-data', className='btn-refresh')
        ], className='top-bar-controls')
    ], className='top-bar-content')
], className='top-bar-header')

app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    dcc.Store(id='dashboard-source-status-store'),
    nav_links,
    html.Div(id='nav-active-sink', style={'display': 'none'}),
    html.Div(id='dashboard-source-status-banner', className='dashboard-source-status-banner'),
    html.Div(id='page-content')
])


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


@app.callback(
    Output('dashboard-source-status-store', 'data'),
    Input('refresh-options-data', 'n_clicks'),
)
def update_dashboard_source_status_store(refresh_clicks):
    return load_dashboard_source_statuses(force=bool(refresh_clicks))


@app.callback(
    Output('dashboard-source-status-banner', 'children'),
    Output('dashboard-source-status-banner', 'className'),
    Input('dashboard-source-status-store', 'data'),
    Input('url', 'pathname'),
    Input('url', 'search'),
)
def render_dashboard_source_status(statuses, pathname, search=None):
    base_class = 'dashboard-source-status-banner'
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
            '/brent_vol_history',
            '/ice_chat_quotes',
        )
        or (
            pathname == '/valuation'
            and valuation_view not in {'pnl-explain', 'pnl_explain'}
        )
    )
    if status_is_inline:
        return None, f'{base_class} dashboard-source-status-banner-hidden'
    if not statuses:
        return None, base_class
    return _build_dashboard_source_status(statuses), base_class


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
    if pathname == '/':
        return pages.greeks.layout
    elif pathname == '/greeks':
        return pages.greeks.layout
    elif pathname == '/valuation':
        return _valuation_workspace(search)
    elif pathname == '/trades':
        return pages.trades.layout
    elif pathname == '/vol_surface':
        return pages.vol_surface.layout
    elif pathname == '/vol_calibration' and pages.vol_calibration.calibration_enabled():
        return pages.vol_calibration.create_layout(search)
    elif pathname == '/brent_vol_history':
        return pages.brent_vol_history.layout
    elif pathname == '/ice_chat_quotes':
        return pages.ice_chat_quotes.layout
    elif pathname == '/prices':
        return pages.prices.layout
    elif pathname == '/pricer':
        return pages.pricer.layout
    elif pathname == '/correlations':
        return pages.correlations.layout
    elif pathname == '/scenarios':
        return pages.scenarios.layout
    elif pathname == '/pnl_explain':
        return _valuation_workspace(search, default_view='pnl-explain')
    else:
        return '404 - Page not found'

# Clientside callback for active navigation states
clientside_callback(
    """
    function(pathname) {
        // Remove active class from all nav links
        var allLinks = document.querySelectorAll('.nav-link-primary, .nav-link-secondary');
        allLinks.forEach(function(link) {
            link.classList.remove('active');
        });
        
        // Add active class based on current pathname
        var activeLink = null;
        if (pathname === '/greeks' || pathname === '/') {
            activeLink = document.getElementById('nav-greeks');
        } else {
            var linkMap = {
                '/valuation': 'nav-valuation',
                '/trades': 'nav-trades',
                '/prices': 'nav-prices',
                '/vol_surface': 'nav-vol-surface',
                '/vol_calibration': 'nav-vol-calibration',
                '/brent_vol_history': 'nav-brent-vol-history',
                '/ice_chat_quotes': 'nav-ice-chat-quotes',
                '/correlations': 'nav-correlations',
                '/scenarios': 'nav-scenarios',
                '/pnl_explain': 'nav-valuation',
                '/pricer': 'nav-pricer'
            };
            
            if (linkMap[pathname]) {
                activeLink = document.getElementById(linkMap[pathname]);
            }
        }
        
        if (activeLink) {
            activeLink.classList.add('active');
        }
        
        return '';
    }
    """,
    Output('nav-active-sink', 'children'),
    [Input('url', 'pathname')]
)

app.validation_layout = html.Div([
    app.layout,
    pages.greeks.layout,
    pages.valuation.layout,
    pages.trades.layout,
    pages.vol_surface.layout,
    pages.vol_calibration.validation_layout(),
    pages.brent_vol_history.layout,
    pages.ice_chat_quotes.layout,
    pages.prices.layout,
    pages.pricer.layout,
    pages.correlations.layout,
    pages.scenarios.layout,
    pages.pnl_explain.layout,
])

if __name__ == '__main__':
    debug_enabled = os.getenv('DASH_DEBUG', '').lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug_enabled, port=int(os.getenv('DASH_PORT', '8071')))
