"""Pinned, read-only market inputs for the Vol Trades quarter/season charts."""

import json
import pandas as pd
from sqlalchemy import bindparam, text

from runtime_config import get_database_engine
from source_identity import source_config_fingerprint
from vol_trades_data import _read_calibrated_surface_points, load_available_snapshots, load_chain_snapshot
from workspace_cache import WorkspaceLoadCache


_INPUT_CACHE = WorkspaceLoadCache(max_entries=16)


def load_strip_settlement_inputs(history, months, *, engine=None):
    """Read the same Bloomberg settlement vintage as the monthly charts.

    Keep the complete chain: quoted strips can extend beyond the monthly display
    horizon. Numerical selection and period completeness belong to options.
    """
    product = history.get("product")
    snapshot_id = history.get("snapshot_id")
    kind = str(history.get("snapshot_kind") or "SETTLEMENT").upper()
    date = pd.to_datetime(history.get("business_date"), errors="coerce")
    if product != "TFO" or not snapshot_id or pd.isna(date):
        raise ValueError("The selected Bloomberg settlement reference is unavailable")

    def read():
        selected_id, cob = str(snapshot_id), date.date()
        if kind == "INTRADAY":
            snapshots = load_available_snapshots(product, engine=engine)
            candidates = snapshots.loc[
                snapshots["snapshot_kind"].eq("SETTLEMENT")
                & pd.to_datetime(snapshots["business_date"], errors="coerce").lt(date)
            ].copy()
            if candidates.empty:
                raise ValueError("Prior Bloomberg monthly settlements unavailable")
            candidates["_date"] = pd.to_datetime(candidates["business_date"])
            candidates["_observed"] = pd.to_datetime(candidates["observed_at"], utc=True)
            selected = candidates.sort_values(["_date", "_observed"], ascending=False).iloc[0]
            selected_id, cob = str(selected["snapshot_id"]), pd.Timestamp(selected["business_date"]).date()
        chain = load_chain_snapshot(selected_id, engine=engine, product=product, snapshot_kind="SETTLEMENT")
        return {"chain": chain, "snapshot_id": selected_id, "cob_date": cob.isoformat()}

    if engine is not None:
        return read()
    return _INPUT_CACHE.get_or_load(
        ("ttf-strip-settlement-inputs", source_config_fingerprint(), str(snapshot_id), kind, str(date.date())),
        read,
        force_refresh=False,
        degraded=lambda result: result["chain"].empty,
        healthy_ttl_seconds=60,
        degraded_ttl_seconds=5,
    )


def load_strip_inputs(history, months, *, engine=None):
    metadata = history.get("calibration") or {}
    publication_id = metadata.get("publication_id") or history.get("publication_id")
    cob = pd.to_datetime(metadata.get("cob_date"), errors="coerce")
    if not publication_id or pd.isna(cob):
        raise ValueError("The selected calibrated TTF publication is unavailable")
    months = tuple(sorted({pd.Timestamp(value).date() for value in months}))
    contracts = tuple(f"{month:%Y}M{month:%m}" for month in months)

    def read():
        db_engine = engine or get_database_engine(required=True)
        with db_engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            catalog = (
                connection.execute(
                    text(
                        "SELECT commodity, cob_date FROM at_lng.vol_surface_publications "
                        "WHERE publication_id = CAST(:publication AS uuid) AND status = 'published'"
                    ),
                    {"publication": str(publication_id)},
                )
                .mappings()
                .one_or_none()
            )
            if not catalog or str(catalog["commodity"]).upper() != "TTF" or catalog["cob_date"] != cob.date():
                raise ValueError("The selected calibrated TTF publication does not match its source date")
            surface = _read_calibrated_surface_points(str(publication_id), months, connection)
            curve = pd.read_sql(
                text(
                    "SELECT contract, value, currency, units, cob FROM at_lng.curve "
                    "WHERE code = 'ICE_TTF' AND cob = :cob AND contract IN :contracts"
                ).bindparams(bindparam("contracts", expanding=True)),
                connection,
                params={"cob": cob.date(), "contracts": contracts},
            )
        surface["publication_id"] = str(publication_id)
        curve["contract_month"] = curve["contract"].map(dict(zip(contracts, months)))
        return {
            "surface": surface,
            "forwards": curve,
            "publication_id": str(publication_id),
            "cob_date": cob.date().isoformat(),
        }

    if engine is not None:
        return read()
    return _INPUT_CACHE.get_or_load(
        (
            "ttf-strip-inputs",
            source_config_fingerprint(),
            json.dumps([str(publication_id), str(cob.date()), contracts]),
        ),
        read,
        force_refresh=False,
        degraded=lambda result: result["surface"].empty or result["forwards"].empty,
        healthy_ttl_seconds=60,
        degraded_ttl_seconds=5,
    )
