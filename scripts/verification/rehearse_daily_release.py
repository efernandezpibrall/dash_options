"""Rehearse additive migrations, all-product publication and application rollback.

Only an owned temporary PostgreSQL is opened. Shared-schema prerequisites are
synthetic; all dashboard migrations run through Alembic without exclusions. Source bundles and the installed rollback release are
explicit paths, so the rehearsal does not depend on a workstation layout.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from alembic import command
from alembic.script import ScriptDirectory
import numpy as np
from sqlalchemy import text
from options.vol_calibration import api

from scripts.verification.benchmark_daily_publication import candidates
from scripts.verification.verify_settlement_pipeline import (
    ROOT,
    temporary_postgres,
    prepare_external_schema_fixture,
)

from scripts.maintenance.upgrade_legacy_schema import (
    CHECKPOINT,
    archived_migrations,
    migration_config,
    upgrade_legacy,
)


def rehearse_schema(engine, baseline_head=CHECKPOINT):
    """Exercise fresh installation or recovery from an archived checkpoint."""
    with engine.begin() as connection:
        prepare_external_schema_fixture(connection)
        if baseline_head != CHECKPOINT:
            with archived_migrations() as legacy:
                config = migration_config(connection, legacy=legacy)
                if ScriptDirectory.from_config(config).get_revision(baseline_head) is None:
                    raise ValueError("A recorded archived revision is required")
                command.upgrade(config, baseline_head)
            transition = upgrade_legacy(connection, apply=True)
        else:
            transition = {"before": [], "after": [CHECKPOINT], "archive_used": False}
        config = migration_config(connection)
        command.upgrade(config, "head")
        heads = ScriptDirectory.from_config(config).get_heads()
        actual = sorted(connection.execute(text("SELECT version_num FROM at_lng.alembic_version")).scalars())
        assert actual == sorted(heads)
    return {
        "baseline_head": baseline_head,
        "heads": actual,
        "transition": transition,
        "all_dashboard_migrations_included": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--cache-candidates", type=Path)
    parser.add_argument("--reference", type=Path, help="Independent numeric reference for all five products")
    parser.add_argument("--baseline-head", default=CHECKPOINT)
    parser.add_argument("--rollback-release", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rollback = args.rollback_release.absolute()
    reader_python = rollback / ("venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python")
    health_paths = ("src/dash_options/core/health.py", "health.py")
    if not reader_python.is_file() or not any((rollback / "dashboard" / name).is_file() for name in health_paths):
        parser.error("An installed paired rollback release is required")
    report = {
        "scope": __doc__,
        "rollback_release": str(rollback),
        "production_changed": False,
        "products": [],
        "complete": False,
    }
    values = candidates(args.sources, args.cache_candidates, args.reference)
    with temporary_postgres() as engine:
        report["schema"] = rehearse_schema(engine, args.baseline_head)
        heads = report["schema"]["heads"]
        assert api.historical_content_snapshot(engine) == {}
        with tempfile.TemporaryDirectory(prefix="daily-rollback-") as directory:
            products = []
            for inputs, candidate in values:
                receipt = api.publish_hybrid_surface(
                    engine,
                    candidate["surface"],
                    candidate["results"],
                    commodity=inputs.product,
                    trading_date=inputs.cob,
                    settlement_cob=inputs.cob,
                    actor_subject="rollback-rehearsal",
                    created_by="rollback-rehearsal",
                    base_publication_id=None,
                    expected_current_publication_id=None,
                    idempotency_key=str(uuid4()),
                    expected_expiries=inputs.targets.contract_date,
                    input_manifest=candidate["manifest"],
                    validation_certificate=candidate["certificate"],
                    return_surface=False,
                    stage_before_activation=True,
                )
                verified = api.verify_daily_readback(engine, candidate, receipt["publication_id"])
                assert api.release_verified_stage(engine, receipt["publication_id"], actor="rollback-rehearsal")
                frame = candidate["surface"].sort_values(["contract_date", "strike"]).reset_index(drop=True)
                expected = Path(directory) / (inputs.product + ".npz")
                np.savez(
                    expected,
                    **{
                        name: frame[name].to_numpy(float)
                        for name in ("strike", "delta", "volatility", "total_variance", "working_forward")
                    },
                )
                products.append(
                    {
                        "product": inputs.product,
                        "cob": str(inputs.cob),
                        "expected": str(expected),
                        "publication_id": receipt["publication_id"],
                        "warnings": candidate["certificate"].get("warnings", []),
                    }
                )
                report["products"].append({"product": inputs.product, "points": verified["point_count"]})
            before = api.historical_content_snapshot(engine)
            path = Path(directory) / "probe.json"
            path.write_text(
                json.dumps(
                    {
                        "database_url": engine.url.render_as_string(hide_password=False),
                        "migration_heads": sorted(heads),
                        "products": products,
                    }
                )
            )
            result = subprocess.run(
                [
                    str(reader_python),
                    "-I",
                    str(ROOT / "scripts/verification/probe_installed_daily_reader.py"),
                    "--input",
                    str(path),
                    "--dashboard",
                    str(rollback / "dashboard"),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise RuntimeError("Rollback reader/schema compatibility probe failed")
            report["rollback_reader_pricer_schema"] = json.loads(result.stdout)
            assert api.historical_content_snapshot(engine) == before
            report["historical_content_unchanged_by_reader_rollback"] = True
    report["complete"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
