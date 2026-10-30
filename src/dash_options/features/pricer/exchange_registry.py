"""Compatibility adapter; numerical implementation is owned by options.contracts.exchange_registry."""
from options.contracts import exchange_registry as _implementation

__all__ = [name for name in vars(_implementation) if not name.startswith("_")]

def __getattr__(name):
    return getattr(_implementation, name)
