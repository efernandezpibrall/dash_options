"""Grids for the Vol Trades workspace."""

from __future__ import annotations

from vol_trades_workspace import chart_data

from typing import Any
import pandas as pd
import vol_trades_data as market_data


_GRID_PRICE_4DP = {
    "function": "params.value == null ? '—' : Number(params.value).toFixed(4)"
}


_GRID_PRICE_3DP = {
    "function": "params.value == null ? '—' : Number(params.value).toFixed(3)"
}


_GRID_IV_2DP = {
    "function": "params.value == null ? '—' : Number(params.value).toFixed(2)"
}


_GRID_PERCENT_2DP = {
    "function": (
        "params.value == null ? '—' : "
        "(100 * Number(params.value)).toFixed(2)"
    )
}


_GRID_INTEGER = {
    "function": (
        "params.value == null ? '—' : "
        "Number(params.value).toLocaleString('en-GB', {maximumFractionDigits: 0})"
    )
}


_GRID_DATE = {
    "function": (
        "params.value == null ? '—' : "
        "new Intl.DateTimeFormat('en-GB', {day: '2-digit', month: 'short', "
        "year: '2-digit', timeZone: 'UTC'}).format("
        "new Date(String(params.value).slice(0, 10) + 'T00:00:00Z'))"
    )
}


_GRID_DATETIME_GST = {
    "function": (
        "params.value == null ? '—' : "
        "new Intl.DateTimeFormat('en-GB', {day: '2-digit', month: 'short', "
        "hour: '2-digit', minute: '2-digit', second: '2-digit', "
        "hour12: false, timeZone: 'Asia/Dubai'}).format(new Date(params.value))"
    )
}


_GRID_SIDE_RULES = {
    "vol-trades-call-cell": "params.value === 'C'",
    "vol-trades-put-cell": "params.value === 'P'",
}


_GRID_STATUS_RULES = {
    "vol-trades-status-ok": (
        "['resolved', 'eligible', 'same_day', 'settlement'].includes("
        "String(params.value || '').toLowerCase())"
    ),
    "vol-trades-status-warning": (
        "!['', 'resolved', 'eligible', 'same_day', 'settlement'].includes("
        "String(params.value || '').toLowerCase())"
    ),
}



