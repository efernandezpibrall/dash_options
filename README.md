# Options dashboard

Dash application for portfolio risk, pricing, live markets, volatility surfaces
and governed calibration. The main application code is in `src/dash_options/`.
Shared pricing models and numerical calibration policy belong to the separate
`options` analytics package.

## Repository structure

All maintained folders are shown below, with the main entry files:

```text
dash_options/
├── src/
│   └── dash_options/              Main application package
│       ├── bootstrap.py           Creates Dash/Flask and configures HTTP delivery
│       ├── routes.py              Navigation, page registration and redirects
│       ├── wsgi.py                Production server entrypoint
│       ├── prewarm.py             Explicit snapshot prewarming
│       ├── core/                  Configuration, database access, caches and health
│       ├── shared/                Shared market/surface readers and display helpers
│       ├── features/              Dashboard code grouped by business feature
│       │   ├── portfolio/         Greeks, valuations, trades, scenarios and P&L
│       │   ├── prices/            Historical underlying prices
│       │   ├── live_prices/       Live Prices page and data service
│       │   ├── correlations/      Correlation analysis
│       │   ├── pricer/            Structure inputs, pricing callbacks and charts
│       │   ├── vol_surface/       Published surfaces and historical comparisons
│       │   └── vol_trades/        Snapshots, trade tape and calibration handoff
│       │       ├── ice_quotes/    ICE quote readers, filters and workbench
│       │       └── intraday/      Brent/TTF previews, jobs and publication controls
│       ├── calibration/          Calibration coordination, permissions and publication
│       │   ├── workspaces/        Product workflows: Brent, HH, JKM and TTF
│       │   ├── components/        Calibration tables, modals and reusable UI
│       │   ├── adapters/          Translates UI inputs and analytics results
│       │   └── execution/         Jobs, workers, leases, checkpoints and daily runs
│       ├── assets/               CSS and browser JavaScript
│       └── resources/            Required files distributed with the application
│           ├── migration_history/         Archived upgrade path for older databases
│           └── volatility_history_archive/ Historical observations and manifest
├── migrations/                    Current database installation/upgrade logic
│   ├── env.py                     Connection, transaction and prerequisite checks
│   ├── script.py.mako             Template for future migrations
│   └── versions/
│       └── 20261029_03_baseline.py Single frozen baseline
├── scripts/                       Explicit operational commands
│   ├── audit/                     Read-only diagnostics
│   ├── maintenance/               Legacy-schema upgrades and history retirement
│   └── verification/              Database rehearsals, source replay and benchmarks
├── tests/                         Automated tests grouped by responsibility
│   ├── core/                      Configuration, caching, routing and health
│   ├── portfolio/                 Portfolio risk, valuation and trade ledger
│   ├── pricer/                    Pricing interface and structure behavior
│   ├── live_prices/               Live-price readers and presentation
│   ├── correlations/              Correlation functionality
│   ├── vol_surface/               Surface displays and historical comparisons
│   ├── vol_trades/                Market refresh, quotes and intraday workflows
│   ├── calibration/               Calibration, workers and publication
│   ├── migrations/                Baseline, archived upgrades and database safeguards
│   ├── helpers/                   Reusable testing utilities
│   └── fixtures/
│       └── calibration/           Fixed inputs for reproducible tests
├── docs/
│   ├── architecture/              Code ownership, dependencies and cleanup decisions
│   ├── operations/                Installation, deployment, migrations and daily runs
│   ├── user/                      Trader guides
│   ├── releases/
│   │   └── 2026/                  Dated implementation and verification records
│   └── archive/
│       ├── cleanup/               Historical cleanup inventories
│       └── retired-pages/         Documentation for retired functionality
├── README.md                      Repository overview and startup instructions
├── AGENTS.md                      Project-specific contribution instructions
├── Procfile                       Production startup command
├── pyproject.toml                 Package, test and lint configuration
├── requirements.txt               Application dependencies
├── requirements-dev.txt           Development dependencies
├── alembic.ini                    Alembic configuration
└── .gitignore                     Generated/local files excluded from Git
```

## Where to find the main code

