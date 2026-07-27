from types import SimpleNamespace

import health


def test_liveness_and_read_only_readiness():
    from index_options import server

    client = server.test_client()
    assert client.get("/health/live").get_json() == {"status": "live"}

    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.get_json()["mode"] == "read-only"


def test_write_readiness_fails_closed_without_proxy_auth(monkeypatch):
    monkeypatch.setattr(health, "writes_enabled", lambda: True)
    monkeypatch.setattr(health, "background_jobs_enabled", lambda: False)
    monkeypatch.delenv("OPTIONS_TRUSTED_PROXY_AUTH_ENABLED", raising=False)

    ready, details = health.readiness_status()

    assert ready is False
    assert details["error"] == "trusted proxy authentication is not enabled"


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
    monkeypatch.setenv("OPTIONS_TRUSTED_PROXY_AUTH_ENABLED", "true")

    ready, details = health.readiness_status()

    assert ready is True
    assert details["mode"] == "write-enabled"
