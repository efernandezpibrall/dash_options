from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from options.options_library import asian_76
from options.ttf_volatility import black76_call_delta
from pages import pricer
import pricer_surface_reference as surface_reference
from pricer_structure import build_delivery_month_component, calculate_structure


AS_OF = date(2026, 7, 30)


@pytest.fixture(autouse=True)
def clear_surface_reference_caches():
    surface_reference.clear_published_surface_reference_cache()


def _leg(leg_id="leg-1", *, strike=100.0, call_put="C", basis="VOL"):
    return {
        "leg_id": leg_id,
        "name": leg_id,
        "side": "BUY",
        "ratio": 1.0,
        "call_put": call_put,
        "strike": strike,
        "quote_basis": basis,
        "quote_value": 0.4 if basis == "VOL" else 2.0,
    }


def _surface_expiry(asset, month, as_of=AS_OF):
    model = "asian76" if asset == "JKM" else "black76"
    return date.fromisoformat(
        build_delivery_month_component(asset, model, month, as_of, 100.0)[
            "option_expiration_date"
        ]
    )


def _loader(vol_by_month, *, saved_forward=80.0):
    def load(asset, _valuation_date, months, *, force_refresh=False, engine=None):
        del force_refresh, engine
        rows = []
        for month in months:
            expiry = _surface_expiry(asset, month)
            volatility = vol_by_month[(month.year, month.month)]
            for delta in (0.02, 0.10, 0.25, 0.50, 0.75, 0.90, 0.98):
                rows.append(
                    {
                        "contract_date": month,
                        "option_expiration_date": expiry,
                        "delta": delta,
                        "volatility": volatility,
                        "working_forward": saved_forward,
                        "created_at": pd.Timestamp("2026-08-24T12:00:00Z"),
                    }
                )
        return {
            "publication_id": "00000000-0000-0000-0000-000000000001",
            "run_id": "00000000-0000-0000-0000-000000000002",
            "commodity": asset,
            "cob_date": "2026-07-30",
            "published_at": "2026-08-24T12:00:00+00:00",
            "published_by": "tester",
            "points": pd.DataFrame(rows),
        }

    return load


def _monthly_context(asset, model, month, *, forward=100.0, custom_expiry=None):
    component = build_delivery_month_component(asset, model, month, AS_OF, forward)
    expiration = custom_expiry or date.fromisoformat(
        component["option_expiration_date"]
    )
    context = {
        "premium_convention": "futures_style",
        "delivery_shape": "MONTH",
        "delivery_month": date.fromisoformat(month).replace(day=1).isoformat(),
        "forward": forward,
        "rate": 0.0,
        "expiration_date": expiration.isoformat(),
        "contract_expiration_date": component["contract_expiration_date"],
    }
    if model == "asian76":
        context["averaging_start_date"] = component["averaging_start_date"]
    return context, component


def _snapshot(asset, model, context, rows):
    return calculate_structure(
        model,
        {**context, "asset": asset},
        {"structure_quantity": 1, "contract_multiplier": 1.0},
        rows,
        as_of=AS_OF,
    )


def _comparison_item(snapshot, structure_id="structure-1", label="S1"):
    return {
        "structure_id": structure_id,
        "structure_label": label,
        "snapshot": snapshot,
    }


def _operational_loader(volatility, *, cob_date="2026-07-30"):
    def load(asset, _valuation_date, months, *, force_refresh=False):
        del force_refresh
        pricing_asset = "Brent" if asset == "BRENT" else asset
        rows = []
        for month in months:
            expiry = date.fromisoformat(
                build_delivery_month_component(
                    pricing_asset,
                    "black76",
                    month,
                    AS_OF,
                    100.0,
                )["option_expiration_date"]
            )
            for delta in (0.02, 0.10, 0.25, 0.50, 0.75, 0.90, 0.98):
                rows.append(
                    {
                        "contract_date": month,
                        "option_expiration_date": expiry,
                        "delta": delta,
                        "put_call": "call",
                        "volatility": volatility,
                    }
                )
        return {
            "publication_id": None,
            "commodity": asset,
            "cob_date": cob_date,
            "published_at": None,
            "source": "raw.icap.implied_volatility_surface_from_prices",
            "source_kind": "operational",
            "date_fallback_used": cob_date < AS_OF.isoformat(),
            "source_fallback_used": False,
            "points": pd.DataFrame(rows),
        }

    return load


def _leaf_columns(definitions):
    return [
        child
        for group in definitions
        for child in (group.get("children") or [group])
    ]


def _assert_compact_surface_tooltip(value, model):
    parts = value.split(" · ")
    assert parts[0] == "Published: 2026-08-24 12:00 UTC"
    assert len(parts) in {2, 3}
    model_label = "Asian-76" if model == "asian76" else "Black-76"
    assert parts[1].startswith(f"Surface call delta ({model_label}): ")
    assert parts[1].endswith("%")
    if len(parts) == 3:
        assert parts[2].startswith("Expiry adjustment: ")


def test_only_black_and_asian_add_two_read_only_surface_columns():
    for model in ("black76", "asian76"):
        definitions = pricer._leg_column_defs(model)
        groups = [group["headerName"] for group in definitions]
        assert groups[2:4] == ["Volatility", "Published surface"]
        assert "pricer-result-column-group-published" in definitions[3][
            "headerClass"
        ]
        surface_columns = definitions[3]["children"]
        assert [column["headerName"] for column in surface_columns] == [
            "Input vol",
            "Pricing vol",
        ]
        assert all(column["editable"] is False for column in surface_columns)
        assert all(".2%" in column["valueFormatter"]["function"] for column in surface_columns)
        assert all("surfaceRows" in column["valueGetter"]["function"] for column in surface_columns)
        assert all("surfaceRows" in column["tooltipValueGetter"]["function"] for column in surface_columns)

    kirk_definitions = pricer._leg_column_defs("kirk")
    assert "Published surface" not in [
        group["headerName"] for group in kirk_definitions
    ]
    assert not any(
        column.get("colId", "").startswith("surface_")
        for column in _leaf_columns(kirk_definitions)
    )


