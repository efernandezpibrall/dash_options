import json
from datetime import date, timedelta

import pytest

from options.options_library import (
    asian_76,
    black_76,
    black_76_futures_style,
    kirk_model_with_substitution,
    kirk_spread_greeks,
)
from pricer_structure import (
    GREEK_FIELDS,
    MAX_LEGS,
    MAX_OPTION_HORIZON_DAYS,
    SCHEMA_VERSION,
    StructureValidationError,
    _calculate_trade_value_only,
    asset_price_spec,
    available_delivery_months,
    available_jkm_apo_delivery_months,
    build_delivery_month_component,
    build_jkm_month_component,
    build_ttf_strip_components,
    calculate_structure,
    correlation_sensitivity_series,
    default_contract_size,
    default_model_for_asset,
    default_premium_convention,
    default_leg,
    expiration_extension_series,
    parallel_volatility_series,
    payoff_series,
    rate_sensitivity_series,
    time_decay_series,
)


AS_OF = date(2026, 7, 29)
EXPIRY = AS_OF + timedelta(days=92)


def black_context(contract_expiry=None):
    return {
        "forward": 100.0,
        "rate": 0.03,
        "expiration_date": EXPIRY.isoformat(),
        "contract_expiration_date": (contract_expiry or EXPIRY).isoformat(),
    }


def sizing(quantity=1.0, multiplier=1.0):
    return {
        "structure_quantity": quantity,
        "contract_multiplier": multiplier,
    }


def black_leg(
    sequence,
    *,
    side="BUY",
    ratio=1.0,
    call_put="C",
    strike=100.0,
    volatility=0.20,
):
    return {
        "leg_id": f"leg-{sequence}",
        "name": f"Leg {sequence}",
        "side": side,
        "ratio": ratio,
        "call_put": call_put,
        "strike": strike,
        "volatility": volatility,
    }


def assert_totals_reconcile(snapshot):
    scale = snapshot["sizing"]["position_scale"]
    assert snapshot["totals"]["unit_structure_value"] == pytest.approx(
        sum(leg["unit_contribution"]["value"] for leg in snapshot["legs"])
    )
    assert snapshot["totals"]["trade_value"] == pytest.approx(
        sum(leg["trade_contribution"]["value"] for leg in snapshot["legs"])
    )
    assert snapshot["totals"]["trade_value"] == pytest.approx(
        snapshot["totals"]["unit_structure_value"] * scale
    )
    for metric in snapshot["greek_fields"]:
        unit_values = [
            leg["unit_contribution"]["greeks"][metric] for leg in snapshot["legs"]
        ]
        trade_values = [
            leg["trade_contribution"]["greeks"][metric] for leg in snapshot["legs"]
        ]
        if any(value is None for value in unit_values):
            assert snapshot["totals"]["unit_structure_greeks"][metric] is None
            assert snapshot["totals"]["trade_greeks"][metric] is None
        else:
            assert snapshot["totals"]["unit_structure_greeks"][metric] == pytest.approx(
                sum(unit_values)
            )
            assert snapshot["totals"]["trade_greeks"][metric] == pytest.approx(
                sum(trade_values)
            )


def test_single_black_leg_matches_library_and_is_json_safe():
    snapshot = calculate_structure(
        "black76",
        {
            **black_context(),
            "asset": "Brent",
        },
        sizing(3, 100),
        [black_leg(1, strike=105, volatility=0.32)],
        as_of=AS_OF,
    )
    expected = black_76_futures_style("C", 100, 105, 92 / 365.25, 0.32)

    assert snapshot["legs"][0]["unit"]["value"] == pytest.approx(expected[0])
    assert snapshot["legs"][0]["unit"]["greeks"]["delta"] == pytest.approx(expected[1])
    assert snapshot["legs"][0]["unit"]["greeks"]["gamma"] == pytest.approx(expected[2])
    assert snapshot["legs"][0]["unit"]["greeks"]["theta"] == pytest.approx(expected[3])
    assert snapshot["legs"][0]["unit"]["greeks"]["vega"] == pytest.approx(expected[4])
    assert snapshot["legs"][0]["unit"]["greeks"]["rho"] == pytest.approx(expected[5])
    assert snapshot["totals"]["trade_value"] == pytest.approx(expected[0] * 300)
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["context"]["asset"] == "Brent"
    assert snapshot["context"]["premium_convention"] == "futures_style"
    assert snapshot["context"]["resolved_premium_convention"] == "futures_style"
    assert snapshot["context"]["rate"] == 0.0
    assert snapshot["input"]["context"]["asset"] == "Brent"
    json.dumps(snapshot)


def test_premium_convention_is_validated_as_part_of_the_shared_context():
    with pytest.raises(StructureValidationError, match="Premium convention must be"):
        calculate_structure(
            "black76",
            {**black_context(), "premium_convention": "Unsupported"},
            sizing(),
            [black_leg(1)],
            as_of=AS_OF,
        )

    with pytest.raises(StructureValidationError, match="Premium convention must be"):
        calculate_structure(
            "black76",
            {**black_context(), "premium_convention": "product_default"},
            sizing(),
            [black_leg(1)],
            as_of=AS_OF,
        )


@pytest.mark.parametrize(
    ("asset", "expected"),
    [
        ("TTF", "futures_style"),
        ("JKM", "futures_style"),
        ("HH", "upfront"),
        ("Brent", "futures_style"),
        ("NBP", "futures_style"),
    ],
)
def test_asset_selects_a_concrete_default_premium_convention(asset, expected):
    assert default_premium_convention(asset, "black76") == expected


@pytest.mark.parametrize(
    ("asset", "currency", "unit", "label"),
    [
        ("TTF", "EUR", "MWh", "EUR/MWh"),
        ("JKM", "USD", "MMBtu", "USD/MMBtu"),
        ("HH", "USD", "MMBtu", "USD/MMBtu"),
        ("Brent", "USD", "bbl", "USD/bbl"),
        ("NBP", "GBP", "therm", "GBp/therm"),
    ],
)
def test_asset_price_spec_exposes_currency_and_unit(asset, currency, unit, label):
    spec = asset_price_spec(asset)
    assert spec["currency"] == currency
    assert spec["unit"] == unit
    assert spec["price_unit_label"] == label


def test_ttf_month_contract_size_uses_exact_delivery_hours():
    assert default_contract_size(
        "TTF",
        {
            "delivery_shape": "MONTH",
            "expiration_date": "2026-09-25",
        },
        as_of=AS_OF,
    ) == pytest.approx(745.0)
    assert default_contract_size(
        "TTF",
        {
            "delivery_shape": "MONTH",
            "delivery_month": "2026-09-01",
            "expiration_date": "2030-01-01",
        },
        as_of=AS_OF,
    ) == pytest.approx(720.0)


def test_ttf_strip_contract_size_includes_dst_delivery_hours():
    assert default_contract_size(
        "TTF",
        {"delivery_shape": "Q4", "delivery_year": 2026},
        as_of=AS_OF,
    ) == pytest.approx(2209.0)


