# Vol Calibration migration

The contents of this package were imported into `dash_options` as a squashed
Git subtree and then adapted to run inside the existing Dash application.

- Source repository: `/Users/fernandezpibrall/Repositories/dash_vol_surface_calibration`
- Source commit: `638463d775324e7494eda8a65241714e85e0ddc9`
- Import destination: `vol_calibration/`
- Import policy: one-time migration; there is no ongoing subtree synchronization

The standalone Dash app, router, port 8056 server, and calibration navbar were
removed. The root `dash_options` app is the sole callback owner and server.

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
- `OPTIONS_TRUSTED_PROXY_AUTH_ENABLED=false`
- `TRINOS_VERIFY_SSL=true`

When trusted proxy authentication is eventually enabled, the proxy must
provide a server-verified user, roles, and shared secret. Roles are enforced by
server-side authorization helpers; UI visibility is not treated as
authorization. Database credentials remain external configuration and no
migration is applied during application import.

The analytics implementation remains in the separately managed `options`
package and is not duplicated here.
