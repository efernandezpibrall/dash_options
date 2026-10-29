> Historical record captured before the 29 October 2026 package reorganization. Paths and runtime claims below describe that record. Use [current operations](../../operations/deployment.md) for current commands.

# Deployment

## Realized versus implied volatility — 29 October 2026

The five-product monthly comparison is deployed locally on port 8071 in
`1.4.4rc4-realized-comparison-20261029-r2`. It overlays only `pages/vol_surface.py`,
`realized_comparison_data.py` and the installed `options.realized_comparison`
module on the shared-SOFR release. Existing publications, permissions and
collectors are preserved. Rollback is the preceding
`1.4.4rc4-shared-sofr-20261028-r2/start.sh`.

Exact-month prices come from the existing `bbg_settlements`, `curve` and
settlement snapshot/forward tables. IV retains the page's operational surface
authority. No SQL migration or product-specific RV table is introduced.
JKM reads only `ICE_JKM_MO`; its historical backfill was deferred by the user.
After that table is updated, Refresh Options Data captures the new prices.
RV requires complete 30-calendar-day windows of daily log returns and is
annualized by sqrt(252). TTF uses underlying Endex settlement sessions, including
31 August, separately from option expiry business-day calculations.

`BBG/exact_futures_history.py` provides a dry-run-by-default shared-store refresh
for Brent, HH, TTF and NBP; it validates metadata and inserts only missing prices
with explicit `--write`. It creates no schedule. Keep ingestion outside callbacks.
The two `brent_realized_history_*` tables are no longer read by this chart.
The three `brent_delta_history_*` tables still support the separate historical
delta chart and must not be dropped without replacing that reader. All five
tables were archived; all archived settlement prices match the shared store.

Detailed methodology, per-contract/date coverage, insertion receipts and table
retirement evidence are in
`options/outputs/realized_comparison_review_20261029/`. The installed release
passed 45 focused tests and five-product browser verification. Its broader
suite passed 1,019 tests with three failures reproduced in the unchanged baseline.

## Live Prices

The 22 px row-height release was deployed locally on port 8071 on 23 October
2026 at `~/.local/share/brent-surface-release/1.4.4rc4-live-prices-22px-20261023/`.
It changes only `pages/live_prices.py` and `assets/live_prices.css`: rows are
22 px, cell line height is 20 px, and the font remains 12 px. Production browser
verification covered all 18 grids with no clipped glyphs. The installed analytics
wheel, collector, existing snapshot and market-data publications were preserved.
`evidence/completion.json` records production proof. Rollback is
`1.4.4rc4-live-prices-all-products-20261023-r1/start.sh`.

The refresh-performance candidate uses the paired `BBG/live_prices_worker.py`
with the existing collector and the updated page/data module. It retains the
current marking implementation, schema 2 and namespace `live-prices-v5`.
The builder pins the worker alongside the existing request/session helpers.
The gateway starts a source-scoped owner-only Unix-socket worker on demand;
it keeps its Bloomberg session and validated reference/daily inputs between
manual captures and exits after 30 minutes idle. No LaunchAgent, subscription,
database migration or feature-flag change is required. Initial preparation,
recovery and contract rolls remain slower than warm refreshes. Pricing failures
retain the complete prior snapshot and force fresh preparation on retry.
See `options/docs/live_prices_refresh.md` for cache invalidation and measured
before/after evidence. The reviewed `1.4.4rc4-live-prices-fast-refresh-20261023-r6`
artifact was activated locally on port 8071 on 23 October 2026. Production warm
refreshes took 2.62–2.92 seconds; the initial connection/preparation took 32.32
seconds. `evidence/deployment.json`, `production-validation.json` and
`browser-production.json` record the active identity, exact snapshot readback,
complete CAL 2030 and preserved calibration/ICEchat state. Rollback is
`1.4.4rc4-live-prices-comparison-charts-20261023/start.sh`.

The HH historical-spread candidate is staged separately as
`1.4.4rc4-live-prices-hh-spreads-20261023-r1`, including the reviewed compact rows.
It is not deployed. `options/docs/hh_historical_spreads.md` records its source,
complete-period and statistical review. Six saved captures with qualified HH
anchors produced complete curves; the short conditional daily settlement
backtest worsened far-tail errors. Structural acceptance is distinct from
economic acceptance, and this candidate is not recommended for activation.