@pytest.mark.parametrize(
    ("shape", "expected"),
    [("MONTH", 10_000.0), ("Q3", 30_000.0), ("SUM", 60_000.0)],
)
def test_jkm_contract_size_uses_one_exchange_lot_per_delivery_month(
    shape,
    expected,
):
    assert default_contract_size(
        "JKM",
        {"delivery_shape": shape, "delivery_year": 2027},
        as_of=AS_OF,
    ) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("asset", "context", "expected"),
    [
        ("Brent", {}, 1_000.0),
        ("HH", {}, 2_500.0),
        ("NBP", {"delivery_month": "2026-11-01"}, 30_000.0),
        ("NBP", {"delivery_month": "2028-02-01"}, 29_000.0),
    ],
)
def test_exchange_contract_size_defaults_cover_brent_hh_and_nbp(
    asset,
    context,
    expected,
):
    assert default_contract_size(asset, context, as_of=AS_OF) == pytest.approx(
        expected
    )


def test_nbp_position_totals_convert_gbpence_quotes_to_gbp_once():
    snapshot = calculate_structure(
        "black76",
        {
            **black_context(),
            "asset": "NBP",
        },
        sizing(1, 30_000),
        [black_leg(1)],
        as_of=AS_OF,
    )

    assert snapshot["context"]["price_unit_label"] == "GBp/therm"
    assert snapshot["context"]["trade_currency"] == "GBP"
    assert snapshot["sizing"]["quantity_scale"] == pytest.approx(30_000)
    assert snapshot["sizing"]["price_currency_conversion_factor"] == pytest.approx(
        0.01
    )
    assert snapshot["sizing"]["position_scale"] == pytest.approx(300)
    assert snapshot["totals"]["trade_value"] == pytest.approx(
        snapshot["totals"]["unit_structure_value"] * 300
    )
    assert _calculate_trade_value_only(
        snapshot["model"],
        snapshot["input"]["context"],
        snapshot["input"]["sizing"],
        snapshot["input"]["legs"],
        AS_OF,
    ) == pytest.approx(snapshot["totals"]["trade_value"])


def test_kirk_default_remains_supported_for_upfront_default_assets():
    assert default_premium_convention("HH", "kirk") == "futures_style"


def test_asset_is_validated_as_part_of_the_shared_context():
    with pytest.raises(StructureValidationError, match="Asset must be one of"):
        calculate_structure(
            "black76",
            {**black_context(), "asset": "Unsupported"},
            sizing(),
            [black_leg(1)],
            as_of=AS_OF,
        )


def test_structure_quantity_is_normalized_to_a_positive_integer():
    snapshot = calculate_structure(
        "black76",
        black_context(),
        sizing("3.0", 2.5),
        [black_leg(1)],
        as_of=AS_OF,
    )

    assert snapshot["sizing"]["structure_quantity"] == 3
    assert isinstance(snapshot["sizing"]["structure_quantity"], int)
    assert snapshot["sizing"]["position_scale"] == pytest.approx(7.5)

    with pytest.raises(StructureValidationError, match="positive whole number"):
        calculate_structure(
            "black76",
            black_context(),
            sizing(1.5, 2.5),
            [black_leg(1)],
            as_of=AS_OF,
        )
    with pytest.raises(StructureValidationError, match="positive whole number"):
        calculate_structure(
            "black76",
            black_context(),
            sizing(None, 2.5),
            [black_leg(1)],
            as_of=AS_OF,
        )


@pytest.mark.parametrize(
    "legs",
    [
        [
            black_leg(1, side="BUY", call_put="C", strike=95, volatility=0.19),
            black_leg(2, side="SELL", call_put="C", strike=105, volatility=0.23),
        ],
        [
            black_leg(1, side="BUY", call_put="P", strike=105, volatility=0.24),
            black_leg(2, side="SELL", call_put="P", strike=95, volatility=0.20),
        ],
        [
            black_leg(1, side="BUY", call_put="C", strike=100, volatility=0.21),
            black_leg(2, side="BUY", call_put="P", strike=100, volatility=0.22),
        ],
        [
            black_leg(1, side="BUY", ratio=1, strike=90, volatility=0.18),
            black_leg(2, side="SELL", ratio=2, strike=100, volatility=0.21),
            black_leg(3, side="BUY", ratio=1, strike=110, volatility=0.25),
        ],
        [
            black_leg(1, side="BUY", call_put="P", strike=90, volatility=0.25),
            black_leg(2, side="SELL", call_put="P", strike=95, volatility=0.23),
            black_leg(3, side="SELL", call_put="C", strike=105, volatility=0.21),
            black_leg(4, side="BUY", call_put="C", strike=110, volatility=0.22),
        ],
    ],
    ids=["call-spread", "put-spread", "straddle", "butterfly", "condor"],
)
def test_canonical_structures_reconcile_signed_unit_and_trade_totals(legs):
    snapshot = calculate_structure(
        "black76",
        black_context(),
        sizing(8, 50),
        legs,
        as_of=AS_OF,
    )
    assert_totals_reconcile(snapshot)


def test_signed_lots_match_legacy_buy_sell_pricing_and_risk():
    legacy_legs = [
        black_leg(1, side="BUY", ratio=2, strike=95, volatility=0.19),
        black_leg(2, side="SELL", ratio=1, strike=105, volatility=0.23),
    ]
    signed_legs = []
    for leg, lots in zip(legacy_legs, (2, -1)):
        signed_leg = dict(leg)
        signed_leg.pop("side")
        signed_leg["ratio"] = lots
        signed_legs.append(signed_leg)

    legacy = calculate_structure(
        "black76",
        black_context(),
        sizing(8, 50),
        legacy_legs,
        as_of=AS_OF,
    )
    signed = calculate_structure(
        "black76",
        black_context(),
        sizing(8, 50),
        signed_legs,
        as_of=AS_OF,
    )

    assert [leg["side"] for leg in signed["legs"]] == ["BUY", "SELL"]
    assert [leg["ratio"] for leg in signed["legs"]] == [2, 1]
    assert [leg["weight"] for leg in signed["legs"]] == [2, -1]
    assert signed["totals"]["unit_structure_value"] == pytest.approx(
        legacy["totals"]["unit_structure_value"]
    )
    assert signed["totals"]["trade_value"] == pytest.approx(
        legacy["totals"]["trade_value"]
    )
    assert signed["totals"]["unit_structure_greeks"] == pytest.approx(
        legacy["totals"]["unit_structure_greeks"]
    )
    assert signed["totals"]["trade_greeks"] == pytest.approx(
        legacy["totals"]["trade_greeks"]
    )


def test_signed_lots_reject_zero_without_requiring_side():
    leg = black_leg(1, ratio=0)
    leg.pop("side")

    with pytest.raises(StructureValidationError, match="lots must be non-zero"):
        calculate_structure(
            "black76",
            black_context(),
            sizing(),
            [leg],
            as_of=AS_OF,
        )


