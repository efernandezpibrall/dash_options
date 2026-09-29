"""Pinned-source Brent candidate for the Vol Trades single-surface workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from options.brent_single_surface import (
    BRENT_SINGLE_SURFACE_POLICY_VERSION,
    calibrate_brent_intraday_chain,
    calibrate_brent_settlement_snapshot,
)
from options.build_brent_vol_surface import horizon_from_as_of
from vol_calibration.ttf_publication import ttf_surface_fingerprint


@dataclass(frozen=True)
class BrentCandidate:
    surface: pd.DataFrame
    results: list[dict]
    manifest: dict[str, Any]
    fingerprint: str
    preview: list[dict]
    diagnostics: dict[str, Any]
    settlement_cob: date


def active_brent_publication(engine, trading_date) -> dict[str, Any]:
    """Read only the active exact-COB revision identity, without its dense grid."""
    with engine.connect() as connection:
        row = connection.execute(
            text("""
                SELECT publication_id, cob_date AS publication_date, published_at
                FROM at_lng.vol_surface_publications
                WHERE commodity = 'BRENT' AND status = 'published' AND is_active
                  AND cob_date = :cob_date
                ORDER BY published_at DESC, created_at DESC
                LIMIT 1
            """),
            {"cob_date": pd.Timestamp(trading_date).date()},
        ).mappings().one_or_none()
    if row is None:
        return {"publication_id": None, "publication_date": None, "published_at": None}
    return {
        "publication_id": str(row["publication_id"]),
        "publication_date": pd.Timestamp(row["publication_date"]).date().isoformat(),
        "published_at": pd.Timestamp(row["published_at"]).isoformat(),
    }


def _snapshot(engine, snapshot_id: str, kind: str) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text("""
                SELECT snapshot_id, business_date, observed_at, input_fingerprint
                FROM at_lng.vol_market_snapshots
                WHERE snapshot_id = CAST(:snapshot_id AS uuid)
                  AND commodity = 'BRENT' AND status = 'complete'
                  AND COALESCE(metadata ->> 'snapshot_kind', 'SETTLEMENT') = :kind
            """),
            {"snapshot_id": snapshot_id, "kind": kind},
        ).mappings().one_or_none()
    if row is None:
        raise ValueError(f"Pinned Brent {kind.lower()} snapshot is unavailable.")
    return dict(row)


def _preceding_settlement(engine, trading_date: date, as_of) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text("""
                SELECT snapshot_id, business_date, observed_at, input_fingerprint
                FROM at_lng.vol_market_snapshots
                WHERE commodity = 'BRENT' AND status = 'complete'
                  AND COALESCE(metadata ->> 'snapshot_kind', 'SETTLEMENT') = 'SETTLEMENT'
                  AND business_date <= :trading_date AND observed_at <= :as_of
                ORDER BY business_date DESC, observed_at DESC, created_at DESC,
                         snapshot_id DESC
                LIMIT 1
            """),
            {"trading_date": trading_date, "as_of": as_of},
        ).mappings().one_or_none()
    if row is None:
        raise ValueError("No point-in-time Brent settlement precedes this intraday snapshot.")
    return dict(row)


def build_brent_candidate(engine, context: dict[str, Any]) -> BrentCandidate:
    """Recompute only from immutable database snapshots, never browser quote data."""
    if context.get("market_product") != "BRENT":
        raise ValueError("Brent candidate requires a Brent Vol Trades selection.")
    trading_date = date.fromisoformat(str(context["cob_date"]))
    selected_id = str(context["market_snapshot_id"])
    kind = str(context["market_snapshot_kind"]).upper()
    if kind not in {"SETTLEMENT", "INTRADAY"}:
        raise ValueError("Select a Brent settlement or intraday snapshot.")
    selected = _snapshot(engine, selected_id, kind)
    if pd.Timestamp(selected["business_date"]).date() != trading_date:
        raise ValueError("The selected Brent snapshot no longer matches the market date.")
    selected_as_of = pd.Timestamp(selected["observed_at"], tz="UTC") if pd.Timestamp(selected["observed_at"]).tzinfo is None else pd.Timestamp(selected["observed_at"]).tz_convert("UTC")
    if selected_as_of != pd.Timestamp(context["market_as_of"]).tz_convert("UTC"):
        raise ValueError("The selected Brent snapshot no longer matches the market as-of.")

    anchor = selected if kind == "SETTLEMENT" else _preceding_settlement(
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
        from pages.brent_vol_history import load_chain_snapshot

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