The 23 October multi-market candidate uses `MARKET_CURVES_V1`, source schema 2
and namespace `live-prices-v5`. Pair `options.live_price_curve`, the TTF adapter
and `options.live_prices` with the pinned BBG collector and page/data/CSS files.
`options/docs/live_price_market_curves.md` defines product conventions, strict
source evidence, full hidden-month calculations, FX freshness and validation.
The immutable active-release pointer and the selected artifact's `evidence/`
determine deployment status. Snapshot namespaces include the pricing code
fingerprint; old references require Refresh and are never reinterpreted.
Publication/retention now require complete active curves and exact full-period
membership for all markets, not just TTF. Active expired months are excluded
from current-price counts. Henry Hub seasons use Apr–Oct / Nov–Mar. Mark cells
identify reconstructed estimates and official-settlement fallback; no new
diagnostic or Coverage columns are added. Staging/activation must preserve the
current Pricer/calibration files, flags, publications and ICEchat ownership.

The **V3 delayed-feed/completeness release was deployed locally on 22 October
2026** at `~/.local/share/brent-surface-release/1.4.4rc3-live-prices-unified-ttf-20261022/`.
It includes the unified TTF workspace and simplified tables. Its deployment
receipt, production validation and browser verification are in `evidence/`.
Fresh Bloomberg data verified 48/48 monthly TTF Marks and 28/28 active period
Marks, including full Cal 2030 and Winter 2030. Rollback is the previous
`1.4.4rc3-live-prices-quality-20261022` release. It adds a permanent
ten-minute Bloomberg ICE warning, instrument-specific declared delay handling,
labelled official D-1 fallback Marks and a complete-curve publication gate.
Its actor/source/version namespace is `live-prices-v4`. One complete snapshot per
scope is retained atomically under `~/.local/share/dash-options/live-prices-retained/`
with owner-only permissions and an integrity checksum. It preserves the original
capture timestamp on recovery after cache expiry or a failed Refresh. References
never recover a different actor's data or a newer revision in place of an old one.
Active period tables omit expired native instruments; every displayed TTF period
must have full monthly Mark coverage. Current source metadata comes from the
paired pinned collector; old captures lacking delay metadata are historical
evidence and cannot silently become current quote anchors.

Historical V2 release notes follow. That quality-curve revision was deployed locally
on 22 October 2026 and is now the rollback artifact. Its methodology and
provisional limits are documented in `options/docs/live_prices_methodology.md`.
Pair `options.live_prices` and `options.ttf_live_curve` with the dashboard changes.
The candidate uses source schema 2, methodology `TTF_QUALITY_CURVE_V2`, and actor
namespace `live-prices-v3`; old snapshots require Refresh and are never rewritten.
Its collector command adds `--quality-curve --quote-timezone <verified timezone>`.
Set `BLOOMBERG_OPTIONS.LIVE_PRICES_QUOTE_TIMEZONE` only after verifying Bloomberg's
user timezone. Optional `LIVE_PRICES_SPREAD_MAPPINGS` is the path to reviewed
independent spread mappings; expressions computed from outrights are ineligible.
`LIVE_PRICES_REPOSITORY_PATH` / `BBG_LIVE_PRICES_REPOSITORY_PATH` pins this page's
collector independently of the shared Bloomberg repository. The paired release
bundles the collector and request/session helpers, and references the existing
external configuration and TLS material without copying credentials.
The BBG CLI's default remains schema 1 for the existing deployed dashboard.
Do not switch that default independently of a paired, approved rollout.
Historical replay accepts old captures only through an explicit offline option;
strict live mode rejects their missing side timestamps. Model-only monthly Marks
do not generate executable quote sides. Replay results and policy limits must be
reviewed before deployment.
Quarter, season and calendar Marks project the complete internal monthly curve;
the visible 48-month ladder does not truncate their calculations. All 12 months
of Cal 2030 and all six months of Winter 2030 are required. Missing internal
coverage leaves an explicit gap, even when a raw period midpoint is available.
Raw period quotes and native official D-1 remain separate from curve Marks and
the weighted monthly D-1 audit. No additional collector mode or source-schema
change is required beyond the existing quality-curve capture.

