# Single migration baseline and archived upgrades

The active graph contains one frozen baseline, `20261029_03`, with
`down_revision = None`. Future revisions descend from it. A database already
recorded at `20261029_03` performs no DDL or data writes when upgrading to this
baseline. Do not restamp it. Existing immutable releases and rollback artifacts
continue to recognize the same revision ID through their original graph.

The baseline creates the final dashboard schema directly, including constraints,
indexes, views, functions, immutable-history triggers and required seed data.
It never creates retired Brent history tables. Contract conventions, the SOFR
curve definition and the initial unapproved Brent/HH settlement-promotion rows
are retained. Table definitions are frozen in the migration, not imported from
changing application metadata.

## Fresh dashboard installations

Provision the shared `at_lng` trade/catalogue schema first. This repository is
not an installer for the entire trading platform. The baseline expects the
same pre-dashboard shared-table contract as the historical migrations:

- `trades_options` with its supported-convention constraint;
- `trades_options_valuation` with quantity, model, leg/component/expiry fields
  and its adjustment-components constraint;
- `option_contract_conventions` with the original convention metadata columns;
- `option_discount_curve_definitions` with curve/currency/day-count/source metadata.

The baseline performs the historical shared-table adaptations, including
fractional quantities, option-family columns and governance constraints. It
preserves the valuation view's owner, grants and comment. It refuses existing
unversioned dashboard relations, including retired history tables, rather than
assuming that a partially provisioned schema is safe to stamp. A database whose
shared tables already contain some dashboard adaptations needs reconciliation;
it is not a fresh installation.

Use the project environment with its reviewed dependencies and external config:

```sh
export OPTIONS_CONFIG_PATH=/path/to/config.ini
python -m alembic -c /path/to/dashboard/alembic.ini upgrade head
```

Missing prerequisites, unknown stored revisions, conflicting seed definitions
and unsafe existing objects fail without committing partial schema/data changes.
Downgrade is intentionally blocked to preserve audit and publication history.
Offline SQL output cannot establish these preconditions; rehearse online in a
disposable PostgreSQL database.

## Databases and backups older than the baseline

The original 27 revisions, three SQL resources, environment and configuration
are preserved in the packaged recovery archive:

`src/dash_options/resources/migration_history/through_20261029_03.tar.gz`

Source commit: `b946433a2fcde53fb3d5421fada7b0104761aa83`.

SHA-256: `3b26ecfa06fd80746f50bb4d1c435a383a3e8c2fc04b37e0c3fafa9a1ebb71da`.

The archive is outside normal Alembic discovery. The recovery command verifies
its checksum before extraction, then executes its original revision functions
and SQL through the current transactional environment. The archived environment
and configuration are retained for provenance, but are not substituted for the
current connection/configuration controls. The project environment must include
the reviewed analytics dependency, including `options.live_forward_curves`,
which the archived curve migration needs when actually applying that revision.

Inspect the recorded revisions first; this default is read-only:

```sh
python -m scripts.maintenance.upgrade_legacy_schema
```

Apply the archived path, then use the normal baseline and future revisions:

```sh
python -m scripts.maintenance.upgrade_legacy_schema --apply
python -m alembic upgrade head
```

The recovery command refuses an unversioned database and unknown revisions.
Never use `alembic stamp` to bypass unresolved schema differences. Populated
retired history still requires the verified archival/retirement procedure;
unexpected dependencies block drops. Exceptions roll back the entire upgrade.
Keep immutable deployed/rollback releases and this archive when restoring older
backups. No database backup or credentials are included in the archive.

## Readiness, verification and release artifacts

The baseline declares the revision capabilities it supersedes. Readiness walks
the active graph and accepts those explicitly recorded capabilities; future
children inherit them. Unknown revisions and missing required relations still
fail readiness. Migration consolidation does not enable any write/publication
flag or activate a newer application release.

`tests/migrations/test_migration_upgrade_paths.py` independently replays every
archived revision and compares the resulting PostgreSQL catalog and all seed
rows with a fresh baseline installation. It also verifies current-database
no-op behavior, data/object identity, archived upgrade paths, transactional
failure cases, permissions, retirement, future revisions and downgrade refusal.
Catalog comparison normalizes only equivalent literal-array casts introduced
by PostgreSQL's dump/reparse process. Convention review timestamps are
installation-specific; all other seeded values must match.

The persistence suite now bootstraps its database through actual Alembic,
including all shared-table adaptations. `rehearse_daily_release` accepts the
current checkpoint or an archived starting revision and uses actual upgrades,
without exclusions or artificial revision stamps. The installed-reader probe
checks historical capabilities against either supported graph.

Pair source migrations and readiness code in a release. Ship the package's
migration-history resource and the full source artifact needed by operational
commands. This baseline does not require changing the current database or
mutating an already-deployed immutable release.
