from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from dash import no_update

from dash_options.core.snapshots import SnapshotStore, SnapshotReferenceError
from dash_options.calibration.auth import Identity, Role
from dash_options.features.vol_surface import overview, page, selector
from dash_options.shared import surfaces
from tests.vol_surface.test_surface_selector import selected_sources  # noqa: F401


def nodes(product='TTF', date='2026-10-29', months=('2027-01-01', '2027-02-01')):
    return surfaces._normalize_surface_data(pd.DataFrame([
        dict(product=product, cob_date=date, maturity_date=m, put_call='C', delta=d, value=v)
        for m in months for d, v in [(.9, 1.1), (.5, 1.4), (.1, 1.6)]
    ]), volatility_in_percent=False)


def test_exact_nodes_units_and_native_put_convention():
    frame = nodes()
    metrics = overview.metric_frame(frame, 10)
    assert metrics.iloc[0].tolist() == pytest.approx([140, 50, -5])
    put = surfaces._normalize_surface_data(pd.DataFrame([dict(
        product='TTF', cob_date='2026-10-29', maturity_date='2027-01-01', put_call='P', delta=-.1, value=1.2,
    )]), volatility_in_percent=False)
    metrics = overview.metric_frame(pd.concat([frame, put]), 10)
    assert metrics.iloc[0].tolist() == pytest.approx([140, 40, 0])
    assert metrics.iloc[1].tolist() == pytest.approx([140, 50, -5])


def test_missing_rounded_and_conflicting_nodes_are_not_fabricated():
    frame = nodes(months=('2027-01-01',))
    missing_atm = overview.metric_frame(frame.loc[frame.delta.ne(.5)], 10).iloc[0]
    assert np.isnan(missing_atm.atm) and np.isnan(missing_atm.bf)
    assert missing_atm.rr == pytest.approx(50)
    nearby = frame.copy()
    nearby.loc[nearby.delta.eq(.1), 'delta_abs'] = .1001
    assert overview.metric_frame(nearby, 10).rr.isna().all()
    conflict = frame.loc[frame.delta.eq(.1)].copy()
    conflict['volatility'] += .1
    assert overview.metric_frame(pd.concat([frame, conflict]), 10).rr.isna().all()
    identical = pd.concat([frame, frame])
    assert overview.metric_frame(identical, 10).iloc[0].tolist() == pytest.approx([140, 50, -5])


def test_ambiguous_native_put_does_not_substitute_call90():
    frame = nodes(months=('2027-01-01',))
    put = frame.loc[frame.delta.eq(.1)].copy()
    put['put_call'] = 'put'
    put['volatility'] = np.nan
    assert overview.metric_frame(pd.concat([frame, put]), 10).rr.isna().all()


@pytest.fixture
def immutable_snapshot(monkeypatch, tmp_path):
    store = SnapshotStore(tmp_path / 'overview')
    frame = pd.concat([nodes('Brent'), nodes('HH'), nodes('TTF'), nodes('TTF', '2026-10-28')])
    reference = store.publish(surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE, 'first', {'surface_dataset': frame})
    monkeypatch.setattr(overview, 'resolve_snapshot', store.resolve)
    yield store, reference, frame
    store.close()


def test_reference_is_immutable_and_requires_correct_namespace(immutable_snapshot, monkeypatch):
    store, reference, frame = immutable_snapshot
    replacement = frame.copy()
    replacement['volatility'] = .01
    store.publish(surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE, 'second', {'surface_dataset': replacement})
    monkeypatch.setattr(surfaces, 'surface_dataset', replacement)
    result = overview.surface_pairs(reference, '2026-10-29', '2026-10-28', ['TTF'])
    assert overview.metric_frame(result['TTF']['current']).iloc[0].atm == 140
    with pytest.raises(SnapshotReferenceError):
        overview.snapshot_frame({**reference, 'namespace': 'wrong'})


@pytest.mark.usefixtures("selected_sources")
def test_draft_only_overrides_its_product_and_rechecks_owner(immutable_snapshot, monkeypatch):
    _, reference, _ = immutable_snapshot
    selected = selector.select_revision('Brent', 'draft', None, None, None)[4]
    result = overview.surface_pairs(reference, '2026-10-29', '2026-10-28', ['Brent', 'TTF'],
                                    'Brent', 'draft', selected, 'draft1', 'anchor')
    assert overview.metric_frame(result['Brent']['current']).atm.iloc[0] == pytest.approx(54)
    assert overview.metric_frame(result['Brent']['previous']).atm.iloc[0] == 50
    assert overview.metric_frame(result['TTF']['current']).atm.iloc[0] == 140
    assert result['Brent']['origins']['2027-02'] == 'Inherited 2026-10-29'
    monkeypatch.setattr(selector, 'current_request_identity',
                        lambda: Identity('bob', frozenset({Role.VIEWER}), True, 'test'))
    refused = overview.surface_pairs(reference, '2026-10-29', '2026-10-28', ['Brent', 'TTF'],
                                     'Brent', 'draft', selected, 'draft1', 'anchor')
    assert refused['Brent']['current'].empty and refused['Brent']['previous'].empty
    assert refused['Brent']['error']
    assert not refused['TTF']['current'].empty


