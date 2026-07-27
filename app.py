import dash_bootstrap_components as dbc
from dash import Dash

from health import register_health_routes


app = Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
)
server = app.server
register_health_routes(server)
