"""Shared forward identity in actual Pricer preparation and Vol Trades views."""
from datetime import date

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from options.live_forward_curves import LiveForwardCurveRepository, reanchor_surface
from options.tests.test_live_forward_curves import complete_view, NOW
from options.ttf_quote_valuation import TTFMarketSnapshot, TTFOptionSpec, value_ttf_quote
from options.brent_quote_valuation import QuotePrices
from options.ttf_volatility import black76_call_delta
import dash_options.shared.live_forwards as desk
from dash_options.features.pricer.structures import (
    calculate_structure,
    default_context,
    default_leg,
    build_delivery_month_component,
)
import dash_options.features.pricer.surface_reference as references


@pytest.fixture
def curve(monkeypatch):
    engine = create_engine('sqlite://')
    repo = LiveForwardCurveRepository(engine, schema=None)
    repo.metadata.create_all(engine)
    curve = repo.stage(complete_view(), actor='test', source_fingerprint='test', now=NOW)
    repo.activate(curve.revision, actor='test', expected_revision=None, now=NOW)
    monkeypatch.setattr(desk, 'resolve', lambda revision=None: repo.resolve(revision or curve.revision))
    yield curve
    engine.dispose()


@pytest.mark.parametrize('asset,mapping', [('Brent','ICE-BRENT-B'), ('HH','CME-HH-LNE'), ('HH','CME-HH-ON'), ('TTF','ICE-TTF-TFO')])
def test_pricer_uses_exact_month_and_persists_revision(curve, asset, mapping):
    from dash_options.features.pricer.exchange_registry import exchange_option_mapping
    model = exchange_option_mapping(mapping).model
    context = {**default_context(model, NOW.date()), 'asset': asset, 'curve_mode': 'pinned',
               'curve_revision': curve.revision, 'delivery_month': '2027-02-01',
               'exchange_mapping_id': mapping}
    context['premium_convention'] = exchange_option_mapping(mapping).premium_convention
    resolved = desk.pricer_context(asset, context, NOW.date())
    mark = curve.mark(asset, '2027-02-01', now=NOW)
    assert resolved['forward'] == mark['forward']
    snapshot = calculate_structure(model, resolved, {'structure_quantity': 1, 'contract_multiplier': 1},
                                   [dict(default_leg(model, 1), strike=mark['forward'])], as_of=NOW.date())
    assert snapshot['context']['forward'] == mark['forward']
    assert snapshot['context']['curve_revision'] == curve.revision
    assert snapshot['context']['curve_provenance'][0]['curve_captured_at'] == NOW.isoformat()


def test_ttf_strip_uses_each_monthly_forward_instead_of_flat_strip_average(curve):
    context = {**default_context('black76', NOW.date()), 'asset': 'TTF', 'curve_mode': 'pinned',
               'curve_revision': curve.revision, 'delivery_shape': 'Q1', 'delivery_year': 2027,
               'exchange_mapping_id': 'ICE-TTF-TFO', 'premium_convention': 'futures_style'}
    resolved = desk.pricer_context('TTF', context, NOW.date())
    snapshot = calculate_structure('black76', resolved, {'structure_quantity': 1, 'contract_multiplier': 1},
                                   [dict(default_leg('black76', 1), strike=31)], as_of=NOW.date())
    components = snapshot['context']['delivery_components']
    assert len({part['forward'] for part in components}) == 3
    assert sum(part['weight']*part['forward'] for part in components) == pytest.approx(resolved['forward'])
    for part in components:
        assert part['forward'] == curve.mark('TFO', part['contract_month'], now=NOW)['forward']
    # Scenario base points must reproduce the displayed valuation, preserving monthly shape.
    from dash_options.features.pricer.structures import payoff_series, parallel_volatility_series, time_decay_series
    total = snapshot['totals']['trade_value']
    assert payoff_series(snapshot, valuation_date=NOW.date())['current_value'] == pytest.approx(total)
    assert time_decay_series(snapshot, max_points=3)['values'][0] == pytest.approx(total)
    series = parallel_volatility_series(snapshot, points=5)
    assert series['values'][series['shifts_percentage_points'].index(0.)] == pytest.approx(total)


