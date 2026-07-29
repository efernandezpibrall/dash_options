# Deployment

The first supported deployment is read-only calibration, comparison, and
export. Database writes, approval, publication, and background workers are
disabled unless their feature flags are explicitly enabled.

## Build

Install the locked deployment environment with
`at-options-analytics==1.2.0`, built from reviewed `options` commit
`6854666f03bfc181c6eeae155e873e0843db7c54`, and this repository's
requirements. The release wheel SHA-256 is
`58ef4a25b711220479a1793752914e201aefc814c9a8807b4fa52ae5911b21e0`.
Do not resolve an unversioned
checkout of the analytics repository at deployment time.

Build and install the analytics wheel before installing this application:

```bash
git -C /path/to/options checkout 6854666f03bfc181c6eeae155e873e0843db7c54
python -m pip wheel /path/to/options --no-deps --no-build-isolation --wheel-dir dist
echo "58ef4a25b711220479a1793752914e201aefc814c9a8807b4fa52ae5911b21e0  dist/at_options_analytics-1.2.0-py3-none-any.whl" | shasum -a 256 -c -
python -m pip install dist/at_options_analytics-1.2.0-py3-none-any.whl
python -m pip install -r requirements.txt
python -c "from importlib.metadata import version; assert version('at-options-analytics') == '1.2.0'"
```

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