def test_expired_or_loading_replay_fails_closed(immutable_snapshot):
    _, reference, _ = immutable_snapshot
    result = overview.surface_pairs(reference, '2026-10-29', None, ['Brent', 'HH'],
                                    'Brent', 'intraday', None, None, None)
    assert result['Brent']['error'] and result['Brent']['current'].empty
    assert not result['HH']['current'].empty


def test_delivery_axes_align_and_preserve_missing_months():
    frame = pd.concat([nodes('TTF', months=('2027-01-01', '2027-03-01')),
                       nodes('Brent', months=('2027-02-01', '2027-03-01'))])
    pairs = {product: {'current': frame.loc[frame.code.eq(product)]} for product in ['TTF', 'Brent']}
    months = overview.delivery_months(pairs, 24)
    assert list(months) == list(pd.to_datetime(['2027-01-01', '2027-02-01', '2027-03-01']))
    ttf = overview.metric_frame(frame.loc[frame.code.eq('TTF')])
    pair = dict(origins={}, source='settlement', current_label='Selected', comparison_label='Comparison')
    fig = overview.metric_figure('TTF', 'atm', 'ATM', '%', ttf, ttf, months, '2027-03-01', pair)
    assert list(fig.data[1].y) == [140, None, 140]
    assert not fig.data[1].connectgaps
    assert fig.data[1].customdata[2][:2] == ['TTF', '2027-03-01']
    assert fig.data[0].customdata[2][2] == 'Comparison'


def test_navigation_uses_the_clicked_product_and_validates_month(monkeypatch):
    graph = {'type': 'vol-overview-chart', 'product': 'HH', 'metric': 'rr'}
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(triggered_id=graph))
    context = {'months': ['2027-01-01', '2027-02-01'], 'fronts': {'HH': '2027-01-01'}}
    click = {'points': [{'customdata': ['HH', '2027-02-01', 'Settlement']}]}
    result = page.navigate_overview([click], [], [graph], [], context, None)
    assert result == {'product': 'HH', 'month': '2027-02-01', 'sequence': 1}
    click['points'][0]['customdata'][0] = 'TTF'
    assert page.navigate_overview([click], [], [graph], [], context, None) is no_update
    assert overview.click_selection({'points': [{'customdata': 'bad'}]}, 'HH', ['2027-02-01']) is None


def test_month_navigation_updates_existing_smile(immutable_snapshot, monkeypatch):
    _, _, frame = immutable_snapshot
    monkeypatch.setattr(surfaces, '_ensure_cached_data', lambda *args: None)
    monkeypatch.setattr(surfaces, '_get_surface_snapshot',
                        lambda product, date: overview.settlement_slice(frame, product, date))
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(triggered_id='vol-overview-navigation'))
    output = page.update_surface_expiry_dropdown(None, '2026-10-29', 'TTF', None, 'settlement',
                                                'settlement', 'previous',
                                                {'product': 'TTF', 'month': '2027-02-01'}, None, 'month:2027-01-01')
    assert output[1] == 'month:2027-02-01'

    # A revision completing in the same browser batch must not discard navigation.
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(
        triggered_id='surface-selection-ref',
        triggered_prop_ids={'surface-selection-ref.data': 'surface-selection-ref',
                            'vol-overview-navigation.data': 'vol-overview-navigation'},
    ))
    output = page.update_surface_expiry_dropdown(None, '2026-10-29', 'TTF', None, 'settlement',
                                                'settlement', 'previous',
                                                {'product': 'TTF', 'month': '2027-02-01'}, None, 'month:2027-01-01')
    assert output[1] == 'month:2027-02-01'


def test_graph_reset_does_not_navigate(monkeypatch):
    graph = {'type': 'vol-overview-chart', 'product': 'TTF', 'metric': 'atm'}
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(triggered_id=graph))
    output = page.navigate_overview([None], [], [graph], [], {'months': ['2027-02-01'], 'fronts': {}},
                                   {'product': 'TTF', 'month': '2027-01-01'})
    assert output is no_update