`/live_prices` captures all markets only on its own Refresh action. Deploy the
`options.live_prices` module with `pages/live_prices.py`, `live_prices_data.py`,
`assets/live_prices.css`, and the router/configuration changes. The existing
immutable snapshot cache retains each user's captured raw inputs and calculated
view for 24 hours; a different browser tab's refresh does not replace that view.
Job polling reads progress only and stops on completion. No database migration
or settlement/publication worker change is needed.
Tables retain their fixed maturity/period order; column sorting is disabled.

The previous 22 October V2 quality-curve deployment was active at
`~/.local/share/brent-surface-release/1.4.4rc3-live-prices-quality-20261022/`,
over the current settlement-performance release. `evidence/identity.json`,
`scope-preservation.json`, and `preflight-validation.json` prove package identity,
unchanged calibration/dependencies, and a fresh Bloomberg capture with full
Cal 2030 and Winter 2030. Installed calculations exactly match all five reviewed
JSON snapshots. Activation waited for an existing PostgreSQL recovery outage,
then passed database readiness and preserved publication/intraday pointers.
The active-release pointer records the settlement-performance release as rollback. No database
migration is part of this change. `options/scripts/build_live_prices_release.py`
builds the scoped overlay; `options/scripts/activate_live_prices_release.py`
checks current baseline/job ownership, read-only database state and readiness,
then switches atomically with rollback. Run it only for an authorized rollout.

The collector is `BBG/live_prices.py`, using the existing BBG session and request
helpers. `BBG_REPOSITORY_PATH` selects that repository (default
`~/Repositories/BBG`); `BBG_LIVE_PRICES_PYTHON` selects its existing Bloomberg-enabled
Python interpreter (default: dashboard interpreter). The staged dashboard
environment may not contain `blpapi`, so set the latter to the established BBG
interpreter. Both settings are in the `BLOOMBERG_OPTIONS` configuration section
as `REPOSITORY_PATH` and `LIVE_PRICES_PYTHON`. Refresh requires the existing
`refresh_bloomberg` permission; snapshot reads require `view` and the same actor.

The FX basis selected for this page is Bloomberg EURUSD BGN daily closes and
the available EUR BGN forward-points curve. TFU uses the ICE front-month daily
averaging schedule, with those Bloomberg closes for fixed days, not the WMR
benchmark. Forward value dates are checked against Bloomberg metadata. Quoted
TFU futures and this calculated comparison are not interchangeable. In the
previously deployed r3 methodology, missing TTF
monthly sides use Bloomberg's ICE Endex `QZT` quarter side multiplied by the
monthly D-1 settlement divided by the arithmetic average of all three quarterly
monthly settlements, requiring a common settlement date. Complete two-sided
monthly quotes take priority; if either is missing, both Mark sides come from
the quarter. Live Mid/Bid/Ask retain the original monthly quotes separately.
Boundary-quarter months outside the displayed ladder are collected
for the denominator. Shape coefficients, quote provenance and quarter updates
remain visible. TFU consumes these shaped TTF sides. A remaining sole side is the
indicative mid; absent or crossed quotes and incomplete strips remain blank.
The TTF section pairs monthly marks with native Bloomberg calendar (`QTT`),
season (`QQT`) and quarter (`QZT`) quote tables. Their D-1 values are their own
previous-trading-day settlements. Monthly Spread is the preceding month's Mark
minus the current Mark, with no first-row spread. Additional validation columns
retain the shaped sides, coefficient and source metadata.
The bottom TTF comparison chart plots monthly Mark and D-1 from that same frozen
snapshot on one EUR/MWh axis; missing values remain gaps. It makes no extra source
requests and updates only with the page's manual Refresh.

## Coordinated 1.3.3 Brent publication release

Use analytics **1.3.3** and ICEchat **0.1.1** with the dashboard artifact at
`~/.local/share/brent-surface-release/1.3.3-governed-brent-20261003/`.
Its `staging_manifest.json` records dashboard files, analytics wheel digest,
dependency lock and release tools. The ICE wheel is recorded separately in the
same release manifest. `options/docs/governed_brent_quotes.md` defines the new
Brent reader, forward provenance and historical cutoff contract.

