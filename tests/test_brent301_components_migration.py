import importlib.util
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260831_02_brent301_components.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_20260831_02_brent301_components",
        MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_brent301_component_constraint_is_the_next_revision():
    migration = _load_migration()

    assert migration.revision == "20260831_02"
    assert migration.down_revision == "20260806_01"


def test_component_constraint_preserves_calendars_and_adds_brent301():
    migration = _load_migration()

    for suffix in ("a", "b"):
        condition = migration._leg_constraint(suffix)
        assert f"lower(trim(maturity_date_type_{suffix})) = 'calendar'" in condition
        assert (
            f"jsonb_array_length(volatility_adjustment_components_{suffix}) = 12"
            in condition
        )
        assert f"lower(trim(maturity_date_type_{suffix})) = 'brent301'" in condition
        assert f"asset_{suffix} = 'ICE_BRENT_FUTURES'" in condition
        assert (
            f"jsonb_array_length(volatility_adjustment_components_{suffix}) = 3"
            in condition
        )
        assert f"surface_expiry_date_{suffix} IS NULL" in condition
        assert f"vol_adjustment_factor_{suffix} IS NULL" in condition


def test_upgrade_replaces_then_validates_the_constraint(monkeypatch):
    migration = _load_migration()
    calls = []

    class RecordingOperations:
        def drop_constraint(self, *args, **kwargs):
            calls.append(("drop", args, kwargs))

        def create_check_constraint(self, *args, **kwargs):
            calls.append(("create", args, kwargs))

        def execute(self, statement):
            calls.append(("execute", statement))

    monkeypatch.setattr(migration, "op", RecordingOperations())

    migration.upgrade()

    assert [call[0] for call in calls] == ["drop", "create", "execute"]
    assert calls[0][1][0] == migration.CONSTRAINT
    assert calls[1][2]["postgresql_not_valid"] is True
    assert "VALIDATE CONSTRAINT" in calls[2][1]
