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
# Brent/HH settlement worker (analytics 1.4.1)

`settlement_worker.py` is a separate process and uses only
`options.vol_calibration.api` for source preparation, fitting, validation,
lossless transport and publication. It does not run inside a page callback.
The existing gas batch worker remains a separate job family. Job claims now
support an explicit family filter, preventing either worker from consuming the
other's queue.

Brent policy `brent_single_svi_actual_strikes_v2` retains the first accepted
quote fit, then tries admissible minimum-variance SVI coordinates and a bounded
neighbour seed when needed. The new Brent model uses raw SVI wings. Historical
pickles without `raw_svi_wings` retain the old taper. HH policy
`hh_lne_single_svi_seasonal_quotes_v2` uses the same recovery, admissible sparse
coordinates, and explicit sparse quote/optimizer gates. Rich fit failures cannot
silently become extrapolated months. Failure diagnostics contain all failed
maturities; sparse dependencies are reported rather than manufactured.

Source preparation checks independently generated monthly expiry coverage,
quote/contract expiry agreement, exact source rows, forwards, conventions,
weighting policy and code/runtime identity. Publication candidates retain the
same 401 points and 11 anchors per maturity. Certification checks original
quotes and premium/IV reconciliation, the final model's density, variance,
delta inversion and monotone/convex call prices. Calendar monotonicity across
different delivery futures is not an acceptance condition.

The worker reuses PostgreSQL jobs, leases, cancellation and item checkpoints.
Candidate/certificate and readiness state are durable in
`vol_settlement_candidates`, `vol_settlement_source_events` and
`vol_settlement_promotion`. Tagged float transport round-trips all IEEE bits;
no pickle or rounded pandas JSON is used for retained candidates. A source or
implementation change requires a new submission and cannot reuse a checkpoint.
Restart after publication commit recovers through the certified idempotency key.
Permanent source, model, certificate or concurrency failures are held; execution
failures receive at most three claims. Stale workers cannot activate a revision.

Automatic publication requires **all** of: settlement jobs enabled, a separate
per-product auto flag, an explicitly configured service publisher, a database
promotion approval and at least 20 prospective successful shadow sessions under
the exact implementation. Historical replay does not satisfy that gate. All
new flags default off. Automatic jobs expect no existing same-day publication;
corrections and manual/automatic competition are held for review.

Migration `20261005_01` preserves v1 policies and admits v2, removes numerical
scale truncation from point/error storage, and restores the current TTF view
definition, owner, grants and comment around that change. Unknown dependent
views cause the migration to fail transactionally and require a reviewed
extension. Deploy both repositories together; numerical source edits invalidate
certificates and prospective shadow evidence.

Verification commands (project interpreter, paired checkout):

```sh
python integration/verify_settlement_pipeline.py
python integration/replay_settlement_sources.py --from-cob 2026-09-01 --through-cob 2026-10-02 --output-dir /path/to/replay
python ../options/scripts/check_vol_calibration_boundaries.py --dashboard .
```

The PostgreSQL verifier owns a temporary cluster and never loads a configured
database. It tests genuine persistence and locks with pinned synthetic source
fixtures. Historical replay separately loads the configured database with
read-only connections, verifies lossless certificates, and checks active
publication pointers before and after.


Analytics 1.4.1 retains the v2 numerical policies and adds typed HH seasonal
checkpoints containing predecessor/input identities and original sparse-fit
diagnostics. Compatible sparse slices are reused on restart; changed dependencies
are fitted again. Acceptance thresholds come from the pinned product policy for
both fitting and certification. Brent and HH SQL frame readers can reuse an
existing locked SQLAlchemy connection, preserving the publication source check.

Apply `20261005_02` after `20261005_01` for recovery and operational controls.
Jobs carry durable retry eligibility and failure classification; only temporary
execution failures can be retried through the audited operator command. Job
attempt counters are preserved, with three additional claims per authorized
recovery. Source/model/deadline holds remain permanent for that submission.
Promotion now uses reviewed session coverage, successful latest jobs, current
source revisions and an explicitly approved evidence cutoff/identity. A saved
shadow candidate without a successful job cannot count. Worker identity includes
execution, operations, job, authority and feature-flag code.

Session calendars are explicit reviewed imports with exchange timezone and
readiness/deadline timestamps. The worker reconciles due sessions, stores missing
inputs, blocks late activation and persists meaningful alerts with resolution.
See `DEPLOYMENT.md` for calendar import, approval, retry and monitoring commands.
No production calendar, approval, service identity or automatic-publication flag
is enabled by installing this code.
