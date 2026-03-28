# pages/pricer.py
from dash import html, dcc, callback, Output, Input, State, Dash, ALL, ctx
import plotly.graph_objects as go
import dash_bootstrap_components as dbc
import datetime as dt
from datetime import date, timedelta
import numpy as np

# Import options pricing functions
from options.options_library import (black_76, kirk_model_with_substitution, kirk_spread_greeks)

# Cache for storing calculation results to maintain consistency between results and chart
option_cache = {
    "black76": {"value": None, "params": None},
    "kirk": {"value": None, "params": None}
}

# Define option types
option_types = [
    {"label": "Commodity Options (Black-76)", "value": "black76"},
    {"label": "Spread Options (Kirk)", "value": "kirk"}
]


# Helper function to parse dates consistently
def parse_date(date_str, default_date=None):
    """Parse date string with consistent error handling"""
    if not date_str:
        return default_date or date.today() + timedelta(days=365)

    if isinstance(date_str, date):
        return date_str

    try:
        return dt.datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        try:
            return dt.datetime.strptime(date_str.split('T')[0], '%Y-%m-%d').date()
        except (ValueError, AttributeError):
            return default_date or date.today() + timedelta(days=365)


# Create app layout
layout = html.Div([
    # Professional Header Section
    html.Div([
        html.H3("Option Pricing Calculator", className="section-title-inline"),
        html.Label("Option Type:", className="inline-filter-label"),
        dcc.Dropdown(
            id="option-type",
            options=option_types,
            value="black76",
            clearable=False,
            className="inline-dropdown"
        )
    ], className="inline-section-header"),

    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Option Configuration"),
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Label("Call/Put"),
                            dcc.RadioItems(
                                id="option-call-put",
                                options=[
                                    {"label": "Call", "value": "C"},
                                    {"label": "Put", "value": "P"}
                                ],
                                value="C",
                                inline=True
                            )
                        ], width=6)
                    ]),

                    html.Div(id="parameters-container", className="mt-3"),
                ])
            ], className="mb-4")
        ], width=6),

        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Results"),
                dbc.CardBody([
                    html.Div("Click Calculate to see results", id="results-container")
                ])
            ], className="mb-4"),

            dbc.Card([
                dbc.CardHeader("Greeks"),
                dbc.CardBody([
                    html.Div("Greeks will appear here", id="greeks-container")
                ])
            ]),

            dbc.Card([
                dbc.CardHeader("Time to Expiration"),
                dbc.CardBody([
                    html.Div("Time information will appear here", id="time-info")
                ])
            ], className="mt-4")
        ], width=6)
    ]),

    dbc.Row([
        dbc.Col([
            dbc.Button("Calculate", id="calculate-button", color="primary", className="w-100 mt-3")
        ], width={"size": 6, "offset": 3})
    ]),

    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Option Payoff Chart"),
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Label("Valuation Date"),
                            dcc.DatePickerSingle(
                                id="valuation-date",
                                min_date_allowed=date.today(),
                                initial_visible_month=date.today(),
                                date=None,
                                display_format='YYYY-MM-DD',
                                placeholder="At Expiration"
                            )
                        ], width=6),
                        dbc.Col([
                            html.Label("Price Range (%)"),
                            dcc.Slider(
                                id="price-range-slider",
                                min=10,
                                max=100,
                                step=5,
                                value=50,
                                marks={
                                    10: '10%',
                                    25: '25%',
                                    50: '50%',
                                    75: '75%',
                                    100: '100%'
                                }
                            )
                        ], width=6)
                    ], className="mb-3"),
                    dcc.Graph(id="payoff-chart", style={"height": "400px"}, figure=go.Figure().update_layout(title="Click Calculate to see payoff chart", height=400))
                ])
            ], className="mt-4")
        ], width=12)
    ]),

# Add a new Card for the volatility chart to the layout
# Insert this after the existing payoff chart in the layout section

dbc.Row([
    dbc.Col([
        dbc.Card([
            dbc.CardHeader("Volatility vs Option Price Chart"),
            dbc.CardBody([
                html.Div([
                    html.P("Visualizes how changes in volatility affect option price while keeping other parameters constant."),
                ], className="mb-3"),
                dcc.Graph(id="volatility-chart", style={"height": "400px"}, figure=go.Figure().update_layout(title="Click Calculate to see volatility chart", height=400))
            ])
        ], className="mt-4")
    ], width=6),
    dbc.Col([
        dbc.Card([
            dbc.CardHeader("Risk-Free Rate vs Option Price Chart"),
            dbc.CardBody([
                html.Div([
                    html.P("Visualizes how changes in risk-free rate affect option price while keeping other parameters constant."),
                ], className="mb-3"),
                dcc.Graph(id="rate-chart", style={"height": "400px"}, figure=go.Figure().update_layout(title="Click Calculate to see rate chart", height=400))
            ])
        ], className="mt-4")
    ], width=6)
]),

    # Add this to your layout section in pricer.py, after your existing time-chart section
# Row with Correlation and Extension charts side by side
dbc.Row([
    # Left column: Correlation Chart (Kirk only)
    dbc.Col([
        dbc.Card([
            dbc.CardHeader("Correlation vs Option Price Chart"),
            dbc.CardBody([
                html.Div([
                    html.P("Visualizes how changes in correlation affect Kirk spread option price while keeping other parameters constant."),
                    html.P("Only applicable for Kirk spread options.", className="text-muted"),
                ], className="mb-3"),
                dcc.Graph(id="correlation-chart", style={"height": "400px"}, figure=go.Figure().update_layout(title="Click Calculate to see correlation chart", height=400))
            ])
        ], className="mt-4")
    ], width=6),
    # Right column: Expiration Extension Chart
    dbc.Col([
        dbc.Card([
            dbc.CardHeader("Expiration Extension Chart"),
            dbc.CardBody([
                html.Div([
                    html.P("Visualizes how extending the expiration date affects option value while keeping other parameters constant."),
                ], className="mb-3"),
                dcc.Graph(id="extension-chart", style={"height": "400px"}, figure=go.Figure().update_layout(title="Click Calculate to see extension chart", height=400))
            ])
        ], className="mt-4")
    ], width=6)
]),

    dbc.Row([
    dbc.Col([
        dbc.Card([
            dbc.CardHeader("Time to Expiration vs Option Price Chart"),
            dbc.CardBody([
                html.Div([
                    html.P("Visualizes how option price changes over time from tomorrow to 4 months after expiration."),
                ], className="mb-3"),
                dcc.Graph(id="time-chart", style={"height": "400px"}, figure=go.Figure().update_layout(title="Click Calculate to see time decay chart", height=400))
            ])
        ], className="mt-4")
    ], width=12)
]),




], className="options-dashboard-container")


# Callback to update parameters based on option type
@callback(
    Output("parameters-container", "children"),
    Input("option-type", "value")
)
def update_parameters(option_type):
    """Update parameter form based on selected option type"""

    if option_type == "black76":
        # Parameters for Black-76 with MATCH pattern IDs
        params = [
            dbc.Row([
                dbc.Col([
                    html.Label("Underlying Price (S)"),
                    dbc.Input(id={'type': 'param', 'model': 'black76', 'param': 'underlying-price'},
                              type="number", value=100, min=0.01)
                ], width=6),
                dbc.Col([
                    html.Label("Strike Price (K)"),
                    dbc.Input(id={'type': 'param', 'model': 'black76', 'param': 'strike-price'},
                              type="number", value=100, min=0.01)
                ], width=6)
            ], className="mb-2"),

            dbc.Row([
                dbc.Col([
                    html.Label("Expiration Date"),
                    dcc.DatePickerSingle(
                        id={'type': 'param-date', 'model': 'black76', 'param': 'expiration-date'},
                        min_date_allowed=date.today(),
                        initial_visible_month=date.today(),
                        date=date.today() + timedelta(days=30),
                        display_format='YYYY-MM-DD'
                    )
                ], width=6),
                dbc.Col([
                    html.Label("Risk-Free Rate (r)"),
                    dbc.Input(id={'type': 'param', 'model': 'black76', 'param': 'risk-free-rate'},
                              type="number", value=0.05, min=-1, max=2, step=0.000001)
                ], width=6)
            ], className="mb-2"),

            dbc.Row([
                dbc.Col([
                    html.Label("Volatility (σ)"),
                    dbc.Input(id={'type': 'param', 'model': 'black76', 'param': 'volatility'},
                              type="number", value=0.2, min=0.005, max=2, step=0.0000000001)
                ], width=6)
            ], className="mb-2")
        ]

    elif option_type == "kirk":
        # Parameters for Kirk spread options with pattern-matching IDs
        params = [
            dbc.Row([
                dbc.Col([
                    html.Label("Price of Asset 1 (S1)"),
                    dbc.Input(id={'type': 'param', 'model': 'kirk', 'param': 'price-asset1'},
                              type="number", value=100, min=0.01)
                ], width=6),
                dbc.Col([
                    html.Label("Price of Asset 2 (S2)"),
                    dbc.Input(id={'type': 'param', 'model': 'kirk', 'param': 'price-asset2'},
                             type="number", value=90, min=0.01)
                ], width=6)
            ], className="mb-2"),

            dbc.Row([
                dbc.Col([
                    html.Label("Strike Price (K)"),
                    dbc.Input(id={'type': 'param', 'model': 'kirk', 'param': 'spread-strike'},
                              type="number", value=5)
                ], width=6),
                dbc.Col([
                    html.Label("Expiration Date"),
                    dcc.DatePickerSingle(
                        id={'type': 'param-date', 'model': 'kirk', 'param': 'spread-expiration-date'},
                        min_date_allowed=date.today(),
                        initial_visible_month=date.today(),
                        date=date.today() + timedelta(days=30),
                        display_format='YYYY-MM-DD'
                    )
                ], width=6)
            ], className="mb-2"),

            dbc.Row([
                dbc.Col([
                    html.Label("Risk-Free Rate (r)"),
                    dbc.Input(id={'type': 'param', 'model': 'kirk', 'param': 'spread-risk-free-rate'},
                              type="number", value=0.05, min=-1, max=2, step=0.00001)
                ], width=6)
            ], className="mb-2"),

            dbc.Row([
                dbc.Col([
                    html.Label("Volatility of Asset 1 (σ1)"),
                    dbc.Input(id={'type': 'param', 'model': 'kirk', 'param': 'volatility-asset1'},
                              type="number", value=0.2, min=0.005, max=2, step=0.0000000001)
                ], width=6),
                dbc.Col([
                    html.Label("Volatility of Asset 2 (σ2)"),
                    dbc.Input(id={'type': 'param', 'model': 'kirk', 'param': 'volatility-asset2'},
                              type="number", value=0.15, min=0.005, max=2, step=0.0000000001)
                ], width=6)
            ], className="mb-2"),

            dbc.Row([
                dbc.Col([
                    html.Label("Correlation (ρ)"),
                    dbc.Input(id={'type': 'param', 'model': 'kirk', 'param': 'correlation'},
                              type="number", value=0.5, min=-1, max=1, step=0.00001)
                ], width=6)
            ], className="mb-2")
        ]

    else:
        # Default to Black-76 if an unknown option type is selected
        return update_parameters("black76")

    return html.Div(params)


