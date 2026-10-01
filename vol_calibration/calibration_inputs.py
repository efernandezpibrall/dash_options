"""Compatibility adapter for options.vol_calibration.inputs.eligibility."""

from options.vol_calibration.inputs.eligibility import *  # noqa: F403
from options.vol_calibration.inputs import eligibility as _implementation


def __getattr__(name):
    return getattr(_implementation, name)
