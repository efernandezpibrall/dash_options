"""Transactional Alembic environment for the dashboard baseline and successors."""

from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import engine_from_config, pool, text

from dash_options.core.config import config_value


alembic_config = context.config
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)


def run_migrations(connection):
    if not connection.execute(text("SELECT to_regnamespace('at_lng')")).scalar():
        raise RuntimeError("Provision the external at_lng trade/catalogue schema before installing the dashboard")
    if connection.execute(text("SELECT to_regclass('at_lng.alembic_version')")).scalar():
        heads = connection.execute(text("SELECT version_num FROM at_lng.alembic_version ORDER BY 1")).scalars()
        scripts = ScriptDirectory.from_config(alembic_config)
        for head in heads:
            try:
                if scripts.get_revision(head) is None:
                    raise CommandError("The stored revision is not a migration")
            except CommandError as exc:
                raise RuntimeError(
                    "Pre-baseline or unknown database revision. Use "
                    "python -m scripts.maintenance.upgrade_legacy_schema for the archived "
                    "b946433 upgrade path first; do not stamp over existing history."
                ) from exc
    context.configure(
        connection=connection,
        target_metadata=None,
        include_schemas=True,
        version_table_schema="at_lng",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connection = alembic_config.attributes.get("connection")
    if connection is not None:
        # The caller owns this transaction, including its read-only policy.
        run_migrations(connection)
        return
    url = config_value("DATABASE", "CONNECTION_STRING")
    if not url:
        raise RuntimeError("Alembic requires DATABASE_URL or OPTIONS_CONFIG_PATH")
    section = alembic_config.get_section(alembic_config.config_ini_section) or {}
    section["sqlalchemy.url"] = url
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        # Preflight SELECTs and every pending revision share one transaction.
        with engine.begin() as connection:
            run_migrations(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Baseline preconditions require an online disposable-database rehearsal")
else:
    run_migrations_online()
