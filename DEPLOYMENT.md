# Deployment

## Pending volatility refactor candidate

The 1.3.1 candidate completed verification. Its 1.3.2 follow-up cleanup uses the
same version-3 job/checkpoint schemas and strict database errors in required
worker paths. Its current acceptance record is
`options/docs/vol_calibration_cleanup.md`, with separate evidence under
`options/outputs/vol_calibration_cleanup_20261002/`. Neither candidate has been
deployed by this refactor task; a rollout must reconcile the current ICE release.

Analytics 1.3.1 is a separate candidate paired with the dashboard job payload/type
version 3 and numerical checkpoint schema 3. The version-3 input identity retains
exact floating-point values, row order and schema. Old jobs/checkpoints are rejected
and require resubmission; before switching a release, finish or cancel old jobs
through their original owner controls. Persisted publication identifiers are
unchanged. No production switch is part of the refactor implementation.

Use the candidate wheel digest, source/dependency manifest and rollback record from
`options/outputs/vol_calibration_refactor_20261001/` after its gates pass; the older
release digests below identify their recorded releases and must not be reused for
the new wheel. Refer to `vol_calibration/MIGRATION.md` for coordinated acceptance.

The first supported deployment is read-only calibration, comparison, and
export. Database writes, approval, publication, and background workers are
disabled unless their feature flags are explicitly enabled.

## Build

Install the locked deployment environment with
`at-options-analytics==1.2.5`, built from reviewed `options` commit
`ef9f18d` (plus the 1.2.5 packaging version), and this repository's
requirements. The release wheel SHA-256 is
`6fcf30ca3bfc4c26b8effb67995de8df7855a79075359fbc103b811f6a501563`.
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
git -C /path/to/options checkout ef9f18d
# Set project version to 1.2.5, as recorded by the calibration release manifest.
SOURCE_DATE_EPOCH=$(git -C /path/to/options show -s --format=%ct HEAD) python -m pip wheel /path/to/options --no-deps --no-build-isolation --wheel-dir dist
echo "6fcf30ca3bfc4c26b8effb67995de8df7855a79075359fbc103b811f6a501563  dist/at_options_analytics-1.2.5-py3-none-any.whl" | shasum -a 256 -c -
python -m pip install dist/at_options_analytics-1.2.5-py3-none-any.whl
python -m pip install -r requirements.txt
python -c "from importlib.metadata import version; assert version('at-options-analytics') == '1.2.5'"
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

ICE quote conventions and saved-edge interpretation belong to the shared
`options.ice_quote_interpretation` module. Dash consumes its direction, cash-flow,
trade, confidence and current-action fields; it retains table/chart formatting
and read-only source access. ICEchat owns assessment policy, valuation
orchestration, outgoing message formatting and ICE API transport. Deploy the
shared module with the dashboard artifact; an editable source change alone does
not update the running dashboard's analytics wheel.

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

In the 1.3.0 migration artifact, TTF and JKM batch calculations live
in `options.vol_calibration`. Dashboard `vol_calibration/ttf_batch.py` and
`vol_calibration/jkm_batch.py` are execution adapters; `batch_adapter.py` translates
editable tables and numerical results. Ship the same immutable analytics wheel
and dashboard artifact to web and detached workers. Version 2 job payloads and
numerical checkpoints reject incompatible retained work. Historical published
revisions remain readable. The build instructions above describe the historical
1.2.5 release; use version 1.3.0 and the recorded wheel digest for this migration.
The operational release record determines whether the production switch has
passed its gates; package installation alone is not a completed deployment.

The reproducible candidate wheel is `at_options_analytics-1.3.0-py3-none-any.whl`,
SHA-256 `70273ee92cc1527c23ea5bff3b3023f4a60202ea0c392f78de28195d6ac66f6e`.
Its paired dashboard is based on the actually deployed source-alignment v2
artifact, preserving its market-source corrections. The older active-release
pointer was stale; never use that pointer alone to select a baseline or rollback.
The staged environment retains the deployed 60-package dependency lock and
passes `pip check`. Both independent wheel builds have the same digest.
Do not deploy until the final dashboard manifest, browser exports and readiness
are verified together. HH candidate identity hashes semantic surface and
diagnostic values, so cache serialization does not invalidate identical data.

Before a migration release, run the static ownership gate from the analytics
checkout against both the source dashboard and the staged dashboard:

```bash
python scripts/check_vol_calibration_boundaries.py --dashboard /path/to/dash_options
```

Verify the paired file manifest and every installed analytics file against the
wheel before startup and again before production cutover:

```bash
python scripts/check_vol_calibration_release.py --release /path/to/release --wheel /path/to/at_options_analytics-1.3.0-py3-none-any.whl
```

This also rejects unrecorded dashboard code/assets and checks installed dependency
compatibility. It runs the release interpreter outside the source checkout. It
does not contact sources or establish browser/readback readiness.

This verifies import ownership only. Require full captured-input recalibration,
actual isolated PostgreSQL publication/job rehearsals, browser/export reconciliation,
and a coordinated web/worker rollback rehearsal on the exact release artifact.
Use the authorized operational-verification workflow when automated tests are
excluded. Preserve original floating-point inputs; lossy JSON replay is not an
equivalent optimizer input. Recheck the running process's dashboard directory
before staging: a saved release pointer alone may be stale.

Set database and Trino values through environment variables or point
`OPTIONS_CONFIG_PATH` at a mounted configuration file. Do not put credentials
in the image.

