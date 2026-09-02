import importlib.util
from pathlib import Path

import pytest


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260831_03_inline_vol_trades_calibration.py"
)
AUTHORITY_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260902_02_governed_surface_authority.py"
)


class _Operations:
    def __init__(self):
        self.constraints = []
        self.dropped_constraints = []
        self.indexes = []
        self.statements = []

    def create_check_constraint(self, name, table, expression, **kwargs):
        self.constraints.append((name, table, expression, kwargs))

    def execute(self, statement):
        self.statements.append(str(statement))

    def drop_constraint(self, name, table, **kwargs):
        self.dropped_constraints.append((name, table, kwargs))

    def create_index(self, name, table, columns, **kwargs):
        self.indexes.append((name, table, tuple(columns), kwargs))


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "inline_vol_trades_calibration_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_authority_migration():
    spec = importlib.util.spec_from_file_location(
        "governed_surface_authority_migration", AUTHORITY_MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_additive_after_20260831_02_and_enforces_new_policies(monkeypatch):
    migration = _load_migration()
    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.down_revision == "20260831_02"
    assert {item[0] for item in operations.constraints} == {
        "ck_brent_calibrated_surface_inline_policy",
        "ck_hh_calibrated_surface_lne_policy",
        "ck_jkm_calibrated_surface_inline_policy",
    }
    assert all(item[1] == "implied_volatility_surface_calibrated" for item in operations.constraints)
    assert all(item[3]["schema"] == "at_lng" for item in operations.constraints)
    assert all(item[3]["postgresql_not_valid"] is True for item in operations.constraints)
    assert operations.statements == []
    assert "LNE settlement" in migration.CONSTRAINTS["ck_hh_calibrated_surface_lne_policy"]
    assert "ICE PHE" in migration.CONSTRAINTS["ck_hh_calibrated_surface_lne_policy"]
    assert "jkm_%" in migration.CONSTRAINTS["ck_jkm_calibrated_surface_inline_policy"]
    assert "ICAP" in migration.CONSTRAINTS["ck_jkm_calibrated_surface_inline_policy"]


def test_migration_has_no_destructive_downgrade():
    source = MIGRATION_PATH.read_text()
    migration = _load_migration()

    assert "op.drop_" not in source
    assert "op.alter_" not in source
    with pytest.raises(RuntimeError, match="no safe automatic downgrade"):
        migration.downgrade()


def test_authority_migration_replaces_text_checks_with_five_product_contract(monkeypatch):
    migration = _load_authority_migration()
    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.down_revision == "20260902_01"
    assert {item[0] for item in operations.dropped_constraints} == set(
        migration.OLD_CONSTRAINTS
    )
    assert {item[0] for item in operations.constraints} == set(
        migration.CONSTRAINTS
    )
    common = migration.CONSTRAINTS[
        "ck_calibrated_surface_five_product_dense_policy"
    ]
    gas = migration.CONSTRAINTS[
        "ck_calibrated_surface_euro_gas_source_policy"
    ]
    assert "'BRENT', 'HH', 'JKM', 'NBP', 'TTF'" in common
    assert "PCHIP-core/Wing-v2-tail" in common
    assert "official_surface_euro_gas_lng_v" in gas
    assert operations.indexes == [
        (
            "uq_calibrated_surface_publication_contract_strike",
            "implied_volatility_surface_calibrated",
            ("publication_id", "contract_date", "strike"),
            {"unique": True, "schema": "at_lng"},
        )
    ]
    assert operations.statements == ["SET LOCAL lock_timeout = '5s'"]


def test_authority_migration_has_no_automatic_downgrade():
    migration = _load_authority_migration()

    with pytest.raises(RuntimeError, match="no safe automatic downgrade"):
        migration.downgrade()
