# Options Dashboard

## Runtime and task-specific references

- [src/dash_options/routes.py](src/dash_options/routes.py) owns live pages and routes; `/` and `/greeks`
  use `dash_options.features.portfolio.greeks.layout`. [Procfile](Procfile) prewarms snapshots before serving
  `dash_options.wsgi:server`; startup may access configured sources.
- [src/dash_options/core/config.py](src/dash_options/core/config.py) resolves optional configuration files and
  environment overrides; `OPTIONS_CONFIG_PATH` selects an explicit file.
  [src/dash_options/core/source_status.py](src/dash_options/core/source_status.py) owns source freshness/alignment summaries.
- Calibration UI/service code lives in `src/dash_options/calibration/`; analytics remain in the
  separate `options` package. Read [docs/architecture/repository.md](docs/architecture/repository.md)
  when changing that boundary; the former standalone app is not the active server.
- New calibration calculations belong in `options.vol_calibration`; dashboard
  code calls its public `api.py` and owns presentation and execution. Existing
  compatibility adapters are temporary. Do not add numerical fitting, repair,
  source-eligibility policy, or arbitrage acceptance logic to Dash pages/components.
  Before releasing calibration changes, run the analytics repository's
  `tools/verification/check_vol_calibration_boundaries.py --dashboard <dashboard path>`
  against both this checkout and the staged artifact. Named compatibility
  bridges are temporary exceptions; pages and workers must use the public API.
  This import check does not replace function ownership review or operational
  calibration, persistence, export and browser verification.
- For deployment, schema, write/publication, or worker changes, read
  [docs/operations/deployment.md](docs/operations/deployment.md). Preserve independent feature flags and server-side
  identity/role checks; implementing a feature does not itself enable it in deployment.

## Current contracts

Preserve these unless the requested change explicitly changes the contract:

- Greeks aggregation includes the unit; do not combine unlike units. Exports retain
  maturity-axis alignment and distinguish missing values from zero. Existing layout
  choices, including the omitted standalone summary, are covered in
  `tests/portfolio/test_greeks_aspect.py` rather than a separate design prescription here.
- Snapshot consumers validate namespace and resolve the selected immutable snapshot;
  forcing a refresh must not change an already-issued reference's data. See
  `src/dash_options/core/snapshots.py` and `tests/core/test_snapshot_cache.py`.

## Verification

Install this checkout with `python -m pip install -e .` and use the project interpreter. Full suite: `python -m pytest`
(`pyproject.toml` selects `tests` and quiet output); select affected files for local
changes. Configuration/source-status checks are in `tests/core/test_runtime_config.py`
and `tests/core/test_source_status.py`; identity/role checks are in
`tests/calibration/test_vol_calibration_auth.py`. Use the release checks in `docs/operations/deployment.md`
when preparing a deployment, not as a prerequisite for every edit.
