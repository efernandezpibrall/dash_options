"""Selection, saved-node parity and unavailable-contract behavior for the workspace."""
from dash_options.features.vol_surface import page, overview
from tests.vol_surface.test_overview import nodes


def test_detail_clicks_decode_months_and_never_treat_aggregates_as_options():
    assert page.detail_click_selection('surface-heatmap-graph', {'points':[{'y':'2027-02'}]}, 'HH') == ('HH','2027-02-01')
    event = {'data':{'product':'Brent'}, 'colId':'02-27'}
    assert page.detail_click_selection('volatility-table', event, 'TTF') == ('Brent','2027-02-01')
    assert page.detail_click_selection('volatility-table', event, 'TTF','quarterly') is None
    assert page.detail_click_selection('surface-current-table', {'data':{'expiry':"Feb'27"}}, 'TTF') == ('TTF','2027-02-01')
    assert page.detail_click_selection('surface-heatmap-graph', {'points':[]}, 'TTF') is None


def test_primary_contract_follows_shared_selector_even_without_history(monkeypatch):
    frame = nodes('HH')
    monkeypatch.setattr(overview, 'history_frame', lambda _: frame)
    monkeypatch.setattr(page.delta_history_data, 'available_contracts', lambda *a: ['2027-01-01'])
    result = page.select_linked_history_contracts({'snapshot_id':'test'}, '2026-10-29', 'HH', 90,
        'month:2027-02-01',None,'2027-01-01',None,{'product':'HH'})
    assert result[2] == '2027-02-01'
    assert '2027-02-01' in [o['value'] for o in result[1]]
    result = page.select_linked_history_contracts({'snapshot_id':'test'}, '2026-10-29', 'HH', 90,
        None,None,'2027-01-01',None,{'product':'HH'})
    assert result[2] is None


def test_actual_grid_click_payload_resolves_product_from_displayed_rows():
    rows=[{'product':'Brent'},{'product':'TTF'}]
    event={'rowIndex':1,'colId':'02-27','value':'64.00','timestamp':123}
    hydrated=page._overview_grid_event(event,rows)
    assert page.detail_click_selection('volatility-table',hydrated,'Brent')==('TTF','2027-02-01')
    assert page._overview_grid_event({'rowIndex':-1},rows) is None
    assert page._overview_grid_event({'rowIndex':2},rows) is None
    assert page._overview_grid_event(event,None) is None
