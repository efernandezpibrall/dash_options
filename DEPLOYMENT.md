# Deployment

The first supported deployment is read-only calibration, comparison, and
export. Database writes, approval, publication, and background workers are
disabled unless their feature flags are explicitly enabled.

## Build

Install the locked deployment environment with the packaged
`at-options-analytics` artifact and this repository's requirements. The
analytics distribution must be built from the same reviewed `options` commit
used by the dashboard.

Run before producing the deployment artifact:

```bash
python -m pip check
python -m pytest
ruff check .
python -c "import index_options; index_options.app._setup_server()"
```

## Configuration

Set database and Trino values through environment variables or point
`OPTIONS_CONFIG_PATH` at a mounted configuration file. Do not put credentials
in the image.

The read-only release uses:

```text
VOL_CALIBRATION_ENABLED=true
VOL_CALIBRATION_WRITES_ENABLED=false
VOL_CALIBRATION_PUBLISH_ENABLED=false
VOL_CALIBRATION_BACKGROUND_JOBS_ENABLED=false
OPTIONS_TRUSTED_PROXY_AUTH_ENABLED=false
```

Trino TLS verification defaults to enabled. An internal environment that
explicitly requires otherwise must set `TRINOS_VERIFY_SSL=false`.

## Database and write enablement

Compile and review the additive migration before running it:

```bash
alembic upgrade head --sql
alembic upgrade head
```

Do not enable writes until the migration is applied, trusted proxy identity is
verified, role mappings are tested, and the background worker is deployed.
Publication remains disabled until verified option-expiry calendars and source
eligibility rules are complete for every enabled product.

## Serving and health

The `Procfile` serves `index_options:server` through Gunicorn.

- `/health/live` verifies the web process without touching the database.
- `/health/ready` is immediately ready in read-only mode.
- When writes are enabled, readiness also requires trusted proxy auth and the
  required database relations.

Rollback by disabling write/publication/job intake first, restoring the prior
web artifact, and leaving additive audit tables intact.
