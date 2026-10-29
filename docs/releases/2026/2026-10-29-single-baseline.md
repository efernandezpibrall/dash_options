# Single migration baseline — 29 October 2026

The active migration chain is replaced by one frozen root at `20261029_03`.
The 27 former revision files and three retired-history SQL files are removed
from active discovery. Their exact contents remain in the checksum-pinned,
packaged recovery archive from commit `b946433`.

Existing databases retain the same checkpoint and perform no DDL/data writes.
Fresh dashboard installations create the final schema and required seeds after
the external shared-table prerequisites are provisioned. Readiness recognizes
the historical capabilities explicitly superseded by the baseline. Recovery of
older checkpoints uses the archived graph through the current transactional
Alembic environment, with no manual revision stamping.

Verification of the committed implementation:

- Full suite: **1,079 passed**, 20 existing warnings, 97.79 seconds.
- Independent full archived replay matched the baseline's relations, columns,
  constraints/validation flags, indexes, triggers, functions, views and seed
  data. Equivalent string-array casts and convention review timestamps are
  handled as documented in the baseline guide.
- Existing data, relation identity, fractional quantities, view permissions,
  old-checkpoint recovery, failed-upgrade rollback, populated-history refusal,
  future revisions and readiness were tested in owned temporary PostgreSQL.
- The configured database remained at `20261029_03`. The exact source upgrade
  path executed five SELECTs in a server-enforced read-only transaction, with a
  pre-execution guard rejecting other statements. There were **zero writes**.
- The 30 dashboard-owned live relations and their dependent schema objects
  matched the independently replayed schema; live readiness remained healthy.
- Both installed active and rollback artifacts recognized the unchanged head.
- A built dashboard wheel contained the archive with its expected checksum,
  and an isolated resource import verified it. Ruff, diff checks and the
  analytics-boundary verifier passed.

The application process and immutable deployed artifacts were not replaced;
this repository change does not require a database transition or service
restart. No write/publication flags were changed.

[Installation, archive provenance and recovery commands](../../operations/migration-baseline.md)
are maintained with the source. Detailed local execution receipts and logs are
in `~/Documents/Codex/Research/2026-10-29-baseline-implementation/`.
