"""Financial and selection regressions for the shared history workspace."""

import math
from copy import deepcopy
import numpy as np
import pandas as pd
import pytest
from dash import no_update
from dash_options.features.vol_surface import page, realized_comparison as data
from dash_options.shared import surfaces
from dash_options.core.snapshots import SnapshotStore, SnapshotReferenceError
from tests.vol_surface.test_realized_comparison import fixture


@pytest.mark.parametrize("product", ["Brent", "HH", "TTF"])
def test_native_source_is_pinned_across_ranges_and_missing_dates(product):
    _, prices, iv = fixture(product)
    official = prices.loc[prices.cob_date.le("2026-10-26")].copy()
    alternative = prices.copy()
    alternative.settlement_price *= np.exp(0.1 * np.cos(np.arange(len(prices))))
    frames = {"Shared final curve": alternative, "Bloomberg PX_SETTLE": official}
    long, lm = data.build_comparison(product, "2027-01-01", "2026-10-28", 90, iv, frames)
    short, sm = data.build_comparison(product, "2027-01-01", "2026-10-28", 10, iv, frames)
    assert lm["price_source"] == sm["price_source"] == "Bloomberg PX_SETTLE"
    pd.testing.assert_frame_equal(long.loc[long.cob_date.isin(short.cob_date)].reset_index(drop=True), short)
    assert short.tail(2).realized_volatility.isna().all()
    assert short.tail(2).volatility.notna().all()
    missing, meta = data.build_comparison(
        product, "2027-01-01", "2026-10-28", 90, iv, {"Shared final curve": alternative}
    )
    assert missing.realized_volatility.isna().all()
    assert meta["coverage"]["rv_observed"] == 0