def test_new_pricer_consolidates_surface_vols_and_places_premium_then_value():
    definitions = pricer._leg_column_defs(
        "black76",
        signed_lots=True,
        use_published_surface=True,
    )
    assert [group["headerName"] for group in definitions] == [
        "Leg",
        "Leg inputs",
        "Volatility",
        "Volatility adjustment",
        "",
        "Premium",
        "Unit Greeks",
        "Position Greeks",
    ]
    volatility_columns = definitions[2]["children"]
    assert [column["headerName"] for column in volatility_columns] == [
        "Input vol",
        "ATM",
        "Skew",
    ]
    assert [column["colId"] for column in volatility_columns] == [
        "surface_input_vol",
        "surface_atm_input_vol",
        "surface_skew_input_vol",
    ]
    assert all(
        "surfaceRows" in column["valueGetter"]["function"]
        for column in volatility_columns
    )
    assert "pricingRows" in volatility_columns[0]["valueGetter"]["function"]
    adjustment_columns = definitions[3]["children"]
    assert [column["headerName"] for column in adjustment_columns] == [
        "ATM",
        "Skew",
        "Smile",
    ]
    assert [column["field"] for column in adjustment_columns] == [
        "atm_vol_adjustment",
        "skew_vol_adjustment",
        "smile_vol_adjustment",
    ]
    assert all(column["editable"] for column in adjustment_columns)
    assert all("volatility percentage points" in column["headerTooltip"] for column in adjustment_columns)
    pricing_vol_group = definitions[4]
    assert pricing_vol_group["headerName"] == ""
    assert "pricer-result-column-group-volatility" in pricing_vol_group[
        "headerClass"
    ]
    assert len(pricing_vol_group["children"]) == 1
    pricing_vol_column = pricing_vol_group["children"][0]
    assert pricing_vol_column["headerName"] == "Pricing vol"
    assert pricing_vol_column["colId"] == "surface_pricing_vol"
    assert pricing_vol_column["editable"] is False
    assert "surfaceRows" in pricing_vol_column["valueGetter"]["function"]
    assert "pricingRows" in pricing_vol_column["valueGetter"]["function"]
    assert "pricer-result-column-group-premium" in definitions[5]["headerClass"]
    assert [column["headerName"] for column in definitions[5]["children"]] == [
        "Premium",
        "Value",
    ]


def test_current_pricer_leg_grid_uses_route_specific_compact_geometry():
    expected_widths = {
        "name": 86,
        "ratio": 50,
        "call_put": 70,
        "strike": 60,
        "surface_input_vol": 70,
        "surface_atm_input_vol": 58,
        "surface_skew_input_vol": 58,
        "atm_vol_adjustment": 58,
        "skew_vol_adjustment": 58,
        "smile_vol_adjustment": 58,
        "surface_pricing_vol": 72,
        "unit_value": 68,
        "trade_value": 74,
        "unit_delta": 60,
        "unit_gamma": 60,
        "unit_theta": 60,
        "unit_vega": 60,
        "unit_rho": 60,
        "trade_delta": 68,
        "trade_gamma": 68,
        "trade_theta": 68,
        "trade_vega": 68,
        "trade_rho": 68,
    }

    for model in ("black76", "asian76"):
        definitions = pricer._leg_column_defs(
            model,
            signed_lots=True,
            use_published_surface=True,
        )
        columns = _leaf_columns(definitions)
        widths = {
            column.get("field") or column.get("colId"): column["minWidth"]
            for column in columns
        }
        assert widths == expected_widths
        assert sum(widths.values()) == 1480
        strike_column = next(
            column for column in columns if column.get("field") == "strike"
        )
        assert strike_column["maxWidth"] == 60
        assert all(
            column.get("flex") == 1
            for column in columns
            if (column.get("field") or column.get("colId")) != "name"
        )
        assert all(column.get("tooltipValueGetter") for column in columns)

    definitions = pricer._leg_column_defs(
        "black76",
        signed_lots=True,
        use_published_surface=True,
    )
    assert "pricer-result-column-group-volatility" in definitions[2][
        "headerClass"
    ]
    assert "pricer-result-column-group-adjustment" in definitions[3][
        "headerClass"
    ]
    assert "pricer-result-column-group-premium" in definitions[5]["headerClass"]
    assert "pricer-result-column-group-unit" in definitions[6]["headerClass"]
    assert "pricer-result-column-group-position" in definitions[7]["headerClass"]
    assert all(
        "pricer-column-group-start" in group["children"][0]["cellClass"]
        for group in definitions
        if group.get("children")
    )

    current_grid = pricer._build_legs_grid(
        signed_lots=True,
        use_published_surface=True,
    )
    legacy_grid = pricer._build_legs_grid()
    assert current_grid.dashGridOptions["rowHeight"] == 28
    assert current_grid.dashGridOptions["headerHeight"] == 30
    assert current_grid.dashGridOptions["groupHeaderHeight"] == 24
    assert current_grid.dashGridOptions["tooltipShowMode"] == "whenTruncated"
    assert current_grid.dashGridOptions["rowSelection"]["checkboxes"] is False
    assert "selectionColumnDef" not in current_grid.dashGridOptions
    current_name = next(
        column
        for column in _leaf_columns(current_grid.columnDefs)
        if column.get("field") == "name"
    )
    assert current_name["cellRenderer"] == "PricerLegSelector"
    assert legacy_grid.dashGridOptions["rowHeight"] == 30
    assert legacy_grid.dashGridOptions["headerHeight"] == 34
    assert legacy_grid.dashGridOptions["groupHeaderHeight"] == 27
    assert "tooltipShowMode" not in legacy_grid.dashGridOptions
    assert legacy_grid.dashGridOptions["rowSelection"]["checkboxes"] is True
    assert legacy_grid.dashGridOptions["selectionColumnDef"]["width"] == 34
    legacy_name = next(
        column
        for column in _leaf_columns(legacy_grid.columnDefs)
        if column.get("field") == "name"
    )
    assert legacy_name["width"] == 104
    assert "cellRenderer" not in legacy_name

    kirk_columns = {
        column.get("field") or column.get("colId"): column
        for column in _leaf_columns(
            pricer._leg_column_defs(
                "kirk",
                signed_lots=True,
                use_published_surface=True,
            )
        )
    }
    assert kirk_columns["unit_gamma_s1s2"]["minWidth"] == 96
    assert kirk_columns["trade_corr_sensitivity"]["minWidth"] == 88
    assert kirk_columns["trade_vega_equiv"]["minWidth"] == 92


