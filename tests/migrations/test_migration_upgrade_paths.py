"""Real baseline/archive upgrades in disposable PostgreSQL, with no exclusions."""

import shutil
import subprocess
import sys

from alembic import command
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from dash_options.core import health
from scripts.maintenance.upgrade_legacy_schema import (
    CHECKPOINT,
    archived_migrations,
    migration_config,
    upgrade_legacy,
)
from scripts.maintenance.retire_brent_history import TABLES as RETIRED
from scripts.verification.verify_settlement_pipeline import (
    ROOT,
    prepare_external_schema_fixture,
    temporary_postgres,
)
from scripts.verification.rehearse_daily_release import rehearse_schema
from tests.migrations.support import catalog, canonical_catalog, data_state, oids, seed_sentinels


@pytest.fixture
def migration_database(blank_database):
    with blank_database.begin() as db:
        prepare_external_schema_fixture(db)
    return blank_database


def migrate(engine, target="head", *, legacy=False):
    with engine.begin() as db:
        if legacy:
            with archived_migrations() as archive:
                command.upgrade(migration_config(db, legacy=archive), target)
        else:
            command.upgrade(migration_config(db), target)


def heads(engine):
    with engine.connect() as db:
        return list(db.execute(text("SELECT version_num FROM at_lng.alembic_version ORDER BY 1")).scalars())


def snapshot(engine):
    return catalog(engine), data_state(engine), oids(engine)


def assert_retired(engine):
    with engine.connect() as db:
        assert not any(
            db.execute(text("SELECT to_regclass(:name)"), {"name": "at_lng." + name}).scalar() for name in RETIRED
        )
    assert heads(engine) == [CHECKPOINT]


def rows(engine):
    with engine.connect() as db:
        tables = db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='at_lng' ORDER BY 1")).scalars()
        result = {}
        for name in tables:
            values = [dict(row) for row in db.execute(text("SELECT * FROM at_lng." + name + " ORDER BY 1")).mappings()]
            # Review time is installation-specific; every policy value must match.
            if name == "option_contract_conventions":
                for row in values:
                    row.pop("reviewed_at")
            result[name] = values
        return result


def test_single_baseline_and_checksum_pinned_archive():
    scripts = ScriptDirectory(str(ROOT / "migrations"))
    assert scripts.get_heads() == [CHECKPOINT]
    assert len(list(scripts.walk_revisions())) == 1
    assert not list((ROOT / "migrations").glob("*.sql"))
    with archived_migrations() as archive:
        historical = ScriptDirectory(str(archive))
        assert len(list(historical.walk_revisions())) == 27
        assert len(list(archive.glob("*.sql"))) == 3
        assert set(scripts.get_revision(CHECKPOINT).module.supersedes_revisions) == {
            revision.revision for revision in historical.walk_revisions() if revision.revision != CHECKPOINT
        }


def test_fresh_baseline_matches_full_archive_schema_and_every_seed(
    migration_database, forbid_live_postgres_connections
):
    baseline = migration_database
    migrate(baseline)
    with temporary_postgres() as legacy:
        forbid_live_postgres_connections.add(legacy.url.port)
        with legacy.begin() as db:
            prepare_external_schema_fixture(db)
        migrate(legacy, legacy=True)
        assert canonical_catalog(catalog(baseline)) == canonical_catalog(catalog(legacy))
        assert rows(baseline) == rows(legacy)
    assert_retired(baseline)
    before = snapshot(baseline)
    migrate(baseline)
    assert snapshot(baseline) == before


def test_current_checkpoint_is_select_only_and_preserves_data(migration_database):
    engine = migration_database
    migrate(engine, legacy=True)
    seed_sentinels(engine)
    before = snapshot(engine)
    statements = []
    with engine.begin() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))

        def reject_writes(conn, cursor, statement, parameters, context, executemany):
            assert statement.lstrip().upper().startswith(("SELECT", "SHOW")), statement
            statements.append(statement)

        event.listen(db, "before_cursor_execute", reject_writes)
        command.upgrade(migration_config(db), "head")
    assert statements
    assert snapshot(engine) == before
    # Existing rollback artifacts recognize the unchanged checkpoint.
    migrate(engine, legacy=True)
    assert snapshot(engine) == before


@pytest.mark.parametrize("checkpoint", ("20261021_01", "20261029_01", "20261029_02", "20261028_01"))
def test_older_checkpoints_require_and_can_use_archived_upgrade(migration_database, checkpoint):
    engine = migration_database
    migrate(engine, checkpoint, legacy=True)
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="archived"):
        migrate(engine)
    assert snapshot(engine) == before
    with engine.begin() as db:
        preview = upgrade_legacy(db)
    assert not preview["applied"] and snapshot(engine) == before
    with engine.begin() as db:
        result = upgrade_legacy(db, apply=True)
    assert result["after"] == [CHECKPOINT]
    before = snapshot(engine)
    migrate(engine)
    assert snapshot(engine) == before
    assert_retired(engine)


