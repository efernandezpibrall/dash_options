"""Bounded process-local cache for read-only calibration workspaces."""

from __future__ import annotations

import hashlib
import os
from datetime import date
from functools import wraps
from typing import Callable

import pandas as pd

from source_identity import source_config_fingerprint
from workspace_cache import WorkspaceLoadCache
from dash import ctx
from dash.exceptions import MissingCallbackContextException


NO_CALLBACK_CONTEXT = object()


def _positive_int_setting(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    value = int(raw_value)
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value


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
        def wrapper(trade_date, reload_clicks, *context_args):
            triggered_id = triggered_id_factory()
            force_refresh = (
                bool(reload_clicks)
                if triggered_id is NO_CALLBACK_CONTEXT
                else triggered_id == f"{product.lower()}-reload-btn"
            )
            key = (
                product.upper(),
                _date_key(trade_date, default_date_factory),
                hashlib.sha256(
                    (
                        fingerprint_factory()
                        + "|"
                        + repr(context_args)
                    ).encode("utf-8")
                ).hexdigest(),
            )
            return cache.get_or_load(
                key,
                lambda: loader(trade_date, reload_clicks, *context_args),
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
