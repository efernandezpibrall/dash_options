"""Application resources and external configuration locations.

An installed wheel uses OPTIONS_APP_ROOT for the deployment's Alembic project.
Source checkouts discover that root without depending on the current directory.
"""

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def application_root() -> Path:
    explicit = os.getenv("OPTIONS_APP_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    for parent in PACKAGE_ROOT.parents:
        if (parent / "alembic.ini").is_file() and (parent / "migrations").is_dir():
            return parent
    return Path.cwd()


def config_candidates() -> list[Path]:
    explicit = os.getenv("OPTIONS_CONFIG_PATH") or os.getenv("OPTIONS_CONFIG_FILE")
    root = application_root()
    return ([Path(explicit).expanduser()] if explicit else []) + [
        root / "config.ini",
        root.parent / "config.ini",
        Path.cwd() / "config.ini",
    ]
