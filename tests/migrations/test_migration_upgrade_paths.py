"""Run real Alembic upgrades against owned PostgreSQL, including revision stamps.

Only the pre-existing, externally owned trade/catalogue schema is synthetic.
Every Dashboard revision runs through migrations/env.py without exclusions.
"""

from itertools import combinations, permutations
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from dash_options.core import config as runtime_config
from dash_options.core.health import _migration_lineage_contains
from scripts.maintenance.retire_brent_history import TABLES
from scripts.verification.verify_settlement_pipeline import prepare_external_calibration_fixture


ROOT = Path(__file__).resolve().parents[2]
OLD_HEADS = ("20261021_01", "20261029_01", "20261029_02")
MERGED_HEAD = "20261029_03"


@pytest.fixture
def migration_database(blank_database, monkeypatch):
    """Supply external schema prerequisites, never bypass a Dashboard migration."""
    with blank_database.begin() as db:
        db.execute(text("CREATE SCHEMA at_lng"))
        columns = ["quantity bigint", "model text"]
        for suffix in ("a", "b"):
            columns.extend(f"{name}_{suffix} {kind}" for name, kind in (
                ("volatility_adjustment_components", "jsonb"),
                ("maturity_date_type", "text"), ("asset", "text"),
                ("surface_expiry_date", "date"), ("surface_expiry_status", "text"),
                ("volatility_reference_convention", "text"),
                ("volatility_reference_convention_version", "text"),
                ("variance_calendar_code", "text"), ("variance_calendar_version", "text"),
                ("business_days_to_trade_expiry", "integer"),
                ("business_days_to_surface_expiry", "integer"),
                ("vol_adjustment_factor", "numeric"),
            ))
        db.execute(text("CREATE TABLE at_lng.trades_options_valuation (" + ", ".join(columns) +
                        ", CONSTRAINT trades_options_valuation_adjustment_components_ck CHECK (true))"))
        db.execute(text("INSERT INTO at_lng.trades_options_valuation(quantity, model) VALUES (7, 'black76')"))
        db.execute(text("""CREATE TABLE at_lng.trades_options (
            model text, contract_convention_code text,
            CONSTRAINT trades_options_supported_convention_ck CHECK (true));
            CREATE TABLE at_lng.option_contract_conventions (
                convention_code text PRIMARY KEY, venue text, product_code text,
                pricing_model text, exercise_style text, margin_style text, settlement_type text,
                premium_currency text, discount_curve_required boolean, source_url text,
                reviewed_at timestamptz, active boolean)
        """))
        prepare_external_calibration_fixture(db)
    url = blank_database.url.render_as_string(hide_password=False)

    def owned_url(section, key):
        assert (section, key) == ("DATABASE", "CONNECTION_STRING")
        return url

    monkeypatch.setattr(runtime_config, "config_value", owned_url)
    return blank_database, Config(str(ROOT / "alembic.ini"))


def table_oids(engine):
    with engine.connect() as db:
        return {name: db.execute(text("SELECT to_regclass(:name)::oid"),
                                {"name": "at_lng." + name}).scalar() for name in TABLES}


def current_heads(engine):
    with engine.connect() as db:
        return set(db.execute(text("SELECT version_num FROM at_lng.alembic_version")).scalars())


def assert_retired(engine):
    assert not any(table_oids(engine).values())
    assert current_heads(engine) == {MERGED_HEAD}
    with engine.connect() as db:
        assert db.execute(text("SELECT quantity FROM at_lng.trades_options_valuation")).scalar_one() == 7
        assert db.execute(text("SELECT to_regclass('at_lng.vol_publication_stages')")).scalar()


def recreate_with_old_heads(config):
    # This was a valid ordering before convergence: retirement, then the
    # independent history/staging branch, which recreates all five tables.
    for head in ("20261029_02", "20261029_01", "20261021_01"):
        command.upgrade(config, head)


def test_fresh_upgrade_does_not_recreate_retired_history(migration_database):
    engine, config = migration_database
    command.upgrade(config, "heads")
    assert_retired(engine)
    command.upgrade(config, "head")
    assert_retired(engine)


@pytest.mark.parametrize("starting_heads", [
    subset for count in range(1, len(OLD_HEADS) + 1) for subset in combinations(OLD_HEADS, count)
])
def test_existing_old_heads_converge_and_retain_readiness(migration_database, starting_heads):
    engine, config = migration_database
    for head in starting_heads:
        command.upgrade(config, head)
    assert current_heads(engine) == set(starting_heads)
    command.upgrade(config, "head")
    assert_retired(engine)
    for required in (*OLD_HEADS, "20260902_02", "20260928_01", "20261023_01"):
        assert _migration_lineage_contains(MERGED_HEAD, required)


@pytest.mark.parametrize("branch_order", list(permutations(OLD_HEADS)))
def test_retirement_survives_every_old_branch_order(migration_database, branch_order):
    engine, config = migration_database
    for head in branch_order:
        command.upgrade(config, head)
    command.upgrade(config, "head")
    assert_retired(engine)


@pytest.mark.parametrize("already_absent", [TABLES[:3], TABLES])
def test_merge_accepts_partial_or_complete_prior_retirement(migration_database, already_absent):
    engine, config = migration_database
    recreate_with_old_heads(config)
    with engine.begin() as db:
        for name in already_absent:
            db.execute(text("DROP TABLE at_lng." + name + " RESTRICT"))
    command.upgrade(config, "head")
    assert_retired(engine)


def test_populated_history_blocks_merge_without_dropping_tables_or_advancing_head(migration_database):
    engine, config = migration_database
    recreate_with_old_heads(config)
    before = table_oids(engine)
    with engine.begin() as db:
        db.execute(text("""INSERT INTO at_lng.brent_realized_history_runs
            (run_id, start_date, end_date, input_fingerprint, input_payload, status)
            VALUES ('00000000-0000-0000-0000-000000000001', '2026-10-01', '2026-10-02',
                    'must-retain', '{"proof":"original"}', 'building')"""))
    with pytest.raises(RuntimeError, match="archived and retired"):
        command.upgrade(config, "head")
    assert table_oids(engine) == before
    assert current_heads(engine) == set(OLD_HEADS)
    with engine.connect() as db:
        assert db.execute(text("SELECT input_payload FROM at_lng.brent_realized_history_runs")).scalar_one() == {
            "proof": "original"
        }


def test_dependency_failure_rolls_back_drops_and_version_then_allows_retry(migration_database):
    engine, config = migration_database
    recreate_with_old_heads(config)
    before = table_oids(engine)
    with engine.begin() as db:
        db.execute(text("""CREATE VIEW at_lng.unexpected_history_consumer AS
            SELECT * FROM at_lng.brent_realized_history_runs"""))
    with pytest.raises(DBAPIError, match="depend"):
        command.upgrade(config, "head")
    assert table_oids(engine) == before
    assert current_heads(engine) == set(OLD_HEADS)
    with engine.begin() as db:
        db.execute(text("DROP VIEW at_lng.unexpected_history_consumer"))
    command.upgrade(config, "head")
    assert_retired(engine)


def test_downgrade_does_not_recreate_empty_history_or_change_revision(migration_database):
    engine, config = migration_database
    command.upgrade(config, "head")
    with pytest.raises(RuntimeError, match="verified full archive"):
        command.downgrade(config, "20261029_02")
    assert_retired(engine)
