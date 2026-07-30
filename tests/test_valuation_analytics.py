import numpy as np
import pandas as pd

from options.options_library import black_76, kirk_model_with_substitution, kirk_spread_greeks
from valuation_analytics import (
    POSITION_KEY_COLUMNS,
    aggregate_pnl_explain,
    aggregate_scenario,
    calculate_pnl_explain,
    run_portfolio_scenario,
)


def _position(cob='2026-07-08', price_a=12.0, price_b=8.0, vol_a=0.30, vol_b=0.25, rho=0.25, quantity=1000):
    time = 1.0
    strike = 2.0
    price = kirk_model_with_substitution(price_a, price_b, strike, vol_a, vol_b, rho, time, 'call')
    greeks = kirk_spread_greeks(price_a, price_b, strike, vol_a, vol_b, rho, time, 'call')
    row = {
        'cob_date': pd.Timestamp(cob),
        'trade_date': pd.Timestamp('2025-01-01'),
        'entity': 'Desk',
        'book': 'Book',
        'strategy': 'Strategy',
        'substrategy': 'Substrategy',
        'type_trade': 'Physical Option',
        'type_option': 'spread',
        'model': 'kirk',
        'put_call': 'call',
        'buy_sell': 'buy',
        'currency': 'USD',
        'premium': 0.0,
        'expiration_date': pd.Timestamp('2027-07-08'),
        'quantity': quantity,
        'unit_quantity': 'MMBtu',
        'strike': strike,
        'asset_a': 'ICE_JKM',
        'asset_a_multiplier': 1.0,
        'asset_a_premium': 0.0,
        'maturity_date_type_a': 'month',
        'maturity_date_a': pd.Timestamp('2027-08-01'),
        'asset_b': 'ICE_TTF',
        'asset_b_multiplier': 1.0,
        'asset_b_premium': 0.0,
        'maturity_date_type_b': 'month',
        'maturity_date_b': pd.Timestamp('2027-08-01'),
        'adjusted_price_a': price_a,
        'adjusted_price_b': price_b,
        'adjusted_strike': strike,
        'time_to_expiry': time,
        'adjusted_vol_a': vol_a,
        'adjusted_vol_b': vol_b,
        'correlation': rho,
        'price': price,
        'qty_value': quantity * price,
        'qty_pnl': quantity * price,
        **greeks,
    }
    return row


def _black_position(cob, price_a):
    row = _position(cob=cob, price_a=price_a, quantity=74_500)
    time = row['time_to_expiry']
    vol = 0.721
    strike = 60.0
    value, delta, gamma, theta, vega, _rho = black_76(
        'C',
        price_a,
        strike,
        time,
        0.0,
        vol,
    )
    row.update({
        'strategy': 'AD-LNG-Fin-TTF-TEST',
        'substrategy': 'AD-LNG-Fin-TTF-Oct26-CS60x65',
        'type_trade': 'Financial Option',
        'type_option': 'european',
        'model': 'black76',
        'currency': 'EUR',
        'premium': -7.65,
        'strike': strike,
        'asset_a': 'ICE_TTF',
        'maturity_date_a': pd.Timestamp('2026-10-01'),
        'asset_b': None,
        'asset_b_multiplier': None,
        'asset_b_premium': None,
        'maturity_date_type_b': None,
        'maturity_date_b': None,
        'adjusted_price_a': price_a,
        'adjusted_price_b': None,
        'adjusted_strike': strike,
        'adjusted_vol_a': vol,
        'adjusted_vol_b': None,
        'correlation': None,
        'price': value,
        'delta_S1': delta,
        'delta_S2': 0.0,
        'gamma_S1': gamma,
        'gamma_S2': 0.0,
        'gamma_S1S2': 0.0,
        'vega_sigma1': vega,
        'vega_sigma2': 0.0,
        'corr_sensitivity': 0.0,
        'theta': theta,
        'qty_value': row['quantity'] * value,
        'qty_pnl': row['quantity'] * (value - 7.65),
    })
    return row


def test_zero_scenario_reconciles_to_zero():
    results, invalid = run_portfolio_scenario(pd.DataFrame([_position()]))
    assert invalid.empty
    assert len(results) == 1
    assert abs(results.iloc[0]['exact_pnl']) < 1e-8
    assert abs(results.iloc[0]['base_reconciliation']) < 1e-8