def test_grid_merge_preserves_pricing_totals_and_keeps_references_out_of_rows():
    pricing = pricer._leg_grid_options()
    pricing["pinnedBottomRowData"] = [{"leg_id": "__total__", "trade_value": 4.0}]
    pricing["context"]["pricingRows"] = {"leg-1": {"trade_value": 4.0}}
    payload = {
        "schema_version": surface_reference.REFERENCE_SCHEMA_VERSION,
        "rows": {
            "leg-1": {
                "surface_input_vol": 0.4,
                "surface_pricing_vol": 0.35,
            }
        },
    }

    leg_row = _leg()
    rendered = pricer.render_leg_grid_options(pricing, payload)

    assert rendered["pinnedBottomRowData"] == pricing["pinnedBottomRowData"]
    assert rendered["context"]["pricingRows"] == pricing["context"]["pricingRows"]
    assert rendered["context"]["surfaceRows"] == payload["rows"]
    assert pricing["context"]["surfaceRows"] == {}
    assert "surface_input_vol" not in leg_row
    assert "surface_pricing_vol" not in leg_row


def test_ttf_monthly_rebases_forward_and_reverses_existing_expiry_factor():
    month = "2026-12-01"
    reference_expiry = _surface_expiry("TTF", month)
    selected_expiry = reference_expiry - timedelta(days=5)
    context, _component = _monthly_context(
        "TTF",
        "black76",
        month,
        forward=100.0,
        custom_expiry=selected_expiry,
    )
    rows = [
        _leg("call", strike=100.0, call_put="C"),
        _leg("put", strike=100.0, call_put="P", basis="PREMIUM"),
    ]

    payload = surface_reference.build_published_surface_reference(
        "TTF",
        "black76",
        context,
        rows,
        AS_OF,
        surface_loader=_loader({(2026, 12): 0.4}, saved_forward=72.0),
    )

    for leg_id in ("call", "put"):
        result = payload["rows"][leg_id]
        assert result["surface_input_vol"] == pytest.approx(0.4)
        assert result["surface_atm_input_vol"] == pytest.approx(0.4)
        assert result["surface_skew_input_vol"] == pytest.approx(0.0)
        assert result["surface_pricing_vol"] < result["surface_input_vol"]
        normalized = surface_reference._normalize_reference_context(
            "TTF", "black76", context, AS_OF
        )
        assert result["surface_input_vol"] * normalized[
            "vol_adjustment_factor"
        ] == pytest.approx(result["surface_pricing_vol"])
        assert result["surface_input_tooltip"] == result[
            "surface_pricing_tooltip"
        ]
        _assert_compact_surface_tooltip(
            result["surface_pricing_tooltip"], "black76"
        )


def test_hh_pricer_uses_operational_cme_lne_surface_as_the_unit_vol_source():
    month = "2026-12-01"
    context, component = _monthly_context(
        "HH",
        "black76",
        month,
        forward=3.25,
    )
    context["premium_convention"] = "upfront"
    context["rate"] = 0.04

    def governed_loader(*_args, **_kwargs):
        raise AssertionError("HH must use the operational LNE surface loader")

    rows = [_leg(strike=3.25)]
    payload = surface_reference.build_published_surface_reference(
        "HH",
        "black76",
        context,
        rows,
        AS_OF,
        surface_loader=governed_loader,
        operational_loader=_operational_loader(0.55),
    )

    result = payload["rows"]["leg-1"]
    assert component["expiry_convention_code"] == "CME_HH_LNE_560_EXPIRY"
    assert component["variance_calendar_code"] == "CME_NYMEX_HH_OPTION_TRADING"
    assert payload["source_kind"] == "operational"
    assert payload["source_revision"]
    assert payload["publication_id"] is None
    assert payload["publication_cob"] == AS_OF.isoformat()
    assert result["surface_input_vol"] == pytest.approx(0.55)
    assert result["surface_pricing_vol"] == pytest.approx(0.55)
    assert result["surface_atm_input_vol"] == pytest.approx(0.55)
    assert result["surface_skew_input_vol"] == pytest.approx(0.0)
    assert result["surface_input_tooltip"].startswith(
        "Surface COB: 2026-07-30 · Source: raw.icap."
    )
    expected_signature = pricer._surface_reference_input_signature(
        "HH",
        "black76",
        rows,
        context,
        AS_OF.isoformat(),
    )
    payload["_ui_reference_signature"] = expected_signature
    resolved_rows, source_signature = pricer._published_surface_calculation_rows(
        "HH",
        "black76",
        rows,
        payload,
        expected_signature,
    )
    assert resolved_rows[0]["quote_value"] == pytest.approx(0.55)
    assert source_signature["source_kind"] == "operational"
    assert source_signature["source_revision"] == payload["source_revision"]


def test_brent_pricer_uses_the_operational_surface_for_unit_analytics():
    context, _component = _monthly_context(
        "Brent",
        "black76",
        "2026-12-01",
        forward=80.0,
    )

    def governed_loader(*_args, **_kwargs):
        raise AssertionError("Brent must use the operational surface loader")

    rows = [_leg(strike=80.0)]
    payload = surface_reference.build_published_surface_reference(
        "Brent",
        "black76",
        context,
        rows,
        AS_OF,
        surface_loader=governed_loader,
        operational_loader=_operational_loader(0.42),
    )

    result = payload["rows"]["leg-1"]
    assert payload["asset"] == "Brent"
    assert payload["source_kind"] == "operational"
    assert payload["source_revision"]
    assert payload["publication_cob"] == AS_OF.isoformat()
    assert result["surface_input_vol"] == pytest.approx(0.42)
    assert result["surface_pricing_vol"] == pytest.approx(0.42)


