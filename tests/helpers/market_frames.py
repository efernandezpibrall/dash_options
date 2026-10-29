"""Shared synthetic chain and calibrated-surface fixtures."""
import pandas as pd
from dash_options.features.vol_trades import data as market_data


def _chain_frame():
    rows = []
    for security, put_call, strike, settlement, volume, open_interest, iv in (
        ("COZ6P 70 Comdty", "P", 70.0, 1.25, 20, 250, 0.31),
        ("COZ6C 90 Comdty", "C", 90.0, 1.10, 0, 180, 0.29),
        ("COZ6P 50 Comdty", "P", 50.0, None, None, 10, None),
    ):
        rows.append(
            {
                "snapshot_id": "00000000-0000-0000-0000-000000000001",
                "product": "BRENT",
                "business_date": "2026-08-10",
                "observed_at": "2026-08-10T12:00:00Z",
                "discovery_method": "OPT_CHAIN",
                "bloomberg_description": "Brent option",
                "underlying_type": "FUTURE",
                "underlying_security": "COZ6 Comdty",
                "underlying_global_id": "BBG-FUTURE",
                "underlying_contract_month": "2026-12-01",
                "underlying_last_tradeable_date": "2026-10-30",
                "underlying_price": 80.0,
                "option_security": security,
                "option_global_id": f"BBG-{security}",
                "put_call": put_call,
                "strike": strike,
                "option_expiration_date": "2026-10-27",
                "option_last_tradeable_date": "2026-10-27",
                "option_style": "AMERICAN",
                "premium_style": "FUTURES_STYLE",
                "exchange_code": "ICE",
                "currency": "USD",
                "price_unit": "USD/BBL",
                "contract_multiplier": 1000,
                "settlement_price": settlement,
                "volume": volume,
                "open_interest": open_interest,
                "implied_volatility": iv,
                "pricing_model": "american_on_futures_futures_style",
                "pricing_model_version": "american_futures_crr_margin_style_v2",
                "iv_status": "resolved" if iv is not None else "missing_settlement",
                "iv_exclusion_reason": None if iv is not None else "missing settlement",
                "ingested_at": "2026-08-10T12:00:00Z",
                "input_fingerprint": "abc123",
                "source_name": "Bloomberg option-chain settlement",
                "source_revision": "v1",
                "snapshot_metadata": {},
            }
        )
    return market_data._normalize_chain_frame(pd.DataFrame(rows))

def _calibrated_frame():
    frame = pd.DataFrame(
        {
            "contract_date": pd.to_datetime(["2026-12-01"] * 3),
            "option_expiration_date": pd.to_datetime(["2026-10-27"] * 3),
            "strike": [70.0, 80.0, 90.0],
            "delta": [0.75, 0.50, 0.25],
            "put_call": ["C", "C", "C"],
            "volatility": [0.32, 0.30, 0.28],
            "total_variance": [vol**2 * 64 / 365.25 for vol in (0.32, 0.30, 0.28)],
            "working_forward": [80.0, 80.0, 80.0],
            "forward_value": [80.0, 80.0, 80.0],
            "source_name": ["TTF PCHIP/Wing"] * 3,
            "calibration_basis": ["observed"] * 3,
            "created_at": pd.to_datetime(["2026-08-25T09:33:19Z"] * 3),
            "publication_id": ["9572ae7f-90ca-4c8f-aecd-fbaff1ed081d"] * 3,
            "run_id": ["1fb16e4f-d1c7-4bc9-9e32-6dc4899ebf9a"] * 3,
            "commodity": ["TTF"] * 3,
            "publication_cob_date": pd.to_datetime(["2026-08-24"] * 3),
            "published_at": pd.to_datetime(["2026-08-25T09:33:19Z"] * 3),
            "published_by": ["publisher"] * 3,
        }
    )
    frame.attrs["publication_status"] = "available"
    frame.attrs["publication_metadata"] = {
        "publication_id": "9572ae7f-90ca-4c8f-aecd-fbaff1ed081d",
        "run_id": "1fb16e4f-d1c7-4bc9-9e32-6dc4899ebf9a",
        "commodity": "TTF",
        "cob_date": pd.Timestamp("2026-08-24"),
        "published_at": pd.Timestamp("2026-08-25T09:33:19Z"),
        "published_by": "publisher",
    }
    return frame