def test_partial_multibranch_upgrade_is_rejected_without_writes(migration_database):
    engine = migration_database
    for checkpoint in ("20261021_01", "20261029_01"):
        migrate(engine, checkpoint, legacy=True)
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="archived"):
        migrate(engine)
    assert snapshot(engine) == before
    with engine.begin() as db:
        upgrade_legacy(db, apply=True)
    assert_retired(engine)


def test_missing_shared_schema_does_not_create_anything(blank_database):
    before = catalog(blank_database)
    with pytest.raises(RuntimeError, match="external at_lng"):
        migrate(blank_database)
    assert catalog(blank_database) == before


def test_missing_external_table_rolls_back_version_creation(migration_database):
    engine = migration_database
    with engine.begin() as db:
        db.execute(text("DROP TABLE at_lng.trades_options"))
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="external trade/catalogue"):
        migrate(engine)
    assert snapshot(engine) == before


def test_conflicting_sofr_seed_rolls_back_all_schema_and_seed_changes(migration_database):
    engine = migration_database
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO at_lng.option_discount_curve_definitions VALUES "
                "('USD_SOFR_BBG490_MID_V1','EUR','ACT/365.25','continuous','WRONG',true)"
            )
        )
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="Conflicting SOFR"):
        migrate(engine)
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "relation", ("vol_calibration_runs", "brent_realized_history_runs", "live_forward_curve_heads")
)
def test_unversioned_dashboard_or_retired_relation_is_never_stamped(migration_database, relation):
    engine = migration_database
    with engine.begin() as db:
        db.execute(text("CREATE TABLE at_lng." + relation + " (id integer PRIMARY KEY)"))
        db.execute(text("INSERT INTO at_lng." + relation + " VALUES (1)"))
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="unversioned dashboard"):
        migrate(engine)
    assert snapshot(engine) == before
    with pytest.raises(RuntimeError, match="No recorded"), engine.begin() as db:
        upgrade_legacy(db, apply=True)


def test_downgrade_refuses_without_recreating_history(migration_database):
    engine = migration_database
    migrate(engine)
    seed_sentinels(engine)
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="downgrade is unsupported"), engine.begin() as db:
        command.downgrade(migration_config(db), "base")
    assert snapshot(engine) == before


def test_external_view_grants_owner_comment_and_fractional_quantities_survive(migration_database):
    engine = migration_database
    with engine.begin() as db:
        db.execute(
            text(
                "CREATE VIEW at_lng.trades_options_valuation_current AS SELECT quantity FROM at_lng.trades_options_valuation"
            )
        )
        db.execute(text("COMMENT ON VIEW at_lng.trades_options_valuation_current IS 'desk view'"))
        db.execute(text("GRANT SELECT ON at_lng.trades_options_valuation_current TO PUBLIC"))
    migrate(engine)
    seed_sentinels(engine)
    with engine.connect() as db:
        assert (
            str(db.execute(text("SELECT quantity FROM at_lng.trades_options_valuation_current")).scalar()) == "1.234567"
        )
        assert (
            db.execute(
                text("SELECT obj_description('at_lng.trades_options_valuation_current'::regclass,'pg_class')")
            ).scalar()
            == "desk view"
        )
        assert (
            db.execute(
                text(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='at_lng.trades_options_valuation_current'::regclass"
                )
            ).scalar()
            == "settlement_test"
        )
        assert db.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_class c,LATERAL aclexplode(c.relacl) a WHERE c.oid='at_lng.trades_options_valuation_current'::regclass AND a.grantee=0 AND a.privilege_type='SELECT')"
            )
        ).scalar()


def test_baseline_guards_preserve_append_only_history_and_convention_rules(migration_database):
    engine = migration_database
    migrate(engine)
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO at_lng.vol_market_snapshots(snapshot_id,commodity,business_date,observed_at,source_name,input_fingerprint,status,option_quote_count,forward_count,metadata) VALUES ('22222222-2222-2222-2222-222222222222','BRENT','2026-10-29',now(),'test','test','complete',0,0,'{}')"
            )
        )
    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as db:
            db.execute(text("DELETE FROM at_lng.vol_market_snapshots"))
    with pytest.raises(DBAPIError, match="asian_rule"):
        with engine.begin() as db:
            db.execute(
                text(
                    "UPDATE at_lng.option_contract_conventions SET asian_averaging_rule_code=NULL WHERE pricing_model='asian76'"
                )
            )


