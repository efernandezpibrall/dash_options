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

    if not config.has_section("DATABASE"):
        config.add_section("DATABASE")
    if not config.has_section("TRINOS"):
        config.add_section("TRINOS")

    env_mappings = {
        ("DATABASE", "CONNECTION_STRING"): ("DATABASE_URL", "OPTIONS_DATABASE_URL"),
        ("DATABASE", "MIDDLE_OFFICE_CONNECTION_STRING"): ("MIDDLE_OFFICE_DATABASE_URL",),
        ("DATABASE", "SCHEMA"): ("DB_SCHEMA", "OPTIONS_DB_SCHEMA"),
        ("TRINOS", "HOST"): ("TRINOS_HOST",),
        ("TRINOS", "USERNAME"): ("TRINOS_USERNAME",),
        ("TRINOS", "TOKEN"): ("TRINOS_TOKEN",),
        ("TRINOS", "PORT"): ("TRINOS_PORT",),
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
