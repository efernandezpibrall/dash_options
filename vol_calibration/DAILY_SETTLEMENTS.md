# Daily settlement performance workflow

This explicit operator command supports Brent, HH, TTF, JKM and NBP. It does not
schedule runs or enable automatic promotion. Analytics live in the installed
`options.vol_calibration` package; Dash owns authority, leases and persistence.

## Installation and execution

Install the paired analytics wheel and dashboard artifact. Apply additive
migration `20261009_01` after `20261005_02`. Preserve the existing connection-pool
fix and independent product flags. Set the settlement-jobs flag only when this
workflow is ready for use. Publication additionally requires the corresponding
product's write/publication flags and an authenticated publisher. The CLI uses
explicitly configured local-loopback identity, never an actor supplied on its
command line. Remote automatic Brent/HH publication retains its separate
service authority, session calendar and promotion controls.

From the installed dashboard directory, using its dedicated interpreter:

```text
python -m vol_calibration.daily_runner --cob 2026-10-08
python -m vol_calibration.daily_runner --cob YYYY-MM-DD --publish --timings daily-timings.jsonl
python -m vol_calibration.daily_runner --resume JOB_UUID
```

The first command prepares candidates only. The date above is illustrative;
do not rerun an already-published date without reviewing its active revision.
`--products BRENT HH` limits the scope; omission selects all five products.
`--resume` retains the saved mode, source, prior parameters, candidate and owner.
A changed implementation requires review and a new submission. A prepared job
is not silently promoted by adding `--publish` to its resume command.

The optional timing file records stage durations, SQL query identities/counts during
publication, copy sizes and compressed artifact sizes without credentials or SQL values.

One repeatable-read source view pins exact settlements, the governed calendar,
prior parameters and the active revision expected at publication. Inputs are
stored atomically with job creation. Lossless compressed JSON records retain
full floating-point values; there is no pickle or rounded numerical transport.
Checkpoints flush in chunks of eight under the current lease fence. Independent
observed gas expiries use at most four workers; dependent tails remain ordered.
`DAILY_OBSERVED_FIT_WORKERS=1` selects serial execution. Publication is serialized.

## Failure and verification contract

SQL failures schedule at most three attempts, with 5/10-second delays before
remaining retries. The batch retries transient failures after that delay; a stopped
command can also be resumed. There is no recurring scheduler. Fitting, source, authorization or revision conflicts hold the job for
review. Cancellation and lease reclaim fence checkpoint and publication writes.
The ledger distinguishes accepted, publishing, published and verified. A lost
commit response is resolved by the original publication key and certificate
before replay. A superseded committed publication is held, never reactivated.

A publication uses one same-transaction source-content check under locks. The
first advisory lock, subsequent SQL and idle transaction all have time limits.
Every certified point is hashed with an exact binary encoding before activation;
one fresh full canonical read checks all numerical and provenance fields plus
expiry diagnostics after commit. Metadata lookups do not fetch dense grids or
large source manifests. Audit schema 2 references the immutable run configuration;
legacy audit records remain readable through the explicit evidence API.

The batch wrapper retains the full historical content scrub before and after
execution, even on failure. It covers every prior point, expiry diagnostic and
run configuration. Existing append-only triggers reject updates/deletes, but do
not alone prove insertion is impossible after publication. Therefore the full
scrub has **not** been replaced with cached digests. A future migration must
prove that stronger boundary and baseline/backfill before enabling that shortcut.

After the batch, release acceptance also requires the separate installed-reader,
Pricer and hydrated selected-product UI checks against exact publication IDs.
A job's `verified` state proves canonical persisted readback; it does not claim
that a browser or another installed consumer was inspected.

## Rollback

Do not drop durable artifacts or publication history to roll back code. Stop new
daily claims, let active claims finish or cancel them through owner controls,
then restore the recorded prior dashboard and analytics artifact together.
Keep the additive tables and schema revision; older workers ignore this job
family. Retained jobs require their original implementation fingerprint.
Rolling back the application does not roll back a published market revision.
Use governed supersession after review if a data correction is needed.

The five-minute median/eight-minute p95 remain proposed targets until sufficient
comparable end-to-end production observations establish them. Component timings,
local replays and isolated publication rehearsals do not establish those SLAs.