def test_shared_delta_updates_current_and_comparison_curves_without_moving_atm(monkeypatch):
    frame = surfaces._normalize_surface_data(pd.DataFrame([
        dict(product='TTF', cob_date='2026-10-29', maturity_date='2027-01-01', put_call='C', delta=d, value=v)
        for d, v in [(.1, .8), (.2, .7), (.3, .6), (.5, .5), (.7, .56), (.8, .64), (.9, .76)]
    ]), volatility_in_percent=False)
    previous = frame.copy()
    previous.loc[previous.delta.eq(.2), 'volatility'] = .68
    pair = dict(current=frame, previous=previous, origins={}, source='settlement', error=None,
                current_label='Current', comparison_label='Previous')
    monkeypatch.setattr(overview, 'surface_pairs', lambda *args: {'TTF': pair, 'Brent': pair})
    def figures(delta):
        _, grid, _, _ = page.render_overview(None, '2026-10-29', '2026-10-28', ['TTF', 'Brent'],
                                             24, delta, 'TTF', None, 'settlement', None, None)
        return [[child.children[1].figure for child in card.children
                 if getattr(child, 'className', '') == 'vol-overview-metric'] for card in grid.children]

    ten, twenty_five = figures(10), figures(25)
    for exact, estimated in zip(ten, twenty_five):
        assert exact[0].data[1].y == estimated[0].data[1].y == (50,)
        assert exact[1].data[1].y == pytest.approx([4])
        assert estimated[1].data[1].y == pytest.approx([5])
        assert estimated[1].data[0].y == pytest.approx([4])
        assert estimated[2].data[1].y == pytest.approx([12.5])
        assert estimated[2].data[0].y == pytest.approx([12])
        assert '25Δ Risk Reversal' in estimated[1].data[1].hovertemplate
        assert estimated[1].data[1].customdata[0][3] == 'Interpolated wing IVs'


def test_front_contract_uses_source_cob_and_keeps_incomplete_front_nodes():
    frame = nodes(months=('2026-11-01', '2026-12-01'))
    frame['option_expiration_date'] = frame.contract_date.map({
        pd.Timestamp('2026-11-01'): pd.Timestamp('2026-10-20'),
        pd.Timestamp('2026-12-01'): pd.Timestamp('2026-11-20'),
    })
    assert overview.front_month(frame) == pd.Timestamp('2026-12-01')
    replay = frame.copy()
    replay['cob_date'] = pd.Timestamp('2026-10-13')
    assert overview.front_month(replay) == pd.Timestamp('2026-11-01')
    incomplete = replay.loc[~(replay.contract_date.eq(pd.Timestamp('2026-11-01')) & replay.delta.eq(.5))]
    assert overview.front_month(incomplete) == pd.Timestamp('2026-11-01')
    assert overview.front_month(surfaces._empty_surface_df()) is None


def test_front_readouts_label_each_products_contract_and_compare_that_same_month(monkeypatch):
    brent = nodes('Brent', months=('2027-01-01', '2027-02-01'))
    ttf = nodes('TTF', months=('2026-12-01', '2027-01-01'))
    ttf.loc[ttf.contract_date.eq(pd.Timestamp('2026-12-01')) & ttf.delta.eq(.5), 'volatility'] = .7
    previous = ttf.loc[ttf.contract_date.eq(pd.Timestamp('2027-01-01'))]
    pairs = {product: dict(current=frame, previous=previous, origins={}, source='settlement', error=None,
                           current_label='Current', comparison_label='Previous')
             for product, frame in [('Brent', brent), ('TTF', ttf)]}
    monkeypatch.setattr(overview, 'surface_pairs', lambda *args: pairs)
    _, grid, context, _ = page.render_overview(None, '2026-10-29', '2026-10-28', ['Brent', 'TTF'],
                                              24, 10, 'TTF', None, 'settlement', None, None)
    assert context['months'][0] == '2026-12-01'
    assert context['fronts'] == {'Brent': '2027-01-01', 'TTF': '2026-12-01'}
    for card, contract in zip(grid.children, ["Jan'27", "Dec'26"]):
        readouts = [c.children[0].children[1] for c in card.children
                    if getattr(c, 'className', '') == 'vol-overview-metric']
        assert all(r.children[0].children == contract for r in readouts)
    ttf_atm = grid.children[1].children[1].children[0].children[1]
    assert ttf_atm.children[1].children == '70.00%'
    assert ttf_atm.children[2].children == 'Δ —'
    assert ttf_atm.title == 'Comparison: —'


def test_inspect_always_opens_its_front_contract_including_repeated_clicks(monkeypatch):
    link = {'type': 'vol-overview-inspect', 'product': 'TTF'}
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(triggered_id=link))
    context = {'months': ['2026-12-01', '2027-01-01'], 'fronts': {'TTF': '2026-12-01'}}
    previous = {'product': 'TTF', 'month': '2026-12-01', 'sequence': 1}
    result = page.navigate_overview([], [2], [], [link], context, previous)
    assert result == {'product': 'TTF', 'month': '2026-12-01', 'sequence': 2}