def test_cme_bzo_uses_user_forward_and_ice_brent_surface_with_target_expiry():
    month = "2026-12-01"
    component = build_delivery_month_component(
        "Brent",
        "black76",
        month,
        AS_OF,
        80.0,
        mapping_id="CME-BRENT-BZO",
    )
    context = {
        "exchange_mapping_id": "CME-BRENT-BZO",
        "premium_convention": "futures_style",
        "delivery_shape": "MONTH",
        "delivery_month": month,
        "forward": 80.0,
        "rate": 0.0,
        "expiration_date": component["option_expiration_date"],
        "contract_expiration_date": component["contract_expiration_date"],
    }
    rows = [_leg(strike=80.0)]

    payload = surface_reference.build_published_surface_reference(
        "Brent",
        "black76",
        context,
        rows,
        AS_OF,
        operational_loader=_operational_loader(0.42),
    )
    snapshot = _snapshot("Brent", "black76", context, rows)

    assert payload["source_kind"] == "operational"
    assert payload["rows"]["leg-1"]["surface_input_vol"] == pytest.approx(0.42)
    assert snapshot["context"]["exchange_mapping_id"] == "CME-BRENT-BZO"
    assert (
        snapshot["context"]["expiry_convention_code"]
        == "CME_BRENT_BZO_504_EXPIRY"
    )
    assert snapshot["context"]["forward_source"] == "USER_INPUT"
    assert snapshot["context"]["volatility_surface_source"] == "ICE_BRENT"


def test_phe_allows_the_governed_one_day_january_2033_surface_extension():
    month = "2033-01-01"
    component = build_delivery_month_component(
        "HH", "black76", month, AS_OF, 3.0, mapping_id="ICE-HH-PHE"
    )
    assert component["surface_option_expiration_date"] == "2032-12-27"
    assert component["option_expiration_date"] == "2032-12-28"
    context = {
        "exchange_mapping_id": "ICE-HH-PHE",
        "premium_convention": "upfront",
        "delivery_shape": "MONTH",
        "delivery_month": month,
        "forward": 3.0,
        "rate": 0.03,
        "expiration_date": component["option_expiration_date"],
        "contract_expiration_date": component["contract_expiration_date"],
    }

    payload = surface_reference.build_published_surface_reference(
        "HH",
        "black76",
        context,
        [_leg(strike=3.0)],
        AS_OF,
        operational_loader=_operational_loader(0.5),
    )
    result = payload["rows"]["leg-1"]

    assert result["surface_pricing_vol"] > 0.5
    assert "2032-12-27 to 2032-12-28" in result["surface_pricing_tooltip"]
    assert "extended" in result["surface_pricing_tooltip"]


@pytest.mark.parametrize(
    ("mapping_id", "asset", "model", "month", "direction"),
    [
        ("CME-TTF-TTL", "TTF", "black76", "2031-12-01", "shortening"),
        ("CME-TTF-TFP", "TTF", "black76", "2026-10-01", "extension"),
        ("CME-TTF-TFF", "TTF", "black76", "2026-10-01", "extension"),
        ("CME-JKM-JKO", "JKM", "asian76", "2027-03-01", "shortening"),
        ("CME-JKM-JFO", "JKM", "asian76", "2027-03-01", "shortening"),
        ("CME-HH-ON", "HH", "american_futures", "2026-09-01", "equal"),
    ],
)
def test_remaining_mappings_apply_and_disclose_governed_expiry_factors(
    mapping_id,
    asset,
    model,
    month,
    direction,
):
    mapping = pricer.exchange_option_mapping(mapping_id)
    forward = 12.0 if asset != "HH" else 3.0
    component = build_delivery_month_component(
        asset,
        model,
        month,
        AS_OF,
        forward,
        mapping_id=mapping_id,
    )
    context = {
        "exchange_mapping_id": mapping_id,
        "premium_convention": mapping.premium_convention,
        "delivery_shape": "MONTH",
        "delivery_month": month,
        "forward": forward,
        "rate": 0.03,
        "expiration_date": component["option_expiration_date"],
        "contract_expiration_date": component["contract_expiration_date"],
    }
    if model == "asian76":
        context["averaging_start_date"] = component["averaging_start_date"]
    year, month_number = map(int, month[:7].split("-"))
    loader = (
        {"surface_loader": _loader({(year, month_number): 0.4})}
        if asset in {"TTF", "JKM"}
        else {"operational_loader": _operational_loader(0.4)}
    )

    payload = surface_reference.build_published_surface_reference(
        asset,
        model,
        context,
        [_leg(strike=forward)],
        AS_OF,
        **loader,
    )
    row = payload["rows"]["leg-1"]
    adjustment = row["surface_expiry_adjustments"][0]

    assert adjustment["source_expiry"] == component[
        "surface_option_expiration_date"
    ]
    assert adjustment["target_expiry"] == component["option_expiration_date"]
    assert adjustment["direction"] == direction
    assert adjustment["source_governed_days"] > 0
    assert adjustment["target_governed_days"] > 0
    if direction == "shortening":
        assert adjustment["factor"] < 1.0
        assert row["surface_pricing_vol"] < 0.4
    elif direction == "extension":
        assert adjustment["factor"] > 1.0
        assert row["surface_pricing_vol"] > 0.4
    else:
        assert adjustment["factor"] == pytest.approx(1.0)
        assert row["surface_pricing_vol"] == pytest.approx(0.4)


def test_tfp_strip_uses_distinct_monthly_expiry_factors_before_flattening():
    context = {
        "exchange_mapping_id": "CME-TTF-TFP",
        "premium_convention": "upfront",
        "delivery_shape": "Q1",
        "delivery_year": 2027,
        "forward": 12.0,
        "rate": 0.03,
    }
    payload = surface_reference.build_published_surface_reference(
        "TTF",
        "black76",
        context,
        [_leg(strike=12.0)],
        AS_OF,
        surface_loader=_loader(
            {(2027, 1): 0.4, (2027, 2): 0.4, (2027, 3): 0.4}
        ),
    )
    row = payload["rows"]["leg-1"]
    adjustments = row["surface_expiry_adjustments"]

    assert len(adjustments) == 3
    assert len({round(item["factor"], 10) for item in adjustments}) > 1
    assert all(item["direction"] == "extension" for item in adjustments)
    assert row["surface_pricing_vol"] > 0.4