def _detail_rows(chain: pd.DataFrame, expiry_value: str | None) -> list[dict[str, Any]]:
    if chain is None or chain.empty or not expiry_value:
        return []
    expiry = pd.Timestamp(expiry_value).normalize()
    selected = chain.loc[chart_data._expiry_mask(chain, expiry, "underlying_contract_month")].copy()
    if selected.empty:
        return []
    prepared = market_data.prepare_market_observations(selected)
    eligibility: dict[float, tuple[bool, str]] = {}
    if not prepared.empty:
        for strike, group in prepared.groupby("strike"):
            is_eligible = bool(group["calibration_eligible"].fillna(False).any())
            reasons = ";".join(
                dict.fromkeys(
                    reason
                    for reason in group["exclusion_reason"].fillna("").astype(str)
                    if reason
                )
            )
            eligibility[float(strike)] = (is_eligible, reasons)
    rows = []
    for row in selected.itertuples(index=False):
        eligible, smile_reason = eligibility.get(float(row.strike), (False, ""))
        rows.append(
            {
                "option_security": row.option_security,
                "option_global_id": row.option_global_id,
                "native_option_underlier": getattr(
                    row, "underlying_security", None
                ),
                "pricing_future": getattr(
                    row, "pricing_underlying_security", None
                ),
                "finance_rate_pct": (
                    None
                    if market_data._numeric_or_none(
                        getattr(row, "pricing_discount_rate", None)
                    ) is None
                    else 100.0 * float(row.pricing_discount_rate)
                ),
                "finance_rate_observed_at": (
                    None
                    if pd.isna(
                        getattr(row, "pricing_discount_rate_observed_at", None)
                    )
                    else pd.Timestamp(
                        getattr(row, "pricing_discount_rate_observed_at")
                    ).isoformat()
                ),
                "put_call": row.put_call,
                "strike": market_data._numeric_or_none(row.strike),
                "settlement_price": market_data._numeric_or_none(row.settlement_price),
                "last_price": market_data._numeric_or_none(getattr(row, "last_price", None)),
                "option_bid": market_data._numeric_or_none(getattr(row, "option_bid", None)),
                "option_mid": market_data._numeric_or_none(getattr(row, "option_mid", None)),
                "option_ask": market_data._numeric_or_none(getattr(row, "option_ask", None)),
                "option_spread": market_data._numeric_or_none(getattr(row, "option_spread", None)),
                "option_spread_pct": market_data._numeric_or_none(getattr(row, "option_spread_pct", None)),
                "underlying_bid": market_data._numeric_or_none(getattr(row, "underlying_bid", None)),
                "underlying_mid": market_data._numeric_or_none(getattr(row, "underlying_mid", None)),
                "underlying_ask": market_data._numeric_or_none(getattr(row, "underlying_ask", None)),
                "underlying_spread": market_data._numeric_or_none(getattr(row, "underlying_spread", None)),
                "quote_batch_id": market_data._numeric_or_none(getattr(row, "quote_batch_id", None)),
                "quote_capture_skew_ms": market_data._numeric_or_none(getattr(row, "quote_capture_skew_ms", None)),
                "quote_request_started_at": (
                    None if pd.isna(getattr(row, "quote_request_started_at", None))
                    else pd.Timestamp(getattr(row, "quote_request_started_at")).isoformat()
                ),
                "quote_response_at": (
                    None if pd.isna(getattr(row, "quote_response_at", None))
                    else pd.Timestamp(getattr(row, "quote_response_at")).isoformat()
                ),
                "executable_iv_bid_pct": (
                    None if market_data._numeric_or_none(getattr(row, "executable_iv_bid", None)) is None
                    else 100.0 * float(row.executable_iv_bid)
                ),
                "executable_iv_mid_pct": (
                    None if market_data._numeric_or_none(getattr(row, "executable_iv_mid", None)) is None
                    else 100.0 * float(row.executable_iv_mid)
                ),
                "executable_iv_ask_pct": (
                    None if market_data._numeric_or_none(getattr(row, "executable_iv_ask", None)) is None
                    else 100.0 * float(row.executable_iv_ask)
                ),
                "executable_iv_status": getattr(row, "executable_iv_status", None),
                "executable_iv_exclusion_reason": getattr(
                    row, "executable_iv_exclusion_reason", None
                ),
                "last_trade_date": (
                    None
                    if pd.isna(getattr(row, "last_trade_date", None))
                    else pd.Timestamp(getattr(row, "last_trade_date")).date().isoformat()
                ),
                "open_interest_date": (
                    None
                    if pd.isna(getattr(row, "open_interest_date", None))
                    else pd.Timestamp(getattr(row, "open_interest_date")).date().isoformat()
                ),
                "settlement_open_interest": market_data._numeric_or_none(
                    getattr(row, "settlement_open_interest", None)
                ),
                "settlement_open_interest_date": (
                    None
                    if pd.isna(
                        getattr(row, "settlement_open_interest_date", None)
                    )
                    else pd.Timestamp(
                        getattr(row, "settlement_open_interest_date")
                    ).date().isoformat()
                ),
                "intraday_open_interest": market_data._numeric_or_none(
                    getattr(row, "intraday_open_interest", None)
                ),
                "intraday_open_interest_date": (
                    None
                    if pd.isna(
                        getattr(row, "intraday_open_interest_date", None)
                    )
                    else pd.Timestamp(
                        getattr(row, "intraday_open_interest_date")
                    ).date().isoformat()
                ),
                "implied_volatility_pct": (
                    None
                    if market_data._numeric_or_none(row.implied_volatility) is None
                    else 100.0 * float(row.implied_volatility)
                ),
                "volume": market_data._numeric_or_none(row.volume),
                "volume_scope_status": getattr(
                    row, "volume_scope_status", "settlement"
                ),
                "volume_delta": market_data._numeric_or_none(getattr(row, "volume_delta", None)),
                "volume_delta_status": getattr(
                    row, "volume_delta_status", "not_applicable"
                ),
                "open_interest": market_data._numeric_or_none(row.open_interest),
                "open_interest_scope_status": getattr(
                    row, "open_interest_scope_status", "settlement"
                ),
                "open_interest_source": getattr(
                    row, "open_interest_source", None
                ),
                "underlying_price": market_data._numeric_or_none(row.underlying_price),
                "last_trade_price_exact": market_data._numeric_or_none(getattr(row, "last_trade_price", None)),
                "last_trade_at_gst": (
                    None if pd.isna(getattr(row, "last_trade_at", None))
                    else pd.Timestamp(getattr(row, "last_trade_at")).tz_convert("Asia/Dubai").isoformat()
                ),
                "last_trade_underlying_price": market_data._numeric_or_none(
                    getattr(row, "last_trade_underlying_price", None)
                ),
                "last_trade_underlying_at_gst": (
                    None if pd.isna(getattr(row, "last_trade_underlying_at", None))
                    else pd.Timestamp(getattr(row, "last_trade_underlying_at")).tz_convert("Asia/Dubai").isoformat()
                ),
                "last_trade_underlying_source": chart_data._trade_match_source_label(
                    getattr(row, "last_trade_underlying_source", None)
                ),
                "last_trade_match_lag_ms": market_data._numeric_or_none(
                    getattr(row, "last_trade_match_lag_ms", None)
                ),
                "last_trade_condition_codes": getattr(
                    row, "last_trade_condition_codes", None
                ),
                "last_trade_iv_pct": (
                    None if market_data._numeric_or_none(getattr(row, "last_trade_iv", None)) is None
                    else 100.0 * float(row.last_trade_iv)
                ),
                "last_trade_iv_status": getattr(row, "last_trade_iv_status", None),
                "last_trade_iv_exclusion_reason": getattr(
                    row, "last_trade_iv_exclusion_reason", None
                ),
                "last_trade_reused": (
                    "Yes"
                    if getattr(row, "last_trade_match_source_snapshot_id", None)
                    else "No"
                ),
                "option_expiration_date": (
                    None
                    if pd.isna(row.option_expiration_date)
                    else pd.Timestamp(row.option_expiration_date).date().isoformat()
                ),
                "discovery_method": row.discovery_method,
                "iv_status": row.iv_status,
                "smile_eligible": "Yes" if eligible else "No",
                "exclusion_reason": row.iv_exclusion_reason or smile_reason or None,
            }
        )
    return rows


