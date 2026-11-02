"""Read-only exact-contract coverage and independent RV reconciliation.

Run with the installed release interpreter. Does not calibrate or publish IV.
"""

import argparse
import json
import math
from pathlib import Path
import pandas as pd
from sqlalchemy import text
from dash_options.core.config import get_database_engine
from dash_options.shared.surfaces import load_surface_data
from dash_options.features.vol_surface.realized_comparison import load_price_sources, build_comparison
from options.market_data.realized_comparison import comparison_calendar


def verify(end, output):
    output.mkdir(parents=True, exist_ok=True)
    surface, source = load_surface_data()
    results, discrepancies = [], []
    for product in ("Brent", "HH", "TTF"):
        start, end_date, sessions, _ = comparison_calendar(product, end, 90)
        sources, errors = load_price_sources(product, sessions[0], end_date)
        assert not errors, errors
        raw = sources["Bloomberg PX_SETTLE"]
        current = surface.loc[
            surface.code.eq(product) & surface.cob_date.eq(end_date) & surface.option_expiration_date.gt(end_date)
        ]
        for contract in sorted(current.contract_date.unique()):
            contract = pd.Timestamp(contract)
            rows, meta = build_comparison(product, str(contract.date()), end, 90, surface, sources)
            selected = raw.loc[raw.contract_date.eq(contract)].copy()
            assert not selected.cob_date.duplicated().any()
            prices = selected.set_index("cob_date").settlement_price.to_dict()
            checks = 0
            for row in rows.itertuples():
                indices = [
                    i for i, d in enumerate(sessions) if row.cob_date - pd.Timedelta(days=30) < d <= row.cob_date
                ]
                complete = all(i > 0 and sessions[i] in prices and sessions[i - 1] in prices for i in indices)
                if not complete:
                    assert pd.isna(row.realized_volatility)
                    continue
                returns = [math.log(float(prices[sessions[i]]) / float(prices[sessions[i - 1]])) for i in indices]
                mean = sum(returns) / len(returns)
                expected = math.sqrt(252 * sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
                error = abs(expected - float(row.realized_volatility))
                if not math.isfinite(error) or error > 1e-12:
                    discrepancies.append(
                        {"product": product, "contract": str(contract.date()), "cob": str(row.cob_date), "error": error}
                    )
                checks += 1
            result = {
                "product": product,
                "contract": str(contract.date()),
                **meta["coverage"],
                "last_price": meta["last_price_date"],
                "price_source": meta["price_source"],
                "independent_rv_checks": checks,
            }
            results.append(result)
        print(product, "contracts", len(current.contract_date.unique()), "reconciled", flush=True)
    pd.DataFrame(results).to_csv(output / "history-coverage.csv", index=False)
    report = {
        "status": "passed" if not discrepancies else "failed",
        "cob": end,
        "contracts": len(results),
        "rv_checks": sum(r["independent_rv_checks"] for r in results),
        "discrepancies": discrepancies,
        "products": {
            p: {
                "contracts": sum(r["product"] == p for r in results),
                "full_rv_contracts": sum(r["product"] == p and r["rv_observed"] == r["sessions"] for r in results),
                "full_iv_contracts": sum(r["product"] == p and r["iv_observed"] == r["iv_expected"] for r in results),
            }
            for p in ("Brent", "HH", "TTF")
        },
        "coverage": results,
        "surface_source": source,
    }
    (output / "history-verification.json").write_text(json.dumps(report, default=str, indent=2))
    assert not discrepancies, discrepancies
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cob", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with get_database_engine().connect() as db:
        assert db.execute(text("SHOW default_transaction_read_only")).scalar() == "on", "Use PGOPTIONS read-only"
    result = verify(args.cob, args.output)
    print(json.dumps({k: v for k, v in result.items() if k not in ("coverage", "surface_source")}))
