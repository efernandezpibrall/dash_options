"""Liveness and readiness endpoints for production serving."""

from __future__ import annotations

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
    ttf_intraday_writes_enabled,
    ttf_publication_enabled,
    writes_enabled,
)
def readiness_status() -> tuple[bool, dict]:
    """Return fail-closed readiness details without exposing credentials."""
    details = {
        "writes_enabled": writes_enabled(),
        "ttf_intraday_writes_enabled": ttf_intraday_writes_enabled(),
        "ttf_publication_enabled": ttf_publication_enabled(),
        "background_jobs_enabled": background_jobs_enabled(),
        "bbg_option_chain_intraday_refresh_enabled": intraday_refresh_enabled(),
        "bbg_option_chain_settlement_refresh_enabled": settlement_refresh_enabled(),
        "bbg_option_chain_enabled_products": sorted(enabled_products()),
    }
    if not (
        details["writes_enabled"]
        or details["ttf_intraday_writes_enabled"]
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
    if details["writes_enabled"] or details["ttf_intraday_writes_enabled"]:
        required_relations.append("at_lng.vol_calibration_runs")
    if details["ttf_intraday_writes_enabled"]:
        required_relations.extend(
            [
                "at_lng.vol_surface_publications",
                "at_lng.implied_volatility_surface_calibrated",
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
