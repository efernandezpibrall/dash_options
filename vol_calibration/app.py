"""
Dash Vol Surface Calibration Application.

Wing Model volatility surface calibration dashboard.
"""
import dash
import dash_bootstrap_components as dbc

# Create Dash app instance
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="Vol Surface Calibration",
    meta_tags=[
        {"name": "viewport", "content": "width=device-width, initial-scale=1"}
    ]
)

server = app.server