def test_ice_ttf_and_pricer_share_live_moneyness_convention(curve):
    month = date(2027, 2, 1)
    forward = curve.mark('TTF', month, now=NOW)['forward']
    component = build_delivery_month_component('TTF', 'black76', month, NOW.date(), forward, mapping_id='ICE-TTF-TFO')
    expiry = date.fromisoformat(component['option_expiration_date'])
    years = (expiry-NOW.date()).days/365.25
    old_forward = 29.5
    strikes = np.linspace(18., 46., 401)
    vols = .4+.1*np.log(strikes/old_forward)**2
    points = pd.DataFrame({'contract_date': month, 'strike': strikes, 'volatility': vols,
        'working_forward': old_forward, 'option_expiration_date': expiry,
        'delta': [black76_call_delta(old_forward, k, years, v) for k,v in zip(strikes, vols)], 'put_call': 'C'})
    prepared = references._prepare_component_surface(points, component, asset='TTF', valuation_date=NOW.date(), current_forward=forward)
    market = TTFMarketSnapshot(month, expiry, 'test-publication', NOW.date(), 0, 'test', forward,
        'Live Prices desk curve', None, tuple(strikes), tuple(vols), surface_forward=old_forward,
        surface_years=years, curve_provenance=curve.mark('TFO', month, now=NOW))
    live_view = reanchor_surface(points, curve, 'TFO', now=NOW)
    assert live_view.forward_value.iloc[0] == forward
    for strike in (25., 30., 35.):
        chat = value_ttf_quote(TTFOptionSpec(month, 'C', strike, NOW), QuotePrices(), market)
        result = references._prepared_component_result(prepared, model='black76', current_forward=forward, strike=strike)
        assert chat.our_volatility == pytest.approx(result['pricing_volatility'], abs=1e-13)
        context = desk.pricer_context('TTF', {**default_context('black76', NOW.date()),
            'asset': 'TTF', 'curve_mode': 'pinned', 'curve_revision': curve.revision,
            'delivery_month': month.isoformat(), 'exchange_mapping_id': 'ICE-TTF-TFO',
            'expiration_date': expiry.isoformat(), 'contract_expiration_date': expiry.isoformat(),
            'premium_convention': 'futures_style'}, NOW.date())
        priced = calculate_structure('black76', context, {'structure_quantity': 1, 'contract_multiplier': 1},
            [dict(default_leg('black76', 1), strike=strike, quote_value=result['reference_volatility'])], as_of=NOW.date())
        assert chat.theoretical_price == pytest.approx(priced['totals']['unit_structure_value'], abs=1e-11)
        for metric in ('delta', 'gamma', 'theta', 'vega', 'rho'):
            assert getattr(chat, metric) == pytest.approx(priced['totals']['unit_structure_greeks'][metric], abs=1e-11)


def test_manual_and_phe_contract_boundaries(curve):
    manual = {'curve_mode': 'manual', 'forward': 7.123}
    assert desk.pricer_context('HH', manual, NOW.date()) == manual
    with pytest.raises(ValueError, match='PHE'):
        desk.pricer_context('HH', {'curve_mode': 'pinned', 'curve_revision': curve.revision,
                                 'exchange_mapping_id': 'ICE-HH-PHE'}, NOW.date())
    with pytest.raises(ValueError, match='after the valuation'):
        desk.pricer_context('TTF', {'curve_mode': 'pinned', 'curve_revision': curve.revision}, date(2026,10,21))
    with pytest.raises(ValueError, match='equivalence'):
        desk.pricer_context('TTF', {'curve_mode': 'pinned', 'curve_revision': curve.revision,
                                   'exchange_mapping_id': 'CME-TTF-TFO'}, NOW.date())
    # A capture late in UTC already belongs to the next Dubai valuation day.
    from dataclasses import replace
    payload = {**curve.payload, 'view': {**curve.payload['view'], 'captured_at': '2026-10-22T23:00:00+00:00'}}
    with pytest.raises(ValueError, match='after the valuation'):
        desk.pricer_context('TTF', {'curve_mode': 'pinned', 'curve_revision': curve.revision},
                           date(2026,10,22), curve=replace(curve, payload=payload))


