"""Counterbalanced legacy/staged publication replay in owned temporary PostgreSQL.

No configured database is opened. A source directory is an explicit captured
input bundle, never a request to load today's market. Fits are outside the
publication timer; this measures publication plus exact canonical readback.
"""

import argparse
from datetime import datetime, timezone
from functools import partial
import json
import hashlib
import importlib.util
import os
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time
from uuid import uuid4

from sqlalchemy import text
from options.vol_calibration import api
from scripts.verification.verify_settlement_pipeline import temporary_postgres, apply_migrations, fixture_inputs
from dash_options.calibration.execution.fit_pool import prefit_observed_expiries, observed_fit_session, worker_budget
from dash_options.calibration.execution.daily_runner import worker_identity

PRODUCTS = ("BRENT", "HH", "TTF", "JKM", "NBP")

def verify_reference(candidate, product, reference_directory):
    reference = reference_directory / f"{product}_expected.npz"
    if reference.exists():
        import numpy as np
        frame = candidate["surface"].sort_values(["contract_date", "strike"]).reset_index(drop=True)
        with np.load(reference) as expected:
            for name in expected.files:
                np.testing.assert_allclose(frame[name].to_numpy(float), expected[name], rtol=0, atol=1e-12)


def candidates(source_directory, cache_directory=None, reference_directory=None, *, products=PRODUCTS):
    if source_directory is None:
        inputs = api.restore_daily_inputs(vars(fixture_inputs()))
        return [(inputs, api.build_daily_candidate(inputs))]
    values = []
    reference_directory = reference_directory or source_directory
    with observed_fit_session():
        for product in products:
            saved = api.unpack_settlement(json.loads((source_directory / f"{product}_inputs.json").read_text()))
            record = saved["inputs"]
            record["manifest"]["code_identity"] = api.implementation_fingerprint()
            record["identity"] = api.settlement_identity(record["manifest"])
            inputs = api.restore_daily_inputs(record)
            cache_key = product + "_" + inputs.identity
            cached = cache_directory / cache_key if cache_directory else None
            if cached and cached.with_suffix('.json').exists():
                metadata = json.loads(cached.with_suffix('.json').read_text())
                candidate = api.decode_daily_artifact({**metadata, "payload": cached.with_suffix('.zlib').read_bytes()})
                api.verify_daily_candidate(candidate)
                if candidate["source_identity"] != inputs.identity:
                    raise ValueError("Benchmark cache belongs to another source")
                verify_reference(candidate, product, reference_directory)
                values.append((inputs, candidate))
                continue
            checkpoints = {}
            checkpoint_file = source_directory / f"{product}_checkpoints.jsonl"
            if checkpoint_file.exists() and product in {"BRENT", "HH"}:
                for line in checkpoint_file.read_text().splitlines():
                    row = json.loads(line)
                    # Use the first accepted slices so the recorded final-density
                    # failure also exercises canonical bounded recovery.
                    checkpoints.setdefault(row["key"], api.unpack_settlement(row["value"]))
            import pandas as pd
            cache = {pd.Timestamp(k): api.restore_slice(v) for k, v in checkpoints.items()}
            fit_started = time.perf_counter()
            candidate = api.build_daily_candidate(inputs, base_publication=saved.get("base"), cached_slices=cache,
                observed_executor=partial(prefit_observed_expiries, environment_variable="DAILY_OBSERVED_FIT_WORKERS"))
            verify_reference(candidate, product, reference_directory)
            values.append((inputs, candidate))
            if cached:
                cache_directory.mkdir(parents=True, exist_ok=True)
                encoded = api.encode_daily_artifact(candidate)
                cached.with_suffix('.zlib').write_bytes(encoded.pop('payload'))
                cached.with_suffix('.json').write_text(json.dumps(encoded))
            print(json.dumps({"stage": "candidate_ready", "product": product,
                              "seconds": time.perf_counter() - fit_started}), flush=True)
    return values


