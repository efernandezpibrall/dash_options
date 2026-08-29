"""Read-only published-surface references for the structure Pricer.

The module deliberately returns only compact per-leg advisory values.  Published
surface curves stay server-side and never enter persisted Pricer drafts,
calculation snapshots, or exports.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from hashlib import sha256
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from sqlalchemy import text

from options.options_library import (
    american_on_futures_equity_style,
    american_on_futures_equity_style_price,
    asian_76,
    black_76,
    black_76_futures_style,
)
from options.option_contract_conventions import FlatDiscountCurve
from options.option_expiry_engine import (
    business_days_between,
    get_surface_calendar_mapping,
)
from options.ttf_volatility import black76_call_delta, delta_node_to_strike
from pricer_structure import (
    PRODUCT_METADATA_FIELDS,
    SUPPORTED_ASSETS,
    StructureValidationError,
    _asian_determination_time,
    _asian_fixing_times,
    calculate_structure,
    default_leg,
    volatility_adjustment,
)
from runtime_config import get_database_engine
from vol_calibration.data_cache import WorkspaceLoadCache, source_config_fingerprint
from vol_calibration.ttf_publication import PUBLICATION_TABLE, SURFACE_TABLE


LOGGER = logging.getLogger(__name__)
REFERENCE_SCHEMA_VERSION = 2
SUPPORTED_SURFACE_ASSETS = {"TTF", "JKM"}
OPERATIONAL_SURFACE_ASSETS = {"BRENT", "HH", "NBP"}
PRICER_SURFACE_ASSETS = SUPPORTED_SURFACE_ASSETS | OPERATIONAL_SURFACE_ASSETS
SUPPORTED_SURFACE_MODELS = {"black76", "asian76", "american_futures"}
DAY_COUNT_DENOMINATOR = 365.25
COMPARISON_SCHEMA_VERSION = 1
COMPARISON_CURVE_POINT_LIMIT = 81

_CATALOG_CACHE = WorkspaceLoadCache(max_entries=64)
_SLICE_CACHE = WorkspaceLoadCache(max_entries=128)
_COMPARISON_CURVE_CACHE = WorkspaceLoadCache(max_entries=64)


class SurfaceReferenceError(ValueError):
    """Raised when an advisory published-surface value cannot be produced."""


def clear_published_surface_reference_cache() -> None:
    """Clear the bounded process-local read cache (tests and explicit refresh)."""
    _CATALOG_CACHE.clear()
    _SLICE_CACHE.clear()
    _COMPARISON_CURVE_CACHE.clear()


def _as_date(value, label: str) -> date:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise SurfaceReferenceError(f"{label} is invalid.")
    return parsed.date()


def _month_start(value) -> date:
    parsed = _as_date(value, "Contract month")
    return date(parsed.year, parsed.month, 1)


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _load_publication_catalog(engine, asset: str, valuation_date: date) -> dict | None:
    query = text(
        f"""
        SELECT p.publication_id, p.run_id, p.cob_date, p.published_at,
               p.published_by
        FROM {PUBLICATION_TABLE} p
        WHERE p.commodity = :commodity
          AND p.status = 'published'
          AND p.is_active
          AND p.cob_date <= :valuation_date
        ORDER BY p.cob_date DESC, p.published_at DESC, p.created_at DESC
        LIMIT 1
        """
    )
    with engine.connect() as connection:
        row = connection.execute(
            query,
            {"commodity": asset, "valuation_date": valuation_date},
        ).mappings().first()
    if row is None:
        return None
    return {
        "publication_id": str(row["publication_id"]),
        "run_id": str(row["run_id"]),
        "commodity": asset,
        "cob_date": pd.Timestamp(row["cob_date"]).date().isoformat(),
        "published_at": pd.Timestamp(row["published_at"]).isoformat(),
        "published_by": row.get("published_by"),
    }


def _load_publication_points(
    engine,
    publication_id: str,
    contract_months: tuple[date, ...],
) -> pd.DataFrame:
    predicates = []
    params: dict[str, object] = {"publication_id": publication_id}
    for index, month in enumerate(contract_months):
        predicates.append(
            f"(contract_date >= :month_start_{index} "
            f"AND contract_date < :month_end_{index})"
        )
        params[f"month_start_{index}"] = month
        params[f"month_end_{index}"] = _next_month(month)
    month_filter = " OR ".join(predicates) or "FALSE"
    query = text(
        f"""
        SELECT contract_date, option_expiration_date, delta, volatility,
               working_forward, created_at
        FROM {SURFACE_TABLE}
        WHERE publication_id = CAST(:publication_id AS uuid)
          AND ({month_filter})
        ORDER BY contract_date, strike
        """
    )
    with engine.connect() as connection:
        points = pd.read_sql(query, connection, params=params)
    for column in ("contract_date", "option_expiration_date", "created_at"):
        if column in points.columns:
            points[column] = pd.to_datetime(points[column], errors="coerce")
    return points


def load_published_surface_slice(
    asset: str,
    valuation_date,
    contract_months,
    *,
    force_refresh: bool = False,
    engine=None,
) -> dict:
    """Load one latest active publication and only the requested month slices."""
    normalized_asset = str(asset or "").strip().upper()
    if normalized_asset not in SUPPORTED_SURFACE_ASSETS:
        raise SurfaceReferenceError(
            "Published surface references are available only for TTF and JKM."
        )
    selected_date = _as_date(valuation_date, "Valuation date")
    months = tuple(sorted({_month_start(item) for item in contract_months or []}))
    if not months:
        raise SurfaceReferenceError("A governed delivery month is required.")
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        raise SurfaceReferenceError("Published surface storage is unavailable.")

    fingerprint = source_config_fingerprint()
    catalog_key = (normalized_asset, selected_date.isoformat(), fingerprint)
    catalog = _CATALOG_CACHE.get_or_load(
        catalog_key,
        lambda: _load_publication_catalog(
            db_engine,
            normalized_asset,
            selected_date,
        ),
        force_refresh=force_refresh,
        degraded=lambda value: value is None,
        healthy_ttl_seconds=300,
        degraded_ttl_seconds=5,
    )
    if catalog is None:
        raise SurfaceReferenceError(
            f"No active {normalized_asset} publication has COB on or before "
            f"{selected_date.isoformat()}."
        )

    month_key = ",".join(month.isoformat() for month in months)
    slice_key = (catalog["publication_id"], month_key, fingerprint)
    points = _SLICE_CACHE.get_or_load(
        slice_key,
        lambda: _load_publication_points(
            db_engine,
            catalog["publication_id"],
            months,
        ),
        force_refresh=False,
        degraded=lambda value: not isinstance(value, pd.DataFrame) or value.empty,
        healthy_ttl_seconds=86_400,
        degraded_ttl_seconds=5,
    )
    return {**catalog, "contract_months": months, "points": points}


def _normalize_reference_context(
    asset: str,
    model: str,
    context: Mapping,
    valuation_date: date,
) -> dict:
    if model not in SUPPORTED_SURFACE_MODELS:
        raise SurfaceReferenceError(
            f"Published references are not available for model {model}."
        )
    raw_context = dict(context or {})
    canonical_assets = {item.upper(): item for item in SUPPORTED_ASSETS}
    raw_context["asset"] = canonical_assets.get(str(asset).upper(), asset)
    try:
        forward = float(raw_context.get("forward"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SurfaceReferenceError("A positive finite forward is required.") from exc
    if not math.isfinite(forward) or forward <= 0:
        raise SurfaceReferenceError("A positive finite forward is required.")
    dummy_leg = default_leg(model, 1)
    dummy_leg["strike"] = forward
    try:
        snapshot = calculate_structure(
            model,
            raw_context,
            {"structure_quantity": 1, "contract_multiplier": 1.0},
            [dummy_leg],
            as_of=valuation_date,
        )
    except StructureValidationError as exc:
        raise SurfaceReferenceError(str(exc)) from exc
    return snapshot["context"]


def _delivery_components(context: Mapping) -> list[dict]:
    components = context.get("delivery_components")
    if components:
        return [dict(item) for item in components]
    delivery_month = context.get("delivery_month")
    if not delivery_month:
        raise SurfaceReferenceError("A governed delivery month is required.")
    component = {
        "contract_month": _month_start(delivery_month).isoformat(),
        "contract_month_label": _month_start(delivery_month).strftime("%b-%y"),
        "option_expiration_date": context.get("expiration_date"),
        "time_to_expiry": context.get("time_to_expiry"),
        "forward": context.get("forward"),
        "rate": context.get("rate"),
        "weight": 1.0,
    }
    for field in (
        "surface_option_expiration_date",
        "surface_expiry_convention_code",
        "surface_expiry_convention_version",
        "expiry_convention_code",
        "expiry_convention_version",
        "volatility_surface_source",
        "max_surface_extension_days",
        "averaging_fixing_dates",
        "averaging_fixing_times",
        "floating_price_determination_date",
        "floating_price_determination_calendar_code",
        "time_to_floating_price_determination",
        *PRODUCT_METADATA_FIELDS,
    ):
        if context.get(field) is not None:
            component[field] = context[field]
    if context.get("time_to_averaging_start") is not None:
        component["time_to_averaging_start"] = context[
            "time_to_averaging_start"
        ]
    return [component]


def _month_points(points: pd.DataFrame, contract_month: date) -> pd.DataFrame:
    if points is None or points.empty or "contract_date" not in points:
        raise SurfaceReferenceError(
            f"The publication has no {contract_month.strftime('%b-%y')} surface."
        )
    periods = pd.to_datetime(points["contract_date"], errors="coerce").dt.to_period("M")
    selected = points[periods == pd.Period(contract_month, freq="M")].copy()
    if selected.empty:
        raise SurfaceReferenceError(
            f"The publication has no {contract_month.strftime('%b-%y')} surface."
        )
    return selected


def _surface_model_call_delta(
    model: str,
    component: Mapping,
    *,
    current_forward: float,
    strike: float,
    reference_time: float,
    reference_volatility: float,
    pricing_volatility: float,
) -> float:
    if model == "black76":
        return black76_call_delta(
            current_forward,
            strike,
            reference_time,
            reference_volatility,
        )
    if model == "american_futures":
        try:
            call_delta = american_on_futures_equity_style(
                "C",
                current_forward,
                strike,
                float(component.get("time_to_expiry")),
                FlatDiscountCurve(float(component.get("rate") or 0.0)),
                pricing_volatility,
                steps=400,
            )[1]
        except Exception as exc:
            raise SurfaceReferenceError(
                "The American futures surface call delta could not be calculated."
            ) from exc
        call_delta = float(call_delta)
        if not math.isfinite(call_delta) or not 0.0 < call_delta < 1.0:
            raise SurfaceReferenceError(
                "The American futures surface call delta is invalid."
            )
        return call_delta
    if model != "asian76":
        raise SurfaceReferenceError(
            f"Published surface delta is not supported for model {model}."
        )

    try:
        time_to_expiry = float(component.get("time_to_expiry"))
        time_to_averaging_start = float(
            component.get("time_to_averaging_start")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise SurfaceReferenceError(
            "The Asian-76 surface delta requires valid averaging times."
        ) from exc
    if (
        not math.isfinite(time_to_expiry)
        or not math.isfinite(time_to_averaging_start)
        or time_to_expiry <= 0.0
        or time_to_averaging_start < 0.0
        or time_to_averaging_start > time_to_expiry
    ):
        raise SurfaceReferenceError(
            "The Asian-76 surface delta requires valid averaging times."
        )
    try:
        call_delta = asian_76(
            "C",
            current_forward,
            strike,
            time_to_expiry,
            time_to_averaging_start,
            0.0,
            pricing_volatility,
            fixing_times=_asian_fixing_times(dict(component)),
            determination_time=_asian_determination_time(dict(component)),
        )[1]
    except Exception as exc:
        raise SurfaceReferenceError(
            "The Asian-76 surface call delta could not be calculated."
        ) from exc
    call_delta = float(call_delta)
    if not math.isfinite(call_delta) or not 0.0 < call_delta < 1.0:
        raise SurfaceReferenceError("The Asian-76 surface call delta is invalid.")
    return call_delta


def _source_call_delta(row: Mapping) -> float:
    try:
        raw_delta = float(row.get("delta"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SurfaceReferenceError("The surface delta is invalid.") from exc
    delta_abs = abs(raw_delta)
    if delta_abs > 1.0:
        delta_abs /= 100.0
    put_call = str(row.get("put_call") or "").strip().lower()
    if put_call in {"p", "put"} or (not put_call and raw_delta < 0.0):
        call_delta = 1.0 - delta_abs
    else:
        call_delta = delta_abs
    if not math.isfinite(call_delta) or not 0.0 < call_delta < 1.0:
        raise SurfaceReferenceError("The surface call delta is invalid.")
    return call_delta


def _prepare_component_surface(
    points: pd.DataFrame,
    component: Mapping,
    *,
    asset: str,
    valuation_date: date,
    current_forward: float,
) -> dict:
    contract_month = _month_start(component.get("contract_month"))
    selected = _month_points(points, contract_month)
    selected["delta"] = pd.to_numeric(selected.get("delta"), errors="coerce")
    selected["volatility"] = pd.to_numeric(
        selected.get("volatility"), errors="coerce"
    )
    selected["working_forward"] = pd.to_numeric(
        selected.get("working_forward"), errors="coerce"
    )
    selected["option_expiration_date"] = pd.to_datetime(
        selected.get("option_expiration_date"), errors="coerce"
    )
    selected = selected.dropna(subset=["delta", "volatility", "option_expiration_date"])
    selected = selected[
        selected["volatility"].gt(0.0) & np.isfinite(selected["volatility"])
    ]
    source_call_deltas = []
    valid_indices = []
    for row_index, row in selected.iterrows():
        try:
            source_call_deltas.append(_source_call_delta(row))
            valid_indices.append(row_index)
        except SurfaceReferenceError:
            continue
    selected = selected.loc[valid_indices].copy()
    selected["source_call_delta"] = source_call_deltas
    reference_expiries = selected["option_expiration_date"].dt.date.unique()
    if selected.empty or len(reference_expiries) != 1:
        raise SurfaceReferenceError(
            f"{contract_month.strftime('%b-%y')} has an invalid published smile."
        )
    reference_expiry = reference_expiries[0]
    if reference_expiry <= valuation_date:
        raise SurfaceReferenceError(
            f"The published {contract_month.strftime('%b-%y')} reference expiry "
            "is not after valuation."
        )
    reference_time = (reference_expiry - valuation_date).days / DAY_COUNT_DENOMINATOR
    selected["rebased_strike"] = [
        delta_node_to_strike(
            current_forward,
            reference_time,
            float(delta),
            float(volatility),
        )
        for delta, volatility in zip(
            selected["source_call_delta"], selected["volatility"]
        )
    ]
    selected = selected.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["rebased_strike"]
    )
    selected = selected.sort_values("rebased_strike")
    selected = selected.drop_duplicates("rebased_strike", keep=False)
    if len(selected) < 2:
        raise SurfaceReferenceError(
            f"{contract_month.strftime('%b-%y')} has insufficient published points."
        )
    delta_sorted = selected.sort_values("source_call_delta")
    atm_reference_volatility = float(
        np.interp(
            0.5,
            delta_sorted["source_call_delta"].to_numpy(dtype=float),
            delta_sorted["volatility"].to_numpy(dtype=float),
        )
    )
    if not math.isfinite(atm_reference_volatility) or atm_reference_volatility <= 0:
        raise SurfaceReferenceError(
            f"{contract_month.strftime('%b-%y')} has an invalid ATM volatility."
        )
    option_expiry = _as_date(
        component.get("option_expiration_date"),
        "Selected option expiration",
    )
    extension_days = 0
    if option_expiry > reference_expiry:
        variance_calendar = get_surface_calendar_mapping(
            asset
        ).variance_calendar_code
        extension_days = business_days_between(
            reference_expiry,
            option_expiry,
            variance_calendar,
        )
        maximum_extension = int(component.get("max_surface_extension_days", 0))
        if extension_days > maximum_extension:
            raise SurfaceReferenceError(
                f"Selected expiry {option_expiry.isoformat()} extends the published "
                f"reference expiry {reference_expiry.isoformat()} by "
                f"{extension_days} governed day(s); this mapping permits "
                f"{maximum_extension}."
            )
    try:
        adjustment_factor, option_days, reference_days = volatility_adjustment(
            valuation_date,
            option_expiry,
            reference_expiry,
            asset=asset,
        )
    except StructureValidationError as exc:
        raise SurfaceReferenceError(str(exc)) from exc
    saved_forward_values = selected["working_forward"].dropna()
    saved_forward = (
        float(saved_forward_values.median()) if not saved_forward_values.empty else None
    )
    return {
        "contract_month": contract_month,
        "contract_month_label": contract_month.strftime("%b-%y"),
        "reference_expiry": reference_expiry,
        "option_expiry": option_expiry,
        "reference_time": reference_time,
        "surface_adjustment_factor": adjustment_factor,
        "atm_reference_volatility": atm_reference_volatility,
        "atm_pricing_volatility": atm_reference_volatility * adjustment_factor,
        "option_business_days": option_days,
        "reference_business_days": reference_days,
        "surface_extension_business_days": extension_days,
        "saved_forward": saved_forward,
        "minimum_strike": float(selected["rebased_strike"].iloc[0]),
        "maximum_strike": float(selected["rebased_strike"].iloc[-1]),
        "weight": float(component.get("weight", 1.0)),
        "component": dict(component),
        "strike_nodes": selected["rebased_strike"].to_numpy(dtype=float),
        "volatility_nodes": selected["volatility"].to_numpy(dtype=float),
    }


def _prepared_component_result(
    prepared: Mapping,
    *,
    model: str,
    current_forward: float,
    strike: float,
) -> dict:
    minimum_strike = float(prepared["minimum_strike"])
    maximum_strike = float(prepared["maximum_strike"])
    tolerance = 1e-12 * max(abs(minimum_strike), abs(maximum_strike), 1.0)
    if strike < minimum_strike - tolerance or strike > maximum_strike + tolerance:
        raise SurfaceReferenceError(
            f"Strike {strike:.6g} is outside the rebased published range "
            f"{minimum_strike:.6g}-{maximum_strike:.6g} for "
            f"{prepared['contract_month_label']}."
        )
    reference_volatility = float(
        np.interp(
            strike,
            np.asarray(prepared["strike_nodes"], dtype=float),
            np.asarray(prepared["volatility_nodes"], dtype=float),
        )
    )
    pricing_volatility = reference_volatility * float(
        prepared["surface_adjustment_factor"]
    )
    call_delta = _surface_model_call_delta(
        model,
        prepared["component"],
        current_forward=current_forward,
        strike=strike,
        reference_time=float(prepared["reference_time"]),
        reference_volatility=reference_volatility,
        pricing_volatility=pricing_volatility,
    )
    return {
        **dict(prepared),
        "reference_volatility": reference_volatility,
        "pricing_volatility": pricing_volatility,
        "call_delta": call_delta,
    }


def _surface_component_volatility(
    points: pd.DataFrame,
    component: Mapping,
    *,
    asset: str,
    model: str,
    valuation_date: date,
    current_forward: float,
    strike: float,
) -> dict:
    prepared = _prepare_component_surface(
        points,
        component,
        asset=asset,
        valuation_date=valuation_date,
        current_forward=current_forward,
    )
    return _prepared_component_result(
        prepared,
        model=model,
        current_forward=current_forward,
        strike=strike,
    )


def _component_price(
    model: str,
    context: Mapping,
    component: Mapping,
    call_put: str,
    strike: float,
    volatility: float,
) -> float:
    if model == "black76":
        if context.get("margin_style") == "futures_style":
            value = black_76_futures_style(
                call_put,
                float(component["forward"]),
                strike,
                float(component["time_to_expiry"]),
                volatility,
            )[0]
        else:
            value = black_76(
                call_put,
                float(component["forward"]),
                strike,
                float(component["time_to_expiry"]),
                float(context["rate"]),
                volatility,
            )[0]
    elif model == "asian76":
        value = asian_76(
            call_put,
            float(component["forward"]),
            strike,
            float(component["time_to_expiry"]),
            float(component["time_to_averaging_start"]),
            float(context["rate"]),
            volatility,
            fixing_times=_asian_fixing_times(dict(component)),
            determination_time=_asian_determination_time(dict(component)),
        )[0]
    elif model == "american_futures":
        value = american_on_futures_equity_style_price(
            call_put,
            float(component["forward"]),
            strike,
            float(component["time_to_expiry"]),
            FlatDiscountCurve(float(context["rate"])),
            volatility,
            steps=400,
        )
    else:
        raise SurfaceReferenceError(f"Unsupported surface model {model}.")
    value = float(value)
    if not math.isfinite(value):
        raise SurfaceReferenceError("Published surface pricing became non-finite.")
    return value


def _premium_equivalent_flat_volatility(
    model: str,
    context: Mapping,
    component_results: list[dict],
    call_put: str,
    strike: float,
) -> tuple[float, float, float]:
    target = sum(
        item["weight"]
        * _component_price(
            model,
            context,
            item["component"],
            call_put,
            strike,
            item["pricing_volatility"],
        )
        for item in component_results
    )

    def objective(volatility: float) -> float:
        return (
            sum(
                item["weight"]
                * _component_price(
                    model,
                    context,
                    item["component"],
                    call_put,
                    strike,
                    volatility,
                )
                for item in component_results
            )
            - target
        )

    lower, upper = 0.005, 2.0
    lower_error = objective(lower)
    upper_error = objective(upper)
    tolerance = 1e-11 * max(abs(target), 1.0)
    if abs(lower_error) <= tolerance:
        flat = lower
    elif abs(upper_error) <= tolerance:
        flat = upper
    elif not (lower_error < 0.0 < upper_error):
        raise SurfaceReferenceError(
            "The monthly published vols do not identify a supported flat strip vol."
        )
    else:
        flat = float(brentq(objective, lower, upper, xtol=1e-13, rtol=1e-12))
    residual = objective(flat)
    if not math.isfinite(flat) or abs(residual) > tolerance:
        raise SurfaceReferenceError(
            "The premium-equivalent strip volatility did not converge."
        )
    return flat, target, residual


def _publication_detail(publication: Mapping) -> str:
    if publication.get("source_kind") == "operational":
        return (
            f"COB {publication.get('cob_date')}; source "
            f"{publication.get('source') or 'operational surface'}; revision "
            f"{_surface_revision(publication)}"
        )
    return (
        f"COB {publication['cob_date']}; published {publication['published_at']}; "
        f"revision {publication['publication_id']}"
    )


def _published_surface_tooltip(
    publication: Mapping,
    component_results: list[dict],
    model: str,
) -> str:
    if publication.get("source_kind") == "operational":
        source_label = (
            f"Surface COB: {publication.get('cob_date')} · "
            f"Source: {publication.get('source') or 'operational surface'}"
        )
    else:
        published_at = pd.to_datetime(
            publication.get("published_at"),
            errors="coerce",
        )
        if pd.isna(published_at):
            raise SurfaceReferenceError("The publication timestamp is invalid.")
        timezone_label = ""
        if published_at.tzinfo is not None:
            published_at = published_at.tz_convert("UTC")
            timezone_label = " UTC"
        published_label = (
            published_at.strftime("%Y-%m-%d %H:%M") + timezone_label
        )
        source_label = f"Published: {published_label}"

    deltas = [float(item["call_delta"]) for item in component_results]
    if not deltas or not all(math.isfinite(value) for value in deltas):
        raise SurfaceReferenceError("The surface delta is unavailable.")
    lower_label = f"{min(deltas):.2%}"
    upper_label = f"{max(deltas):.2%}"
    delta_label = (
        lower_label if lower_label == upper_label else f"{lower_label}–{upper_label}"
    )
    model_label = {
        "asian76": "Asian-76",
        "american_futures": "American futures",
    }.get(model, "Black-76")
    tooltip = (
        f"{source_label} · "
        f"Surface call delta ({model_label}): {delta_label}"
    )
    adjustments = {
        (
            item["reference_expiry"],
            item["option_expiry"],
            float(item["surface_adjustment_factor"]),
        )
        for item in component_results
        if item["reference_expiry"] != item["option_expiry"]
    }
    if adjustments:
        details = []
        for source_expiry, target_expiry, factor in sorted(adjustments):
            direction = "extended" if target_expiry > source_expiry else "shortened"
            details.append(
                f"{source_expiry.isoformat()} to {target_expiry.isoformat()} "
                f"({direction}, factor {factor:.6f})"
            )
        tooltip += " · Expiry adjustment: " + "; ".join(details)
    return tooltip


def _failed_rows(rows, reason: str, publication: Mapping | None = None) -> dict:
    detail = f"Published surface unavailable: {reason}"
    if publication:
        detail = f"{detail} Publication {_publication_detail(publication)}."
    return {
        str(row.get("leg_id")): {
            "surface_input_vol": None,
            "surface_atm_input_vol": None,
            "surface_skew_input_vol": None,
            "surface_pricing_vol": None,
            "surface_input_tooltip": detail,
            "surface_pricing_tooltip": detail,
        }
        for row in rows or []
        if isinstance(row, Mapping) and row.get("leg_id")
    }


def load_operational_surface_slice(
    asset: str,
    valuation_date,
    contract_months,
    *,
    force_refresh: bool = False,
    snapshot_loader: Callable | None = None,
) -> dict:
    """Load the exact requested operational months from the nearest prior COB."""
    normalized_asset = str(asset or "").strip().upper()
    if normalized_asset not in OPERATIONAL_SURFACE_ASSETS:
        raise SurfaceReferenceError(
            "Operational surface comparisons are available only for Brent, HH, and NBP."
        )
    selected_date = _as_date(valuation_date, "Valuation date")
    months = tuple(sorted({_month_start(item) for item in contract_months or []}))
    if not months:
        raise SurfaceReferenceError("A governed delivery month is required.")
    if snapshot_loader is None:
        from pages.vol_surface import get_operational_surface_snapshot

        snapshot_loader = get_operational_surface_snapshot
    snapshot = snapshot_loader(
        normalized_asset,
        selected_date,
        refresh=force_refresh,
    )
    error = snapshot.get("error") if isinstance(snapshot, Mapping) else None
    if error:
        raise SurfaceReferenceError(str(error))
    points = snapshot.get("data") if isinstance(snapshot, Mapping) else None
    if not isinstance(points, pd.DataFrame) or points.empty:
        raise SurfaceReferenceError(
            f"No operational {normalized_asset} surface is available on or before "
            f"{selected_date.isoformat()}."
        )
    periods = pd.to_datetime(points.get("contract_date"), errors="coerce").dt.to_period("M")
    requested_periods = {pd.Period(month, freq="M") for month in months}
    points = points.loc[periods.isin(requested_periods)].copy()
    if points.empty:
        labels = ", ".join(month.strftime("%b-%y") for month in months)
        raise SurfaceReferenceError(
            f"The operational {normalized_asset} surface has no exact {labels} contract."
        )
    actual_cob = pd.to_datetime(snapshot.get("actual_cob"), errors="coerce")
    if pd.isna(actual_cob):
        raise SurfaceReferenceError("The operational surface COB is invalid.")
    return {
        "publication_id": None,
        "run_id": None,
        "commodity": normalized_asset,
        "cob_date": actual_cob.date().isoformat(),
        "published_at": None,
        "source": snapshot.get("source") or "Operational surface",
        "source_kind": "operational",
        "date_fallback_used": bool(snapshot.get("date_fallback_used")),
        "source_fallback_used": bool(snapshot.get("source_fallback_used")),
        "contract_months": months,
        "points": points,
    }


def _comparison_context_payload(snapshot: Mapping) -> dict:
    if not isinstance(snapshot, Mapping):
        raise SurfaceReferenceError("The calculated structure snapshot is invalid.")
    model = str(snapshot.get("model") or "")
    context = snapshot.get("context")
    if model not in {"black76", "asian76", "american_futures", "kirk"} or not isinstance(
        context, Mapping
    ):
        raise SurfaceReferenceError("The calculated structure snapshot is invalid.")
    components = []
    if model != "kirk":
        for component in _delivery_components(context):
            components.append(
                {
                    key: component.get(key)
                    for key in (
                        "contract_month",
                        "option_expiration_date",
                        "averaging_start_date",
                        "averaging_fixing_dates",
                        "averaging_fixing_times",
                        "floating_price_determination_date",
                        "floating_price_determination_calendar_code",
                        "time_to_floating_price_determination",
                        "time_to_expiry",
                        "time_to_averaging_start",
                        "forward",
                        "weight",
                    )
                }
            )
    return {
        "asset": str(context.get("asset") or "").strip().upper(),
        "model": model,
        "calculation_date": snapshot.get("calculation_date"),
        "forward": context.get("forward"),
        "rate": context.get("rate"),
        "margin_style": context.get("margin_style"),
        "premium_convention": context.get("premium_convention"),
        "delivery_shape": context.get("delivery_shape"),
        "delivery_month": context.get("delivery_month"),
        "delivery_year": context.get("delivery_year"),
        "delivery_period_label": context.get("delivery_period_label"),
        "expiration_date": context.get("expiration_date"),
        "contract_expiration_date": context.get("contract_expiration_date"),
        "vol_adjustment_factor": context.get("vol_adjustment_factor"),
        "components": components,
    }


def surface_comparison_context_key(snapshot: Mapping) -> str:
    """Return a stable exact-pricing-context key for card de-duplication."""
    payload = _comparison_context_payload(snapshot)
    normalized = repr(payload).encode("utf-8")
    return sha256(normalized).hexdigest()


def _pricing_model_call_delta(
    model: str,
    context: Mapping,
    component: Mapping,
    strike: float,
    pricing_volatility: float,
) -> float:
    try:
        forward = float(component.get("forward", context.get("forward")))
        time_to_expiry = float(component.get("time_to_expiry"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SurfaceReferenceError("The selected pricing times are invalid.") from exc
    if model == "black76":
        call_delta = black76_call_delta(
            forward,
            strike,
            time_to_expiry,
            pricing_volatility,
        )
    elif model == "asian76":
        try:
            averaging_time = float(component.get("time_to_averaging_start"))
            call_delta = asian_76(
                "C",
                forward,
                strike,
                time_to_expiry,
                averaging_time,
                float(context.get("rate") or 0.0),
                pricing_volatility,
                fixing_times=_asian_fixing_times(dict(component)),
                determination_time=_asian_determination_time(dict(component)),
            )[1]
        except Exception as exc:
            raise SurfaceReferenceError(
                "The Asian-76 comparison delta could not be calculated."
            ) from exc
    elif model == "american_futures":
        try:
            call_delta = american_on_futures_equity_style(
                "C",
                forward,
                strike,
                time_to_expiry,
                FlatDiscountCurve(float(context.get("rate") or 0.0)),
                pricing_volatility,
                steps=400,
            )[1]
        except Exception as exc:
            raise SurfaceReferenceError(
                "The American futures comparison delta could not be calculated."
            ) from exc
    else:
        raise SurfaceReferenceError("Kirk has no governed single-asset surface delta.")
    call_delta = float(call_delta)
    if not math.isfinite(call_delta) or not 0.0 <= call_delta <= 1.0:
        raise SurfaceReferenceError("The model-consistent surface delta is invalid.")
    return call_delta


def _weighted_pricing_call_delta(
    model: str,
    context: Mapping,
    component_results: Sequence[Mapping],
    strike: float,
    pricing_volatility: float,
) -> float:
    call_delta = sum(
        float(item["weight"])
        * _pricing_model_call_delta(
            model,
            context,
            item["component"],
            strike,
            pricing_volatility,
        )
        for item in component_results
    )
    if not math.isfinite(call_delta) or not 0.0 <= call_delta <= 1.0:
        raise SurfaceReferenceError("The weighted surface delta is invalid.")
    return call_delta


def _surface_revision(publication: Mapping) -> str:
    publication_id = publication.get("publication_id")
    if publication_id:
        return str(publication_id)
    points = publication.get("points")
    if not isinstance(points, pd.DataFrame):
        raise SurfaceReferenceError("The operational surface revision is invalid.")
    columns = [
        column
        for column in (
            "contract_date",
            "option_expiration_date",
            "delta",
            "put_call",
            "volatility",
        )
        if column in points.columns
    ]
    digest = sha256()
    digest.update(str(publication.get("source") or "").encode("utf-8"))
    digest.update(str(publication.get("cob_date") or "").encode("utf-8"))
    digest.update(pd.util.hash_pandas_object(points[columns], index=False).values.tobytes())
    return digest.hexdigest()


def _comparison_curve(
    publication: Mapping,
    *,
    asset: str,
    model: str,
    context: Mapping,
    valuation_date: date,
) -> dict:
    points = publication.get("points")
    if not isinstance(points, pd.DataFrame) or points.empty:
        raise SurfaceReferenceError("The selected surface slice is empty.")
    current_forward = float(context["forward"])
    components = _delivery_components(context)
    prepared = [
        _prepare_component_surface(
            points,
            component,
            asset=asset,
            valuation_date=valuation_date,
            current_forward=current_forward,
        )
        for component in components
    ]
    input_adjustment_factor = float(context.get("vol_adjustment_factor", 1.0))
    if not math.isfinite(input_adjustment_factor) or input_adjustment_factor <= 0.0:
        raise SurfaceReferenceError("The Pricer expiry adjustment factor is invalid.")

    if len(prepared) == 1:
        source_strikes = np.asarray(prepared[0]["strike_nodes"], dtype=float)
        if len(source_strikes) > COMPARISON_CURVE_POINT_LIMIT:
            indices = np.linspace(
                0,
                len(source_strikes) - 1,
                COMPARISON_CURVE_POINT_LIMIT,
            ).round().astype(int)
            strikes = source_strikes[np.unique(indices)]
        else:
            strikes = source_strikes
    else:
        minimum_strike = max(float(item["minimum_strike"]) for item in prepared)
        maximum_strike = min(float(item["maximum_strike"]) for item in prepared)
        if not minimum_strike < maximum_strike:
            raise SurfaceReferenceError(
                "The monthly surface strike ranges do not overlap for this strip."
            )
        strikes = np.linspace(
            minimum_strike,
            maximum_strike,
            COMPARISON_CURVE_POINT_LIMIT,
        )

    curve_points = []
    for strike in strikes:
        component_results = [
            _prepared_component_result(
                item,
                model=model,
                current_forward=current_forward,
                strike=float(strike),
            )
            for item in prepared
        ]
        if len(component_results) == 1:
            pricing_volatility = float(component_results[0]["pricing_volatility"])
        else:
            pricing_volatility, _target, _residual = _premium_equivalent_flat_volatility(
                model,
                context,
                component_results,
                "C",
                float(strike),
            )
        call_delta = _weighted_pricing_call_delta(
            model,
            context,
            component_results,
            float(strike),
            pricing_volatility,
        )
        curve_points.append(
            {
                "delta": 1.0 - call_delta,
                "call_delta": call_delta,
                "strike": float(strike),
                "input_volatility": pricing_volatility / input_adjustment_factor,
                "pricing_volatility": pricing_volatility,
            }
        )
    curve_points.sort(key=lambda item: item["delta"])
    interior_points = [
        point
        for point in curve_points
        if 1e-10 < float(point["delta"]) < 1.0 - 1e-10
    ]
    if len(interior_points) >= 2:
        curve_points = interior_points
    if len(curve_points) < 2:
        raise SurfaceReferenceError("The comparison surface has insufficient points.")
    return {"curve_points": curve_points, "prepared": prepared}


def _quote_short_label(structure_label: str, leg: Mapping, index: int) -> str:
    leg_id = str(leg.get("leg_id") or "")
    suffix = leg_id.rsplit("-", 1)[-1]
    leg_label = f"L{int(suffix)}" if suffix.isdigit() else f"L{index}"
    return f"{structure_label} {leg_label}"


def _comparison_base_view(structures: Sequence[Mapping]) -> dict:
    first = structures[0]
    snapshot = first["snapshot"]
    context = snapshot["context"]
    labels = [str(item["structure_label"]) for item in structures]
    delivery_label = context.get("delivery_period_label")
    if not delivery_label and context.get("delivery_month"):
        delivery_label = _month_start(context["delivery_month"]).strftime("%b-%y")
    if not delivery_label:
        delivery_label = "Single expiry"
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "context_key": surface_comparison_context_key(snapshot),
        "status": "ready",
        "structure_ids": [str(item["structure_id"]) for item in structures],
        "structure_labels": labels,
        "structure_label": ", ".join(labels),
        "asset": str(context.get("asset") or ""),
        "model": snapshot["model"],
        "model_label": snapshot.get("model_label") or snapshot["model"],
        "delivery_label": delivery_label,
        "valuation_date": snapshot["calculation_date"],
        "curve_points": [],
        "quote_points": [],
        "warnings": [],
    }


def build_surface_comparison_view(
    structures: Sequence[Mapping],
    *,
    publication: Mapping | None = None,
    source_error: str | None = None,
    force_refresh: bool = False,
    surface_loader: Callable = load_published_surface_slice,
    operational_loader: Callable = load_operational_surface_slice,
) -> dict:
    """Build one compact card view model for one exact calculated context."""
    if not structures:
        raise SurfaceReferenceError("At least one calculated structure is required.")
    view = _comparison_base_view(structures)
    first_snapshot = structures[0]["snapshot"]
    context = first_snapshot["context"]
    model = first_snapshot["model"]
    asset = str(context.get("asset") or "").strip().upper()
    if model == "kirk":
        view.update(
            {
                "status": "unsupported",
                "message": (
                    "Kirk is not supported: it has two volatility inputs and no "
                    "governed per-leg asset-to-surface mapping."
                ),
                "source_kind": "unsupported",
                "source_label": "Unsupported for Kirk",
            }
        )
        return view
    if asset not in SUPPORTED_SURFACE_ASSETS | OPERATIONAL_SURFACE_ASSETS:
        view.update(
            {
                "status": "error",
                "message": f"No volatility-surface route is configured for {asset}.",
            }
        )
        return view
    if source_error:
        view.update({"status": "error", "message": source_error})
        return view

    valuation_date = _as_date(first_snapshot["calculation_date"], "Valuation date")
    components = _delivery_components(context)
    months = [_month_start(item["contract_month"]) for item in components]
    try:
        if publication is None:
            if asset in SUPPORTED_SURFACE_ASSETS:
                publication = surface_loader(
                    asset,
                    valuation_date,
                    months,
                    force_refresh=force_refresh,
                )
            else:
                publication = operational_loader(
                    asset,
                    valuation_date,
                    months,
                    force_refresh=force_refresh,
                )
        revision = _surface_revision(publication)
        cache_key = (revision, view["context_key"], f"comparison-v{COMPARISON_SCHEMA_VERSION}")
        curve = _COMPARISON_CURVE_CACHE.get_or_load(
            cache_key,
            lambda: _comparison_curve(
                publication,
                asset=asset,
                model=model,
                context=context,
                valuation_date=valuation_date,
            ),
            force_refresh=False,
            degraded=lambda value: not value or not value.get("curve_points"),
            healthy_ttl_seconds=86_400,
            degraded_ttl_seconds=5,
        )
    except SurfaceReferenceError as exc:
        view.update({"status": "error", "message": str(exc)})
        return view
    except Exception:
        LOGGER.exception("Surface comparison source preparation failed")
        view.update(
            {
                "status": "error",
                "message": "The volatility surface comparison could not be prepared.",
            }
        )
        return view

    source_kind = str(publication.get("source_kind") or "governed")
    source_label = (
        "Governed calibrated publication"
        if source_kind == "governed"
        else f"Operational surface · {publication.get('source') or 'configured source'}"
    )
    surface_cob = _as_date(publication.get("cob_date"), "Surface COB")
    published_at = publication.get("published_at")
    view.update(
        {
            "source_kind": source_kind,
            "source_label": source_label,
            "surface_cob": surface_cob.isoformat(),
            "published_at": published_at,
            "source_revision": revision,
            "curve_points": curve["curve_points"],
        }
    )
    if surface_cob < valuation_date:
        view["warnings"].append(
            f"Prior COB: using {surface_cob.isoformat()} for valuation "
            f"{valuation_date.isoformat()}."
        )
    if publication.get("source_fallback_used"):
        view["warnings"].append("Operational source fallback is in use.")

    current_forward = float(context["forward"])
    input_adjustment_factor = float(context.get("vol_adjustment_factor", 1.0))
    for structure in structures:
        snapshot = structure["snapshot"]
        structure_label = str(structure["structure_label"])
        for leg_index, leg in enumerate(snapshot.get("legs") or [], start=1):
            try:
                strike = float(leg.get("strike"))
                contract_volatility = float(leg.get("raw_volatility"))
                if (
                    not math.isfinite(strike)
                    or strike <= 0.0
                    or not math.isfinite(contract_volatility)
                    or contract_volatility <= 0.0
                ):
                    raise SurfaceReferenceError("Strike or solved contract vol is invalid.")
                component_results = [
                    _prepared_component_result(
                        item,
                        model=model,
                        current_forward=current_forward,
                        strike=strike,
                    )
                    for item in curve["prepared"]
                ]
                call_put = str(leg.get("call_put") or "").strip().upper()
                if call_put not in {"C", "P"}:
                    raise SurfaceReferenceError("Option type must be Call or Put.")
                if len(component_results) == 1:
                    pricing_volatility = float(
                        component_results[0]["pricing_volatility"]
                    )
                else:
                    pricing_volatility, _target, _residual = (
                        _premium_equivalent_flat_volatility(
                            model,
                            context,
                            component_results,
                            call_put,
                            strike,
                        )
                    )
                reference_volatility = pricing_volatility / input_adjustment_factor
                call_delta = _weighted_pricing_call_delta(
                    model,
                    context,
                    component_results,
                    strike,
                    pricing_volatility,
                )
                quote_basis = str(leg.get("quote_basis") or "VOL").upper()
                view["quote_points"].append(
                    {
                        "structure_id": structure["structure_id"],
                        "structure_label": structure_label,
                        "leg_id": leg.get("leg_id"),
                        "leg_label": leg.get("name") or f"Leg {leg_index}",
                        "short_label": _quote_short_label(
                            structure_label,
                            leg,
                            leg_index,
                        ),
                        "call_put": call_put,
                        "strike": strike,
                        "quote_basis": quote_basis,
                        "quote_basis_label": (
                            "Premium-implied" if quote_basis == "PREMIUM" else "Input vol"
                        ),
                        "contract_volatility": contract_volatility,
                        "reference_volatility": reference_volatility,
                        "pricing_volatility": pricing_volatility,
                        "difference_vol_points": 100.0
                        * (contract_volatility - reference_volatility),
                        "delta": 1.0 - call_delta,
                        "call_delta": call_delta,
                        "surface_cob": surface_cob.isoformat(),
                        "source": source_label,
                    }
                )
            except SurfaceReferenceError as exc:
                view["warnings"].append(
                    f"{_quote_short_label(structure_label, leg, leg_index)}: {exc}"
                )
            except Exception:
                LOGGER.exception("Surface comparison quote preparation failed")
                view["warnings"].append(
                    f"{_quote_short_label(structure_label, leg, leg_index)}: "
                    "quote comparison could not be calculated."
                )
    return view


def build_surface_comparison_views(
    structures: Sequence[Mapping],
    *,
    force_refresh: bool = False,
    surface_loader: Callable = load_published_surface_slice,
    operational_loader: Callable = load_operational_surface_slice,
) -> list[dict]:
    """Group exact contexts, batch their source months, and build isolated cards."""
    grouped: dict[str, list[dict]] = {}
    for item in structures or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("snapshot"), Mapping):
            continue
        try:
            key = surface_comparison_context_key(item["snapshot"])
        except SurfaceReferenceError:
            continue
        grouped.setdefault(key, []).append(dict(item))
    if not grouped:
        return []

    requests: dict[tuple[str, str, str], set[date]] = {}
    for items in grouped.values():
        snapshot = items[0]["snapshot"]
        if snapshot.get("model") == "kirk":
            continue
        context = snapshot.get("context") or {}
        asset = str(context.get("asset") or "").strip().upper()
        if asset not in SUPPORTED_SURFACE_ASSETS | OPERATIONAL_SURFACE_ASSETS:
            continue
        valuation_date = _as_date(snapshot.get("calculation_date"), "Valuation date")
        source_kind = "governed" if asset in SUPPORTED_SURFACE_ASSETS else "operational"
        request_key = (source_kind, asset, valuation_date.isoformat())
        requests.setdefault(request_key, set()).update(
            _month_start(component["contract_month"])
            for component in _delivery_components(context)
        )

    loaded: dict[tuple[str, str, str], Mapping] = {}
    source_errors: dict[tuple[str, str, str], str] = {}
    for request_key, months in requests.items():
        source_kind, asset, valuation_text = request_key
        try:
            if source_kind == "governed":
                loaded[request_key] = surface_loader(
                    asset,
                    valuation_text,
                    sorted(months),
                    force_refresh=force_refresh,
                )
            else:
                loaded[request_key] = operational_loader(
                    asset,
                    valuation_text,
                    sorted(months),
                    force_refresh=force_refresh,
                )
        except SurfaceReferenceError as exc:
            source_errors[request_key] = str(exc)
        except Exception:
            LOGGER.exception("Surface comparison batch source loading failed")
            source_errors[request_key] = "The volatility surface source could not be loaded."

    views = []
    for items in grouped.values():
        snapshot = items[0]["snapshot"]
        context = snapshot.get("context") or {}
        model = snapshot.get("model")
        asset = str(context.get("asset") or "").strip().upper()
        if model == "kirk" or asset not in SUPPORTED_SURFACE_ASSETS | OPERATIONAL_SURFACE_ASSETS:
            request_key = None
        else:
            valuation_text = _as_date(
                snapshot.get("calculation_date"), "Valuation date"
            ).isoformat()
            request_key = (
                "governed" if asset in SUPPORTED_SURFACE_ASSETS else "operational",
                asset,
                valuation_text,
            )
        views.append(
            build_surface_comparison_view(
                items,
                publication=loaded.get(request_key) if request_key else None,
                source_error=source_errors.get(request_key) if request_key else None,
                force_refresh=False,
                surface_loader=surface_loader,
                operational_loader=operational_loader,
            )
        )
    return views


def build_published_surface_reference(
    asset: str,
    model: str,
    context: Mapping,
    rows,
    valuation_date,
    *,
    force_refresh: bool = False,
    engine=None,
    surface_loader: Callable = load_published_surface_slice,
    operational_loader: Callable = load_operational_surface_slice,
) -> dict:
    """Build a compact, read-only per-leg published-surface payload."""
    normalized_rows = [dict(row) for row in rows or [] if isinstance(row, Mapping)]
    payload = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "asset": str(asset or ""),
        "model": model,
        "rows": {},
    }
    if not normalized_rows:
        return payload
    normalized_asset = str(asset or "").strip().upper()
    if normalized_asset not in PRICER_SURFACE_ASSETS:
        payload["rows"] = _failed_rows(
            normalized_rows,
            "published references are not configured for this asset.",
        )
        return payload
    if model not in SUPPORTED_SURFACE_MODELS:
        return payload
    try:
        selected_date = _as_date(valuation_date, "Valuation date")
        normalized_context = _normalize_reference_context(
            normalized_asset,
            model,
            context,
            selected_date,
        )
        components = _delivery_components(normalized_context)
        months = [_month_start(item["contract_month"]) for item in components]
        if normalized_asset in SUPPORTED_SURFACE_ASSETS:
            publication = surface_loader(
                normalized_asset,
                selected_date,
                months,
                force_refresh=force_refresh,
                engine=engine,
            )
        else:
            publication = operational_loader(
                normalized_asset,
                selected_date,
                months,
                force_refresh=force_refresh,
            )
        points = publication.get("points")
        if not isinstance(points, pd.DataFrame) or points.empty:
            raise SurfaceReferenceError("The selected publication slice is empty.")
    except SurfaceReferenceError as exc:
        payload["rows"] = _failed_rows(normalized_rows, str(exc))
        return payload
    except Exception:
        LOGGER.exception("Published surface reference loading failed")
        payload["rows"] = _failed_rows(
            normalized_rows,
            "published surface data could not be loaded.",
        )
        return payload

    payload.update(
        {
            "publication_id": publication.get("publication_id"),
            "publication_cob": publication["cob_date"],
            "published_at": publication.get("published_at"),
            "source_kind": publication.get("source_kind") or "governed",
            "source": publication.get("source") or "governed publication",
            "source_revision": _surface_revision(publication),
        }
    )
    current_forward = float(normalized_context["forward"])
    prepared_components = None
    for row in normalized_rows:
        leg_id = row.get("leg_id")
        if not leg_id:
            continue
        try:
            strike = float(row.get("strike"))
            if not math.isfinite(strike) or strike <= 0:
                if normalized_context.get("positive_domain_required"):
                    raise SurfaceReferenceError(
                        f"{normalized_context.get('exchange_mapping_id')} uses a "
                        "lognormal surface and requires a strictly positive strike."
                    )
                raise SurfaceReferenceError("A positive finite strike is required.")
            call_put = str(row.get("call_put") or "").strip().upper()
            if call_put not in {"C", "P"}:
                raise SurfaceReferenceError("Option type must be Call or Put.")
            if prepared_components is None:
                prepared_components = [
                    _prepare_component_surface(
                        points,
                        component,
                        asset=normalized_asset,
                        valuation_date=selected_date,
                        current_forward=current_forward,
                    )
                    for component in components
                ]
            component_results = [
                _prepared_component_result(
                    prepared,
                    model=model,
                    current_forward=current_forward,
                    strike=strike,
                )
                for prepared in prepared_components
            ]
            if len(component_results) == 1:
                component_result = component_results[0]
                input_volatility = component_result["reference_volatility"]
                atm_input_volatility = component_result[
                    "atm_reference_volatility"
                ]
                pricing_volatility = component_result["pricing_volatility"]
                atm_pricing_volatility = component_result[
                    "atm_pricing_volatility"
                ]
            else:
                pricing_volatility, _target_premium, _residual = (
                    _premium_equivalent_flat_volatility(
                        model,
                        normalized_context,
                        component_results,
                        call_put,
                        strike,
                    )
                )
                input_component_results = [
                    {
                        **item,
                        "pricing_volatility": item["reference_volatility"],
                    }
                    for item in component_results
                ]
                input_volatility, _input_target_premium, _input_residual = (
                    _premium_equivalent_flat_volatility(
                        model,
                        normalized_context,
                        input_component_results,
                        call_put,
                        strike,
                    )
                )
                atm_component_results = [
                    {
                        **item,
                        "pricing_volatility": item["atm_pricing_volatility"],
                    }
                    for item in component_results
                ]
                atm_pricing_volatility, _atm_target_premium, _atm_residual = (
                    _premium_equivalent_flat_volatility(
                        model,
                        normalized_context,
                        atm_component_results,
                        call_put,
                        strike,
                    )
                )
                atm_input_component_results = [
                    {
                        **item,
                        "pricing_volatility": item[
                            "atm_reference_volatility"
                        ],
                    }
                    for item in component_results
                ]
                (
                    atm_input_volatility,
                    _atm_input_target_premium,
                    _atm_input_residual,
                ) = _premium_equivalent_flat_volatility(
                    model,
                    normalized_context,
                    atm_input_component_results,
                    call_put,
                    strike,
                )
            skew_input_volatility = input_volatility - atm_input_volatility
            try:
                manual_adjustment = 0.01 * sum(
                    float(row.get(field) or 0.0)
                    for field in (
                        "atm_vol_adjustment",
                        "skew_vol_adjustment",
                        "smile_vol_adjustment",
                    )
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise SurfaceReferenceError(
                    "Volatility adjustments must be finite."
                ) from exc
            effective_component_results = [
                {
                    **item,
                    "input_volatility": item["reference_volatility"]
                    + manual_adjustment,
                    "pricing_volatility": (
                        item["reference_volatility"] + manual_adjustment
                    )
                    * item["surface_adjustment_factor"],
                }
                for item in component_results
            ]
            if len(effective_component_results) == 1:
                effective_input_volatility = effective_component_results[0][
                    "input_volatility"
                ]
                effective_pricing_volatility = effective_component_results[0][
                    "pricing_volatility"
                ]
            else:
                effective_input_volatility, _target, _residual = (
                    _premium_equivalent_flat_volatility(
                        model,
                        normalized_context,
                        [
                            {
                                **item,
                                "pricing_volatility": item[
                                    "input_volatility"
                                ],
                            }
                            for item in effective_component_results
                        ],
                        call_put,
                        strike,
                    )
                )
                effective_pricing_volatility, _target, _residual = (
                    _premium_equivalent_flat_volatility(
                        model,
                        normalized_context,
                        effective_component_results,
                        call_put,
                        strike,
                    )
                )
            tooltip = _published_surface_tooltip(
                publication,
                component_results,
                model,
            )
            payload["rows"][str(leg_id)] = {
                "surface_input_vol": input_volatility,
                "surface_atm_input_vol": atm_input_volatility,
                "surface_skew_input_vol": skew_input_volatility,
                "surface_pricing_vol": pricing_volatility,
                "surface_effective_input_vol": effective_input_volatility,
                "surface_effective_pricing_vol": effective_pricing_volatility,
                "surface_input_tooltip": tooltip,
                "surface_pricing_tooltip": tooltip,
                "surface_component_volatilities": [
                    {
                        "contract_month": item["contract_month"].isoformat(),
                        "input_volatility": item["reference_volatility"],
                        "pricing_volatility": item["pricing_volatility"],
                        "expiry_adjustment_factor": item[
                            "surface_adjustment_factor"
                        ],
                    }
                    for item in component_results
                ],
                "surface_expiry_adjustments": [
                    {
                        "contract_month": item["contract_month"].isoformat(),
                        "source_expiry": item["reference_expiry"].isoformat(),
                        "target_expiry": item["option_expiry"].isoformat(),
                        "source_governed_days": item[
                            "reference_business_days"
                        ],
                        "target_governed_days": item["option_business_days"],
                        "direction": (
                            "equal"
                            if item["option_expiry"] == item["reference_expiry"]
                            else (
                                "extension"
                                if item["option_expiry"] > item["reference_expiry"]
                                else "shortening"
                            )
                        ),
                        "factor": item["surface_adjustment_factor"],
                    }
                    for item in component_results
                ],
            }
        except SurfaceReferenceError as exc:
            payload["rows"].update(
                _failed_rows([row], str(exc), publication=publication)
            )
        except Exception:
            LOGGER.exception("Published surface reference calculation failed")
            payload["rows"].update(
                _failed_rows(
                    [row],
                    "the published surface value could not be calculated.",
                    publication=publication,
                )
            )
    return payload
