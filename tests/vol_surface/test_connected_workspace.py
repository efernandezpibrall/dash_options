"""Selection, saved-node parity and unavailable-contract behavior for the workspace."""
import pandas as pd
import pytest
from dash_options.features.vol_surface import page, overview, selector
from tests.vol_surface.test_overview import nodes


def test_detail_clicks_decode_months_and_never_treat_aggregates_as_options():
    assert page.detail_click_selection('surface-term-graph', {'points':[{'x':'2027-02-01'}]}, 'TTF') == ('TTF','2027-02-01')
    assert page.detail_click_selection('surface-heatmap-graph', {'points':[{'y':'2027-02'}]}, 'HH') == ('HH','2027-02-01')
    event = {'data':{'product':'Brent'}, 'colId':'02-27'}
    assert page.detail_click_selection('volatility-table', event, 'TTF') == ('Brent','2027-02-01')
    assert page.detail_click_selection('volatility-table', event, 'TTF','quarterly') is None
    assert page.detail_click_selection('surface-current-table', {'data':{'expiry':"Feb'27"}}, 'TTF') == ('TTF','2027-02-01')
    assert page.detail_click_selection('surface-term-graph', {'points':[]}, 'TTF') is None


def test_term_uses_exact_saved_atm_and_keeps_missing_nodes():
    current = nodes(months=('2027-01-01','2027-03-01'))
    previous = nodes(date='2026-10-28',months=('2027-01-01','2027-02-01','2027-03-01'))
    previous['volatility'] -= .1
    figure = page.create_term_figure('TTF', current, previous, 'month:2027-03-01',
                                    {'current_label':'Draft exact-id', 'comparison_label':'Settlement 28 Oct'})
    assert list(figure.data[0].y)[::2] == pytest.approx([1.4,1.4])
    assert pd.isna(figure.data[0].y[1])
    assert figure.data[0].connectgaps is False
    assert list(figure.data[1].y) == pytest.approx([1.3]*3)
    assert figure.data[0].name == 'Draft exact-id'
    assert figure.layout.meta['contract'] == '2027-03-01'
    assert len(figure.layout.shapes) == 1
    assert figure.layout.shapes[0].x0 == '2027-03-01'
    assert figure.layout.xaxis.type == 'date'


def test_primary_contract_follows_shared_selector_even_without_history(monkeypatch):
    frame = nodes('HH')
    monkeypatch.setattr(overview, 'snapshot_frame', lambda _: frame)
    monkeypatch.setattr(page.delta_history_data, 'available_contracts', lambda *a: ['2027-01-01'])
    result = page.select_linked_history_contracts({'snapshot_id':'test'}, '2026-10-29', 'HH', 90,
        'month:2027-02-01','2027-01-01',None,{'product':'HH'})
    assert result[2] == '2027-02-01'
    assert '2027-02-01' in [o['value'] for o in result[1]]
    result = page.select_linked_history_contracts({'snapshot_id':'test'}, '2026-10-29', 'HH', 90,
        None,'2027-01-01',None,{'product':'HH'})
    assert result[2] is None


def test_unavailable_revision_cannot_show_settlement_term(monkeypatch):
    monkeypatch.setattr(page.surface_data, '_ensure_cached_data', lambda _: None)
    def refuse(*a):
        raise selector.SelectionError('Revision unavailable')
    monkeypatch.setattr(page, '_selected_surface_pair', refuse)
    figure, detail, history = page.linked_term_view({'snapshot_id':'x'}, '2026-10-29',None,
        'TTF','month:2027-02-01',{},'draft','missing','anchor')
    assert not figure.data
    assert 'Revision unavailable' in figure.layout.annotations[0].text
    assert detail == 'TTF' and 'TTF' in history and "Feb'27" in history
