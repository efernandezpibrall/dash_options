"""Authoritative governed-surface references for the structure Pricer.

The module returns compact per-leg values and immutable publication provenance.
Published curves stay server-side and never enter persisted Pricer drafts or
browser stores in full.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import date
from hashlib import sha256
from time import perf_counter
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
from options.ttf_volatility import black76_call_delta
from options.vol_calibration.api import interpolate_published_smile
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
from source_identity import source_config_fingerprint
from workspace_cache import WorkspaceLoadCache
from vol_calibration.ttf_publication import PUBLICATION_TABLE, SURFACE_TABLE


LOGGER = logging.getLogger(__name__)


def _log_pricer_surface_timing(stage, started, **details):
    if os.getenv("CALIBRATION_TIMING", "").lower() in {"1", "true", "yes", "on"}:
        suffix = " ".join(f"{key}={value}" for key, value in details.items())
        LOGGER.info(
            "calibration_timing product=PRICER stage=%s seconds=%.6f %s",
            stage, perf_counter() - started, suffix,
        )
REFERENCE_SCHEMA_VERSION = 3
SUPPORTED_SURFACE_ASSETS = {"BRENT", "HH", "JKM", "NBP", "TTF"}
# Kept as an empty compatibility export for callers which imported the old
# distinction.  The Pricer never falls back to the operational surface table.
OPERATIONAL_SURFACE_ASSETS = frozenset()
PRICER_SURFACE_ASSETS = SUPPORTED_SURFACE_ASSETS
SUPPORTED_SURFACE_MODELS = {"black76", "asian76", "american_futures", "kirk"}
DAY_COUNT_DENOMINATOR = 365.25

_CATALOG_CACHE = WorkspaceLoadCache(max_entries=64)
_SLICE_CACHE = WorkspaceLoadCache(max_entries=128)


class SurfaceReferenceError(ValueError):
    """Raised when an advisory published-surface value cannot be produced."""


def clear_published_surface_reference_cache() -> None:
    """Clear the bounded process-local read cache (tests and explicit refresh)."""
    _CATALOG_CACHE.clear()
    _SLICE_CACHE.clear()


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
    started = perf_counter()
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
    _log_pricer_surface_timing(
        "catalog_read", started, asset=asset, found=bool(row),
    )
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
    started = perf_counter()
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
        SELECT run_id, commodity, cob_date, contract_date,
               option_expiration_date, strike, delta, put_call, volatility,
               working_forward, created_at
        FROM {SURFACE_TABLE}
        WHERE publication_id = CAST(:publication_id AS uuid)
          AND ({month_filter})
        ORDER BY contract_date, strike
        """
    )
    with engine.connect() as connection:
        points = pd.read_sql(query, connection, params=params)
    _log_pricer_surface_timing(
        "surface_slice_read", started, months=len(contract_months), rows=len(points),
    )
    for column in ("contract_date", "option_expiration_date", "created_at"):
        if column in points.columns:
            points[column] = pd.to_datetime(points[column], errors="coerce")
    return points


def _load_publication_expiry_points(
    engine,
    publication_id: str,
    reference_expiry: date,
) -> pd.DataFrame:
    started = perf_counter()
    query = text(
        f"""
        SELECT run_id, commodity, cob_date, contract_date,
               option_expiration_date, strike, delta, put_call, volatility,
               working_forward, created_at
        FROM {SURFACE_TABLE}
        WHERE publication_id = CAST(:publication_id AS uuid)
          AND option_expiration_date = :reference_expiry
        ORDER BY contract_date, strike
        """
    )
    with engine.connect() as connection:
        points = pd.read_sql(
            query,
            connection,
            params={
                "publication_id": publication_id,
                "reference_expiry": reference_expiry,
            },
        )
    _log_pricer_surface_timing(
        "surface_expiry_read", started, rows=len(points),
    )
    for column in ("cob_date", "contract_date", "option_expiration_date", "created_at"):
        if column in points.columns:
            points[column] = pd.to_datetime(points[column], errors="coerce")
    return points


