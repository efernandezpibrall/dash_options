import json
from datetime import date, timedelta

import pytest

from options.options_library import (
    black_76,
    kirk_model_with_substitution,
    kirk_spread_greeks,
)
from pricer_structure import (
    GREEK_FIELDS,
    MAX_LEGS,
    SCHEMA_VERSION,
    StructureValidationError,
    calculate_structure,
    correlation_sensitivity_series,
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
            "structure_type": "Physical",
            "asset": "Brent",
        },
        sizing(3, 100),
        [black_leg(1, strike=105, volatility=0.32)],
        as_of=AS_OF,
    )
    expected = black_76("C", 100, 105, 92 / 365, 0.03, 0.32)

    assert snapshot["legs"][0]["unit"]["value"] == pytest.approx(expected[0])
    assert snapshot["legs"][0]["unit"]["greeks"]["delta"] == pytest.approx(expected[1])
    assert snapshot["legs"][0]["unit"]["greeks"]["gamma"] == pytest.approx(expected[2])
    assert snapshot["legs"][0]["unit"]["greeks"]["theta"] == pytest.approx(expected[3])
    assert snapshot["legs"][0]["unit"]["greeks"]["vega"] == pytest.approx(expected[4])
    assert snapshot["legs"][0]["unit"]["greeks"]["rho"] == pytest.approx(expected[5])
    assert snapshot["totals"]["trade_value"] == pytest.approx(expected[0] * 300)
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["context"]["structure_type"] == "Physical"
    assert snapshot["context"]["asset"] == "Brent"
    assert snapshot["input"]["context"]["structure_type"] == "Physical"
    assert snapshot["input"]["context"]["asset"] == "Brent"
    json.dumps(snapshot)


def test_structure_type_is_validated_as_part_of_the_shared_context():
    with pytest.raises(StructureValidationError, match="Type must be one of"):
        calculate_structure(
            "black76",
            {**black_context(), "structure_type": "Unsupported"},
            sizing(),
            [black_leg(1)],
            as_of=AS_OF,
        )


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
        black_context(contract_expiry),
        sizing(),
        [black_leg(1, volatility=0.40)],
        as_of=AS_OF,
    )
    factor = snapshot["context"]["vol_adjustment_factor"]
    leg = snapshot["legs"][0]
    expected = black_76(
        "C",
        100,
        100,
        92 / 365,
        0.03,
        0.40 * factor,
    )
    assert leg["volatility_used"] == pytest.approx(0.40 * factor)
    assert leg["unit"]["greeks"]["vega"] == pytest.approx(expected[4] * factor)


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
    time_to_expiry = (EXPIRY - AS_OF).days / 365
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
        black_context(),
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
