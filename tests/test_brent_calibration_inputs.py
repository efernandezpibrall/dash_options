from io import StringIO

import pandas as pd

from vol_calibration.data_cache import clear_workspace_load_cache
from vol_calibration.pages import brent


def test_brent_page_requests_observed_data_and_enables_calibration(monkeypatch):
    clear_workspace_load_cache()
    calls = []
    market_data = pd.DataFrame(
        {
            "expiry": pd.to_datetime(["2026-09-01", "2026-09-01"]),
            "dte": [1, 1],
            "delta": [-0.25, 0.25],
            "iv": [0.30, 0.28],
            "strike": [85.0, 91.0],
            "forward": [88.36, 88.36],
        }
    )

    def fake_loader(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "data": market_data,
            "source": "postgres",
            "is_synthetic": False,
            "last_update": None,
            "message": "Loaded observed Brent option settlements",
            "error": None,
        }

    monkeypatch.setattr(brent, "load_market_data_with_metadata", fake_loader)
    monkeypatch.setattr(brent, "get_database_engine", lambda: None)
    monkeypatch.setattr(brent, "evaluate_fit", lambda **kwargs: {"rmse": 0.01})

    result = brent.load_data("2026-07-27", 0)

    loaded_market = pd.read_json(StringIO(result[0]), orient="split")
    assert calls[0][1]["allow_synthetic_fallback"] is False
    assert len(loaded_market) == 2
    assert result[4] is False
    assert result[6] is False
    assert "PostgreSQL" in str(result[2])
    assert "Params: Defaults" in result[3]


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
