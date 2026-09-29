"""Exact, ordered checkpoints for long gas calibration batches.

The caller owns the immutable whole-batch input snapshot.  These per-expiry
fingerprints additionally guard against reusing a result after a changed row
or a changed chronological warm start.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd


class StaleCalibrationCheckpoint(ValueError):
    def __init__(self, expiry: str):
        self.expiry = expiry
        super().__init__(f"Calibration checkpoint for {expiry} is stale.")


class CalibrationCancelled(RuntimeError):
    """Raised at an expiry boundary when a batch has been cancelled."""


def canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if hasattr(value, "item"):
        try:
            return canonical(value.item())
        except (AttributeError, TypeError, ValueError):
            pass
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")
    ).hexdigest()


def expiry_input_fingerprint(
    market_data: pd.DataFrame,
    expiry,
    table_row: dict | None,
    *,
    policy: str,
    skip_good: bool,
    node_edits: Any = None,
) -> str:
    raw = market_data.loc[market_data["expiry"] == expiry]
    return digest({
        "expiry": pd.Timestamp(expiry).isoformat(),
        "observations": raw.to_json(
            orient="split", date_format="iso", double_precision=15
        ),
        "editable_row": table_row,
        "policy": policy,
        "skip_good": skip_good,
        "node_edits": node_edits,
    })


def make_checkpoint(
    *, expiry: str, result_row: dict, updated_row: dict | None,
    warm_start_after: dict | None, input_fingerprint: str,
    dependency_fingerprint: str,
) -> dict:
    body = canonical({
        "expiry": expiry,
        "result_row": result_row,
        "updated_row": updated_row,
        "warm_start_after": warm_start_after,
        "input_fingerprint": input_fingerprint,
        "dependency_fingerprint": dependency_fingerprint,
    })
    return {**body, "output_fingerprint": digest(body)}


def verified_checkpoint(
    checkpoint: dict, *, expiry: str, input_fingerprint: str,
    dependency_fingerprint: str,
) -> dict:
    if not isinstance(checkpoint, dict):
        raise StaleCalibrationCheckpoint(expiry)
    body = {key: value for key, value in checkpoint.items() if key != "output_fingerprint"}
    if (
        checkpoint.get("expiry") != expiry
        or checkpoint.get("input_fingerprint") != input_fingerprint
        or checkpoint.get("dependency_fingerprint") != dependency_fingerprint
        or checkpoint.get("output_fingerprint") != digest(body)
        or not isinstance(checkpoint.get("result_row"), dict)
        or checkpoint["result_row"].get("status") not in {"Success", "Skipped"}
        or not isinstance(checkpoint.get("updated_row"), dict)
        or not isinstance(checkpoint.get("warm_start_after"), dict)
    ):
        raise StaleCalibrationCheckpoint(expiry)
    return checkpoint