def _validate_publication_points(catalog: Mapping, points: pd.DataFrame) -> None:
    if not isinstance(points, pd.DataFrame) or points.empty:
        return
    required = {
        "run_id",
        "commodity",
        "cob_date",
        "contract_date",
        "option_expiration_date",
        "strike",
        "delta",
        "volatility",
        "working_forward",
    }
    missing = sorted(required - set(points.columns))
    if missing:
        raise SurfaceReferenceError(
            "The governed publication slice is missing: " + ", ".join(missing) + "."
        )
    commodities = points["commodity"].astype(str).str.strip().str.upper().unique()
    if len(commodities) != 1 or commodities[0] != str(catalog["commodity"]).upper():
        raise SurfaceReferenceError(
            "The governed publication contains mismatched commodity points."
        )
    run_ids = points["run_id"].astype(str).unique()
    if len(run_ids) != 1 or run_ids[0] != str(catalog["run_id"]):
        raise SurfaceReferenceError(
            "The governed publication contains mismatched calibration-run points."
        )
    point_cobs = pd.to_datetime(points["cob_date"], errors="coerce").dt.date.unique()
    if len(point_cobs) != 1 or point_cobs[0].isoformat() != str(catalog["cob_date"]):
        raise SurfaceReferenceError(
            "The governed publication contains mismatched COB points."
        )


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
            f"Governed calibrated surfaces are not configured for {normalized_asset}."
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
    _validate_publication_points(catalog, points)
    return {**catalog, "contract_months": months, "points": points}


def load_published_surface_expiry_slice(
    asset: str,
    valuation_date,
    reference_expiry,
    *,
    force_refresh: bool = False,
    engine=None,
) -> dict:
    """Load one exact reference-expiry slice from the latest active publication."""
    normalized_asset = str(asset or "").strip().upper()
    if normalized_asset not in SUPPORTED_SURFACE_ASSETS:
        raise SurfaceReferenceError(
            f"Governed calibrated surfaces are not configured for {normalized_asset}."
        )
    selected_date = _as_date(valuation_date, "Valuation date")
    selected_expiry = _as_date(reference_expiry, "Volatility-reference expiry")
    db_engine = engine or get_database_engine(required=False)
    if db_engine is None:
        raise SurfaceReferenceError("Published surface storage is unavailable.")

    fingerprint = source_config_fingerprint()
    catalog_key = (normalized_asset, selected_date.isoformat(), fingerprint)
    catalog = _CATALOG_CACHE.get_or_load(
        catalog_key,
        lambda: _load_publication_catalog(db_engine, normalized_asset, selected_date),
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

    slice_key = (
        catalog["publication_id"],
        f"expiry:{selected_expiry.isoformat()}",
        fingerprint,
    )
    points = _SLICE_CACHE.get_or_load(
        slice_key,
        lambda: _load_publication_expiry_points(
            db_engine,
            catalog["publication_id"],
            selected_expiry,
        ),
        force_refresh=False,
        degraded=lambda value: not isinstance(value, pd.DataFrame) or value.empty,
        healthy_ttl_seconds=86_400,
        degraded_ttl_seconds=5,
    )
    if not isinstance(points, pd.DataFrame) or points.empty:
        raise SurfaceReferenceError(
            f"The active {normalized_asset} publication has no exact "
            f"{selected_expiry.isoformat()} volatility-reference expiry."
        )
    _validate_publication_points(catalog, points)
    return {
        **catalog,
        "reference_expiry": selected_expiry.isoformat(),
        "points": points,
    }


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


def volatility_overlay_points(adjustments: Mapping, smile_coordinate: float) -> float:
    """Vol-point overlay: parallel ATM, 25-delta call-minus-put, and wings."""
    x = float(smile_coordinate)
    return (
        float(adjustments.get("atm_vol_adjustment") or 0.0)
        + 0.5 * float(adjustments.get("skew_vol_adjustment") or 0.0) * x
        + float(adjustments.get("smile_vol_adjustment") or 0.0) * x * x
    )


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
    for column in ("strike", "delta", "volatility", "working_forward"):
        if column not in selected.columns:
            raise SurfaceReferenceError(
                f"{contract_month.strftime('%b-%y')} is missing governed {column}."
            )
        selected[column] = pd.to_numeric(selected[column], errors="coerce")
    selected["option_expiration_date"] = pd.to_datetime(
        selected.get("option_expiration_date"), errors="coerce"
    )
    required_numeric = selected[["strike", "volatility", "working_forward"]]
    if (
        selected["option_expiration_date"].isna().any()
        or not np.isfinite(required_numeric.to_numpy(dtype=float)).all()
        or (required_numeric <= 0.0).any().any()
    ):
        raise SurfaceReferenceError(
            f"{contract_month.strftime('%b-%y')} has invalid governed coordinates."
        )
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
    saved_forwards = selected["working_forward"].to_numpy(dtype=float)
    saved_forward = float(np.median(saved_forwards))
    if not np.allclose(
        saved_forwards,
        saved_forward,
        rtol=1e-10,
        atol=1e-12,
    ):
        raise SurfaceReferenceError(
            f"{contract_month.strftime('%b-%y')} has inconsistent governed forwards."
        )
    selected["log_moneyness"] = np.log(
        selected["strike"].to_numpy(dtype=float) / saved_forwards
    )
    if not np.isfinite(selected["log_moneyness"].to_numpy(dtype=float)).all():
        raise SurfaceReferenceError(
            f"{contract_month.strftime('%b-%y')} has invalid log-moneyness coordinates."
        )
    selected = selected.sort_values("log_moneyness")
    if selected["log_moneyness"].round(12).duplicated(keep=False).any():
        raise SurfaceReferenceError(
            f"{contract_month.strftime('%b-%y')} has duplicate governed coordinates."
        )
    selected["rebased_strike"] = current_forward * np.exp(
        selected["log_moneyness"].to_numpy(dtype=float)
    )
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
        "log_moneyness_nodes": selected["log_moneyness"].to_numpy(dtype=float),
        "source_call_delta_nodes": selected["source_call_delta"].to_numpy(dtype=float),
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
    target_log_moneyness = math.log(strike / current_forward)
    try:
        reference_volatility = interpolate_published_smile(
            prepared["log_moneyness_nodes"], prepared["volatility_nodes"],
            forward=current_forward, strike=strike,
        )
    except ValueError as exc:
        raise SurfaceReferenceError(str(exc)) from exc
    source_call_delta = float(
        np.interp(
            target_log_moneyness,
            np.asarray(prepared["log_moneyness_nodes"], dtype=float),
            np.asarray(prepared["source_call_delta_nodes"], dtype=float),
        )
    )
    smile_coordinate = (0.5 - source_call_delta) / 0.25
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
        "smile_coordinate": smile_coordinate,
        "pricing_volatility": pricing_volatility,
        "call_delta": call_delta,
    }


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
        source_label = (
            f"Surface COB: {publication.get('cob_date')} · "
            f"Published: {published_label} · "
            f"Publication: {publication.get('publication_id')}"
        )

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
            "surface_volatility_asset_1": None,
            "surface_volatility_asset_2": None,
            "surface_pricing_volatility_asset_1": None,
            "surface_pricing_volatility_asset_2": None,
            "surface_asset_1_tooltip": detail,
            "surface_asset_2_tooltip": detail,
        }
        for row in rows or []
        if isinstance(row, Mapping) and row.get("leg_id")
    }


