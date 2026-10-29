# Architecture and ownership

The installable package is `dash_options`; the distribution name is `at-options-dashboard`. There are no root-module compatibility copies. Install the package and use its canonical imports.

## Dependency direction

`routes` assembles feature pages. Features use shared readers, core infrastructure and calibration workspaces. Calibration workspaces use execution services and the public `options.vol_calibration.api`. Analytics must not import Dash. Browser assets and required archive observations ship with the application package.

`core` owns configuration, source identity, database pool ownership, immutable snapshot storage and health. `shared` owns market/surface readers and reusable display components. A feature owns its page, callbacks, data projections and presentation. Different product or source-selection policies are explicit; visual similarity alone is not a reason to combine policies.

## One implementation per shared behavior

- `core/paths.py`: package resources, application root and configuration candidates.
- `core/code_identity.py`: execution-code fingerprinting shared by daily and automatic settlement workers.
- `core/workspace_cache.py`: reusable LRU/TTL implementation, adapted by calibration data caching.
- `features/vol_trades/intraday/jobs.py`: shared preview queue/cache/cancellation implementation. Brent and TTF retain separate one-worker pools, product namespaces and canonical analytics bindings.
- `features/vol_trades/intraday/controller.py`: authorization, preview submission/polling and candidate-window checks. Product workspaces retain their callback IDs and product-specific review/publication flows.
- `tests/helpers/`: shared component and publication helpers; tests do not depend on collecting another test module for helpers.

Three retained compatibility bridges still import analytics internals: `calibration/adapters/jkm_batch.py`, `calibration/adapters/ttf_batch.py`, and `calibration/publication.py`. They retain execution injection or authorized publication/cache adaptation. They contain no copied numerical engine. New callers use the public API. The analytics boundary verifier recognizes these exact paths and historical release paths.

## Runtime resources and migrations

`resources/volatility_history_archive/` is required fallback history, not disposable output. Its manifest checksums and observation identifiers remain unchanged. The active `migrations/` graph contains one frozen `20261029_03` baseline. The checksum-pinned original upgrade chain is packaged in `resources/migration_history/` for older databases and backups; see [baseline recovery](../operations/migration-baseline.md).

An installed wheel finds CSS, JavaScript and history inside its package. Set `OPTIONS_APP_ROOT` to the paired deployment directory containing `alembic.ini` and `migrations/`. A source checkout discovers that root automatically. Set `OPTIONS_CONFIG_PATH` for deployments instead of relying on working-directory configuration.

## Commands

Run repository tools as modules from the repository root, for example `python -m scripts.verification.verify_settlement_pipeline`. Worker commands are package modules and work from an installed environment. See the current operations runbooks for their exact names and authority requirements.

The previous partial-overlay release builders in the analytics repository describe dated flat-layout releases. Do not use them to combine packaged source with a flat baseline. Build and install the full dashboard wheel with the full paired migration/tool tree. The historical runtime remains a separate rollback artifact.
