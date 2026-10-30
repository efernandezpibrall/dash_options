# Installation and operation

This is the current package layout. [The pre-reorganization deployment record](../releases/2026/2026-10-29-pre-reorganization.md) preserves historical policy/release evidence; its flat-module commands are historical.

## Build and install

Use an immutable application source artifact containing `alembic.ini`, `migrations/`, `scripts/`, `tests/fixtures/`, and the paired configuration references. Build the dashboard as a complete wheel; do not overlay the former root Python files onto this package.

```sh
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir /path/to/staging/wheels
```

Install the reviewed analytics wheel, ICEchat adapter, locked dependencies and dashboard wheel into the same dedicated environment used by web and workers. Record the exact source/wheel hashes. Set these external paths for the deployment:

```sh
export OPTIONS_APP_ROOT=/path/to/release/dashboard
export OPTIONS_CONFIG_PATH=/path/to/release/config.ini
python -m pip install --no-deps /path/to/at_options_dashboard-0.1.0-py3-none-any.whl
python -m pip check
```

Use the full paired source tree for operational tooling and migration definitions. The three historical scoped-overlay builders in `options/tools/releases/` expect flat-layout dashboards; they are not the packaging route for this layout. Existing immutable deployments remain valid rollback artifacts.

## Serve

```sh
python -m dash_options.prewarm
python -m gunicorn -w 1 -k gthread --threads 8 -b 127.0.0.1:8071 --timeout 180 dash_options.wsgi:server
```

Prewarming can read configured sources. WSGI initializes the callback registry before threaded requests. `/health/live` checks the process. `/health/ready` also checks configured authentication, migrations and required relations when the relevant writes/jobs/refresh gates require them. Readiness is not proof that a selected market has fresh data.

The route registry is `src/dash_options/routes.py`. `/vol_trades` is the current route; retained `/brent_vol_history`, `/vol_calibration` and ICE bookmarks keep their existing redirects. `/pricer_old` remains retired.

## Configuration and authority

`core/config.py` resolves environment overrides and external config. `core/source_identity.py` hashes source-affecting configuration for cache and worker isolation. Never place credentials in this repository or a wheel.

Product write/publication gates, automatic settlement gates, gas batch jobs, and Bloomberg refresh intake remain independent. This reorganization does not enable any flag. Use the existing verified release configuration when building a replacement.

For direct loopback workstation use, `local_loopback` requires a named user and explicit roles and rejects forwarded requests. Shared/proxied deployments use the configured trusted proxy secret and injected identity/role headers. The server authorizes every write; browser-supplied identity is not authority. A publisher can publish their own validated candidate; approver maker/checker restrictions remain enforced. Trino TLS verification remains enabled by default.

## Schema and verification

Aspect portfolio snapshots add revisions `20261030_01`, `20261030_02` and `20261030_03` after the frozen baseline;
see [Aspect Greeks](aspect-greeks.md) for the read path, import and rollback rules.

The active graph has a single frozen root, `20261029_03`. Existing databases
at that checkpoint upgrade through additive successors without restamping. Fresh dashboard
installations execute the final schema and seed data once, after provisioning
the external shared trade/catalogue schema.

```sh
python -m alembic -c /path/to/release/dashboard/alembic.ini heads
python -m alembic -c /path/to/release/dashboard/alembic.ini upgrade head
```

Earlier databases must first use the checksum-verified archived upgrade path:

```sh
python -m scripts.maintenance.upgrade_legacy_schema
python -m scripts.maintenance.upgrade_legacy_schema --apply
python -m alembic upgrade head
```

The first command only inspects recorded state. Unknown/unversioned schemas
must be reconciled, never blindly stamped. Readiness recognizes the baseline's
explicit historical capabilities. Keep the archive and immutable rollback
releases; only the active migration graph has been consolidated.

[Baseline installation and recovery](migration-baseline.md) documents exact
prerequisites, archive provenance, transaction guards and verification. The
[implementation verification record](../releases/2026/2026-10-29-single-baseline.md)
records fresh-install, archived-upgrade and live read-only checks. The
[29 October schema application record](../releases/2026/2026-10-29-schema-convergence.md)
records the original live checkpoint application before consolidation.

From the source checkout, run `python -m pytest`. Persistence tests use owned temporary PostgreSQL clusters; install `initdb` and `pg_ctl` for that coverage. Run the analytics boundary verifier against source and staged application paths. The paired release verifier recognizes both the old `integration/` and new `scripts/verification/` resource layouts.

