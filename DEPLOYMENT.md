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
BBG_OPTION_CHAIN_INTRADAY_REFRESH_ENABLED=false
BBG_OPTION_CHAIN_SETTLEMENT_REFRESH_ENABLED=false
```

For a workstation process bound directly to loopback, the same settings may be
provided in the external `config.ini` without putting credentials in the
repository:

```ini
[VOL_CALIBRATION]
WRITES_ENABLED = false
PUBLISH_ENABLED = false
BACKGROUND_JOBS_ENABLED = false
TTF_INTRADAY_WRITES_ENABLED = true
TTF_PUBLICATION_ENABLED = true

[OPTIONS_AUTH]
MODE = local_loopback
LOCAL_USER = workstation-user
LOCAL_ROLES = calibrator,publisher

[BLOOMBERG_OPTIONS]
INTRADAY_REFRESH_ENABLED = false
SETTLEMENT_REFRESH_ENABLED = false
```

`local_loopback` rejects non-loopback and forwarded requests. It must not be
used behind a reverse proxy. Shared deployments must use `trusted_proxy` with a
server-held shared secret and proxy-injected user and role headers. The explicit
`publisher` role permits an accountable trader to publish their own validated
surface; `approver` retains maker-checker self-publication protection.

Trino TLS verification defaults to enabled. An internal environment that
explicitly requires otherwise must set `TRINOS_VERIFY_SSL=false`.

## Database and write enablement

Compile and review the additive migration before running it:

```bash
alembic upgrade head --sql
alembic upgrade head
```

Do not enable writes until the migration is applied, the selected server-side
identity mode is verified, and role mappings are tested. Background jobs remain
independently disabled until their worker is deployed.
Publication remains disabled until verified option-expiry calendars and source
eligibility rules are complete for every enabled product.

The Brent market-data refreshes have separate fail-closed intake flags. Apply
BBG migrations `002` through `009` in numeric order; the worker-registry
contract is consolidated into `migrations/003_bbg_option_chain_intraday.sql`
and settlement refresh into `migrations/008_bbg_option_settlement_refresh.sql`.
Register the worker in the logged-in Bloomberg user session and verify
`/health/ready` before enabling either
`BBG_OPTION_CHAIN_INTRADAY_REFRESH_ENABLED` or
`BBG_OPTION_CHAIN_SETTLEMENT_REFRESH_ENABLED` (or their `config.ini`
equivalents). The web process only queues jobs; Bloomberg calls, persistence,
and IV pricing run in the warmed Python worker. The portable service manager
uses a macOS user LaunchAgent or a Windows Task Scheduler task to start that
same command at login and restart it after failure:

```bash
python option_chain_worker_service.py register --config /path/to/config.ini
python option_chain_worker_service.py status
```

Use `start`, `stop`, or `uninstall` in place of `status` for lifecycle control.
For foreground diagnosis, run `python option_chain_refresh_worker.py
--poll-seconds 1 --config /path/to/config.ini`. Worker readiness is independent
from web readiness and requires a fresh row in
`at_lng.bbg_option_chain_workers`; the Vol Trades page fails closed when no
eligible Brent/TFO worker has heartbeated within 30 seconds.

Rollback by disabling both flags before stopping the worker. Existing snapshots
and the additive audit tables remain immutable.

## Serving and health

The `Procfile` serves `index_options:server` through Gunicorn.

- `/health/live` verifies the web process without touching the database.
- `/health/ready` is immediately ready in read-only mode.
- When writes or Bloomberg refresh intake are enabled, readiness also requires
  configured authentication and the required database relations.

Rollback by disabling write/publication/job intake first, restoring the prior
web artifact, and leaving additive audit tables intact.
