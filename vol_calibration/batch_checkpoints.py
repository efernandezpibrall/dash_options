"""Compatibility adapter for options.vol_calibration.calibration.checkpoints."""

from options.vol_calibration.calibration.checkpoints import *  # noqa: F403
from options.vol_calibration.calibration import checkpoints as _implementation


def __getattr__(name):
    return getattr(_implementation, name)