def _grid_column(
    header: str,
    field: str,
    *,
    width: int | None = None,
    min_width: int | None = None,
    flex: int | None = None,
    pinned: str | None = None,
    formatter: dict[str, str] | None = None,
    numeric: bool = False,
    group_show: str | None = None,
    cell_class: str | None = None,
    cell_rules: dict[str, str] | None = None,
) -> dict[str, Any]:
    column: dict[str, Any] = {
        "headerName": header,
        "field": field,
        "tooltipField": field,
    }
    if width is not None:
        column["width"] = width
    if min_width is not None:
        column["minWidth"] = min_width
    if flex is not None:
        column["flex"] = flex
    if pinned is not None:
        column["pinned"] = pinned
        column["lockPinned"] = True
    if formatter is not None:
        column["valueFormatter"] = formatter
    if numeric:
        column["type"] = "rightAligned"
        column["cellClass"] = "vol-trades-number-cell"
        column["headerClass"] = "vol-trades-number-header"
    elif cell_class is not None:
        column["cellClass"] = cell_class
    if group_show is not None:
        column["columnGroupShow"] = group_show
    if cell_rules is not None:
        column["cellClassRules"] = cell_rules
    return column


def _grid_group(
    header: str,
    css_class: str,
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "headerName": header,
        "headerClass": css_class,
        "marryChildren": True,
        "children": children,
    }