Install the built wheels into the release's dedicated environment. Validate
installed contents outside the source checkout with the paired-release checker;
do not reuse wheel digests from the historical build examples below. Use this
release's start/switch/rollback controls only after its acceptance gates pass.
Preserve the current feature flags and inspect queued/running jobs before the
paired switch. This change does not authorize background batch activation.

## Historical volatility refactor candidates

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

## Historical 1.2.5 build record

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
# Settlement automation candidate: analytics 1.4.1

This implementation is staged work. Installation does not enable automatic
publication. Deploy the paired dashboard and analytics wheel with migration
`20261005_01` and `20261005_02`; preserve current source routing, authentication, existing
publication flags and unrelated services. Back up the schema and publication
metadata first. Run the migration as the owner of the current surface view;
it preserves its definition, grants, owner and comment. It uses a five-second
lock timeout and aborts transactionally for unexpected view dependencies.
Published v2 rows have no automatic downgrade: rollback the application only
after verifying it can still consume their stored parameters and points.

Use a dedicated supervised settlement process with the same configured database
and the installed release interpreter, independent of Gunicorn. Start in shadow:

```sh
export OPTIONS_CONFIG_PATH=/absolute/path/to/runtime-config.ini
export VOL_CALIBRATION_SETTLEMENT_JOBS_ENABLED=true
python -m vol_calibration.settlement_worker --from-cob 2026-10-05 --mode shadow
```

`--from-cob` is an explicit operational boundary. Polling reconciles persisted
complete settlement snapshots on startup and every 60 seconds, deduplicating
source events and catching up after downtime. A scheduled one-shot may also
supply `--expected-cob YYYY-MM-DD --once` from the exchange business calendar;
an absent source is recorded as waiting. The worker does not invent daily
business dates from a weekday calendar. Intraday snapshots are excluded.

Inspect persisted state with:

```sh
python -m vol_calibration.settlement_worker --from-cob 2026-10-05 --status
```

Watch waiting inputs, held calibrations, source revisions, exhausted retries,
lease expiry, point/maturity counts and time to completion. A hold keeps the
previous publication active. Diagnose and submit corrected immutable inputs;
never relax a gate merely to complete a settlement. Source/model validation and
concurrency failures are permanent for that job. Runtime/database failures retry
up to three claims with persisted 30/60-second backoff (bounded at 300 seconds).
A lease expired at that limit becomes a retryable failed job. Permanent numerical,
source, deadline and authority holds require corrected inputs or configuration;
they cannot use the execution-retry command.

Promotion is deliberately separate. Collect at least 20 prospective shadow
sessions from `vol_settlement_promotion.shadow_start_cob`, replay the available
history, review all holds and confirm real consumer readbacks. Historical
sessions before that date do not count. The most recent required sessions must
all contain exact certificates for the current code/runtime and worker. Approve the explicit reviewed evidence window using the operator command below.
The gate checks every required calendar session, its latest shadow job and the
current source revision. Failed or pending jobs count as failures even without
candidate rows. Counted shadow jobs must be created and completed inside their
reviewed readiness/deadline window; late replays cannot count. It binds the approval to the exact evidence identity and cutoff;
future publish sessions do not extend that approval window implicitly. Corrections
to reviewed source sessions or changes in implementation require renewed review.
Promote HH first; assess it before approving Brent. No script in this change
automatically approves either product.

Only after those checks configure the dedicated service publisher:

```sh
export VOL_CALIBRATION_SETTLEMENT_SERVICE_ACTOR=settlement-publication-service
export VOL_CALIBRATION_SETTLEMENT_SERVICE_ROLE=publisher
export VOL_CALIBRATION_HH_AUTO_SETTLEMENT_ENABLED=true
# Enable Brent separately after its review:
# export VOL_CALIBRATION_BRENT_AUTO_SETTLEMENT_ENABLED=true
python -m vol_calibration.settlement_worker --from-cob YYYY-MM-DD --mode publish
```

The service does not inherit a local browser identity or generic write flags.
Publication rechecks promotion, the current source revision, source identity,
reviewed session deadline, cancellation and lease within the transaction. Its
bounded SHARE lock on the snapshot table prevents correction inserts from
committing between the final revision check and activation. It verifies every saved point and expiry result before
activation, then reads the committed publication on a fresh connection. A
completed publication followed by a process crash is recovered idempotently.
Existing same-day revisions cause a hold rather than automatic supersession.

