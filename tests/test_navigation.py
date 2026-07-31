from dash import dcc, html

import index_options


def _walk(component):
    yield component
    children = getattr(component, 'children', None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if child is not None:
            yield from _walk(child)


def _links(component):
    return [item for item in _walk(component) if isinstance(item, dcc.Link)]


def test_top_navigation_order_and_pricer_separator():
    links = _links(index_options.nav_links)

    assert [link.children for link in links] == [
        'Greeks',
        'Valuation',
        'Trades',
        'Underlying Prices',
        'Volatility Surface',
        'Vol Calibration',
        'Correlations',
        'Scenarios',
        'Pricer',
    ]
    assert all(link.children != 'P&L Explain' for link in links)
    assert links[-1].id == 'nav-pricer'
    assert getattr(links[-1], 'href') == '/pricer'

    pricer_group = next(
        item
        for item in _walk(index_options.nav_links)
        if getattr(item, 'className', None) == 'nav-group-pricer'
    )
    assert pricer_group.children.id == 'nav-pricer'


def test_valuation_route_integrates_pnl_explain_as_a_view():
    valuation_workspace = index_options.display_page('/valuation', None)
    pnl_workspace = index_options.display_page(
        '/valuation',
        '?view=pnl-explain',
    )
    legacy_workspace = index_options.display_page('/pnl_explain', None)

    assert getattr(valuation_workspace, 'data-active-view') == 'valuation'
    assert getattr(pnl_workspace, 'data-active-view') == 'pnl-explain'
    assert getattr(legacy_workspace, 'data-active-view') == 'pnl-explain'

    valuation_links = [
        item
        for item in _walk(valuation_workspace)
        if isinstance(item, html.A)
    ]
    assert [link.children for link in valuation_links[:2]] == [
        'Valuation',
        'P&L Explain',
    ]
    assert getattr(valuation_links[0], 'aria-current') == 'page'
    assert getattr(
        pnl_workspace.children[0].children[1],
        'aria-current',
    ) == 'page'

    valuation_ids = {
        getattr(item, 'id', None)
        for item in _walk(valuation_workspace)
    }
    pnl_ids = {
        getattr(item, 'id', None)
        for item in _walk(pnl_workspace)
    }
    assert 'pnl-table' in valuation_ids
    assert 'pnl-explain-waterfall' not in valuation_ids
    assert 'pnl-explain-waterfall' in pnl_ids
    assert 'pnl-table' not in pnl_ids
