"""Compatibility adapter for options.vol_calibration.adjustments.brent."""

from options.vol_calibration.adjustments.brent import *  # noqa: F403
from options.vol_calibration.adjustments import brent as _implementation


def __getattr__(name):
    return getattr(_implementation, name)
