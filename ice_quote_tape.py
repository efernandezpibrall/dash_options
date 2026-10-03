"""Display projections of saved ICE valuations. No pricing or edge policy here."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import math
import pandas as pd

from options.ice_quote_interpretation import assessment_summary


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def amount(value, decimals=4):
    value = number(value)
    if value is None:
        return "—"
    rounded = Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return f"{abs(rounded) if rounded == 0 else rounded:.{decimals}f}"


def gst(value, seconds=False):
    if value is None or pd.isna(value):
        return "—"
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("Asia/Dubai").strftime("%d %b %H:%M:%S GST" if seconds else "%d %b %H:%M GST")


def display_orientation(row):
    convention = (row.get("edge_assessment") or {}).get("check_1_convention", {})
    if convention.get("assigned"):
        return convention.get("orientation", 1)
    return -1 if row.get("structure_code") == "CLLR" and row.get("fence_premium_orientation") == "to_put" else 1


def project_quote(row):
    """Render persisted values using the shared interpretation already attached."""
    result = {}
    decimals = int(row.get("price_decimals") or 3)
    bid, offer, trade = (number(row.get(key)) for key in ("bid", "offer", "single_price"))
    if bid is None and offer is None and trade is not None:
        result["broker_display"] = "TRADE " + amount(trade, decimals)
        result["broker_iv_display"] = amount(row.get("single_iv_pct"), 2)
    else:
        result["broker_display"] = (
            f"{amount(bid, decimals)} / {amount(offer, decimals)}" if bid is not None and offer is not None
            else "B " + amount(bid, decimals) if bid is not None
            else "O " + amount(offer, decimals) if offer is not None
            else "—"
        )
        biv, oiv = number(row.get("bid_iv_pct")), number(row.get("offer_iv_pct"))
        result["broker_iv_display"] = (
            f"{amount(biv, 2)} / {amount(oiv, 2)}" if biv is not None and oiv is not None
            else "B " + amount(biv, 2) if biv is not None
            else "O " + amount(oiv, 2) if oiv is not None else "—"
        )
    indication = number(row.get("broker_indication_magnitude"))
    if indication is not None:
        result["broker_display"] = row.get("broker_indication_display") or amount(indication, decimals)
    if row.get("structure_code") == "CLLR" and indication is None and result["broker_display"] != "—":
        result["broker_display"] += "*"
    result["broker_tooltip"] = "Bid / offer; B or O identifies a one-sided quote. A single broker price is a reported TRADE."
    if row.get("structure_code") == "CLLR":
        result["broker_tooltip"] += " Fence cash-flow side is inferred, not broker-confirmed: " + str(row.get("fence_premium_orientation") or "unassigned")
    result["broker_iv_tooltip"] = "Bid / offer IV %, or trade IV %. A structure has no single implied volatility unless explicitly defined."
    components = row.get("components") or []
    structure = bool(row.get("structure_code"))
    result["our_iv_display"] = "Leg IVs" if structure and components else amount(row.get("our_iv_pct"), 2)
    result["our_iv_tooltip"] = "Saved model volatility %. For structures, select this row to inspect each leg and delivery month."

    legs = row.get("structure_legs") or []
    orientation = display_orientation(row)
    if legs:
        terms = []
        for leg in legs:
            ratio = number(leg.get("ratio"))
            sign = "+" if (number(leg.get("direction")) or 1) * orientation > 0 else "−"
            ratio_label = f"{ratio:g}×" if ratio is not None and ratio != 1 else ""
            terms.append(f"{sign}{ratio_label}{number(leg.get('strike')):g}{leg.get('option_type', '')}" if number(leg.get('strike')) is not None else str(row.get("strike_label") or "—"))
        result["strategy_display"] = "/".join(terms)
    else:
        strike = number(row.get("strike"))
        result["strategy_display"] = f"{strike:g}{row.get('option_type') or ''}" if strike is not None else row.get("structure_label") or "—"
    if row.get("structure_code") == "DIAGONAL_CALL_SPREAD":
        result["our_iv_display"] = " / ".join(amount(number(part.get("strike_volatility")) * 100 if number(part.get("strike_volatility")) is not None else None, 2) for part in components) or "—"
        result["our_iv_tooltip"] = "Monthly leg IVs in contract order: " + "; ".join(f"{part.get('contract_month')}: {amount(number(part.get('strike_volatility')) * 100, 2)}%" for part in components if number(part.get('strike_volatility')) is not None)
    result["forward_display"] = amount(row.get("forward"), decimals)
    result["forward_tooltip"] = "Saved forward used for valuation."
    if row.get("structure_code") == "DIAGONAL_CALL_SPREAD":
        result["forward_display"] = " / ".join(amount(leg.get("future_price"), 2) for leg in legs)
        result["forward_tooltip"] = "; ".join(f"{leg.get('contract_month')}: {amount(leg.get('future_price'), 3)} EUR/MWh" for leg in legs)
    result["strategy_tooltip"] = f"{row.get('option_label') or 'Option'} · {row.get('structure_label') or result['strategy_display']}"
    if row.get("hedge_ratio") is not None:
        result["strategy_tooltip"] += f" · futures hedge ratio {row['hedge_ratio']}"
    model = number(row.get("theoretical_cash_premium"))
    result["model_sort_value"] = abs(model) if model is not None else None
    result["model_display"] = row.get("theoretical_display") or "—"
    result["pricing_reason"] = row.get("valuation_reason") or row.get("normalization_error") or ""
    if row.get("valuation_status") != "valued":
        result["model_display"] = "FAIL" if row.get("valuation_status") == "failed" or row.get("normalization_status") == "rejected" else "HOLD" if row.get("valuation_status") == "blocked" else "Pending"
    result["model_tooltip"] = result["pricing_reason"] or "Saved model premium. D = debit paid; Cr = credit received."
    result["model_display"] = result["model_display"].replace(" debit", " D").replace(" credit", " Cr")
    summary = assessment_summary(row.get("edge_assessment"), row)
    side = summary["side"] if summary else row.get("signal_side")
    edge = number(summary["net"] if summary else row.get("best_edge"))
    trade_only = bid is None and offer is None and trade is not None
    priced = row.get("valuation_status") == "valued" and model is not None
    if not priced:
        result.update(edge_marker="unpriced", edge_display="UNPRICED")
    elif trade_only:
        result.update(edge_marker="none", edge_display="TRADE")
    elif side in {"BUY", "SELL"} and edge is not None and edge > 0:
        # Colour identifies direction. Confidence is separately stated in text.
        provisional = summary is not None and not summary["qualified"]
        result.update(edge_marker=side.lower(), edge_display=f"{side} +{amount(edge, decimals + 1)}" + ("*" if provisional else ""))
    else:
        label = row.get("signal_label")
        label = label if label in {"SIDE?", "QUOTE?", "UNVERIFIED", "BUFFER", "CHECK"} else "NO EDGE"
        result.update(edge_marker="none", edge_display=label)
    result["edge_tooltip"] = "Assessment at valuation time. " + (row.get("edge_explanation") or "No positive comparable net edge recorded.")
    if not row.get("edge_fresh_now") and priced:
        result["edge_tooltip"] += " Historical quote; not currently actionable."
    for name, places in (("delta", 4), ("gamma", 6), ("vega", 4)):
        raw = number(row.get("saved_" + name)) if priced else None
        result["tape_" + name] = raw * orientation if raw is not None else None
        result[name + "_display"] = amount(result["tape_" + name], places)
    result["greek_tooltip"] = "Option-package Greeks for the displayed position, excluding the futures hedge. Vega is premium change per 1 volatility percentage point."
    if row.get("structure_code") == "DIAGONAL_CALL_SPREAD":
        result["greek_tooltip"] += " Diagonal totals describe parallel moves of both monthly forwards; select the row for separate leg risks."
    published = row.get("surface_published_at")
    result["surface_display"] = gst(published)
    cob = row.get("surface_cob_date") or "—"
    result["surface_tooltip"] = f"Calibration publication {row.get('surface_publication_id') or 'unrecorded'} · underlying market date {cob}."
    if not published or pd.isna(published):
        result["surface_display"] = "Legacy " + str(cob) if row.get("surface_source") and model is not None else "—"
        result["surface_tooltip"] += " Publication timestamp unavailable; no intraday time inferred."
    observed = pd.Timestamp(row["observed_at"]).tz_convert("Asia/Dubai")
    result["time_display"] = observed.strftime("%d %b %H:%M:%S")
    result["time_full_display"] = observed.strftime("%d %b %H:%M:%S")
    leg_only = (row.get("structure_code") == "DIAGONAL_CALL_SPREAD" and bool(components)
                and all(part.get("premium_basis") == "unassigned" for part in components))
    if leg_only:
        result["model_sort_value"] = None
        result["model_display"] = " / ".join(amount(part.get("theoretical_price")) for part in components)
        result["model_tooltip"] = "Individual long-option premiums in contract order (EUR/MWh). Package premium is unassigned; these are not bid/offer prices."
        result.update(edge_marker="unpriced", edge_display="BASIS REQUIRED")
        result["edge_tooltip"] = "Monthly legs priced. Equal MWh versus equal MW/lots is unassigned; no combined premium or edge."
        for name, places in (("delta", 4), ("gamma", 6), ("vega", 4)):
            result[name + "_display"] = " / ".join(amount(part.get(name), places) for part in components)
        result["greek_tooltip"] = "Separate signed leg Greeks in contract order, per MWh of each leg. No package aggregate. Vega is per one volatility percentage point."
    state = row.get("outbound_status")
    result["reply_display"] = {"acknowledged": "Sent", "pending": "Pending", "suppressed": "Suppressed", "blocked": "On hold", "explicit_failure": "Failed", "ambiguous_timeout": "Unconfirmed"}.get(state, "Not sent")
    result["reply_tooltip"] = row.get("outbound_error_message") or ("ICE acknowledged " + gst(row.get("outbound_acknowledged_at"), True) if state == "acknowledged" else result["reply_display"])
    if (leg_only and state == "acknowledged" and row.get("valued_at") is not None
            and row.get("outbound_acknowledged_at") is not None
            and pd.Timestamp(row["valued_at"]) > pd.Timestamp(row["outbound_acknowledged_at"])):
        result["reply_display"] = "Prior reply"
        result["reply_tooltip"] = "ICE acknowledged a reply before this leg valuation was saved. Review the delivery details for the exact message."
    source = str(row.get("forward_source") or "—")
    result["forward_source_display"] = {
        "ice_chat.future_leg.price": "ICE quote",
        "at_lng.curve:ICE_TTF": "ICE TTF curve",
        "ice_chat.future_leg.price + at_lng.curve:ICE_TTF shape": "ICE quote + curve",
        "individual exact-month ICE future references; see components": "ICE monthly quotes",
    }.get(source, source)
    result["forward_source_tooltip"] = source
    return result


def enrich_rows(frame):
    rows = frame.to_dict("records")
    for row in rows:
        row.update(project_quote(row))
    return pd.DataFrame(rows, index=frame.index) if rows else frame
