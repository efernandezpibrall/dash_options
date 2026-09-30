# Options Dashboard

## Runtime and task-specific references

- [index_options.py](index_options.py) owns live pages and routes; `/` and `/greeks`
  use `pages.greeks.layout`. [Procfile](Procfile) prewarms snapshots before serving
  `index_options:server`; startup may access configured sources.
- [runtime_config.py](runtime_config.py) resolves optional configuration files and
  environment overrides; `OPTIONS_CONFIG_PATH` selects an explicit file.
  [source_status.py](source_status.py) owns source freshness/alignment summaries.
- Calibration UI/service code lives in `vol_calibration/`; analytics remain in the
  separate `options` package. Read [vol_calibration/MIGRATION.md](vol_calibration/MIGRATION.md)
  when changing that boundary; the former standalone app is not the active server.
- For deployment, schema, write/publication, or worker changes, read
  [DEPLOYMENT.md](DEPLOYMENT.md). Preserve independent feature flags and server-side
  identity/role checks; implementing a feature does not itself enable it in deployment.

## Current contracts

Preserve these unless the requested change explicitly changes the contract:

- Greeks aggregation includes the unit; do not combine unlike units. Exports retain
  maturity-axis alignment and distinguish missing values from zero. Existing layout
  choices, including the omitted standalone summary, are covered in
  `tests/test_greeks_aspect.py` rather than a separate design prescription here.
- Snapshot consumers validate namespace and resolve the selected immutable snapshot;
  forcing a refresh must not change an already-issued reference's data. See
  `snapshot_cache.py` and `tests/test_snapshot_cache.py`.

## Verification

From this checkout, use the project interpreter. Full suite: `python -m pytest`
(`pyproject.toml` selects `tests` and quiet output); select affected files for local
changes. Configuration/source-status checks are in `tests/test_runtime_config.py`
and `tests/test_source_status.py`; identity/role checks are in
`tests/test_vol_calibration_auth.py`. Use the release checks in `DEPLOYMENT.md`
when preparing a deployment, not as a prerequisite for every edit.
