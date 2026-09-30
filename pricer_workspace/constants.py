"""Pricer workspace constants and control options."""

from __future__ import annotations

from pricer_exchange_registry import exchange_mapping_options
from pricer_structure import (
    PREMIUM_CONVENTION_LABELS,
    SUPPORTED_ASSETS,
    SUPPORTED_PREMIUM_CONVENTIONS,
)


EXCHANGE_WORKFLOW = "exchange"


OTC_WORKFLOW = "otc"


EXCHANGE_MAPPING_OPTIONS = tuple(exchange_mapping_options())


EXCHANGE_MODEL_OPTIONS = {
    "TTF": ({"label": "ICE TTF option", "value": "black76"},),
    "JKM": (
        {"label": "JKM average price option", "value": "asian76"},
        {"label": "JKM vanilla option", "value": "black76"},
    ),
    "HH": ({"label": "ICE Henry Hub PHE option", "value": "black76"},),
    "Brent": ({"label": "ICE Brent option", "value": "black76"},),
    "NBP": ({"label": "ICE NBP option", "value": "black76"},),
}


option_types = [
    {"label": "Black-76", "value": "black76"},
    {"label": "Asian-76", "value": "asian76"},
    {"label": "Kirk", "value": "kirk"},
]


asset_options = [{"label": asset, "value": asset} for asset in SUPPORTED_ASSETS]


premium_convention_options = [
    {"label": PREMIUM_CONVENTION_LABELS[value], "value": value}
    for value in SUPPORTED_PREMIUM_CONVENTIONS
]


COMPACT_DELIVERY_SHAPE_LABELS = {
    "MONTH": "Month",
    "Q1": "Q1",
    "Q2": "Q2",
    "Q3": "Q3",
    "Q4": "Q4",
    "SUM": "Summer",
    "WIN": "Winter",
}


MAX_PRICER_DECIMALS = 20


PRICER_CHART_FONT = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'


PRICER_CHART_TEXT = "#0f172a"


PRICER_CHART_MUTED = "#64748b"


PRICER_CHART_GRID = "rgba(148, 163, 184, 0.18)"


PRICER_CHART_AXIS = "#94a3b8"


PRICER_GRAPH_CONFIG = {
    "displayModeBar": "hover",
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}


PRICER_WORKSPACE_SCHEMA_VERSION = 9


PRICER_CONTRACT_SIZE_MIGRATION_SCHEMA_VERSION = 7


DEFAULT_STRUCTURE_ID = "structure-1"


VOLATILITY_ADJUSTMENT_FIELDS = (
    "atm_vol_adjustment",
    "skew_vol_adjustment",
    "smile_vol_adjustment",
)


VOLATILITY_ADJUSTMENT_SCALE = 0.01


MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT = 50.0


FUTURES_STYLE_RATE_NOTE = (
    "The futures-style premium convention is undiscounted; the risk-free rate "
    "and Rho are not applicable."
)


UPFRONT_RATE_NOTE = (
    "Risk-free rate used to discount the upfront option premium and calculate Rho."
)


_CURRENT_PRICER_COLUMN_WIDTHS = {
    "name": (86, 86),
    "ratio": (50, 64),
    "call_put": (70, 82),
    "strike": (60, 60),
    "surface_input_vol": (70, 82),
    "surface_atm_input_vol": (58, 72),
    "surface_skew_input_vol": (58, 72),
    "atm_vol_adjustment": (58, 72),
    "skew_vol_adjustment": (58, 72),
    "smile_vol_adjustment": (58, 72),
    "surface_pricing_vol": (72, 82),
    "unit_value": (68, 80),
    "trade_value": (74, 96),
    "volatility_asset_1": (104, 118),
    "volatility_asset_2": (104, 118),
    "surface_volatility_asset_1": (104, 118),
    "surface_volatility_asset_2": (104, 118),
}


_CURRENT_PRICER_LONG_GREEK_WIDTHS = {
    "gamma_s1s2": 96,
    "corr_sensitivity": 88,
    "vega_equiv": 92,
}


EXCHANGE_STRUCTURE_ID = "exchange-structure-1"


EXCHANGE_WORKSPACE_SCHEMA_VERSION = 2
