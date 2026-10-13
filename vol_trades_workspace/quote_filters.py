"""Shared quote filtering for the tape and expiry-chart overlays."""

from __future__ import annotations

import pandas as pd

import ice_quote_data as quote_data


def filter_quote_rows(
    rows: list[dict],
    *,
    product=None,
    contract=None,
    option_type=None,
    strike=None,
    sender=None,
    source_channel=None,
    status=None,
    positive_only=False,
) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if product is not None:
        selected_product = quote_data._selected_product(product)
        if selected_product not in quote_data.QUOTE_FEED_PRODUCTS:
            return frame.iloc[0:0].copy()
        codes = frame.get("product_code", pd.Series("B", index=frame.index))
        frame = frame.loc[codes.map(quote_data._row_product).eq(selected_product)].copy()
    if contract:
        frame = frame[frame["contract_label"].astype(str) == str(contract)]
    if option_type:
        frame = frame[frame["option_filter_key"] == option_type]
    if strike is not None:
        frame = frame[frame["strike_filter_key"] == str(strike)]
    if sender:
        frame = frame[frame["sender_handle"] == sender]
    if source_channel:
        frame = frame[frame["source_channel"] == source_channel]
    if status:
        frame = frame[frame["processing_status"] == status]
    if positive_only:
        frame = frame[frame["edge_actionable_now"].eq(True)]
    return frame.sort_values("observed_at", ascending=False)
