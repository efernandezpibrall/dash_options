"""Per-browser-session state helpers for lazily mounted product workspaces."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd


def _normalized_cob_date(value) -> str | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def persist_product_table(state, product: str, cob_date, table_data):
    next_state = deepcopy(state) if isinstance(state, dict) else {}
    if table_data is None:
        return next_state
    next_state[product.lower()] = {
        "cob_date": _normalized_cob_date(cob_date),
        "table_data": deepcopy(table_data),
    }
    return next_state


def restore_product_table(state, product: str, cob_date):
    if not isinstance(state, dict):
        return None
    product_state = state.get(product.lower())
    if not isinstance(product_state, dict):
        return None
    if product_state.get("cob_date") != _normalized_cob_date(cob_date):
        return None
    table_data = product_state.get("table_data")
    return deepcopy(table_data) if isinstance(table_data, list) else None