def test_identical_long_and_short_legs_offset_every_metric():
    legs = [
        black_leg(1, side="BUY", strike=102, volatility=0.27),
        black_leg(2, side="SELL", strike=102, volatility=0.27),
    ]
    snapshot = calculate_structure(
        "black76",
        black_context(),
        sizing(12, 1000),
        legs,
        as_of=AS_OF,
    )
    assert snapshot["totals"]["unit_structure_value"] == pytest.approx(0.0)
    assert snapshot["totals"]["trade_value"] == pytest.approx(0.0)
    for value in snapshot["totals"]["trade_greeks"].values():
        assert value == pytest.approx(0.0)


def test_independent_strike_volatility_changes_each_legs_delta_and_vega():
    legs = [
        black_leg(1, strike=95, volatility=0.17),
        black_leg(2, strike=105, volatility=0.31),
    ]
    snapshot = calculate_structure(
        "black76",
        black_context(),
        sizing(),
        legs,
        as_of=AS_OF,
    )
    first, second = snapshot["legs"]
    assert first["raw_volatility"] == 0.17
    assert second["raw_volatility"] == 0.31
    assert first["unit"]["greeks"]["delta"] != pytest.approx(
        second["unit"]["greeks"]["delta"]
    )
    assert first["unit"]["greeks"]["vega"] != pytest.approx(
        second["unit"]["greeks"]["vega"]
    )


def test_contract_date_scaling_applies_once_and_input_vega_uses_chain_rule():
    contract_expiry = EXPIRY + timedelta(days=92)
    snapshot = calculate_structure(
        "black76",
        {**black_context(contract_expiry), "asset": "Brent"},
        sizing(),
        [black_leg(1, volatility=0.40)],
        as_of=AS_OF,
    )
    factor = snapshot["context"]["vol_adjustment_factor"]
    leg = snapshot["legs"][0]
    expected = black_76_futures_style(
        "C",
        100,
        100,
        92 / 365.25,
        0.40 * factor,
    )
    assert leg["volatility_used"] == pytest.approx(0.40 * factor)
    assert leg["unit"]["greeks"]["vega"] == pytest.approx(expected[4] * factor)


@pytest.mark.parametrize(
    ("model", "context", "calculation_date", "quoted_leg"),
    [
        (
            "black76",
            {
                **black_context(),
                "asset": "Brent",
            },
            AS_OF,
            black_leg(1, volatility=0.42),
        ),
        (
            "asian76",
            {
                **black_context(),
                "averaging_start_date": (AS_OF + timedelta(days=30)).isoformat(),
            },
            AS_OF,
            black_leg(1, volatility=0.42),
        ),
        (
            "black76",
            {
                "asset": "JKM",
                "forward": 16.37,
                "rate": 0.0,
                "expiration_date": "2026-12-02",
                "contract_expiration_date": "2027-03-15",
            },
            date(2026, 8, 21),
            black_leg(1, strike=17.0, volatility=64.85),
        ),
        (
            "black76",
            {
                "asset": "TTF",
                "delivery_shape": "SUM",
                "delivery_year": 2027,
                "forward": 42.9,
            },
            date(2026, 8, 21),
            black_leg(1, call_put="P", strike=40.0, volatility=0.4956),
        ),
    ],
)
def test_premium_quote_recovers_contract_and_pricing_volatility(
    model,
    context,
    calculation_date,
    quoted_leg,
):
    baseline = calculate_structure(
        model,
        context,
        sizing(),
        [quoted_leg],
        as_of=calculation_date,
    )
    premium = baseline["legs"][0]["unit"]["value"]
    premium_leg = {
        key: value for key, value in quoted_leg.items() if key != "volatility"
    }
    premium_leg.update({"quote_basis": "PREMIUM", "quote_value": premium})

    solved = calculate_structure(
        model,
        context,
        sizing(),
        [premium_leg],
        as_of=calculation_date,
    )
    baseline_leg = baseline["legs"][0]
    solved_leg = solved["legs"][0]

    assert solved_leg["quote_basis"] == "PREMIUM"
    assert solved_leg["quote_input"] == pytest.approx(premium)
    assert solved_leg["entered_premium"] == pytest.approx(premium)
    assert solved_leg["raw_volatility"] == pytest.approx(
        baseline_leg["raw_volatility"], abs=1e-10
    )
    assert solved_leg["volatility_used"] == pytest.approx(
        baseline_leg["volatility_used"], abs=1e-10
    )
    assert solved_leg["unit"]["value"] == pytest.approx(premium, abs=1e-10)
    assert solved["input"]["legs"][0]["volatility"] == pytest.approx(
        baseline_leg["raw_volatility"], abs=1e-10
    )


def test_mixed_vol_and_premium_legs_share_one_structure_pricing_path():
    premium_source = calculate_structure(
        "black76",
        black_context(),
        sizing(),
        [black_leg(2, side="SELL", strike=105, volatility=0.31)],
        as_of=AS_OF,
    )
    premium = premium_source["legs"][0]["unit"]["value"]
    snapshot = calculate_structure(
        "black76",
        black_context(),
        sizing(),
        [
            black_leg(1, strike=95, volatility=0.18),
            {
                **black_leg(2, side="SELL", strike=105),
                "quote_basis": "PREMIUM",
                "quote_value": premium,
            },
        ],
        as_of=AS_OF,
    )

    assert [leg["quote_basis"] for leg in snapshot["legs"]] == ["VOL", "PREMIUM"]
    assert snapshot["legs"][1]["raw_volatility"] == pytest.approx(0.31)
    assert_totals_reconcile(snapshot)


def test_premium_quote_validation_is_actionable_and_kirk_remains_vol_only():
    premium_leg = {
        **black_leg(1),
        "quote_basis": "PREMIUM",
        "quote_value": -1,
    }
    with pytest.raises(StructureValidationError, match="premium must be greater"):
        calculate_structure(
            "black76", black_context(), sizing(), [premium_leg], as_of=AS_OF
        )

    premium_leg["quote_value"] = 1_000
    with pytest.raises(StructureValidationError, match="outside the attainable"):
        calculate_structure(
            "black76", black_context(), sizing(), [premium_leg], as_of=AS_OF
        )

    with pytest.raises(StructureValidationError, match="volatility inputs only"):
        calculate_structure(
            "kirk",
            {
                "asset_1": 100,
                "asset_2": 90,
                "correlation": 0.5,
                "expiration_date": EXPIRY.isoformat(),
                "contract_expiration_date": EXPIRY.isoformat(),
            },
            sizing(),
            [{**default_leg("kirk", 1), "quote_basis": "PREMIUM"}],
            as_of=AS_OF,
        )


def test_premium_based_snapshot_sensitivity_shifts_the_solved_volatility():
    baseline = calculate_structure(
        "black76",
        black_context(),
        sizing(),
        [black_leg(1, volatility=0.42)],
        as_of=AS_OF,
    )
    premium = baseline["legs"][0]["unit"]["value"]
    snapshot = calculate_structure(
        "black76",
        black_context(),
        sizing(),
        [
            {
                **black_leg(1),
                "quote_basis": "PREMIUM",
                "quote_value": premium,
            }
        ],
        as_of=AS_OF,
    )

    sensitivity = parallel_volatility_series(snapshot)
    assert len({round(value, 10) for value in sensitivity["values"]}) > 1
    zero_index = sensitivity["shifts_percentage_points"].index(0.0)
    assert sensitivity["values"][zero_index] == pytest.approx(
        snapshot["totals"]["trade_value"]
    )


