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

The migration graph converges at `20261029_03`, following the former
`20261021_01`, `20261029_01` and `20261029_02` heads. Inspect the graph from the
paired artifact, using its configuration:

```sh
python -m alembic -c /path/to/release/dashboard/alembic.ini heads
python -m alembic -c /path/to/release/dashboard/alembic.ini history
```

Applying `upgrade head` to a database is a separate operational action. The
convergence migration repeats the existing guarded retirement after every
history-creation branch has finished. It removes only empty obsolete tables;
populated history still requires the verified archive/retirement procedure.
Missing tables are accepted, unexpected dependencies prevent removal, and a
failure rolls back table drops and the revision update together. Downgrade
remains blocked pending verified archive restoration.

These migrations inspect database contents and need an online rehearsal against
a disposable database; an offline `--sql` export cannot establish that their
guards pass. Preserve old revisions and their SQL resources. Enable writes only
after the corresponding schema, source calendars, identity and role checks pass.

An applied revision is still required history: Alembic and readiness checks load
the full ancestry, and fresh installations execute it. The 27 revisions and
three SQL resources are all referenced; none becomes removable merely because
production reached `20261029_03`. Only generated migration caches can be removed
without replacing and validating the migration baseline. Keep optional writer
imports inside migration execution so older readers can inspect the graph.

The [29 October schema application record](../releases/2026/2026-10-29-schema-convergence.md)
documents the applied head and compatible runtime/rollback artifacts.

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