def _prior_cob_warning(asset: str, publication: Mapping, valuation_date: date) -> str | None:
    surface_cob = _as_date(publication.get("cob_date"), "Surface COB")
    if surface_cob >= valuation_date:
        return None
    return (
        f"Prior COB: using {asset} {surface_cob.isoformat()} governed publication "
        f"for valuation {valuation_date.isoformat()}."
    )


def _normalize_kirk_reference_context(context: Mapping, valuation_date: date) -> dict:
    try:
        snapshot = calculate_structure(
            "kirk",
            dict(context or {}),
            {"structure_quantity": 1, "contract_multiplier": 1.0},
            [default_leg("kirk", 1)],
            as_of=valuation_date,
        )
    except StructureValidationError as exc:
        raise SurfaceReferenceError(str(exc)) from exc
    return snapshot["context"]


def _fifty_call_delta_anchor(points: pd.DataFrame, asset: str) -> dict:
    if not isinstance(points, pd.DataFrame) or points.empty:
        raise SurfaceReferenceError(f"The {asset} reference-expiry slice is empty.")
    selected = points.copy()
    selected["volatility"] = pd.to_numeric(
        selected.get("volatility"), errors="coerce"
    )
    source_call_deltas = []
    valid_indices = []
    for row_index, row in selected.iterrows():
        try:
            call_delta = _source_call_delta(row)
        except SurfaceReferenceError:
            continue
        volatility = float(row.get("volatility"))
        if math.isfinite(volatility) and 0.005 <= volatility <= 2.0:
            source_call_deltas.append(call_delta)
            valid_indices.append(row_index)
    selected = selected.loc[valid_indices].copy()
    selected["source_call_delta"] = source_call_deltas
    if len(selected) < 2:
        raise SurfaceReferenceError(
            f"The {asset} reference-expiry slice has insufficient valid delta points."
        )
    contract_dates = pd.to_datetime(
        selected.get("contract_date"), errors="coerce"
    ).dt.date.unique()
    reference_expiries = pd.to_datetime(
        selected.get("option_expiration_date"), errors="coerce"
    ).dt.date.unique()
    if len(contract_dates) != 1 or len(reference_expiries) != 1:
        raise SurfaceReferenceError(
            f"The {asset} reference expiry does not identify exactly one contract."
        )
    selected = (
        selected.sort_values("source_call_delta")
        .drop_duplicates("source_call_delta", keep="first")
    )
    delta_nodes = selected["source_call_delta"].to_numpy(dtype=float)
    if delta_nodes[0] > 0.5 or delta_nodes[-1] < 0.5:
        raise SurfaceReferenceError(
            f"The {asset} reference expiry does not bracket 50-call-delta."
        )
    anchor_volatility = float(
        np.interp(
            0.5,
            delta_nodes,
            selected["volatility"].to_numpy(dtype=float),
        )
    )
    if not math.isfinite(anchor_volatility) or not 0.005 <= anchor_volatility <= 2.0:
        raise SurfaceReferenceError(
            f"The {asset} 50-call-delta volatility is outside the supported range."
        )
    return {
        "contract_date": contract_dates[0].isoformat(),
        "reference_expiry": reference_expiries[0].isoformat(),
        "call_delta": 0.5,
        "volatility": anchor_volatility,
    }


