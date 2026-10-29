"""Inspect or upgrade recorded pre-baseline databases using the frozen old graph.

The verified archive is never part of normal Alembic discovery. The current
transactional environment executes its original revision functions and SQL.
"""

import argparse
from contextlib import contextmanager
import hashlib
from importlib.resources import files
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import tempfile

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from dash_options.core.config import get_database_engine
from dash_options.core.paths import application_root


CHECKPOINT = "20261029_03"
ARCHIVE_COMMIT = "b946433a2fcde53fb3d5421fada7b0104761aa83"
ARCHIVE_SHA256 = "3b26ecfa06fd80746f50bb4d1c435a383a3e8c2fc04b37e0c3fafa9a1ebb71da"


@contextmanager
def archived_migrations():
    """Extract the checksum-pinned legacy graph into an owned temporary folder."""
    data = files("dash_options").joinpath("resources/migration_history/through_20261029_03.tar.gz").read_bytes()
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("Legacy migration archive checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="dashboard-migration-history-") as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or not (member.isdir() or member.isfile()):
                    raise RuntimeError("Unsafe legacy migration archive member")
                target = root.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
        yield root / "migrations"


def migration_config(connection, *, legacy=None):
    """Use one controlled environment for normal and explicitly archived graphs."""
    config = Config()
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(application_root() / "migrations"))
    if legacy is not None:
        config.set_main_option("version_locations", str(legacy / "versions"))
    config.attributes["connection"] = connection
    return config


def upgrade_legacy(connection, *, apply=False):
    """Require a recorded checkpoint; never stamp an unknown/unversioned schema."""
    if not connection.execute(text("SELECT to_regclass('at_lng.alembic_version')")).scalar():
        raise RuntimeError("No recorded migration history; reconcile the schema before upgrading")
    before = sorted(connection.execute(text("SELECT version_num FROM at_lng.alembic_version")).scalars())
    if not before:
        raise RuntimeError("Empty migration history; reconcile the schema before upgrading")
    with archived_migrations() as legacy:
        config = migration_config(connection, legacy=legacy)
        graph = ScriptDirectory.from_config(config)
        for revision in before:
            if graph.get_revision(revision) is None:
                raise RuntimeError("Unrecognized legacy migration checkpoint")
        if apply:
            command.upgrade(config, CHECKPOINT)
        after = sorted(connection.execute(text("SELECT version_num FROM at_lng.alembic_version")).scalars())
        if apply and after != [CHECKPOINT]:
            raise RuntimeError("Archived upgrade did not reach the baseline checkpoint")
    return {
        "before": before,
        "after": after,
        "applied": apply,
        "target": CHECKPOINT,
        "archive_commit": ARCHIVE_COMMIT,
        "archive_sha256": ARCHIVE_SHA256,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Apply the verified archive; default is read-only inspection"
    )
    args = parser.parse_args()
    engine = get_database_engine()
    with engine.begin() as connection:
        if not args.apply:
            connection.execute(text("SET TRANSACTION READ ONLY"))
        else:
            connection.execute(text("SET LOCAL lock_timeout='5s'"))
            connection.execute(text("SET LOCAL statement_timeout='30s'"))
        result = upgrade_legacy(connection, apply=args.apply)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
