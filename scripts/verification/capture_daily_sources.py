"""Capture exact prepared daily inputs for offline parity and performance checks.

All source reads use the runner's repeatable-read, read-only pinning transaction.
No jobs, candidates or publications are written to the configured database.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from options.vol_calibration import api
from dash_options.core.config import get_database_engine
from dash_options.calibration.execution.daily_runner import PRODUCTS, _pin_inputs


def capture_base_seed(engine, publication_ids, output):
    """Export only the immutable preceding publications, under READ ONLY."""
    from psycopg2 import sql
    from sqlalchemy import text
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema": 1, "publications": sorted(publication_ids), "tables": []}
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout='120s'"))
            run_ids = [str(value) for value in connection.execute(text(
                "SELECT run_id FROM at_lng.vol_surface_publications WHERE publication_id=ANY(CAST(:ids AS uuid[]))"
            ), {"ids": list(publication_ids)}).scalars()]
            if len(run_ids) != len(publication_ids):
                raise ValueError("A pinned preceding publication is unavailable")
            for table, key, ids in (
                ("vol_calibration_runs", "run_id", run_ids),
                ("vol_calibration_expiry_results", "run_id", run_ids),
                ("vol_surface_publications", "publication_id", publication_ids),
                ("implied_volatility_surface_calibrated", "publication_id", publication_ids),
            ):
                columns = list(connection.execute(text("""SELECT column_name FROM information_schema.columns
                    WHERE table_schema='at_lng' AND table_name=:table ORDER BY ordinal_position"""),
                    {"table": table}).scalars())
                if not columns:
                    raise ValueError("Publication table is unavailable")
                query = sql.SQL("COPY (SELECT {} FROM {} WHERE {}=ANY(%s::uuid[])) TO STDOUT WITH (FORMAT BINARY)").format(
                    sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier("at_lng", table), sql.Identifier(key))
                path = output / (table + ".bin")
                with connection.connection.driver_connection.cursor() as cursor, path.open("wb") as stream:
                    cursor.copy_expert(cursor.mogrify(query, (list(ids),)).decode(), stream)
                manifest["tables"].append(dict(table=table, columns=columns, file=path.name,
                                                sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cob", required=True)
    parser.add_argument("--products", nargs="+", choices=PRODUCTS, default=list(PRODUCTS))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-base", action="store_true", help="Also capture preceding publications for owned-database workflow replay")
    args = parser.parse_args()
    from datetime import date
    cob = date.fromisoformat(args.cob)
    args.output.mkdir(parents=True, exist_ok=False)
    engine = get_database_engine()
    manifest = {"schema": 1, "cob": args.cob, "captured_at": datetime.now(timezone.utc).isoformat(),
                "implementation": api.implementation_fingerprint(), "products": {}, "complete": False}
    try:
        base_ids = set()
        for product in args.products:
            with api.calibration_stage(product, "source_capture"):
                inputs, base, current = _pin_inputs(engine, product, cob)
            if base.get("publication_id"):
                base_ids.add(base["publication_id"])
            raw = json.dumps(api.pack_settlement({"inputs": api.daily_input_record(inputs), "base": base,
                                                  "expected_current_publication_id": current.get("publication_id")}),
                             sort_keys=True, allow_nan=False).encode()
            name = product + "_inputs.json"
            (args.output / name).write_bytes(raw)
            manifest["products"][product] = dict(file=name, sha256=hashlib.sha256(raw).hexdigest(),
                                                  source_identity=inputs.identity, expiries=len(inputs.targets))
            print(json.dumps({"product": product, "expiries": len(inputs.targets), "captured": True}), flush=True)
        if args.seed_base:
            capture_base_seed(engine, base_ids, args.output / "base_seed")
        manifest["complete"] = True
    finally:
        (args.output / "capture_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        engine.dispose()


if __name__ == "__main__":
    main()