Before a production cutover, verify the exact installed artifact, numerical/readback parity, selected-product browser interactions and exports, worker ownership and rollback. Tests of this reorganization do not constitute production activation.

## Workers and rollback

```sh
python -m dash_options.calibration.execution.daily_runner --help
python -m dash_options.calibration.execution.settlement_worker --help
python -m scripts.verification.rehearse_daily_release --help
```

`gas_batch_runner` is dispatched by the application and retains separate job/lease ownership. Complete or cancel pending work under its original release before switching artifacts: module moves intentionally change execution fingerprints, so old in-flight jobs must not resume under a different implementation.

Restore web and workers together with their recorded analytics dependency. Keep additive schema, published revisions and immutable history. A code rollback never reactivates a superseded publication. The installed-reader probe supports both packaged and legacy rollback layouts and checks that it imported the selected artifact.

[Daily settlement operations](daily-settlements.md) documents explicit preparation/publication commands and their recovery rules.

## Portable configuration and Linux/SFTP preflight

The dashboard requires Python **3.11 or newer** (including the pinned NumPy/SciPy
requirements). The analytics package can still be used independently on its own
supported Python versions. Use the same Linux virtual environment for web and
workers. Upload the complete paired release into a new directory through SFTP;
keep credentials and each installation's database external to that bundle.

Web, analytics, Aspect workers and the Brent/HH/gas command-line tools share
`options.market_data.runtime_config`. The configuration contract is:

1. An explicit CLI `--config` selects that file; otherwise `OPTIONS_CONFIG_PATH`
   precedes `OPTIONS_CONFIG_FILE`.
2. An explicitly selected missing file fails; it never silently selects another
   machine's config. Without an explicit file, shared discovery checks the app
   root/parent, repository parent and working directory. Production should always
   specify `OPTIONS_CONFIG_PATH` and `OPTIONS_APP_ROOT`.
3. Environment values override the selected file. Both names in an alias pair
   may be present only if their nonempty values agree: `OPTIONS_DATABASE_URL` /
   `DATABASE_URL`, `OPTIONS_DB_SCHEMA` / `DB_SCHEMA`, and `OPTIONS_TRINOS_*` /
   `TRINOS_*`. Conflicts report variable names without their values.
4. An explicitly empty `ASPECT_POSITION_REPORT_BOOK` clears a file-based book
   filter. Configuration changes require restarting web and workers together.

Run the new preflight **on Linux using the runtime interpreter**, after installing
both packages, before starting the service:

```sh
export OPTIONS_CONFIG_PATH=/path/to/external/config.ini
export OPTIONS_APP_ROOT=/path/to/release/dashboard
python -m dash_options.core.release_diagnostics
python -m dash_options.core.release_diagnostics --database
# Optional exact-COB source probe; does not ingest, value or publish:
python -m dash_options.core.release_diagnostics --curve-cob 2026-10-29
```

The default command performs no network calls. It reports Python/dependency
compatibility, imported module paths and hashes, analytics implementation identity,
and configuration consistency. Add `--expected-dashboard-root` and
`--expected-analytics-root` pointing to the intended installed package directories
(or editable source roots) to reject imports from stale/shadowing installations.
A package version alone does not establish source identity: compare the release
manifest and reported hashes as well. The paired release verifier remains the
complete wheel/source identity check.

`--database` uses a read-only transaction to inspect required relations and the
recorded migration heads. Each separate database must pass independently. Older
recorded schemas are labelled for normal or archived upgrades; the diagnostic never
runs or stamps migrations. `at_lng.curve` is an optional PostgreSQL fallback, so its
absence alone is a warning. `--curve-cob` additionally checks all three gas curves
for the exact date and reports their selected source and content identity. Database
and source exceptions report their types without printing connection strings,
tokens, passwords or raw driver messages. A nonzero exit status means the selected
checks did not pass. Offline success does not establish database/source readiness.

Daily gas curve reads prefer `transformed.enverus.curve` in Trino, with the
configured PostgreSQL `curve` table as fallback for unavailable/empty exact-date
Trino results. Nonempty invalid results fail validation. Once prepared, a job pins
its source and complete curve evidence: publication re-reads that same source and
rejects changed inputs or source unavailability instead of switching providers.
PostgreSQL curve locks are taken only for PostgreSQL-sourced candidates. The gas
surface/calendar tables remain PostgreSQL-backed. Trino revalidation is a fresh
read, not a distributed lock; source changes after that read are not prevented by
the PostgreSQL publication transaction. Exact COB, pricing, quote-error, financing,
expiry and arbitrage policies are unchanged.