@pytest.mark.parametrize(
    (
        "forward",
        "strike",
        "expiration",
        "listed_expiration",
        "input_vol_percent",
        "adjusted_vol",
        "premium",
        "delta",
        "vega",
    ),
    [
        (
            16.37,
            17.00,
            "2026-12-02",
            "2027-03-15",
            64.85,
            0.4617322024,
            1.3322033642,
            0.4874700426,
            0.0346631453,
        ),
        (
            14.56,
            16.00,
            "2027-02-01",
            "2027-05-14",
            46.40,
            0.3622843744,
            0.8655763666,
            0.3946905682,
            0.0375582011,
        ),
        (
            13.375,
            14.00,
            "2027-07-30",
            "2028-02-15",
            43.20,
            0.3438426524,
            1.5154170611,
            0.5117829418,
            0.0516852144,
        ),
    ],
)
def test_jkm_governed_calendar_percent_inputs_match_verified_calls(
    forward,
    strike,
    expiration,
    listed_expiration,
    input_vol_percent,
    adjusted_vol,
    premium,
    delta,
    vega,
):
    snapshot = calculate_structure(
        "black76",
        {
            "asset": "JKM",
            "forward": forward,
            "rate": 0.0,
            "expiration_date": expiration,
            "contract_expiration_date": listed_expiration,
        },
        sizing(),
        [black_leg(1, strike=strike, volatility=input_vol_percent)],
        as_of=date(2026, 8, 21),
    )

    leg = snapshot["legs"][0]
    assert snapshot["context"]["day_count_basis"] == "ACT/365.25"
    assert snapshot["context"]["variance_calendar_code"] == (
        "ICE_JKM_71090519_TRADING"
    )
    assert snapshot["context"]["vega_basis"] == "adjusted_pricing_vol"
    assert leg["raw_volatility"] == pytest.approx(input_vol_percent / 100.0)
    assert leg["volatility_used"] == pytest.approx(adjusted_vol)
    assert leg["unit"]["value"] == pytest.approx(premium)
    assert leg["unit"]["greeks"]["delta"] == pytest.approx(delta)
    assert leg["unit"]["greeks"]["vega"] == pytest.approx(vega)
    assert snapshot["greek_labels"]["vega"] == (
        "Vega (adjusted pricing vol, 1 point)"
    )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda legs: [], "At least one"),
        (lambda legs: legs + [dict(legs[0])], "unique"),
        (
            lambda legs: [{**legs[0], "ratio": 0}],
            "ratio must be greater",
        ),
        (
            lambda legs: [{**legs[0], "volatility": float("nan")}],
            "must be finite",
        ),
    ],
)
def test_invalid_leg_blocks_the_entire_structure(mutator, message):
    legs = mutator([black_leg(1)])
    with pytest.raises(StructureValidationError, match=message):
        calculate_structure(
            "black76",
            black_context(),
            sizing(),
            legs,
            as_of=AS_OF,
        )


def test_leg_count_is_bounded():
    legs = [black_leg(index) for index in range(1, MAX_LEGS + 2)]
    with pytest.raises(StructureValidationError, match=str(MAX_LEGS)):
        calculate_structure(
            "black76",
            black_context(),
            sizing(),
            legs,
            as_of=AS_OF,
        )


def test_asian_structure_preserves_scaling_and_propagates_unavailable_theta():
    context = {
        "forward": 100,
        "rate": 0.03,
        "averaging_start_date": AS_OF.isoformat(),
        "expiration_date": EXPIRY.isoformat(),
        "contract_expiration_date": (EXPIRY + timedelta(days=30)).isoformat(),
    }
    legs = [
        {
            **black_leg(1, strike=95, volatility=0.28),
            "call_put": "C",
        },
        {
            **black_leg(2, side="SELL", strike=105, volatility=0.31),
            "call_put": "C",
        },
    ]
    snapshot = calculate_structure(
        "asian76",
        context,
        sizing(5, 100),
        legs,
        as_of=AS_OF,
    )
    assert snapshot["totals"]["trade_greeks"]["theta"] is None
    assert "theta" in snapshot["unavailable_metrics"]
    assert any("Theta is unavailable" in warning for warning in snapshot["warnings"])
    assert_totals_reconcile(snapshot)


def test_kirk_structure_keeps_risk_dimensions_and_explicitly_has_no_rho():
    context = {
        "asset_1": 100,
        "asset_2": 90,
        "correlation": 0.45,
        "expiration_date": EXPIRY.isoformat(),
        "contract_expiration_date": EXPIRY.isoformat(),
    }
    legs = [
        {
            **default_leg("kirk", 1),
            "strike": 3,
            "volatility_asset_1": 0.22,
            "volatility_asset_2": 0.18,
        },
        {
            **default_leg("kirk", 2),
            "side": "SELL",
            "strike": 8,
            "volatility_asset_1": 0.24,
            "volatility_asset_2": 0.20,
        },
    ]
    snapshot = calculate_structure(
        "kirk",
        context,
        sizing(4, 100),
        legs,
        as_of=AS_OF,
    )
    assert tuple(snapshot["greek_fields"]) == GREEK_FIELDS["kirk"]
    assert "rho" not in snapshot["totals"]["trade_greeks"]
    assert snapshot["unsupported_metrics"] == ["rho"]
    assert any("undiscounted" in warning for warning in snapshot["warnings"])
    assert_totals_reconcile(snapshot)


def test_single_kirk_leg_matches_existing_library_value_and_greeks():
    context = {
        "asset_1": 100,
        "asset_2": 90,
        "correlation": 0.45,
        "expiration_date": EXPIRY.isoformat(),
        "contract_expiration_date": EXPIRY.isoformat(),
    }
    leg = {
        **default_leg("kirk", 1),
        "strike": 3,
        "volatility_asset_1": 0.22,
        "volatility_asset_2": 0.18,
    }
    snapshot = calculate_structure(
        "kirk",
        context,
        sizing(),
        [leg],
        as_of=AS_OF,
    )
    time_to_expiry = (EXPIRY - AS_OF).days / 365.25
    expected_value = kirk_model_with_substitution(
        100,
        90,
        3,
        0.22,
        0.18,
        0.45,
        time_to_expiry,
        "call",
    )
    expected_greeks = kirk_spread_greeks(
        100,
        90,
        3,
        0.22,
        0.18,
        0.45,
        time_to_expiry,
        "call",
    )
    actual = snapshot["legs"][0]["unit"]
    assert actual["value"] == pytest.approx(expected_value)
    mapping = {
        "delta_s1": "delta_S1",
        "delta_s2": "delta_S2",
        "gamma_s1": "gamma_S1",
        "gamma_s2": "gamma_S2",
        "gamma_s1s2": "gamma_S1S2",
        "vega_sigma1": "vega_sigma1",
        "vega_sigma2": "vega_sigma2",
        "theta": "theta",
        "corr_sensitivity": "corr_sensitivity",
        "vega_equiv": "vega_equiv",
    }
    for metric, library_key in mapping.items():
        assert actual["greeks"][metric] == pytest.approx(
            expected_greeks[library_key]
        )


