# Vol Calibration migration

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
inside Vol Trades at `/brent_vol_history`. The `vol_calibration` package and
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
