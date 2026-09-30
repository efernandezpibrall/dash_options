"""Chart data for the Vol Trades workspace."""

from __future__ import annotations



import math
from typing import Any
import numpy as np
import pandas as pd
from options.calibration_engine.converters.delta import strike_to_delta
from options.calibration_engine.io.brent_market import (
    CALIBRATION_MONEYNESS_BAND,
    DISPLAY_MONEYNESS_BAND,
    MIN_OPEN_INTEREST,
)
from options.brent_single_surface import BRENT_SINGLE_SURFACE_POLICY_VERSION
from options.hh_single_surface import HH_SINGLE_SURFACE_POLICY_VERSION
import vol_trades_data as market_data

X_AXIS_STRIKE = "strike"


X_AXIS_DELTA = "delta"


SETTLEMENT_IV_LOWER_BOUND = 0.005



_TRADE_MATCH_SOURCE_LABELS = {
    "QUOTE_MID": "Exact mid",
    "PREVAILING_MID": "Prevailing mid",
    "TRADE": "Future trade",
}


def _trade_match_source_label(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return _TRADE_MATCH_SOURCE_LABELS.get(normalized, normalized or "—")



def _calibrated_surface_label(product: str, surface: pd.DataFrame) -> str:
    policies = (
        surface["calibration_policy_version"].dropna().astype(str).unique().tolist()
        if "calibration_policy_version" in surface else []
    )
    if product == "BRENT":
        return "BRENT surface" if policies == [BRENT_SINGLE_SURFACE_POLICY_VERSION] else "Legacy BRENT surface"
    if product in {"LNE", "ON"}:
        return "HH surface · LNE calibrated" if policies == [HH_SINGLE_SURFACE_POLICY_VERSION] else "Legacy HH surface"
    return f"Calibrated {market_data._product_spec(product)['published_product']}"


def _is_icap_settlement_source(value: Any) -> bool:
    """Identify rows supplied by the shared ICAP settlement snapshot loader."""
    source = str(value or "").strip().lower()
    return source == "icap" or source.startswith(("icap:", "icap_", "icap "))


def _hover_count_strings(values: pd.Series) -> pd.Series:
    """Format activity counts once so missing values render as an em dash."""
    return pd.to_numeric(values, errors="coerce").map(
        lambda value: f"{value:,.0f}" if pd.notna(value) else "—"
    )




def _hover_date_strings(values: pd.Series) -> pd.Series:
    """Keep effective dates compact; the selected business date carries the year."""
    return pd.to_datetime(values, errors="coerce").map(
        lambda value: value.strftime("%d %b") if pd.notna(value) else "—"
    )


def _hover_oi_status_strings(values: pd.Series) -> pd.Series:
    return values.fillna("unavailable").astype(str).map(
        {
            "same_day": "same day",
            "stale": "stale",
            "effective_date_unavailable": "date unavailable",
            "settlement": "official close",
            "unavailable": "unavailable",
        }
    ).fillna("reported")


def _expiry_mask(frame: pd.DataFrame, expiry: pd.Timestamp, column: str) -> pd.Series:
    return pd.to_datetime(frame[column], errors="coerce").dt.normalize().eq(expiry)


def _normalize_x_axis(value: Any) -> str:
    return X_AXIS_DELTA if str(value).strip().lower() == X_AXIS_DELTA else X_AXIS_STRIKE


def _option_side_for_strike(strike: Any, forward: Any) -> str:
    strike_value = market_data._numeric_or_none(strike)
    forward_value = market_data._numeric_or_none(forward)
    return (
        "P"
        if strike_value is not None
        and forward_value is not None
        and strike_value < forward_value
        else "C"
    )


def _display_delta(signed_delta: Any, put_call: Any = None) -> float:
    """Project signed Black-76 delta onto put wing -> ATM -> call wing."""
    delta = market_data._numeric_or_none(signed_delta)
    if delta is None or not -1.0 <= delta <= 1.0:
        return np.nan
    is_put = (
        str(put_call).strip().lower().startswith("p")
        if put_call is not None
        else delta < 0.0
    )
    display = abs(delta) if is_put else 1.0 - delta
    return float(np.clip(display, 0.0, 1.0))


def _delta_x_from_market_inputs(
    *,
    strike: Any,
    forward: Any,
    volatility: Any,
    dte: Any,
    put_call: Any,
) -> float:
    strike_value = market_data._numeric_or_none(strike)
    forward_value = market_data._numeric_or_none(forward)
    volatility_value = market_data._numeric_or_none(volatility)
    dte_value = market_data._numeric_or_none(dte)
    if (
        strike_value is None
        or strike_value <= 0.0
        or forward_value is None
        or forward_value <= 0.0
        or volatility_value is None
        or volatility_value <= 0.0
        or dte_value is None
        or dte_value <= 0.0
    ):
        return np.nan
    option_type = (
        "put" if str(put_call).strip().lower().startswith("p") else "call"
    )
    try:
        signed_delta = strike_to_delta(
            strike_value,
            forward_value,
            volatility_value,
            dte_value,
            option_type=option_type,
        )
    except (ArithmeticError, ValueError):
        return np.nan
    return _display_delta(signed_delta, option_type)


def _row_delta_x(
    row: pd.Series,
    *,
    volatility_column: str,
    forward_column: str,
) -> float:
    expiration = pd.to_datetime(row.get("option_expiration_date"), errors="coerce")
    business_date = pd.to_datetime(row.get("business_date"), errors="coerce")
    if pd.isna(expiration) or pd.isna(business_date):
        return np.nan
    dte = (expiration.normalize() - business_date.normalize()).days
    return _delta_x_from_market_inputs(
        strike=row.get("strike"),
        forward=row.get(forward_column),
        volatility=row.get(volatility_column),
        dte=dte,
        put_call=row.get("put_call"),
    )


def _last_price_parity_quality(raw: pd.DataFrame) -> dict[str, Any]:
    """Audit TFO LAST_PRICE timing without treating it as a current smile."""
    result = {
        "status": "not_applicable",
        "pair_count": 0,
        "parity_forward": None,
        "parity_mad": None,
        "live_forward": None,
        "live_spread": None,
        "gap": None,
        "tolerance": None,
        "timestamp_coverage": 0.0,
    }
    if raw is None or raw.empty:
        return result
    work = raw.copy()
    if (
        market_data._normalize_product(work.get("product", pd.Series([market_data.PRODUCT])).iloc[0]) != "TFO"
        or str(work.get("snapshot_kind", pd.Series([""])).iloc[0]).upper()
        != "INTRADAY"
    ):
        return result

    work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
    work["last_price"] = pd.to_numeric(work.get("last_price"), errors="coerce")
    work["put_call"] = work.get(
        "put_call", pd.Series("", index=work.index)
    ).astype(str).str.upper()
    valid = work.loc[
        work["strike"].gt(0.0)
        & work["last_price"].gt(0.0)
        & work["put_call"].isin(["C", "P"])
    ].copy()
    if valid.empty:
        result["status"] = "insufficient"
        return result

    paired = valid.pivot_table(
        index="strike", columns="put_call", values="last_price", aggfunc="median"
    )
    if "C" not in paired or "P" not in paired:
        result["status"] = "insufficient"
        return result
    paired = paired.dropna(subset=["C", "P"])
    parity_forwards = (
        pd.Series(paired.index.to_numpy(dtype=float), index=paired.index)
        + paired["C"]
        - paired["P"]
    )
    parity_forwards = parity_forwards.loc[
        np.isfinite(parity_forwards) & parity_forwards.gt(0.0)
    ]
    result["pair_count"] = int(len(parity_forwards))
    if parity_forwards.empty:
        result["status"] = "insufficient"
        return result

    parity_forward = float(parity_forwards.median())
    parity_mad = float((parity_forwards - parity_forward).abs().median())
    live_values = pd.to_numeric(
        work.get("underlying_mid", pd.Series(np.nan, index=work.index)),
        errors="coerce",
    )
    live_values = live_values.loc[live_values.gt(0.0)]
    live_forward = float(live_values.median()) if not live_values.empty else None
    bid = pd.to_numeric(
        work.get("underlying_bid", pd.Series(np.nan, index=work.index)),
        errors="coerce",
    )
    ask = pd.to_numeric(
        work.get("underlying_ask", pd.Series(np.nan, index=work.index)),
        errors="coerce",
    )
    spreads = (ask - bid).loc[ask.gt(bid) & bid.gt(0.0)]
    live_spread = float(spreads.median()) if not spreads.empty else 0.0
    timestamp_coverage = float(
        pd.to_datetime(
            valid.get("last_trade_date", pd.Series(pd.NaT, index=valid.index)),
            errors="coerce",
        ).notna().mean()
    )
    result.update(
        {
            "parity_forward": parity_forward,
            "parity_mad": parity_mad,
            "live_forward": live_forward,
            "live_spread": live_spread,
            "timestamp_coverage": timestamp_coverage,
        }
    )
    if len(parity_forwards) < 3:
        result["status"] = "insufficient"
        return result
    if live_forward is None:
        result["status"] = "unverifiable"
        return result

    tolerance = max(2.0 * live_spread, 0.25, 0.005 * live_forward)
    gap = live_forward - parity_forward
    result.update({"gap": gap, "tolerance": tolerance})
    if parity_mad > tolerance:
        result["status"] = "incoherent"
    elif abs(gap) <= tolerance:
        result["status"] = "current_compatible"
    else:
        result["status"] = "coherent_historical"
    return result


def _settlement_reference_selection(
    raw: pd.DataFrame,
    x_axis: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return strict-OTM settlement IVs and auditable chart exclusions.

    Liquidity governs calibration eligibility, not whether a published
    settlement is visible. Select the OTM option before testing IV availability;
    an intrinsic-value ITM option must never replace an unresolved OTM wing.
    """
    columns = [
        "strike",
        "put_call",
        "option_security",
        "settlement_price",
        "reference_iv",
        "forward",
        "volume",
        "open_interest",
        "calibration_status",
        "display_x",
    ]
    exclusion_columns = [
        "underlying_contract_month",
        "strike",
        "put_call",
        "option_security",
        "settlement_price",
        "reason_code",
        "reason",
    ]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=exclusion_columns)
    work = raw.copy()
    if str(work.get("snapshot_kind", pd.Series([""])).iloc[0]).upper() == "INTRADAY":
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=exclusion_columns)
    for column in (
        "strike",
        "underlying_price",
        "settlement_price",
        "implied_volatility",
        "volume",
        "open_interest",
    ):
        work[column] = pd.to_numeric(work.get(column), errors="coerce")
    work = work.loc[
        work["strike"].gt(0.0)
    ].copy()
    if work.empty:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=exclusion_columns)

    rows = []
    exclusions = []
    for strike, strike_rows in work.groupby("strike", sort=True):
        forward_values = pd.to_numeric(
            strike_rows["underlying_price"], errors="coerce"
        ).dropna()
        forward_values = forward_values.loc[forward_values.gt(0.0)]
        if forward_values.empty:
            exclusions.append(
                {
                    "underlying_contract_month": strike_rows.get(
                        "underlying_contract_month", pd.Series([pd.NaT])
                    ).iloc[0],
                    "strike": float(strike),
                    "put_call": None,
                    "option_security": None,
                    "settlement_price": None,
                    "reason_code": "pricing_future_unavailable",
                    "reason": "Pricing-future settlement is unavailable",
                }
            )
            continue
        forward = float(forward_values.median())
        preferred_side = _option_side_for_strike(strike, forward)
        preferred_rows = strike_rows.loc[
            strike_rows["put_call"].astype(str).str.upper().eq(preferred_side)
        ].copy()
        if preferred_rows.empty:
            exclusions.append(
                {
                    "underlying_contract_month": strike_rows.get(
                        "underlying_contract_month", pd.Series([pd.NaT])
                    ).iloc[0],
                    "strike": float(strike),
                    "put_call": preferred_side,
                    "option_security": None,
                    "settlement_price": None,
                    "reason_code": "otm_contract_unavailable",
                    "reason": "OTM option contract is unavailable",
                }
            )
            continue
        candidate = (
            preferred_rows.assign(
                _has_settlement=preferred_rows["settlement_price"].gt(0.0),
                _has_iv=preferred_rows["implied_volatility"].gt(0.0),
            )
            .sort_values(
                ["_has_settlement", "_has_iv", "option_security"],
                ascending=[False, False, True],
            )
            .iloc[0]
        )
        settlement_price = market_data._numeric_or_none(candidate.get("settlement_price"))
        reference_iv = market_data._numeric_or_none(candidate.get("implied_volatility"))
        reason_code = None
        reason = None
        if settlement_price is None or settlement_price <= 0.0:
            reason_code = "otm_settlement_unavailable"
            reason = "OTM settlement premium is unavailable"
        elif reference_iv is None or reference_iv <= 0.0:
            raw_reason = str(candidate.get("iv_exclusion_reason") or "").strip()
            if "(0.5%, 200%)" in raw_reason:
                reason_code = "otm_iv_outside_supported_range"
                reason = "OTM premium does not imply IV within 0.5%–200%"
            else:
                reason_code = "otm_iv_unavailable"
                reason = raw_reason or "OTM implied volatility is unavailable"
        elif reference_iv <= SETTLEMENT_IV_LOWER_BOUND + 1e-12:
            reason_code = "otm_iv_lower_boundary"
            reason = "OTM IV is pinned to the 0.5% numerical boundary"
        if reason_code is not None:
            exclusions.append(
                {
                    "underlying_contract_month": candidate.get(
                        "underlying_contract_month"
                    ),
                    "strike": float(strike),
                    "put_call": preferred_side,
                    "option_security": candidate.get("option_security"),
                    "settlement_price": settlement_price,
                    "reason_code": reason_code,
                    "reason": reason,
                }
            )
            continue
        display_x = (
            _row_delta_x(
                candidate,
                volatility_column="implied_volatility",
                forward_column="underlying_price",
            )
            if _normalize_x_axis(x_axis) == X_AXIS_DELTA
            else float(strike)
        )
        if not math.isfinite(display_x):
            exclusions.append(
                {
                    "underlying_contract_month": candidate.get(
                        "underlying_contract_month"
                    ),
                    "strike": float(strike),
                    "put_call": preferred_side,
                    "option_security": candidate.get("option_security"),
                    "settlement_price": settlement_price,
                    "reason_code": "delta_unavailable",
                    "reason": "Delta coordinate is unavailable",
                }
            )
            continue
        rows.append(
            {
                "strike": float(strike),
                "put_call": str(candidate["put_call"]).upper(),
                "option_security": candidate.get("option_security"),
                "settlement_price": float(candidate["settlement_price"]),
                "reference_iv": reference_iv,
                "forward": forward,
                "volume": candidate.get("volume"),
                "open_interest": candidate.get("open_interest"),
                "calibration_status": _settlement_calibration_status(
                    candidate,
                    forward,
                ),
                "display_x": float(display_x),
            }
        )
    selected = pd.DataFrame(rows, columns=columns).sort_values("display_x")
    excluded = pd.DataFrame(exclusions, columns=exclusion_columns)
    return selected, excluded


def _settlement_calibration_status(candidate: pd.Series, forward: float) -> str:
    """Summarize Brent calibration suitability without creating another trace."""
    if market_data._normalize_product(candidate.get("product")) != "BRENT":
        return ""
    open_interest = market_data._numeric_or_none(candidate.get("open_interest"))
    if open_interest is None:
        return "Not assessed · OI unavailable"
    if open_interest < MIN_OPEN_INTEREST:
        return (
            f"Excluded · OI {open_interest:,.0f} < "
            f"{MIN_OPEN_INTEREST:,.0f}"
        )
    strike = market_data._numeric_or_none(candidate.get("strike"))
    if strike is None or forward <= 0.0:
        return "Not assessed · moneyness unavailable"
    moneyness = strike / forward
    display_low, display_high = DISPLAY_MONEYNESS_BAND
    if not display_low < moneyness < display_high:
        return "Excluded · outside supported moneyness"
    body_low, body_high = CALIBRATION_MONEYNESS_BAND
    if not body_low < moneyness < body_high:
        return "Excluded · outside calibration body"
    return "Eligible · OI and moneyness gates passed"


def _settlement_reference_smile(
    raw: pd.DataFrame,
    x_axis: str,
) -> pd.DataFrame:
    """Return only strict-OTM, price-valid Bloomberg settlement IVs."""
    selected, _ = _settlement_reference_selection(raw, x_axis)
    return selected


def _activity_delta_projection(
    raw: pd.DataFrame,
    trade_tape: pd.DataFrame | None = None,
    prior_settlement: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Map activity to current quotes, exact trades, then prior settlement."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["strike", "display_delta", "delta_source"])
    work = raw.copy()
    is_intraday = str(work["snapshot_kind"].iloc[0]).upper() == "INTRADAY"
    if is_intraday:
        primary_iv = pd.to_numeric(work["executable_iv_mid"], errors="coerce").where(
            work["executable_iv_status"].astype(str).eq("resolved")
        )
        forward_values = pd.to_numeric(work["underlying_mid"], errors="coerce").where(
            pd.to_numeric(work["underlying_mid"], errors="coerce").gt(0.0),
            pd.to_numeric(work["underlying_price"], errors="coerce"),
        )
    else:
        primary_iv = pd.to_numeric(work["implied_volatility"], errors="coerce")
        forward_values = pd.to_numeric(work["underlying_price"], errors="coerce")
    work["_primary_iv"] = primary_iv.where(primary_iv.gt(0.0))
    work["_forward"] = forward_values.where(forward_values.gt(0.0))
    reference = (
        work.loc[work["_primary_iv"].notna(), ["strike", "_primary_iv"]]
        .assign(strike=lambda frame: pd.to_numeric(frame["strike"], errors="coerce"))
        .dropna()
        .groupby("strike", as_index=False)["_primary_iv"]
        .mean()
        .sort_values("strike")
    )
    business_date = pd.to_datetime(work["business_date"], errors="coerce").dropna()
    expiration = pd.to_datetime(
        work["option_expiration_date"], errors="coerce"
    ).dropna()
    forwards = work["_forward"].dropna()
    if business_date.empty or expiration.empty or forwards.empty:
        return pd.DataFrame(columns=["strike", "display_delta", "delta_source"])
    dte = (expiration.iloc[0].normalize() - business_date.iloc[0].normalize()).days
    forward = float(forwards.median())
    if dte <= 0 or forward <= 0.0:
        return pd.DataFrame(columns=["strike", "display_delta", "delta_source"])

    activity_strikes = sorted(
        pd.to_numeric(work["strike"], errors="coerce").dropna().unique()
    )
    rows: list[dict[str, Any]] = []
    projected: set[float] = set()

    def add_curve_projection(
        curve: pd.DataFrame,
        *,
        curve_forward: float,
        curve_dte: int,
        label: str,
    ) -> None:
        if curve.empty or curve_forward <= 0.0 or curve_dte <= 0:
            return
        curve = curve.sort_values("strike")
        reference_strikes = curve["strike"].to_numpy(dtype=float)
        reference_vols = curve["_primary_iv"].to_numpy(dtype=float)
        direct_strikes = set(reference_strikes.tolist())
        for strike in activity_strikes:
            strike_value = float(strike)
            if strike_value in projected:
                continue
            reference_iv = float(
                np.interp(strike_value, reference_strikes, reference_vols)
            )
            if strike_value in direct_strikes:
                source = f"{label} at strike"
            elif reference_strikes[0] < strike_value < reference_strikes[-1]:
                source = f"Interpolated {label.lower()}"
            else:
                source = f"Nearest {label.lower()} wing"
            rows.append(
                {
                    "strike": strike_value,
                    "display_delta": _delta_x_from_market_inputs(
                        strike=strike_value,
                        forward=curve_forward,
                        volatility=reference_iv,
                        dte=curve_dte,
                        put_call=_option_side_for_strike(
                            strike_value, curve_forward
                        ),
                    ),
                    "delta_source": source,
                }
            )
            projected.add(strike_value)

    if not reference.empty:
        add_curve_projection(
            reference,
            curve_forward=forward,
            curve_dte=dte,
            label="Current executable smile",
        )

    if is_intraday and trade_tape is not None and not trade_tape.empty:
        trades = trade_tape.copy()
        contract_months = pd.to_datetime(
            trades.get(
                "underlying_contract_month",
                pd.Series(pd.NaT, index=trades.index),
            ),
            errors="coerce",
        ).dt.normalize()
        raw_month = pd.to_datetime(
            work["underlying_contract_month"], errors="coerce"
        ).dropna()
        if not raw_month.empty:
            trades = trades.loc[contract_months.eq(raw_month.iloc[0].normalize())]
        trades = trades.loc[
            trades.get(
                "trade_iv_status", pd.Series("", index=trades.index)
            ).astype(str).eq("resolved")
            & pd.to_numeric(
                trades.get("trade_iv", pd.Series(np.nan, index=trades.index)),
                errors="coerce",
            ).gt(0.0)
            & pd.to_numeric(
                trades.get(
                    "future_match_price", pd.Series(np.nan, index=trades.index)
                ),
                errors="coerce",
            ).gt(0.0)
        ].copy()
        if not trades.empty:
            trades["strike"] = pd.to_numeric(trades["strike"], errors="coerce")
            trades = (
                trades.sort_values("trade_at")
                .dropna(subset=["strike"])
                .drop_duplicates("strike", keep="last")
            )
            for trade in trades.itertuples(index=False):
                strike_value = float(trade.strike)
                if strike_value in projected or strike_value not in activity_strikes:
                    continue
                trade_expiration = pd.to_datetime(
                    trade.option_expiration_date, errors="coerce"
                )
                trade_business_date = pd.to_datetime(
                    trade.business_date, errors="coerce"
                )
                trade_dte = (
                    trade_expiration.normalize() - trade_business_date.normalize()
                ).days
                rows.append(
                    {
                        "strike": strike_value,
                        "display_delta": _delta_x_from_market_inputs(
                            strike=strike_value,
                            forward=float(trade.future_match_price),
                            volatility=float(trade.trade_iv),
                            dte=trade_dte,
                            put_call=_option_side_for_strike(
                                strike_value, float(trade.future_match_price)
                            ),
                        ),
                        "delta_source": (
                            "Trade-time IV · "
                            + _trade_match_source_label(
                                trade.future_match_source
                            )
                        ),
                    }
                )
                projected.add(strike_value)

    if is_intraday and prior_settlement is not None and not prior_settlement.empty:
        prior = prior_settlement.copy()
        prior["strike"] = pd.to_numeric(prior["strike"], errors="coerce")
        prior["_primary_iv"] = pd.to_numeric(
            prior["reference_iv"], errors="coerce"
        )
        prior = prior.dropna(subset=["strike", "_primary_iv"])
        prior_dates = pd.to_datetime(
            prior.get("business_date", pd.Series(dtype=object)), errors="coerce"
        ).dropna()
        prior_expirations = pd.to_datetime(
            prior.get("option_expiration_date", pd.Series(dtype=object)),
            errors="coerce",
        ).dropna()
        prior_forwards = pd.to_numeric(prior["forward"], errors="coerce").dropna()
        if not prior_dates.empty and not prior_expirations.empty and not prior_forwards.empty:
            prior_date = prior_dates.iloc[0].normalize()
            add_curve_projection(
                prior[["strike", "_primary_iv"]],
                curve_forward=float(prior_forwards.median()),
                curve_dte=(prior_expirations.iloc[0].normalize() - prior_date).days,
                label=f"Prior settlement {prior_date.strftime('%d %b %Y')}",
            )

    return pd.DataFrame(rows, columns=["strike", "display_delta", "delta_source"])


def _intraday_expiry_quality(
    raw: pd.DataFrame,
    trade_tape: pd.DataFrame | None,
    prior_settlement: pd.DataFrame | None,
) -> dict[str, Any]:
    """Return one compact, trader-facing quality state for an expiry."""
    if raw is None or raw.empty or str(raw["snapshot_kind"].iloc[0]).upper() != "INTRADAY":
        return {}
    executable_count = int(
        (
            raw.get("executable_iv_status", pd.Series(index=raw.index, dtype=object))
            .astype(str)
            .eq("resolved")
            & pd.to_numeric(
                raw.get("executable_iv_mid", pd.Series(np.nan, index=raw.index)),
                errors="coerce",
            ).gt(0.0)
        ).sum()
    )
    trade_count = 0
    if trade_tape is not None and not trade_tape.empty:
        trade_count = int(
            (
                trade_tape.get(
                    "trade_iv_status",
                    pd.Series(index=trade_tape.index, dtype=object),
                )
                .astype(str)
                .eq("resolved")
                & pd.to_numeric(
                    trade_tape.get(
                        "trade_iv", pd.Series(np.nan, index=trade_tape.index)
                    ),
                    errors="coerce",
                ).gt(0.0)
            ).sum()
        )
    elif "last_trade_iv_status" in raw:
        trade_count = int(
            (
                raw["last_trade_iv_status"].astype(str).eq("resolved")
                & pd.to_numeric(raw["last_trade_iv"], errors="coerce").gt(0.0)
            ).sum()
        )

    prior_count = 0 if prior_settlement is None else int(len(prior_settlement))
    prior_dates = (
        pd.Series(dtype="datetime64[ns]")
        if prior_settlement is None or prior_settlement.empty
        else pd.to_datetime(prior_settlement.get("business_date"), errors="coerce").dropna()
    )
    prior_label = (
        prior_dates.iloc[0].strftime("%d %b %Y") if not prior_dates.empty else None
    )
    parity = _last_price_parity_quality(raw)

    if executable_count:
        label = "Live executable"
        color = "success"
        detail = f"{executable_count:,} synchronized two-sided IV observations"
    elif trade_count:
        label = "Trades only"
        color = "info"
        detail = f"No executable smile · {trade_count:,} resolved trade-time IV observations"
    elif prior_count:
        label = f"Prior settle · {prior_label}" if prior_label else "Prior settle reference"
        color = "secondary"
        detail = "No reliable current IV"
    else:
        label = "No reliable IV"
        color = "danger"
        detail = "No executable quotes, matched trades, or prior settlement reference"

    if not executable_count and parity["status"] == "coherent_historical":
        direction = "higher" if float(parity["gap"]) > 0.0 else "lower"
        detail += (
            f" · FJS last-price parity implies TZT {parity['parity_forward']:.4f}; "
            f"live TZT {parity['live_forward']:.4f} is {abs(parity['gap']):.4f} "
            f"EUR/MWh {direction}"
        )
    elif not executable_count and parity["status"] == "incoherent":
        detail += " · FJS last-price pairs are internally inconsistent"
    elif not executable_count and parity["status"] in {"insufficient", "unverifiable"}:
        detail += " · Bloomberg LAST_PRICE timing cannot be verified"

    return {
        "status": label,
        "color": color,
        "detail": detail,
        "executable_count": executable_count,
        "trade_count": trade_count,
        "prior_settlement_count": prior_count,
        "prior_settlement_date": prior_label,
        "last_price_parity": parity,
    }


def _settlement_chart_exclusions(
    chain: pd.DataFrame,
    x_axis: str,
) -> pd.DataFrame:
    columns = [
        "underlying_contract_month",
        "strike",
        "put_call",
        "option_security",
        "settlement_price",
        "reason_code",
        "reason",
    ]
    if chain is None or chain.empty:
        return pd.DataFrame(columns=columns)
    exclusions = []
    contract_months = pd.to_datetime(
        chain.get("underlying_contract_month"), errors="coerce"
    ).dt.normalize()
    for contract_month in sorted(contract_months.dropna().unique()):
        expiry_rows = chain.loc[contract_months.eq(contract_month)].copy()
        _, expiry_exclusions = _settlement_reference_selection(expiry_rows, x_axis)
        if not expiry_exclusions.empty:
            expiry_exclusions["underlying_contract_month"] = pd.Timestamp(
                contract_month
            )
            exclusions.append(expiry_exclusions)
    if not exclusions:
        return pd.DataFrame(columns=columns)
    return pd.concat(exclusions, ignore_index=True)[columns]

