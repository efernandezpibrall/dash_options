import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from options.option_contract_conventions import CONTRACT_CONVENTIONS


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260831_04_fractional_option_quantities.py"
)
GENERIC_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260902_01_generic_option_families.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_20260831_04_fractional_option_quantities",
        MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_generic_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_20260902_01_generic_option_families",
        GENERIC_MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_fractional_quantity_migration_follows_existing_head():
    migration = _load_migration()

    assert migration.revision == "20260831_04"
    assert migration.down_revision == "20260831_03"


def test_upgrade_changes_only_valuation_quantity_to_numeric_30_6(monkeypatch):
    migration = _load_migration()
    calls = []

    class RecordingOperations:
        def get_bind(self):
            return object()

        def alter_column(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(migration, "_dependent_view_snapshot", lambda bind: None)
    monkeypatch.setattr(migration, "_restore_dependent_view", lambda bind, snapshot: None)
    monkeypatch.setattr(migration, "op", RecordingOperations())
    migration.upgrade()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("trades_options_valuation", "quantity")
    assert kwargs["schema"] == "at_lng"
    assert isinstance(kwargs["existing_type"], sa.BigInteger)
    assert isinstance(kwargs["type_"], sa.Numeric)
    assert kwargs["type_"].precision == 30
    assert kwargs["type_"].scale == 6
    assert kwargs["postgresql_using"] == "quantity::numeric(30,6)"


def test_fractional_quantity_migration_has_no_lossy_downgrade():
    migration = _load_migration()

    with pytest.raises(RuntimeError, match="no safe automatic downgrade"):
        migration.downgrade()


def test_fractional_quantity_migration_preserves_dependent_current_view():
    source = MIGRATION_PATH.read_text()

    assert "pg_get_viewdef" in source
    assert "role_table_grants" in source
    assert "ALTER VIEW" in source
    assert "dependent views" in source


def test_generic_option_family_migration_follows_fractional_quantities():
    migration = _load_generic_migration()

    assert migration.revision == "20260902_01"
    assert migration.down_revision == "20260831_04"
    conventions = {row[0]: row for row in migration.CONVENTIONS}
    assert conventions["ICE_JKM_71090519"][3] == "asian76"
    assert conventions["ICE_JKM_71090519"][-1] == (
        "JKM_CONTINUOUS_16_TO_EXPIRY_V1"
    )
    assert conventions["CME_JKM_JKO_869"][-1] == (
        "JKM_PLATTS_PUBLICATION_DAYS_16_TO_15_V1"
    )
    assert conventions["ICE_BRENT_AMERICAN_218"][-2] == "black76"


def test_generic_option_family_manifest_exactly_matches_runtime_code():
    migration = _load_generic_migration()
    database_manifest = {
        row[0]: row[1:]
        for row in migration.CONVENTIONS
    }
    code_manifest = {
        code: (
            convention.venue,
            convention.product_code,
            convention.pricing_model,
            convention.exercise_style,
            convention.margin_style,
            convention.settlement_type,
            convention.premium_currency,
            convention.discount_curve_required,
            convention.source_url,
            convention.pricer_pricing_model,
            convention.asian_averaging_rule_code,
        )
        for code, convention in CONTRACT_CONVENTIONS.items()
    }

    assert database_manifest == code_manifest


def test_generic_option_family_migration_persists_required_provenance():
    source = GENERIC_MIGRATION_PATH.read_text()

    for column in (
        "pricer_pricing_model",
        "asian_averaging_rule_code",
        "pricing_model_application",
        "model_horizon_date",
        "discount_factor_to_model_horizon",
        "asian_averaging_schedule",
    ):
        assert column in source
    assert "trades_options_supported_convention_ck" in source
    assert "trades_options_valuation_asian_schedule_ck" in source
