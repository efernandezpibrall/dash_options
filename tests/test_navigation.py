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
        'Vol Trades',
        'ICE Quotes',
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


def test_source_status_moves_inline_on_current_state_pages_without_weakening_alignment():
    statuses = [
            {
                'label': 'Portfolio',
                'source': 'at_lng.trades_options_valuation_current',
                'latest_cob': '2026-07-30',
                'business_day_age': 5,
                'fallback_used': False,
                'error': None,
            },
            {
                'label': 'Vol Surface',
                'source': 'at_lng.implied_volatility_surface_from_prices',
                'latest_cob': '2026-07-30',
                'business_day_age': 0,
                'fallback_used': False,
                'error': None,
            },
            {
                'label': 'Forward Curves',
                'source': 'at_lng.curve',
                'latest_cob': '2026-07-30',
                'business_day_age': 0,
                'fallback_used': False,
                'error': None,
            },
    ]

    global_banner = index_options._build_dashboard_source_status(statuses)
    headline, vol_chip, curve_chip = global_banner.children

    assert headline.children == 'Stale market data'
    assert vol_chip.children[0].children == 'Vol Surface: '
    assert vol_chip.children[1] == '2026-07-30'
    assert curve_chip.children[0].children == 'Forward Curves: '
    assert 'Portfolio' not in str(global_banner)
    assert 'bd old' not in str(global_banner)

    inline_banner = index_options.render_greeks_source_status(statuses, True)
    inline_icon, inline_headline, inline_vol, inline_curves = inline_banner.children
    assert inline_icon.children == '!'
    assert inline_headline.children == 'Stale data'
    assert inline_vol.children[1] == '30 Jul'
    assert inline_vol.children[0].children == 'Vol: '
    assert inline_curves.children[0].children == 'Curves: '
    assert 'greeks-source-status-content' in inline_banner.className.split()
    assert getattr(inline_banner, 'role') == 'status'
    assert 'Portfolio' not in getattr(inline_banner, 'aria-label')
    assert 'Vol Surface: 2026-07-30' in getattr(inline_banner, 'aria-label')

    valuation_inline = index_options.render_valuation_source_status(
        statuses,
        True,
        {
            'label': 'Aspect',
            'source': 'at_lng.pnl_aspect',
            'latest_cob': None,
            'business_day_age': None,
            'fallback_used': False,
            'error': 'Aspect data unavailable',
        },
    )
    assert valuation_inline.children[0].children == '×'
    assert valuation_inline.children[1].children == 'Source unavailable'
    assert valuation_inline.children[-1].children[0].children == 'Aspect: '
    assert valuation_inline.children[-1].children[1] == 'unavailable'
    assert 'greeks-source-status-content' in valuation_inline.className.split()
    assert getattr(valuation_inline, 'role') == 'status'
    assert 'Aspect: unavailable' in getattr(valuation_inline, 'aria-label')

    trades_inline = index_options.render_trades_dashboard_source_status(
        statuses,
        True,
    )
    assert trades_inline.children[1].children == 'Stale data'
    assert 'greeks-source-status-content' in trades_inline.className.split()
    assert getattr(trades_inline, 'role') == 'status'

    hidden_content, hidden_class = index_options.render_dashboard_source_status(
        statuses,
        '/greeks',
    )
    assert hidden_content is None
    assert 'dashboard-source-status-banner-hidden' in hidden_class.split()

    valuation_content, valuation_class = index_options.render_dashboard_source_status(
        statuses,
        '/valuation',
    )
    assert valuation_content is None
    assert 'dashboard-source-status-banner-hidden' in valuation_class.split()

    trades_content, trades_class = index_options.render_dashboard_source_status(
        statuses,
        '/trades',
    )
    assert trades_content is None
    assert 'dashboard-source-status-banner-hidden' in trades_class.split()

    brent_content, brent_class = index_options.render_dashboard_source_status(
        statuses,
        '/brent_vol_history',
    )
    assert brent_content is None
    assert 'dashboard-source-status-banner-hidden' in brent_class.split()

    brent_inline = index_options.render_brent_vol_history_source_status(
        statuses,
        True,
    )
    assert brent_inline.children[1].children == 'Stale data'
    assert brent_inline.children[2].children[0].children == 'Vol: '
    assert brent_inline.children[2].children[1] == '30 Jul'
    assert brent_inline.children[3].children[0].children == 'Curves: '
    assert 'greeks-source-status-content' in brent_inline.className.split()
    assert getattr(brent_inline, 'role') == 'status'

    shown_content, shown_class = index_options.render_dashboard_source_status(
        statuses,
        '/valuation',
        '?view=pnl-explain',
    )
    assert shown_content.children[0].children == 'Stale market data'
    assert shown_class == 'dashboard-source-status-banner'
