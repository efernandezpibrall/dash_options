# Deployment

The first supported deployment is read-only calibration, comparison, and
export. Database writes, approval, publication, and background workers are
disabled unless their feature flags are explicitly enabled.

## Build

Install the locked deployment environment with
`at-options-analytics==1.2.4`, built from reviewed `options` commit
`63baa7a29f9876d36153d115c462b80de13b6043`, and this repository's
requirements. The release wheel SHA-256 is
`709ee0e145726aa83bba221c13e4645de2d431b7c7066a72c507e6df6778e676`.
Do not resolve an unversioned
checkout of the analytics repository at deployment time.
Use a dedicated virtual environment for this application; other tools in the
shared repository environment have incompatible `requests` requirements.
The pinned NumPy, pandas, SciPy, and SQLAlchemy versions reproduce the reviewed
Brent settlement calibration and its 25 September publication to persisted precision.
Analytics 1.2.4 adds the HH actual-strike single-SVI seasonal policy and preserves the fitted forward when
producing an accepted Brent intraday smile. Gas fit workers require the pinned
`threadpoolctl` dependency; their code fingerprints resolve the installed
analytics package and require no sibling options checkout.

Build and install the analytics wheel before installing this application:

```bash
git -C /path/to/options checkout 63baa7a29f9876d36153d115c462b80de13b6043
SOURCE_DATE_EPOCH=$(git -C /path/to/options show -s --format=%ct HEAD) python -m pip wheel /path/to/options --no-deps --no-build-isolation --wheel-dir dist
echo "709ee0e145726aa83bba221c13e4645de2d431b7c7066a72c507e6df6778e676  dist/at_options_analytics-1.2.4-py3-none-any.whl" | shasum -a 256 -c -
python -m pip install dist/at_options_analytics-1.2.4-py3-none-any.whl
python -m pip install -r requirements.txt
python -c "from importlib.metadata import version; assert version('at-options-analytics') == '1.2.4'"
```

Run before producing the deployment artifact:

```bash
python -m pip check
python -m pytest
ruff check .
python -c "import wsgi"
```

Serve this Dash build with one process and multiple threads so every request
uses the same registered callback map while long chart loads remain responsive.
The WSGI entrypoint completes callback registration before requests can arrive,
including polling requests from browser tabs left open during a restart:

```bash
gunicorn -w 1 -k gthread --threads 8 -b 127.0.0.1:8071 --timeout 180 wsgi:server
```

## Configuration

Shared read-only data access lives outside the Dash pages:

- `surface_data.py` owns operational surface loading, COB resolution and shared snapshots.
- `vol_trades_data.py` owns pinned market snapshots, product mappings and market preparation.
- `ice_quote_data.py` owns quote and reply-audit reads and persisted quote normalization.
- `market_data.py` owns forward history and recent underlying prices, with separate selection rules and caches.
- `source_identity.py` owns the same source-configuration fingerprint for web caches and workers.

The page registry in `index_options.py` defines route titles, navigation groups
and validation layouts together. `/pricer_old` is removed without a redirect.
Pricer composition lives in `pages/pricer.py`; `pricer_workspace/` separates
state and pricing from controls, grids, charts and the four callback groups.
Vol Trades and ICE Quotes retain their callbacks in the page modules and use
`vol_trades_workspace/` for presentation and chart projections. These helper
modules do not register callbacks.

`workspace_cache.py` owns the shared LRU/TTL cache without importing Dash;
`vol_calibration/data_cache.py` adapts it to calibration callback reloads.
Calibration input selection and result helpers are shared by web and worker
callers while their distinct publication-ID and date-error policies remain
explicit in their respective helpers.

TTF and JKM batch calculations live in `vol_calibration/ttf_batch.py` and
`vol_calibration/jkm_batch.py`; pages and detached workers call those same
implementations. Ship these modules with the web and worker code. Calculation
fingerprints include the service files, so earlier-code checkpoints remain
incompatible with a changed calculation build rather than being silently reused.

Set database and Trino values through environment variables or point
`OPTIONS_CONFIG_PATH` at a mounted configuration file. Do not put credentials
in the image.

Calibration controls are mounted in Vol Trades at `/brent_vol_history`;
`VOL_CALIBRATION_ENABLED` remains the shared feature gate. The retired
`/vol_calibration` URL redirects to Vol Trades. The Vol Trades calibration
button is visible by default; set `VOL_TRADES_INLINE_CALIBRATION_ENABLED=false`
to hide it explicitly. Publication still uses separate write flags.

Brent calibration fits observed strikes from a pinned settlement or intraday
snapshot with one SVI model. The 11 saved deltas are output samples for sharing,
not calibration targets. The Brent residual/PCHIP callback is removed from the
app path. Apply Alembic revision `20260929_01` to permit the exact single-SVI
policy in the governed dense-surface table. After verifying the source, expiry
calendar, and publisher identity,
enable `VOL_CALIBRATION_BRENT_WRITES_ENABLED` and
`VOL_CALIBRATION_BRENT_PUBLICATION_ENABLED`; other product gates remain separate.

HH LNE now uses one actual-strike SVI smile per delivery month. Sparse months
are constrained by available LNE quotes and same-season predecessors. The
401-point governed surface and operational 11-delta grid sample the same model.
HH is calibrated exclusively from LNE. The LNE and ON selections are market
context views of the same exact-COB HH publication; ON observations never enter
calibration. Vol Trades displays the dense HH curve only, labels legacy policy
revisions, and reports a missing publication instead of substituting another
date. The operational 11-delta grid remains available to existing consumers.
Successful Brent and HH publications refresh the matching charts through the
page-level publication revision signal after persisted readback succeeds.
Apply Alembic revisions `20260929_02` and `20260929_03` before enabling
`VOL_CALIBRATION_HH_WRITES_ENABLED` and
`VOL_CALIBRATION_HH_PUBLICATION_ENABLED`. Pin the exact LNE settlement snapshot,
verify the quote-fit and density diagnostics, then read back the immutable HH
publication by ID and inspect the selected HH surface in Vol Trades.

The read-only release uses:

```text
VOL_CALIBRATION_ENABLED=true
VOL_CALIBRATION_WRITES_ENABLED=false
VOL_CALIBRATION_PUBLISH_ENABLED=false
VOL_CALIBRATION_BACKGROUND_JOBS_ENABLED=false
VOL_CALIBRATION_GAS_BATCH_JOBS_ENABLED=false
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
GAS_BATCH_JOBS_ENABLED = false
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

The TTF and JKM settlement batch worker is controlled separately by
`VOL_CALIBRATION_GAS_BATCH_JOBS_ENABLED=true` (or
`[VOL_CALIBRATION] GAS_BATCH_JOBS_ENABLED = true`). Apply Alembic revision
`20260928_01` before enabling it. The Dash process launches a detached Python
worker for each queued batch; the worker needs the same environment, database
access and current `options`/`dash_options` code as the web process. The job
stores one verified checkpoint per expiry, resumes after an interrupted lease,
and rereads the official source before and after fitting. A finished job remains
a candidate in the page; publication still requires its existing explicit
action and product write flags. `/health/ready` checks the migration and queue
tables when this flag is enabled.

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
