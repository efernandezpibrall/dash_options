"""Credential-safe source configuration identity for UI caches and workers."""

import hashlib
import os
from pathlib import Path


SOURCE_ENVIRONMENT_NAMES = (
    "OPTIONS_CONFIG_PATH",
    "OPTIONS_CONFIG_FILE",
    "OPTIONS_DATABASE_URL",
    "DATABASE_URL",
    "OPTIONS_DB_SCHEMA",
    "DB_SCHEMA",
    "OPTIONS_TRINOS_HOST",
    "TRINOS_HOST",
    "OPTIONS_TRINOS_PORT",
    "TRINOS_PORT",
    "OPTIONS_TRINOS_USERNAME",
    "TRINOS_USERNAME",
    "OPTIONS_TRINOS_TOKEN",
    "TRINOS_TOKEN",
    "OPTIONS_TRINOS_HTTP_SCHEME",
    "TRINOS_HTTP_SCHEME",
    "OPTIONS_TRINOS_VERIFY_SSL",
    "TRINOS_VERIFY_SSL",
    "VOL_CALIBRATION_SOURCE_CACHE_NAMESPACE",
)


def source_config_fingerprint() -> str:
    """Hash source-affecting configuration without exposing credential values."""
    digest = hashlib.sha256()
    for name in SOURCE_ENVIRONMENT_NAMES:
        value = os.getenv(name, "")
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\0")

    configured_path = os.getenv("OPTIONS_CONFIG_PATH") or os.getenv("OPTIONS_CONFIG_FILE")
    if configured_path:
        path = Path(configured_path).expanduser()
    else:
        repo_dir = Path(__file__).resolve().parent
        candidates = (
            repo_dir / "config.ini",
            repo_dir.parent / "config.ini",
            Path.cwd() / "config.ini",
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), None)

    if path is not None:
        digest.update(str(path.resolve(strict=False)).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()
