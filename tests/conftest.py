"""Keep unit tests independent of live database configuration."""

import psycopg2
import pytest


@pytest.fixture(autouse=True)
def forbid_live_postgres_connections(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Unit test attempted a live PostgreSQL connection; patch the current data owner.")
    monkeypatch.setattr(psycopg2, "connect", blocked)
