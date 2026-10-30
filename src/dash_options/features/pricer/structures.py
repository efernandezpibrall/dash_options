"""Compatibility adapter; numerical implementation is owned by options.valuation.structures."""
from options.valuation import structures as _implementation

__all__ = [name for name in vars(_implementation) if not name.startswith("_")]

def __getattr__(name):
    return getattr(_implementation, name)

from options.valuation.api import (calculate_structure, payoff_series, time_decay_series,
    parallel_volatility_series, rate_sensitivity_series, correlation_sensitivity_series,
    expiration_extension_series)
