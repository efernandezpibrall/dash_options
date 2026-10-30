# Aspect source positions and valuations

Greeks combines independently booked Aspect and `trades_options` option valuations
only for the selected COB. Linear Aspect exposure is shown separately. Units, signs,
maturity aggregation, filters and workbook values are unchanged. The displayed
linear positions always belong to the exact source snapshot used by the displayed
option valuation; a failed newer fetch or revaluation produces an explicit warning.

## Four-table contract

Revision `20261030_03` separates the old combined snapshots into:

| Relation | Ownership |
|---|---|
| `aspect_position_snapshot` | One ingestion attempt: UUID, requested COB/mode/scope, request parameters, HTTP outcome and attempts, original response/body, content or import-file hash, receipt timestamps and validation status |
| `aspect_position` | Every received position with immutable raw JSON and original response index; typed source fields and accepted/rejected validation results |
| `aspect_valuation_run` | One pricing attempt referencing a source snapshot: UUID, status, timings, expected/valued option counts, engine revision, input manifest and publication flag |
| `aspect_option_valuation` | Option-only model inputs and results, keyed by run and source row; composite foreign keys prevent linking results to another run's source |

One source snapshot can have several valuation runs. Source receipt commits before
business validation or market-data loading. Unsupported option classifications,
missing market inputs, malformed JSON and authentication/network failures retain
an ingestion record and available response evidence. Raw rows are not deleted when
rejected. The normalization projection is finalized once; revaluation does not
rewrite a successfully validated source. Original response indexes are retained
before filtering. They are local row identifiers, not assumed Aspect trade IDs.

Only complete successful runs can become current. Publication is transactional,
serialized per COB/mode/scope, and ordered first by source request time and then by
valuation request time. Repricing an older portfolio cannot replace newer positions.
A failed run retains its diagnostic state and leaves the previous published run
available. A confirmed empty source can publish an explicit zero-position run.
Ingestion status and valuation status remain distinct.

The migration backfills the existing data and checks both directions with SQL
`EXCEPT ALL` before replacing the two old names with read-only compatibility views.
Original decimals, raw rows, timestamps, hashes, UUIDs and current flags survive.
Legacy imports are labelled `legacy_import`: absent full responses, original row
indexes, engine revisions and valuation timestamps remain unknown. Reconstructed
source data is never presented as an original HTTP response. Migration downgrade
refuses to collapse or discard history.

## Fetch and revalue

Normal page loads and Refresh Options Data only read saved data. The Aspect COB/Live
buttons fetch and save a new source snapshot, then value it. **Revalue saved positions**
selects the latest received source for the selected COB/mode/scope and prices it
without making an Aspect request. The selected source UUID is pinned in the job;
subsequent fetches cannot change what that job prices. Revaluation requires database
and market-data access but no Aspect credentials.

All three actions require the independent `ASPECT_PERSISTENCE_WRITES_ENABLED` flag
and server-side `REFRESH_ASPECT` permission. Deployment keeps the flag false until
separately enabled. Authentication modes and other product flags are unchanged.

Jobs run in a separate installed Python process with a shared diskcache status store,
two-job workstation limit, duplicate protection and a 15-minute deadline. SQL has
bounded statement/lock timeouts. HTTP GETs have connect/read timeouts and up to three
attempts for transient errors; authentication failures are not retried. Backoff has
jitter and observes `Retry-After` up to 60 seconds per wait. Logs are under the snapshot
cache's `aspect-jobs/` directory. This executor supports one host; multi-host execution
requires shared job coordination. A killed worker may leave a pending source/run;
it cannot publish incomplete data, and an explicit retry creates a new attempt.

```sh
# Fetch, retain source positions, and value them.
ASPECT_PERSISTENCE_WRITES_ENABLED=true python -m options.aspect --mode settlement --cob-date 2026-10-29 --config /path/to/config.ini

# Reprice a specific saved source with no Aspect HTTP call.
ASPECT_PERSISTENCE_WRITES_ENABLED=true python -m options.aspect --source-snapshot-id UUID --config /path/to/config.ini
```

Configure `ASPECT_USERNAME`, `ASPECT_PASSWORD`, and optionally
`ASPECT_POSITION_REPORT_URL`, `ASPECT_POSITION_REPORT_STRATEGIES`, and
`ASPECT_POSITION_REPORT_BOOK`. `AD-LNG-Phy-OPT` remains mandatory in the configured
scope. TLS verification defaults to enabled. Credentials and authentication headers
are never persisted in request metadata. `pricingDate` is retained as an Aspect
source field and is not assumed to mean report COB. Live source mode still uses the
governed pricing inputs; it does not imply an intraday option-market feed.

## Reads, exports and rollback

Readers select a published valuation UUID, then join only its source positions.
Ordinary reads exclude raw response JSON/body; shared decoded/combined caches are
keyed by immutable run identity and source/configuration/release identity. Latest
attempt status is checked separately, so failed newer work remains visible without
changing a previously issued snapshot's contents. Source errors are never cached
as successful empty portfolios. Clearing filters remains different from defaults.

Exports identify source snapshot, valuation run, source origin, engine revision,
input dates, scope, original timestamps and rejection details. Source and valuation
metadata can be audited without calling the webservice. Importing historical INSERT
files remains supported through the literal-only importer and is atomic across the
four tables; it refuses to replace an existing publication in the same scope/mode/COB.

Before deployment, drain Aspect and calibration jobs. Build paired complete wheels
with the migration tree, verify the installed artifact, rehearse migration and
compare browser/workbook results. The prior version-aware dashboard can read the
compatibility views for rollback with writes disabled and the current migration tree
in `OPTIONS_APP_ROOT`. Old writers must not be restarted: compatibility views are
read-only. Never downgrade the schema or discard new source/run history to roll back
application code.

The preserved 29 October import contains 205 positions, including 63 options. It uses
16 October volatility. The separate core valuation for that COB remains unavailable
because governed SOFR model metadata is missing; do not bypass validation or combine
another COB to fill that gap.