def test_archived_retirement_refuses_populated_history_and_dependency_failures(migration_database):
    engine = migration_database
    migrate(engine, "20261021_01", legacy=True)
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO at_lng.brent_realized_history_runs(run_id,start_date,end_date,input_fingerprint,input_payload,status) VALUES ('00000000-0000-0000-0000-000000000001','2026-10-01','2026-10-02','retain','{}','building')"
            )
        )
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="archived and retired"), engine.begin() as db:
        upgrade_legacy(db, apply=True)
    assert snapshot(engine) == before
    with engine.begin() as db:
        db.execute(text("DELETE FROM at_lng.brent_realized_history_runs"))
        db.execute(text("CREATE VIEW at_lng.unexpected_consumer AS SELECT * FROM at_lng.brent_realized_history_runs"))
    before = snapshot(engine)
    with pytest.raises(DBAPIError, match="depend"):
        with engine.begin() as db:
            upgrade_legacy(db, apply=True)
    assert snapshot(engine) == before
    with engine.begin() as db:
        db.execute(text("DROP VIEW at_lng.unexpected_consumer"))
        upgrade_legacy(db, apply=True)
    assert_retired(engine)


def test_graph_loads_without_mutable_analytics_metadata():
    probe = """
import sys
class NoMutableAnalytics:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('options.market_data.live_forward_curves', 'options.market_data.live_forward_curves'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoMutableAnalytics())
from dash_options.core.health import _migration_lineage_contains
assert _migration_lineage_contains('20261029_03', '20261021_01')
assert _migration_lineage_contains('20261029_03', '20261023_01')
"""
    subprocess.run([sys.executable, "-c", probe], cwd=ROOT, check=True, timeout=30)


def test_future_revision_retains_baseline_capabilities(migration_database, tmp_path, monkeypatch):
    future = tmp_path / "migrations"
    shutil.copytree(ROOT / "migrations", future)
    (future / "versions/20261030_01_test.py").write_text(
        "from alembic import op\nrevision='20261030_01'\ndown_revision='20261029_03'\n"
        "def upgrade(): op.execute('CREATE TABLE at_lng.baseline_test_child(id integer PRIMARY KEY)')\n"
    )
    migrate(migration_database)
    with migration_database.begin() as db:
        config = migration_config(db)
        config.set_main_option("script_location", str(future))
        command.upgrade(config, "head")
    monkeypatch.setattr(health, "application_root", lambda: tmp_path)
    for required in (CHECKPOINT, "20260902_02", "20260928_01", "20261023_01"):
        assert health._migration_lineage_contains("20261030_01", required)
    assert not health._migration_lineage_contains("20261030_01", "20990101_99")


@pytest.mark.parametrize("start", (CHECKPOINT, "20261021_01"))
def test_release_rehearsal_uses_real_baseline_and_archive(blank_database, start):
    report = rehearse_schema(blank_database, start)
    assert report["heads"] == [CHECKPOINT]
    assert report["all_dashboard_migrations_included"]
    assert_retired(blank_database)


def test_tampered_archive_is_rejected_before_loading(monkeypatch):
    from scripts.maintenance import upgrade_legacy_schema as recovery

    monkeypatch.setattr(recovery, "ARCHIVE_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        with recovery.archived_migrations():
            pytest.fail("Tampered archive was opened")


def test_dependent_external_view_blocks_bootstrap_and_preserves_schema(migration_database):
    engine = migration_database
    with engine.begin() as db:
        db.execute(
            text(
                "CREATE VIEW at_lng.trades_options_valuation_current AS SELECT quantity FROM at_lng.trades_options_valuation"
            )
        )
        db.execute(
            text("CREATE VIEW at_lng.dependent_consumer AS SELECT * FROM at_lng.trades_options_valuation_current")
        )
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="dependent views"):
        migrate(engine)
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "enabled", ("jkm_publication_enabled", "gas_batch_jobs_enabled", "brent_intraday_publication_enabled")
)
def test_readiness_checks_actual_baseline_database_and_rejects_unknown_head(migration_database, monkeypatch, enabled):
    engine = migration_database
    migrate(engine)
    for name in (
        "writes_enabled",
        "ttf_intraday_writes_enabled",
        "ttf_publication_enabled",
        "jkm_writes_enabled",
        "jkm_publication_enabled",
        "nbp_writes_enabled",
        "nbp_publication_enabled",
        "brent_writes_enabled",
        "brent_publication_enabled",
        "brent_intraday_writes_enabled",
        "brent_intraday_publication_enabled",
        "hh_writes_enabled",
        "hh_publication_enabled",
        "background_jobs_enabled",
        "gas_batch_jobs_enabled",
        "intraday_refresh_enabled",
        "settlement_refresh_enabled",
    ):
        monkeypatch.setattr(health, name, lambda: False)
    monkeypatch.setattr(health, enabled, lambda: True)
    monkeypatch.setattr(health, "validate_auth_configuration", lambda: "test-isolated")
    monkeypatch.setattr(health, "get_database_engine", lambda required=False: engine)
    ready, details = health.readiness_status()
    assert ready, details
    with engine.begin() as db:
        db.execute(text("UPDATE at_lng.alembic_version SET version_num='20990101_99'"))
    ready, _ = health.readiness_status()
    assert not ready
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="unknown database revision"):
        migrate(engine)
    assert snapshot(engine) == before
