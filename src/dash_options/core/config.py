"""Runtime configuration resolved without requiring credentials at import time."""

from __future__ import annotations

import configparser
from functools import lru_cache

from options.market_data.connection_pool import get_shared_database_engine, reset_database_pools


from options.market_data.runtime_config import load_config


@lru_cache(maxsize=1)
def load_runtime_config() -> configparser.ConfigParser:
    """Load the first available optional config file and environment overrides."""
    config = load_config(allow_empty=True)
    for section in ("DATABASE", "TRINOS", "ASPECT", "NETWORK", "VOL_CALIBRATION", "OPTIONS_AUTH", "BLOOMBERG_OPTIONS"):
        if not config.has_section(section):
            config.add_section(section)
    return config


def config_value(section: str, option: str, fallback=None):
    return load_runtime_config().get(section, option, fallback=fallback)


def config_bool(section: str, option: str, *, fallback: bool) -> bool:
    raw_value = config_value(section, option)
    if raw_value is None:
        return fallback
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{section}.{option} must be a boolean value.")


def get_database_engine(*, middle_office: bool = False, required: bool = True):
    """Create a cached engine only when a data operation actually needs one."""
    option = "MIDDLE_OFFICE_CONNECTION_STRING" if middle_office else "CONNECTION_STRING"
    connection_string = config_value("DATABASE", option)
    if middle_office and not connection_string:
        connection_string = config_value("DATABASE", "CONNECTION_STRING")
    if not connection_string:
        if required:
            variable = "MIDDLE_OFFICE_DATABASE_URL" if middle_office else "DATABASE_URL"
            raise RuntimeError(
                f"Database configuration is unavailable. Set {variable} or OPTIONS_CONFIG_PATH."
            )
        return None
    return get_shared_database_engine(connection_string)


def clear_runtime_config_cache():
    """Clear cached settings and engines; intended for tests and explicit reloads."""
    reset_database_pools()
    load_runtime_config.cache_clear()