@pytest.mark.parametrize("product", ["Brent", "HH", "TTF"])
def test_rv_independent_calculation_and_coverage(product):
    sessions, prices, iv = fixture(product)
    rows, meta = data.build_comparison(product, "2027-01-01", "2026-10-28", 90, iv, {"Bloomberg PX_SETTLE": prices})
    lookup = prices.set_index("cob_date").settlement_price.to_dict()
    for row in rows.itertuples():
        returns = []
        for i, d in enumerate(sessions):
            if row.cob_date - pd.Timedelta(days=30) < d <= row.cob_date:
                returns.append(math.log(lookup[d] / lookup[sessions[i - 1]]))
        mean = sum(returns) / len(returns)
        expected = math.sqrt(252 * sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
        assert row.realized_volatility == pytest.approx(expected, abs=1e-12)
    assert meta["coverage"]["paired_observed"] == len(rows)
    bad = prices.loc[prices.cob_date.ne("2026-10-19")]
    missing, _ = data.build_comparison(product, "2027-01-01", "2026-10-28", 30, iv, {"Bloomberg PX_SETTLE": bad})
    assert missing.loc[missing.cob_date.ge("2026-10-19"), "realized_volatility"].isna().all()


def test_product_change_resets_to_own_front_and_preserves_explicit_contract(monkeypatch):
    _, _, iv = fixture("HH")
    front = iv.copy()
    front.contract_date = pd.Timestamp("2026-12-01")
    frame = pd.concat([iv, front], ignore_index=True)
    monkeypatch.setattr(page.overview, "snapshot_frame", lambda *_: frame)
    monkeypatch.setattr(page.delta_history_data, "available_contracts", lambda *a: ["2026-12-01", "2027-01-01"])
    values = page.select_history_contracts({"snapshot_id": "test"}, "2026-10-28", "HH", 90, "2027-01-01", None, {"product": "Brent"})
    assert values[2] == "2026-12-01" and values[4] is None
    values = page.select_history_contracts({"snapshot_id": "test"}, "2026-10-28", "HH", 30, "2027-01-01", "2026-12-01", {"product": "HH"})
    assert values[2] == "2027-01-01" and values[4] == "2026-12-01"
    assert page.linked_history_contracts("2027-01-01", "2027-01-01") == ["2027-01-01"]


def test_rolling_contract_does_not_replace_missing_front_or_clamp_rank():
    frame = pd.DataFrame(
        [
            ("2026-10-01", "2026-11-01", "2026-10-20", 0.4),
            ("2026-10-01", "2026-12-01", "2026-11-20", 0.5),
            ("2026-10-02", "2026-12-01", "2026-11-20", 0.6),
            ("2026-10-21", "2026-12-01", "2026-11-20", 0.7),
        ],
        columns=["cob_date", "contract_date", "option_expiration_date", "volatility"],
    )
    for col in frame.columns[:3]:
        frame[col] = pd.to_datetime(frame[col])
    selected = surfaces._select_rolling_tenor_history(frame, 0)
    assert list(selected.volatility) == [0.4, 0.7]
    selected = surfaces._select_rolling_tenor_history(frame, 1)
    assert list(selected.volatility) == [0.5, 0.6]
    assert surfaces._select_rolling_tenor_history(frame, 2).empty


def test_warm_process_rejects_expired_and_wrong_namespace_reference(monkeypatch, tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    reference = store.publish("vol-surface-v2", "same", {})
    monkeypatch.setattr(surfaces, "_ACTIVE_SURFACE_SNAPSHOT_ID", reference["snapshot_id"])
    monkeypatch.setitem(surfaces.DATA_CACHE_STATE, "initialized", True)
    monkeypatch.setattr(surfaces, "resolve_snapshot", store.resolve)
    invalid = {**reference, "namespace": "other"}
    with pytest.raises(SnapshotReferenceError):
        surfaces._activate_surface_snapshot(invalid)
    store.cache.delete("snapshot:vol-surface-v2:" + reference["snapshot_id"])
    with pytest.raises(SnapshotReferenceError):
        surfaces._activate_surface_snapshot(reference)
    store.close()


def test_expired_reference_recovers_even_after_first_publication_poll(monkeypatch):
    from dash_options.calibration import publication_status
    import dash_options.core.config as config
    import dash_options.core.snapshots as snapshots

    monkeypatch.setattr(config, "get_database_engine", lambda: object())
    value = {"revision": "same", "publications": [], "jobs": []}
    monkeypatch.setattr(publication_status, "load_publication_status", lambda *a, **kw: deepcopy(value))

    def expired(*a, **kw):
        raise SnapshotReferenceError("expired")

    monkeypatch.setattr(snapshots, "resolve_snapshot", expired)
    _, _, clicks = page.poll_surface_publications(
        200, "2026-10-30", {"cob": "2026-10-30", "revision": "same"}, 4, {"snapshot_id": "old"}
    )
    assert clicks == 5 and clicks is not no_update


def test_both_charts_roll_to_same_contract_without_price_jump_returns(monkeypatch):
    from dash_options.features.vol_surface import delta_history as history
    from options.market_data.realized_comparison import comparison_calendar

    _, _, sessions, _ = comparison_calendar("Brent", "2026-10-28", 30)
    frames = []
    prices = []
    for month, expiry, base in [("2026-11-01", "2026-10-20", 50.0), ("2026-12-01", "2026-11-20", 100.0)]:
        frames.append(
            pd.DataFrame(
                {
                    "cob_date": sessions,
                    "contract_date": pd.Timestamp(month),
                    "option_expiration_date": pd.Timestamp(expiry),
                    "code": "Brent",
                    "delta_bucket": "ATM",
                    "put_call": "call",
                    "volatility": 0.4,
                }
            )
        )
        prices.append(
            pd.DataFrame(
                {
                    "cob_date": sessions,
                    "contract_date": pd.Timestamp(month),
                    "settlement_price": base * np.exp(0.01 * np.sin(np.arange(len(sessions)))),
                }
            )
        )
    surface = pd.concat(frames, ignore_index=True)
    raw = pd.concat(prices, ignore_index=True)

    def load(p, m, end, days, frame, revision):
        return data.build_comparison(p, m, end, days, frame, {"Bloomberg PX_SETTLE": raw})

    monkeypatch.setattr(history.realized_comparison_data, "load_comparison", load)
    result, meta = history.load_rolling_comparison("Brent", "2026-12-01", "2026-10-28", 30, surface)
    schedule, rank = history.rolling_schedule(surface, "Brent", "2026-12-01", "2026-10-28", 30)
    assert result.contract_date.equals(schedule.contract_date)
    assert rank == 1 and meta["tenor_rank"] == 1
    assert result.loc[result.cob_date.eq("2026-10-19"), "contract_date"].iloc[0] == pd.Timestamp("2026-11-01")
    assert result.loc[result.cob_date.eq("2026-10-20"), "contract_date"].iloc[0] == pd.Timestamp("2026-12-01")
    assert result.realized_volatility.max() < 0.2