For the kill switch, stop the supervised worker and disable the relevant auto
flag before restarting. Environment/configuration is resolved by the running
process, so changing a configuration file alone is not a live kill switch.
Keep shadow calculations enabled independently if useful. Do not republish
portfolio valuations or resend ICEchat messages as part of surface rollout.
Prove the active release path/version, persisted revision, hydrated selected
product dashboard and downstream pricing readbacks before declaring deployment
verified. A ready health endpoint or historical test alone is insufficient.


The 1.4.1 recovery migration adds `next_attempt_at` and `failure_kind` to jobs,
an immutable retry audit, reviewed session calendars, persistent alerts and
approval evidence identities. It clears older approvals because the corrected
worker needs fresh review. Apply both migrations before starting the new worker;
older applications remain readable, but old workers must be stopped during the
paired rollout.

Load an exchange calendar reviewed by the operator before enabling automatic
publication. Each product has a separate JSON manifest with `schema: 1`,
`commodity` (`BRENT` or `HH`), `version`, `source`, IANA `timezone`, `from_cob`,
`through_cob`, and an explicit `sessions` list. Each session contains `cob`,
`ready_at` and `deadline_at` as ISO timestamps with timezone offsets. The declared
coverage must contain every exchange session, including special holiday hours;
closed dates have no session row. The worker does not derive sessions from the
option-expiry calendar. Import is idempotent; removing or changing an existing
reviewed session is rejected and requires a reviewed correction migration.

```sh
export VOL_CALIBRATION_SETTLEMENT_OPERATOR_ACTOR=reviewed-settlement-operator
export VOL_CALIBRATION_SETTLEMENT_OPERATOR_ROLES=approver
python -m vol_calibration.settlement_worker --from-cob YYYY-MM-DD --import-calendar /path/to/reviewed-brent-sessions.json
python -m vol_calibration.settlement_worker --from-cob YYYY-MM-DD --import-calendar /path/to/reviewed-hh-sessions.json
python -m vol_calibration.settlement_worker --from-cob YYYY-MM-DD --approve-product HH --evidence-through YYYY-MM-DD
```

Calendar sessions become eligible at their explicit readiness time. A missing
source stays waiting; a missed deadline prevents automatic activation. Alerts
for holds, exhausted execution retries, missing/expired calendars, missed
completion deadlines and source corrections are persisted in
`vol_settlement_alerts`. The worker logs only new or changed alerts and their
resolution; direct them through the supervisor's existing monitoring. `--status`
includes active alerts, reviewed deadlines, next retry times, failure kinds and
completion latency. No external notification destination is configured by this
change.

After repairing a temporary execution failure, an authorized calibrator can
extend the retry allowance by three claims. The operation retains the job ID,
attempt history, valid maturity checkpoints and any certified candidate. It
rechecks current source, implementation and immutable input identity, and saves
operator, reason and previous/new retry limits in `vol_settlement_retry_audit`.
A duplicate retry of a queued/running/succeeded job is rejected.

```sh
export VOL_CALIBRATION_SETTLEMENT_OPERATOR_ROLES=calibrator
python -m vol_calibration.settlement_worker --from-cob YYYY-MM-DD --retry-job JOB_UUID --reason 'Temporary database outage repaired'
```

The official test suite includes disposable PostgreSQL regression coverage for
source corrections before and during activation, the phantom-insert lock,
missing/failed/pending promotion sessions, exhausted-job recovery, deadline
alerts and migration lock contention. The real Brent source loader is exercised
against persisted quote, forward and governed-expiry rows, including reads on
the publication transaction's locked connection. PostgreSQL binaries are
required for these tests; skipped persistence tests are not release evidence.

## Connection pool repair candidate — 9 October 2026

The inactive `1.3.3-connection-pools-20261009` dashboard artifact is derived from
`1.3.3-ice-fast-review-20261008`, with only the database ownership repair. Its
analytics wheel is `1.3.3+poolfix.20261009`; unrelated 1.4.2 source work is not
part of this artifact. The paired ICEchat candidate is
`0.1.3-connection-pools-20261009`, wheel `0.1.3+poolfix.20261009`.