def test_structure_scenarios_are_total_only_and_zero_shift_matches_base():
    snapshot = calculate_structure(
        "black76",
        {**black_context(), "asset": "HH"},
        sizing(2, 100),
        [
            black_leg(1, strike=95, volatility=0.18),
            black_leg(2, side="SELL", strike=105, volatility=0.26),
        ],
        as_of=AS_OF,
    )
    vol = parallel_volatility_series(snapshot)
    zero_index = vol["shifts_percentage_points"].index(0.0)
    assert vol["values"][zero_index] == pytest.approx(
        snapshot["totals"]["trade_value"]
    )
    rate = rate_sensitivity_series(snapshot)
    base_index = rate["rates"].index(0.03)
    assert rate["values"][base_index] == pytest.approx(
        snapshot["totals"]["trade_value"]
    )
    payoff = payoff_series(snapshot, price_range=40)
    assert len(payoff["x"]) == len(payoff["theoretical"]) == len(payoff["payoff"])
    decay = time_decay_series(snapshot)
    assert decay["dates"][0] == AS_OF.isoformat()
    extension = expiration_extension_series(snapshot)
    assert EXPIRY.isoformat() in extension["dates"]


def test_correlation_scenario_rejects_non_kirk_model():
    snapshot = calculate_structure(
        "black76",
        black_context(),
        sizing(),
        [black_leg(1)],
        as_of=AS_OF,
    )
    with pytest.raises(StructureValidationError, match="only available"):
        correlation_sensitivity_series(snapshot)


def test_ttf_delivery_shapes_use_dst_aware_hours_and_winter_crosses_year():
    summer, summer_hours = build_ttf_strip_components(
        "SUM", 2027, date(2026, 8, 21), 42.9
    )
    winter, winter_hours = build_ttf_strip_components(
        "WIN", 2027, date(2026, 8, 21), 42.9
    )
    q1, q1_hours = build_ttf_strip_components(
        "Q1", 2027, date(2026, 8, 21), 42.9
    )

    assert [row["contract_month"][:7] for row in summer] == [
        "2027-04",
        "2027-05",
        "2027-06",
        "2027-07",
        "2027-08",
        "2027-09",
    ]
    assert [row["contract_month"][:7] for row in winter] == [
        "2027-10",
        "2027-11",
        "2027-12",
        "2028-01",
        "2028-02",
        "2028-03",
    ]
    assert summer_hours == 4392
    assert winter_hours == 4392
    assert q1_hours == 2159
    assert [row["delivery_hours"] for row in q1] == [744, 672, 743]
    assert sum(row["weight"] for row in summer) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("call_put", "strike", "expected_value"),
    [("P", 40.0, 5.894909897031116), ("C", 45.0, 6.68710905662961)],
)
def test_ttf_sum27_exact_monthly_pricing_matches_reference(
    call_put, strike, expected_value
):
    snapshot = calculate_structure(
        "black76",
        {
            "asset": "TTF",
            "delivery_shape": "SUM",
            "delivery_year": 2027,
            "forward": 42.9,
        },
        sizing(),
        [black_leg(1, call_put=call_put, strike=strike, volatility=49.56)],
        as_of=date(2026, 8, 21),
    )

    context = snapshot["context"]
    leg = snapshot["legs"][0]
    assert snapshot["totals"]["unit_structure_value"] == pytest.approx(
        expected_value
    )
    assert context["delivery_period_label"] == "SUM27"
    assert context["delivery_component_count"] == 6
    assert context["delivery_total_hours"] == 4392
    assert context["day_count_basis"] == "ACT/365.25"
    assert context["margin_style"] == "futures_style"
    assert context["rate"] == 0.0
    assert snapshot["unsupported_metrics"] == ["rho"]
    assert all(
        component["expiry_status"] == "official"
        for component in context["delivery_components"]
    )
    assert leg["unit"]["value"] == pytest.approx(
        sum(item["weighted_unit_value"] for item in leg["components"])
    )
    for metric in ("delta", "gamma", "theta", "vega", "rho"):
        assert leg["unit"]["greeks"][metric] == pytest.approx(
            sum(item["weighted_greeks"][metric] for item in leg["components"])
        )
    first = leg["components"][0]
    expected_first = black_76_futures_style(
        call_put,
        42.9,
        strike,
        first["time_to_expiry"],
        0.4956,
    )
    assert first["unit_value"] == pytest.approx(expected_first[0])
    json.dumps(snapshot)


def test_jkm_models_select_distinct_exchange_products_and_monthly_expiries():
    apo = calculate_structure(
        "asian76",
        {
            "asset": "JKM",
            "delivery_shape": "Q1",
            "delivery_year": 2027,
            "forward": 16.0,
        },
        sizing(),
        [black_leg(1, strike=16.0, volatility=0.60)],
        as_of=date(2026, 8, 21),
    )
    vanilla = calculate_structure(
        "black76",
        {
            "asset": "JKM",
            "delivery_shape": "Q1",
            "delivery_year": 2027,
            "forward": 16.0,
        },
        sizing(),
        [black_leg(1, strike=16.0, volatility=0.60)],
        as_of=date(2026, 8, 21),
    )

    assert default_model_for_asset("JKM") == "asian76"
    assert apo["context"]["exchange_product_code"] == "JKM"
    assert vanilla["context"]["exchange_product_code"] == "JKZ"
    assert apo["context"]["delivery_total_quantity"] == 30_000
    assert vanilla["context"]["delivery_total_quantity"] == 30_000
    assert [
        row["option_expiration_date"]
        for row in apo["context"]["delivery_components"]
    ] == ["2026-12-15", "2027-01-15", "2027-02-15"]
    assert [
        row["averaging_start_date"]
        for row in apo["context"]["delivery_components"]
    ] == ["2026-11-16", "2026-12-16", "2027-01-18"]
    assert [
        row["option_expiration_date"]
        for row in vanilla["context"]["delivery_components"]
    ] == ["2026-10-27", "2026-11-26", "2026-12-24"]
    assert all(
        row["expiry_convention_code"] == "ICE_JKM_JKZ_VIA_TFO"
        for row in vanilla["context"]["delivery_components"]
    )
    assert sum(
        row["weighted_unit_value"] for row in apo["legs"][0]["components"]
    ) == pytest.approx(apo["legs"][0]["unit"]["value"])
    for snapshot in (apo, vanilla):
        payload = snapshot["input"]
        assert _calculate_trade_value_only(
            snapshot["model"],
            payload["context"],
            payload["sizing"],
            payload["legs"],
            date.fromisoformat(snapshot["calculation_date"]),
        ) == pytest.approx(snapshot["totals"]["trade_value"])
    assert time_decay_series(apo)["dates"][-1] == "2026-11-16"