@pytest.mark.parametrize("mapping_id", ["CME-TTF-TFP", "CME-TTF-TFF"])
def test_ttf_usd_surface_selection_is_unit_independent_at_equal_moneyness(
    mapping_id,
):
    mapping = pricer.exchange_option_mapping(mapping_id)
    month = "2026-10-01"

    def skew_loader(
        asset,
        _valuation_date,
        months,
        *,
        force_refresh=False,
        engine=None,
    ):
        del force_refresh, engine
        rows = []
        for contract_month in months:
            expiry = _surface_expiry(asset, contract_month)
            for delta in (0.10, 0.25, 0.50, 0.75, 0.90):
                rows.append(
                    {
                        "contract_date": contract_month,
                        "option_expiration_date": expiry,
                        "delta": delta,
                        "volatility": 0.30 + 0.15 * abs(delta - 0.50),
                        "working_forward": 999.0,
                    }
                )
        return {
            "publication_id": "00000000-0000-0000-0000-000000000001",
            "run_id": "00000000-0000-0000-0000-000000000002",
            "commodity": asset,
            "cob_date": AS_OF.isoformat(),
            "published_at": "2026-08-24T12:00:00+00:00",
            "published_by": "tester",
            "points": pd.DataFrame(rows),
        }

    vols = []
    for forward in (12.0, 42.0):
        component = build_delivery_month_component(
            "TTF",
            "black76",
            month,
            AS_OF,
            forward,
            mapping_id=mapping_id,
        )
        context = {
            "exchange_mapping_id": mapping_id,
            "premium_convention": mapping.premium_convention,
            "delivery_shape": "MONTH",
            "delivery_month": month,
            "forward": forward,
            "rate": 0.03,
            "expiration_date": component["option_expiration_date"],
            "contract_expiration_date": component["contract_expiration_date"],
        }
        payload = surface_reference.build_published_surface_reference(
            "TTF",
            "black76",
            context,
            [_leg(strike=forward)],
            AS_OF,
            surface_loader=skew_loader,
        )
        vols.append(payload["rows"]["leg-1"]["surface_pricing_vol"])

    assert vols[0] == pytest.approx(vols[1], rel=1e-12, abs=1e-12)


@pytest.mark.parametrize(
    ("asset", "mapping_id", "premium_convention"),
    [
        ("TTF", "CME-TTF-TFO", "futures_style"),
        ("TTF", "CME-TTF-TTO", "upfront"),
        ("NBP", "CME-NBP-UFO", "futures_style"),
        ("NBP", "CME-NBP-UKO", "upfront"),
    ],
)
def test_cme_ttf_nbp_pairs_use_ice_surface_with_target_expiry_adjustment(
    asset,
    mapping_id,
    premium_convention,
):
    month = "2028-01-01"
    forward = 42.0
    component = build_delivery_month_component(
        asset, "black76", month, AS_OF, forward, mapping_id=mapping_id
    )
    assert component["surface_option_expiration_date"] == "2027-12-24"
    expected_target_expiry = "2027-12-24" if asset == "TTF" else "2027-12-23"
    assert component["option_expiration_date"] == expected_target_expiry
    context = {
        "exchange_mapping_id": mapping_id,
        "premium_convention": premium_convention,
        "delivery_shape": "MONTH",
        "delivery_month": month,
        "forward": forward,
        "rate": 0.03,
        "expiration_date": component["option_expiration_date"],
        "contract_expiration_date": component["contract_expiration_date"],
    }
    kwargs = (
        {"surface_loader": _loader({(2028, 1): 0.4})}
        if asset == "TTF"
        else {"operational_loader": _operational_loader(0.4)}
    )

    payload = surface_reference.build_published_surface_reference(
        asset,
        "black76",
        context,
        [_leg(strike=forward)],
        AS_OF,
        **kwargs,
    )
    result = payload["rows"]["leg-1"]

    if asset == "TTF":
        assert result["surface_pricing_vol"] == pytest.approx(0.4)
        assert "Expiry adjustment" not in result["surface_pricing_tooltip"]
    else:
        assert 0.0 < result["surface_pricing_vol"] < 0.4
        assert "2027-12-24 to 2027-12-23" in result[
            "surface_pricing_tooltip"
        ]
        assert "shortened" in result["surface_pricing_tooltip"]


def test_surface_extension_rejects_more_than_the_mapping_allowance():
    month = date(2033, 1, 1)
    publication = _operational_loader(0.5)("HH", AS_OF, [month])
    component = {
        "contract_month": month.isoformat(),
        "option_expiration_date": "2032-12-30",
        "max_surface_extension_days": 1,
        "weight": 1.0,
        "forward": 3.0,
    }

    with pytest.raises(
        surface_reference.SurfaceReferenceError,
        match="this mapping permits 1",
    ):
        surface_reference._prepare_component_surface(
            publication["points"],
            component,
            asset="HH",
            valuation_date=AS_OF,
            current_forward=3.0,
        )


def test_surface_input_vol_decomposes_exactly_into_atm_and_skew():
    month = "2026-12-01"
    context, _component = _monthly_context(
        "TTF",
        "black76",
        month,
        forward=100.0,
    )

    def skewed_loader(
        asset,
        _valuation_date,
        months,
        *,
        force_refresh=False,
        engine=None,
    ):
        del force_refresh, engine
        rows = []
        for contract_month in months:
            expiry = _surface_expiry(asset, contract_month)
            for delta in (0.02, 0.10, 0.25, 0.50, 0.75, 0.90, 0.98):
                rows.append(
                    {
                        "contract_date": contract_month,
                        "option_expiration_date": expiry,
                        "delta": delta,
                        "volatility": 0.4 + 0.2 * abs(delta - 0.5),
                        "working_forward": 100.0,
                    }
                )
        return {
            "publication_id": "00000000-0000-0000-0000-000000000021",
            "run_id": "00000000-0000-0000-0000-000000000022",
            "commodity": asset,
            "cob_date": AS_OF.isoformat(),
            "published_at": "2026-08-24T12:00:00+00:00",
            "published_by": "tester",
            "points": pd.DataFrame(rows),
        }

    payload = surface_reference.build_published_surface_reference(
        "TTF",
        "black76",
        context,
        [_leg(strike=110.0)],
        AS_OF,
        surface_loader=skewed_loader,
    )
    result = payload["rows"]["leg-1"]

    assert result["surface_atm_input_vol"] == pytest.approx(0.4)
    assert result["surface_skew_input_vol"] > 0.0
    assert result["surface_input_vol"] == pytest.approx(
        result["surface_atm_input_vol"] + result["surface_skew_input_vol"]
    )


