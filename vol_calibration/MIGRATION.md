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

The analytics implementation remains in the separately managed `options`
package and is not duplicated here.