# Callback to calculate option prices and greeks
@callback(
    [
        Output("results-container", "children"),
        Output("greeks-container", "children"),
        Output("time-info", "children")
    ],
    Input("calculate-button", "n_clicks"),
    [
        State("option-type", "value"),
        State("option-call-put", "value"),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date')
    ],
    prevent_initial_call=True
)
def calculate_option(n_clicks, option_type, call_put, all_params, all_dates):
    """Calculate option price and greeks using either Black-76 or Kirk."""
    print("=" * 60)
    print("DEBUG: calculate_option CALLBACK TRIGGERED")
    print(f"DEBUG: n_clicks = {n_clicks}")
    print(f"DEBUG: option_type = {option_type}")
    print(f"DEBUG: call_put = {call_put}")
    print(f"DEBUG: all_params = {all_params}")
    print(f"DEBUG: all_dates = {all_dates}")
    print("=" * 60)

    if not n_clicks:
        print("DEBUG: n_clicks is falsy, returning early")
        return html.Div("No calculation performed"), html.Div(), html.Div()

    try:
        # --------------------------------------------------
        # 1) Pull out parameter values by index
        #    based on the known order of fields in the layout
        # --------------------------------------------------

        if option_type == "black76":
            # Expect all_params has 4 values in this order:
            #   0 -> Underlying Price (S)
            #   1 -> Strike Price (K)
            #   2 -> Risk-Free Rate (r)
            #   3 -> Volatility (v)
            #
            # all_dates[0] -> Expiration Date
            try:
                S = float(all_params[0])
            except (TypeError, ValueError):
                S = 100
            try:
                K = float(all_params[1])
            except (TypeError, ValueError):
                K = 100
            try:
                r = float(all_params[2])
            except (TypeError, ValueError):
                r = 0.05
            try:
                v = float(all_params[3])
            except (TypeError, ValueError):
                v = 0.2
            print(f"DEBUG Black76: S={S}, K={K}, r={r}, v={v}")

            # Expiration date could be None if user didn't pick, so handle that:
            if all_dates and all_dates[0]:
                expiration_date = parse_date(all_dates[0])
                print(f"DEBUG: Parsed expiration_date = {expiration_date}")
            else:
                expiration_date = date.today() + timedelta(days=365)
                print(f"DEBUG: Using default expiration_date = {expiration_date}")

            # --------------------------------------------------
            # 2) Compute time to expiration T (years)
            # --------------------------------------------------
            days_to_expiry = (expiration_date - date.today()).days
            T = max(days_to_expiry / 365.0, 0.001)
            print(f"DEBUG: days_to_expiry={days_to_expiry}, T={T}")

            # Validate parameters
            if S <= 0:
                S = 100
            if K <= 0:
                K = 100
            if v <= 0:
                v = 0.2

            # --------------------------------------------------
            # 3) Calculate using your black_76() function
            # --------------------------------------------------
            print(f"DEBUG: Calling black_76({call_put}, {S}, {K}, {T}, {r}, {v})")
            results = black_76(call_put, S, K, T, r, v)
            print(f"DEBUG: black_76 returned: {results}")
            option_value, delta, gamma, theta, vega, rho_greek = results
            print(f"DEBUG: option_value={option_value}, delta={delta}, gamma={gamma}")

            # Store for payoff chart
            option_cache["black76"]["value"] = option_value
            option_cache["black76"]["params"] = {
                "S": S,
                "K": K,
                "T": T,
                "r": r,
                "v": v,
                "call_put": call_put,
                "expiration_date": expiration_date.strftime('%Y-%m-%d')
            }

            # Prepare output Divs
            results_div = html.Div([
                html.H4(f"Option Value: {option_value:.4f}"),
                html.P([
                    html.Strong("Valuation Method: "),
                    "Black-76 (Commodities)"
                ])
            ])

            # Greeks are already in trader convention from black_76():
            # - theta: per day
            # - vega: per 1% vol change
            # - rho: per 1% rate change
            greeks_div = html.Div([
                html.Table([
                    html.Tr([html.Th("Greek"), html.Th("Value")]),
                    html.Tr([html.Td("Delta"), html.Td(f"{delta:.4f}")]),
                    html.Tr([html.Td("Gamma"), html.Td(f"{gamma:.6f}")]),
                    html.Tr([html.Td("Theta"), html.Td(f"{theta:.6f}")]),
                    html.Tr([html.Td("Vega"), html.Td(f"{vega:.4f}")]),
                    html.Tr([html.Td("Rho"), html.Td(f"{rho_greek:.6f}")])
                ], className="table table-striped")
            ])

            time_info = html.Div([
                html.H5(f"Time to Expiration: {T:.4f} years"),
                html.P(f"({days_to_expiry} days)")
            ])

            print("DEBUG: Black76 calculation SUCCESS - returning results")
            return results_div, greeks_div, time_info

        elif option_type == "kirk":
            # Expect all_params has 7 values in this order:
            #   0 -> Price of Asset 1 (S1)
            #   1 -> Price of Asset 2 (S2)
            #   2 -> Strike Price (K_spread)
            #   3 -> Risk-Free Rate (r_spread)
            #   4 -> Volatility of Asset 1 (v1)
            #   5 -> Volatility of Asset 2 (v2)
            #   6 -> Correlation (rho)
            #
            # all_dates[0] -> Expiration Date
            try:
                S1 = float(all_params[0])
            except (TypeError, ValueError):
                S1 = 100
            try:
                S2 = float(all_params[1])
            except (TypeError, ValueError):
                S2 = 90
            try:
                K_spread = float(all_params[2])
            except (TypeError, ValueError):
                K_spread = 5
            try:
                r_spread = float(all_params[3])
            except (TypeError, ValueError):
                r_spread = 0.05
            try:
                v1 = float(all_params[4])
            except (TypeError, ValueError):
                v1 = 0.2
            try:
                v2 = float(all_params[5])
            except (TypeError, ValueError):
                v2 = 0.15
            try:
                rho = float(all_params[6])
            except (TypeError, ValueError):
                rho = 0.5

            # Expiration date
            if all_dates and all_dates[0]:
                spread_expiration_date = parse_date(all_dates[0])
            else:
                spread_expiration_date = date.today() + timedelta(days=365)

            days_to_expiry = (spread_expiration_date - date.today()).days
            T_spread = max(days_to_expiry / 365.0, 0.001)

            # Validate
            if S1 <= 0:
                S1 = 100
            if S2 <= 0:
                S2 = 90
            if v1 <= 0:
                v1 = 0.2
            if v2 <= 0:
                v2 = 0.15
            if rho < -1 or rho > 1:
                rho = 0.5

            call_put_expanded = "call" if call_put == "C" else "put"

            # --------------------------------------------------
            # 3) Calculate using Kirk model
            # --------------------------------------------------
            option_value = kirk_model_with_substitution(
                S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
            )
            option_cache["kirk"]["value"] = option_value
            option_cache["kirk"]["params"] = {
                "S1": S1,
                "S2": S2,
                "K_spread": K_spread,
                "T_spread": T_spread,
                "v1": v1,
                "v2": v2,
                "rho": rho,
                "call_put": call_put_expanded,
                "expiration_date": spread_expiration_date.strftime('%Y-%m-%d')
            }

            # Get greeks
            greeks = kirk_spread_greeks(
                S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
            )
            delta_S1 = greeks.get('delta_S1', 0)
            delta_S2 = greeks.get('delta_S2', 0)
            gamma_S1 = greeks.get('gamma_S1', 0)
            gamma_S2 = greeks.get('gamma_S2', 0)
            gamma_S1S2 = greeks.get('gamma_S1S2', 0)
            vega_sigma1 = greeks.get('vega_sigma1', 0)
            vega_sigma2 = greeks.get('vega_sigma2', 0)
            corr_sensitivity = greeks.get('corr_sensitivity', 0)
            theta = greeks.get('theta', 0)
            vega_equiv = greeks.get('vega_equiv', 0)

            results_div = html.Div([
                html.H4(f"Option Value: {option_value:.4f}"),
                html.P([
                    html.Strong("Valuation Method: "),
                    "Kirk Spread Option Model"
                ])
            ])

            greeks_div = html.Div([
                html.Table([
                    html.Tr([
                        html.Th("Greek"),
                        html.Th("Asset 1"),
                        html.Th("Asset 2"),
                        html.Th("Cross")
                    ]),
                    html.Tr([
                        html.Td("Delta"),
                        html.Td(f"{delta_S1:.4f}"),
                        html.Td(f"{delta_S2:.4f}"),
                        html.Td("")
                    ]),
                    html.Tr([
                        html.Td("Gamma"),
                        html.Td(f"{gamma_S1:.4f}"),
                        html.Td(f"{gamma_S2:.4f}"),
                        html.Td(f"{gamma_S1S2:.4f}")
                    ]),
                    html.Tr([
                        html.Td("Vega"),
                        html.Td(f"{vega_sigma1:.4f}"),
                        html.Td(f"{vega_sigma2:.4f}"),
                        html.Td("")
                    ]),
                    html.Tr([
                        html.Td("Theta"),
                        html.Td(f"{theta:.4f}"),
                        html.Td(""),
                        html.Td("")
                    ]),
                    html.Tr([
                        html.Td("Corr Sensitivity"),
                        html.Td(f"{corr_sensitivity:.4f}"),
                        html.Td(""),
                        html.Td("")
                    ]),
                    html.Tr([
                        html.Td("Vega Equiv"),
                        html.Td(f"{vega_equiv:.4f}"),
                        html.Td(""),
                        html.Td("")
                    ])
                ], className="table table-striped")
            ])

            time_info = html.Div([
                html.H5(f"Time to Expiration: {T_spread:.4f} years"),
                html.P(f"({days_to_expiry} days)")
            ])

            return results_div, greeks_div, time_info

        else:
            # Unknown option type
            return (
                html.Div(f"Unknown option type: {option_type}"),
                html.Div(),
                html.Div()
            )

    except Exception as e:
        import traceback
        error_message = f"Error in calculation: {str(e)}"
        print("=" * 60)
        print(f"DEBUG: EXCEPTION in calculate_option: {e}")
        print(f"DEBUG: Traceback:\n{traceback.format_exc()}")
        print("=" * 60)
        return (
            html.Div([html.Div(error_message, className="alert alert-danger")]),
            html.Div(),
            html.Div("Calculation error")
        )


