from datetime import date

from vol_calibration.data_cache import (
    NO_CALLBACK_CONTEXT,
    WorkspaceLoadCache,
    cached_workspace_callback,
    source_config_fingerprint,
)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _callback_result(label, *, synthetic=False):
    source = "Synthetic" if synthetic else "PostgreSQL"
    return (
        f"market-{label}",
        f"params-{label}",
        {"badge": source},
        f"Data Source: {source}",
    )


def test_warm_product_and_date_uses_cached_callback_result():
    clock = _Clock()
    cache = WorkspaceLoadCache(max_entries=4, clock=clock)
    calls = []

    @cached_workspace_callback(
        "TTF",
        lambda: date(2026, 7, 24),
        cache=cache,
        fingerprint_factory=lambda: "source-a",
        triggered_id_factory=lambda: NO_CALLBACK_CONTEXT,
    )
    def loader(trade_date, reload_clicks):
        calls.append((trade_date, reload_clicks))
        return _callback_result(len(calls))

    first = loader("2026-07-24", None)
    second = loader("2026-07-24", None)

    assert first == second
    assert first is not second
    assert calls == [("2026-07-24", None)]


def test_reload_click_bypasses_and_replaces_cached_snapshot():
    cache = WorkspaceLoadCache(max_entries=4)
    calls = []

    @cached_workspace_callback(
        "JKM",
        lambda: date(2026, 7, 24),
        cache=cache,
        fingerprint_factory=lambda: "source-a",
        triggered_id_factory=lambda: NO_CALLBACK_CONTEXT,
    )
    def loader(trade_date, reload_clicks):
        calls.append((trade_date, reload_clicks))
        return _callback_result(len(calls))

    first = loader("2026-07-24", None)
    refreshed = loader("2026-07-24", 1)
    warm = loader("2026-07-24", None)

    assert refreshed != first
    assert warm == refreshed
    assert len(calls) == 2


def test_source_configuration_is_part_of_the_cache_key():
    cache = WorkspaceLoadCache(max_entries=4)
    source = {"fingerprint": "source-a"}
    calls = []

    @cached_workspace_callback(
        "BRENT",
        lambda: date(2026, 7, 24),
        cache=cache,
        fingerprint_factory=lambda: source["fingerprint"],
        triggered_id_factory=lambda: NO_CALLBACK_CONTEXT,
    )
    def loader(trade_date, reload_clicks):
        calls.append((trade_date, reload_clicks))
        return _callback_result(len(calls))

    first = loader("2026-07-24", None)
    source["fingerprint"] = "source-b"
    changed = loader("2026-07-24", None)

    assert changed != first
    assert len(calls) == 2


def test_synthetic_fallback_expires_quickly_but_healthy_data_stays_warm(
    monkeypatch,
):
    monkeypatch.setenv("VOL_CALIBRATION_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("VOL_CALIBRATION_SYNTHETIC_CACHE_TTL_SECONDS", "5")
    clock = _Clock()
    cache = WorkspaceLoadCache(max_entries=4, clock=clock)
    calls = []

    @cached_workspace_callback(
        "HH",
        lambda: date(2026, 7, 24),
        cache=cache,
        fingerprint_factory=lambda: "source-a",
        triggered_id_factory=lambda: NO_CALLBACK_CONTEXT,
    )
    def loader(trade_date, reload_clicks):
        calls.append((trade_date, reload_clicks))
        return _callback_result(
            len(calls),
            synthetic=len(calls) == 1,
        )

    synthetic = loader("2026-07-24", None)
    clock.now = 4.9
    assert loader("2026-07-24", None) == synthetic
    clock.now = 5.1
    recovered = loader("2026-07-24", None)
    clock.now = 100
    assert loader("2026-07-24", None) == recovered
    assert len(calls) == 2


def test_stale_reload_count_does_not_refresh_a_date_triggered_callback():
    cache = WorkspaceLoadCache(max_entries=4)
    trigger = {"id": "ttf-reload-btn"}
    calls = []

    @cached_workspace_callback(
        "TTF",
        lambda: date(2026, 7, 24),
        cache=cache,
        fingerprint_factory=lambda: "source-a",
        triggered_id_factory=lambda: trigger["id"],
    )
    def loader(trade_date, reload_clicks):
        calls.append((trade_date, reload_clicks))
        return _callback_result(len(calls))

    reloaded = loader("2026-07-24", 1)
    trigger["id"] = "ttf-date-picker"
    warm = loader("2026-07-24", 1)

    assert warm == reloaded
    assert len(calls) == 1


def test_cache_is_bounded_by_lru_entry_count():
    cache = WorkspaceLoadCache(max_entries=2)
    for day in ("2026-07-22", "2026-07-23", "2026-07-24"):
        cache.get_or_load(
            ("TTF", day, "source-a"),
            lambda day=day: _callback_result(day),
            force_refresh=False,
            degraded=lambda value: False,
            healthy_ttl_seconds=300,
            degraded_ttl_seconds=5,
        )

    assert len(cache._entries) == 2
    assert ("TTF", "2026-07-22", "source-a") not in cache._entries


def test_source_fingerprint_changes_when_explicit_config_changes(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.ini"
    config_path.write_text("[DATABASE]\nSCHEMA=at_lng\n")
    monkeypatch.setenv("OPTIONS_CONFIG_PATH", str(config_path))
    first = source_config_fingerprint()

    config_path.write_text("[DATABASE]\nSCHEMA=alternate\n")

    assert source_config_fingerprint() != first