def test_jkm_apo_month_delivery_resolves_the_same_governed_dates_as_a_strip():
    as_of = date(2026, 8, 21)

    assert available_jkm_apo_delivery_months(as_of)[0] == date(2026, 11, 1)
    component = build_jkm_month_component(
        "2027-01-24",
        as_of,
        16.0,
        "asian76",
    )

    assert component["contract_month"] == "2027-01-01"
    assert component["contract_month_label"] == "Jan-27"
    assert component["averaging_start_date"] == "2026-11-16"
    assert component["averaging_end_date"] == "2026-12-15"
    assert component["option_expiration_date"] == "2026-12-15"
    assert component["contract_expiration_date"] == "2026-12-15"
    assert component["exchange_product_code"] == "JKM"


@pytest.mark.parametrize(
    ("asset", "model", "expected_month", "expected_expiration"),
    [
        ("TTF", "black76", date(2026, 9, 1), "2026-08-26"),
        ("JKM", "asian76", date(2026, 11, 1), "2026-10-15"),
        ("HH", "black76", date(2026, 9, 1), "2026-08-26"),
        ("Brent", "black76", date(2026, 10, 1), "2026-08-25"),
        ("NBP", "black76", date(2026, 9, 1), "2026-08-26"),
    ],
)
def test_all_assets_expose_governed_monthly_deliveries(
    asset,
    model,
    expected_month,
    expected_expiration,
):
    as_of = date(2026, 8, 21)

    months = available_delivery_months(asset, model, as_of)
    component = build_delivery_month_component(
        asset,
        model,
        months[0],
        as_of,
    )

    assert months[0] == expected_month
    assert component["contract_month"] == expected_month.isoformat()
    assert component["option_expiration_date"] == expected_expiration
    assert component["contract_expiration_date"] == expected_expiration
    if asset == "HH":
        assert component["expiry_convention_code"] == "ICE_HH_PHE_6590274_EXPIRY"
        assert component["expiry_status"] == "official"


def test_hh_january_2028_uses_the_published_phe_expiry():
    component = build_delivery_month_component(
        "HH",
        "black76",
        "2028-01-01",
        date(2026, 8, 21),
    )

    assert component["option_expiration_date"] == "2027-12-28"
    assert component["expiry_status"] == "official"


def test_ttf_month_delivery_governs_contract_anchor_but_keeps_custom_option_expiry():
    snapshot = calculate_structure(
        "black76",
        {
            "asset": "TTF",
            "premium_convention": "futures_style",
            "delivery_shape": "MONTH",
            "delivery_month": "2026-09-01",
            "forward": 42.9,
            "rate": 0.0,
            "expiration_date": "2026-08-25",
            "contract_expiration_date": "2029-01-01",
        },
        sizing(1, 720),
        [black_leg(1, strike=40.0, volatility=0.50)],
        as_of=date(2026, 8, 21),
    )

    context = snapshot["context"]
    assert context["delivery_month"] == "2026-09-01"
    assert context["delivery_period_label"] == "Sep-26"
    assert context["expiration_date"] == "2026-08-25"
    assert context["contract_expiration_date"] == "2026-08-26"
    assert context["expiry_convention_code"] == "ICE_TTF_TFO_71085679_EXPIRY"
    assert snapshot["input"]["context"]["delivery_month"] == "2026-09-01"


def test_jkm_apo_month_delivery_is_authoritative_over_manual_dates():
    snapshot = calculate_structure(
        "asian76",
        {
            "asset": "JKM",
            "delivery_shape": "MONTH",
            "delivery_month": "2027-01-01",
            "forward": 16.0,
            "averaging_start_date": "2026-09-01",
            "expiration_date": "2026-10-01",
            "contract_expiration_date": "2028-01-01",
        },
        sizing(),
        [black_leg(1, strike=16.0, volatility=0.60)],
        as_of=date(2026, 8, 21),
    )

    context = snapshot["context"]
    assert context["delivery_month"] == "2027-01-01"
    assert context["delivery_period_label"] == "Jan-27"
    assert context["averaging_start_date"] == "2026-11-16"
    assert context["averaging_end_date"] == "2026-12-15"
    assert context["expiration_date"] == "2026-12-15"
    assert context["contract_expiration_date"] == "2026-12-15"
    assert context["vol_adjustment_factor"] == pytest.approx(1.0)


def test_jkm_apo_month_delivery_rejects_a_started_averaging_period():
    with pytest.raises(StructureValidationError, match="averaging period has started"):
        build_jkm_month_component(
            "2026-10-01",
            date(2026, 8, 21),
            16.0,
            "asian76",
        )


def test_delivery_strip_rejects_unsupported_model_and_partly_expired_period():
    with pytest.raises(StructureValidationError, match="require TTF Black-76"):
        calculate_structure(
            "asian76",
            {
                "asset": "TTF",
                "delivery_shape": "SUM",
                "delivery_year": 2027,
                "forward": 42.9,
            },
            sizing(),
            [black_leg(1)],
            as_of=date(2026, 8, 21),
        )
    with pytest.raises(StructureValidationError, match="complete strip"):
        calculate_structure(
            "black76",
            {
                "asset": "TTF",
                "delivery_shape": "Q1",
                "delivery_year": 2027,
                "forward": 42.9,
            },
            sizing(),
            [black_leg(1)],
            as_of=date(2027, 3, 1),
        )


def test_ttf_strip_scenarios_respect_staggered_expiries_and_rate_contract():
    snapshot = calculate_structure(
        "black76",
        {
            "asset": "TTF",
            "delivery_shape": "Q2",
            "delivery_year": 2027,
            "forward": 42.9,
        },
        sizing(),
        [black_leg(1, strike=40, volatility=0.4956)],
        as_of=date(2026, 8, 21),
    )
    payoff = payoff_series(snapshot)
    decay = time_decay_series(snapshot)

    assert payoff["valuation_date"] == snapshot["context"]["first_expiration_date"]
    assert payoff["at_expiration"] is False
    assert payoff["xaxis_title"] == "Parallel monthly forward price"
    assert decay["dates"][-1] == snapshot["context"]["first_expiration_date"]
    with pytest.raises(StructureValidationError, match="futures-style premium"):
        rate_sensitivity_series(snapshot)
    with pytest.raises(StructureValidationError, match="governed monthly expiries"):
        expiration_extension_series(snapshot)


@pytest.mark.parametrize(
    ("asset", "calendar_code", "option_days", "contract_days"),
    [
        ("TTF", "ICE_TTF_TFO_TRADING", 2, 6),
        ("JKM", "ICE_JKM_71090519_TRADING", 3, 7),
        ("HH", "ICE_HH_PHE_TRADING", 3, 7),
        ("Brent", "ICE_BRENT_218_TRADING", 3, 7),
        ("NBP", "ICE_NBP_UKF_TRADING", 2, 6),
    ],
)
def test_supported_assets_use_their_governed_variance_calendars(
    asset,
    calendar_code,
    option_days,
    contract_days,
):
    calculation_date = date(2026, 12, 23)
    option_expiration = date(2026, 12, 29)
    contract_expiration = date(2027, 1, 5)
    snapshot = calculate_structure(
        "black76",
        {
            "asset": asset,
            "forward": 100.0,
            "rate": 0.03,
            "expiration_date": option_expiration.isoformat(),
            "contract_expiration_date": contract_expiration.isoformat(),
        },
        sizing(),
        [black_leg(1)],
        as_of=calculation_date,
    )

    context = snapshot["context"]
    assert context["variance_calendar_code"] == calendar_code
    assert context["option_business_days"] == option_days
    assert context["contract_business_days"] == contract_days
    assert context["vol_adjustment_factor"] == pytest.approx(
        (option_days / contract_days) ** 0.5
    )