def test_live_prices_poll_never_activates_and_manual_accept_requires_publisher(curve, monkeypatch):
    from types import SimpleNamespace
    from dash import no_update
    from dash_options.features.live_prices import page
    from dash_options.calibration.auth import Permission
    calls = []
    repo = SimpleNamespace(resolve=lambda: curve,
        stage=lambda *a, **k: calls.append('stage') or curve,
        activate=lambda *a, **k: calls.append(('activate', k)) or curve)
    monkeypatch.setattr(desk, 'enabled', lambda: True)
    monkeypatch.setattr(desk, 'repository', lambda: repo)
    monkeypatch.setattr(page, 'identity', lambda permission: calls.append(permission) or 'publisher')
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(triggered_id='live-prices-desk-poll'))
    reference, head, status = page.manage_desk_curve(1, 0, 0, None, None)
    assert reference == {'kind': 'desk_curve', 'revision': curve.revision}
    assert head == curve.revision and 'Stale / held' in status
    assert calls == [Permission.VIEW]
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(triggered_id='live-prices-accept-desk'))
    monkeypatch.setattr(page.data, 'resolve', lambda *a: curve.view())
    page.manage_desk_curve(1, 0, 1, {'capture': 'candidate'}, head)
    assert calls[-3] == Permission.PUBLISH
    assert calls[-1][1]['expected_revision'] == head
    def deny(permission):
        raise PermissionError('Publisher permission required')
    monkeypatch.setattr(page, 'identity', deny)
    count = len(calls)
    blocked = page.manage_desk_curve(1, 0, 2, {'capture': 'candidate'}, head)
    assert blocked[:2] == (no_update, no_update)
    assert 'Publisher' in blocked[2] and len(calls) == count


def test_follow_desk_stale_is_held_and_pinned_retains_revision(curve, monkeypatch):
    from dash_options.features.vol_trades import page as history
    from dash_options.features.pricer.input_callbacks import sync_desk_forward
    from dash import no_update
    monkeypatch.setattr(desk, 'enabled', lambda: True)
    state, message = history.update_desk_curve(1, 'desk', None)
    assert not state.get('revision') and 'stale' in message
    state, message = history.update_desk_curve(2, 'pinned', {'revision': curve.revision})
    assert state['revision'] == curve.revision and 'Stale / held' in message
    result = sync_desk_forward(1, 'pinned', '2027-02-01', 'MONTH', 2027, 'Brent',
                              'ICE-BRENT-B', NOW.date().isoformat(), curve.revision, 0)
    assert result[0] == curve.mark('Brent', '2027-02-01', now=NOW)['forward']
    assert result[1] is True and 'Pinned' in result[3]
    result = sync_desk_forward(2, 'manual', None, None, None, 'Brent', None, None, curve.revision, 12)
    assert result[:3] == (no_update, False, None)


@pytest.mark.parametrize('product', ['BRENT', 'ON', 'LNE', 'TFO'])
def test_vol_trades_valuation_view_uses_same_revision_without_changing_quotes(curve, monkeypatch, product):
    from dash_options.features.vol_trades import page as history
    import dash_options.features.vol_trades.data as market_data
    from tests.helpers.market_frames import _chain_frame, _calibrated_frame
    today = pd.Timestamp.now(tz=history.MARKET_TIMEZONE).date().isoformat()
    chain = _chain_frame().assign(product=product, business_date=today)
    original = chain.copy(deep=True)
    identifier = chain.iloc[0]['snapshot_id']
    snapshots = pd.DataFrame([{'snapshot_id': identifier, 'business_date': today,
                              'observed_at': today+'T08:00:00Z', 'snapshot_kind': 'INTRADAY'}])
    surface = _calibrated_frame()
    calls = []
    monkeypatch.setattr(market_data, 'load_available_snapshots', lambda *a: snapshots)
    monkeypatch.setattr(market_data, 'load_chain_snapshot', lambda *a, **k: chain)
    monkeypatch.setattr(market_data, 'load_trade_tape', lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(market_data, 'load_latest_calibrated_surface', lambda *a, **k: surface)
    monkeypatch.setattr(market_data, 'load_published_surface', lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(desk, 'live_surface', lambda *a: calls.append(a) or surface)
    result = history.render_history(identifier, 'strike', product,
        _live_curve={'revision': curve.revision, 'mode': 'pinned'})
    assert result[0]['live_curve']['revision'] == curve.revision
    assert result[0]['live_curve']['marks'][0]['forward'] == curve.mark(product, '2026-12-01', now=NOW)['forward']
    pd.testing.assert_frame_equal(chain, original)
    assert calls[0][0] == product
    # A historical capture retains its original calibration, even when a desk mode is selected.
    snapshots['business_date'] = '2026-10-21'
    calls.clear()
    old = history.render_history(identifier, 'strike', product,
        _live_curve={'revision': curve.revision, 'mode': 'pinned'})
    assert old[0]['live_curve'] is None and calls == []