def test_jkm_black_uses_apo_publication_expiry_and_jkz_pricing_expiry():
    month = "2026-11-01"
    context, component = _monthly_context(
        "JKM", "black76", month, forward=23.0
    )
    apo_expiry = _surface_expiry("JKM", month)
    jkz_expiry = date.fromisoformat(component["option_expiration_date"])
    assert jkz_expiry < apo_expiry

    payload = surface_reference.build_published_surface_reference(
        "JKM",
        "black76",
        context,
        [_leg(strike=23.0)],
        AS_OF,
        surface_loader=_loader({(2026, 11): 0.6}),
    )
    result = payload["rows"]["leg-1"]

    assert 0 < result["surface_pricing_vol"] < 0.6
    assert result["surface_input_vol"] == pytest.approx(
        result["surface_pricing_vol"]
    )
    _assert_compact_surface_tooltip(result["surface_pricing_tooltip"], "black76")


def test_jkm_asian_surface_delta_uses_asian76_with_published_volatility():
    month = "2027-04-01"
    forward = 17.0
    published_volatility = 0.560741154658339
    context, _component = _monthly_context(
        "JKM", "asian76", month, forward=forward
    )

    payload = surface_reference.build_published_surface_reference(
        "JKM",
        "asian76",
        context,
        [_leg(strike=forward, basis="PREMIUM")],
        AS_OF,
        surface_loader=_loader({(2027, 4): published_volatility}),
    )
    result = payload["rows"]["leg-1"]
    normalized = surface_reference._normalize_reference_context(
        "JKM", "asian76", context, AS_OF
    )
    component = surface_reference._delivery_components(normalized)[0]
    expected_asian_delta = asian_76(
        "C",
        forward,
        forward,
        component["time_to_expiry"],
        component["time_to_averaging_start"],
        0.0,
        result["surface_pricing_vol"],
    )[1]
    black_delta = black76_call_delta(
        forward,
        forward,
        component["time_to_expiry"],
        result["surface_pricing_vol"],
    )

    assert expected_asian_delta != pytest.approx(black_delta)
    assert result["surface_pricing_vol"] == pytest.approx(published_volatility)
    assert result["surface_pricing_tooltip"].endswith(
        f"Surface call delta (Asian-76): {expected_asian_delta:.2%}"
    )
    _assert_compact_surface_tooltip(
        result["surface_pricing_tooltip"], "asian76"
    )


@pytest.mark.parametrize(
    ("asset", "model", "shape", "year", "vols"),
    [
        (
            "TTF",
            "black76",
            "Q4",
            2026,
            {(2026, 10): 0.35, (2026, 11): 0.42, (2026, 12): 0.48},
        ),
        (
            "JKM",
            "asian76",
            "Q1",
            2027,
            {(2027, 1): 0.50, (2027, 2): 0.58, (2027, 3): 0.66},
        ),
    ],
)
def test_strip_flat_vol_is_premium_equivalent(asset, model, shape, year, vols):
    context = {
        "premium_convention": "futures_style",
        "delivery_shape": shape,
        "delivery_year": year,
        "forward": 30.0,
        "rate": 0.0,
    }
    row = _leg(strike=30.0, call_put="P", basis="PREMIUM")
    payload = surface_reference.build_published_surface_reference(
        asset,
        model,
        context,
        [row],
        AS_OF,
        surface_loader=_loader(vols),
    )
    flat = payload["rows"]["leg-1"]["surface_pricing_vol"]
    assert flat is not None

    normalized = surface_reference._normalize_reference_context(
        asset, model, context, AS_OF
    )
    components = surface_reference._delivery_components(normalized)
    monthly_target = 0.0
    flat_target = 0.0
    for component in components:
        month = date.fromisoformat(component["contract_month"])
        monthly_vol = vols[(month.year, month.month)]
        monthly_target += component["weight"] * surface_reference._component_price(
            model, normalized, component, "P", 30.0, monthly_vol
        )
        flat_target += component["weight"] * surface_reference._component_price(
            model, normalized, component, "P", 30.0, flat
        )
    assert flat_target == pytest.approx(monthly_target, abs=1e-10)
    assert payload["rows"]["leg-1"]["surface_input_vol"] == pytest.approx(flat)
    assert payload["rows"]["leg-1"]["surface_atm_input_vol"] == pytest.approx(flat)
    assert payload["rows"]["leg-1"]["surface_skew_input_vol"] == pytest.approx(0.0)
    _assert_compact_surface_tooltip(
        payload["rows"]["leg-1"]["surface_pricing_tooltip"], model
    )


def test_failure_cells_are_advisory_and_explain_the_reason():
    unsupported = surface_reference.build_published_surface_reference(
        "Unsupported asset",
        "black76",
        {},
        [_leg()],
        AS_OF,
    )
    assert unsupported["rows"]["leg-1"]["surface_input_vol"] is None
    assert "not configured for this asset" in unsupported["rows"]["leg-1"][
        "surface_input_tooltip"
    ]

    context, _component = _monthly_context(
        "TTF", "black76", "2026-12-01", forward=100.0
    )
    outside = surface_reference.build_published_surface_reference(
        "TTF",
        "black76",
        context,
        [_leg(strike=1_000_000.0)],
        AS_OF,
        surface_loader=_loader({(2026, 12): 0.4}),
    )
    assert outside["rows"]["leg-1"]["surface_pricing_vol"] is None
    assert "outside the rebased published range" in outside["rows"]["leg-1"][
        "surface_pricing_tooltip"
    ]

    kirk = surface_reference.build_published_surface_reference(
        "TTF", "kirk", {}, [_leg()], AS_OF
    )
    assert kirk["rows"] == {}


def test_catalog_and_immutable_month_slice_are_cached(monkeypatch):
    surface_reference.clear_published_surface_reference_cache()
    calls = {"catalog": 0, "points": 0}

    def fake_catalog(_engine, asset, valuation_date):
        calls["catalog"] += 1
        return {
            "publication_id": "00000000-0000-0000-0000-000000000010",
            "run_id": "00000000-0000-0000-0000-000000000011",
            "commodity": asset,
            "cob_date": valuation_date.isoformat(),
            "published_at": "2026-08-24T12:00:00+00:00",
            "published_by": "tester",
        }

    def fake_points(_engine, _publication_id, months):
        calls["points"] += 1
        return pd.DataFrame({"contract_date": list(months)})

    monkeypatch.setattr(surface_reference, "_load_publication_catalog", fake_catalog)
    monkeypatch.setattr(surface_reference, "_load_publication_points", fake_points)
    monkeypatch.setattr(surface_reference, "source_config_fingerprint", lambda: "test")
    engine = object()
    month = date(2026, 12, 1)

    first = surface_reference.load_published_surface_slice(
        "TTF", AS_OF, [month], engine=engine
    )
    second = surface_reference.load_published_surface_slice(
        "TTF", AS_OF, [month], engine=engine
    )
    refreshed = surface_reference.load_published_surface_slice(
        "TTF", AS_OF, [month], force_refresh=True, engine=engine
    )

    assert first["publication_id"] == second["publication_id"]
    assert refreshed["publication_id"] == first["publication_id"]
    assert calls == {"catalog": 2, "points": 1}


