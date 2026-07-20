import numpy as np
import pandas as pd

from options.options_library import kirk_model_with_substitution, kirk_spread_greeks
from valuation_analytics import (
    POSITION_KEY_COLUMNS,
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
