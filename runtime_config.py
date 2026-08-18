"""Runtime configuration resolved without requiring credentials at import time."""

from __future__ import annotations

import configparser
import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine


def _config_candidates() -> list[Path]:
    explicit = os.getenv("OPTIONS_CONFIG_PATH") or os.getenv("OPTIONS_CONFIG_FILE")
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())

    repo_dir = Path(__file__).resolve().parent
    candidates.extend(
        [
            repo_dir / "config.ini",
            repo_dir.parent / "config.ini",
            Path.cwd() / "config.ini",
        ]
    )
    return candidates


@lru_cache(maxsize=1)
def load_runtime_config() -> configparser.ConfigParser:
    """Load the first available optional config file and environment overrides."""
    config = configparser.ConfigParser(interpolation=None)
    for candidate in _config_candidates():
        if candidate.is_file():
            config.read(candidate)
            break

    for section in (
        "DATABASE",
        "TRINOS",
        "ASPECT",
        "NETWORK",
        "VOL_CALIBRATION",
        "OPTIONS_AUTH",
        "BLOOMBERG_OPTIONS",
    ):
        if not config.has_section(section):
            config.add_section(section)

    env_mappings = {
        ("DATABASE", "CONNECTION_STRING"): ("DATABASE_URL", "OPTIONS_DATABASE_URL"),
        ("DATABASE", "MIDDLE_OFFICE_CONNECTION_STRING"): ("MIDDLE_OFFICE_DATABASE_URL",),
        ("DATABASE", "SCHEMA"): ("DB_SCHEMA", "OPTIONS_DB_SCHEMA"),
        ("TRINOS", "HOST"): ("TRINOS_HOST",),
        ("TRINOS", "USERNAME"): ("TRINOS_USERNAME",),
        ("TRINOS", "TOKEN"): ("TRINOS_TOKEN",),
        ("TRINOS", "PORT"): ("TRINOS_PORT",),
        ("TRINOS", "VERIFY_SSL"): ("TRINOS_VERIFY_SSL",),
        ("ASPECT", "USERNAME"): ("ASPECT_USERNAME",),
        ("ASPECT", "PASSWORD"): ("ASPECT_PASSWORD",),
        ("ASPECT", "BASE_URL"): ("ASPECT_BASE_URL",),
        ("ASPECT", "BOOK"): ("ASPECT_BOOK",),
        ("ASPECT", "VERIFY_SSL"): ("ASPECT_VERIFY_SSL",),
        ("ASPECT", "REQUEST_TIMEOUT_SECONDS"): ("ASPECT_REQUEST_TIMEOUT_SECONDS",),
        ("NETWORK", "PROXY_URL"): ("OPTIONS_PROXY_URL", "PROXY_URL"),
        ("VOL_CALIBRATION", "ENABLED"): ("VOL_CALIBRATION_ENABLED",),
        ("VOL_CALIBRATION", "WRITES_ENABLED"): (
            "VOL_CALIBRATION_WRITES_ENABLED",
        ),
        ("VOL_CALIBRATION", "PUBLISH_ENABLED"): (
            "VOL_CALIBRATION_PUBLISH_ENABLED",
        ),
        ("VOL_CALIBRATION", "BACKGROUND_JOBS_ENABLED"): (
            "VOL_CALIBRATION_BACKGROUND_JOBS_ENABLED",
        ),
        ("VOL_CALIBRATION", "TTF_INTRADAY_WRITES_ENABLED"): (
            "VOL_CALIBRATION_TTF_INTRADAY_WRITES_ENABLED",
        ),
        ("VOL_CALIBRATION", "TTF_PUBLICATION_ENABLED"): (
            "VOL_CALIBRATION_TTF_PUBLICATION_ENABLED",
        ),
        ("OPTIONS_AUTH", "MODE"): ("OPTIONS_AUTH_MODE",),
        ("OPTIONS_AUTH", "LOCAL_USER"): ("OPTIONS_LOCAL_AUTH_USER",),
        ("OPTIONS_AUTH", "LOCAL_ROLES"): ("OPTIONS_LOCAL_AUTH_ROLES",),
        ("OPTIONS_AUTH", "TRUSTED_PROXY_SHARED_SECRET"): (
            "OPTIONS_TRUSTED_PROXY_SHARED_SECRET",
        ),
        ("OPTIONS_AUTH", "TRUSTED_PROXY_SECRET_HEADER"): (
            "OPTIONS_TRUSTED_PROXY_SECRET_HEADER",
        ),
        ("OPTIONS_AUTH", "TRUSTED_PROXY_USER_HEADER"): (
            "OPTIONS_TRUSTED_PROXY_USER_HEADER",
        ),
        ("OPTIONS_AUTH", "TRUSTED_PROXY_ROLES_HEADER"): (
            "OPTIONS_TRUSTED_PROXY_ROLES_HEADER",
        ),
        ("BLOOMBERG_OPTIONS", "INTRADAY_REFRESH_ENABLED"): (
            "BBG_OPTION_CHAIN_INTRADAY_REFRESH_ENABLED",
        ),
        ("BLOOMBERG_OPTIONS", "SETTLEMENT_REFRESH_ENABLED"): (
            "BBG_OPTION_CHAIN_SETTLEMENT_REFRESH_ENABLED",
        ),
        ("BLOOMBERG_OPTIONS", "ENABLED_PRODUCTS"): (
            "BBG_OPTION_CHAIN_ENABLED_PRODUCTS",
        ),
    }
    for (section, option), names in env_mappings.items():
        for name in names:
            value = os.getenv(name)
            if value:
                config.set(section, option, value)
                break
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


@lru_cache(maxsize=2)
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
    return create_engine(connection_string, pool_pre_ping=True)


def clear_runtime_config_cache():
    """Clear cached settings and engines; intended for tests and explicit reloads."""
    get_database_engine.cache_clear()
    load_runtime_config.cache_clear()