@pytest.mark.parametrize("asset", ["TTF", "JKM", "HH", "Brent", "NBP"])
def test_zero_governed_trading_day_expiry_fails_closed(asset):
    calculation_date = date(2026, 8, 21)  # Friday
    weekend_expiration = date(2026, 8, 22)  # Saturday

    with pytest.raises(
        StructureValidationError,
        match="at least one governed exchange trading day",
    ):
        calculate_structure(
            "black76",
            {
                "asset": asset,
                "forward": 100.0,
                "rate": 0.03,
                "expiration_date": weekend_expiration.isoformat(),
                "contract_expiration_date": weekend_expiration.isoformat(),
            },
            sizing(),
            [black_leg(1)],
            as_of=calculation_date,
        )


def test_ttf_month_is_futures_style_act36525_and_marks_rho_unsupported():
    calculation_date = date(2026, 8, 21)
    expiration = date(2026, 12, 2)
    snapshot = calculate_structure(
        "black76",
        {
            "asset": "TTF",
            "forward": 42.9,
            # A non-zero input proves it is governed out of the futures-style path.
            "rate": 0.75,
            "expiration_date": expiration.isoformat(),
            "contract_expiration_date": expiration.isoformat(),
        },
        sizing(),
        [black_leg(1, call_put="P", strike=40.0, volatility=0.4956)],
        as_of=calculation_date,
    )
    time_to_expiry = (expiration - calculation_date).days / 365.25
    expected = black_76_futures_style("P", 42.9, 40.0, time_to_expiry, 0.4956)
    discounted = black_76("P", 42.9, 40.0, time_to_expiry, 0.75, 0.4956)

    context = snapshot["context"]
    leg = snapshot["legs"][0]
    assert context["day_count_basis"] == "ACT/365.25"
    assert context["time_to_expiry"] == pytest.approx(time_to_expiry)
    assert context["margin_style"] == "futures_style"
    assert context["rate"] == 0.0
    assert leg["unit"]["value"] == pytest.approx(expected[0])
    assert leg["unit"]["value"] != pytest.approx(discounted[0])
    assert leg["unit"]["greeks"]["rho"] == 0.0
    assert snapshot["unsupported_metrics"] == ["rho"]
    vol_series = parallel_volatility_series(snapshot)
    zero_index = vol_series["shifts_percentage_points"].index(0.0)
    assert vol_series["values"][zero_index] == pytest.approx(
        snapshot["totals"]["trade_value"]
    )
    with pytest.raises(StructureValidationError, match="futures-style premium"):
        rate_sensitivity_series(snapshot)


def test_explicit_premium_convention_overrides_asset_defaults():
    calculation_date = date(2026, 8, 21)
    expiration = date(2026, 12, 2)
    time_to_expiry = (expiration - calculation_date).days / 365.25
    common = {
        "forward": 42.9,
        "rate": 0.07,
        "expiration_date": expiration.isoformat(),
        "contract_expiration_date": expiration.isoformat(),
    }
    leg = black_leg(1, call_put="P", strike=40.0, volatility=0.4956)

    ttf_upfront = calculate_structure(
        "black76",
        {
            **common,
            "asset": "TTF",
            "premium_convention": "upfront",
        },
        sizing(),
        [leg],
        as_of=calculation_date,
    )
    brent_futures = calculate_structure(
        "black76",
        {
            **common,
            "asset": "Brent",
            "premium_convention": "futures_style",
        },
        sizing(),
        [leg],
        as_of=calculation_date,
    )

    assert ttf_upfront["context"]["resolved_premium_convention"] == "upfront"
    assert ttf_upfront["context"]["rate"] == pytest.approx(0.07)
    assert ttf_upfront["legs"][0]["unit"]["value"] == pytest.approx(
        black_76("P", 42.9, 40.0, time_to_expiry, 0.07, 0.4956)[0]
    )
    assert ttf_upfront["unsupported_metrics"] == []
    assert rate_sensitivity_series(ttf_upfront)["rates"]

    assert brent_futures["context"]["resolved_premium_convention"] == (
        "futures_style"
    )
    assert brent_futures["context"]["rate"] == 0.0
    assert brent_futures["legs"][0]["unit"]["value"] == pytest.approx(
        black_76_futures_style("P", 42.9, 40.0, time_to_expiry, 0.4956)[0]
    )
    assert brent_futures["unsupported_metrics"] == ["rho"]


def test_asian_futures_style_is_undiscounted_and_has_no_rate_sensitivity():
    calculation_date = date(2026, 8, 21)
    averaging_start = date(2026, 10, 1)
    expiration = date(2026, 12, 2)
    time_to_expiry = (expiration - calculation_date).days / 365.25
    time_to_averaging = (averaging_start - calculation_date).days / 365.25
    snapshot = calculate_structure(
        "asian76",
        {
            "asset": "JKM",
            "premium_convention": "futures_style",
            "forward": 100.0,
            "rate": 0.11,
            "averaging_start_date": averaging_start.isoformat(),
            "expiration_date": expiration.isoformat(),
            "contract_expiration_date": expiration.isoformat(),
        },
        sizing(),
        [black_leg(1, strike=105.0, volatility=0.32)],
        as_of=calculation_date,
    )

    assert snapshot["context"]["rate"] == 0.0
    assert snapshot["legs"][0]["unit"]["value"] == pytest.approx(
        asian_76(
            "C",
            100.0,
            105.0,
            time_to_expiry,
            time_to_averaging,
            0.0,
            snapshot["legs"][0]["volatility_used"],
        )[0]
    )
    assert snapshot["unsupported_metrics"] == ["rho"]
    with pytest.raises(StructureValidationError, match="futures-style premium"):
        rate_sensitivity_series(snapshot)


def test_ttf_strip_upfront_premium_discounts_each_monthly_component():
    snapshot = calculate_structure(
        "black76",
        {
            "asset": "TTF",
            "premium_convention": "upfront",
            "delivery_shape": "Q2",
            "delivery_year": 2027,
            "forward": 42.9,
            "rate": 0.08,
        },
        sizing(),
        [black_leg(1, call_put="P", strike=40.0, volatility=0.4956)],
        as_of=date(2026, 8, 21),
    )
    expected = sum(
        component["weight"]
        * black_76(
            "P",
            42.9,
            40.0,
            component["time_to_expiry"],
            0.08,
            0.4956,
        )[0]
        for component in snapshot["context"]["delivery_components"]
    )

    assert snapshot["context"]["margin_style"] == "upfront"
    assert snapshot["totals"]["unit_structure_value"] == pytest.approx(expected)
    assert snapshot["unsupported_metrics"] == []
    rate_series = rate_sensitivity_series(snapshot)
    base_index = rate_series["rates"].index(0.08)
    assert rate_series["values"][base_index] == pytest.approx(
        snapshot["totals"]["trade_value"]
    )
    assert time_decay_series(snapshot)["values"][0] == pytest.approx(
        snapshot["totals"]["trade_value"]
    )


