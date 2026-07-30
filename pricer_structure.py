"""Pure pricing and scenario helpers for the multi-leg option structure pricer.

The module deliberately has no Dash imports.  It accepts JSON-shaped inputs,
validates the entire structure, prices every leg through the existing options
library, and returns a versioned JSON-safe calculation snapshot.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import numpy as np

from options.options_library import (
    asian_76,
    black_76,
    kirk_model_with_substitution,
    kirk_spread_greeks,
)


SCHEMA_VERSION = 5
MAX_LEGS = 20
SUPPORTED_MODELS = {"black76", "asian76", "kirk"}
SUPPORTED_STRUCTURE_TYPES = ("Financial", "Physical")
DEFAULT_STRUCTURE_TYPE = "Financial"
SUPPORTED_ASSETS = ("TTF", "JKM", "HH", "Brent", "NBP")
DEFAULT_ASSET = "TTF"
SIDE_SIGN = {"BUY": 1.0, "SELL": -1.0}
MODEL_LABELS = {
    "black76": "Black-76",
    "asian76": "Asian-76",
    "kirk": "Kirk",
}
GREEK_FIELDS = {
    "black76": ("delta", "gamma", "theta", "vega", "rho"),
    "asian76": ("delta", "gamma", "theta", "vega", "rho"),
    "kirk": (
        "delta_s1",
        "delta_s2",
        "gamma_s1",
        "gamma_s2",
        "gamma_s1s2",
        "vega_sigma1",
        "vega_sigma2",
        "theta",
        "corr_sensitivity",
        "vega_equiv",
    ),
}
GREEK_LABELS = {
    "delta": "Delta",
    "gamma": "Gamma",
    "theta": "Theta",
    "vega": "Vega (input vol, 1 point)",
    "rho": "Rho (1 rate point)",
    "delta_s1": "Delta — Asset 1",
    "delta_s2": "Delta — Asset 2",
    "gamma_s1": "Gamma — Asset 1",
    "gamma_s2": "Gamma — Asset 2",
    "gamma_s1s2": "Cross-gamma — Asset 1/2",
    "vega_sigma1": "Vega — Asset 1 input vol (1 point)",
    "vega_sigma2": "Vega — Asset 2 input vol (1 point)",
    "corr_sensitivity": "Correlation sensitivity (1 point)",
    "vega_equiv": "Equivalent-vol Vega (1 point)",
}


class StructureValidationError(ValueError):
    """Raised when a structure cannot be priced safely as a complete unit."""


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.split("T", 1)[0])
        except ValueError as exc:
            raise StructureValidationError(f"{field} must be a valid date.") from exc
    raise StructureValidationError(f"{field} is required.")


def _finite_number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strict_minimum: bool = False,
) -> float:
    if value is None or isinstance(value, bool):
        raise StructureValidationError(f"{field} is required.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StructureValidationError(f"{field} must be numeric.") from exc
    if not math.isfinite(number):
        raise StructureValidationError(f"{field} must be finite.")
    if minimum is not None:
        invalid = number <= minimum if strict_minimum else number < minimum
        if invalid:
            operator = "greater than" if strict_minimum else "at least"
            raise StructureValidationError(f"{field} must be {operator} {minimum:g}.")
    if maximum is not None and number > maximum:
        raise StructureValidationError(f"{field} must be at most {maximum:g}.")
    return number


def _json_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def count_business_days(start_date: date, end_date: date) -> int:
    """Return inclusive Monday-Friday days, matching the existing pricer."""
    if end_date < start_date:
        return 0
    total_days = (end_date - start_date).days + 1
    return sum(
        1
        for offset in range(total_days)
        if (start_date + timedelta(days=offset)).weekday() < 5
    )


def volatility_adjustment(
    as_of: date,
    expiration_date: date,
    contract_expiration_date: date,
) -> tuple[float, int, int]:
    option_business_days = count_business_days(as_of, expiration_date)
    contract_business_days = count_business_days(as_of, contract_expiration_date)
    if option_business_days <= 0 or contract_business_days <= 0:
        return 1.0, option_business_days, contract_business_days
    factor = math.sqrt(option_business_days / contract_business_days)
    return factor, option_business_days, contract_business_days


def _default_dates(as_of: date) -> tuple[str, str]:
    expiration = as_of + timedelta(days=30)
    return expiration.isoformat(), expiration.isoformat()


def default_context(model: str, as_of: date | None = None) -> dict[str, Any]:
    as_of = as_of or date.today()
    expiration, contract_expiration = _default_dates(as_of)
    if model == "black76":
        return {
            "structure_type": DEFAULT_STRUCTURE_TYPE,
            "asset": DEFAULT_ASSET,
            "forward": 100.0,
            "rate": 0.05,
            "expiration_date": expiration,
            "contract_expiration_date": contract_expiration,
        }
    if model == "asian76":
        return {
            "structure_type": DEFAULT_STRUCTURE_TYPE,
            "asset": DEFAULT_ASSET,
            "forward": 100.0,
            "rate": 0.05,
            "averaging_start_date": (as_of + timedelta(days=7)).isoformat(),
            "expiration_date": expiration,
            "contract_expiration_date": contract_expiration,
        }
    if model == "kirk":
        return {
            "structure_type": DEFAULT_STRUCTURE_TYPE,
            "asset": DEFAULT_ASSET,
            "asset_1": 100.0,
            "asset_2": 90.0,
            "correlation": 0.5,
            "expiration_date": expiration,
            "contract_expiration_date": contract_expiration,
        }
    raise StructureValidationError(f"Unsupported pricing model: {model}.")


def default_leg(model: str, sequence: int = 1) -> dict[str, Any]:
    common = {
        "leg_id": f"leg-{sequence}",
        "name": f"Leg {sequence}",
        "side": "BUY",
        "ratio": 1.0,
        "call_put": "C",
        "strike": 100.0 if model != "kirk" else 5.0,
    }
    if model in {"black76", "asian76"}:
        common["volatility"] = 0.2
    elif model == "kirk":
        common["volatility_asset_1"] = 0.2
        common["volatility_asset_2"] = 0.15
    else:
        raise StructureValidationError(f"Unsupported pricing model: {model}.")
    return common


def default_draft(model: str = "black76", as_of: date | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model": model,
        "context": default_context(model, as_of),
        "sizing": {"structure_quantity": 1, "contract_multiplier": 1.0},
        "legs": [default_leg(model, 1)],
        "next_leg_sequence": 2,
    }


def _normalize_context(
    model: str,
    context: dict[str, Any],
    as_of: date,
) -> dict[str, Any]:
    raw_structure_type = context.get("structure_type", DEFAULT_STRUCTURE_TYPE)
    if raw_structure_type is None:
        raise StructureValidationError("Type is required.")
    structure_type = str(raw_structure_type).strip()
    if structure_type not in SUPPORTED_STRUCTURE_TYPES:
        supported = ", ".join(SUPPORTED_STRUCTURE_TYPES)
        raise StructureValidationError(f"Type must be one of: {supported}.")
    raw_asset = context.get("asset", DEFAULT_ASSET)
    if raw_asset is None:
        raise StructureValidationError("Asset is required.")
    asset = str(raw_asset).strip()
    if asset not in SUPPORTED_ASSETS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    expiration_date = _as_date(context.get("expiration_date"), "Expiration date")
    contract_expiration_date = _as_date(
        context.get("contract_expiration_date"),
        "Contract expiration date",
    )
    if expiration_date <= as_of:
        raise StructureValidationError(
            "Expiration date must be after the valuation date."
        )
    if contract_expiration_date < expiration_date:
        raise StructureValidationError(
            "Contract expiration date must be on or after option expiration."
        )

    factor, option_business_days, contract_business_days = volatility_adjustment(
        as_of,
        expiration_date,
        contract_expiration_date,
    )
    time_to_expiry = (expiration_date - as_of).days / 365.0
    normalized: dict[str, Any] = {
        "structure_type": structure_type,
        "asset": asset,
        "expiration_date": expiration_date.isoformat(),
        "contract_expiration_date": contract_expiration_date.isoformat(),
        "time_to_expiry": time_to_expiry,
        "vol_adjustment_factor": factor,
        "option_business_days": option_business_days,
        "contract_business_days": contract_business_days,
    }

    if model in {"black76", "asian76"}:
        normalized["forward"] = _finite_number(
            context.get("forward"),
            "Forward price",
            minimum=0.0,
            strict_minimum=True,
        )
        normalized["rate"] = _finite_number(
            context.get("rate"),
            "Risk-free rate",
            minimum=-1.0,
            maximum=2.0,
        )
    elif model == "kirk":
        normalized["asset_1"] = _finite_number(
            context.get("asset_1"),
            "Asset 1 price",
            minimum=0.0,
            strict_minimum=True,
        )
        normalized["asset_2"] = _finite_number(
            context.get("asset_2"),
            "Asset 2 price",
            minimum=0.0,
            strict_minimum=True,
        )
        normalized["correlation"] = _finite_number(
            context.get("correlation"),
            "Correlation",
            minimum=-1.0,
            maximum=1.0,
        )
    else:
        raise StructureValidationError(f"Unsupported pricing model: {model}.")

    if model == "asian76":
        averaging_start_date = _as_date(
            context.get("averaging_start_date"),
            "Averaging start date",
        )
        if averaging_start_date < as_of:
            raise StructureValidationError(
                "Averaging start must be on or after the valuation date; "
                "realized fixings are not supported."
            )
        if averaging_start_date > expiration_date:
            raise StructureValidationError(
                "Averaging start must be on or before option expiration."
            )
        normalized["averaging_start_date"] = averaging_start_date.isoformat()
        normalized["time_to_averaging_start"] = (
            averaging_start_date - as_of
        ).days / 365.0

    return normalized


def _normalize_sizing(sizing: dict[str, Any]) -> dict[str, float | int]:
    raw_structure_quantity = sizing.get("structure_quantity")
    if raw_structure_quantity is None or isinstance(raw_structure_quantity, bool):
        raise StructureValidationError(
            "Structure quantity must be a positive whole number."
        )
    structure_quantity = _finite_number(
        raw_structure_quantity,
        "Structure quantity",
        minimum=0.0,
        strict_minimum=True,
    )
    if not structure_quantity.is_integer():
        raise StructureValidationError(
            "Structure quantity must be a positive whole number."
        )
    return {
        "structure_quantity": int(structure_quantity),
        "contract_multiplier": _finite_number(
            sizing.get("contract_multiplier"),
            "Contract multiplier",
            minimum=0.0,
            strict_minimum=True,
        ),
    }


def _normalize_leg(
    model: str,
    raw_leg: dict[str, Any],
    position: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    prefix = f"Leg {position}"
    leg_id = str(raw_leg.get("leg_id") or "").strip()
    if not leg_id:
        raise StructureValidationError(f"{prefix}: leg ID is required.")
    name = str(raw_leg.get("name") or "").strip()
    if not name:
        raise StructureValidationError(f"{prefix}: name is required.")
    if len(name) > 48:
        raise StructureValidationError(f"{prefix}: name must be 48 characters or fewer.")
    side = str(raw_leg.get("side") or "").strip().upper()
    if side not in SIDE_SIGN:
        raise StructureValidationError(f"{prefix}: side must be Buy or Sell.")
    call_put = str(raw_leg.get("call_put") or "").strip().upper()
    if call_put not in {"C", "P"}:
        raise StructureValidationError(f"{prefix}: option type must be Call or Put.")
    ratio = _finite_number(
        raw_leg.get("ratio"),
        f"{prefix} ratio",
        minimum=0.0,
        strict_minimum=True,
    )
    strike = _finite_number(raw_leg.get("strike"), f"{prefix} strike")
    if model in {"black76", "asian76"} and strike <= 0:
        raise StructureValidationError(f"{prefix}: strike must be greater than zero.")
    if model == "kirk" and context["asset_2"] + strike <= 0:
        raise StructureValidationError(
            f"{prefix}: Asset 2 plus spread strike must be greater than zero."
        )

    leg: dict[str, Any] = {
        "leg_id": leg_id,
        "name": name,
        "side": side,
        "side_sign": SIDE_SIGN[side],
        "ratio": ratio,
        "weight": SIDE_SIGN[side] * ratio,
        "call_put": call_put,
        "strike": strike,
    }
    factor = context["vol_adjustment_factor"]

    def normalize_vol(raw_key: str, label: str) -> tuple[float, float]:
        raw_vol = _finite_number(
            raw_leg.get(raw_key),
            f"{prefix} {label}",
            minimum=0.005,
            maximum=2.0,
        )
        used_vol = raw_vol * factor
        if used_vol < 0.005:
            raise StructureValidationError(
                f"{prefix}: {label} is below 0.005 after the contract-date adjustment "
                f"({used_vol:.6f})."
            )
        if used_vol > 2.0:
            raise StructureValidationError(
                f"{prefix}: {label} becomes {used_vol:.6f} after the "
                "contract-date adjustment and is outside the supported 0.005–2.0 range."
            )
        return raw_vol, used_vol

    if model in {"black76", "asian76"}:
        raw_vol, used_vol = normalize_vol("volatility", "input volatility")
        leg["raw_volatility"] = raw_vol
        leg["volatility_used"] = used_vol
    else:
        raw_v1, used_v1 = normalize_vol(
            "volatility_asset_1",
            "Asset 1 input volatility",
        )
        raw_v2, used_v2 = normalize_vol(
            "volatility_asset_2",
            "Asset 2 input volatility",
        )
        leg["raw_volatility_asset_1"] = raw_v1
        leg["raw_volatility_asset_2"] = raw_v2
        leg["volatility_asset_1_used"] = used_v1
        leg["volatility_asset_2_used"] = used_v2
    return leg


def _price_leg(
    model: str,
    context: dict[str, Any],
    leg: dict[str, Any],
) -> tuple[float, dict[str, float | None]]:
    if model == "black76":
        result = black_76(
            leg["call_put"],
            context["forward"],
            leg["strike"],
            context["time_to_expiry"],
            context["rate"],
            leg["volatility_used"],
        )
        value, delta, gamma, theta, vega, rho = result
        greeks = {
            "delta": delta,
            "gamma": gamma,
            "theta": theta,
            "vega": vega * context["vol_adjustment_factor"],
            "rho": rho,
        }
    elif model == "asian76":
        result = asian_76(
            leg["call_put"],
            context["forward"],
            leg["strike"],
            context["time_to_expiry"],
            context["time_to_averaging_start"],
            context["rate"],
            leg["volatility_used"],
        )
        value, delta, gamma, theta, vega, rho = result
        greeks = {
            "delta": delta,
            "gamma": gamma,
            "theta": theta,
            "vega": vega * context["vol_adjustment_factor"],
            "rho": rho,
        }
    else:
        call_put = "call" if leg["call_put"] == "C" else "put"
        value = kirk_model_with_substitution(
            context["asset_1"],
            context["asset_2"],
            leg["strike"],
            leg["volatility_asset_1_used"],
            leg["volatility_asset_2_used"],
            context["correlation"],
            context["time_to_expiry"],
            call_put,
        )
        raw_greeks = kirk_spread_greeks(
            context["asset_1"],
            context["asset_2"],
            leg["strike"],
            leg["volatility_asset_1_used"],
            leg["volatility_asset_2_used"],
            context["correlation"],
            context["time_to_expiry"],
            call_put,
        )
        volatility_factor = context["vol_adjustment_factor"]
        vega_sigma1 = _json_number(raw_greeks.get("vega_sigma1"))
        vega_sigma2 = _json_number(raw_greeks.get("vega_sigma2"))
        greeks = {
            "delta_s1": raw_greeks.get("delta_S1"),
            "delta_s2": raw_greeks.get("delta_S2"),
            "gamma_s1": raw_greeks.get("gamma_S1"),
            "gamma_s2": raw_greeks.get("gamma_S2"),
            "gamma_s1s2": raw_greeks.get("gamma_S1S2"),
            "vega_sigma1": (
                None if vega_sigma1 is None else vega_sigma1 * volatility_factor
            ),
            "vega_sigma2": (
                None if vega_sigma2 is None else vega_sigma2 * volatility_factor
            ),
            "theta": raw_greeks.get("theta"),
            "corr_sensitivity": raw_greeks.get("corr_sensitivity"),
            "vega_equiv": raw_greeks.get("vega_equiv"),
        }

    value = _json_number(value)
    if value is None:
        raise StructureValidationError(f"{leg['name']}: pricing returned a non-finite value.")
    normalized_greeks = {key: _json_number(value) for key, value in greeks.items()}
    return value, normalized_greeks


def _raw_context_from_normalized(model: str, context: dict[str, Any]) -> dict[str, Any]:
    common = {
        "structure_type": context["structure_type"],
        "asset": context["asset"],
        "expiration_date": context["expiration_date"],
        "contract_expiration_date": context["contract_expiration_date"],
    }
    if model in {"black76", "asian76"}:
        common.update({"forward": context["forward"], "rate": context["rate"]})
    else:
        common.update(
            {
                "asset_1": context["asset_1"],
                "asset_2": context["asset_2"],
                "correlation": context["correlation"],
            }
        )
    if model == "asian76":
        common["averaging_start_date"] = context["averaging_start_date"]
    return common


def _raw_leg_from_normalized(model: str, leg: dict[str, Any]) -> dict[str, Any]:
    raw = {
        key: leg[key]
        for key in ("leg_id", "name", "side", "ratio", "call_put", "strike")
    }
    if model in {"black76", "asian76"}:
        raw["volatility"] = leg["raw_volatility"]
    else:
        raw["volatility_asset_1"] = leg["raw_volatility_asset_1"]
        raw["volatility_asset_2"] = leg["raw_volatility_asset_2"]
    return raw


def calculate_structure(
    model: str,
    context: dict[str, Any],
    sizing: dict[str, Any],
    legs: list[dict[str, Any]],
    *,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """Validate and price one complete same-product option structure."""
    if model not in SUPPORTED_MODELS:
        raise StructureValidationError(f"Unsupported pricing model: {model}.")
    calculation_date = (
        _as_date(as_of, "Calculation date") if as_of is not None else date.today()
    )
    if not isinstance(legs, list) or not legs:
        raise StructureValidationError("At least one option leg is required.")
    if len(legs) > MAX_LEGS:
        raise StructureValidationError(
            f"A structure can contain at most {MAX_LEGS} legs."
        )

    normalized_context = _normalize_context(model, context or {}, calculation_date)
    normalized_sizing = _normalize_sizing(sizing or {})
    normalized_legs = [
        _normalize_leg(model, raw_leg or {}, index, normalized_context)
        for index, raw_leg in enumerate(legs, 1)
    ]
    leg_ids = [leg["leg_id"] for leg in normalized_legs]
    if len(set(leg_ids)) != len(leg_ids):
        raise StructureValidationError("Leg IDs must be unique within the structure.")

    position_scale = (
        normalized_sizing["structure_quantity"]
        * normalized_sizing["contract_multiplier"]
    )
    greek_fields = GREEK_FIELDS[model]
    unit_value_total = 0.0
    trade_value_total = 0.0
    priced_legs: list[dict[str, Any]] = []

    for leg in normalized_legs:
        try:
            unit_value, unit_greeks = _price_leg(model, normalized_context, leg)
        except StructureValidationError:
            raise
        except Exception as exc:
            raise StructureValidationError(
                f"{leg['name']}: pricing failed ({type(exc).__name__})."
            ) from exc
        unit_contribution = leg["weight"] * unit_value
        trade_contribution = unit_contribution * position_scale
        unit_greek_contributions = {
            metric: (
                None
                if unit_greeks.get(metric) is None
                else leg["weight"] * unit_greeks[metric]
            )
            for metric in greek_fields
        }
        trade_greek_contributions = {
            metric: (
                None
                if unit_greek_contributions[metric] is None
                else unit_greek_contributions[metric] * position_scale
            )
            for metric in greek_fields
        }
        unit_value_total += unit_contribution
        trade_value_total += trade_contribution
        priced_legs.append(
            {
                **leg,
                "unit": {"value": unit_value, "greeks": unit_greeks},
                "unit_contribution": {
                    "value": unit_contribution,
                    "greeks": unit_greek_contributions,
                },
                "trade_contribution": {
                    "value": trade_contribution,
                    "greeks": trade_greek_contributions,
                },
            }
        )

    unit_greek_totals: dict[str, float | None] = {}
    trade_greek_totals: dict[str, float | None] = {}
    unavailable_metrics: list[str] = []
    for metric in greek_fields:
        unit_values = [
            leg["unit_contribution"]["greeks"].get(metric) for leg in priced_legs
        ]
        trade_values = [
            leg["trade_contribution"]["greeks"].get(metric) for leg in priced_legs
        ]
        if any(value is None for value in unit_values):
            unit_greek_totals[metric] = None
            trade_greek_totals[metric] = None
            unavailable_metrics.append(metric)
        else:
            unit_greek_totals[metric] = float(sum(unit_values))
            trade_greek_totals[metric] = float(sum(trade_values))

    warnings: list[str] = []
    if "theta" in unavailable_metrics and model == "asian76":
        warnings.append(
            "Structure Theta is unavailable because at least one Asian leg cannot "
            "be rolled one calendar day without entering its averaging period."
        )
    unsupported_metrics = ["rho"] if model == "kirk" else []
    if model == "kirk":
        warnings.append(
            "Kirk pricing is undiscounted in the current library; rate sensitivity "
            "and Rho are not applicable."
        )

    raw_context = _raw_context_from_normalized(model, normalized_context)
    raw_legs = [_raw_leg_from_normalized(model, leg) for leg in normalized_legs]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "model_label": MODEL_LABELS[model],
        "calculation_date": calculation_date.isoformat(),
        "context": normalized_context,
        "sizing": {
            **normalized_sizing,
            "position_scale": position_scale,
        },
        "legs": priced_legs,
        "totals": {
            "unit_structure_value": unit_value_total,
            "trade_value": trade_value_total,
            "unit_structure_greeks": unit_greek_totals,
            "trade_greeks": trade_greek_totals,
        },
        "greek_fields": list(greek_fields),
        "greek_labels": {field: GREEK_LABELS[field] for field in greek_fields},
        "unavailable_metrics": unavailable_metrics,
        "unsupported_metrics": unsupported_metrics,
        "warnings": warnings,
        "input": {
            "context": raw_context,
            "sizing": normalized_sizing,
            "legs": raw_legs,
        },
    }
    return snapshot


def _snapshot_input(snapshot: dict[str, Any]) -> tuple[str, dict, dict, list, date]:
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise StructureValidationError("Unsupported or stale pricer snapshot.")
    model = snapshot.get("model")
    payload = snapshot.get("input") or {}
    return (
        model,
        dict(payload.get("context") or {}),
        dict(payload.get("sizing") or {}),
        [dict(leg) for leg in payload.get("legs") or []],
        _as_date(snapshot.get("calculation_date"), "Calculation date"),
    )


def _price_at_state(
    snapshot: dict[str, Any],
    valuation_date: date,
    underlying_value: float,
) -> float:
    model = snapshot["model"]
    context = snapshot["context"]
    expiration_date = _as_date(context["expiration_date"], "Expiration date")
    if valuation_date >= expiration_date:
        return _payoff_at_underlying(snapshot, underlying_value)

    time_to_expiry = max((expiration_date - valuation_date).days / 365.0, 0.001)
    position_scale = snapshot["sizing"]["position_scale"]
    total = 0.0
    if model == "asian76":
        averaging_start = _as_date(
            context["averaging_start_date"],
            "Averaging start date",
        )
        if valuation_date > averaging_start:
            raise StructureValidationError(
                "Asian valuation after averaging starts requires realized fixings "
                "and an accrued average."
            )
        time_to_averaging_start = max(
            (averaging_start - valuation_date).days / 365.0,
            0.0,
        )
    else:
        time_to_averaging_start = None

    for leg in snapshot["legs"]:
        if model == "black76":
            value = black_76(
                leg["call_put"],
                underlying_value,
                leg["strike"],
                time_to_expiry,
                context["rate"],
                leg["volatility_used"],
            )[0]
        elif model == "asian76":
            value = asian_76(
                leg["call_put"],
                underlying_value,
                leg["strike"],
                time_to_expiry,
                time_to_averaging_start,
                context["rate"],
                leg["volatility_used"],
            )[0]
        else:
            value = kirk_model_with_substitution(
                underlying_value,
                context["asset_2"],
                leg["strike"],
                leg["volatility_asset_1_used"],
                leg["volatility_asset_2_used"],
                context["correlation"],
                time_to_expiry,
                "call" if leg["call_put"] == "C" else "put",
            )
        total += leg["weight"] * float(value) * position_scale
    return total


def _payoff_at_underlying(snapshot: dict[str, Any], underlying_value: float) -> float:
    model = snapshot["model"]
    context = snapshot["context"]
    position_scale = snapshot["sizing"]["position_scale"]
    total = 0.0
    for leg in snapshot["legs"]:
        if model == "kirk":
            spread = underlying_value - context["asset_2"]
            intrinsic = (
                max(spread - leg["strike"], 0.0)
                if leg["call_put"] == "C"
                else max(leg["strike"] - spread, 0.0)
            )
        else:
            intrinsic = (
                max(underlying_value - leg["strike"], 0.0)
                if leg["call_put"] == "C"
                else max(leg["strike"] - underlying_value, 0.0)
            )
        total += leg["weight"] * intrinsic * position_scale
    return total


def payoff_series(
    snapshot: dict[str, Any],
    *,
    valuation_date: date | str | None = None,
    price_range: float = 50.0,
    points: int = 101,
) -> dict[str, Any]:
    model, _context, _sizing, _legs, calculation_date = _snapshot_input(snapshot)
    normalized_range = _finite_number(
        price_range,
        "Price range",
        minimum=1.0,
        maximum=200.0,
    )
    if model == "kirk":
        current_underlying = snapshot["context"]["asset_1"]
        axis_title = "Asset 1 price"
    else:
        current_underlying = snapshot["context"]["forward"]
        axis_title = "Forward price"
    price_min = max(0.01, current_underlying * (1.0 - normalized_range / 100.0))
    price_max = current_underlying * (1.0 + normalized_range / 100.0)
    prices = np.linspace(price_min, price_max, max(11, min(int(points), 401)))
    expiration_date = _as_date(
        snapshot["context"]["expiration_date"],
        "Expiration date",
    )
    selected_date = (
        expiration_date
        if valuation_date is None
        else min(_as_date(valuation_date, "Valuation date"), expiration_date)
    )
    if model == "asian76" and selected_date >= expiration_date:
        axis_title = "Final Arithmetic Average"
    if selected_date < calculation_date:
        raise StructureValidationError(
            "Valuation date cannot be before the calculation date."
        )

    payoff_values = [_payoff_at_underlying(snapshot, value) for value in prices]
    theoretical_values = [
        _price_at_state(snapshot, selected_date, value) for value in prices
    ]
    return {
        "x": [float(value) for value in prices],
        "theoretical": [float(value) for value in theoretical_values],
        "payoff": [float(value) for value in payoff_values],
        "current_underlying": current_underlying,
        "current_value": _price_at_state(snapshot, selected_date, current_underlying),
        "valuation_date": selected_date.isoformat(),
        "at_expiration": selected_date >= expiration_date,
        "xaxis_title": axis_title,
    }


def _inclusive_sample_dates(start: date, end: date, max_points: int) -> list[date]:
    if end <= start:
        return [start]
    span = (end - start).days
    step = max(1, math.ceil(span / max(1, max_points - 1)))
    dates = [start + timedelta(days=offset) for offset in range(0, span + 1, step)]
    if dates[-1] != end:
        dates.append(end)
    return dates


def time_decay_series(
    snapshot: dict[str, Any],
    *,
    max_points: int = 60,
) -> dict[str, Any]:
    _model, _context, _sizing, _legs, calculation_date = _snapshot_input(snapshot)
    expiration_date = _as_date(
        snapshot["context"]["expiration_date"],
        "Expiration date",
    )
    end_date = expiration_date
    truncated = False
    if snapshot["model"] == "asian76":
        end_date = _as_date(
            snapshot["context"]["averaging_start_date"],
            "Averaging start date",
        )
        truncated = end_date < expiration_date
    dates = _inclusive_sample_dates(calculation_date, end_date, max_points)
    underlying = (
        snapshot["context"]["asset_1"]
        if snapshot["model"] == "kirk"
        else snapshot["context"]["forward"]
    )
    values = [_price_at_state(snapshot, item, underlying) for item in dates]
    return {
        "dates": [item.isoformat() for item in dates],
        "values": values,
        "truncated_at_averaging_start": truncated,
    }


def _include_base_number(values: Iterable[float], base: float) -> list[float]:
    rounded = {round(float(value), 12) for value in values}
    rounded.add(round(float(base), 12))
    return sorted(rounded)


def parallel_volatility_series(
    snapshot: dict[str, Any],
    *,
    minimum_shift: float = -0.20,
    maximum_shift: float = 0.20,
    points: int = 41,
) -> dict[str, Any]:
    model, context, sizing, legs, calculation_date = _snapshot_input(snapshot)
    shifts = np.linspace(minimum_shift, maximum_shift, max(5, min(points, 101)))
    values: list[float] = []
    valid_shifts: list[float] = []
    for raw_shift in _include_base_number(shifts, 0.0):
        shifted_legs = [dict(leg) for leg in legs]
        for leg in shifted_legs:
            if model in {"black76", "asian76"}:
                leg["volatility"] += raw_shift
            else:
                leg["volatility_asset_1"] += raw_shift
                leg["volatility_asset_2"] += raw_shift
        try:
            result = calculate_structure(
                model,
                context,
                sizing,
                shifted_legs,
                as_of=calculation_date,
            )
        except StructureValidationError:
            continue
        valid_shifts.append(raw_shift * 100.0)
        values.append(result["totals"]["trade_value"])
    return {"shifts_percentage_points": valid_shifts, "values": values}


def rate_sensitivity_series(
    snapshot: dict[str, Any],
    *,
    minimum_rate: float = -0.02,
    maximum_rate: float = 0.15,
    points: int = 41,
) -> dict[str, Any]:
    model, context, sizing, legs, calculation_date = _snapshot_input(snapshot)
    if model == "kirk":
        raise StructureValidationError(
            "Rate sensitivity is not applicable to the undiscounted Kirk model."
        )
    levels = _include_base_number(
        np.linspace(minimum_rate, maximum_rate, max(5, min(points, 101))),
        context["rate"],
    )
    values = []
    for level in levels:
        candidate_context = dict(context)
        candidate_context["rate"] = level
        result = calculate_structure(
            model,
            candidate_context,
            sizing,
            legs,
            as_of=calculation_date,
        )
        values.append(result["totals"]["trade_value"])
    return {"rates": levels, "values": values}


def correlation_sensitivity_series(
    snapshot: dict[str, Any],
    *,
    points: int = 41,
) -> dict[str, Any]:
    model, context, sizing, legs, calculation_date = _snapshot_input(snapshot)
    if model != "kirk":
        raise StructureValidationError(
            "Correlation sensitivity is only available for Kirk structures."
        )
    levels = _include_base_number(
        np.linspace(-1.0, 1.0, max(5, min(points, 101))),
        context["correlation"],
    )
    values = []
    valid_levels = []
    for level in levels:
        candidate_context = dict(context)
        candidate_context["correlation"] = level
        try:
            result = calculate_structure(
                model,
                candidate_context,
                sizing,
                legs,
                as_of=calculation_date,
            )
        except (StructureValidationError, ValueError, FloatingPointError):
            continue
        valid_levels.append(level)
        values.append(result["totals"]["trade_value"])
    return {"correlations": valid_levels, "values": values}


def expiration_extension_series(
    snapshot: dict[str, Any],
    *,
    max_points: int = 41,
) -> dict[str, Any]:
    model, context, sizing, legs, calculation_date = _snapshot_input(snapshot)
    base_expiration = _as_date(context["expiration_date"], "Expiration date")
    contract_expiration = _as_date(
        context["contract_expiration_date"],
        "Contract expiration date",
    )
    start = calculation_date + timedelta(days=1)
    if model == "asian76":
        start = max(
            start,
            _as_date(context["averaging_start_date"], "Averaging start date"),
        )
    dates = _inclusive_sample_dates(start, contract_expiration, max_points)
    if base_expiration not in dates:
        dates.append(base_expiration)
        dates.sort()
    valid_dates: list[str] = []
    values: list[float] = []
    for candidate_date in dates:
        candidate_context = dict(context)
        candidate_context["expiration_date"] = candidate_date.isoformat()
        try:
            result = calculate_structure(
                model,
                candidate_context,
                sizing,
                legs,
                as_of=calculation_date,
            )
        except StructureValidationError:
            continue
        valid_dates.append(candidate_date.isoformat())
        values.append(result["totals"]["trade_value"])
    return {
        "dates": valid_dates,
        "values": values,
        "base_expiration": base_expiration.isoformat(),
    }