def _build_kirk_published_surface_reference(
    asset: str,
    context: Mapping,
    rows: Sequence[Mapping],
    valuation_date: date,
    *,
    force_refresh: bool,
    engine,
    expiry_surface_loader: Callable,
) -> dict:
    normalized_context = _normalize_kirk_reference_context(context, valuation_date)
    publications = {}
    warnings = []
    for asset_number in (1, 2):
        key = f"asset_{asset_number}"
        product = str(normalized_context[f"{key}_code"]).strip().upper()
        reference_expiry = normalized_context[f"{key}_reference_expiry"]
        publication = expiry_surface_loader(
            product,
            valuation_date,
            reference_expiry,
            force_refresh=force_refresh,
            engine=engine,
        )
        required_publication_fields = (
            "publication_id",
            "run_id",
            "commodity",
            "cob_date",
            "published_at",
            "points",
        )
        if not isinstance(publication, Mapping) or any(
            field not in publication
            or publication[field] is None
            or (field != "points" and publication[field] == "")
            for field in required_publication_fields
        ):
            raise SurfaceReferenceError(
                f"The {product} governed publication metadata is incomplete."
            )
        if str(publication["commodity"]).strip().upper() != product:
            raise SurfaceReferenceError(
                f"The {product} volatility-reference slice has mismatched commodity."
            )
        anchor = _fifty_call_delta_anchor(publication.get("points"), product)
        if anchor["reference_expiry"] != str(reference_expiry):
            raise SurfaceReferenceError(
                f"The {product} publication does not match its requested "
                "volatility-reference expiry."
            )
        warning = _prior_cob_warning(product, publication, valuation_date)
        if warning:
            warnings.append(warning)
        publications[key] = {
            "asset": product,
            "publication_id": str(publication["publication_id"]),
            "run_id": str(publication["run_id"]),
            "publication_cob": str(publication["cob_date"]),
            "published_at": str(publication["published_at"]),
            "contract_date": anchor["contract_date"],
            "reference_expiry": anchor["reference_expiry"],
            "anchor_call_delta": anchor["call_delta"],
            "anchor_volatility": anchor["volatility"],
        }

    digest = sha256()
    for key in ("asset_1", "asset_2"):
        digest.update(publications[key]["publication_id"].encode("utf-8"))
        digest.update(publications[key]["reference_expiry"].encode("utf-8"))
    payload = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "asset": str(asset or ""),
        "model": "kirk",
        "source_kind": "governed",
        "source": "governed calibrated publications",
        "source_revision": digest.hexdigest(),
        "asset_publications": publications,
        "warnings": warnings,
        "rows": {},
    }
    for row in rows:
        leg_id = row.get("leg_id")
        if not leg_id:
            continue
        row_payload = {}
        for asset_number in (1, 2):
            key = f"asset_{asset_number}"
            publication = publications[key]
            input_volatility = float(publication["anchor_volatility"])
            factor = float(normalized_context[f"{key}_vol_adjustment_factor"])
            tooltip = (
                f"{publication['asset']} governed 50-call-delta anchor · "
                f"Reference expiry: {publication['reference_expiry']} · "
                f"Surface COB: {publication['publication_cob']} · "
                f"Publication: {publication['publication_id']}"
            )
            row_payload[f"surface_volatility_{key}"] = input_volatility
            row_payload[f"surface_pricing_volatility_{key}"] = (
                input_volatility * factor
            )
            row_payload[f"surface_{key}_tooltip"] = tooltip
        payload["rows"][str(leg_id)] = row_payload
    return payload


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
        from surface_data import get_operational_surface_snapshot

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
    expiry_surface_loader: Callable = load_published_surface_expiry_slice,
) -> dict:
    """Build a compact, authoritative per-leg governed-surface payload."""
    del operational_loader  # compatibility only; governed publications are authoritative
    normalized_rows = [dict(row) for row in rows or [] if isinstance(row, Mapping)]
    payload = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "asset": str(asset or ""),
        "model": model,
        "rows": {},
    }
    if not normalized_rows:
        return payload
    try:
        selected_date = _as_date(valuation_date, "Valuation date")
        if model == "kirk":
            return _build_kirk_published_surface_reference(
                str(asset or ""),
                context,
                normalized_rows,
                selected_date,
                force_refresh=force_refresh,
                engine=engine,
                expiry_surface_loader=expiry_surface_loader,
            )
    except SurfaceReferenceError as exc:
        payload["rows"] = _failed_rows(normalized_rows, str(exc))
        return payload
    except Exception:
        LOGGER.exception("Published Kirk surface reference loading failed")
        payload["rows"] = _failed_rows(
            normalized_rows,
            "published surface data could not be loaded.",
        )
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
        normalized_context = _normalize_reference_context(
            normalized_asset,
            model,
            context,
            selected_date,
        )
        components = _delivery_components(normalized_context)
        months = [_month_start(item["contract_month"]) for item in components]
        publication = surface_loader(
            normalized_asset,
            selected_date,
            months,
            force_refresh=force_refresh,
            engine=engine,
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
            "warnings": [],
        }
    )
    warning = _prior_cob_warning(normalized_asset, publication, selected_date)
    if warning:
        payload["warnings"].append(warning)
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
                adjustment_values = {
                    field: float(row.get(field) or 0.0)
                    for field in (
                        "atm_vol_adjustment",
                        "skew_vol_adjustment",
                        "smile_vol_adjustment",
                    )
                }
                if not all(math.isfinite(value) for value in adjustment_values.values()):
                    raise ValueError("Volatility adjustments must be finite.")
            except (TypeError, ValueError, OverflowError) as exc:
                raise SurfaceReferenceError(
                    "Volatility adjustments must be finite."
                ) from exc
            effective_component_results = [
                {
                    **item,
                    "input_volatility": item["reference_volatility"]
                    + 0.01 * volatility_overlay_points(
                        adjustment_values, item["smile_coordinate"]
                    ),
                    "pricing_volatility": (
                        item["reference_volatility"]
                        + 0.01 * volatility_overlay_points(
                            adjustment_values, item["smile_coordinate"]
                        )
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
                "surface_smile_coordinate": (
                    component_results[0]["smile_coordinate"]
                    if len(component_results) == 1
                    else None
                ),
                "surface_effective_input_vol": effective_input_volatility,
                "surface_effective_pricing_vol": effective_pricing_volatility,
                "surface_input_tooltip": tooltip,
                "surface_pricing_tooltip": tooltip,
                "surface_component_volatilities": [
                    {
                        "contract_month": item["contract_month"].isoformat(),
                        "input_volatility": item["reference_volatility"],
                        "pricing_volatility": item["pricing_volatility"],
                        "smile_coordinate": item["smile_coordinate"],
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
