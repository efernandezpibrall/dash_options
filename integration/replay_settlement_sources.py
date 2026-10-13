"""Read-only historical source replay; never submits jobs or publications."""
import argparse
from pathlib import Path
import json
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import event, text
from options.vol_calibration import api
from runtime_config import get_database_engine


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-cob", required=True)
    parser.add_argument("--through-cob", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--save-candidates", action="store_true")
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    engine = get_database_engine()
    @event.listens_for(engine, "checkout")
    def readonly(connection, *_):
        connection.set_session(readonly=True)
    frozen_code = api.implementation_fingerprint()
    def active():
        with engine.connect() as connection:
            return [dict(row) for row in connection.execute(text("""SELECT commodity,cob_date,publication_id
                FROM at_lng.vol_surface_publications WHERE is_active AND commodity IN ('BRENT','HH')
                ORDER BY commodity,cob_date""")).mappings()]
    before = active()
    with engine.connect() as connection:
        sources = connection.execute(text("""SELECT DISTINCT ON (commodity,business_date)
            commodity,business_date,snapshot_id FROM at_lng.vol_market_snapshots
            WHERE commodity IN ('BRENT','LNE') AND status='complete'
            AND business_date BETWEEN :start AND :end
            AND COALESCE(metadata->>'snapshot_kind','SETTLEMENT')='SETTLEMENT'
            ORDER BY commodity,business_date,observed_at DESC,created_at DESC,snapshot_id DESC"""),
            {"start": args.from_cob, "end": args.through_cob}).mappings().all()
    report = []
    for source in sources:
        if api.implementation_fingerprint() != frozen_code:
            raise RuntimeError("Implementation changed during replay; rerun under a frozen artifact")
        start = time.monotonic()
        product = "HH" if source["commodity"] == "LNE" else "BRENT"
        result = {"product": product, "cob": source["business_date"].isoformat(), "snapshot_id": str(source["snapshot_id"])}
        try:
            inputs = api.load_settlement_inputs(engine, product, source["business_date"], str(source["snapshot_id"]))
            candidate = api.build_settlement_candidate(inputs)
            encoded = api.pack_settlement(candidate)
            restored = api.unpack_settlement(json.loads(json.dumps(encoded, allow_nan=False)))
            api.verify_settlement_certificate(restored["surface"], restored["results"], restored["manifest"], restored["certificate"])
            result.update(status="accepted", points=len(candidate["surface"]), expiries=len(candidate["results"]),
                metrics=candidate["certificate"]["metrics"], certificate_identity=candidate["certificate"]["certificate_identity"])
            if args.save_candidates:
                (args.output_dir / f"{product}_{result['cob']}_candidate.json").write_text(json.dumps(encoded, allow_nan=False))
        except (ValueError, RuntimeError) as exc:
            result.update(status="held", error=f"{type(exc).__name__}: {exc}", diagnostics=getattr(exc, "diagnostics", {}))
        result["seconds"] = round(time.monotonic() - start, 3)
        report.append(result)
        (args.output_dir / "historical_replay.json").write_text(json.dumps({"code_identity": frozen_code, "results": report}, indent=2))
        print(result, flush=True)
    after = active()
    if before != after:
        raise RuntimeError("Active publications changed during replay; inspect concurrent publication activity")
    (args.output_dir / "live_publications_unchanged.json").write_text(json.dumps({"verified": True, "before": before, "after": after}, default=str, indent=2))


if __name__ == "__main__":
    main()
