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
    from options.market_data.runtime_config import config_candidates as shared_candidates
    return shared_candidates()
