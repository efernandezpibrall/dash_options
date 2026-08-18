import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260803_01_vol_market_snapshots.py"
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
    spec = importlib.util.spec_from_file_location("vol_market_snapshot_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_migration_is_additive_append_only_and_timestamped(monkeypatch):
    migration = _load_migration()
    operations = _OperationRecorder()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.down_revision == "20260727_01"
    assert set(operations.tables) == {
        "vol_market_snapshots",
        "vol_market_option_quotes",
        "vol_market_forwards",
    }
    snapshot_columns = {
        item.name
        for item in operations.tables["vol_market_snapshots"]["objects"]
        if isinstance(item, sa.Column)
    }
    assert {"snapshot_id", "business_date", "observed_at", "input_fingerprint"}.issubset(
        snapshot_columns
    )
    sql = "\n".join(operations.statements)
    assert "append-only" in sql
    assert sql.count("BEFORE UPDATE OR DELETE") == 3


def test_snapshot_migration_has_no_destructive_downgrade():
    migration = _load_migration()
    source = MIGRATION_PATH.read_text()

    assert "op.drop_" not in source
    with pytest.raises(RuntimeError, match="no destructive downgrade"):
        migration.downgrade()
