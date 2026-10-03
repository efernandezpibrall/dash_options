import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import dialect

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


class _QueryResult:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def first(self):
        return self.value

    def all(self):
        return self.value

    def scalar_one(self):
        return self.value


class _MigrationRecorder:
    """Record executed migration operations without replacing its view helpers."""

    dialect = dialect()

    def __init__(self, query_results):
        self.query_results = iter(query_results)
        self.executions = []
        self.changes = []
        self.alterations = []

    def get_bind(self):
        return self

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.executions.append((sql, params))
        if sql.startswith("SELECT"):
            return _QueryResult(next(self.query_results))
        self.changes.append(sql)
        return _QueryResult(None)

    def alter_column(self, *args, **kwargs):
        self.alterations.append((args, kwargs))
        self.changes.append("ALTER valuation quantity")


def _assert_fractional_alteration(operations):
    assert len(operations.alterations) == 1
    args, kwargs = operations.alterations[0]
    assert args == ("trades_options_valuation", "quantity")
    assert kwargs["schema"] == "at_lng"
    assert isinstance(kwargs["existing_type"], sa.BigInteger)
    assert isinstance(kwargs["type_"], sa.Numeric)
    assert kwargs["type_"].precision == 30
    assert kwargs["type_"].scale == 6
    assert kwargs["postgresql_using"] == "quantity::numeric(30,6)"


def test_upgrade_changes_only_valuation_quantity_when_current_view_is_absent(monkeypatch):
    migration = _load_migration()
    operations = _MigrationRecorder([None])
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    _assert_fractional_alteration(operations)
    assert operations.changes == ["ALTER valuation quantity"]
    assert operations.executions[0][1] == {
        "schema": "at_lng", "view_name": "trades_options_valuation_current",
    }


def test_fractional_quantity_migration_has_no_lossy_downgrade():
    migration = _load_migration()

    with pytest.raises(RuntimeError, match="no safe automatic downgrade"):
        migration.downgrade()


def test_fractional_quantity_migration_preserves_current_view_metadata_and_order(monkeypatch):
    migration = _load_migration()
    operations = _MigrationRecorder([
        {
            "view_definition": "SELECT quantity FROM at_lng.trades_options_valuation",
            "owner_name": "pricing owner",
            "comment": "trader's audit",
        },
        True,
        [],
        [
            {"grantee": "PUBLIC", "privilege_type": "SELECT", "is_grantable": "NO"},
            {"grantee": "risk desk", "privilege_type": "SELECT", "is_grantable": "YES"},
        ],
    ])
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    _assert_fractional_alteration(operations)
    view = "at_lng.trades_options_valuation_current"
    assert operations.changes == [
        'DROP VIEW "at_lng"."trades_options_valuation_current"',
        "ALTER valuation quantity",
        f"CREATE VIEW {view} AS SELECT quantity FROM at_lng.trades_options_valuation",
        f"GRANT SELECT ON {view} TO PUBLIC",
        f'GRANT SELECT ON {view} TO "risk desk" WITH GRANT OPTION',
        f"COMMENT ON VIEW {view} IS 'trader''s audit'",
        f'ALTER VIEW {view} OWNER TO "pricing owner"',
    ]
    assert [params for sql, params in operations.executions if sql.startswith("SELECT")] == [
        {"schema": "at_lng", "view_name": "trades_options_valuation_current"},
        {"owner_name": "pricing owner"},
        {"qualified_view": view},
        {"schema": "at_lng", "view_name": "trades_options_valuation_current"},
    ]


@pytest.mark.parametrize(
    ("may_manage", "dependants", "message"),
    [
        (False, [], "must run as owner"),
        (True, [("at_lng", "risk_report")], "found dependent views"),
    ],
)
def test_fractional_migration_refuses_unsafe_view_change_before_mutation(
    monkeypatch, may_manage, dependants, message,
):
    migration = _load_migration()
    operations = _MigrationRecorder([
        {"view_definition": "SELECT quantity", "owner_name": "owner", "comment": None},
        may_manage,
        dependants,
    ])
    monkeypatch.setattr(migration, "op", operations)

    with pytest.raises(RuntimeError, match=message):
        migration.upgrade()

    assert operations.changes == []
    assert operations.alterations == []


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
