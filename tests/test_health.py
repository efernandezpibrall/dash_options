from types import SimpleNamespace

import health


def _disable_calibration_mutations(monkeypatch):
    for name in (
        "ttf_intraday_writes_enabled",
        "ttf_publication_enabled",
        "jkm_writes_enabled",
        "jkm_publication_enabled",
        "nbp_writes_enabled",
        "nbp_publication_enabled",
        "brent_writes_enabled",
        "brent_publication_enabled",
        "hh_writes_enabled",
        "hh_publication_enabled",
    ):
        monkeypatch.setattr(health, name, lambda: False)


def test_liveness_and_read_only_readiness(monkeypatch):
    _disable_calibration_mutations(monkeypatch)
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
    _disable_calibration_mutations(monkeypatch)
    monkeypatch.setattr(health, "writes_enabled", lambda: True)
    monkeypatch.setattr(health, "background_jobs_enabled", lambda: False)
    monkeypatch.setenv("OPTIONS_AUTH_MODE", "disabled")

    ready, details = health.readiness_status()

    assert ready is False
    assert details["error"] == "Authentication is disabled."


def test_write_readiness_checks_required_relations(monkeypatch):
    _disable_calibration_mutations(monkeypatch)
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
    _disable_calibration_mutations(monkeypatch)
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


def test_hh_publication_requires_new_head_and_complete_lne_source(monkeypatch):
    _disable_calibration_mutations(monkeypatch)
    monkeypatch.setattr(health, "writes_enabled", lambda: False)
    monkeypatch.setattr(health, "background_jobs_enabled", lambda: False)
    monkeypatch.setattr(health, "hh_writes_enabled", lambda: True)
    monkeypatch.setattr(health, "hh_publication_enabled", lambda: True)
    monkeypatch.setattr(health, "intraday_refresh_enabled", lambda: False)
    monkeypatch.setattr(health, "settlement_refresh_enabled", lambda: False)
    monkeypatch.setenv("OPTIONS_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("OPTIONS_TRUSTED_PROXY_SHARED_SECRET", "test-secret")

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar_one(self):
            return self.value

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, parameters=None):
            sql = str(statement)
            if "to_regclass" in sql:
                return _Result(parameters["relation"])
            if "alembic_version" in sql:
                return _Result("20260902_02")
            if "SELECT EXISTS" in sql:
                return _Result(True)
            raise AssertionError(sql)

    monkeypatch.setattr(
        health,
        "get_database_engine",
        lambda required=False: SimpleNamespace(connect=lambda: _Connection()),
    )

    ready, details = health.readiness_status()

    assert ready is True
    assert details["migration_head"] == "20260902_02"
    assert details["lne_settlement_available"] is True


def test_publication_readiness_fails_on_stale_migration_head(monkeypatch):
    _disable_calibration_mutations(monkeypatch)
    monkeypatch.setattr(health, "writes_enabled", lambda: False)
    monkeypatch.setattr(health, "background_jobs_enabled", lambda: False)
    monkeypatch.setattr(health, "jkm_writes_enabled", lambda: True)
    monkeypatch.setattr(health, "jkm_publication_enabled", lambda: True)
    monkeypatch.setattr(health, "intraday_refresh_enabled", lambda: False)
    monkeypatch.setattr(health, "settlement_refresh_enabled", lambda: False)
    monkeypatch.setenv("OPTIONS_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("OPTIONS_TRUSTED_PROXY_SHARED_SECRET", "test-secret")

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

        def execute(self, statement, parameters=None):
            if "to_regclass" in str(statement):
                return _Result(parameters["relation"])
            return _Result("20260831_02")

    monkeypatch.setattr(
        health,
        "get_database_engine",
        lambda required=False: SimpleNamespace(connect=lambda: _Connection()),
    )

    ready, details = health.readiness_status()

    assert ready is False
    assert details["required_migration_head"] == "20260902_02"


def test_publication_migration_requirement_accepts_descendant_head():
    assert health._migration_lineage_contains("20260902_02", "20260902_02") is True
    assert health._migration_lineage_contains("20260902_01", "20260902_02") is False
