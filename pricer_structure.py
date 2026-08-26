"""Pure pricing and scenario helpers for the multi-leg option structure pricer.

The module deliberately has no Dash imports.  It accepts JSON-shaped inputs,
validates the entire structure, prices every leg through the existing options
library, and returns a versioned JSON-safe calculation snapshot.
"""

from __future__ import annotations

import csv
from functools import lru_cache
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import brentq

from options import option_expiry_engine as expiry_engine_module
from options.options_library import (
    asian_76,
    black_76,
    black_76_futures_style,
    kirk_model_with_substitution,
    kirk_spread_greeks,
)
from options.option_expiry_engine import (
    business_days_between,
    get_business_calendar,
    get_surface_calendar_mapping,
    resolve_surface_expiry,
    validate_expiry_records,
)


SCHEMA_VERSION = 13
MAX_LEGS = 20
SUPPORTED_MODELS = {"black76", "asian76", "kirk"}
SUPPORTED_PREMIUM_CONVENTIONS = ("futures_style", "upfront")
PREMIUM_CONVENTION_LABELS = {
    "futures_style": "Futures-style",
    "upfront": "Upfront",
}
SUPPORTED_ASSETS = ("TTF", "JKM", "HH", "Brent", "NBP")
DEFAULT_ASSET = "TTF"
ASSET_DEFAULT_PREMIUM_CONVENTIONS = {
    "TTF": "futures_style",
    "JKM": "futures_style",
    "HH": "upfront",
    "Brent": "futures_style",
    "NBP": "futures_style",
}
ASSET_PRICE_SPECS = {
    "TTF": {
        "currency": "EUR",
        "unit": "MWh",
        "price_unit_label": "EUR/MWh",
        "description": "Euro per megawatt-hour.",
    },
    "JKM": {
        "currency": "USD",
        "unit": "MMBtu",
        "price_unit_label": "USD/MMBtu",
        "description": "US dollars per million British thermal units.",
    },
    "HH": {
        "currency": "USD",
        "unit": "MMBtu",
        "price_unit_label": "USD/MMBtu",
        "description": "US dollars per million British thermal units.",
    },
    "Brent": {
        "currency": "USD",
        "unit": "bbl",
        "price_unit_label": "USD/bbl",
        "description": "US dollars per barrel.",
    },
    "NBP": {
        "currency": "GBP",
        "unit": "therm",
        "price_unit_label": "GBp/therm",
        "description": "Pence sterling per therm; the underlying currency is GBP.",
    },
}
JKM_VARIANCE_CALENDAR = "ICE_JKM_71090519_TRADING"
TTF_VARIANCE_CALENDAR = "ICE_TTF_TFO_TRADING"
JKM_APO_PRODUCT_CODE = "JKM"
JKM_APO_PRODUCT_ID = "71090519"
JKM_APO_PRODUCT_NAME = "JKM LNG (Platts) Average Price Options"
JKM_VANILLA_PRODUCT_CODE = "JKZ"
JKM_VANILLA_PRODUCT_NAME = "JKM LNG (Platts) Options"
JKM_VANILLA_EXPIRY_CONVENTION = "ICE_JKM_JKZ_VIA_TFO"
JKM_VANILLA_EXPIRY_VERSION = "ICE_JKM_JKZ_VIA_TFO_RULE_V1"
JKM_CONTRACT_SIZE_MMBTU = 10_000
BRENT_CONTRACT_SIZE_BBL = 1_000
HH_CONTRACT_SIZE_MMBTU = 2_500
NBP_CONTRACT_SIZE_THERMS_PER_DAY = 1_000
OPTION_DAY_COUNT_DENOMINATOR = 365.25
JKM_DAY_COUNT_DENOMINATOR = OPTION_DAY_COUNT_DENOMINATOR
TTF_DAY_COUNT_DENOMINATOR = OPTION_DAY_COUNT_DENOMINATOR
MAX_OPTION_HORIZON_DAYS = int(100 * OPTION_DAY_COUNT_DENOMINATOR)
OPTION_EXPIRY_SNAPSHOT = (
    Path(expiry_engine_module.__file__).resolve().parent
    / "data"
    / "option_contract_expiries.csv"
)
# Compatibility alias retained for callers/tests that patch the former name.
TTF_EXPIRY_SNAPSHOT = OPTION_EXPIRY_SNAPSHOT
TTF_DELIVERY_TIMEZONE = ZoneInfo("Europe/Amsterdam")
SUPPORTED_DELIVERY_SHAPES = ("MONTH", "Q1", "Q2", "Q3", "Q4", "SUM", "WIN")
DELIVERY_SHAPE_LABELS = {
    "MONTH": "Month",
    "Q1": "Q1 (Jan-Mar)",
    "Q2": "Q2 (Apr-Jun)",
    "Q3": "Q3 (Jul-Sep)",
    "Q4": "Q4 (Oct-Dec)",
    "SUM": "Summer (Apr-Sep)",
    "WIN": "Winter (Oct-Mar)",
}
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
    "vega": "Vega (contract vol, 1 point)",
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
    *,
    asset: str | None = None,
) -> tuple[float, int, int]:
    if asset:
        try:
            variance_calendar = get_surface_calendar_mapping(
                asset
            ).variance_calendar_code
        except Exception as exc:
            raise StructureValidationError(
                f"No governed variance calendar is available for {asset}."
            ) from exc
        option_business_days = business_days_between(
            as_of,
            expiration_date,
            variance_calendar,
        )
        contract_business_days = business_days_between(
            as_of,
            contract_expiration_date,
            variance_calendar,
        )
    else:
        option_business_days = count_business_days(as_of, expiration_date)
        contract_business_days = count_business_days(as_of, contract_expiration_date)
    if option_business_days <= 0:
        raise StructureValidationError(
            "Expiration must leave at least one governed exchange trading day "
            "after the valuation date."
        )
    if contract_business_days <= 0:
        raise StructureValidationError(
            "Contract expiration must leave at least one governed exchange "
            "trading day after the valuation date."
        )
    factor = math.sqrt(option_business_days / contract_business_days)
    return factor, option_business_days, contract_business_days


@lru_cache(maxsize=16)
def _load_expiry_records_cached(
    snapshot_path: str,
    modified_ns: int,
    size: int,
    asset: str,
) -> tuple[Any, ...]:
    """Load one governed asset expiry set once per snapshot revision."""
    del modified_ns, size
    mapping = get_surface_calendar_mapping(asset)
    with Path(snapshot_path).open(newline="", encoding="utf-8") as source:
        rows = [
            row
            for row in csv.DictReader(source)
            if row.get("expiry_convention_code") == mapping.expiry_convention_code
        ]
    return validate_expiry_records(mapping.expiry_convention_code, rows)


def _asset_expiry_records(asset: str) -> tuple[Any, ...]:
    normalized_asset = str(asset or "").strip()
    if normalized_asset not in SUPPORTED_ASSETS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    snapshot = TTF_EXPIRY_SNAPSHOT if normalized_asset == "TTF" else OPTION_EXPIRY_SNAPSHOT
    try:
        identity = snapshot.stat()
    except OSError as exc:
        raise StructureValidationError(
            f"The governed {normalized_asset} option-expiry snapshot is unavailable."
        ) from exc
    try:
        return _load_expiry_records_cached(
            str(snapshot),
            identity.st_mtime_ns,
            identity.st_size,
            normalized_asset,
        )
    except Exception as exc:
        raise StructureValidationError(
            f"The governed {normalized_asset} option-expiry snapshot could not be validated."
        ) from exc


def _ttf_expiry_records() -> tuple[Any, ...]:
    return _asset_expiry_records("TTF")


