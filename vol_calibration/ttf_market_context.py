"""Dashboard source/cache adapter and serialization for TTF trading context."""
import pandas as pd
from options.vol_calibration.api import resolve_ttf_trading_context
from options.vol_calibration.api import load_market_data_with_metadata

def load_ttf_trading_context(
    trading_date, *, refresh=False, snapshot_loader=None,
    market_loader=load_market_data_with_metadata,
):
    if snapshot_loader is None:
        from surface_data import get_operational_surface_snapshot
        snapshot_loader = get_operational_surface_snapshot
    return resolve_ttf_trading_context(
        trading_date, refresh=refresh, snapshot_loader=snapshot_loader,
        market_loader=market_loader,
    )


def serialize_ttf_trading_context(context: dict) -> dict:
    """Return a Dash-store-safe context without duplicating DataFrames."""
    market_data = context.get("market_data")
    payload = {
        key: value
        for key, value in context.items()
        if key not in {"market_data", "surface_snapshot", "last_update"}
    }
    payload["last_update"] = (
        pd.Timestamp(context["last_update"]).isoformat()
        if context.get("last_update") is not None
        and not pd.isna(context.get("last_update"))
        else None
    )
    payload["market_row_count"] = (
        int(len(market_data)) if isinstance(market_data, pd.DataFrame) else 0
    )
    return payload
