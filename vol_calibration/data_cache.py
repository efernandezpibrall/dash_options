"""Bounded process-local cache for read-only calibration workspaces."""

from __future__ import annotations

import copy
import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Callable

import pandas as pd
from dash import ctx
from dash.exceptions import MissingCallbackContextException


SOURCE_ENVIRONMENT_NAMES = (
    "OPTIONS_CONFIG_PATH",
    "OPTIONS_CONFIG_FILE",
    "OPTIONS_DATABASE_URL",
    "DATABASE_URL",
    "OPTIONS_DB_SCHEMA",
    "DB_SCHEMA",
    "OPTIONS_TRINOS_HOST",
    "TRINOS_HOST",
    "OPTIONS_TRINOS_PORT",
    "TRINOS_PORT",
    "OPTIONS_TRINOS_USERNAME",
    "TRINOS_USERNAME",
    "OPTIONS_TRINOS_TOKEN",
    "TRINOS_TOKEN",
    "OPTIONS_TRINOS_HTTP_SCHEME",
    "TRINOS_HTTP_SCHEME",
    "OPTIONS_TRINOS_VERIFY_SSL",
    "TRINOS_VERIFY_SSL",
    "VOL_CALIBRATION_SOURCE_CACHE_NAMESPACE",
)
NO_CALLBACK_CONTEXT = object()


def _positive_int_setting(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    value = int(raw_value)
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value


def source_config_fingerprint() -> str:
    """Hash source-affecting configuration without exposing credential values."""
    digest = hashlib.sha256()
    for name in SOURCE_ENVIRONMENT_NAMES:
        value = os.getenv(name, "")
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\0")

    configured_path = os.getenv("OPTIONS_CONFIG_PATH") or os.getenv("OPTIONS_CONFIG_FILE")
    if configured_path:
        path = Path(configured_path).expanduser()
    else:
        repo_dir = Path(__file__).resolve().parents[1]
        candidates = (
            repo_dir / "config.ini",
            repo_dir.parent / "config.ini",
            Path.cwd() / "config.ini",
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), None)

    if path is not None:
        digest.update(str(path.resolve(strict=False)).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


@dataclass
class _CacheEntry:
    value: object
    expires_at: float


class WorkspaceLoadCache:
    """Small thread-safe LRU/TTL cache with per-key load serialization."""

    def __init__(
        self,
        *,
        max_entries: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_entries <= 0:
            raise ValueError("max_entries must be positive.")
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str, str], _CacheEntry] = (
            OrderedDict()
        )
        self._key_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _get(self, key):
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return copy.deepcopy(entry.value)

    def _put(self, key, value, ttl_seconds: int) -> None:
        with self._lock:
            self._entries[key] = _CacheEntry(
                value=copy.deepcopy(value),
                expires_at=self._clock() + ttl_seconds,
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get_or_load(
        self,
        key: tuple[str, str, str],
        loader: Callable[[], object],
        *,
        force_refresh: bool,
        degraded: Callable[[object], bool],
        healthy_ttl_seconds: int,
        degraded_ttl_seconds: int,
    ):
        if force_refresh:
            with self._lock:
                self._entries.pop(key, None)
        else:
            cached = self._get(key)
            if cached is not None:
                return cached

        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())

        try:
            with key_lock:
                if not force_refresh:
                    cached = self._get(key)
                    if cached is not None:
                        return cached
                value = loader()
                ttl_seconds = (
                    degraded_ttl_seconds
                    if degraded(value)
                    else healthy_ttl_seconds
                )
                self._put(key, value, ttl_seconds)
                return copy.deepcopy(value)
        finally:
            with self._lock:
                if self._key_locks.get(key) is key_lock:
                    self._key_locks.pop(key, None)


WORKSPACE_LOAD_CACHE = WorkspaceLoadCache(
    max_entries=_positive_int_setting("VOL_CALIBRATION_CACHE_MAX_ENTRIES", 32)
)


def clear_workspace_load_cache() -> None:
    WORKSPACE_LOAD_CACHE.clear()


def _date_key(trade_date, default_date_factory: Callable[[], date]) -> str:
    if trade_date is None:
        return default_date_factory().isoformat()
    parsed = pd.to_datetime(trade_date, errors="coerce")
    if pd.isna(parsed):
        return str(trade_date)
    return parsed.date().isoformat()


def _is_degraded_callback_result(value) -> bool:
    rendered = str(value).casefold()
    return any(
        marker in rendered
        for marker in ("synthetic", "unavailable", "calibration blocked", "no exact-cob")
    )


def _triggered_id():
    try:
        return ctx.triggered_id
    except MissingCallbackContextException:
        return NO_CALLBACK_CONTEXT


def cached_workspace_callback(
    product: str,
    default_date_factory: Callable[[], date],
    *,
    cache: WorkspaceLoadCache = WORKSPACE_LOAD_CACHE,
    fingerprint_factory: Callable[[], str] = source_config_fingerprint,
    triggered_id_factory: Callable[[], object] = _triggered_id,
):
    """Cache an existing page loader by product/date/source configuration."""

    def decorator(loader):
        @wraps(loader)
        def wrapper(trade_date, reload_clicks):
            triggered_id = triggered_id_factory()
            force_refresh = (
                bool(reload_clicks)
                if triggered_id is NO_CALLBACK_CONTEXT
                else triggered_id == f"{product.lower()}-reload-btn"
            )
            key = (
                product.upper(),
                _date_key(trade_date, default_date_factory),
                fingerprint_factory(),
            )
            return cache.get_or_load(
                key,
                lambda: loader(trade_date, reload_clicks),
                force_refresh=force_refresh,
                degraded=_is_degraded_callback_result,
                healthy_ttl_seconds=_positive_int_setting(
                    "VOL_CALIBRATION_CACHE_TTL_SECONDS",
                    300,
                ),
                degraded_ttl_seconds=_positive_int_setting(
                    "VOL_CALIBRATION_SYNTHETIC_CACHE_TTL_SECONDS",
                    5,
                ),
            )

        return wrapper

    return decorator
