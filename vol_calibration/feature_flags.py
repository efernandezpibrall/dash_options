"""Feature flags for the staged calibration migration."""

from __future__ import annotations

import os

from runtime_config import config_bool


def _enabled(name: str, option: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return config_bool("VOL_CALIBRATION", option, fallback=default)
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def calibration_enabled() -> bool:
    return _enabled("VOL_CALIBRATION_ENABLED", "ENABLED", True)


def writes_enabled() -> bool:
    return calibration_enabled() and _enabled(
        "VOL_CALIBRATION_WRITES_ENABLED", "WRITES_ENABLED", False
    )


def publication_enabled() -> bool:
    return writes_enabled() and _enabled(
        "VOL_CALIBRATION_PUBLISH_ENABLED", "PUBLISH_ENABLED", False
    )


def background_jobs_enabled() -> bool:
    return writes_enabled() and _enabled(
        "VOL_CALIBRATION_BACKGROUND_JOBS_ENABLED",
        "BACKGROUND_JOBS_ENABLED",
        False,
    )


def ttf_intraday_writes_enabled() -> bool:
    return calibration_enabled() and _enabled(
        "VOL_CALIBRATION_TTF_INTRADAY_WRITES_ENABLED",
        "TTF_INTRADAY_WRITES_ENABLED",
        False,
    )


def ttf_publication_enabled() -> bool:
    return ttf_intraday_writes_enabled() and _enabled(
        "VOL_CALIBRATION_TTF_PUBLICATION_ENABLED",
        "TTF_PUBLICATION_ENABLED",
        False,
    )


def jkm_writes_enabled() -> bool:
    return calibration_enabled() and _enabled(
        "VOL_CALIBRATION_JKM_WRITES_ENABLED",
        "JKM_WRITES_ENABLED",
        False,
    )


def jkm_publication_enabled() -> bool:
    return jkm_writes_enabled() and _enabled(
        "VOL_CALIBRATION_JKM_PUBLICATION_ENABLED",
        "JKM_PUBLICATION_ENABLED",
        False,
    )
