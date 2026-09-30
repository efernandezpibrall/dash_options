from io import StringIO

import numpy as np
import pandas as pd

from vol_calibration.data_cache import clear_workspace_load_cache
from vol_calibration.pages import brent
from pages import brent_vol_history


def test_brent_workspace_uses_pinned_vol_trades_snapshot(monkeypatch):
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

    def fake_snapshot(snapshot_id, **kwargs):
        calls.append((snapshot_id, kwargs))
        return pd.DataFrame()

    monkeypatch.setattr(brent_vol_history, "load_chain_snapshot", fake_snapshot)
    monkeypatch.setattr(
        brent_vol_history,
        "prepare_market_observations",
        lambda chain, *, product: market_data,
    )
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

    result = brent.load_data(
        "2026-07-27",
        0,
        {
            "market_product": "BRENT",
            "market_snapshot_id": "snapshot-123",
            "market_snapshot_kind": "SETTLEMENT",
            "market_as_of": "2026-07-27T18:00:00Z",
        },
    )

    loaded_market = pd.read_json(StringIO(result[0]), orient="split")
    assert calls == [("snapshot-123", {"product": "BRENT", "snapshot_kind": "SETTLEMENT"})]
    assert len(loaded_market) == 8
    assert result[4] is False
    assert result[6] is False
    assert "Bloomberg Immutable Snapshot" in result[3]
    assert "exact-COB official SVI" in result[3]


def test_brent_workspace_blocks_calibration_without_pinned_snapshot():
    clear_workspace_load_cache()
    result = brent.load_data("2026-07-27", 0)

    loaded_market = pd.read_json(StringIO(result[0]), orient="split")
    loaded_params = pd.read_json(StringIO(result[1]), orient="split")
    assert loaded_market.empty
    assert loaded_params.empty
    assert result[4] is True
    assert result[6] is True
    assert "Select a Vol Trades Brent snapshot" in result[5]
    assert "Unavailable" in str(result[2])
    assert "pinned Brent market snapshot" in result[3]
