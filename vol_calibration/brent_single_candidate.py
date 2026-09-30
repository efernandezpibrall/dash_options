"""Pinned-source Brent candidate for the Vol Trades single-surface workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from options.brent_single_surface import (
    BRENT_SINGLE_SURFACE_POLICY_VERSION,
    calibrate_brent_intraday_chain,
    calibrate_brent_settlement_snapshot,
)
from options.build_brent_vol_surface import horizon_from_as_of
from vol_calibration.ttf_publication import ttf_surface_fingerprint
from vol_trades_data import read_brent_snapshot, read_preceding_brent_settlement


@dataclass(frozen=True)
class BrentCandidate:
    surface: pd.DataFrame
    results: list[dict]
    manifest: dict[str, Any]
    fingerprint: str
    preview: list[dict]
    diagnostics: dict[str, Any]
    settlement_cob: date


def build_brent_candidate(engine, context: dict[str, Any]) -> BrentCandidate:
    """Recompute only from immutable database snapshots, never browser quote data."""
    if context.get("market_product") != "BRENT":
        raise ValueError("Brent candidate requires a Brent Vol Trades selection.")
    trading_date = date.fromisoformat(str(context["cob_date"]))
    selected_id = str(context["market_snapshot_id"])
    kind = str(context["market_snapshot_kind"]).upper()
    if kind not in {"SETTLEMENT", "INTRADAY"}:
        raise ValueError("Select a Brent settlement or intraday snapshot.")
    selected = read_brent_snapshot(engine, selected_id, kind)
    if pd.Timestamp(selected["business_date"]).date() != trading_date:
        raise ValueError("The selected Brent snapshot no longer matches the market date.")
    selected_as_of = pd.Timestamp(selected["observed_at"], tz="UTC") if pd.Timestamp(selected["observed_at"]).tzinfo is None else pd.Timestamp(selected["observed_at"]).tz_convert("UTC")
    if selected_as_of != pd.Timestamp(context["market_as_of"]).tz_convert("UTC"):
        raise ValueError("The selected Brent snapshot no longer matches the market as-of.")

    anchor = selected if kind == "SETTLEMENT" else read_preceding_brent_settlement(
        engine, trading_date, selected_as_of.to_pydatetime()
    )
    settlement_cob = pd.Timestamp(anchor["business_date"]).date()
    model, settlement_diagnostics = calibrate_brent_settlement_snapshot(
        engine,
        settlement_cob.isoformat(),
        str(anchor["snapshot_id"]),
        horizon_date=horizon_from_as_of(trading_date),
        allow_pending_open_interest=True,
    )
    intraday_diagnostics: dict[str, Any] = {}
    intraday_contract_dates: list[str] = []
    if kind == "INTRADAY":
        from vol_trades_data import load_chain_snapshot

        model = model.roll_to(trading_date)
        chain = load_chain_snapshot(
            selected_id, engine, product="BRENT", snapshot_kind="INTRADAY"
        )
        if chain.empty or chain["snapshot_id"].astype(str).nunique() != 1:
            raise ValueError("The pinned Brent intraday chain is empty or mixed.")
        model, intraday_diagnostics = calibrate_brent_intraday_chain(model, chain)
        if intraday_diagnostics.get("updated_expiries", 0) == 0:
            raise ValueError(
                "No intraday expiry improved the accepted settlement fit; "
                "there is no new intraday candidate to publish."
            )
        intraday_contract_dates = intraday_diagnostics["updated_contract_dates"]

    source_name = (
        f"Bloomberg Brent settlement={anchor['snapshot_id']}"
        + (f";intraday={selected_id}" if kind == "INTRADAY" else "")
    )
    surface, results = model.publication_candidate(
        source_name, intraday_contract_dates=intraday_contract_dates
    )
    surface["cob_date"] = pd.Timestamp(trading_date)
    fingerprint = ttf_surface_fingerprint(surface, commodity="BRENT")
    preview = pd.concat(
        (
            model.sample_deltas(row.contract_date, np.linspace(0.05, 0.95, 37))
            for row in model.targets.itertuples()
        ),
        ignore_index=True,
    )
    preview["contract_date"] = preview["contract_date"].dt.date.astype(str)
    preview_rows = preview[["contract_date", "strike", "delta", "volatility"]].to_dict("records")
    sources = [{
        "kind": "SETTLEMENT",
        "snapshot_id": str(anchor["snapshot_id"]),
        "business_date": settlement_cob.isoformat(),
        "observed_at": pd.Timestamp(anchor["observed_at"]).isoformat(),
        "input_fingerprint": str(anchor["input_fingerprint"]),
        "price_status": settlement_diagnostics["price_status"],
        "activity_status": settlement_diagnostics["activity_status"],
    }]
    if kind == "INTRADAY":
        sources.append({
            "kind": "INTRADAY",
            "snapshot_id": selected_id,
            "business_date": trading_date.isoformat(),
            "observed_at": selected_as_of.isoformat(),
            "input_fingerprint": str(selected["input_fingerprint"]),
        })
    manifest = {
        "commodity": "BRENT",
        "cob_date": trading_date.isoformat(),
        "settlement_cob": settlement_cob.isoformat(),
        "source_snapshots": sources,
        "model_version": BRENT_SINGLE_SURFACE_POLICY_VERSION,
        "policy_version": BRENT_SINGLE_SURFACE_POLICY_VERSION,
        "surface_fingerprint": fingerprint,
        "intraday_updated_contract_dates": intraday_contract_dates,
        "manual_trade_ids": [],
    }
    diagnostics = {
        "validation": model.validate(),
        "settlement": settlement_diagnostics,
        "intraday": intraday_diagnostics,
        "source_kind": kind,
        "observed_expiries": len(model.slices),
        "target_expiries": len(model.targets),
    }
    return BrentCandidate(
        surface, results, manifest, fingerprint, preview_rows, diagnostics,
        settlement_cob,
    )
