"""Dashboard source-provider and preview-format adapter for Brent candidates."""
from dataclasses import dataclass
from datetime import date
from typing import Any
import pandas as pd
from options.vol_calibration.api import build_brent_calibration_candidate
from vol_trades_data import read_brent_snapshot, read_preceding_brent_settlement, load_chain_snapshot


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
    result = build_brent_calibration_candidate(
        engine, context, snapshot_reader=read_brent_snapshot,
        preceding_settlement_reader=read_preceding_brent_settlement,
        intraday_chain_loader=load_chain_snapshot,
    )
    preview = result.preview.copy()
    preview['contract_date'] = preview['contract_date'].dt.date.astype(str)
    return BrentCandidate(
        result.surface, result.results, result.manifest, result.fingerprint,
        preview.to_dict('records'), result.diagnostics, result.settlement_cob,
    )