`options.connection_pool` owns both dashboard and cached analytics engines.
Required/optional lookup flags do not enter pool identity. Equivalent default
PostgreSQL driver/port spellings share an engine; distinct effective database
identities never share connections. Initialization is synchronized. Each source
reserves five physical connections with zero overflow and a ten-second checkout
wait; all owned source reservations together are capped at ten per process.
The normal main/middle-office fallback therefore has one five-connection pool.
A separately configured middle-office source can use the second five-connection
pool. A third source is rejected explicitly until a quiescent configuration
reload, rather than silently expanding the process budget.

`clear_runtime_config_cache()` now disposes the shared pools before clearing
configuration. It refuses to run during active checkouts. Callers must reacquire
engines after a reset; retired references cannot silently open new pools. Child
processes discard inherited pools with `dispose(close=False)` and establish their
own connections. Never pass a borrowed connection across a fork. The aggregate
server budget remains per-process capacity multiplied by worker count; retain the
single-worker dashboard topology.

`options.connection_pool.database_pool_snapshot()` exposes process ID, hashed
source identity, reserved capacity, physical connect/close counters,
checkout/checkin/invalidation/disposal counters and recent retired-pool totals.
It contains no connection strings or credentials. Bloomberg's worker pool and
ICEchat's persistence pool are separately bounded to two retained plus one
overflow connection each. ICEchat's three-connection ceiling includes its
long-lived advisory-lock connection. Worker startup and shutdown failures must
still dispose pools. A rejected ICEchat lock acquisition must never publish a
stopped status for the process that actually owns the bot.

Source defaults and staged launchers identify sessions as `dash_options`,
`bbg_option_chain_worker`, and `icechat`; the dedicated ICEchat session continues
to use `icechat_service_lock`. Saved LaunchAgent settings are not active until a
normal reload/login. Do not edit a sealed release launcher without regenerating
its paired manifest; the original dashboard launcher was restored and the naming
change is included in the candidate instead.

Verification evidence and the source/staged PostgreSQL soak are in
`../connection-audit-20261009/`. No production service was restarted to build or
test this candidate. Activation requires a later controlled maintenance step:
confirm no running Bloomberg job, switch the dashboard using its release controls,
reload the Bloomberg LaunchAgent, and gracefully stop/reload ICEchat only when
replacing its existing lock is authorized. Verify the new application names,
per-process connection plateau, worker heartbeat, the single reacquired ICEchat
lock and hydrated dashboard before declaring the incident resolved. Preserve the
old artifacts and launch configurations for rollback; do not enable new features
or automatic publication as part of this repair.

## Daily settlement performance release

On 22 October 2026, the local dashboard on port 8071 was switched to
`~/.local/share/brent-surface-release/1.4.4rc3-settlement-performance-20261022/`.
Its paired revision is `a9a10cecbcdfbcab00b7d96a4401367fc452ec87732eaa0ce347ab1c96e3373f`;
the database migration head is `20261021_01`. Acceptance is limited to this
computer as requested. All existing feature flags, Live Prices code, external
configuration and snapshot-cache location were preserved. Daily scheduling and
automatic promotion remain disabled. The active-release pointer records the
schema-compatible r3 rollback launcher. The rollout did not publish new surfaces.

The daily runner for Brent, HH, TTF, JKM and NBP uses the paired analytics
implementation and additive migration `20261021_01`. Its commands and current
performance/acceptance evidence are documented in
`options/docs/settlement_performance.md`. Installation does not activate
the daily flag, a scheduler, service publication or automatic promotion.

An intended daily invocation needs `VOL_CALIBRATION_SETTLEMENT_JOBS_ENABLED=true`,
a configured local identity, and the existing product publication permissions and
flags when `--publish` is requested. `--status` reads owned progress; `--resume`
keeps the saved mode/date. Exhausted temporary execution failures require an
explicit `--retry-job JOB_ID --reason 'reviewed cause'`; it audits three further
claims and rechecks pinned sources, the prior publication, code and owner.
Schema/data/permission/validation holds cannot use that retry operation. Finite
mean quote-error breaches remain publish-with-warning events.