def test_full_scenario_matches_direct_kirk_revaluation():
    row = _position()
    results, invalid = run_portfolio_scenario(
        pd.DataFrame([row]),
        price_a_pct=10,
        price_b_pct=-5,
        vol_a_points=3,
        vol_b_points=2,
        correlation_points=10,
        business_days_forward=5,
    )
    expected = row['quantity'] * (
        kirk_model_with_substitution(13.2, 7.6, 2.0, 0.33, 0.27, 0.35, 1 - 5 / 252, 'call')
        - row['price']
    )
    assert invalid.empty
    assert np.isclose(results.iloc[0]['exact_pnl'], expected)


def test_pnl_explain_reconciles_actual_value_change():
    previous = pd.DataFrame([_position(cob='2026-06-19', vol_a=0.30)])
    current = pd.DataFrame([_position(cob='2026-07-08', vol_a=0.31)])
    # Position identity must not depend on the changing market inputs.
    assert not {'adjusted_vol_a', 'price', 'qty_value'} & set(POSITION_KEY_COLUMNS)
    explain = calculate_pnl_explain(previous, current)
    assert len(explain) == 1
    assert explain.iloc[0]['position_status'] == 'Matched'
    assert not explain.iloc[0]['price_mark_changed']
    assert explain.iloc[0]['vol_mark_changed']
    assert np.isclose(
        explain.iloc[0]['actual_pnl'],
        explain.iloc[0]['explained_pnl'] + explain.iloc[0]['unexplained_pnl'],
    )


def test_pnl_explain_keeps_single_asset_black76_a_side_risk():
    previous = pd.DataFrame([_black_position('2026-07-27', 42.375)])
    current = pd.DataFrame([_black_position('2026-07-28', 43.375)])

    explain = calculate_pnl_explain(previous, current)

    assert len(explain) == 1
    assert explain.iloc[0]['position_status'] == 'Matched'
    assert explain.iloc[0]['price_mark_changed']
    assert np.isclose(
        explain.iloc[0]['delta_pnl'],
        previous.iloc[0]['quantity'] * previous.iloc[0]['delta_S1'],
    )
    assert explain.iloc[0]['gamma_pnl'] > 0
    assert explain.iloc[0]['vega_pnl'] == 0
    assert explain.iloc[0]['correlation_pnl'] == 0


def test_scenario_aggregation_never_sums_across_currencies():
    results = pd.DataFrame(
        [
            {
                'currency': 'USD',
                'substrategy': 'Same',
                'base_value': 100.25,
                'shocked_value': 101.50,
                'exact_pnl': 1.25,
                'delta_pnl': 1.25,
                'gamma_pnl': 0.0,
                'vega_pnl': 0.0,
                'correlation_pnl': 0.0,
                'theta_pnl': 0.0,
                'rate_pnl': 0.0,
                'approximation_pnl': 1.25,
                'interaction_residual': 0.0,
                'base_reconciliation': 0.0,
            },
            {
                'currency': 'EUR',
                'substrategy': 'Same',
                'base_value': 200.25,
                'shocked_value': 202.50,
                'exact_pnl': 2.25,
                'delta_pnl': 2.25,
                'gamma_pnl': 0.0,
                'vega_pnl': 0.0,
                'correlation_pnl': 0.0,
                'theta_pnl': 0.0,
                'rate_pnl': 0.0,
                'approximation_pnl': 2.25,
                'interaction_residual': 0.0,
                'base_reconciliation': 0.0,
            },
        ]
    )

    aggregated = aggregate_scenario(results, 'substrategy')

    assert len(aggregated) == 2
    assert dict(zip(aggregated['currency'], aggregated['exact_pnl'])) == {
        'EUR': 2.25,
        'USD': 1.25,
    }


def test_pnl_explain_aggregation_never_sums_across_currencies():
    rows = []
    for currency, actual in [('USD', 1.25), ('EUR', 2.25)]:
        row = {
            'currency': currency,
            'substrategy': 'Same',
            'actual_pnl': actual,
            'delta_pnl': actual,
            'gamma_pnl': 0.0,
            'vega_pnl': 0.0,
            'correlation_pnl': 0.0,
            'theta_pnl': 0.0,
            'trade_pnl': 0.0,
            'explained_pnl': actual,
            'unexplained_pnl': 0.0,
        }
        rows.append(row)

    aggregated = aggregate_pnl_explain(
        pd.DataFrame(rows),
        'substrategy',
    )

    assert len(aggregated) == 2
    assert dict(zip(aggregated['currency'], aggregated['actual_pnl'])) == {
        'EUR': 2.25,
        'USD': 1.25,
    }