| Task | Starting point |
| --- | --- |
| Navigation or page registration | [`routes.py`](src/dash_options/routes.py) |
| Portfolio Greeks or valuations | [`portfolio/greeks.py`](src/dash_options/features/portfolio/greeks.py), [`valuation.py`](src/dash_options/features/portfolio/valuation.py) |
| Pricer interface and calculation flow | [`pricer/page.py`](src/dash_options/features/pricer/page.py), [`calculation_callbacks.py`](src/dash_options/features/pricer/calculation_callbacks.py) |
| Live Prices data loading | [`live_prices/service.py`](src/dash_options/features/live_prices/service.py) |
| Volatility screens | [`vol_surface/page.py`](src/dash_options/features/vol_surface/page.py), [`vol_trades/page.py`](src/dash_options/features/vol_trades/page.py) |
| Product calibration workflows | [`calibration/workspaces/`](src/dash_options/calibration/workspaces) |
| Automated settlement processing | [`execution/settlement_worker.py`](src/dash_options/calibration/execution/settlement_worker.py) |
| Database configuration and snapshots | [`core/config.py`](src/dash_options/core/config.py), [`snapshots.py`](src/dash_options/core/snapshots.py) |
| Database baseline and older-checkpoint recovery | [`migrations/`](migrations), [`upgrade_legacy_schema.py`](scripts/maintenance/upgrade_legacy_schema.py) |

Within most feature folders, `page.py` is the page entrypoint, callback modules
handle interactions, and `data.py` or `service.py` handles data access. Portfolio
pages use descriptive filenames such as `greeks.py` and `valuation.py`.

New numerical calibration and acceptance rules belong in
`options.vol_calibration`; Dash calls its public API. Shared source/cache
behavior belongs in `core/` or `shared/`, with tests in the corresponding group.

## Install for local development

Use a dedicated environment containing the reviewed `options` analytics package
and ICEchat adapter. The analytics package is maintained separately; do not
install an unrelated public package named `options`.

From the repository root, with that environment active:

```sh
python -m pip install -e .
python -m pip install -r requirements-dev.txt
```

Installation is needed when setting up an environment. An existing editable
installation already follows changes to this checkout.

## Start the dashboard from this checkout

With the project environment active, run from the repository root:

```sh
export OPTIONS_CONFIG_PATH=/absolute/path/to/config.ini
PYTHONDONTWRITEBYTECODE=1 DASH_PORT=8072 python -m dash_options.routes
```

Open **http://127.0.0.1:8072**. Stop the local server with **Ctrl+C**.
Port `8072` lets this source checkout run alongside a deployed dashboard on
`8071`. Choose another available port with `DASH_PORT`; without that variable,
the application defaults to `8071`. Debug mode is off unless `DASH_DEBUG=true`.

Configuration and credentials remain external. This command uses the selected
configuration's authentication and feature flags; it does not reproduce flags
exported only by a deployed release's launcher. Follow
[deployment instructions](docs/operations/deployment.md) for production or
publication-enabled operation. Starting the source checkout does not replace
the separately deployed immutable release.

Production uses `dash_options.wsgi:server`, which initializes callbacks before
threaded requests. Snapshot prewarming is a separate command and can access
configured sources:

```sh
python -m dash_options.prewarm
python -m gunicorn -w 1 -k gthread --threads 8 -b 127.0.0.1:8071 --timeout 180 dash_options.wsgi:server
```

Use an available bind address/port. The root `Procfile` provides a configurable
production startup command.

## Database migrations

The active graph contains one frozen baseline, `20261029_03`. A database already
at that checkpoint needs no restamping or schema transition. Fresh dashboard
installations require the externally owned shared trade/catalogue schema first.
The original migration chain is retained in a checksum-verified packaged archive
for older databases and backups. See
[baseline installation and recovery](docs/operations/migration-baseline.md).

## Tests and generated files

Tests block live PostgreSQL connections and use disposable local clusters for
persistence coverage. To run without recreating repository bytecode or pytest
caches:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider
ruff check --no-cache .
```

`build/`, `dist/`, `*.egg-info/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`
and Finder's `.DS_Store` files are generated and ignored by Git. Builds, installs
and tools may recreate them; they are not application source. Keep the `.git/`
repository metadata, the project environment, both packaged resource archives
and any actively used runtime snapshot cache. Generated exports, secrets and
one-off trade valuations belong outside this repository.

See [architecture and ownership](docs/architecture/repository.md),
[daily settlement workflow](docs/operations/daily-settlements.md), and the
[baseline verification record](docs/releases/2026/2026-10-29-single-baseline.md).
Dated release records describe their own artifacts and do not identify the
currently running release.