def test_kirk_rejects_unsupported_upfront_premium_convention():
    context = {
        "asset": "TTF",
        "premium_convention": "upfront",
        "asset_1": 100.0,
        "asset_2": 90.0,
        "correlation": 0.5,
        "expiration_date": EXPIRY.isoformat(),
        "contract_expiration_date": EXPIRY.isoformat(),
    }
    with pytest.raises(StructureValidationError, match="not supported for Kirk"):
        calculate_structure(
            "kirk",
            context,
            sizing(),
            [default_leg("kirk", 1)],
            as_of=AS_OF,
        )


def test_option_horizon_accepts_exact_boundary_and_rejects_one_day_more():
    boundary = AS_OF + timedelta(days=MAX_OPTION_HORIZON_DAYS)
    accepted = calculate_structure(
        "black76",
        {
            "asset": "HH",
            "forward": 100.0,
            "rate": 0.03,
            "expiration_date": boundary.isoformat(),
            "contract_expiration_date": boundary.isoformat(),
        },
        sizing(),
        [black_leg(1)],
        as_of=AS_OF,
    )
    assert accepted["context"]["time_to_expiry"] == pytest.approx(100.0)

    beyond = boundary + timedelta(days=1)
    with pytest.raises(StructureValidationError, match="Expiration date.*100-year"):
        calculate_structure(
            "black76",
            {
                "asset": "HH",
                "forward": 100.0,
                "rate": 0.03,
                "expiration_date": beyond.isoformat(),
                "contract_expiration_date": beyond.isoformat(),
            },
            sizing(),
            [black_leg(1)],
            as_of=AS_OF,
        )

    with pytest.raises(
        StructureValidationError,
        match="Contract expiration date.*100-year",
    ):
        calculate_structure(
            "black76",
            {
                "asset": "HH",
                "forward": 100.0,
                "rate": 0.03,
                "expiration_date": EXPIRY.isoformat(),
                "contract_expiration_date": beyond.isoformat(),
            },
            sizing(),
            [black_leg(1)],
            as_of=AS_OF,
        )


@pytest.mark.parametrize("model", ["black76", "asian76"])
def test_micro_premium_inversion_recovers_black_and_asian_volatility(model):
    context = {
        "asset": "HH",
        "forward": 100.0,
        "rate": 0.03,
        "expiration_date": EXPIRY.isoformat(),
        "contract_expiration_date": EXPIRY.isoformat(),
    }
    if model == "asian76":
        context["averaging_start_date"] = (AS_OF + timedelta(days=30)).isoformat()

    baseline = calculate_structure(
        model,
        context,
        sizing(),
        [black_leg(1, strike=250.0, volatility=0.20)],
        as_of=AS_OF,
    )
    premium = baseline["legs"][0]["unit"]["value"]
    premium_leg = {
        key: value
        for key, value in black_leg(1, strike=250.0).items()
        if key != "volatility"
    }
    premium_leg.update({"quote_basis": "PREMIUM", "quote_value": premium})
    solved = calculate_structure(
        model,
        context,
        sizing(),
        [premium_leg],
        as_of=AS_OF,
    )
    solved_leg = solved["legs"][0]

    assert 0.0 < premium < 1e-12
    assert solved_leg["raw_volatility"] == pytest.approx(0.20, abs=1e-10)
    assert solved_leg["unit"]["value"] == pytest.approx(
        premium,
        rel=1e-8,
        abs=0.0,
    )


def test_finite_overflow_inputs_are_rejected_in_full_and_price_only_paths():
    context = {**black_context(), "asset": "HH"}

    with pytest.raises(StructureValidationError, match="non-finite position scale"):
        calculate_structure(
            "black76",
            context,
            sizing(2, 1e308),
            [black_leg(1)],
            as_of=AS_OF,
        )

    risk_overflow_context = {
        **context,
        "forward": 0.01,
    }
    with pytest.raises(StructureValidationError, match="non-finite risk contribution"):
        calculate_structure(
            "black76",
            risk_overflow_context,
            sizing(),
            [black_leg(1, ratio=1e308, strike=0.01)],
            as_of=AS_OF,
        )

    with pytest.raises(StructureValidationError, match="scenario value became non-finite"):
        _calculate_trade_value_only(
            "black76",
            context,
            sizing(),
            [black_leg(1, ratio=1e308)],
            AS_OF,
        )


def test_price_only_scenario_path_matches_full_pricing_for_every_pricing_branch():
    ttf_as_of = date(2026, 8, 21)
    cases = [
        (
            "premium-style Black-76",
            "black76",
            {**black_context(), "asset": "HH"},
            [black_leg(1, strike=105.0, volatility=0.31)],
            AS_OF,
        ),
        (
            "Asian-76",
            "asian76",
            {
                **black_context(),
                "asset": "HH",
                "averaging_start_date": (AS_OF + timedelta(days=30)).isoformat(),
            },
            [black_leg(1, strike=105.0, volatility=0.31)],
            AS_OF,
        ),
        (
            "Kirk",
            "kirk",
            {
                "asset": "Brent",
                "asset_1": 100.0,
                "asset_2": 90.0,
                "correlation": 0.45,
                "expiration_date": EXPIRY.isoformat(),
                "contract_expiration_date": EXPIRY.isoformat(),
            },
            [
                {
                    **default_leg("kirk", 1),
                    "strike": 3.0,
                    "volatility_asset_1": 0.22,
                    "volatility_asset_2": 0.18,
                }
            ],
            AS_OF,
        ),
        (
            "monthly futures-style TTF Black-76",
            "black76",
            {
                "asset": "TTF",
                "forward": 42.9,
                "rate": 0.50,
                "expiration_date": "2026-12-02",
                "contract_expiration_date": "2026-12-02",
            },
            [black_leg(1, call_put="P", strike=40.0, volatility=0.4956)],
            ttf_as_of,
        ),
        (
            "TTF delivery strip",
            "black76",
            {
                "asset": "TTF",
                "delivery_shape": "Q2",
                "delivery_year": 2027,
                "forward": 42.9,
            },
            [black_leg(1, strike=40.0, volatility=0.4956)],
            ttf_as_of,
        ),
    ]

    for label, model, context, legs, calculation_date in cases:
        snapshot = calculate_structure(
            model,
            context,
            sizing(2, 100),
            legs,
            as_of=calculation_date,
        )
        payload = snapshot["input"]
        price_only_value = _calculate_trade_value_only(
            model,
            payload["context"],
            payload["sizing"],
            payload["legs"],
            calculation_date,
        )

        assert price_only_value == pytest.approx(
            snapshot["totals"]["trade_value"],
            rel=1e-12,
            abs=1e-12,
        ), label