def publication_variant(staged, baseline):
    if not staged and baseline is not None:
        return baseline.publish_hybrid_surface, baseline.load_latest_hybrid_publication
    return api.publish_hybrid_surface, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--products", nargs="+", choices=PRODUCTS, default=list(PRODUCTS),
                        help="Explicit replay subset; defaults to all five products")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=6)
    parser.add_argument("--cache-candidates", type=Path)
    parser.add_argument("--reference", type=Path, help="Explicit independently captured numerical baseline; defaults to the source bundle")
    parser.add_argument("--baseline-service", type=Path, help="Explicit frozen pre-change analytics service source")
    parser.add_argument("--reader-python", type=Path, help="Independently installed reader interpreter")
    parser.add_argument("--reader-dashboard", type=Path, help="Paired reader dashboard for Pricer verification")
    parser.add_argument("--readers-only", action="store_true", help="One staged consumer rehearsal; no performance claim")
    args = parser.parse_args()
    if args.pairs < 2 or args.pairs % 2:
        parser.error("--pairs must be an even integer of at least two")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    baseline = None
    if args.baseline_service:
        specification = importlib.util.spec_from_file_location(
            "options.vol_calibration.publication.benchmark_baseline", args.baseline_service)
        baseline = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(baseline)
    values = candidates(args.sources, args.cache_candidates, args.reference, products=args.products)
    manifest = {"captured_at": datetime.now(timezone.utc).isoformat(),
                "scope": "publication_and_canonical_readback", "fit_in_timer": False,
                "python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
                "cpu_count": os.cpu_count(), "workers": worker_budget("DAILY_OBSERVED_FIT_WORKERS"),
                "code_identity": api.implementation_fingerprint(), "worker_identity": worker_identity(),
                "sources": {i.product: i.identity for i, _ in values},
                "points": sum(len(c["surface"]) for _, c in values),
                "pairs": args.pairs, "transport": "owned_loopback_postgres", "samples": []}
    manifest["baseline_service_sha256"] = hashlib.sha256(args.baseline_service.read_bytes()).hexdigest() if baseline else None
    reference_directory = args.reference or args.sources
    manifest['reference_sha256'] = ({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in sorted(reference_directory.glob('*_expected.npz'))}
                                   if reference_directory else {})
    def save():
        args.output.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    with temporary_postgres() as engine:
        apply_migrations(engine)
        with engine.connect() as connection:
            manifest["postgres"] = connection.execute(text("SHOW server_version")).scalar_one()
        for pair in range(1 if args.readers_only else args.pairs):
            order = ((True,) if args.readers_only else
                     ((False, True) if pair % 2 == 0 else (True, False)))
            for staged in order:
                records = []
                started_at = datetime.now(timezone.utc).isoformat()
                started = time.perf_counter()
                for inputs, candidate in values:
                    current = api.load_publication_header(engine, inputs.cob, commodity=inputs.product, require_exact_cob=True)
                    publish, reader = publication_variant(staged, baseline)
                    with api.calibration_trace(records.append):
                        variant = {"stage_before_activation": True} if staged else {}
                        receipt = publish(engine, candidate["surface"], candidate["results"],
                            commodity=inputs.product, trading_date=inputs.cob, settlement_cob=inputs.cob,
                            actor_subject="benchmark", created_by="benchmark", base_publication_id=None,
                            expected_current_publication_id=current.get("publication_id"), idempotency_key=str(uuid4()),
                            expected_expiries=inputs.targets.contract_date, input_manifest=candidate["manifest"],
                            validation_certificate=candidate["certificate"], return_surface=False, **variant)
                        api.verify_daily_readback(engine, candidate, receipt["publication_id"], publication_reader=reader)
                        if staged:
                            api.release_verified_stage(engine, receipt["publication_id"], actor="benchmark")
                sample = {"pair": pair, "variant": "staged" if staged else "legacy", "started_at": started_at,
                          "seconds": time.perf_counter() - started, "events": records, "exact_parity": True}
                manifest["samples"].append(sample)
                save()
                print(json.dumps({k: v for k, v in sample.items() if k != "events"}), flush=True)
        if args.reader_python:
            import numpy as np
            with tempfile.TemporaryDirectory(prefix="daily-reader-probe-") as directory:
                products = []
                for inputs, candidate in values:
                    current = api.load_publication_header(engine, inputs.cob, commodity=inputs.product, require_exact_cob=True)
                    frame = candidate["surface"].sort_values(["contract_date", "strike"]).reset_index(drop=True)
                    expected = Path(directory) / (inputs.product + '.npz')
                    np.savez(expected, **{name: frame[name].to_numpy(float) for name in
                             ('strike','delta','volatility','total_variance','working_forward')})
                    products.append({"product": inputs.product, "cob": str(inputs.cob), "expected": str(expected),
                                     "publication_id": current['publication_id'],
                                     "warnings": candidate['certificate'].get('warnings', [])})
                probe_input = Path(directory) / 'input.json'
                probe_input.write_text(json.dumps({'database_url': engine.url.render_as_string(hide_password=False),
                                                   'products': products}))
                command = [str(args.reader_python), '-I', str(Path(__file__).with_name('probe_installed_daily_reader.py')),
                           '--input', str(probe_input)]
                if args.reader_dashboard:
                    command += ['--dashboard', str(args.reader_dashboard)]
                result = subprocess.run(command, capture_output=True, text=True)
                args.output.with_name(args.output.stem + '_reader_probe.json').write_text(json.dumps({
                    'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}, indent=2) + '\n')
                if result.returncode:
                    raise RuntimeError('Independent reader failed; see the saved reader probe receipt')
                manifest['independent_reader'] = json.loads(result.stdout)
    manifest['readers_only'] = args.readers_only
    if not args.readers_only:
        manifest["medians"] = {variant: statistics.median(s["seconds"] for s in manifest["samples"] if s["variant"] == variant)
                               for variant in ("legacy", "staged")}
    manifest["complete"] = True
    save()


if __name__ == "__main__":
    main()
