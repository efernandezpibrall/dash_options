"""
Navigation bar component with commodity tabs.

Implements Framework Section 4.1: Horizontal tabs for BRENT | HH | TTF | JKM
"""
import dash_bootstrap_components as dbc
from dash import html


# Commodity configuration
COMMODITIES = [
    {'id': 'brent', 'name': 'BRENT', 'color': 'danger'},
    {'id': 'hh', 'name': 'HH', 'color': 'success'},
    {'id': 'ttf', 'name': 'TTF', 'color': 'primary'},
    {'id': 'jkm', 'name': 'JKM', 'color': 'warning'},
]


def create_nav_bar(active_commodity: str = 'ttf') -> dbc.Navbar:
    """
    Create the navigation bar with commodity tabs.

    Parameters
    ----------
    active_commodity : str
        Currently active commodity tab (default: 'ttf')

    Returns
    -------
    dbc.Navbar
        Navigation bar component
    """
    nav_items = []

    for commodity in COMMODITIES:
        is_active = commodity['id'] == active_commodity.lower()
        nav_items.append(
            dbc.NavItem(
                dbc.NavLink(
                    commodity['name'],
                    href=f"/{commodity['id']}",
                    active=is_active,
                    className=f"nav-link-{commodity['color']}" if is_active else "",
                    style={
                        'fontWeight': 'bold' if is_active else 'normal',
                        'borderBottom': f'3px solid var(--bs-{commodity["color"]})' if is_active else 'none',
                        'padding': '0.5rem 1rem',
                    }
                ),
            )
        )

    navbar = dbc.Navbar(
        dbc.Container(
            [
                dbc.NavbarBrand(
                    [
                        html.Span("Wing Model", style={'fontWeight': 'bold'}),
                        html.Span(" Vol Surface", style={'fontWeight': 'normal', 'opacity': '0.8'}),
                    ],
                    href="/",
                    className="me-4",
                ),
                dbc.Nav(
                    nav_items,
                    className="me-auto",
                    navbar=True,
                ),
                dbc.Nav(
                    [
                        dbc.NavItem(
                            dbc.NavLink(
                                html.I(className="fas fa-question-circle me-1"),
                                href="#",
                                id="help-link",
                            )
                        ),
                    ],
                    className="ms-auto",
                    navbar=True,
                ),
            ],
            fluid=True,
        ),
        color="dark",
        dark=True,
        className="mb-3",
    )

    return navbar


def create_commodity_tabs(active_commodity: str = 'ttf') -> dbc.Tabs:
    """
    Create commodity tabs for inline use (alternative to navbar).

    Parameters
    ----------
    active_commodity : str
        Currently active commodity

    Returns
    -------
    dbc.Tabs
        Tab component
    """
    tabs = []
    for commodity in COMMODITIES:
        tabs.append(
            dbc.Tab(
                label=commodity['name'],
                tab_id=commodity['id'],
                label_style={'fontWeight': 'bold'},
                active_label_style={
                    'fontWeight': 'bold',
                    'borderBottom': f'3px solid var(--bs-{commodity["color"]})'
                }
            )
        )

    return dbc.Tabs(
        tabs,
        id='commodity-tabs',
        active_tab=active_commodity.lower(),
        className="mb-3",
    )