def test_publication_queries_are_select_only_and_month_bounded(monkeypatch):
    class Result:
        def mappings(self):
            return self

        def first(self):
            return {
                "publication_id": "00000000-0000-0000-0000-000000000010",
                "run_id": "00000000-0000-0000-0000-000000000011",
                "cob_date": AS_OF,
                "published_at": pd.Timestamp("2026-08-24T12:00:00Z"),
                "published_by": "tester",
            }

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, query, params):
            self.statements.append((str(query), params))
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Engine:
        def __init__(self):
            self.connection = Connection()

        def connect(self):
            return self.connection

    engine = Engine()
    surface_reference._load_publication_catalog(engine, "TTF", AS_OF)
    statement, params = engine.connection.statements[0]

    assert statement.lstrip().upper().startswith("SELECT")
    assert "p.cob_date <= :valuation_date" in statement
    assert "p.is_active" in statement
    assert params["valuation_date"] == AS_OF

    captured = {}

    def fake_read_sql(query, _connection, params):
        captured["statement"] = str(query)
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(surface_reference.pd, "read_sql", fake_read_sql)
    surface_reference._load_publication_points(
        engine,
        "00000000-0000-0000-0000-000000000010",
        (date(2026, 12, 1), date(2027, 2, 1)),
    )
    rendered = captured["statement"]
    assert rendered.lstrip().upper().startswith("SELECT")
    assert "contract_date >= :month_start_0" in rendered
    assert "contract_date < :month_end_1" in rendered
    assert captured["params"]["month_start_0"] == date(2026, 12, 1)
    assert captured["params"]["month_end_1"] == date(2027, 3, 1)
    assert not any(word in rendered.upper() for word in ("INSERT ", "UPDATE ", "DELETE "))


@pytest.mark.parametrize("model", ["black76", "asian76"])
def test_comparison_marker_uses_raw_contract_vol_and_model_consistent_delta(model):
    month = "2027-04-01"
    forward = 17.0
    context, _component = _monthly_context(
        "JKM",
        model,
        month,
        forward=forward,
    )
    snapshot = _snapshot(
        "JKM",
        model,
        context,
        [_leg(strike=18.0, basis="VOL")],
    )
    views = surface_reference.build_surface_comparison_views(
        [_comparison_item(snapshot)],
        surface_loader=_loader({(2027, 4): 0.56}),
    )

    assert len(views) == 1
    view = views[0]
    assert view["status"] == "ready"
    assert view["source_kind"] == "governed"
    assert len(view["quote_points"]) == 1
    assert all(0.0 < point["delta"] < 1.0 for point in view["curve_points"])
    quote = view["quote_points"][0]
    assert quote["contract_volatility"] == pytest.approx(
        snapshot["legs"][0]["raw_volatility"]
    )
    normalized_component = surface_reference._delivery_components(
        snapshot["context"]
    )[0]
    expected_call_delta = surface_reference._pricing_model_call_delta(
        model,
        snapshot["context"],
        normalized_component,
        quote["strike"],
        quote["pricing_volatility"],
    )
    assert quote["delta"] == pytest.approx(1.0 - expected_call_delta)


def test_asian_comparison_delta_differs_from_black_for_averaging_contract():
    month = "2027-04-01"
    items = []
    for index, model in enumerate(("black76", "asian76"), start=1):
        context, _component = _monthly_context(
            "JKM", model, month, forward=17.0
        )
        snapshot = _snapshot(
            "JKM", model, context, [_leg(strike=17.0, basis="VOL")]
        )
        items.append(_comparison_item(snapshot, f"structure-{index}", f"S{index}"))

    views = surface_reference.build_surface_comparison_views(
        items,
        surface_loader=_loader({(2027, 4): 0.56}),
    )
    deltas = {view["model"]: view["quote_points"][0]["delta"] for view in views}

    assert deltas["asian76"] != pytest.approx(deltas["black76"])


def test_short_dated_far_wing_delta_saturation_is_a_valid_axis_endpoint():
    context = {"forward": 100.0, "rate": 0.0}
    component = {"forward": 100.0, "time_to_expiry": 1.0 / 365.25}

    call_delta = surface_reference._pricing_model_call_delta(
        "black76",
        context,
        component,
        1_000.0,
        0.20,
    )

    assert call_delta == 0.0


def test_premium_implied_quote_marker_reconciles_with_published_surface_column():
    month = "2027-04-01"
    context, _component = _monthly_context(
        "JKM", "asian76", month, forward=17.0
    )
    direct_snapshot = _snapshot(
        "JKM",
        "asian76",
        context,
        [_leg(strike=17.0, basis="VOL")],
    )
    premium = direct_snapshot["legs"][0]["unit"]["value"]
    premium_leg = _leg(strike=17.0, basis="PREMIUM")
    premium_leg["quote_value"] = premium
    snapshot = _snapshot("JKM", "asian76", context, [premium_leg])
    loader = _loader({(2027, 4): 0.560741154658339})

    view = surface_reference.build_surface_comparison_views(
        [_comparison_item(snapshot)],
        surface_loader=loader,
    )[0]
    published = surface_reference.build_published_surface_reference(
        "JKM",
        "asian76",
        snapshot["context"],
        snapshot["legs"],
        AS_OF,
        surface_loader=loader,
    )
    quote = view["quote_points"][0]

    assert snapshot["legs"][0]["quote_basis"] == "PREMIUM"
    assert quote["quote_basis_label"] == "Premium-implied"
    assert quote["contract_volatility"] == pytest.approx(
        snapshot["legs"][0]["raw_volatility"]
    )
    assert quote["reference_volatility"] == pytest.approx(
        published["rows"]["leg-1"]["surface_input_vol"]
    )


