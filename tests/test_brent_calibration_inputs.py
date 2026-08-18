from io import StringIO

import numpy as np
import pandas as pd

from vol_calibration.data_cache import clear_workspace_load_cache
from vol_calibration.pages import brent


def test_brent_page_requests_observed_data_and_enables_calibration(monkeypatch):
    clear_workspace_load_cache()
    calls = []
    expiry = pd.Timestamp("2026-09-01")
    market_data = pd.DataFrame({
        "expiry": expiry,
        "dte": 31.0,
        "delta": [-0.40, -0.30, -0.20, -0.10, 0.10, 0.20, 0.30, 0.40],
        "iv": [0.32, 0.31, 0.30, 0.29, 0.29, 0.30, 0.31, 0.32],
        "strike": np.linspace(78.0, 98.0, 8),
        "forward": 88.36,
        "weight": 100.0,
        "calibration_eligible": True,
        "exclusion_reason": "",
    })
    surface = pd.DataFrame({
        "cob_date": pd.Timestamp("2026-07-27"),
        "code": "Brent",
        "contract_date": expiry,
        "option_expiration_date": pd.Timestamp("2026-08-27"),
        "delta": np.linspace(0.01, 0.99, 11),
        "delta_abs": np.linspace(0.01, 0.99, 11),
        "put_call": "call",
        "volatility": np.linspace(0.34, 0.30, 11),
        "delta_bucket": "node",
        "delta_sort_key": np.arange(11),
        "delta_pct": np.linspace(1, 99, 11),
    })

    def fake_loader(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "data": market_data,
            "source": "postgres",
            "is_synthetic": False,
            "last_update": None,
            "message": "Loaded observed Brent option settlements",
            "error": None,
            "calibration_mode": "intraday_snapshot",
            "provenance_complete": True,
        }

    monkeypatch.setattr(brent, "load_market_data_with_metadata", fake_loader)
    monkeypatch.setattr(
        brent,
        "load_operational_surface_payload",
        lambda *args, **kwargs: {
            "data": surface.to_json(date_format="iso", orient="split"),
            "requested_cob": "2026-07-27",
            "actual_cob": "2026-07-27",
            "source": "test",
        },
    )

    result = brent.load_data("2026-07-27", 0)

    loaded_market = pd.read_json(StringIO(result[0]), orient="split")
    assert calls[0][1]["allow_synthetic_fallback"] is False
    assert len(loaded_market) == 8
    assert result[4] is False
    assert result[6] is False
    assert "PostgreSQL" in str(result[2])
    assert "exact-COB official SVI" in result[3]


def test_brent_page_blocks_calibration_when_observed_data_is_unavailable(monkeypatch):
    clear_workspace_load_cache()
    monkeypatch.setattr(
        brent,
        "load_market_data_with_metadata",
        lambda *args, **kwargs: {
            "data": pd.DataFrame(),
            "source": "unavailable",
            "is_synthetic": False,
            "last_update": None,
            "message": "No eligible BRENT market data for 2026-07-27",
            "error": "physical source schema mismatch",
        },
    )

    result = brent.load_data("2026-07-27", 0)

    loaded_market = pd.read_json(StringIO(result[0]), orient="split")
    loaded_params = pd.read_json(StringIO(result[1]), orient="split")
    assert loaded_market.empty
    assert loaded_params.empty
    assert result[4] is True
    assert result[6] is True
    assert "No eligible BRENT market data for 2026-07-27" in result[5]
    assert "Unavailable" in str(result[2])
    assert "physical source schema mismatch" in result[3]
