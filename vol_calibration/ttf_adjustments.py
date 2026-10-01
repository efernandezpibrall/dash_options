"""Compatibility adapter for options.vol_calibration.adjustments.ttf."""

from options.vol_calibration.adjustments.ttf import *  # noqa: F403
from options.vol_calibration.adjustments import ttf as _implementation


def __getattr__(name):
    return getattr(_implementation, name)
