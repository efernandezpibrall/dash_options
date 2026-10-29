"""Shared market workspaces render frozen rows without changing their values."""
from copy import deepcopy

from options.market_data.ttf_live_curve import month_add
from dash_options.features.live_prices import page


def walk(component):
    if isinstance(component, (tuple, list)):
        for child in component:
            yield from walk(child)
    elif hasattr(component, 'to_plotly_json'):
        yield component
        yield from walk(getattr(component, 'children', None))


def test_saved_snapshot_has_four_workspaces_without_coverage_or_duplicate_ids():
    months = [month_add('2026-11-01', i) for i in range(48)]
    view = {'captured_at': '2026-10-23T06:31:00+00:00', 'months': months,
            'products': {}, 'strips': {'TTF': []}, 'warnings': [], 'fx': [], 'ttf_curve': {}}
    for index, (product, _) in enumerate(page.PRODUCTS, 1):
        view['products'][product] = [
            {'id': f'{product}-{month}', 'month': month, 'maturity': month,
             'mid': index * 10, 'prior': index * 10 - 1,
             'bid': index * 10 - 2, 'ask': index * 10 + 2}
            for month in months]
    # Legacy saved snapshots have no HH strips or explicit generic period kinds.
    before = deepcopy(view)
    rendered = page.render(view)
    nodes = list(walk(rendered))
    ids = [node.id for node in nodes if isinstance(getattr(node, 'id', None), str)]
    assert len(ids) == len(set(ids))
    for product, _ in page.PRODUCTS:
        assert f'live-prices-{product.lower()}-workspace' in ids
        monthly = next(n for n in nodes if getattr(n, 'id', None) ==
                       {'type': 'live-prices-grid-values', 'name': f'live-prices-{product.lower()}-grid'})
        assert monthly.rowData == view['products'][product]
        for kind in ('calendar', 'season', 'quarter'):
            if product == 'TTF':
                continue  # This fixture does not manufacture native TTF strips.
            grid_id = f'live-prices-{product.lower()}-{kind}-grid'
            periods = next(n for n in nodes if getattr(n, 'id', None) == {'type': 'live-prices-grid-values', 'name': grid_id})
            assert periods.rowData and all(r['kind'] == kind for r in periods.rowData)
            assert periods.rowData[0]['mid'] == view['products'][product][0]['mid']
    for node in nodes:
        if type(node).__name__ == 'AgGrid':
            assert not node.defaultColDef['sortable']
            assert node.dashGridOptions['suppressScrollOnNewData']
            assert all(c['field'] != 'coverage' for c in node.columnDefs)
    assert view == before
    charts = [n for n in nodes if type(n).__name__ == 'Graph']
    assert [chart.id['name'] for chart in charts] == [f'live-prices-{p}-curve' for p in ('ttf', 'brent', 'hh')]
    for chart, (product, unit) in zip(charts, [p for p in page.PRODUCTS if p[0] != 'TFU']):
        assert [trace.name for trace in chart.figure.data] == [product + ' Mark', 'D-1']
        assert list(chart.figure.data[0].x) == months
        assert list(chart.figure.data[0].y) == [r['mid'] for r in view['products'][product]]
        assert list(chart.figure.data[1].y) == [r['prior'] for r in view['products'][product]]
        assert chart.figure.layout.yaxis.title.text == unit
        assert all(trace.connectgaps is False for trace in chart.figure.data)
    assert getattr(rendered.children[-1], 'aria-labelledby') == 'live-prices-hh-curve-heading'


def test_ttf_strips_keep_reconciled_mark_and_raw_quotes_after_coverage_removal():
    row = {'id': 'ttf-calendar-2030', 'period': 'Cal ’30', 'kind': 'calendar',
           'mid': 29, 'live_mid': 40, 'bid': 20, 'ask': 60, 'prior': 28, 'coverage': '12/12'}
    panel = page.period_card('TTF', [row])
    grid = next(n for n in walk(panel) if getattr(n, 'id', None) ==
                {'type': 'live-prices-grid-values', 'name': 'live-prices-ttf-calendar-grid'})
    assert grid.rowData == [row]
    assert [c['field'] for c in grid.columnDefs] == ['period', 'mid', 'prior', 'change', 'live_mid', 'bid', 'ask']


def test_direct_updates_match_full_render_and_keep_static_columns(monkeypatch):
    months = [month_add('2026-11-01', i) for i in range(48)]
    old = {'captured_at': '2026-10-23T06:31:00+00:00', 'months': months,
           'products': {}, 'strips': {}, 'warnings': [], 'fx': []}
    for product, _ in page.PRODUCTS:
        old['products'][product] = [{'id': f'{product}-{m}', 'month': m, 'maturity': m,
                                     'mid': 30, 'prior': 29, 'mark_basis': 'Monthly'} for m in months]
    new = deepcopy(old)
    new['captured_at'] = '2026-10-23T06:32:00+00:00'
    new['products']['TTF'][0].update(mid=32, mark_basis='D-1 fallback', mark_quality='Settlement fallback')
    new['products']['HH'][1]['mid'] = None
    new['warnings'] = ['Updated source notice']
    from dash import no_update
    from options.market_data.live_prices import METHODOLOGY_VERSION
    old['methodology_version'] = new['methodology_version'] = METHODOLOGY_VERSION
    _, values = page.render_parts(new)
    kinds = ('live-prices-grid-values', 'live-prices-curve-values', 'live-prices-section-values')
    identifiers = [[{'type': kind, 'name': name} for name in values[kind]] for kind in kinds]
    monkeypatch.setattr(page, 'identity', lambda: 'alice')
    monkeypatch.setattr(page.data, 'resolve', lambda ref, actor: {'old': old, 'new': new}[ref])
    result = page.show_snapshot('new', 'old', *identifiers)
    assert result[0] is no_update and result[1] == 'new'
    grids, curves, sections = result[2:]
    ttf = identifiers[0].index({'type': kinds[0], 'name': 'live-prices-ttf-grid'})
    assert grids[ttf] == new['products']['TTF']
    assert len(sections) == 11 and len(curves) == 3
    for patch, (product, _) in zip(curves, [p for p in page.PRODUCTS if p[0] != 'TFU']):
        operation, = patch.to_plotly_json()['operations']
        assert operation['location'] == ['data']  # Zoom/layout and mounted graphs survive.
        assert operation['params']['value'][0]['y'] == [r.get('mid') for r in new['products'][product]]
    initial = page.show_snapshot('new')
    assert initial[0] is not no_update and initial[2:] == ([], [], [])
    # Exercise Dash's real wildcard-output validator for first mount and updates.
    from dash._validate import validate_multi_return
    validate_multi_return([{'id': 'board'}, {'id': 'reference'}, [], [], []], initial, 'first-mount')
    validate_multi_return([{'id': 'board'}, {'id': 'reference'}, *identifiers], result, 'mounted-update')
