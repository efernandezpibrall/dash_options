from types import SimpleNamespace

import health


def test_liveness_and_read_only_readiness(monkeypatch):
    monkeypatch.setattr(health, "writes_enabled", lambda: False)
    monkeypatch.setattr(
        health, "ttf_intraday_writes_enabled", lambda: False
    )
    monkeypatch.setattr(health, "ttf_publication_enabled", lambda: False)
    monkeypatch.setattr(health, "intraday_refresh_enabled", lambda: False)
    monkeypatch.setattr(health, "settlement_refresh_enabled", lambda: False)
    from index_options import server

    client = server.test_client()
    assert client.get("/health/live").get_json() == {"status": "live"}

    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.get_json()["mode"] == "read-only"


def test_write_readiness_fails_closed_without_proxy_auth(monkeypatch):
    monkeypatch.setattr(health, "writes_enabled", lambda: True)
    monkeypatch.setattr(health, "background_jobs_enabled", lambda: False)
    monkeypatch.setenv("OPTIONS_AUTH_MODE", "disabled")

    ready, details = health.readiness_status()

    assert ready is False
    assert details["error"] == "Authentication is disabled."


def test_write_readiness_checks_required_relations(monkeypatch):
    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _statement, parameters):
            return _Result(parameters["relation"])

    monkeypatch.setattr(health, "writes_enabled", lambda: True)
    monkeypatch.setattr(health, "background_jobs_enabled", lambda: True)
    monkeypatch.setattr(
        health,
        "get_database_engine",
        lambda required=False: SimpleNamespace(connect=lambda: _Connection()),
    )
    monkeypatch.setenv("OPTIONS_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("OPTIONS_TRUSTED_PROXY_SHARED_SECRET", "test-secret")

    ready, details = health.readiness_status()

    assert ready is True
    assert details["mode"] == "write-enabled"
    assert details["auth_mode"] == "trusted_proxy"


def test_bloomberg_refresh_readiness_requires_queue_and_snapshot_relations(monkeypatch):
    requested = []

    class _Result:
        def scalar_one_or_none(self):
            return "available"

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _statement, parameters):
            requested.append(parameters["relation"])
            return _Result()

    monkeypatch.setattr(health, "writes_enabled", lambda: False)
    monkeypatch.setattr(health, "ttf_intraday_writes_enabled", lambda: False)
    monkeypatch.setattr(health, "intraday_refresh_enabled", lambda: False)
    monkeypatch.setattr(health, "settlement_refresh_enabled", lambda: True)
    monkeypatch.setattr(
        health,
        "get_database_engine",
        lambda required=False: SimpleNamespace(connect=lambda: _Connection()),
    )
    monkeypatch.setenv("OPTIONS_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("OPTIONS_TRUSTED_PROXY_SHARED_SECRET", "test-secret")

    ready, _details = health.readiness_status()

    assert ready is True
    assert requested == [
        "at_lng.vol_market_snapshots",
        "at_lng.bbg_option_chain",
        "at_lng.bbg_option_chain_refresh_jobs",
    ]
