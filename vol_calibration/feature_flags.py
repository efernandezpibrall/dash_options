"""Feature flags for the staged calibration migration."""

from __future__ import annotations

import os


def _enabled(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def calibration_enabled() -> bool:
    return _enabled("VOL_CALIBRATION_ENABLED", True)


def writes_enabled() -> bool:
    return calibration_enabled() and _enabled("VOL_CALIBRATION_WRITES_ENABLED", False)


def publication_enabled() -> bool:
    return writes_enabled() and _enabled("VOL_CALIBRATION_PUBLISH_ENABLED", False)
