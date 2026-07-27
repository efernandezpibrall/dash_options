"""
Main entry point for Vol Surface Calibration dashboard.

Implements Framework Section 4.1:
- Horizontal commodity tabs: BRENT | HH | TTF | JKM
- One dedicated page per commodity
- Default to TTF (most commonly used)

Run with: python3 index.py
Navigate to: http://localhost:8056
"""
import sys
from pathlib import Path

# Add parent directory to path for calibration_engine imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import dcc, html
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output

from app import app

# Import commodity page layouts
from pages import ttf, brent, hh, jkm

# Import navigation component
from components.nav_bar import create_nav_bar


# App layout with commodity-based navigation
app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    html.Div(id='navbar-container'),
    dbc.Container(
        id='page-content',
        fluid=True,
        className="mt-3"
    )
])


@app.callback(
    [Output('navbar-container', 'children'),
     Output('page-content', 'children')],
    Input('url', 'pathname')
)
def display_page(pathname):
    """Route to appropriate commodity page based on URL."""

    # Default to TTF if no path or root
    if pathname is None or pathname == '/' or pathname == '':
        return create_nav_bar('ttf'), ttf.layout

    # Route to commodity pages
    pathname_lower = pathname.lower().strip('/')

    if pathname_lower == 'ttf':
        return create_nav_bar('ttf'), ttf.layout

    elif pathname_lower == 'brent':
        return create_nav_bar('brent'), brent.layout

    elif pathname_lower == 'hh':
        return create_nav_bar('hh'), hh.layout

    elif pathname_lower == 'jkm':
        return create_nav_bar('jkm'), jkm.layout

    else:
        # 404 page
        return create_nav_bar('ttf'), html.Div([
            dbc.Container([
                dbc.Row([
                    dbc.Col([
                        html.H3("404 - Page Not Found", className="text-danger"),
                        html.P(f"The requested page '{pathname}' does not exist."),
                        html.P("Available pages: /ttf, /brent, /hh, /jkm"),
                        dbc.Button("Go to TTF", href="/ttf", color="primary", className="mt-3")
                    ], width=6)
                ], justify="center", className="mt-5")
            ])
        ])


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8056)