Vol Trades uses `/vol_trades`; existing `/brent_vol_history` bookmarks redirect
to this route while retaining query parameters and browser section links.
Calibration controls are mounted in Vol Trades at `/vol_trades`;
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

## 1 October calibration update

Analytics 1.2.5 preserves actual LNE rate-capture timestamps and validates their
settlement-date freshness and snapshot clock precision rather than requiring
all row timestamps to equal snapshot completion. The dashboard ships the
quote-preserving TTF v2 convex-call-core fallback and requires migration
`20260930_01`; official anchors and boundary tangents remain unchanged.

Gas candidates retain accepted first fits. Failed observed and extrapolated
hybrids resume deterministic starts through bounded budgets of three and nine;
invalid inputs fail immediately. Original errors, optimizer and blend-gate
attempts, elapsed stages and selected recovery budgets survive worker transport,
checkpoints, batch exports and persisted expiry diagnostics. The shared recovery
module is included in worker code fingerprints. A completed batch remains a
candidate; recovery never auto-publishes.

Publication loaders may request `include_surface=False` for provenance and expiry
parameters. Full published grids are cached only by source/engine and immutable
publication UUID; every request still resolves its active/as-of pointer.
Explicit Reload bypasses the grid cache. Keep full data for chart, node-adjustment
and export consumers. Operational publishers use the compact committed receipt,
one complete authoritative grid reconciliation, then a metadata-only active
revision check rather than downloading the same dense grid twice.

The local update stages calibration modules and their shared data dependencies
over a copy of the prior web artifact, preserving its existing routes and assets.
The isolated environment keeps prior dependency versions, with the analytics
wheel as the only package version change. See the release manifest for exact
file hashes, wheel hash, verification artifacts and rollback command. For this
user-authorized deployment, verification is operational only; no tests are added
or run, per the explicit workspace instruction.

The verified local live artifact is
`~/.local/share/brent-surface-release/1.2.5-calibration-20261001`, serving port 8071
through its `start.sh`. `active-calibration-release.json` in the parent directory
records the active launcher, artifact revision and prior launcher for rollback.
The prior `1.2.4-hh-context-ice-ttf-20260929` release remains available. Preserve
single-worker / eight-thread topology and independent feature gates on restart.
The operational evidence is saved under the options repository in
`outputs/calibration_release_20261001`.

## Vol Trades settlement source alignment

Operational ICAP consumers resolve the latest shared snapshot on each chart
load and pin its immutable payload for the request. Expired or unavailable
snapshots require a rebuild or an explicit refresh-required state; an initialized
worker is not evidence that its source revision remains current.

TFO settlement charts use the selected ICAP COB when available and automatically
use the nearest prior ICAP COB otherwise. A warning beside the Expiry panels
title identifies the actual ICAP date and selected market date. If no usable
ICAP COB exists on or before the selected date, or freshness cannot be verified,
the marks stay hidden with an explicit header warning. Source receipts above the expiry panels
show Bloomberg COB, actual ICAP COB, calibrated COB and publication time; they
are derived from the chart's inputs rather than the global source summary.

A 30-second page poll checks the selected operational source slice (including
same-COB corrections) and active calibration publication. It publishes a revision
signal only after the shared replacement reconciles to the source. A failed
freshness check hides the ICAP layer with an explicit refresh-required warning.
The chart generation includes the ICAP revision so obsolete overlay callbacks
cannot modify replacement charts. The local release stages only these safeguards
over the existing runtime artifact, preserving routes, assets and feature flags.

## ICE quote tape display

The quote tape uses 16 columns: quote identity; broker premium and IV;
model premium, IV and net edge; forward/reference and exact calibration
publication timestamp; option-package Greeks; sender; and reply delivery. Quantity
fields remain in persisted rows and selected-quote details.

`ice_quote_tape.py` projects saved values for display;
`ice_quote_tape_components.py` owns columns and leg details. The deployed
monolithic page adapts these same helpers until the modular page refactor is
released. SQL joins the exact saved publication ID and latest valuation version
for Greeks. It never substitutes the current active surface for an earlier quote.
A legacy surface without a publication timestamp is explicitly labelled Legacy.

Operational readback on 2 October reconciled 158 saved records with no changes
to prior fields and matched Greeks in 12 retained chat replies. No automated
tests were added or run. Evidence is under
`options/outputs/quote_tape_20261002/`. The first tape release was
`1.3.0-quote-tape-20261002`; later releases must retain these scoped files.

The table colour bands distinguish broker marks (warm neutral), model marks
(blue), inputs (slate) and Greeks (violet). Sender sits immediately before
Delivery. Edge markers keep the agreed green BUY, red SELL, white no edge
and black unpriced meanings; reply badges use neutral/blue styling. The
`1.3.0-quote-tape-style-20261002` release changes only column presentation,
scoped CSS and renderers. Prices, formatting precision and loaders are unchanged.

The 2 October diagonal update displays independently saved monthly leg prices,
IVs, forwards and signed Greeks in contract order when the package quantity
basis is unassigned. The aggregate price/risk and all package edges remain
NULL. The tape states `BASIS REQUIRED`; selected details show each month’s
delivery hours and clarify that these are separate leg values. An acknowledged
reply older than the valuation is labelled `Prior reply`. ICEchat owns the
leg valuation and fresh reply text; no historical message is resent.