@pytest.mark.parametrize(
    ("asset", "model", "shape", "year", "vols"),
    [
        (
            "TTF",
            "black76",
            "Q4",
            2026,
            {(2026, 10): 0.35, (2026, 11): 0.42, (2026, 12): 0.48},
        ),
        (
            "JKM",
            "asian76",
            "Q1",
            2027,
            {(2027, 1): 0.50, (2027, 2): 0.58, (2027, 3): 0.66},
        ),
    ],
)
def test_comparison_strip_curve_reproduces_weighted_monthly_premium(
    asset,
    model,
    shape,
    year,
    vols,
):
    context = {
        "asset": asset,
        "premium_convention": "futures_style",
        "delivery_shape": shape,
        "delivery_year": year,
        "forward": 30.0,
        "rate": 0.0,
    }
    snapshot = _snapshot(
        asset,
        model,
        context,
        [_leg(strike=30.0, call_put="P", basis="VOL")],
    )
    view = surface_reference.build_surface_comparison_views(
        [_comparison_item(snapshot)],
        surface_loader=_loader(vols),
    )[0]
    point = view["curve_points"][len(view["curve_points"]) // 2]
    strike = point["strike"]
    flat = point["pricing_volatility"]
    monthly_target = 0.0
    flat_target = 0.0
    for component in snapshot["context"]["delivery_components"]:
        month = date.fromisoformat(component["contract_month"])
        monthly_target += component["weight"] * surface_reference._component_price(
            model,
            snapshot["context"],
            component,
            "C",
            strike,
            vols[(month.year, month.month)],
        )
        flat_target += component["weight"] * surface_reference._component_price(
            model,
            snapshot["context"],
            component,
            "C",
            strike,
            flat,
        )

    assert flat_target == pytest.approx(monthly_target, abs=1e-10)
    arithmetic_vol = sum(
        component["weight"]
        * vols[
            (
                date.fromisoformat(component["contract_month"]).year,
                date.fromisoformat(component["contract_month"]).month,
            )
        ]
        for component in snapshot["context"]["delivery_components"]
    )
    assert flat != pytest.approx(arithmetic_vol, abs=1e-5)


def test_duplicate_contexts_share_one_card_and_source_batch_unions_months():
    december_context, _ = _monthly_context(
        "TTF", "black76", "2026-12-01", forward=100.0
    )
    january_context, _ = _monthly_context(
        "TTF", "black76", "2027-01-01", forward=100.0
    )
    december = _snapshot(
        "TTF", "black76", december_context, [_leg(strike=100.0)]
    )
    january = _snapshot(
        "TTF", "black76", january_context, [_leg(strike=101.0)]
    )
    calls = []
    base_loader = _loader({(2026, 12): 0.4, (2027, 1): 0.42})

    def loader(asset, valuation_date, months, **kwargs):
        calls.append((asset, valuation_date, tuple(months)))
        return base_loader(asset, valuation_date, months, **kwargs)

    views = surface_reference.build_surface_comparison_views(
        [
            _comparison_item(december, "structure-1", "S1"),
            _comparison_item(december, "structure-2", "S2"),
            _comparison_item(january, "structure-3", "S3"),
        ],
        surface_loader=loader,
    )

    assert len(calls) == 1
    assert {month.isoformat() for month in calls[0][2]} == {
        "2026-12-01",
        "2027-01-01",
    }
    assert len(views) == 2
    december_view = next(view for view in views if view["delivery_label"] == "Dec-26")
    assert december_view["structure_label"] == "S1, S2"
    assert len(december_view["quote_points"]) == 2


def test_operational_nbp_route_is_exact_and_prior_cob_is_visible():
    context, _component = _monthly_context(
        "NBP", "black76", "2026-11-01", forward=100.0
    )
    snapshot = _snapshot(
        "NBP", "black76", context, [_leg(strike=100.0, basis="VOL")]
    )

    def governed_loader(*_args, **_kwargs):
        raise AssertionError("NBP must not use the governed publication loader")

    view = surface_reference.build_surface_comparison_views(
        [_comparison_item(snapshot)],
        surface_loader=governed_loader,
        operational_loader=_operational_loader(0.45, cob_date="2026-07-29"),
    )[0]

    assert view["status"] == "ready"
    assert view["source_kind"] == "operational"
    assert view["surface_cob"] == "2026-07-29"
    assert any("Prior COB" in warning for warning in view["warnings"])
    assert view["quote_points"][0]["reference_volatility"] == pytest.approx(0.45)


def test_governed_failure_never_falls_back_to_operational_and_kirk_is_explicit():
    context, _component = _monthly_context(
        "TTF", "black76", "2026-12-01", forward=100.0
    )
    snapshot = _snapshot("TTF", "black76", context, [_leg(strike=100.0)])
    operational_calls = []

    def governed_loader(*_args, **_kwargs):
        raise surface_reference.SurfaceReferenceError("Governed publication missing")

    def operational_loader(*args, **_kwargs):
        operational_calls.append(args)
        raise AssertionError("Governed failure must not use operational data")

    failed = surface_reference.build_surface_comparison_views(
        [_comparison_item(snapshot)],
        surface_loader=governed_loader,
        operational_loader=operational_loader,
    )[0]
    kirk_snapshot = {
        "model": "kirk",
        "model_label": "Kirk",
        "calculation_date": AS_OF.isoformat(),
        "context": {"asset": "TTF"},
        "legs": [{"leg_id": "leg-1"}],
    }
    kirk = surface_reference.build_surface_comparison_views(
        [_comparison_item(kirk_snapshot)]
    )[0]

    assert failed["status"] == "error"
    assert failed["message"] == "Governed publication missing"
    assert operational_calls == []
    assert kirk["status"] == "unsupported"
    assert "two volatility inputs" in kirk["message"]


def test_out_of_range_quote_failure_is_isolated_inside_ready_card():
    context, _component = _monthly_context(
        "TTF", "black76", "2026-12-01", forward=100.0
    )
    snapshot = _snapshot(
        "TTF", "black76", context, [_leg(strike=1_000_000.0, basis="VOL")]
    )
    view = surface_reference.build_surface_comparison_views(
        [_comparison_item(snapshot)],
        surface_loader=_loader({(2026, 12): 0.4}),
    )[0]

    assert view["status"] == "ready"
    assert view["quote_points"] == []
    assert any("outside the rebased published range" in warning for warning in view["warnings"])
