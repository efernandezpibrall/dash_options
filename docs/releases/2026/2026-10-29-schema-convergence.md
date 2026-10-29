# Schema convergence application — 29 October 2026

Production was upgraded from `20261021_01` and `20261028_01` to the single head
`20261029_03`. Alembic executed `20261029_02`, `20261029_01`, then `20261029_03`
with PostgreSQL transactional DDL and bounded lock/statement timeouts.

All five retired Brent history tables were already absent and remained absent.
The three `live_forward_curve_*` tables were created empty. Existing relations
retained their identities; active settlement and intraday publication pointers
were unchanged. Repeating `upgrade head` produced no database changes.

## Runtime compatibility

The deployed analytics wheel does not contain `options.live_forward_curves`.
Its import in `20261029_01` now occurs only inside `upgrade()`: the migration
executor still uses the same table definitions, while existing readers can load
the revision graph and validate readiness without the newer curve writer.

The existing application, analytics wheel, dependencies and launcher flags were
preserved. Two missing migration definitions were added to new immutable copies
of the current and rollback releases under
`~/.local/share/brent-surface-release/`:

| Purpose | Release | Manifest revision |
| --- | --- | --- |
| Active | `1.4.4rc4-schema-convergence-20261029` | `989376457c5847e90d356ab6f6f166a462edae8d2e14ce86e387dd8718ce6c47` |
| Rollback | `1.4.4rc4-schema-convergence-rollback-20261029` | `d4afef50324c03a21c1f381be4f81ff108b8dc81bb9e7d35c49b3d63b4fe9750` |

The active-release pointer now names these schema-compatible launchers. Use the
recorded rollback artifact: the original releases do not recognize the new head.
Database downgrade remains blocked by the retained migration safeguards.

## Verification and cleanup

- All 20 migration upgrade-path tests passed, including older-reader graph
  loading without the Live Prices writer.
- Both staged releases passed file, installed analytics and dependency identity
  checks, plus future-head readiness-lineage checks.
- A separate read-only HTTP preview matched the current layout, all 239 callback
  definitions and readiness flags. Live readback after migration matched them.
- `/health/ready` returned ready with `20261029_03`; repeat upgrade was unchanged.
- Every one of the 27 revision files is in the head's ancestry. All three SQL
  files are consumed by historical revisions. The environment and revision
  template remain operational inputs. No migration source file is obsolete.
- Generated migration bytecode caches were removed from the source checkout.

Execution receipts, per-file retention reasons, staging identities and preview
results are in the local research directory
`~/Documents/Codex/Research/2026-10-29-migration-application/`.
