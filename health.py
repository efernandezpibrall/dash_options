"""Liveness and readiness endpoints for production serving."""

from __future__ import annotations

from pathlib import Path

from flask import jsonify
from sqlalchemy import text

from runtime_config import get_database_engine
from brent_option_chain_refresh import (
    enabled_products,
    intraday_refresh_enabled,
    settlement_refresh_enabled,
)
from vol_calibration.auth import AuthenticationError, validate_auth_configuration
from vol_calibration.feature_flags import (
    background_jobs_enabled,
    brent_publication_enabled,
    brent_writes_enabled,
    hh_publication_enabled,
    hh_writes_enabled,
    inline_calibration_enabled,
    jkm_publication_enabled,
    jkm_writes_enabled,
    nbp_publication_enabled,
    nbp_writes_enabled,
    ttf_intraday_writes_enabled,
    ttf_publication_enabled,
    writes_enabled,
)


INLINE_PUBLICATION_MIGRATION_HEAD = "20260902_02"


def _migration_lineage_contains(current_revision: str | None, required_revision: str) -> bool:
    """Return whether the current Alembic revision descends from the requirement."""
    if not current_revision:
        return False
    if current_revision == required_revision:
        return True
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parent
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        revisions = ScriptDirectory.from_config(config).iterate_revisions(
            current_revision,
            "base",
        )
        return any(revision.revision == required_revision for revision in revisions)
    except Exception:
        # Readiness is deliberately fail-closed when migration lineage is unknown.
        return False


def readiness_status() -> tuple[bool, dict]:
    """Return fail-closed readiness details without exposing credentials."""
    details = {
        "writes_enabled": writes_enabled(),
        "ttf_intraday_writes_enabled": ttf_intraday_writes_enabled(),
        "ttf_publication_enabled": ttf_publication_enabled(),
        "jkm_writes_enabled": jkm_writes_enabled(),
        "jkm_publication_enabled": jkm_publication_enabled(),
        "nbp_writes_enabled": nbp_writes_enabled(),
        "nbp_publication_enabled": nbp_publication_enabled(),
        "brent_writes_enabled": brent_writes_enabled(),
        "brent_publication_enabled": brent_publication_enabled(),
        "hh_writes_enabled": hh_writes_enabled(),
        "hh_publication_enabled": hh_publication_enabled(),
        "vol_trades_inline_calibration_enabled": inline_calibration_enabled(),
        "background_jobs_enabled": background_jobs_enabled(),
        "bbg_option_chain_intraday_refresh_enabled": intraday_refresh_enabled(),
        "bbg_option_chain_settlement_refresh_enabled": settlement_refresh_enabled(),
        "bbg_option_chain_enabled_products": sorted(enabled_products()),
    }
    mutation_enabled = any(
        details[name]
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
            "hh_writes_enabled",
            "hh_publication_enabled",
        )
    )
    if not (
        mutation_enabled
        or details["bbg_option_chain_intraday_refresh_enabled"]
        or details["bbg_option_chain_settlement_refresh_enabled"]
    ):
        details["mode"] = "read-only"
        return True, details

    try:
        details["auth_mode"] = validate_auth_configuration()
    except AuthenticationError as exc:
        details["error"] = str(exc)
        return False, details

    engine = get_database_engine(required=False)
    if engine is None:
        details["error"] = "database configuration is unavailable"
        return False, details

    required_relations = []
    publication_enabled = any(
        details[name]
        for name in (
            "ttf_publication_enabled",
            "jkm_publication_enabled",
            "nbp_publication_enabled",
            "brent_publication_enabled",
            "hh_publication_enabled",
        )
    )
    if mutation_enabled:
        required_relations.extend(
            [
                "at_lng.vol_calibration_runs",
                "at_lng.vol_calibration_expiry_results",
                "at_lng.vol_calibration_audit_events",
            ]
        )
    if publication_enabled:
        required_relations.extend(
            [
                "at_lng.vol_surface_publications",
                "at_lng.implied_volatility_surface_calibrated",
            ]
        )
    if details["ttf_intraday_writes_enabled"]:
        required_relations.extend(
            [
                "at_lng.vol_calibration_intraday_trades",
                "at_lng.vol_calibration_run_trade_inputs",
            ]
        )
    if details["background_jobs_enabled"]:
        required_relations.append("at_lng.vol_calibration_jobs")
    if (
        details["bbg_option_chain_intraday_refresh_enabled"]
        or details["bbg_option_chain_settlement_refresh_enabled"]
    ):
        required_relations.extend(
            [
                "at_lng.vol_market_snapshots",
                "at_lng.bbg_option_chain",
                "at_lng.bbg_option_chain_refresh_jobs",
            ]
        )

    try:
        with engine.connect() as connection:
            missing = [
                relation
                for relation in required_relations
                if connection.execute(
                    text("SELECT to_regclass(:relation)"),
                    {"relation": relation},
                ).scalar_one_or_none()
                is None
            ]
    except Exception:
        details["error"] = "database readiness check failed"
        return False, details

    if missing:
        details["error"] = "required calibration schema is unavailable"
        details["missing_relations"] = missing
        return False, details

    if publication_enabled:
        try:
            with engine.connect() as connection:
                migration_head = connection.execute(
                    text("SELECT version_num FROM at_lng.alembic_version")
                ).scalar_one_or_none()
            details["migration_head"] = migration_head
        except Exception:
            details["error"] = "migration head readiness check failed"
            return False, details
        if not _migration_lineage_contains(
            migration_head,
            INLINE_PUBLICATION_MIGRATION_HEAD,
        ):
            details["error"] = "required calibration migration head is unavailable"
            details["required_migration_head"] = INLINE_PUBLICATION_MIGRATION_HEAD
            return False, details

    if details["hh_publication_enabled"]:
        try:
            with engine.connect() as connection:
                lne_snapshot_available = bool(
                    connection.execute(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM at_lng.vol_market_snapshots
                                WHERE commodity = 'LNE'
                                  AND status = 'complete'
                                  AND COALESCE(metadata ->> 'snapshot_kind', 'SETTLEMENT')
                                      = 'SETTLEMENT'
                            )
                            """
                        )
                    ).scalar_one()
                )
            details["lne_settlement_available"] = lne_snapshot_available
        except Exception:
            details["error"] = "LNE settlement readiness check failed"
            return False, details
        if not lne_snapshot_available:
            details["error"] = "no complete LNE settlement snapshot is available"
            return False, details

    details["mode"] = "write-enabled"
    return True, details


def register_health_routes(server) -> None:
    """Register routes once on the shared Flask server."""
    if getattr(server, "_options_health_routes_registered", False):
        return

    @server.get("/health/live")
    def health_live():
        return jsonify({"status": "live"}), 200

    @server.get("/health/ready")
    def health_ready():
        ready, details = readiness_status()
        return jsonify({"status": "ready" if ready else "not-ready", **details}), (
            200 if ready else 503
        )

    server._options_health_routes_registered = True
