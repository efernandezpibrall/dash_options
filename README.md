# Options dashboard

Dash application for portfolio risk, pricing, live markets, volatility surfaces and governed calibration. Shared pricing models and calibration policy belong to the separate `options` analytics package.

## Repository map

```text
src/dash_options/
  bootstrap.py                 Dash/Flask creation and HTTP delivery
  routes.py                    Navigation, page registry and redirects
  wsgi.py                      Production server entrypoint
  prewarm.py                   Explicit snapshot prewarming
  core/                        Configuration, database access, identity, caches, health
  shared/                      Market/surface readers and shared presentation
  features/
    portfolio/                 Greeks, valuation, P&L, scenarios and trade ledger
    prices/                    Historical prices
    live_prices/               Live prices page and data service
    correlations/              Correlation analysis
    pricer/                    Structure inputs, pricing, charts, grids and callbacks
    vol_surface/               Published surfaces, delta history and realized comparison
    vol_trades/                Market snapshots, charts, trade tape and calibration handoff
      ice_quotes/              ICE quote readers and surface workbench
      intraday/                Brent/TTF previews and explicit publication controls
  calibration/
    workspaces/                Product workflows and callback orchestration
    components/                Reusable calibration UI
    adapters/                  Table/result translation and execution adapters
    execution/                 Jobs, leases, workers, checkpoints and daily runs
  assets/                      CSS and browser JavaScript, with existing load order
  resources/                   Versioned, required historical observations
migrations/                    Complete additive Alembic history and referenced SQL
scripts/
  verification/                Owned-database tests, replay and release verification
  audit/                       Read-only diagnostic commands
  maintenance/                 Explicitly invoked data-maintenance commands
tests/                         Grouped by feature; helpers and fixtures are shared
docs/
  architecture/                Ownership, dependencies and cleanup decisions
  operations/                  Current setup, serving and daily settlement commands
  user/                        Trader guides
  releases/                    Dated implementation/release records
  archive/                     Retired methodology and historical cleanup records
```

## Local development

Use a dedicated environment and install the paired `options` analytics package and ICEchat adapter before installing this application. The analytics package is maintained separately and is not fetched from an unrelated public package named `options`.

```sh
python -m pip install -e .
python -m pip install -r requirements-dev.txt
python -m pytest
python -m dash_options.routes
```

Configuration is external. `OPTIONS_CONFIG_PATH` selects a configuration file; environment overrides remain supported. See [operations/deployment.md](docs/operations/deployment.md) before using real sources, workers or publication. Tests block live PostgreSQL connections and use disposable local clusters for persistence coverage.

Production entrypoint: `dash_options.wsgi:server`. Prewarming is a separate explicit command, `python -m dash_options.prewarm`, which may access configured sources. The root `Procfile` composes these commands.

## Where to make changes

- Start a page change in its `features/<name>/` directory; `routes.py` registers routes.
- Put shared source/cache/authentication behavior in `core/` or `shared/`, not in a copied page helper.
- Keep numerical calibration and acceptance rules in `options.vol_calibration`; call its public API from Dash.
- Add operational commands to the matching `scripts/` group and tests to the owning feature group.
- Keep generated exports, caches, local secrets and one-off trade valuations outside this repository.

See [architecture and ownership](docs/architecture/repository.md), [cleanup record](docs/architecture/cleanup-2026-10-29.md), and [daily settlement workflow](docs/operations/daily-settlements.md). Archived records describe their own dates; they do not identify the currently running release.
