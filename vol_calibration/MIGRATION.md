# Vol Calibration migration

## Refactor candidate 1.3.1

Candidate verification is complete for 1.3.1. The 1.3.2 cleanup retains the same
public operations and job/checkpoint schema 3 while enabling strict database
errors for required worker setup. Follow
`options/docs/vol_calibration_cleanup.md` for that artifact's current verification
status; the earlier reports below retain their original release scope.

The coordinated analytics candidate moves selected-expiry operations to explicit
owners and shares chronological TTF/JKM orchestration. Public API operations and
editable-table/export formatting are retained. Numeric checkpoint schema 3 and
job payload/type version 3 use exact typed input identities; schema-2 checkpoints
and jobs must be resubmitted under the new implementation. Do not transfer a live
lease between these versions. Before a later rollout, finish or cancel old jobs
using their original release and owner controls, then switch the paired artifacts.
Existing publication identifiers and legacy publication fingerprints stay readable.

The candidate requires complete installed-product replay, isolated publication and
worker readbacks, hydrated browser/export checks, paired artifact identity and a
rollback record. It is not a production deployment. Its evidence is kept separately
in the options repository under `outputs/vol_calibration_refactor_20261001/`.

## Shared analytics extraction, October 2026

The numerical implementation is consolidated into the separately installed
`options.vol_calibration` package. Its `api.py` is the application entry point;
request/result contracts contain numerical inputs and diagnostics rather than
table formatting or browser state. Applications retain callbacks, authentication,
feature flags, queues, cancellation/progress, and bounded process dispatch.

The shared Wing engine, gas core/tails and convex-call fallback, input eligibility,
bounded retries, checkpoint data contracts, Brent/TTF adjustments, chart delta
evaluation, parameter assessment, and publication-expiry candidate construction
now live in that package. Brent/HH model and source-preparation implementations
are also canonical there, with operational command lines under `cli/`.
Compatibility paths forward to these implementations without numerical copies.
Chronological gas batches and their checkpoint records are numerical engine
contracts; the dashboard formats foreground and durable-worker results through
the same adapter. Durable job payloads are now version 2 and incompatible
retained jobs are rejected. Publication persistence is shared, with identity
authorization and bounded immutable-grid caching retained in the dashboard.
The immutable-content cache calls the public `load_publication_contents` engine
operation. SQL and serialization remain canonical in options; the dashboard
controls cache lifetime and publication authorization.
TTF manual mark inversion, intraday fitting, published smile rebasing,
trading-date policy and traded-option coordinates/eligibility are also shared.
Legacy official gas extrapolation and long-end shaping are canonical in options.
Selected-expiry fitting, accepted-row evaluation and node edits now use the
public engine API rather than private compatibility helpers. The application
supplies bounded process dispatch. HH publication source-lineage validation is
shared as well; its candidate fingerprint uses relative application labels.

The recorded complete installed replays match the frozen 30 September grids
for all five products. NBP requires the lossless source capture: a pandas JSON
roundtrip changed last-bit input values and materially changed fitted tails.
These replay reports are artifact-specific and are not deployment evidence.

The engine's implementation manifest is included in candidate/job fingerprints.
Installing a changed implementation invalidates incompatible retained candidates;
published revisions remain readable. Web and workers must use the same wheel.

Three unused dashboard forwarding modules (convex-call core, gas retry helper,
and JKM hybrid model) have been retired. Remaining forwarding paths have existing
consumer contracts and contain no numerical copies. Operational pages use the
public API; TTF/JKM adapters supply execution and formatting only.

Actual staged TTF/JKM worker batches, resume/cancellation/stale-job behavior and
their persisted dense grids match the references. The HH browser candidate and
delivered workbook reconcile to all 24,862 points and 3,210 raw observations;
Excel retains timezone-aware capture timestamps as ISO text. TTF export now
resolves the same settlement target as the validated batch and rejects changed
inputs or table state with feedback. The published-surface sheet also contains
timezone-aware timestamps. TTF/JKM workbook frames use a presentation-only
timestamp adapter so capture instants and offsets survive Excel delivery without
changing calibration values. Calibration actions stay disabled until market
inputs and editable parameter rows have arrived; the server also rejects an
attempt to start a batch with missing page inputs.
Fresh-process timing comparisons use six counterbalanced observations per
variant: full Brent/HH runs and representative TTF/JKM/NBP expiry fits, with
unchanged numerical contracts. They do not establish full-batch or browser
performance improvements. The actual prior/candidate/rollback artifacts have
also been rehearsed on an isolated port with database reads enforced read-only.