DETAIL_COLUMN_DEFS = [
    _grid_group(
        "Contract",
        "vol-trades-group-contract",
        [
            _grid_column(
                "Bloomberg security",
                "option_security",
                pinned="left",
                min_width=176,
                cell_class="vol-trades-security-cell",
            ),
            _grid_column(
                "P/C",
                "put_call",
                pinned="left",
                width=58,
                cell_class="vol-trades-side-cell",
                cell_rules=_GRID_SIDE_RULES,
            ),
            _grid_column(
                "Strike",
                "strike",
                pinned="left",
                width=86,
                formatter=_GRID_PRICE_3DP,
                numeric=True,
            ),
        ],
    ),
    _grid_group(
        "Option premium",
        "vol-trades-group-premium",
        [
            _grid_column(
                "Settlement", "settlement_price", width=98,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Latest", "last_price", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Mid", "option_mid", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Bid", "option_bid", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Ask", "option_ask", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Spread", "option_spread", width=88,
                formatter=_GRID_PRICE_4DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Spread %", "option_spread_pct", width=92,
                formatter=_GRID_PERCENT_2DP, numeric=True, group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Volatility (%)",
        "vol-trades-group-volatility",
        [
            _grid_column(
                "Settlement IV", "implied_volatility_pct", width=102,
                formatter=_GRID_IV_2DP, numeric=True,
            ),
            _grid_column(
                "Exec mid", "executable_iv_mid_pct", width=92,
                formatter=_GRID_IV_2DP, numeric=True,
            ),
            _grid_column(
                "Exec bid", "executable_iv_bid_pct", width=92,
                formatter=_GRID_IV_2DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Exec ask", "executable_iv_ask_pct", width=92,
                formatter=_GRID_IV_2DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Exec status",
                "executable_iv_status",
                min_width=116,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Exec exclusion",
                "executable_iv_exclusion_reason",
                min_width=250,
                group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Activity (contracts)",
        "vol-trades-group-activity",
        [
            _grid_column(
                "Volume", "volume", width=94,
                formatter=_GRID_INTEGER, numeric=True,
            ),
            _grid_column(
                "New volume", "volume_delta", width=102,
                formatter=_GRID_INTEGER, numeric=True,
            ),
            _grid_column(
                "Open interest", "open_interest", width=108,
                formatter=_GRID_INTEGER, numeric=True,
            ),
            _grid_column(
                "OI date", "open_interest_date", width=104,
                formatter=_GRID_DATE,
            ),
            _grid_column(
                "Volume session",
                "volume_scope_status",
                min_width=176,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Volume delta status",
                "volume_delta_status",
                min_width=154,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "OI source", "open_interest_source",
                min_width=210, group_show="open",
            ),
            _grid_column(
                "OI session",
                "open_interest_scope_status",
                min_width=176,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Intraday OI", "intraday_open_interest", width=112,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Intraday OI date", "intraday_open_interest_date", width=132,
                formatter=_GRID_DATE, group_show="open",
            ),
            _grid_column(
                "Settlement OI", "settlement_open_interest", width=120,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Settlement OI date", "settlement_open_interest_date", width=142,
                formatter=_GRID_DATE, group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Pricing future",
        "vol-trades-group-future",
        [
            _grid_column("Contract", "pricing_future", min_width=136),
            _grid_column(
                "Reference", "underlying_price", width=96,
                formatter=_GRID_PRICE_3DP, numeric=True,
            ),
            _grid_column(
                "Bid", "underlying_bid", width=88,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Mid", "underlying_mid", width=88,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Ask", "underlying_ask", width=88,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Spread", "underlying_spread", width=90,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Finance rate %", "finance_rate_pct", width=112,
                formatter=_GRID_PRICE_3DP, numeric=True, group_show="open",
            ),
            _grid_column(
                "Rate observed",
                "finance_rate_observed_at",
                min_width=152,
                formatter=_GRID_DATETIME_GST,
                group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Last exact trade",
        "vol-trades-group-trade",
        [
            _grid_column(
                "Price", "last_trade_price_exact", width=92,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Time GST", "last_trade_at_gst", min_width=142,
                formatter=_GRID_DATETIME_GST,
            ),
            _grid_column(
                "Matched future", "last_trade_underlying_price", width=112,
                formatter=_GRID_PRICE_3DP, numeric=True,
            ),
            _grid_column(
                "Trade IV", "last_trade_iv_pct", width=92,
                formatter=_GRID_IV_2DP, numeric=True,
            ),
            _grid_column(
                "Trade date", "last_trade_date", width=104,
                formatter=_GRID_DATE, group_show="open",
            ),
            _grid_column(
                "Future event GST",
                "last_trade_underlying_at_gst",
                min_width=152,
                formatter=_GRID_DATETIME_GST,
                group_show="open",
            ),
            _grid_column(
                "Match method", "last_trade_underlying_source",
                width=118, group_show="open",
            ),
            _grid_column(
                "Lag ms", "last_trade_match_lag_ms", width=88,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Condition", "last_trade_condition_codes",
                min_width=124, group_show="open",
            ),
            _grid_column(
                "IV status",
                "last_trade_iv_status",
                min_width=118,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "IV exclusion",
                "last_trade_iv_exclusion_reason",
                min_width=250,
                group_show="open",
            ),
            _grid_column(
                "Reused", "last_trade_reused", width=86, group_show="open",
            ),
        ],
    ),
    _grid_group(
        "Quality & source",
        "vol-trades-group-quality",
        [
            _grid_column(
                "Smile eligible",
                "smile_eligible",
                width=128,
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Option expiry",
                "option_expiration_date",
                width=108,
                formatter=_GRID_DATE,
                group_show="open",
            ),
            _grid_column(
                "Global ID", "option_global_id", min_width=145, group_show="open",
            ),
            _grid_column(
                "Option underlier",
                "native_option_underlier",
                min_width=148,
                group_show="open",
            ),
            _grid_column(
                "IV status",
                "iv_status",
                min_width=112,
                group_show="open",
                cell_rules=_GRID_STATUS_RULES,
            ),
            _grid_column(
                "Exclusion reason",
                "exclusion_reason",
                min_width=260,
                flex=1,
                group_show="open",
            ),
            _grid_column(
                "Discovery", "discovery_method", min_width=128, group_show="open",
            ),
            _grid_column(
                "Batch", "quote_batch_id", width=76,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Capture ms", "quote_capture_skew_ms", width=98,
                formatter=_GRID_INTEGER, numeric=True, group_show="open",
            ),
            _grid_column(
                "Request start",
                "quote_request_started_at",
                min_width=152,
                formatter=_GRID_DATETIME_GST,
                group_show="open",
            ),
            _grid_column(
                "Response",
                "quote_response_at",
                min_width=152,
                formatter=_GRID_DATETIME_GST,
                group_show="open",
            ),
        ],
    ),
]


TRADE_TAPE_COLUMN_DEFS = [
    _grid_group(
        "Trade",
        "vol-trades-group-contract",
        [
            _grid_column(
                "Executed GST", "execution_time_gst", pinned="left", width=128,
                cell_class="vol-trades-time-cell",
            ),
            _grid_column(
                "Bloomberg security",
                "option_security",
                pinned="left",
                min_width=176,
                cell_class="vol-trades-security-cell",
            ),
            _grid_column(
                "P/C",
                "put_call",
                pinned="left",
                width=58,
                cell_class="vol-trades-side-cell",
                cell_rules=_GRID_SIDE_RULES,
            ),
            _grid_column(
                "Strike",
                "strike",
                pinned="left",
                width=86,
                formatter=_GRID_PRICE_3DP,
                numeric=True,
            ),
        ],
    ),
    _grid_group(
        "Print",
        "vol-trades-group-premium",
        [
            _grid_column(
                "Price", "trade_price", width=94,
                formatter=_GRID_PRICE_4DP, numeric=True,
            ),
            _grid_column(
                "Size", "trade_size", width=82,
                formatter=_GRID_INTEGER, numeric=True,
            ),
            _grid_column("Condition", "condition_codes", min_width=112),
            _grid_column("Trade type", "trade_type_label", min_width=190),
            _grid_column("Reported GST", "reported_time_gst", width=128),
        ],
    ),
    _grid_group(
        "Matched future",
        "vol-trades-group-future",
        [
            _grid_column(
                "Price", "future_match_price", width=96,
                formatter=_GRID_PRICE_3DP, numeric=True,
            ),
            _grid_column("Method", "future_match_source", width=122),
            _grid_column(
                "Bid / ask age", "future_quote_ages", width=136,
            ),
            _grid_column(
                "Lag ms", "future_match_lag_ms", width=86,
                formatter=_GRID_INTEGER, numeric=True,
            ),
        ],
    ),
    _grid_group(
        "Trade volatility",
        "vol-trades-group-volatility",
        [
            _grid_column(
                "IV %", "trade_iv_pct", width=88,
                formatter=_GRID_IV_2DP, numeric=True,
            ),
            _grid_column(
                "Status",
                "trade_iv_label",
                min_width=205,
            ),
            _grid_column(
                "Reason / diagnostics",
                "trade_iv_exclusion_reason",
                min_width=250,
                flex=1,
            ),
        ],
    ),
]
