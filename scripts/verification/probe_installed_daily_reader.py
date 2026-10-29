"""Independent installed-reader/optional Pricer probe for an owned replay database."""

import argparse
import json
from pathlib import Path
import sys
import os
from importlib import import_module
from io import StringIO

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from options.vol_calibration import api


def dashboard_module(dashboard, packaged_name, legacy_name):
    """Load the selected paired artifact, including releases before packaging."""
    dashboard = dashboard.resolve()
    packaged = (dashboard / "src/dash_options").is_dir()
    sys.path.insert(0, str(dashboard / "src" if packaged else dashboard))
    os.environ["OPTIONS_APP_ROOT"] = str(dashboard)
    module = import_module(packaged_name if packaged else legacy_name)
    if not Path(module.__file__).resolve().is_relative_to(dashboard):
        raise ValueError("Dashboard reader was loaded outside the paired artifact")
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--dashboard", type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    url = make_url(data["database_url"])
    if url.host != "127.0.0.1" or url.username != "settlement_test" or url.database != "postgres":
        raise ValueError("Reader probe accepts only an owned replay database")
    if not Path(api.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError("Reader must be imported from its installed virtual environment")
    engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on -c statement_timeout=90000"})
    results = []
    schema = None
    try:
        if data.get('migration_heads') or data.get('migration_head'):
            if not args.dashboard:
                raise ValueError('Migration compatibility requires the paired reader dashboard')
            health = dashboard_module(args.dashboard, "dash_options.core.health", "health")
            with engine.connect() as connection:
                actual_heads = sorted(connection.execute(text('SELECT version_num FROM at_lng.alembic_version')).scalars())
            expected_heads = data.get('migration_heads') or [data['migration_head']]
            assert actual_heads == sorted(expected_heads)
            for required in ('20260928_01', '20260902_02', *expected_heads):
                assert any(health._migration_lineage_contains(head, required) for head in actual_heads)
            schema = {'heads':actual_heads,'rollback_application_recognizes_lineage':True}
        for item in data["products"]:
            saved = api.load_latest_hybrid_publication(engine, item["cob"], commodity=item["product"],
                                                      require_exact_cob=True, force_refresh=True)
            assert saved["publication_id"] == item["publication_id"]
            # This public split-JSON contract predates the frame convenience API.
            # Exercise legacy installed readers without importing private modules.
            frame = pd.read_json(StringIO(saved["data"]), orient="split")
            frame = frame.sort_values(["contract_date", "strike"]).reset_index(drop=True)
            errors = {}
            with np.load(item["expected"]) as expected:
                for name in expected.files:
                    actual = frame[name].to_numpy(float)
                    np.testing.assert_allclose(actual, expected[name], rtol=0, atol=5.1e-11)
                    errors[name] = float(np.max(np.abs(actual - expected[name])))
            warnings = [w for row in saved["expiry_results"] for w in row.get("validation", {}).get("warnings", [])
                        if w.get("code") == "mean_quote_error_exceeded"]
            assert sorted(warnings, key=lambda w: w["contract_date"]) == sorted(item["warnings"], key=lambda w: w["contract_date"])
            result = {"product": item["product"], "publication_id": item["publication_id"], "points": len(frame),
                      "warning_metadata_verified": True, "maximum_errors": errors}
            if args.dashboard:
                pricer = dashboard_module(args.dashboard,
                    "dash_options.features.pricer.surface_reference", "pricer_surface_reference")
                months = sorted(frame.contract_date.unique())
                reference = pricer.load_published_surface_slice(item["product"], item["cob"],
                    [months[0], months[len(months) // 2], months[-1]], force_refresh=True, engine=engine)
                assert reference["publication_id"] == item["publication_id"]
                result["pricer_same_revision"] = True
            results.append(result)
    finally:
        engine.dispose()
    print(json.dumps({"reader_api": api.__file__, "products": results, 'schema_compatibility':schema}), flush=True)


if __name__ == "__main__":
    main()
