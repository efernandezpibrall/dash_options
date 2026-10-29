"""Keep unit tests independent of live database configuration."""

import psycopg2
import pytest

_CONNECT = psycopg2.connect


@pytest.fixture(autouse=True)
def forbid_live_postgres_connections(monkeypatch):
    owned_ports = set()
    def blocked(*args, **kwargs):
        # Connection arguments may contain credentials; keep this guard's frame
        # out of pytest's failure rendering while retaining the caller traceback.
        __tracebackhide__ = True
        if (not args and kwargs.get("host") == "127.0.0.1"
                and int(kwargs.get("port", 0)) in owned_ports
                and kwargs.get("user") == "settlement_test" and kwargs.get("dbname") == "postgres"):
            return _CONNECT(**kwargs)
        pytest.fail("Unit test attempted a live PostgreSQL connection; patch the current data owner.")
    monkeypatch.setattr(psycopg2, "connect", blocked)
    # Only the owned-cluster fixture registers its randomly allocated local port.
    return owned_ports


# Database-backed tests use only a per-test disposable cluster.
import shutil


@pytest.fixture
def blank_database(forbid_live_postgres_connections):
    from scripts.verification.verify_settlement_pipeline import temporary_postgres
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("Local PostgreSQL binaries are required for persistence verification")
    with temporary_postgres() as engine:
        forbid_live_postgres_connections.add(engine.url.port)
        yield engine


@pytest.fixture
def database(blank_database):
    from scripts.verification.verify_settlement_pipeline import apply_migrations
    apply_migrations(blank_database)
    return blank_database