# Callback to update the payoff chart
# Also update the payoff chart callback to use the cache
@callback(
    Output("payoff-chart", "figure"),
    [Input("calculate-button", "n_clicks"),
     Input("valuation-date", "date"),
     Input("price-range-slider", "value")],
    [State("option-type", "value")],
    prevent_initial_call=True
)
def update_payoff_chart(n_clicks, valuation_date, price_range, option_type):

    """Update the payoff chart based on option parameters and valuation date"""
    # Check if calculation has been triggered
    if n_clicks is None:
        # Return empty figure if no calculation performed
        return go.Figure().update_layout(
            title="Calculate option price first",
            xaxis_title="Underlying Price",
            yaxis_title="Option Value",
            height=400
        )

    try:
        if option_type == "black76":
            # Check if we have cached values
            if option_cache["black76"]["value"] is None or option_cache["black76"]["params"] is None:
                return go.Figure().update_layout(
                    title="Calculate option price first",
                    xaxis_title="Underlying Price",
                    yaxis_title="Option Value",
                    height=400
                )

            # Get parameters from cache
            cached_params = option_cache["black76"]["params"]
            S = cached_params["S"]
            K = cached_params["K"]
            r = cached_params["r"]
            v = cached_params["v"]
            call_put = cached_params["call_put"]

            # Parse expiration date from cache
            if "expiration_date" in cached_params and cached_params["expiration_date"]:
                exp_date = parse_date(cached_params["expiration_date"])
            else:
                # Use T to calculate expiration date if not directly available
                today = date.today()
                days_to_expiry = int(cached_params["T"] * 365)  # Approximate
                exp_date = today + timedelta(days=days_to_expiry)

            # Parse valuation date if provided
            if valuation_date is None:
                # If no valuation date, use expiration date
                val_date = exp_date
                time_to_expiry = 0.001  # Almost at expiration
            else:
                val_date = parse_date(valuation_date)
                # If valuation date is after expiration, use expiration date
                if val_date > exp_date:
                    val_date = exp_date
                    time_to_expiry = 0.001
                else:
                    days_to_expiry = (exp_date - val_date).days
                    time_to_expiry = max(days_to_expiry / 365.0, 0.001)

            # Calculate price range
            price_min = max(0.01, S * (1 - price_range / 100))
            price_max = S * (1 + price_range / 100)
            price_step = (price_max - price_min) / 100
            prices = np.arange(price_min, price_max + price_step, price_step)

            option_values = []
            intrinsic_values = []

            # At expiration, option value equals intrinsic value
            if time_to_expiry <= 0.001:
                for price in prices:
                    if call_put == "C":
                        intrinsic = max(0, price - K)
                    else:
                        intrinsic = max(0, K - price)

                    intrinsic_values.append(intrinsic)
                    option_values.append(intrinsic)
            else:
                # Calculate option values across price range
                for price in prices:
                    try:
                        results = black_76(call_put, price, K, time_to_expiry, r, v)
                        option_values.append(results[0])

                        # Calculate intrinsic value
                        if call_put == "C":
                            intrinsic = max(0, price - K)
                        else:
                            intrinsic = max(0, K - price)
                        intrinsic_values.append(intrinsic)
                    except Exception:
                        # If error, use intrinsic value
                        if call_put == "C":
                            intrinsic = max(0, price - K)
                        else:
                            intrinsic = max(0, K - price)

                        option_values.append(intrinsic)
                        intrinsic_values.append(intrinsic)

            # Create figure
            fig = go.Figure()

            # Add option value line
            fig.add_trace(
                go.Scatter(
                    x=prices,
                    y=option_values,
                    mode='lines',
                    name='Option Value',
                    line=dict(color='blue', width=2)
                )
            )

            # Add intrinsic value line
            fig.add_trace(
                go.Scatter(
                    x=prices,
                    y=intrinsic_values,
                    mode='lines',
                    name='Intrinsic Value',
                    line=dict(color='red', width=2, dash='dash')
                )
            )

            # Add marker for originally calculated value
            fig.add_trace(
                go.Scatter(
                    x=[S],
                    y=[option_cache["black76"]["value"]],
                    mode='markers',
                    name='Calculated Value',
                    marker=dict(color='green', size=10, symbol='star')
                )
            )

            # Add current price line
            fig.add_vline(
                x=S,
                line_width=1,
                line_dash="dash",
                line_color="green",
                annotation_text="Current Price",
                annotation_position="top"
            )

            # Add strike price line
            fig.add_vline(
                x=K,
                line_width=1,
                line_dash="dash",
                line_color="orange",
                annotation_text="Strike Price",
                annotation_position="bottom"
            )

            # Update layout
            title_text = f"{'Call' if call_put == 'C' else 'Put'} Option Payoff"
            if valuation_date is None:
                title_text += " at Expiration"
            else:
                title_text += f" on {val_date.strftime('%Y-%m-%d')}"

            fig.update_layout(
                title=title_text,
                xaxis_title="Underlying Price",
                yaxis_title="Option Value",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            return fig

        elif option_type == "kirk":
            # Check if we have cached values
            if option_cache["kirk"]["value"] is None or option_cache["kirk"]["params"] is None:
                return go.Figure().update_layout(
                    title="Calculate option price first",
                    xaxis_title="Price",
                    yaxis_title="Option Value",
                    height=400
                )

            # Get parameters from cache
            cached_params = option_cache["kirk"]["params"]
            S1 = cached_params["S1"]
            S2 = cached_params["S2"]
            K_spread = cached_params["K_spread"]
            v1 = cached_params["v1"]
            v2 = cached_params["v2"]
            rho = cached_params["rho"]
            call_put_expanded = cached_params["call_put"]
            call_put = "C" if call_put_expanded == "call" else "P"

            # Parse expiration date from cache
            if "expiration_date" in cached_params and cached_params["expiration_date"]:
                exp_date = parse_date(cached_params["expiration_date"])
            else:
                # Use T_spread to calculate expiration date if not directly available
                today = date.today()
                days_to_expiry = int(cached_params["T_spread"] * 365)  # Approximate
                exp_date = today + timedelta(days=days_to_expiry)

            # Parse valuation date if provided
            if valuation_date is None:
                # If no valuation date, use expiration date
                val_date = exp_date
                time_to_expiry = 0.001  # Almost at expiration
            else:
                val_date = parse_date(valuation_date)
                # If valuation date is after expiration, use expiration date
                if val_date > exp_date:
                    val_date = exp_date
                    time_to_expiry = 0.001
                else:
                    days_to_expiry = (exp_date - val_date).days
                    time_to_expiry = max(days_to_expiry / 365.0, 0.001)

            # Calculate price range for asset 1
            price_min = max(0.01, S1 * (1 - price_range / 100))
            price_max = S1 * (1 + price_range / 100)
            price_step = (price_max - price_min) / 100
            prices = np.arange(price_min, price_max + price_step, price_step)

            option_values = []
            intrinsic_values = []

            # At expiration, option value equals intrinsic value
            if time_to_expiry <= 0.001:
                for price in prices:
                    if call_put == "C":
                        intrinsic = max(0, price - S2 - K_spread)
                    else:
                        intrinsic = max(0, S2 + K_spread - price)

                    intrinsic_values.append(intrinsic)
                    option_values.append(intrinsic)
            else:
                # Calculate option values across price range
                for price in prices:
                    try:
                        option_value = kirk_model_with_substitution(
                            price, S2, K_spread, v1, v2, rho, time_to_expiry, call_put_expanded
                        )
                        option_values.append(option_value)

                        # Calculate intrinsic value
                        if call_put == "C":
                            intrinsic = max(0, price - S2 - K_spread)
                        else:
                            intrinsic = max(0, S2 + K_spread - price)
                        intrinsic_values.append(intrinsic)
                    except Exception:
                        # If error, use intrinsic value
                        if call_put == "C":
                            intrinsic = max(0, price - S2 - K_spread)
                        else:
                            intrinsic = max(0, S2 + K_spread - price)

                        option_values.append(intrinsic)
                        intrinsic_values.append(intrinsic)

            # Create figure
            fig = go.Figure()

            # Add option value line
            fig.add_trace(
                go.Scatter(
                    x=prices,
                    y=option_values,
                    mode='lines',
                    name='Option Value',
                    line=dict(color='blue', width=2)
                )
            )

            # Add intrinsic value line
            fig.add_trace(
                go.Scatter(
                    x=prices,
                    y=intrinsic_values,
                    mode='lines',
                    name='Intrinsic Value',
                    line=dict(color='red', width=2, dash='dash')
                )
            )

            # Add marker for originally calculated value
            fig.add_trace(
                go.Scatter(
                    x=[S1],
                    y=[option_cache["kirk"]["value"]],
                    mode='markers',
                    name='Calculated Value',
                    marker=dict(color='green', size=10, symbol='star')
                )
            )

            # Add current price line for asset 1
            fig.add_vline(
                x=S1,
                line_width=1,
                line_dash="dash",
                line_color="green",
                annotation_text="Current Asset 1",
                annotation_position="top"
            )

            # Add breakeven price line (S2 + K_spread)
            fig.add_vline(
                x=S2 + K_spread,
                line_width=1,
                line_dash="dash",
                line_color="orange",
                annotation_text="S2 + Strike",
                annotation_position="bottom"
            )

            # Update layout
            title_text = f"{'Call' if call_put == 'C' else 'Put'} Spread Option Payoff (Asset 1 varying)"
            if valuation_date is None:
                title_text += " at Expiration"
            else:
                title_text += f" on {val_date.strftime('%Y-%m-%d')}"

            fig.update_layout(
                title=title_text,
                xaxis_title="Asset 1 Price",
                yaxis_title="Option Value",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            return fig

        else:
            # Unknown option type
            return go.Figure().update_layout(
                title="Unknown option type",
                xaxis_title="Price",
                yaxis_title="Option Value",
                height=400
            )

    except Exception as e:
        # Return error figure
        return go.Figure().update_layout(
            title=f"Error creating chart: {str(e)}",
            xaxis_title="Price",
            yaxis_title="Option Value",
            height=400
        )


# Add a new callback for the volatility chart
# Add a new callback for the volatility chart
@callback(
    Output("volatility-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [State("option-type", "value"),
     State("option-call-put", "value"),
     # Use pattern-matching selectors for all params
     State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
     State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date')],
    prevent_initial_call=True
)
def update_volatility_chart(n_clicks, option_type, call_put, all_params, all_dates):
    """Create a chart showing option price vs volatility for Kirk spread options"""
    # Check if calculation has been performed
    if n_clicks is None:
        return go.Figure().update_layout(
            title="Calculate option price first",
            xaxis_title="Volatility",
            yaxis_title="Option Price",
            height=400
        )

    try:
        # Processing parameters directly as in the calculate_option function

        # Generate range of volatilities to simulate (from 0.05 to 1.00)
        vol_range = np.linspace(0.05, 1.00, 40)

        # Arrays to store results
        option_prices = []
        volatilities = []

        if option_type == "black76":
            # Get Black-76 parameters like in calculate_option
            try:
                S = float(all_params[0])
            except (TypeError, ValueError):
                S = 100

            try:
                K = float(all_params[1])
            except (TypeError, ValueError):
                K = 100

            try:
                r = float(all_params[2])
            except (TypeError, ValueError):
                r = 0.05

            try:
                v = float(all_params[3])
            except (TypeError, ValueError):
                v = 0.2

            # Expiration date could be None if user didn't pick, so handle that:
            if all_dates and all_dates[0]:
                exp_date = parse_date(all_dates[0])
            else:
                exp_date = date.today() + timedelta(days=365)

            days_to_expiry = (exp_date - date.today()).days
            T = max(days_to_expiry / 365.0, 0.001)

            # Calculate option price for each volatility value
            for vol in vol_range:
                volatilities.append(vol)

                try:
                    results = black_76(call_put, S, K, T, r, vol)
                    option_prices.append(results[0])  # First element is option price
                except Exception as e:
                    # If calculation fails, use None to create a gap in the chart
                    option_prices.append(None)

            # Create figure
            fig = go.Figure()

            # Add option price vs volatility line
            fig.add_trace(
                go.Scatter(
                    x=volatilities,
                    y=option_prices,
                    mode='lines+markers',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the current volatility and price
            current_vol = v
            current_price = option_cache["black76"]["value"] if option_cache["black76"]["value"] is not None else \
            black_76(call_put, S, K, T, r, v)[0]

            fig.add_trace(
                go.Scatter(
                    x=[current_vol],
                    y=[current_price],
                    mode='markers',
                    name='Current Parameters',
                    marker=dict(color='red', size=10, symbol='star')
                )
            )

            # Update layout
            fig.update_layout(
                title=f"Black-76 Option Price vs Volatility",
                xaxis_title="Volatility (σ)",
                yaxis_title="Option Price",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add annotation for current volatility
            fig.add_annotation(
                x=current_vol,
                y=current_price,
                text=f"Current: σ={current_vol:.3f}, Price={current_price:.4f}",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                ax=80,
                ay=-40
            )

        elif option_type == "kirk":
            # Get Kirk parameters like in calculate_option
            try:
                S1 = float(all_params[0])
            except (TypeError, ValueError):
                S1 = 100

            try:
                S2 = float(all_params[1])
            except (TypeError, ValueError):
                S2 = 90

            try:
                K_spread = float(all_params[2])
            except (TypeError, ValueError):
                K_spread = 5

            try:
                r_spread = float(all_params[3])
            except (TypeError, ValueError):
                r_spread = 0.05

            try:
                v1 = float(all_params[4])
            except (TypeError, ValueError):
                v1 = 0.2

            try:
                v2 = float(all_params[5])
            except (TypeError, ValueError):
                v2 = 0.15

            try:
                rho = float(all_params[6])
            except (TypeError, ValueError):
                rho = 0.5

            # Expiration date
            if all_dates and all_dates[0]:
                exp_date = parse_date(all_dates[0])
            else:
                exp_date = date.today() + timedelta(days=365)

            days_to_expiry = (exp_date - date.today()).days
            T_spread = max(days_to_expiry / 365.0, 0.001)

            # Convert C/P to call/put for kirk_model_with_substitution
            call_put_expanded = "call" if call_put == "C" else "put"

            # Arrays to store results
            option_prices = []
            implied_vols = []

            # Calculate option price for each volatility combination
            # We'll vary both volatilities together proportionally
            base_v1 = v1
            base_v2 = v2
            ratio = base_v2 / base_v1 if base_v1 > 0 else 1

            for vol_factor in vol_range:
                # Create test volatility for asset 1
                test_v1 = vol_factor
                # Scale volatility for asset 2 to maintain original ratio
                test_v2 = test_v1 * ratio

                # Calculate equivalent volatility
                # Simplified formula: sqrt(v1^2 + v2^2 - 2*rho*v1*v2)
                equiv_vol = np.sqrt(test_v1 ** 2 + test_v2 ** 2 - 2 * rho * test_v1 * test_v2)
                implied_vols.append(equiv_vol)

                # Calculate option price with these volatilities
                try:
                    option_value = kirk_model_with_substitution(
                        S1, S2, K_spread, test_v1, test_v2, rho, T_spread, call_put_expanded
                    )
                    option_prices.append(option_value)
                except Exception as e:
                    # If calculation fails, use None to create a gap in the chart
                    option_prices.append(None)

            # Use implied_vols as our x-axis
            volatilities = implied_vols

            # Create figure
            fig = go.Figure()

            # Add option price vs implied volatility line
            fig.add_trace(
                go.Scatter(
                    x=volatilities,
                    y=option_prices,
                    mode='lines+markers',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the current volatility and price
            current_equiv_vol = np.sqrt(v1 ** 2 + v2 ** 2 - 2 * rho * v1 * v2)
            current_price = option_cache["kirk"]["value"] if option_cache["kirk"][
                "value"] else kirk_model_with_substitution(
                S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
            )

            fig.add_trace(
                go.Scatter(
                    x=[current_equiv_vol],
                    y=[current_price],
                    mode='markers',
                    name='Current Parameters',
                    marker=dict(color='red', size=10, symbol='star')
                )
            )

            # Update layout
            fig.update_layout(
                title=f"Kirk Spread Option Price vs Equivalent Volatility",
                xaxis_title="Equivalent Volatility (σ_eq)",
                yaxis_title="Option Price",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add annotation for current volatility
            fig.add_annotation(
                x=current_equiv_vol,
                y=current_price,
                text=f"Current: σ_eq={current_equiv_vol:.3f}, Price={current_price:.4f}",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                ax=80,
                ay=-40
            )

        else:
            # Unknown option type
            return go.Figure().update_layout(
                title=f"Unknown option type: {option_type}",
                xaxis_title="Volatility",
                yaxis_title="Option Price",
                height=400
            )

        return fig

    except Exception as e:
        # Return error figure
        return go.Figure().update_layout(
            title=f"Error creating volatility chart: {str(e)}",
            xaxis_title="Volatility",
            yaxis_title="Option Price",
            height=400
        )


# Callback for the risk-free rate chart
@callback(
    Output("rate-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [State("option-type", "value"),
     State("option-call-put", "value"),
     State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
     State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date')],
    prevent_initial_call=True
)
def update_rate_chart(n_clicks, option_type, call_put, all_params, all_dates):
    """Create a chart showing option price vs risk-free rate"""
    # Check if calculation has been performed
    if n_clicks is None:
        return go.Figure().update_layout(
            title="Calculate option price first",
            xaxis_title="Risk-Free Rate (%)",
            yaxis_title="Option Price",
            height=400
        )

    try:
        # Processing parameters directly as in the calculate_option function

        # Generate range of risk-free rates to simulate (from -0.02 to 0.15)
        rate_range = np.linspace(-0.02, 0.15, 40)

        # Arrays to store results
        option_prices = []
        rates = []

        if option_type == "black76":
            # Get Black-76 parameters like in calculate_option
            try:
                S = float(all_params[0])
            except (TypeError, ValueError):
                S = 100

            try:
                K = float(all_params[1])
            except (TypeError, ValueError):
                K = 100

            try:
                r = float(all_params[2])
            except (TypeError, ValueError):
                r = 0.05

            try:
                v = float(all_params[3])
            except (TypeError, ValueError):
                v = 0.2

            # Expiration date could be None if user didn't pick, so handle that:
            if all_dates and all_dates[0]:
                exp_date = parse_date(all_dates[0])
            else:
                exp_date = date.today() + timedelta(days=365)

            days_to_expiry = (exp_date - date.today()).days
            T = max(days_to_expiry / 365.0, 0.001)

            # Calculate option price for each risk-free rate value
            for rate in rate_range:
                rates.append(rate)

                try:
                    results = black_76(call_put, S, K, T, rate, v)
                    option_prices.append(results[0])  # First element is option price
                except Exception as e:
                    # If calculation fails, use None to create a gap in the chart
                    option_prices.append(None)

            # Create figure
            fig = go.Figure()

            # Add option price vs risk-free rate line
            fig.add_trace(
                go.Scatter(
                    x=rates,
                    y=option_prices,
                    mode='lines+markers',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the current risk-free rate and price
            current_rate = r
            current_price = option_cache["black76"]["value"] if option_cache["black76"]["value"] is not None else \
            black_76(call_put, S, K, T, r, v)[0]

            fig.add_trace(
                go.Scatter(
                    x=[current_rate],
                    y=[current_price],
                    mode='markers',
                    name='Current Parameters',
                    marker=dict(color='red', size=10, symbol='star')
                )
            )

            # Update layout
            fig.update_layout(
                title=f"Black-76 Option Price vs Risk-Free Rate",
                xaxis_title="Risk-Free Rate (r)",
                yaxis_title="Option Price",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add annotation for current risk-free rate
            fig.add_annotation(
                x=current_rate,
                y=current_price,
                text=f"Current: r={current_rate:.2%}, Price={current_price:.4f}",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                ax=80,
                ay=-40
            )

        elif option_type == "kirk":
            # Get Kirk parameters like in calculate_option
            try:
                S1 = float(all_params[0])
            except (TypeError, ValueError):
                S1 = 100

            try:
                S2 = float(all_params[1])
            except (TypeError, ValueError):
                S2 = 90

            try:
                K_spread = float(all_params[2])
            except (TypeError, ValueError):
                K_spread = 5

            try:
                r_spread = float(all_params[3])
            except (TypeError, ValueError):
                r_spread = 0.05

            try:
                v1 = float(all_params[4])
            except (TypeError, ValueError):
                v1 = 0.2

            try:
                v2 = float(all_params[5])
            except (TypeError, ValueError):
                v2 = 0.15

            try:
                rho = float(all_params[6])
            except (TypeError, ValueError):
                rho = 0.5

            # Expiration date
            if all_dates and all_dates[0]:
                exp_date = parse_date(all_dates[0])
            else:
                exp_date = date.today() + timedelta(days=365)

            days_to_expiry = (exp_date - date.today()).days
            T_spread = max(days_to_expiry / 365.0, 0.001)

            # Convert C/P to call/put for kirk_model_with_substitution
            call_put_expanded = "call" if call_put == "C" else "put"

            # Calculate option price for each risk-free rate value
            for rate in rate_range:
                rates.append(rate)

                try:
                    option_value = kirk_model_with_substitution(
                        S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
                    )
                    option_prices.append(option_value)
                except Exception as e:
                    # If calculation fails, use None to create a gap in the chart
                    option_prices.append(None)

            # Create figure
            fig = go.Figure()

            # Add option price vs risk-free rate line
            fig.add_trace(
                go.Scatter(
                    x=rates,
                    y=option_prices,
                    mode='lines+markers',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the current risk-free rate and price
            current_rate = r_spread
            current_price = option_cache["kirk"]["value"] if option_cache["kirk"][
                                                                 "value"] is not None else kirk_model_with_substitution(
                S1, S2, K_spread, v1, v2, rho, T_spread, call_put_expanded
            )

            fig.add_trace(
                go.Scatter(
                    x=[current_rate],
                    y=[current_price],
                    mode='markers',
                    name='Current Parameters',
                    marker=dict(color='red', size=10, symbol='star')
                )
            )

            # Update layout
            fig.update_layout(
                title=f"Kirk Spread Option Price vs Risk-Free Rate",
                xaxis_title="Risk-Free Rate (r)",
                yaxis_title="Option Price",
                height=400,
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add annotation for current risk-free rate
            fig.add_annotation(
                x=current_rate,
                y=current_price,
                text=f"Current: r={current_rate:.2%}, Price={current_price:.4f}",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                ax=80,
                ay=-40
            )

        else:
            # Unknown option type
            return go.Figure().update_layout(
                title=f"Unknown option type: {option_type}",
                xaxis_title="Risk-Free Rate",
                yaxis_title="Option Price",
                height=400
            )

        return fig

    except Exception as e:
        # Return error figure
        return go.Figure().update_layout(
            title=f"Error creating risk-free rate chart: {str(e)}",
            xaxis_title="Risk-Free Rate",
            yaxis_title="Option Price",
            height=400
        )


@callback(
    Output("time-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [
        State("option-type", "value"),
        State("option-call-put", "value"),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date')
    ],
    prevent_initial_call=True
)
def update_time_chart(n_clicks, option_type, call_put, all_params, all_dates):
    """
    Create a chart showing option price vs time to expiration.
    Fixed to ensure dates display in correct order.
    """
    if n_clicks is None:
        return go.Figure().update_layout(
            title="Calculate option price first",
            xaxis_title="Date",
            yaxis_title="Option Price",
            height=400
        )

    try:
        # ---------------------------------------------------------------
        # STEP 1: Get today and tomorrow as reference points
        # ---------------------------------------------------------------
        today = date.today()

        # ---------------------------------------------------------------
        # STEP 2: Get expiration date (with careful type handling)
        # ---------------------------------------------------------------
        expiration_date = parse_date(all_dates[0])

        # ---------------------------------------------------------------
        # STEP 3: Generate date points for x-axis (with careful type handling)
        # ---------------------------------------------------------------
        try:
            # Create date points from tomorrow up to 4 months after expiration
            date_points = []

            # Start with tomorrow
            date_points.append(today)

            # Add some intermediate points before expiration
            days_to_expiry = (expiration_date - today).days

            # # Calculate intervals for date points
            for i in range(1, days_to_expiry):
                point_date = today + timedelta(days=i )
                if point_date < expiration_date:
                    date_points.append(point_date)

            # Add expiration date
            if expiration_date not in date_points:
                date_points.append(expiration_date)

            # Sort all dates
            date_points.sort()

            # Create formatted dates for x-axis in ISO format for proper sorting
            dates_formatted = [d.strftime('%Y-%m-%d') for d in date_points]

            # Verify date points
            print(f"DEBUG => Generated {len(date_points)} date points")
            if date_points:
                print(f"DEBUG => First date: {date_points[0]}, Last date: {date_points[-1]}")
                print(f"DEBUG => First few dates: {dates_formatted[:5]}")

        except Exception as e:
            print(f"DEBUG => Error generating date points: {e}")


        # ---------------------------------------------------------------
        # STEP 4: Calculate option values for each date
        # ---------------------------------------------------------------
        option_values = []

        # BLACK-76 OPTION CALCULATION
        if option_type == "black76":
            try:
                # Parse parameters
                S = float(all_params[0]) if all_params and len(all_params) > 0 and all_params[0] else 100
                K = float(all_params[1]) if all_params and len(all_params) > 1 and all_params[1] else 100
                r = float(all_params[2]) if all_params and len(all_params) > 2 and all_params[2] else 0.05
                v = float(all_params[3]) if all_params and len(all_params) > 3 and all_params[3] else 0.2

                # Verify parameters
                print(f"DEBUG => Black76 parameters: S={S}, K={K}, r={r}, v={v}")

                # Calculate option value for each date
                for eval_date in date_points:
                    # Need to compute time to expiry in years
                    days_diff = (expiration_date - eval_date).days

                    if days_diff < 0:
                        # After expiration - option value is intrinsic value
                        if call_put == "C":
                            value = max(0, S - K)
                        else:
                            value = max(0, K - S)
                    else:
                        # Before expiration - use Black76
                        T = max(days_diff / 365.0, 0.001)  # Time in years

                        try:
                            option_results = black_76(call_put, S, K, T, r, v)
                            value = option_results[0]  # First return value is option price
                        except Exception as e:
                            print(f"DEBUG => black_76 calculation error for date {eval_date}: {e}")
                            # Fallback to intrinsic value
                            if call_put == "C":
                                value = max(0, S - K)
                            else:
                                value = max(0, K - S)

                    option_values.append(value)

                # Create the data dictionary for the figure
                chart_data = []
                for i in range(len(dates_formatted)):
                    chart_data.append({
                        'date': dates_formatted[i],
                        'value': option_values[i]
                    })

                # Sort the data by date
                chart_data.sort(key=lambda x: x['date'])

                # Extract the sorted data back into separate lists
                sorted_dates = [item['date'] for item in chart_data]
                sorted_values = [item['value'] for item in chart_data]

                # Create figure
                fig = go.Figure()

                # Add option price line with sorted data
                fig.add_trace(
                    go.Scatter(
                        x=sorted_dates,
                        y=sorted_values,
                        mode='lines',
                        name='Option Price',
                        line=dict(color='blue', width=2)
                    )
                )

                # Mark today's price
                try:
                    days_to_expiry_today = (expiration_date - today).days
                    if days_to_expiry_today >= 0:
                        today_T = max(days_to_expiry_today / 365.0, 0.001)
                        today_price = black_76(call_put, S, K, today_T, r, v)[0]

                        fig.add_trace(
                            go.Scatter(
                                x=[today.strftime('%Y-%m-%d')],
                                y=[today_price],
                                mode='markers',
                                name='Today',
                                marker=dict(color='red', size=10, symbol='star')
                            )
                        )
                except Exception as e:
                    print(f"DEBUG => Error marking today's price: {e}")

                # Create layout with proper date axis type
                option_type_label = "Call" if call_put == "C" else "Put"
                fig.update_layout(
                    title=f"Black-76 {option_type_label} Option Price Over Time",
                    xaxis_title="Date",
                    yaxis_title="Option Price",
                    height=400,
                    xaxis=dict(
                        tickangle=-45,
                        type='category',  # Keep as category but ensure data is pre-sorted
                        categoryorder='array',
                        categoryarray=sorted_dates  # Explicitly set the order
                    ),
                    legend=dict(
                        yanchor="top",
                        y=0.99,
                        xanchor="left",
                        x=0.01
                    )
                )

                # Add vertical line at expiration using shapes
                exp_date_str = expiration_date.strftime('%Y-%m-%d')
                fig.update_layout(
                    shapes=[
                        dict(
                            type="line",
                            xref="x",
                            yref="paper",
                            x0=exp_date_str,
                            y0=0,
                            x1=exp_date_str,
                            y1=1,
                            line=dict(
                                color="red",
                                width=1,
                                dash="dash",
                            )
                        )
                    ],
                    annotations=[
                        dict(
                            x=exp_date_str,
                            y=1,
                            xref="x",
                            yref="paper",
                            text="Expiration Date",
                            showarrow=False,
                            xanchor="center",
                            yanchor="bottom"
                        )
                    ]
                )

                return fig

            except Exception as e:
                import traceback
                print(f"DEBUG => Error in Black76 calculation section: {e}")
                print(f"DEBUG => Traceback: {traceback.format_exc()}")
                return go.Figure().update_layout(
                    title=f"Error in Black76 calculation: {str(e)}",
                    xaxis_title="Date",
                    yaxis_title="Option Price",
                    height=400
                )

        # KIRK SPREAD OPTION CALCULATION
        elif option_type == "kirk":
            try:
                # Parse parameters with careful type handling
                S1 = float(all_params[0]) if all_params and len(all_params) > 0 and all_params[0] else 100
                S2 = float(all_params[1]) if all_params and len(all_params) > 1 and all_params[1] else 90
                K_spread = float(all_params[2]) if all_params and len(all_params) > 2 and all_params[2] else 5
                r_spread = float(all_params[3]) if all_params and len(all_params) > 3 and all_params[3] else 0.05
                v1 = float(all_params[4]) if all_params and len(all_params) > 4 and all_params[4] else 0.2
                v2 = float(all_params[5]) if all_params and len(all_params) > 5 and all_params[5] else 0.15
                rho = float(all_params[6]) if all_params and len(all_params) > 6 and all_params[6] else 0.5

                # Verify parameters
                print(
                    f"DEBUG => Kirk parameters: S1={S1}, S2={S2}, K={K_spread}, r={r_spread}, v1={v1}, v2={v2}, rho={rho}")

                # Convert C/P to call/put string
                call_put_expanded = "call" if call_put == "C" else "put"

                # Calculate option value for each date
                for eval_date in date_points:
                    # Need to compute time to expiry in years
                    days_diff = (expiration_date - eval_date).days

                    if days_diff < 0:
                        # After expiration - option value is intrinsic value
                        if call_put == "C":
                            value = max(0, S1 - S2 - K_spread)
                        else:
                            value = max(0, S2 + K_spread - S1)
                    else:
                        # Before expiration - use Kirk model
                        T = max(days_diff / 365.0, 0.001)  # Time in years

                        try:
                            value = kirk_model_with_substitution(
                                S1, S2, K_spread, v1, v2, rho, T, call_put_expanded
                            )
                        except Exception as e:
                            print(f"DEBUG => kirk model calculation error for date {eval_date}: {e}")
                            # Fallback to intrinsic value
                            if call_put == "C":
                                value = max(0, S1 - S2 - K_spread)
                            else:
                                value = max(0, S2 + K_spread - S1)

                    option_values.append(value)

                # Create the data dictionary for the figure
                chart_data = []
                for i in range(len(dates_formatted)):
                    chart_data.append({
                        'date': dates_formatted[i],
                        'value': option_values[i]
                    })

                # Sort the data by date
                chart_data.sort(key=lambda x: x['date'])

                # Extract the sorted data back into separate lists
                sorted_dates = [item['date'] for item in chart_data]
                sorted_values = [item['value'] for item in chart_data]

                # Create figure
                fig = go.Figure()

                # Add option price line with sorted data
                fig.add_trace(
                    go.Scatter(
                        x=sorted_dates,
                        y=sorted_values,
                        mode='lines',
                        name='Option Price',
                        line=dict(color='blue', width=2)
                    )
                )

                # Mark today's price
                try:
                    days_to_expiry_today = (expiration_date - today).days
                    if days_to_expiry_today >= 0:
                        today_T = max(days_to_expiry_today / 365.0, 0.001)
                        today_price = kirk_model_with_substitution(
                            S1, S2, K_spread, v1, v2, rho, today_T, call_put_expanded
                        )

                        fig.add_trace(
                            go.Scatter(
                                x=[today.strftime('%Y-%m-%d')],
                                y=[today_price],
                                mode='markers',
                                name='Today',
                                marker=dict(color='red', size=10, symbol='star')
                            )
                        )
                except Exception as e:
                    print(f"DEBUG => Error marking today's price: {e}")

                # Create layout with proper date axis type
                option_type_label = "Call" if call_put == "C" else "Put"
                fig.update_layout(
                    title=f"Kirk Spread {option_type_label} Option Price Over Time",
                    xaxis_title="Date",
                    yaxis_title="Option Price",
                    height=400,
                    xaxis=dict(
                        tickangle=-45,
                        type='category',  # Keep as category but ensure data is pre-sorted
                        categoryorder='array',
                        categoryarray=sorted_dates  # Explicitly set the order
                    ),
                    legend=dict(
                        yanchor="top",
                        y=0.99,
                        xanchor="left",
                        x=0.01
                    )
                )

                # Add vertical line at expiration using shapes
                exp_date_str = expiration_date.strftime('%Y-%m-%d')
                fig.update_layout(
                    shapes=[
                        dict(
                            type="line",
                            xref="x",
                            yref="paper",
                            x0=exp_date_str,
                            y0=0,
                            x1=exp_date_str,
                            y1=1,
                            line=dict(
                                color="red",
                                width=1,
                                dash="dash",
                            )
                        )
                    ],
                    annotations=[
                        dict(
                            x=exp_date_str,
                            y=1,
                            xref="x",
                            yref="paper",
                            text="Expiration Date",
                            showarrow=False,
                            xanchor="center",
                            yanchor="bottom"
                        )
                    ]
                )

                return fig

            except Exception as e:
                import traceback
                print(f"DEBUG => Error in Kirk calculation section: {e}")
                print(f"DEBUG => Traceback: {traceback.format_exc()}")
                return go.Figure().update_layout(
                    title=f"Error in Kirk calculation: {str(e)}",
                    xaxis_title="Date",
                    yaxis_title="Option Price",
                    height=400
                )

        # Unknown option type
        else:
            return go.Figure().update_layout(
                title=f"Unknown option type: {option_type}",
                xaxis_title="Date",
                yaxis_title="Option Price",
                height=400
            )

    except Exception as e:
        import traceback
        print(f"DEBUG => Top-level error in update_time_chart: {e}")
        print(f"DEBUG => Traceback: {traceback.format_exc()}")

        return go.Figure().update_layout(
            title=f"Error creating time chart: {str(e)}",
            xaxis_title="Date",
            yaxis_title="Option Price",
            height=400
        )


@callback(
    Output("extension-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [
        State("option-type", "value"),
        State("option-call-put", "value"),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date')
    ],
    prevent_initial_call=True
)
def update_extension_chart(n_clicks, option_type, call_put, all_params, all_dates):
    """
    Create a chart showing option price vs different expiration dates.
    This shows how extending the expiration date affects option value.
    """
    if n_clicks is None:
        return go.Figure().update_layout(
            title="Calculate option price first",
            xaxis_title="Expiration Date",
            yaxis_title="Option Price",
            height=400
        )

    try:
        # ---------------------------------------------------------------
        # STEP 1: Get today's date as reference point
        # ---------------------------------------------------------------
        today = date.today()

        # ---------------------------------------------------------------
        # STEP 2: Get base expiration date from Option Configuration
        # ---------------------------------------------------------------
        # Default expiration is 1 year from today
        default_expiration = today + timedelta(days=365)

        try:
            # Check if we have a date from the UI
            if not all_dates or not all_dates[0]:
                print("DEBUG => Using default expiration (no date selected)")
                base_expiration_date = default_expiration
            else:
                # Try to ensure we have a date object
                date_input = all_dates[0]
                print(f"DEBUG => Date input: {date_input}, type: {type(date_input)}")

                # If it's already a date object, use it directly
                if isinstance(date_input, date):
                    base_expiration_date = date_input
                    print(f"DEBUG => Using date object directly: {base_expiration_date}")
                else:
                    # Handle string date
                    try:
                        # First try direct conversion if it's a string with YYYY-MM-DD format
                        if isinstance(date_input, str):
                            # Remove time component if present
                            if 'T' in date_input:
                                date_input = date_input.split('T')[0]

                            # Try to parse using datetime
                            year, month, day = map(int, date_input.split('-'))
                            base_expiration_date = date(year, month, day)
                            print(f"DEBUG => Parsed date from string: {base_expiration_date}")
                        else:
                            # Fall back to default if we can't parse
                            print(f"DEBUG => Couldn't parse date, using default. Input was: {date_input}")
                            base_expiration_date = default_expiration
                    except Exception as e:
                        print(f"DEBUG => Date parsing error: {e}, using default")
                        base_expiration_date = default_expiration
        except Exception as e:
            print(f"DEBUG => Expiration date handling error: {e}, using default")
            base_expiration_date = default_expiration

        # Final check to make absolutely sure we have a date object
        if not isinstance(base_expiration_date, date):
            print(
                f"DEBUG => Final check: base_expiration_date is not a date object! Type: {type(base_expiration_date)}")
            base_expiration_date = default_expiration

        print(f"DEBUG => Base expiration_date from UI = {base_expiration_date}, type: {type(base_expiration_date)}")

        # ---------------------------------------------------------------
        # STEP 3: Generate range of expiration dates to evaluate
        # ---------------------------------------------------------------
        try:
            # Range: from today to 2 months after the base expiration date
            extended_date = base_expiration_date + timedelta(days=60)  # Base + 2 months

            # Create 40 evenly spaced dates from today to the extended date
            total_days = (extended_date - today).days

            # Safety check for negative or very small total_days
            if total_days <= 10:
                # If expiration is in the past or very close, use 1 year from today
                print(f"DEBUG => Base expiration date {base_expiration_date} is too close, adjusting range")
                extended_date = today + timedelta(days=365 + 60)
                total_days = (extended_date - today).days

            # Generate evenly spaced dates
            num_points = 40
            days_step = max(1, total_days // (num_points - 1))

            expiration_dates = []
            for i in range(num_points):
                exp_date = today + timedelta(days=i * days_step)
                expiration_dates.append(exp_date)

            # Ensure base expiration date is in the list
            if base_expiration_date not in expiration_dates:
                expiration_dates.append(base_expiration_date)
                expiration_dates.sort()  # Re-sort after adding

            # Format dates for x-axis
            dates_formatted = [d.strftime('%Y-%m-%d') for d in expiration_dates]

            # Verify the range
            print(f"DEBUG => Generated {len(expiration_dates)} expiration dates to evaluate")
            if expiration_dates:
                print(f"DEBUG => First date: {expiration_dates[0]}, Last date: {expiration_dates[-1]}")
                print(f"DEBUG => First few dates: {dates_formatted[:5]}")

        except Exception as e:
            print(f"DEBUG => Error generating expiration dates: {e}")
            # Fallback to basic date range
            expiration_dates = [
                today + timedelta(days=7),  # 1 week
                today + timedelta(days=30),  # 1 month
                today + timedelta(days=60),  # 2 months
                today + timedelta(days=90),  # 3 months
                today + timedelta(days=180),  # 6 months
                today + timedelta(days=270),  # 9 months
                today + timedelta(days=365),  # 1 year
                today + timedelta(days=365 + 60)  # 1 year + 2 months
            ]
            # Ensure base expiration date is in the list
            if base_expiration_date not in expiration_dates:
                expiration_dates.append(base_expiration_date)
                expiration_dates.sort()
            dates_formatted = [d.strftime('%Y-%m-%d') for d in expiration_dates]

        # ---------------------------------------------------------------
        # STEP 4: Calculate option values for each expiration date
        # ---------------------------------------------------------------
        option_values = []

        # BLACK-76 OPTION CALCULATION
        if option_type == "black76":
            try:
                # Parse parameters
                S = float(all_params[0]) if all_params and len(all_params) > 0 and all_params[0] else 100
                K = float(all_params[1]) if all_params and len(all_params) > 1 and all_params[1] else 100
                r = float(all_params[2]) if all_params and len(all_params) > 2 and all_params[2] else 0.05
                v = float(all_params[3]) if all_params and len(all_params) > 3 and all_params[3] else 0.2

                # Verify parameters
                print(f"DEBUG => Black76 parameters: S={S}, K={K}, r={r}, v={v}")

                # For each potential expiration date, calculate option value
                for exp_date in expiration_dates:
                    # Calculate time to expiry in years (from today to this expiration date)
                    days_to_expiry = (exp_date - today).days

                    # Handle dates in the past
                    if days_to_expiry <= 0:
                        # For expiration dates in the past, option is just intrinsic value
                        if call_put == "C":
                            value = max(0, S - K)
                        else:
                            value = max(0, K - S)
                    else:
                        # Valid future expiration date - calculate Black-76 price
                        T = max(days_to_expiry / 365.0, 0.001)  # Time in years

                        try:
                            option_results = black_76(call_put, S, K, T, r, v)
                            value = option_results[0]  # First return value is option price
                        except Exception as e:
                            print(f"DEBUG => black_76 calculation error for date {exp_date}: {e}")
                            # Fallback to intrinsic value
                            if call_put == "C":
                                value = max(0, S - K)
                            else:
                                value = max(0, K - S)

                    option_values.append(value)

                # Create the data dictionary for the figure
                chart_data = []
                for i in range(len(dates_formatted)):
                    chart_data.append({
                        'date': dates_formatted[i],
                        'value': option_values[i]
                    })

                # Sort the data by date
                chart_data.sort(key=lambda x: x['date'])

                # Extract the sorted data back into separate lists
                sorted_dates = [item['date'] for item in chart_data]
                sorted_values = [item['value'] for item in chart_data]

                # Create figure
                fig = go.Figure()

                # Add option price line with sorted data
                fig.add_trace(
                    go.Scatter(
                        x=sorted_dates,
                        y=sorted_values,
                        mode='lines',
                        name='Option Price',
                        line=dict(color='blue', width=2)
                    )
                )

                # Mark the base expiration date that was selected in the UI
                try:
                    base_exp_str = base_expiration_date.strftime('%Y-%m-%d')
                    if base_exp_str in sorted_dates:
                        base_index = sorted_dates.index(base_exp_str)
                        base_value = sorted_values[base_index]

                        fig.add_trace(
                            go.Scatter(
                                x=[base_exp_str],
                                y=[base_value],
                                mode='markers',
                                name='Base Expiration',
                                marker=dict(color='red', size=10, symbol='star')
                            )
                        )
                except Exception as e:
                    print(f"DEBUG => Error marking base expiration: {e}")

                # Create layout with proper date axis ordering
                option_type_label = "Call" if call_put == "C" else "Put"
                fig.update_layout(
                    title=f"Black-76 {option_type_label} Option Value vs Expiration Date",
                    xaxis_title="Expiration Date",
                    yaxis_title="Option Value",
                    height=400,
                    xaxis=dict(
                        tickangle=-45,
                        type='category',  # Keep as category but ensure data is pre-sorted
                        categoryorder='array',
                        categoryarray=sorted_dates  # Explicitly set the order
                    ),
                    legend=dict(
                        yanchor="top",
                        y=0.99,
                        xanchor="left",
                        x=0.01
                    )
                )

                # Add vertical line at the base expiration date
                base_exp_str = base_expiration_date.strftime('%Y-%m-%d')
                fig.update_layout(
                    shapes=[
                        dict(
                            type="line",
                            xref="x",
                            yref="paper",
                            x0=base_exp_str,
                            y0=0,
                            x1=base_exp_str,
                            y1=1,
                            line=dict(
                                color="red",
                                width=1,
                                dash="dash",
                            )
                        )
                    ],
                    annotations=[
                        dict(
                            x=base_exp_str,
                            y=1,
                            xref="x",
                            yref="paper",
                            text="Selected Expiration",
                            showarrow=False,
                            xanchor="center",
                            yanchor="bottom"
                        )
                    ]
                )

                return fig

            except Exception as e:
                import traceback
                print(f"DEBUG => Error in Black76 calculation section: {e}")
                print(f"DEBUG => Traceback: {traceback.format_exc()}")
                return go.Figure().update_layout(
                    title=f"Error in Black76 calculation: {str(e)}",
                    xaxis_title="Expiration Date",
                    yaxis_title="Option Price",
                    height=400
                )

        # KIRK SPREAD OPTION CALCULATION
        elif option_type == "kirk":
            try:
                # Parse parameters with careful type handling
                S1 = float(all_params[0]) if all_params and len(all_params) > 0 and all_params[0] else 100
                S2 = float(all_params[1]) if all_params and len(all_params) > 1 and all_params[1] else 90
                K_spread = float(all_params[2]) if all_params and len(all_params) > 2 and all_params[2] else 5
                r_spread = float(all_params[3]) if all_params and len(all_params) > 3 and all_params[3] else 0.05
                v1 = float(all_params[4]) if all_params and len(all_params) > 4 and all_params[4] else 0.2
                v2 = float(all_params[5]) if all_params and len(all_params) > 5 and all_params[5] else 0.15
                rho = float(all_params[6]) if all_params and len(all_params) > 6 and all_params[6] else 0.5

                # Verify parameters
                print(
                    f"DEBUG => Kirk parameters: S1={S1}, S2={S2}, K={K_spread}, r={r_spread}, v1={v1}, v2={v2}, rho={rho}")

                # Convert C/P to call/put string
                call_put_expanded = "call" if call_put == "C" else "put"

                # For each potential expiration date, calculate option value
                for exp_date in expiration_dates:
                    # Calculate time to expiry in years (from today to this expiration date)
                    days_to_expiry = (exp_date - today).days

                    # Handle dates in the past
                    if days_to_expiry <= 0:
                        # For expiration dates in the past, option is just intrinsic value
                        if call_put == "C":
                            value = max(0, S1 - S2 - K_spread)
                        else:
                            value = max(0, S2 + K_spread - S1)
                    else:
                        # Valid future expiration date - calculate Kirk price
                        T = max(days_to_expiry / 365.0, 0.001)  # Time in years

                        try:
                            value = kirk_model_with_substitution(
                                S1, S2, K_spread, v1, v2, rho, T, call_put_expanded
                            )
                        except Exception as e:
                            print(f"DEBUG => kirk model calculation error for date {exp_date}: {e}")
                            # Fallback to intrinsic value
                            if call_put == "C":
                                value = max(0, S1 - S2 - K_spread)
                            else:
                                value = max(0, S2 + K_spread - S1)

                    option_values.append(value)

                # Create the data dictionary for the figure
                chart_data = []
                for i in range(len(dates_formatted)):
                    chart_data.append({
                        'date': dates_formatted[i],
                        'value': option_values[i]
                    })

                # Sort the data by date
                chart_data.sort(key=lambda x: x['date'])

                # Extract the sorted data back into separate lists
                sorted_dates = [item['date'] for item in chart_data]
                sorted_values = [item['value'] for item in chart_data]

                # Create figure
                fig = go.Figure()

                # Add option price line with sorted data
                fig.add_trace(
                    go.Scatter(
                        x=sorted_dates,
                        y=sorted_values,
                        mode='lines',
                        name='Option Price',
                        line=dict(color='blue', width=2)
                    )
                )

                # Mark the base expiration date that was selected in the UI
                try:
                    base_exp_str = base_expiration_date.strftime('%Y-%m-%d')
                    if base_exp_str in sorted_dates:
                        base_index = sorted_dates.index(base_exp_str)
                        base_value = sorted_values[base_index]

                        fig.add_trace(
                            go.Scatter(
                                x=[base_exp_str],
                                y=[base_value],
                                mode='markers',
                                name='Base Expiration',
                                marker=dict(color='red', size=10, symbol='star')
                            )
                        )
                except Exception as e:
                    print(f"DEBUG => Error marking base expiration: {e}")

                # Create layout with proper date axis ordering
                option_type_label = "Call" if call_put == "C" else "Put"
                fig.update_layout(
                    title=f"Kirk Spread {option_type_label} Option Value vs Expiration Date",
                    xaxis_title="Expiration Date",
                    yaxis_title="Option Value",
                    height=400,
                    xaxis=dict(
                        tickangle=-45,
                        type='category',  # Keep as category but ensure data is pre-sorted
                        categoryorder='array',
                        categoryarray=sorted_dates  # Explicitly set the order
                    ),
                    legend=dict(
                        yanchor="top",
                        y=0.99,
                        xanchor="left",
                        x=0.01
                    )
                )

                # Add vertical line at the base expiration date
                base_exp_str = base_expiration_date.strftime('%Y-%m-%d')
                fig.update_layout(
                    shapes=[
                        dict(
                            type="line",
                            xref="x",
                            yref="paper",
                            x0=base_exp_str,
                            y0=0,
                            x1=base_exp_str,
                            y1=1,
                            line=dict(
                                color="red",
                                width=1,
                                dash="dash",
                            )
                        )
                    ],
                    annotations=[
                        dict(
                            x=base_exp_str,
                            y=1,
                            xref="x",
                            yref="paper",
                            text="Selected Expiration",
                            showarrow=False,
                            xanchor="center",
                            yanchor="bottom"
                        )
                    ]
                )

                return fig

            except Exception as e:
                import traceback
                print(f"DEBUG => Error in Kirk calculation section: {e}")
                print(f"DEBUG => Traceback: {traceback.format_exc()}")
                return go.Figure().update_layout(
                    title=f"Error in Kirk calculation: {str(e)}",
                    xaxis_title="Expiration Date",
                    yaxis_title="Option Price",
                    height=400
                )

        # Unknown option type
        else:
            return go.Figure().update_layout(
                title=f"Unknown option type: {option_type}",
                xaxis_title="Expiration Date",
                yaxis_title="Option Price",
                height=400
            )

    except Exception as e:
        import traceback
        print(f"DEBUG => Top-level error in update_extension_chart: {e}")
        print(f"DEBUG => Traceback: {traceback.format_exc()}")

        return go.Figure().update_layout(
            title=f"Error creating chart: {str(e)}",
            xaxis_title="Expiration Date",
            yaxis_title="Option Price",
            height=400
        )


@callback(
    Output("correlation-chart", "figure"),
    Input("calculate-button", "n_clicks"),
    [
        State("option-type", "value"),
        State("option-call-put", "value"),
        State({'type': 'param', 'model': ALL, 'param': ALL}, 'value'),
        State({'type': 'param-date', 'model': ALL, 'param': ALL}, 'date')
    ],
    prevent_initial_call=True
)
def update_correlation_chart(n_clicks, option_type, call_put, all_params, all_dates):
    """
    Create a chart showing how correlation affects Kirk spread option price.
    Only applicable for Kirk model - returns empty chart for Black-76.
    """
    if n_clicks is None:
        return go.Figure().update_layout(
            title="Calculate option price first",
            xaxis_title="Correlation (ρ)",
            yaxis_title="Option Price",
            height=400
        )

    # If Black-76 is selected, return a message indicating chart not applicable
    if option_type != "kirk":
        return go.Figure().update_layout(
            title="Correlation Chart (Kirk Spread Options Only)",
            xaxis_title="Correlation (ρ)",
            yaxis_title="Option Price",
            height=400,
            annotations=[
                dict(
                    text="This chart is only applicable for Kirk Spread Options",
                    showarrow=False,
                    xref="paper",
                    yref="paper",
                    x=0.5,
                    y=0.5,
                    font=dict(size=16)
                )
            ]
        )

    try:
        # ---------------------------------------------------------------
        # STEP 1: Get today's date as reference point
        # ---------------------------------------------------------------
        today = date.today()

        # ---------------------------------------------------------------
        # STEP 2: Get expiration date (with careful type handling)
        # ---------------------------------------------------------------
        # Default expiration is 1 year from today
        default_expiration = today + timedelta(days=365)

        try:
            # Check if we have a date from the UI
            if not all_dates or not all_dates[0]:
                print("DEBUG => Using default expiration (no date selected)")
                expiration_date = default_expiration
            else:
                # Try to ensure we have a date object
                date_input = all_dates[0]
                print(f"DEBUG => Date input: {date_input}, type: {type(date_input)}")

                # If it's already a date object, use it directly
                if isinstance(date_input, date):
                    expiration_date = date_input
                    print(f"DEBUG => Using date object directly: {expiration_date}")
                else:
                    # Handle string date
                    try:
                        # First try direct conversion if it's a string with YYYY-MM-DD format
                        if isinstance(date_input, str):
                            # Remove time component if present
                            if 'T' in date_input:
                                date_input = date_input.split('T')[0]

                            # Try to parse using datetime
                            year, month, day = map(int, date_input.split('-'))
                            expiration_date = date(year, month, day)
                            print(f"DEBUG => Parsed date from string: {expiration_date}")
                        else:
                            # Fall back to default if we can't parse
                            print(f"DEBUG => Couldn't parse date, using default. Input was: {date_input}")
                            expiration_date = default_expiration
                    except Exception as e:
                        print(f"DEBUG => Date parsing error: {e}, using default")
                        expiration_date = default_expiration
        except Exception as e:
            print(f"DEBUG => Expiration date handling error: {e}, using default")
            expiration_date = default_expiration

        # Final check to make absolutely sure we have a date object
        if not isinstance(expiration_date, date):
            print(f"DEBUG => Final check: expiration_date is not a date object! Type: {type(expiration_date)}")
            expiration_date = default_expiration

        print(f"DEBUG => Expiration date = {expiration_date}, type: {type(expiration_date)}")

        # ---------------------------------------------------------------
        # STEP 3: Generate correlation values to test
        # ---------------------------------------------------------------
        # Create range of correlation values from -1 to 1 with step 0.05
        correlations = np.arange(-1.0, 1.01, 0.05)

        # ---------------------------------------------------------------
        # STEP 4: Parse other Kirk parameters
        # ---------------------------------------------------------------
        try:
            # Parse parameters with careful type handling
            S1 = float(all_params[0]) if all_params and len(all_params) > 0 and all_params[0] else 100
            S2 = float(all_params[1]) if all_params and len(all_params) > 1 and all_params[1] else 90
            K_spread = float(all_params[2]) if all_params and len(all_params) > 2 and all_params[2] else 5
            r_spread = float(all_params[3]) if all_params and len(all_params) > 3 and all_params[3] else 0.05
            v1 = float(all_params[4]) if all_params and len(all_params) > 4 and all_params[4] else 0.2
            v2 = float(all_params[5]) if all_params and len(all_params) > 5 and all_params[5] else 0.15
            base_rho = float(all_params[6]) if all_params and len(all_params) > 6 and all_params[6] else 0.5

            # Verify parameters
            print(
                f"DEBUG => Kirk parameters: S1={S1}, S2={S2}, K={K_spread}, r={r_spread}, v1={v1}, v2={v2}, rho={base_rho}")

            # Convert C/P to call/put string
            call_put_expanded = "call" if call_put == "C" else "put"

            # Calculate time to expiration in years
            days_to_expiry = (expiration_date - today).days
            T = max(days_to_expiry / 365.0, 0.001)  # Time in years

            # ---------------------------------------------------------------
            # STEP 5: Calculate option values for each correlation
            # ---------------------------------------------------------------
            option_values = []
            valid_correlations = []

            for rho in correlations:
                try:
                    # Calculate option value with this correlation
                    value = kirk_model_with_substitution(
                        S1, S2, K_spread, v1, v2, rho, T, call_put_expanded
                    )
                    option_values.append(value)
                    valid_correlations.append(rho)
                except Exception as e:
                    print(f"DEBUG => kirk model calculation error for rho={rho}: {e}")
                    # Skip this correlation value if calculation fails
                    continue

            # ---------------------------------------------------------------
            # STEP 6: Create correlation chart
            # ---------------------------------------------------------------
            fig = go.Figure()

            # Add option price line
            fig.add_trace(
                go.Scatter(
                    x=valid_correlations,
                    y=option_values,
                    mode='lines',
                    name='Option Price',
                    line=dict(color='blue', width=2)
                )
            )

            # Mark the base correlation from UI input
            try:
                # Find the nearest correlation value to the base correlation
                nearest_idx = min(range(len(valid_correlations)),
                                  key=lambda i: abs(valid_correlations[i] - base_rho))

                fig.add_trace(
                    go.Scatter(
                        x=[valid_correlations[nearest_idx]],
                        y=[option_values[nearest_idx]],
                        mode='markers',
                        name='Base Correlation',
                        marker=dict(color='red', size=10, symbol='star')
                    )
                )
            except Exception as e:
                print(f"DEBUG => Error marking base correlation: {e}")

            # Create layout
            option_type_label = "Call" if call_put == "C" else "Put"
            fig.update_layout(
                title=f"Kirk Spread {option_type_label} Option Value vs Correlation",
                xaxis_title="Correlation (ρ)",
                yaxis_title="Option Value",
                height=400,
                xaxis=dict(
                    tickformat='.2f'
                ),
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01
                )
            )

            # Add vertical line at the base correlation
            fig.update_layout(
                shapes=[
                    dict(
                        type="line",
                        xref="x",
                        yref="paper",
                        x0=base_rho,
                        y0=0,
                        x1=base_rho,
                        y1=1,
                        line=dict(
                            color="red",
                            width=1,
                            dash="dash",
                        )
                    )
                ],
                annotations=[
                    dict(
                        x=base_rho,
                        y=1,
                        xref="x",
                        yref="paper",
                        text=f"ρ = {base_rho:.2f}",
                        showarrow=False,
                        xanchor="center",
                        yanchor="bottom"
                    )
                ]
            )

            return fig

        except Exception as e:
            import traceback
            print(f"DEBUG => Error in correlation chart calculation: {e}")
            print(f"DEBUG => Traceback: {traceback.format_exc()}")
            return go.Figure().update_layout(
                title=f"Error creating correlation chart: {str(e)}",
                xaxis_title="Correlation (ρ)",
                yaxis_title="Option Price",
                height=400
            )

    except Exception as e:
        import traceback
        print(f"DEBUG => Top-level error in update_correlation_chart: {e}")
        print(f"DEBUG => Traceback: {traceback.format_exc()}")

        return go.Figure().update_layout(
            title=f"Error creating correlation chart: {str(e)}",
            xaxis_title="Correlation (ρ)",
            yaxis_title="Option Price",
            height=400
        )
 