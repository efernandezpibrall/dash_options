# index.py
import os

from dash import html, dcc, clientside_callback
from dash.dependencies import Input, Output
from app import app

import pages.greeks
import pages.valuation
import pages.trades
import pages.vol_surface
import pages.prices
import pages.slopes
import pages.spreads
import pages.pricer

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
                dcc.Link('Underlying Prices', href='/prices', className='nav-link-secondary'),
                dcc.Link('Slopes', href='/slopes', className='nav-link-secondary'),
                dcc.Link('Spreads', href='/spreads', className='nav-link-secondary'),
                dcc.Link('Pricer', href='/pricer', className='nav-link-secondary'),
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
    html.Div(id='page-content')
])

# Callback to handle page routing
@app.callback(Output('page-content', 'children'),
              Input('url', 'pathname'))
def display_page(pathname):
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
    elif pathname == '/prices':
        return pages.prices.layout
    elif pathname == '/slopes':
        return pages.slopes.layout
    elif pathname == '/spreads':
        return pages.spreads.layout
    elif pathname == '/pricer':
        return pages.pricer.layout
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
                '/prices': 'Underlying Prices',
                '/slopes': 'Slopes',
                '/spreads': 'Spreads',
                '/pricer': 'Pricer'
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
    Output('url', 'search'),  # Dummy output
    [Input('url', 'pathname')]
)

if __name__ == '__main__':
    debug_enabled = os.getenv('DASH_DEBUG', '').lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug_enabled, port=int(os.getenv('DASH_PORT', '8071')))