def _jkm_expiry_records() -> tuple[Any, ...]:
    return _asset_expiry_records("JKM")


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    return date(month_index // 12, month_index % 12 + 1, 1)


def _delivery_months(shape: str, delivery_year: int) -> tuple[date, ...]:
    starts_and_lengths = {
        "Q1": (1, 3),
        "Q2": (4, 3),
        "Q3": (7, 3),
        "Q4": (10, 3),
        "SUM": (4, 6),
        "WIN": (10, 6),
    }
    try:
        start_month, length = starts_and_lengths[shape]
    except KeyError as exc:
        raise StructureValidationError(
            "Delivery shape must be Month, Q1-Q4, Summer, or Winter."
        ) from exc
    start = date(delivery_year, start_month, 1)
    return tuple(_add_months(start, offset) for offset in range(length))


def _ttf_delivery_hours(contract_month: date) -> int:
    local_start = datetime(
        contract_month.year,
        contract_month.month,
        1,
        tzinfo=TTF_DELIVERY_TIMEZONE,
    )
    next_month = _add_months(contract_month, 1)
    local_end = datetime(
        next_month.year,
        next_month.month,
        1,
        tzinfo=TTF_DELIVERY_TIMEZONE,
    )
    elapsed = local_end.astimezone(timezone.utc) - local_start.astimezone(timezone.utc)
    return int(elapsed.total_seconds() // 3600)


def _delivery_period_label(shape: str, delivery_year: int) -> str:
    short_year = delivery_year % 100
    if shape == "WIN":
        return f"WIN{short_year:02d}/{(short_year + 1) % 100:02d}"
    if shape == "SUM":
        return f"SUM{short_year:02d}"
    return f"{shape}-{short_year:02d}"


def default_model_for_asset(asset: str) -> str:
    """Select the default model that identifies the primary exchange product."""
    normalized_asset = str(asset or DEFAULT_ASSET).strip()
    if normalized_asset not in SUPPORTED_ASSETS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    return "asian76" if normalized_asset == "JKM" else "black76"


def _jkm_product_metadata(model: str) -> dict[str, Any]:
    if model == "asian76":
        return {
            "exchange_product_code": JKM_APO_PRODUCT_CODE,
            "exchange_product_id": JKM_APO_PRODUCT_ID,
            "exchange_product_name": JKM_APO_PRODUCT_NAME,
            "exchange_expiry_rule": "15th calendar day of the prior month, preceding business day",
        }
    if model == "black76":
        return {
            "exchange_product_code": JKM_VANILLA_PRODUCT_CODE,
            "exchange_product_id": None,
            "exchange_product_name": JKM_VANILLA_PRODUCT_NAME,
            "exchange_expiry_rule": (
                "TFO expiry immediately before the JKM future becomes front month"
            ),
        }
    return {}


def _roll_following(calendar_code: str, value: date) -> date:
    calendar = get_business_calendar(calendar_code)
    result = value
    while not calendar.is_business_day(result):
        result += timedelta(days=1)
    return result


def _jkm_apo_averaging_start(contract_month: date) -> date:
    anchor_month = _add_months(contract_month, -2)
    return _roll_following(
        JKM_VARIANCE_CALENDAR,
        date(anchor_month.year, anchor_month.month, 16),
    )


def available_jkm_apo_delivery_months(as_of: date) -> tuple[date, ...]:
    """Return governed, fully unseasoned JKM APO delivery months."""
    return tuple(
        record.contract_month
        for record in _jkm_expiry_records()
        if record.option_expiration_date > as_of
        and _jkm_apo_averaging_start(record.contract_month) >= as_of
    )


def available_delivery_months(
    asset: str,
    model: str,
    as_of: date,
) -> tuple[date, ...]:
    """Return unexpired governed monthly contracts for an asset/model."""
    normalized_asset = str(asset or "").strip()
    if normalized_asset not in SUPPORTED_ASSETS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    if model not in SUPPORTED_MODELS:
        raise StructureValidationError(f"Unsupported pricing model: {model}.")
    if normalized_asset == "JKM" and model == "asian76":
        return available_jkm_apo_delivery_months(as_of)
    if normalized_asset == "JKM" and model == "black76":
        return tuple(
            _add_months(record.contract_month, 2)
            for record in _ttf_expiry_records()
            if record.option_expiration_date > as_of
        )
    return tuple(
        record.contract_month
        for record in _asset_expiry_records(normalized_asset)
        if record.option_expiration_date > as_of
    )


def build_jkm_month_component(
    contract_month: date | str,
    as_of: date,
    forward: float,
    model: str = "asian76",
) -> dict[str, Any]:
    """Resolve one governed JKM APO or JKZ monthly delivery component."""
    if model not in {"black76", "asian76"}:
        raise StructureValidationError(
            "JKM monthly deliveries require Asian-76 or Black-76."
        )
    raw_month = _as_date(contract_month, "Delivery month")
    month = date(raw_month.year, raw_month.month, 1)
    if model == "asian76":
        resolved = resolve_surface_expiry("JKM", month, _jkm_expiry_records())
        averaging_start = _jkm_apo_averaging_start(month)
        if averaging_start < as_of:
            raise StructureValidationError(
                f"{month.strftime('%b-%y')} cannot be priced as an unseasoned "
                "JKM Average Price Option because its averaging period has started "
                "and realized fixings are required."
            )
        expiry_code = resolved.expiry_convention_code
        expiry_version = resolved.expiry_convention_version
        expiry_status = resolved.expiry_status
        option_expiration = resolved.option_expiration_date
    else:
        # ICE JKZ expires with the TFO immediately before the JKM future
        # becomes front month. A JKM delivery month becomes front after the
        # JKM future two contract months earlier completes, so the governed
        # TFO month is delivery month minus two.
        tfo_month = _add_months(month, -2)
        resolved = resolve_surface_expiry("TTF", tfo_month, _ttf_expiry_records())
        averaging_start = None
        option_expiration = resolved.option_expiration_date
        expiry_code = JKM_VANILLA_EXPIRY_CONVENTION
        expiry_version = JKM_VANILLA_EXPIRY_VERSION
        expiry_status = f"TFO {resolved.expiry_status}"
    if option_expiration <= as_of:
        raise StructureValidationError(
            f"{month.strftime('%b-%y')} cannot be priced because its monthly "
            "option has expired."
        )
    component = {
        "contract_month": month.isoformat(),
        "contract_month_label": month.strftime("%b-%y"),
        "option_expiration_date": option_expiration.isoformat(),
        "contract_expiration_date": option_expiration.isoformat(),
        "expiry_status": expiry_status,
        "expiry_convention_code": expiry_code,
        "expiry_convention_version": expiry_version,
        "variance_calendar_code": JKM_VARIANCE_CALENDAR,
        "time_to_expiry": (
            option_expiration - as_of
        ).days / JKM_DAY_COUNT_DENOMINATOR,
        "contract_size": JKM_CONTRACT_SIZE_MMBTU,
        "weight": 1.0,
        "forward": forward,
        **_jkm_product_metadata(model),
    }
    if averaging_start is not None:
        component["averaging_start_date"] = averaging_start.isoformat()
        component["averaging_end_date"] = option_expiration.isoformat()
        component["time_to_averaging_start"] = (
            averaging_start - as_of
        ).days / JKM_DAY_COUNT_DENOMINATOR
    return component


def build_delivery_month_component(
    asset: str,
    model: str,
    contract_month: date | str,
    as_of: date,
    forward: float = 1.0,
) -> dict[str, Any]:
    """Resolve the exchange option expiry for one selected delivery month.

    The exchange expiry is the volatility-contract anchor.  Callers may keep a
    separate OTC option expiry for Black-76, Asian-76, or Kirk and apply the
    existing expiry adjustment against this governed contract date.
    """
    normalized_asset = str(asset or "").strip()
    if normalized_asset not in SUPPORTED_ASSETS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    if model not in SUPPORTED_MODELS:
        raise StructureValidationError(f"Unsupported pricing model: {model}.")
    if normalized_asset == "JKM" and model in {"black76", "asian76"}:
        return build_jkm_month_component(
            contract_month,
            as_of,
            forward,
            model,
        )

    raw_month = _as_date(contract_month, "Delivery month")
    month = date(raw_month.year, raw_month.month, 1)
    resolved = resolve_surface_expiry(
        normalized_asset,
        month,
        _asset_expiry_records(normalized_asset),
    )
    if resolved.option_expiration_date <= as_of:
        raise StructureValidationError(
            f"{month.strftime('%b-%y')} cannot be priced because its monthly "
            "option has expired."
        )
    mapping = get_surface_calendar_mapping(normalized_asset)
    return {
        "contract_month": month.isoformat(),
        "contract_month_label": month.strftime("%b-%y"),
        "option_expiration_date": resolved.option_expiration_date.isoformat(),
        "contract_expiration_date": resolved.option_expiration_date.isoformat(),
        "expiry_status": resolved.expiry_status,
        "expiry_convention_code": resolved.expiry_convention_code,
        "expiry_convention_version": resolved.expiry_convention_version,
        "variance_calendar_code": mapping.variance_calendar_code,
        "time_to_expiry": (
            resolved.option_expiration_date - as_of
        ).days / OPTION_DAY_COUNT_DENOMINATOR,
        "weight": 1.0,
        "forward": forward,
    }


def build_jkm_strip_components(
    shape: str,
    delivery_year: int,
    as_of: date,
    forward: float,
    model: str,
) -> tuple[list[dict[str, Any]], int]:
    """Build equal-lot JKM APO or JKZ monthly components for a delivery strip."""
    if model not in {"black76", "asian76"}:
        raise StructureValidationError(
            "JKM delivery strips require Asian-76 or Black-76."
        )
    months = _delivery_months(shape, delivery_year)
    component_count = len(months)
    components: list[dict[str, Any]] = []
    for month in months:
        component = build_jkm_month_component(month, as_of, forward, model)
        component["weight"] = 1.0 / component_count
        components.append(component)
    return components, component_count * JKM_CONTRACT_SIZE_MMBTU


def build_ttf_strip_components(
    shape: str,
    delivery_year: int,
    as_of: date,
    forward: float,
) -> tuple[list[dict[str, Any]], int]:
    """Resolve exact monthly TFO expiries and delivery-hour weights for a strip."""
    known_expiries = _ttf_expiry_records()
    components: list[dict[str, Any]] = []
    total_hours = sum(_ttf_delivery_hours(month) for month in _delivery_months(shape, delivery_year))
    for month in _delivery_months(shape, delivery_year):
        resolved = resolve_surface_expiry("TTF", month, known_expiries)
        if resolved.option_expiration_date <= as_of:
            raise StructureValidationError(
                f"{_delivery_period_label(shape, delivery_year)} cannot be priced as "
                "a complete strip because at least one monthly option has expired."
            )
        delivery_hours = _ttf_delivery_hours(month)
        components.append(
            {
                "contract_month": month.isoformat(),
                "contract_month_label": month.strftime("%b-%y"),
                "option_expiration_date": resolved.option_expiration_date.isoformat(),
                "expiry_status": resolved.expiry_status,
                "expiry_convention_code": resolved.expiry_convention_code,
                "expiry_convention_version": resolved.expiry_convention_version,
                "variance_calendar_code": TTF_VARIANCE_CALENDAR,
                "time_to_expiry": (
                    resolved.option_expiration_date - as_of
                ).days
                / TTF_DAY_COUNT_DENOMINATOR,
                "delivery_hours": delivery_hours,
                "weight": delivery_hours / total_hours,
                "forward": forward,
            }
        )
    return components, total_hours


def _default_dates(as_of: date) -> tuple[str, str]:
    expiration = as_of + timedelta(days=30)
    return expiration.isoformat(), expiration.isoformat()


def default_premium_convention(asset: str, model: str | None = None) -> str:
    """Return the concrete premium convention selected for an asset/model."""
    normalized_asset = str(asset or DEFAULT_ASSET).strip()
    if normalized_asset not in ASSET_DEFAULT_PREMIUM_CONVENTIONS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    if model is not None and model not in SUPPORTED_MODELS:
        raise StructureValidationError(f"Unsupported pricing model: {model}.")
    # The current Kirk implementation is undiscounted, so it supports only the
    # futures-style convention even when the selected asset normally defaults
    # to an upfront premium.
    if model == "kirk":
        return "futures_style"
    return ASSET_DEFAULT_PREMIUM_CONVENTIONS[normalized_asset]


def asset_price_spec(asset: str) -> dict[str, str]:
    """Return governed currency and price-unit metadata for an asset."""
    normalized_asset = str(asset or DEFAULT_ASSET).strip()
    if normalized_asset not in ASSET_PRICE_SPECS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    return dict(ASSET_PRICE_SPECS[normalized_asset])


def default_contract_size(
    asset: str,
    context: dict[str, Any] | None = None,
    *,
    as_of: date | None = None,
) -> float:
    """Return the exchange-sized quantity used to scale one structure lot.

    TTF option prices are quoted per MWh, so one 1 MW exchange lot is converted
    to the exact delivery-period MWh, including daylight-saving hours. JKM uses
    10,000 MMBtu for each monthly contract in the selected delivery period.
    Brent and Henry Hub use their fixed exchange lot sizes. NBP is quoted per
    therm and one exchange lot is 1,000 therms per calendar delivery day.
    """
    normalized_asset = str(asset or DEFAULT_ASSET).strip()
    if normalized_asset not in SUPPORTED_ASSETS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    if normalized_asset == "Brent":
        return float(BRENT_CONTRACT_SIZE_BBL)
    if normalized_asset == "HH":
        return float(HH_CONTRACT_SIZE_MMBTU)

    resolved_context = context if isinstance(context, dict) else {}
    raw_shape = resolved_context.get("delivery_shape", "MONTH")
    shape = str(raw_shape or "MONTH").strip().upper()
    if shape not in SUPPORTED_DELIVERY_SHAPES:
        raise StructureValidationError(
            "Delivery shape must be Month, Q1-Q4, Summer, or Winter."
        )

    if normalized_asset == "JKM":
        month_count = 1
        if shape != "MONTH":
            raw_year = _finite_number(
                resolved_context.get("delivery_year"),
                "Delivery year",
                minimum=2000,
                maximum=2100,
            )
            if not raw_year.is_integer():
                raise StructureValidationError("Delivery year must be a whole year.")
            month_count = len(_delivery_months(shape, int(raw_year)))
        return float(month_count * JKM_CONTRACT_SIZE_MMBTU)

    if normalized_asset == "NBP":
        reference_date = as_of or date.today()
        if resolved_context.get("delivery_month"):
            delivery_month = _as_date(
                resolved_context["delivery_month"],
                "Delivery month",
            ).replace(day=1)
        else:
            expiration = _as_date(
                resolved_context.get(
                    "expiration_date",
                    reference_date + timedelta(days=30),
                ),
                "Expiration date",
            )
            delivery_month = _add_months(
                date(expiration.year, expiration.month, 1),
                1,
            )
        calendar_days = (_add_months(delivery_month, 1) - delivery_month).days
        return float(NBP_CONTRACT_SIZE_THERMS_PER_DAY * calendar_days)

    if shape == "MONTH":
        if resolved_context.get("delivery_month"):
            delivery_month = _as_date(
                resolved_context["delivery_month"],
                "Delivery month",
            ).replace(day=1)
            return float(_ttf_delivery_hours(delivery_month))
        reference_date = as_of or date.today()
        expiration = _as_date(
            resolved_context.get(
                "expiration_date",
                reference_date + timedelta(days=30),
            ),
            "Expiration date",
        )
        delivery_month = _add_months(date(expiration.year, expiration.month, 1), 1)
        return float(_ttf_delivery_hours(delivery_month))

    raw_year = _finite_number(
        resolved_context.get("delivery_year"),
        "Delivery year",
        minimum=2000,
        maximum=2100,
    )
    if not raw_year.is_integer():
        raise StructureValidationError("Delivery year must be a whole year.")
    return float(
        sum(
            _ttf_delivery_hours(month)
            for month in _delivery_months(shape, int(raw_year))
        )
    )


def default_context(model: str, as_of: date | None = None) -> dict[str, Any]:
    as_of = as_of or date.today()
    expiration, contract_expiration = _default_dates(as_of)
    if model == "black76":
        return {
            "asset": DEFAULT_ASSET,
            "premium_convention": default_premium_convention(DEFAULT_ASSET, model),
            "delivery_shape": "MONTH",
            "delivery_year": as_of.year + 1,
            "forward": 100.0,
            "rate": 0.05,
            "expiration_date": expiration,
            "contract_expiration_date": contract_expiration,
        }
    if model == "asian76":
        return {
            "asset": DEFAULT_ASSET,
            "premium_convention": default_premium_convention(DEFAULT_ASSET, model),
            "delivery_shape": "MONTH",
            "delivery_year": as_of.year + 1,
            "forward": 100.0,
            "rate": 0.05,
            "averaging_start_date": (as_of + timedelta(days=7)).isoformat(),
            "expiration_date": expiration,
            "contract_expiration_date": contract_expiration,
        }
    if model == "kirk":
        return {
            "asset": DEFAULT_ASSET,
            "premium_convention": default_premium_convention(DEFAULT_ASSET, model),
            "delivery_shape": "MONTH",
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
        common["quote_basis"] = "VOL"
        common["quote_value"] = 0.2
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
    raw_asset = context.get("asset", DEFAULT_ASSET)
    if raw_asset is None:
        raise StructureValidationError("Asset is required.")
    asset = str(raw_asset).strip()
    if asset not in SUPPORTED_ASSETS:
        supported = ", ".join(SUPPORTED_ASSETS)
        raise StructureValidationError(f"Asset must be one of: {supported}.")
    price_spec = asset_price_spec(asset)

    raw_premium_convention = context.get(
        "premium_convention", default_premium_convention(asset, model)
    )
    premium_convention = str(raw_premium_convention or "").strip().lower()
    if premium_convention not in SUPPORTED_PREMIUM_CONVENTIONS:
        supported = ", ".join(
            PREMIUM_CONVENTION_LABELS[value]
            for value in SUPPORTED_PREMIUM_CONVENTIONS
        )
        raise StructureValidationError(
            f"Premium convention must be one of: {supported}."
        )
    resolved_premium_convention = premium_convention
    if model == "kirk" and resolved_premium_convention == "upfront":
        raise StructureValidationError(
            "Upfront is not supported for Kirk because the current Kirk "
            "implementation is undiscounted. Use Futures-style."
        )

    raw_delivery_shape = context.get("delivery_shape", "MONTH")
    delivery_shape = str(raw_delivery_shape or "").strip().upper()
    if delivery_shape not in SUPPORTED_DELIVERY_SHAPES:
        raise StructureValidationError(
            "Delivery shape must be Month, Q1-Q4, Summer, or Winter."
        )
    is_delivery_strip = delivery_shape != "MONTH"
    strip_is_supported = (
        (asset == "TTF" and model == "black76")
        or (asset == "JKM" and model in {"black76", "asian76"})
    )
    if is_delivery_strip and not strip_is_supported:
        raise StructureValidationError(
            "Quarter, Summer, and Winter strips require TTF Black-76 or JKM "
            "Asian-76/Black-76."
        )

    if model in {"black76", "asian76"}:
        forward = _finite_number(
            context.get("forward"),
            "Forward price",
            minimum=0.0,
            strict_minimum=True,
        )
    else:
        forward = None

    governed_month_component = None
    if not is_delivery_strip and context.get("delivery_month"):
        governed_month_component = build_delivery_month_component(
            asset,
            model,
            context["delivery_month"],
            as_of,
            float(forward) if forward is not None else 1.0,
        )

    if is_delivery_strip:
        raw_delivery_year = _finite_number(
            context.get("delivery_year"),
            "Delivery year",
            minimum=2000,
            maximum=2100,
        )
        if not raw_delivery_year.is_integer():
            raise StructureValidationError("Delivery year must be a whole year.")
        delivery_year = int(raw_delivery_year)
        if asset == "TTF":
            delivery_components, delivery_total_quantity = build_ttf_strip_components(
                delivery_shape,
                delivery_year,
                as_of,
                float(forward),
            )
            component_weight_basis = "delivery_hours"
            component_quantity_label = "delivery hours"
            variance_calendar_code = TTF_VARIANCE_CALENDAR
            day_count_denominator = TTF_DAY_COUNT_DENOMINATOR
            strip_quantity_fields = {
                "delivery_total_hours": delivery_total_quantity,
            }
            product_metadata: dict[str, Any] = {}
        else:
            delivery_components, delivery_total_quantity = build_jkm_strip_components(
                delivery_shape,
                delivery_year,
                as_of,
                float(forward),
                model,
            )
            component_weight_basis = "equal_contract_lots"
            component_quantity_label = "MMBtu across monthly lots"
            variance_calendar_code = JKM_VARIANCE_CALENDAR
            day_count_denominator = JKM_DAY_COUNT_DENOMINATOR
            strip_quantity_fields = {
                "delivery_total_quantity": delivery_total_quantity,
                "monthly_contract_size": JKM_CONTRACT_SIZE_MMBTU,
            }
            product_metadata = _jkm_product_metadata(model)
        first_expiration = min(
            _as_date(item["option_expiration_date"], "Component expiration date")
            for item in delivery_components
        )
        last_expiration = max(
            _as_date(item["option_expiration_date"], "Component expiration date")
            for item in delivery_components
        )
        weighted_time_to_expiry = sum(
            item["weight"] * item["time_to_expiry"]
            for item in delivery_components
        )
        normalized_strip = {
            "asset": asset,
            "price_unit_label": price_spec["price_unit_label"],
            "trade_currency": price_spec["currency"],
            "premium_convention": premium_convention,
            "premium_convention_label": PREMIUM_CONVENTION_LABELS[
                premium_convention
            ],
            "resolved_premium_convention": resolved_premium_convention,
            "resolved_premium_convention_label": PREMIUM_CONVENTION_LABELS[
                resolved_premium_convention
            ],
            "delivery_shape": delivery_shape,
            "delivery_year": delivery_year,
            "delivery_period_label": _delivery_period_label(
                delivery_shape, delivery_year
            ),
            "delivery_components": delivery_components,
            "delivery_component_count": len(delivery_components),
            "component_weight_basis": component_weight_basis,
            "component_quantity_label": component_quantity_label,
            **strip_quantity_fields,
            "first_expiration_date": first_expiration.isoformat(),
            "last_expiration_date": last_expiration.isoformat(),
            # Compatibility aliases for existing charts and result renderers.
            "expiration_date": last_expiration.isoformat(),
            "contract_expiration_date": last_expiration.isoformat(),
            "time_to_expiry": weighted_time_to_expiry,
            "vol_adjustment_factor": 1.0,
            "option_business_days": None,
            "contract_business_days": None,
            "day_count_denominator": day_count_denominator,
            "day_count_basis": "ACT/365.25",
            "variance_calendar_code": variance_calendar_code,
            "vega_basis": "input_vol",
            "margin_style": resolved_premium_convention,
            "forward": forward,
            "rate": (
                0.0
                if resolved_premium_convention == "futures_style"
                else _finite_number(
                    context.get("rate"),
                    "Risk-free rate",
                    minimum=-1.0,
                    maximum=2.0,
                )
            ),
            **product_metadata,
        }
        if model == "asian76":
            first_averaging_start = min(
                _as_date(
                    item["averaging_start_date"],
                    "Component averaging start date",
                )
                for item in delivery_components
            )
            normalized_strip["averaging_start_date"] = (
                first_averaging_start.isoformat()
            )
            normalized_strip["first_averaging_start_date"] = (
                first_averaging_start.isoformat()
            )
            normalized_strip["time_to_averaging_start"] = (
                first_averaging_start - as_of
            ).days / day_count_denominator
        return normalized_strip

    expiration_date = _as_date(
        (
            governed_month_component["option_expiration_date"]
            if governed_month_component
            and asset == "JKM"
            and model == "asian76"
            else context.get("expiration_date")
        ),
        "Expiration date",
    )
    contract_expiration_date = _as_date(
        (
            governed_month_component["contract_expiration_date"]
            if governed_month_component
            else context.get("contract_expiration_date")
        ),
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
    if (expiration_date - as_of).days > MAX_OPTION_HORIZON_DAYS:
        raise StructureValidationError(
            "Expiration date must be within the supported 100-year horizon."
        )
    if (contract_expiration_date - as_of).days > MAX_OPTION_HORIZON_DAYS:
        raise StructureValidationError(
            "Contract expiration date must be within the supported 100-year horizon."
        )

    factor, option_business_days, contract_business_days = volatility_adjustment(
        as_of,
        expiration_date,
        contract_expiration_date,
        asset=asset,
    )
    day_count_denominator = OPTION_DAY_COUNT_DENOMINATOR
    time_to_expiry = (expiration_date - as_of).days / day_count_denominator
    variance_calendar_code = get_surface_calendar_mapping(
        asset
    ).variance_calendar_code
    is_futures_style = resolved_premium_convention == "futures_style"
    normalized: dict[str, Any] = {
        "asset": asset,
        "price_unit_label": price_spec["price_unit_label"],
        "trade_currency": price_spec["currency"],
        "premium_convention": premium_convention,
        "premium_convention_label": PREMIUM_CONVENTION_LABELS[premium_convention],
        "resolved_premium_convention": resolved_premium_convention,
        "resolved_premium_convention_label": PREMIUM_CONVENTION_LABELS[
            resolved_premium_convention
        ],
        "delivery_shape": delivery_shape,
        "delivery_year": context.get("delivery_year"),
        "expiration_date": expiration_date.isoformat(),
        "contract_expiration_date": contract_expiration_date.isoformat(),
        "time_to_expiry": time_to_expiry,
        "vol_adjustment_factor": factor,
        "option_business_days": option_business_days,
        "contract_business_days": contract_business_days,
        "day_count_denominator": day_count_denominator,
        "day_count_basis": "ACT/365.25",
        "variance_calendar_code": variance_calendar_code,
        "vega_basis": (
            "adjusted_pricing_vol" if asset == "JKM" else "input_vol"
        ),
        "margin_style": resolved_premium_convention,
    }
    if asset == "JKM":
        normalized.update(_jkm_product_metadata(model))
    if governed_month_component:
        normalized.update(
            {
                "delivery_month": governed_month_component["contract_month"],
                "delivery_period_label": governed_month_component[
                    "contract_month_label"
                ],
                "expiry_status": governed_month_component["expiry_status"],
                "expiry_convention_code": governed_month_component[
                    "expiry_convention_code"
                ],
                "expiry_convention_version": governed_month_component[
                    "expiry_convention_version"
                ],
            }
        )
        if "averaging_end_date" in governed_month_component:
            normalized["averaging_end_date"] = governed_month_component[
                "averaging_end_date"
            ]

    if model in {"black76", "asian76"}:
        normalized["forward"] = forward
        normalized["rate"] = (
            0.0
            if is_futures_style
            else _finite_number(
                context.get("rate"),
                "Risk-free rate",
                minimum=-1.0,
                maximum=2.0,
            )
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
        if context.get("correlation") is None:
            raise StructureValidationError(
                "Correlation is required and must be between -1 and 1."
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
            (
                governed_month_component["averaging_start_date"]
                if governed_month_component
                else context.get("averaging_start_date")
            ),
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
        ).days / day_count_denominator

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
            "Contract size",
            minimum=0.0,
            strict_minimum=True,
        ),
    }


def _sizing_scales(
    context: dict[str, Any],
    sizing: dict[str, float | int],
) -> tuple[float, float, float]:
    """Return physical quantity, quote-currency conversion, and value scale."""
    quantity_scale = (
        float(sizing["structure_quantity"])
        * float(sizing["contract_multiplier"])
    )
    # NBP premiums and Greeks are quoted in pence/therm while trade totals are
    # reported in pounds sterling. All other supported prices already use the
    # underlying trade currency.
    currency_conversion_factor = 0.01 if context["asset"] == "NBP" else 1.0
    position_scale = quantity_scale * currency_conversion_factor
    if not all(
        math.isfinite(value)
        for value in (
            quantity_scale,
            currency_conversion_factor,
            position_scale,
        )
    ):
        raise StructureValidationError(
            "Structure quantity and contract size produce a non-finite "
            "position scale."
        )
    return quantity_scale, currency_conversion_factor, position_scale


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
    call_put = str(raw_leg.get("call_put") or "").strip().upper()
    if call_put not in {"C", "P"}:
        raise StructureValidationError(f"{prefix}: option type must be Call or Put.")
    raw_side = str(raw_leg.get("side") or "").strip().upper()
    raw_ratio = _finite_number(raw_leg.get("ratio"), f"{prefix} ratio")
    if raw_side:
        if raw_side not in SIDE_SIGN:
            raise StructureValidationError(f"{prefix}: side must be Buy or Sell.")
        if raw_ratio <= 0:
            raise StructureValidationError(
                f"{prefix}: ratio must be greater than zero when Side is used."
            )
        side = raw_side
        ratio = raw_ratio
        weight = SIDE_SIGN[side] * ratio
    else:
        if raw_ratio == 0:
            raise StructureValidationError(f"{prefix}: lots must be non-zero.")
        side = "SELL" if raw_ratio < 0 else "BUY"
        ratio = abs(raw_ratio)
        weight = raw_ratio
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
        "weight": weight,
        "call_put": call_put,
        "strike": strike,
    }
    factor = context["vol_adjustment_factor"]

    def normalize_vol(raw_key: str, label: str) -> tuple[float, float]:
        entered_vol = _finite_number(
            raw_leg.get(raw_key),
            f"{prefix} {label}",
            minimum=0.005,
            maximum=200.0,
        )
        raw_vol = entered_vol / 100.0 if entered_vol > 2.0 else entered_vol
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
        raw_basis = raw_leg.get("quote_basis")
        quote_basis = str(raw_basis or "VOL").strip().upper()
        if quote_basis == "VOLATILITY":
            quote_basis = "VOL"
        if quote_basis not in {"VOL", "PREMIUM"}:
            raise StructureValidationError(
                f"{prefix}: quote basis must be Vol or Premium."
            )
        quote_key = "quote_value" if raw_basis is not None else "volatility"
        quote_input = raw_leg.get(quote_key)
        if quote_basis == "VOL":
            raw_vol, used_vol = normalize_vol(quote_key, "input volatility")
            entered_premium = None
        else:
            entered_premium = _finite_number(
                quote_input,
                f"{prefix} input premium",
                minimum=0.0,
                strict_minimum=True,
            )
            raw_vol, used_vol = _implied_contract_volatility(
                model,
                context,
                leg,
                entered_premium,
                prefix,
            )
        leg["quote_basis"] = quote_basis
        leg["quote_input"] = float(quote_input)
        leg["entered_premium"] = entered_premium
        leg["raw_volatility"] = raw_vol
        leg["volatility_used"] = used_vol
    else:
        raw_basis = raw_leg.get("quote_basis")
        if raw_basis is not None and str(raw_basis).strip().upper() not in {
            "VOL",
            "VOLATILITY",
        }:
            raise StructureValidationError(
                f"{prefix}: Kirk supports volatility inputs only."
            )
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


def _implied_contract_volatility(
    model: str,
    context: dict[str, Any],
    leg: dict[str, Any],
    premium: float,
    prefix: str,
) -> tuple[float, float]:
    """Invert a unit premium through the same pricing path used for final results."""
    factor = context["vol_adjustment_factor"]
    lower = max(0.005, 0.005 / factor)
    upper = min(2.0, 2.0 / factor)
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise StructureValidationError(
            f"{prefix}: the contract-date adjustment leaves no supported "
            "implied-volatility range."
        )

    def objective(raw_volatility: float) -> float:
        candidate = {
            **leg,
            "raw_volatility": raw_volatility,
            "volatility_used": raw_volatility * factor,
        }
        value, _greeks, _components = _price_leg(model, context, candidate)
        return value - premium

    lower_value = objective(lower) + premium
    upper_value = objective(upper) + premium
    price_scale = max(
        abs(float(context.get("forward") or 0.0)),
        abs(float(leg.get("strike") or 0.0)),
        abs(lower_value),
        abs(upper_value),
        abs(premium),
        1.0,
    )
    price_resolution = 64.0 * np.finfo(float).eps * price_scale
    if not math.isfinite(lower_value) or not math.isfinite(upper_value):
        raise StructureValidationError(
            f"{prefix}: implied-volatility bounds returned non-finite prices."
        )
    if upper_value < lower_value - price_resolution:
        raise StructureValidationError(
            f"{prefix}: premium is not monotone over the supported volatility range."
        )
    if upper_value - lower_value <= price_resolution:
        raise StructureValidationError(
            f"{prefix}: input premium does not identify a unique supported volatility."
        )
    if (
        premium < lower_value - price_resolution
        or premium > upper_value + price_resolution
    ):
        raise StructureValidationError(
            f"{prefix}: input premium {premium:.6g} is outside the attainable "
            f"{lower_value:.6g}–{upper_value:.6g} range for supported contract "
            "volatility."
        )
    lower_error = lower_value - premium
    upper_error = upper_value - premium
    if premium <= lower_value:
        raw_volatility = lower
    elif premium >= upper_value:
        raw_volatility = upper
    else:
        if not (lower_error < 0.0 < upper_error):
            raise StructureValidationError(
                f"{prefix}: input premium does not imply a unique supported volatility."
            )
        try:
            raw_volatility = float(
                brentq(objective, lower, upper, xtol=1e-14, rtol=1e-12)
            )
        except (ValueError, RuntimeError) as exc:
            raise StructureValidationError(
                f"{prefix}: input premium does not imply a unique supported volatility."
            ) from exc
    solved_value = objective(raw_volatility) + premium
    residual_tolerance = max(price_resolution, 1e-10 * abs(premium))
    if (
        not math.isfinite(raw_volatility)
        or raw_volatility < lower
        or raw_volatility > upper
        or not math.isfinite(solved_value)
        or abs(solved_value - premium) > residual_tolerance
    ):
        raise StructureValidationError(
            f"{prefix}: implied-volatility inversion did not converge to the input premium."
        )
    return raw_volatility, raw_volatility * factor


def _price_leg(
    model: str,
    context: dict[str, Any],
    leg: dict[str, Any],
) -> tuple[float, dict[str, float | None], list[dict[str, Any]]]:
    component_results: list[dict[str, Any]] = []
    if model == "black76":
        if context.get("delivery_components"):
            weighted = {
                "value": 0.0,
                "delta": 0.0,
                "gamma": 0.0,
                "theta": 0.0,
                "vega": 0.0,
                "rho": 0.0,
            }
            for component in context["delivery_components"]:
                if context.get("margin_style") == "futures_style":
                    result = black_76_futures_style(
                        leg["call_put"],
                        component["forward"],
                        leg["strike"],
                        component["time_to_expiry"],
                        leg["volatility_used"],
                    )
                else:
                    result = black_76(
                        leg["call_put"],
                        component["forward"],
                        leg["strike"],
                        component["time_to_expiry"],
                        context["rate"],
                        leg["volatility_used"],
                    )
                component_value, delta, gamma, theta, vega, rho = result
                component_greeks = {
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "rho": rho,
                }
                component_results.append(
                    {
                        **component,
                        "input_volatility": leg["raw_volatility"],
                        "pricing_volatility": leg["volatility_used"],
                        "unit_value": component_value,
                        "weighted_unit_value": component["weight"]
                        * component_value,
                        "greeks": component_greeks,
                        "weighted_greeks": {
                            key: component["weight"] * greek
                            for key, greek in component_greeks.items()
                        },
                    }
                )
                weighted["value"] += component["weight"] * component_value
                for metric, greek in component_greeks.items():
                    weighted[metric] += component["weight"] * greek
            value = weighted.pop("value")
            greeks = weighted
        else:
            pricing_function = (
                black_76_futures_style
                if context.get("margin_style") == "futures_style"
                else black_76
            )
            if pricing_function is black_76_futures_style:
                result = pricing_function(
                    leg["call_put"],
                    context["forward"],
                    leg["strike"],
                    context["time_to_expiry"],
                    leg["volatility_used"],
                )
            else:
                result = pricing_function(
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
                "vega": (
                    vega
                    if context["vega_basis"] == "adjusted_pricing_vol"
                    else vega * context["vol_adjustment_factor"]
                ),
                "rho": rho,
            }
    elif model == "asian76":
        if context.get("delivery_components"):
            weighted = {
                "value": 0.0,
                "delta": 0.0,
                "gamma": 0.0,
                "theta": 0.0,
                "vega": 0.0,
                "rho": 0.0,
            }
            for component in context["delivery_components"]:
                result = asian_76(
                    leg["call_put"],
                    component["forward"],
                    leg["strike"],
                    component["time_to_expiry"],
                    component["time_to_averaging_start"],
                    context["rate"],
                    leg["volatility_used"],
                )
                component_value, delta, gamma, theta, vega, rho = result
                if context.get("margin_style") == "futures_style":
                    rho = 0.0
                component_greeks = {
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "rho": rho,
                }
                component_results.append(
                    {
                        **component,
                        "input_volatility": leg["raw_volatility"],
                        "pricing_volatility": leg["volatility_used"],
                        "unit_value": component_value,
                        "weighted_unit_value": component["weight"] * component_value,
                        "greeks": component_greeks,
                        "weighted_greeks": {
                            key: component["weight"] * greek
                            for key, greek in component_greeks.items()
                        },
                    }
                )
                weighted["value"] += component["weight"] * component_value
                for metric, greek in component_greeks.items():
                    weighted[metric] += component["weight"] * greek
            value = weighted.pop("value")
            greeks = weighted
        else:
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
            if context.get("margin_style") == "futures_style":
                rho = 0.0
            greeks = {
                "delta": delta,
                "gamma": gamma,
                "theta": theta,
                "vega": (
                    vega
                    if context["vega_basis"] == "adjusted_pricing_vol"
                    else vega * context["vol_adjustment_factor"]
                ),
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
    return value, normalized_greeks, component_results


def _price_leg_value_only(
    model: str,
    context: dict[str, Any],
    leg: dict[str, Any],
) -> float:
    """Price one normalized leg without calculating Greeks for scenario charts."""
    if model == "black76":
        if context.get("delivery_components"):
            if context.get("margin_style") == "futures_style":
                value = sum(
                    component["weight"]
                    * black_76_futures_style(
                        leg["call_put"],
                        component["forward"],
                        leg["strike"],
                        component["time_to_expiry"],
                        leg["volatility_used"],
                    )[0]
                    for component in context["delivery_components"]
                )
            else:
                value = sum(
                    component["weight"]
                    * black_76(
                        leg["call_put"],
                        component["forward"],
                        leg["strike"],
                        component["time_to_expiry"],
                        context["rate"],
                        leg["volatility_used"],
                    )[0]
                    for component in context["delivery_components"]
                )
        elif context.get("margin_style") == "futures_style":
            value = black_76_futures_style(
                leg["call_put"],
                context["forward"],
                leg["strike"],
                context["time_to_expiry"],
                leg["volatility_used"],
            )[0]
        else:
            value = black_76(
                leg["call_put"],
                context["forward"],
                leg["strike"],
                context["time_to_expiry"],
                context["rate"],
                leg["volatility_used"],
            )[0]
    elif model == "asian76":
        if context.get("delivery_components"):
            value = sum(
                component["weight"]
                * asian_76(
                    leg["call_put"],
                    component["forward"],
                    leg["strike"],
                    component["time_to_expiry"],
                    component["time_to_averaging_start"],
                    context["rate"],
                    leg["volatility_used"],
                )[0]
                for component in context["delivery_components"]
            )
        else:
            value = asian_76(
                leg["call_put"],
                context["forward"],
                leg["strike"],
                context["time_to_expiry"],
                context["time_to_averaging_start"],
                context["rate"],
                leg["volatility_used"],
            )[0]
    else:
        value = kirk_model_with_substitution(
            context["asset_1"],
            context["asset_2"],
            leg["strike"],
            leg["volatility_asset_1_used"],
            leg["volatility_asset_2_used"],
            context["correlation"],
            context["time_to_expiry"],
            "call" if leg["call_put"] == "C" else "put",
        )
    normalized_value = _json_number(value)
    if normalized_value is None:
        raise StructureValidationError(
            f"{leg['name']}: scenario pricing returned a non-finite value."
        )
    return normalized_value


def _calculate_trade_value_only(
    model: str,
    context: dict[str, Any],
    sizing: dict[str, Any],
    legs: list[dict[str, Any]],
    calculation_date: date,
) -> float:
    """Validate scenario inputs and return only the signed trade value."""
    if model not in SUPPORTED_MODELS:
        raise StructureValidationError(f"Unsupported pricing model: {model}.")
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
    if len({leg["leg_id"] for leg in normalized_legs}) != len(normalized_legs):
        raise StructureValidationError("Leg IDs must be unique within the structure.")
    _, _, position_scale = _sizing_scales(
        normalized_context,
        normalized_sizing,
    )
    total = 0.0
    for leg in normalized_legs:
        total += (
            leg["weight"]
            * _price_leg_value_only(model, normalized_context, leg)
            * position_scale
        )
        if not math.isfinite(total):
            raise StructureValidationError(
                "Structure scenario value became non-finite; reduce ratios or sizing."
            )
    return total


def _raw_context_from_normalized(model: str, context: dict[str, Any]) -> dict[str, Any]:
    common = {
        "asset": context["asset"],
        "premium_convention": context["premium_convention"],
        "expiration_date": context["expiration_date"],
        "contract_expiration_date": context["contract_expiration_date"],
        "delivery_shape": context.get("delivery_shape", "MONTH"),
        "delivery_year": context.get("delivery_year"),
    }
    if context.get("delivery_month"):
        common["delivery_month"] = context["delivery_month"]
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

    quantity_scale, currency_conversion_factor, position_scale = _sizing_scales(
        normalized_context,
        normalized_sizing,
    )
    greek_fields = GREEK_FIELDS[model]
    unit_value_total = 0.0
    trade_value_total = 0.0
    priced_legs: list[dict[str, Any]] = []

    for leg in normalized_legs:
        try:
            unit_value, unit_greeks, component_results = _price_leg(
                model, normalized_context, leg
            )
        except StructureValidationError:
            raise
        except Exception as exc:
            raise StructureValidationError(
                f"{leg['name']}: pricing failed ({type(exc).__name__})."
            ) from exc
        unit_contribution = leg["weight"] * unit_value
        trade_contribution = unit_contribution * position_scale
        if not math.isfinite(unit_contribution) or not math.isfinite(
            trade_contribution
        ):
            raise StructureValidationError(
                f"{leg['name']}: ratio and sizing produce a non-finite value contribution."
            )
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
        if any(
            value is not None and not math.isfinite(value)
            for value in (
                *unit_greek_contributions.values(),
                *trade_greek_contributions.values(),
            )
        ):
            raise StructureValidationError(
                f"{leg['name']}: ratio and sizing produce a non-finite risk contribution."
            )
        unit_value_total += unit_contribution
        trade_value_total += trade_contribution
        if not math.isfinite(unit_value_total) or not math.isfinite(
            trade_value_total
        ):
            raise StructureValidationError(
                "Structure totals became non-finite; reduce ratios or sizing."
            )
        priced_legs.append(
            {
                **leg,
                "unit": {"value": unit_value, "greeks": unit_greeks},
                "components": component_results,
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
            if not math.isfinite(unit_greek_totals[metric]) or not math.isfinite(
                trade_greek_totals[metric]
            ):
                raise StructureValidationError(
                    f"Structure {GREEK_LABELS[metric]} total became non-finite; "
                    "reduce ratios or sizing."
                )

    warnings: list[str] = []
    if "theta" in unavailable_metrics and model == "asian76":
        warnings.append(
            "Structure Theta is unavailable because at least one Asian leg cannot "
            "be rolled one calendar day without entering its averaging period."
        )
    is_futures_style = normalized_context.get("margin_style") == "futures_style"
    unsupported_metrics = ["rho"] if model == "kirk" or is_futures_style else []
    if model == "kirk":
        warnings.append(
            "Kirk pricing is undiscounted in the current library; rate sensitivity "
            "and Rho are not applicable."
        )
        warnings.append(
            "Kirk applies the selected asset calendar and one shared contract-expiration "
            "adjustment to both volatility inputs; Asset 1, Asset 2, and strike must "
            "use the same price unit."
        )
    if is_futures_style and model != "kirk":
        warnings.append(
            "The futures-style premium convention is undiscounted; the risk-free "
            "rate and Rho are not applicable."
        )
    if normalized_context["asset"] == "NBP":
        warnings.append(
            "NBP unit analytics are quoted in GBp/therm; position values and "
            "Greeks are converted to GBP at 100 pence per pound."
        )
    if normalized_context.get("delivery_components"):
        if normalized_context["asset"] == "TTF":
            warnings.append(
                "The strip uses exact monthly TFO expiries and delivery-hour "
                "weights. The entered forward and each leg's volatility are "
                "applied flat across the monthly components."
            )
        elif model == "asian76":
            warnings.append(
                "The JKM Average Price Option strip uses exact monthly exchange "
                "expiries, governed averaging starts, and equal 10,000 MMBtu lots. "
                "Asian-76 is a continuous arithmetic-average approximation; the "
                "entered forward and each leg's volatility are applied flat across "
                "the monthly components."
            )
        else:
            warnings.append(
                "The JKZ strip uses the governed monthly TFO-linked expiries and "
                "equal 10,000 MMBtu lots. The entered forward and each leg's "
                "volatility are applied flat across the monthly components."
            )

    raw_context = _raw_context_from_normalized(model, normalized_context)
    raw_legs = [_raw_leg_from_normalized(model, leg) for leg in normalized_legs]
    greek_labels = {field: GREEK_LABELS[field] for field in greek_fields}
    greek_labels["theta"] = (
        "Theta (instantaneous annual derivative / 365)"
        if model == "black76"
        else "Theta (one-calendar-day roll)"
    )
    if (
        model in {"black76", "asian76"}
        and normalized_context["vega_basis"] == "adjusted_pricing_vol"
    ):
        greek_labels["vega"] = "Vega (adjusted pricing vol, 1 point)"

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "model_label": MODEL_LABELS[model],
        "calculation_date": calculation_date.isoformat(),
        "context": normalized_context,
        "sizing": {
            **normalized_sizing,
            "quantity_scale": quantity_scale,
            "price_currency_conversion_factor": currency_conversion_factor,
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
        "greek_labels": greek_labels,
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
    delivery_components = context.get("delivery_components") or []
    if delivery_components:
        first_expiration = _as_date(
            context["first_expiration_date"], "First component expiration date"
        )
        if valuation_date > first_expiration:
            raise StructureValidationError(
                "Strip valuation cannot move beyond the first monthly option expiry."
            )
        position_scale = snapshot["sizing"]["position_scale"]
        total = 0.0
        for leg in snapshot["legs"]:
            leg_value = 0.0
            for component in leg["components"]:
                component_expiration = _as_date(
                    component["option_expiration_date"],
                    "Component expiration date",
                )
                if valuation_date >= component_expiration:
                    value = (
                        max(underlying_value - leg["strike"], 0.0)
                        if leg["call_put"] == "C"
                        else max(leg["strike"] - underlying_value, 0.0)
                    )
                else:
                    time_to_expiry = (
                        component_expiration - valuation_date
                    ).days / float(context["day_count_denominator"])
                    if model == "asian76":
                        averaging_start = _as_date(
                            component["averaging_start_date"],
                            "Component averaging start date",
                        )
                        if valuation_date > averaging_start:
                            raise StructureValidationError(
                                "JKM Average Price Option strip valuation after a "
                                "monthly averaging period starts requires realized fixings."
                            )
                        time_to_averaging_start = max(
                            (
                                averaging_start - valuation_date
                            ).days / float(context["day_count_denominator"]),
                            0.0,
                        )
                        value = asian_76(
                            leg["call_put"],
                            underlying_value,
                            leg["strike"],
                            time_to_expiry,
                            time_to_averaging_start,
                            context["rate"],
                            leg["volatility_used"],
                        )[0]
                    elif context.get("margin_style") == "futures_style":
                        value = black_76_futures_style(
                            leg["call_put"],
                            underlying_value,
                            leg["strike"],
                            time_to_expiry,
                            leg["volatility_used"],
                        )[0]
                    else:
                        value = black_76(
                            leg["call_put"],
                            underlying_value,
                            leg["strike"],
                            time_to_expiry,
                            context["rate"],
                            leg["volatility_used"],
                        )[0]
                leg_value += component["weight"] * float(value)
            total += leg["weight"] * leg_value * position_scale
        return total

    expiration_date = _as_date(context["expiration_date"], "Expiration date")
    if valuation_date >= expiration_date:
        return _payoff_at_underlying(snapshot, underlying_value)

    day_count_denominator = float(context.get("day_count_denominator") or 365.0)
    time_to_expiry = max(
        (expiration_date - valuation_date).days / day_count_denominator,
        0.001,
    )
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
            (averaging_start - valuation_date).days / day_count_denominator,
            0.0,
        )
    else:
        time_to_averaging_start = None

    for leg in snapshot["legs"]:
        if model == "black76":
            if context.get("margin_style") == "futures_style":
                value = black_76_futures_style(
                    leg["call_put"],
                    underlying_value,
                    leg["strike"],
                    time_to_expiry,
                    leg["volatility_used"],
                )[0]
            else:
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
        axis_title = (
            "Parallel monthly forward price"
            if snapshot["context"].get("delivery_components")
            else "Forward price"
        )
    price_min = max(0.01, current_underlying * (1.0 - normalized_range / 100.0))
    price_max = current_underlying * (1.0 + normalized_range / 100.0)
    prices = np.linspace(price_min, price_max, max(11, min(int(points), 401)))
    is_delivery_strip = bool(snapshot["context"].get("delivery_components"))
    expiration_date = _as_date(
        snapshot["context"].get("first_expiration_date")
        if is_delivery_strip
        else snapshot["context"]["expiration_date"],
        "Expiration date",
    )
    default_valuation_date = expiration_date
    if model == "asian76" and is_delivery_strip:
        default_valuation_date = _as_date(
            snapshot["context"]["first_averaging_start_date"],
            "First averaging start date",
        )
    selected_date = (
        default_valuation_date
        if valuation_date is None
        else min(
            _as_date(valuation_date, "Valuation date"),
            default_valuation_date,
        )
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
        "at_expiration": selected_date >= expiration_date and not is_delivery_strip,
        "xaxis_title": axis_title,
        "payoff_label": (
            "Parallel strip intrinsic benchmark"
            if is_delivery_strip
            else "Payoff at expiration"
        ),
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
        snapshot["context"].get("first_expiration_date")
        or snapshot["context"]["expiration_date"],
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
            value = _calculate_trade_value_only(
                model,
                context,
                sizing,
                shifted_legs,
                calculation_date,
            )
        except StructureValidationError:
            continue
        valid_shifts.append(raw_shift * 100.0)
        values.append(value)
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
    if snapshot["context"].get("margin_style") == "futures_style":
        raise StructureValidationError(
            "Rate sensitivity is not applicable to the futures-style premium convention."
        )
    levels = _include_base_number(
        np.linspace(minimum_rate, maximum_rate, max(5, min(points, 101))),
        context["rate"],
    )
    values = []
    for level in levels:
        candidate_context = dict(context)
        candidate_context["rate"] = level
        value = _calculate_trade_value_only(
            model,
            candidate_context,
            sizing,
            legs,
            calculation_date,
        )
        values.append(value)
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
            value = _calculate_trade_value_only(
                model,
                candidate_context,
                sizing,
                legs,
                calculation_date,
            )
        except (StructureValidationError, ValueError, FloatingPointError):
            continue
        valid_levels.append(level)
        values.append(value)
    return {"correlations": valid_levels, "values": values}


def expiration_extension_series(
    snapshot: dict[str, Any],
    *,
    max_points: int = 41,
) -> dict[str, Any]:
    model, context, sizing, legs, calculation_date = _snapshot_input(snapshot)
    if snapshot["context"].get("delivery_components"):
        raise StructureValidationError(
            "Expiration extension is not applicable to a strip with governed "
            "monthly expiries."
        )
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
            value = _calculate_trade_value_only(
                model,
                candidate_context,
                sizing,
                legs,
                calculation_date,
            )
        except StructureValidationError:
            continue
        valid_dates.append(candidate_date.isoformat())
        values.append(value)
    return {
        "dates": valid_dates,
        "values": values,
        "base_expiration": base_expiration.isoformat(),
    }
