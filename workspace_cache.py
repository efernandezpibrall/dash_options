"""Thread-safe process-local cache shared by read-only data services."""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable


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

