# index.py
import os

from dash import html, dcc, clientside_callback
from dash.dependencies import Input, Output
from app import app
from source_status import load_dashboard_source_statuses, summarize_alignment

import pages.greeks
import pages.valuation
import pages.trades
import pages.vol_surface
import pages.vol_calibration
import pages.prices
import pages.slopes
import pages.spreads
import pages.pricer
import pages.correlations
import pages.scenarios
import pages.pnl_explain

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
                dcc.Link('Valuation', href='/valuation', className='nav-link-secondary'),
                dcc.Link('Trades', href='/trades', className='nav-link-secondary'),
                dcc.Link('Volatility Surface', href='/vol_surface', className='nav-link-secondary'),
                dcc.Link(
                    'Vol Calibration',
                    href='/vol_calibration?product=ttf',
                    className='nav-link-secondary',
                ) if pages.vol_calibration.calibration_enabled() else None,
                dcc.Link('Underlying Prices', href='/prices', className='nav-link-secondary'),
                dcc.Link('Slopes', href='/slopes', className='nav-link-secondary'),
                dcc.Link('Spreads', href='/spreads', className='nav-link-secondary'),
                dcc.Link('Pricer', href='/pricer', className='nav-link-secondary'),
                dcc.Link('Correlations', href='/correlations', className='nav-link-secondary'),
                dcc.Link('Scenarios', href='/scenarios', className='nav-link-secondary'),
                dcc.Link('P&L Explain', href='/pnl_explain', className='nav-link-secondary'),
            ], className='nav-group-secondary')
        ], className='main-navigation'),
        
        # Professional Controls Section - Options Dashboard specific
        html.Div([
            html.Button('Refresh Options Data', id='refresh-options-data', className='btn-refresh')
        ], className='top-bar-controls')
    ], className='top-bar-content')
], className='top-bar-header')

app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    nav_links,
    html.Div(id='nav-active-sink', style={'display': 'none'}),
    html.Div(id='dashboard-source-status-banner', className='dashboard-source-status-banner'),
    html.Div(id='page-content')
])


@app.callback(
    Output('dashboard-source-status-banner', 'children'),
    Input('url', 'pathname'),
    Input('refresh-options-data', 'n_clicks'),
)
def update_dashboard_source_status(pathname, refresh_clicks):
    del pathname, refresh_clicks
    statuses = load_dashboard_source_statuses()
    alignment = summarize_alignment(statuses)
    chips = []
    for status in statuses:
        if status.get('error'):
            detail = 'unavailable'
        elif status.get('latest_cob'):
            age = status.get('business_day_age')
            detail = f"{status['latest_cob']} · {age}bd old"
        else:
            detail = 'no COB'
        chips.append(
            html.Span(
                [html.Strong(f"{status['label']}: "), detail],
                className='dashboard-source-status-chip',
                title=status.get('source'),
            )
        )

    if alignment['error_labels']:
        headline = 'Source unavailable'
    elif alignment['misaligned']:
        headline = 'COB mismatch — comparisons may mix market dates'
    elif alignment['stale_labels']:
        headline = 'Stale market data'
    else:
        headline = 'Sources aligned'
    return html.Div(
        [html.Span(headline, className='dashboard-source-status-headline'), *chips],
        className=f"dashboard-source-status-content dashboard-source-status-{alignment['tone']}",
    )

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
        return pages.valuation.layout
    elif pathname == '/trades':
        return pages.trades.layout
    elif pathname == '/vol_surface':
        return pages.vol_surface.layout
    elif pathname == '/vol_calibration' and pages.vol_calibration.calibration_enabled():
        return pages.vol_calibration.create_layout(search)
    elif pathname == '/prices':
        return pages.prices.layout
    elif pathname == '/slopes':
        return pages.slopes.layout
    elif pathname == '/spreads':
        return pages.spreads.layout
    elif pathname == '/pricer':
        return pages.pricer.layout
    elif pathname == '/correlations':
        return pages.correlations.layout
    elif pathname == '/scenarios':
        return pages.scenarios.layout
    elif pathname == '/pnl_explain':
        return pages.pnl_explain.layout
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
                '/valuation': 'Valuation',
                '/trades': 'Trades',
                '/vol_surface': 'Volatility Surface',
                '/vol_calibration': 'Vol Calibration',
                '/prices': 'Underlying Prices',
                '/slopes': 'Slopes',
                '/spreads': 'Spreads',
                '/pricer': 'Pricer',
                '/correlations': 'Correlations',
                '/scenarios': 'Scenarios',
                '/pnl_explain': 'P&L Explain'
            };
            
            if (linkMap[pathname]) {
                allLinks.forEach(function(link) {
                    if (link.innerText === linkMap[pathname]) {
                        activeLink = link;
                    }
                });
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
    pages.prices.layout,
    pages.slopes.layout,
    pages.spreads.layout,
    pages.pricer.layout,
    pages.correlations.layout,
    pages.scenarios.layout,
    pages.pnl_explain.layout,
])

if __name__ == '__main__':
    debug_enabled = os.getenv('DASH_DEBUG', '').lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug_enabled, port=int(os.getenv('DASH_PORT', '8071')))