The 27 October 2026 worker correction selects the latest active published COB
strictly before the requested settlement, then loads that immutable publication
by ID inside the input-pinning transaction. It covers weekends, holidays and
late approvals without changing historical readers' point-in-time semantics or
the final preceding-publication/source/revision checks. The worker fingerprint
changes, so jobs pinned by the older worker require a new explicit submission.
The paired `1.4.4rc4-settlement-recovery-20261027` release includes this correction
and the explicitly approved HH first-month body maximum of 5.30 volatility
points. HH mean limits, later-month limits and all other validation gates are
unchanged. The 26 October operation preserves the earlier held attempts under
`options/outputs/settlement_calibration_20261026_20261027/`.

After a reviewed gas optimizer failure, an explicit invocation may set
`VOL_CALIBRATION_DAILY_WARM_START_COB=YYYY-MM-DD` to use an older active
publication's parameters as starting guesses. The date must precede the current
base settlement. Its immutable ID is pinned in the job identity and candidate
manifest separately from the preceding publication used for lineage/CAS; cache
reuse requires both identities to match. All quotes and the full final grid
are fitted and validated for the requested COB. The normal launcher leaves this
recovery override unset.

The `1.4.4rc4-settlement-recovery-20261027-r2` update adds a bounded gas-only
optimizer fallback after nine ordinary starts fail. Three additional starts
target a stricter density buffer of 0.02; acceptance retains the original 0.006
density floor, dense call-convexity checks, final 401-point price grid and all
source/expiry/quote gates. Accepted ordinary fits are returned unchanged. The
extra starts and their solver buffer are recorded in fit diagnostics. All seven
previously held 26 October gas expiries passed the unchanged checks in the
captured-input review; the hybrid model/grid suite passed 22 tests.

The `1.4.4rc4-hh-expiry-reconciliation-20261028` release handles LNE expiry-day
snapshots whose raw chain retains expired contracts. Raw/normalized count
differences must reconcile exactly to explicitly declared expiry-day rows with
zero or missing settlements; positive-price and live omissions remain blocking.
Inclusive and live underlying counts are checked against their respective raw
cohorts. Financing rates and original capture timestamps remain mandatory for
every live option; expired intrinsic settlements do not require a rate. Raw
quote, price, convention, forward and expiry checks still cover the full source.
The 27 October snapshot passed source preparation with 61 live HH maturities
starting December 2026. No calibration limits or numerical fitting changed.

Use `python -m integration.rehearse_daily_release --help` for the portable
owned-PostgreSQL migration/publication/rollback rehearsal. The fixture explicitly
excludes old external trade-table migrations and is not a production database
clone. A rollback application needs the new migration definitions so readiness
can recognize the upgraded head. Preserve the actual active non-calibration app
and library, existing feature flags, external configuration and snapshot cache.


## HH response refinement — staged review only, 23 October 2026

The immutable candidate `1.4.4rc4-live-prices-hh-response-20261023-r1` is paired
at revision `84a2c076cec68fea5aac3acfbbb91698b3f739940ac5558ab783e045e322f088`.
It is **not deployed**. Production remains the fast-refresh r6 release on 8071.
The private review runs on 8098, with saved source inputs, a separate actor/cache
namespace and calibration write/publication/background flags disabled.

HH unsupported months now estimate their response to an original current anchor's
move from aligned daily changes, with fixed regularization and complete monthly
projections for periods. Six eligible saved captures remain complete; three
older captures lacking accepted HH anchors are rejected. Full CAL 2030 includes
hidden November/December. Other-product prices and accepted HH quotes are unchanged.

Structural validation is not economic acceptance. On eleven shared historical
outcome dates, far-tail MAE improves slightly versus deployed but full CAL 2030
MAE worsens slightly; the old full-move proposal performs worse. The same history
was already inspected, so this is not a new independent validation set. No passed
activation acceptance receipt is issued. Review artifacts and the all-product
improvement plan are under `~/.local/share/live-prices-hh-response-review-20261023/`
and `options/docs/live_price_market_curves.md`. Do not activate this candidate by
confusing source/unit-test completeness with prospective price-quality evidence.


## All-product long-history candidate — review only, 23 October 2026

