"""Model-version policy for calibration calculations and visuals."""

from options.calibration_engine.models.wing_model import WING_V2
from options.calibration_engine.calibration import (
    calibrate as _calibrate,
    evaluate_fit as _evaluate_fit,
)


DEFAULT_CALIBRATION_MODEL_VERSION = WING_V2


def calibrate_v2(*args, **kwargs):
    kwargs.setdefault("model_version", DEFAULT_CALIBRATION_MODEL_VERSION)
    return _calibrate(*args, **kwargs)


def evaluate_fit_v2(*args, **kwargs):
    kwargs.setdefault("model_version", DEFAULT_CALIBRATION_MODEL_VERSION)
    return _evaluate_fit(*args, **kwargs)