Deployment status is recorded separately from the source code. Before switching
production, verify the exact paired dashboard/wheel manifest, delivered browser
exports, complete published reads, retained compatibility consumers, and the
coordinated web/worker rollback. Preserve the actual deployed configuration and
product flags. This migration does not republish existing surfaces. Operational
evidence and the production switch record are kept in the options repository's
`outputs/vol_calibration_migration_20261001/` directory; historical reports must
not be presented as verification of a later artifact.

## Historical dashboard integration

The contents of this package were imported into `dash_options` as a squashed
Git subtree and then adapted to run inside the existing Dash application.

- Archived source repository:
  [`efernandezpibrall/dash_vol_surface_calibration`](https://github.com/efernandezpibrall/dash_vol_surface_calibration)
- Source commit: `638463d775324e7494eda8a65241714e85e0ddc9`
- Import destination: `vol_calibration/`
- Import policy: one-time migration; there is no ongoing subtree synchronization
- Retirement status: the standalone repository is retained only as a read-only
  historical archive; no local clone is required

The standalone Dash app, router, port 8056 server, and calibration navbar were
removed. The root `dash_options` app is the sole callback owner and server.
The later `/vol_calibration` page was also retired; calibration is mounted
inside Vol Trades at `/vol_trades`. The `vol_calibration` package and
governed database tables remain in use by that workspace and its consumers.

Release 1 enables reading, diagnostic calibration, comparison, and export.
Database writes and publication are disabled by default through:

- `VOL_CALIBRATION_ENABLED=true`
- `VOL_CALIBRATION_WRITES_ENABLED=false`
- `VOL_CALIBRATION_PUBLISH_ENABLED=false`

Release 3 persistence is scaffolding only. The additive Alembic revision creates
immutable run inputs, append-only results/audit/surface records, guarded
publication lifecycles, a verified option-expiry calendar, and a
PostgreSQL-backed lease queue. It does not run automatically and it does not
enable any write callback.

Read-only product workspaces use a bounded process-local cache keyed by product,
COB date, and a hash of source configuration. Successful snapshots live for
five minutes by default; synthetic fallbacks live for only five seconds so
source recovery is detected quickly. Reload bypasses the cache. The relevant
settings are `VOL_CALIBRATION_CACHE_MAX_ENTRIES`,
`VOL_CALIBRATION_CACHE_TTL_SECONDS`, and
`VOL_CALIBRATION_SYNTHETIC_CACHE_TTL_SECONDS`.

Keep these production defaults until authentication, migration rehearsal,
worker, approval, and rollback gates pass:

- `VOL_CALIBRATION_WRITES_ENABLED=false`
- `VOL_CALIBRATION_PUBLISH_ENABLED=false`
- `VOL_CALIBRATION_BACKGROUND_JOBS_ENABLED=false`
- `VOL_CALIBRATION_GAS_BATCH_JOBS_ENABLED=false`
- `OPTIONS_TRUSTED_PROXY_AUTH_ENABLED=false`
- `TRINOS_VERIFY_SSL=true`

When trusted proxy authentication is eventually enabled, the proxy must
provide a server-verified user, roles, and shared secret. Roles are enforced by
server-side authorization helpers; UI visibility is not treated as
authorization. Database credentials remain external configuration and no
migration is applied during application import.

Local workstation operation may instead use the explicit `local_loopback`
authentication mode in the external `config.ini`. That mode takes its user and
roles only from server configuration, rejects forwarded headers and non-loopback
addresses, and is not supported behind a proxy. Assign `calibrator,publisher`
only to a trader authorized to publish their own validated intraday surface.

The analytics implementation remains in the separately managed `options`
package and is not duplicated here.

TTF and JKM settlement batches can use the separate durable gas batch flag
after Alembic revision `20260928_01` has been applied. This mode snapshots the
selected official inputs, persists verified per-expiry checkpoints in
`at_lng.vol_calibration_job_items`, and returns completed candidates to the
existing publication gate. It does not publish automatically. The feature is
independent of the older generic background-job flag above.