V3 completes the final displayed calendar year and all final seasons. The review
shows Nov 2026–Dec 2030 and computes internal months through Mar 2031. Preserve
manual Refresh, compact unsortable grids, the ten-minute delay notice, existing
snapshot retention and omitted diagnostic columns. The analytics wheel adds
`options.curve_history`; BBG owns the year-long settlement/FX capture and prepared
archive cache. Dash contains presentation only. The candidate is isolated from
production and must not be activated until the user reviews its curves and marks.

## Shared volatility history and Brent table retirement — 29 October 2026

The live release is `~/.local/share/brent-surface-release/1.4.4rc4-shared-delta-history-20261029`.
`delta_history_data.py` supplies monthly history for Brent, HH, JKM, NBP and TTF.
The fixed-expiry history selector discovers contracts from the dated surface and
preserved archive; it no longer depends on a hardcoded Brent contract list.

Published IV uses the existing operational surface loader: active published
Brent/HH calibrated surfaces, and the existing derived/raw surface history for
the other products and legacy dates. Prices use `at_lng.curve`,
`at_lng.bbg_settlements` (PX_SETTLE), and eligible complete settlement snapshots
in `at_lng.vol_market_snapshots` / `at_lng.vol_market_forwards` for Brent/HH.
JKM uses only the existing ICE_JKM_MO curve; no Bloomberg JKM futures fallback.
For each fixed month, select one whole price source by complete-window coverage
then recency. Never splice providers, fill missing sessions, roll contracts, or
plot different currency/unit conventions on one settlement axis. The price
capture stays fixed within a surface snapshot. After an upstream backfill,
Refresh Options Data creates a new capture and recalculates coverage.

Realized volatility requires every scheduled settlement in the trailing
30-calendar-day window, using sample daily log-return standard deviation and
sqrt(252) annualization. IV is the 50-delta call, masked at option expiry.
Delta history preserves each selected bucket, contract and trading-date gap.

The five `brent_delta_history_{runs,points,settlements}` and
`brent_realized_history_{runs,settlements}` tables are retired. No replacement
SQL tables were created. All 3,861 reconstruction rows and all three delta runs
are sealed in `data/volatility_history_archive/`; latest scoped run selection
matches the old reader's 2,651 observations. Reconstructions are separate dotted
series, explicitly labelled, and never overwrite official or published IV.
All five original tables, including full run inputs and superseded data, are
preserved in `evidence/retirement/brent_history_restore.dump` plus compressed
raw exports. The complete backup was restored, reconciled, retired, and restored
again in disposable PostgreSQL before the live retirement.

Migration `20261029_02` removes empty obsolete tables on new installations and
refuses populated ones. Use `integration.retire_brent_history` only after
archival and compatible-reader deployment; it locks and reconciles all rows
before one atomic DROP RESTRICT transaction. Legacy backfill CLIs now refuse
execution, preventing accidental recreation of the old stores.

Rollback must restore the verified tables BEFORE starting the previous reader.
Use the live release's `restore-and-rollback.sh`, recorded in
`active-calibration-release.json`; do not start the previous release directly.
The helper refuses partial or changed existing tables and verifies every restored
row before switching the owned dashboard process. Keep the release and backup
until this recovery path is no longer required.

Release evidence includes exact archive parity, restore rehearsal, all-product
browser checks, product/contract/trading-date coverage, and the guarded retirement
receipt. The installed suite has 1,032 passing tests and three known baseline
failures in ICEchat quote-sign serialization and TTF optimizer retry ordering;
all 41 focused history and retirement tests pass.


## Surface table layout — 29 October 2026

The live release is `~/.local/share/brent-surface-release/1.4.4rc4-surface-layout-20261029`.
The Volatility Surface page no longer renders or queries the legacy TTF ICAP/ICE
comparison section. Its chart/grid helpers and callback were removed; valuation
comparison data and other pages are untouched. Surface Matrix and Difference vs.
Previous Date now use a page-scoped responsive grid: two columns when the container
can accommodate two 780px panels and a 14px gap, otherwise one column. Each AG Grid
retains its own horizontal scrolling. Table values, column definitions and exports
are unchanged (all 69 remaining page functions match the preceding release).
All 39 affected tests pass; browser verification covers 1920, 1280, 960 and 320px.
Rollback to the immediately preceding shared-delta-history release is schema
compatible and does not require restoring the retired Brent tables. The earlier
restore-and-rollback helper remains in that preceding release for a deeper rollback.
