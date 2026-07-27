import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260727_01_vol_calibration_scaffolding.py"
)


class _OperationRecorder:
    def __init__(self):
        self.tables = {}
        self.indexes = {}
        self.statements = []

    def execute(self, statement):
        self.statements.append(str(statement))

    def create_table(self, name, *objects, **kwargs):
        self.tables[name] = {"objects": objects, "kwargs": kwargs}

    def create_index(self, name, table, columns, **kwargs):
        self.indexes[name] = {
            "table": table,
            "columns": columns,
            "kwargs": kwargs,
        }


def _load_migration():
    spec = importlib.util.spec_from_file_location("vol_calibration_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_additive_migration_creates_all_governed_storage(monkeypatch):
    migration = _load_migration()
    operations = _OperationRecorder()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert set(operations.tables) == {
        "vol_calibration_runs",
        "vol_calibration_expiry_results",
        "vol_calibration_audit_events",
        "vol_surface_publications",
        "implied_volatility_surface_calibrated",
        "option_expiry_calendar",
        "vol_calibration_jobs",
        "vol_calibration_job_items",
    }
    assert all(table["kwargs"]["schema"] == "at_lng" for table in operations.tables.values())
    assert any(
        isinstance(item, sa.UniqueConstraint)
        and item.name == "uq_vol_calibration_runs_idempotency"
        for item in operations.tables["vol_calibration_runs"]["objects"]
    )
    assert any(
        isinstance(item, sa.UniqueConstraint)
        and item.name == "uq_vol_calibration_jobs_idempotency"
        for item in operations.tables["vol_calibration_jobs"]["objects"]
    )

    active_index = operations.indexes[
        "uq_vol_surface_publications_active_product_cob"
    ]
    assert active_index["kwargs"]["unique"] is True
    assert str(active_index["kwargs"]["postgresql_where"]) == "is_active"


def test_migration_guards_immutability_lifecycle_and_worker_leases(monkeypatch):
    migration = _load_migration()
    operations = _OperationRecorder()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()
    sql = "\n".join(operations.statements)

    assert "calibration run inputs are immutable" in sql
    assert "invalid calibration run status transition" in sql
    assert "new calibration runs must start as draft" in sql
    assert "publication identity is immutable" in sql
    assert "new publications must be approved and inactive" in sql
    assert "is append-only" in sql
    assert "BEFORE UPDATE OR DELETE" in sql


def test_migration_has_no_destructive_downgrade_or_drop_operations():
    migration = _load_migration()
    source = MIGRATION_PATH.read_text()

    assert "op.drop_" not in source
    assert "op.alter_" not in source
    with pytest.raises(RuntimeError, match="no destructive downgrade"):
        migration.downgrade()
