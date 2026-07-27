"""Liveness and readiness endpoints for production serving."""

from __future__ import annotations

import os

from flask import jsonify
from sqlalchemy import text

from runtime_config import get_database_engine
from vol_calibration.feature_flags import background_jobs_enabled, writes_enabled


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def readiness_status() -> tuple[bool, dict]:
    """Return fail-closed readiness details without exposing credentials."""
    details = {
        "writes_enabled": writes_enabled(),
        "background_jobs_enabled": background_jobs_enabled(),
    }
    if not details["writes_enabled"]:
        details["mode"] = "read-only"
        return True, details

    if not _enabled("OPTIONS_TRUSTED_PROXY_AUTH_ENABLED"):
        details["error"] = "trusted proxy authentication is not enabled"
        return False, details

    engine = get_database_engine(required=False)
    if engine is None:
        details["error"] = "database configuration is unavailable"
        return False, details

    required_relations = ["at_lng.vol_calibration_runs"]
    if details["background_jobs_enabled"]:
        required_relations.append("at_lng.vol_calibration_jobs")

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
