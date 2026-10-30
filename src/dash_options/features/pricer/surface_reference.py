"""Application connections and caches for shared valuation surface references."""
from __future__ import annotations
from datetime import date
from time import perf_counter
from typing import Callable, Mapping
import pandas as pd
from sqlalchemy import text
from options.vol_calibration.api import attach_publication_models
from options.valuation import surface_reference as _valuation
from options.valuation.publications import select_publication
from options.valuation.surface_reference import (
    REFERENCE_SCHEMA_VERSION, SUPPORTED_SURFACE_ASSETS, OPERATIONAL_SURFACE_ASSETS,
    SurfaceReferenceError, _as_date, _month_start, _next_month, _validate_publication_points,
    _log_pricer_surface_timing, _failed_rows,
)
from dash_options.core.config import get_database_engine
from dash_options.core.database_reads import DB_SCHEMA
from dash_options.core.source_identity import source_config_fingerprint
from dash_options.core.workspace_cache import WorkspaceLoadCache
from dash_options.calibration.publication import SURFACE_TABLE


def __getattr__(name):
    """Retain existing imports of shared helpers during consumer migration."""
    return getattr(_valuation, name)


_CATALOG_CACHE = WorkspaceLoadCache(max_entries=64)
_SLICE_CACHE = WorkspaceLoadCache(max_entries=128)

def clear_published_surface_reference_cache() -> None:
    """Clear the bounded process-local read cache (tests and explicit refresh)."""
    _CATALOG_CACHE.clear()
    _SLICE_CACHE.clear()


def _load_publication_catalog(engine, asset: str, valuation_date: date, *, publication_id=None, valuation_timestamp=None) -> dict | None:
    if asset == "BRENT" and publication_id is None and valuation_timestamp is None:
        from options.vol_calibration.api import resolve_brent_intraday_publication
        intraday = resolve_brent_intraday_publication(engine, valuation_date)
        if intraday:
            return intraday
    if asset == "TTF" and publication_id is None and valuation_timestamp is None:
        from options.vol_calibration.api import resolve_tfo_intraday_publication
        intraday = resolve_tfo_intraday_publication(engine, valuation_date)
        if intraday:
            return intraday
    started = perf_counter()
    row = select_publication(engine, asset, valuation_date, schema=DB_SCHEMA,
                             publication_id=publication_id, timestamp=valuation_timestamp)
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
               working_forward, created_at, calibration_policy_version,
               source_name, calibration_basis
        FROM {SURFACE_TABLE}
        WHERE publication_id = CAST(:publication_id AS uuid)
          AND ({month_filter})
        ORDER BY contract_date, strike
        """
    )
    with engine.connect() as connection:
        points = pd.read_sql(query, connection, params=params)
        points = attach_publication_models(points, connection)
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
               working_forward, created_at, calibration_policy_version
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
        points = attach_publication_models(points, connection)
    _log_pricer_surface_timing(
        "surface_expiry_read", started, rows=len(points),
    )
    for column in ("cob_date", "contract_date", "option_expiration_date", "created_at"):
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
    publication_id=None,
    valuation_timestamp=None,
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
    catalog_key = (normalized_asset, selected_date.isoformat(), fingerprint, publication_id, str(valuation_timestamp))
    catalog = _CATALOG_CACHE.get_or_load(
        catalog_key,
        lambda: _load_publication_catalog(
            db_engine,
            normalized_asset,
            selected_date,
            **({"publication_id": publication_id, "valuation_timestamp": valuation_timestamp}
               if publication_id is not None or valuation_timestamp is not None else {}),
        ),
        force_refresh=force_refresh or (normalized_asset in {"TTF", "BRENT"} and selected_date == pd.Timestamp.now(tz="Asia/Dubai").date()),
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
    publication_id=None,
    valuation_timestamp=None,
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
    catalog_key = (normalized_asset, selected_date.isoformat(), fingerprint, publication_id, str(valuation_timestamp))
    catalog = _CATALOG_CACHE.get_or_load(
        catalog_key,
        lambda: _load_publication_catalog(db_engine, normalized_asset, selected_date,
            **({"publication_id": publication_id, "valuation_timestamp": valuation_timestamp}
               if publication_id is not None or valuation_timestamp is not None else {})),
        force_refresh=force_refresh or (normalized_asset in {"TTF", "BRENT"} and selected_date == pd.Timestamp.now(tz="Asia/Dubai").date()),
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
        from dash_options.shared.surfaces import get_operational_surface_snapshot

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


def build_published_surface_reference(asset, model, context, rows, valuation_date, *,
        force_refresh=False, engine=None, surface_loader=load_published_surface_slice,
        operational_loader=load_operational_surface_slice,
        expiry_surface_loader=load_published_surface_expiry_slice):
    """Resolve application live-curve context, then delegate valuation."""
    if model != "kirk" and context.get("curve_mode", "manual") != "manual":
        from dash_options.shared.live_forwards import pricer_context
        try:
            context = pricer_context(asset, context, _as_date(valuation_date, "Valuation date"))
        except Exception as exc:
            return {"schema_version": REFERENCE_SCHEMA_VERSION, "asset": str(asset or ""),
                    "model": model, "rows": _failed_rows(rows, str(exc))}
    return _valuation.build_published_surface_reference(
        asset, model, context, rows, valuation_date, force_refresh=force_refresh,
        engine=engine, surface_loader=surface_loader, operational_loader=operational_loader,
        expiry_surface_loader=expiry_surface_loader)
