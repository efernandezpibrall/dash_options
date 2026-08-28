from dash import dcc, html

import index_options
from app import app
from pricer_exchange_registry import EXCHANGE_OPTION_MAPPINGS


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


def test_browser_titles_cover_every_route_and_disable_dash_title_overrides():
    assert app.title == 'Options'
    assert app.config.update_title is None
    assert index_options.PAGE_TITLES == {
        '/': 'Greeks',
        '/greeks': 'Greeks',
        '/valuation': 'Valuation',
        '/trades': 'Trades',
        '/prices': 'Underlying Prices',
        '/vol_surface': 'Volatility Surface',
        '/vol_calibration': 'Vol Calibration',
        '/brent_vol_history': 'Vol Trades',
        '/ice_chat_quotes': 'ICE Quotes',
        '/correlations': 'Correlations',
        '/scenarios': 'Scenarios',
        '/pnl_explain': 'P&L Explain',
        '/pricer': 'Pricer',
        '/pricer_old': 'Pricer Old',
    }
    assert index_options.PNL_EXPLAIN_VIEWS == ('pnl-explain', 'pnl_explain')
    assert index_options.PAGE_NOT_FOUND_TITLE == 'Page Not Found'

    app._setup_server()
    nav_callback = app.callback_map['nav-active-sink.children']
    assert nav_callback['inputs'] == [
        {'id': 'url', 'property': 'pathname'},
        {'id': 'url', 'property': 'search'},
    ]


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
        'Pricer Old',
    ]
    assert all(link.children != 'P&L Explain' for link in links)
    assert links[-2].id == 'nav-pricer'
    assert getattr(links[-2], 'href') == '/pricer'
    assert links[-1].id == 'nav-pricer-old'
    assert getattr(links[-1], 'href') == '/pricer_old'

    pricer_group = next(
        item
        for item in _walk(index_options.nav_links)
        if getattr(item, 'className', None) == 'nav-group-pricer'
    )
    assert [link.id for link in pricer_group.children] == [
        'nav-pricer',
        'nav-pricer-old',
    ]


def test_pricer_route_keeps_pricer_old_separate_and_uses_signed_lots():
    pricer_layout = index_options.display_page('/pricer', None)
    pricer_old_layout = index_options.display_page('/pricer_old', None)

    assert pricer_layout is index_options.pages.pricer_new.layout
    assert pricer_old_layout is index_options.pages.pricer.layout
    assert pricer_layout is not pricer_old_layout

    pricer_headings = [
        item.children
        for item in _walk(pricer_layout)
        if isinstance(item, html.H1)
    ]
    old_headings = [
        item.children
        for item in _walk(pricer_old_layout)
        if isinstance(item, html.H1)
    ]
    assert pricer_headings == ['Pricer']
    assert old_headings == ['Pricer Old']
    current_heading = next(
        item for item in _walk(pricer_layout) if isinstance(item, html.H1)
    )
    assert current_heading.className == 'pricer-visually-hidden-title'
    assert 'pricer-page-current' in pricer_layout.className.split()
    assert 'pricer-page-current' not in pricer_old_layout.className.split()

    pricer_ids = [
        item.id
        for item in _walk(pricer_layout)
        if getattr(item, 'id', None) is not None
    ]
    old_ids = [
        item.id
        for item in _walk(pricer_old_layout)
        if getattr(item, 'id', None) is not None
    ]
    assert len(list(map(repr, pricer_ids))) == len(set(map(repr, pricer_ids)))
    assert set(map(repr, old_ids)).issubset(set(map(repr, pricer_ids)))
    assert 'pricer-current-page' in pricer_ids
    assert 'pricer-workflow-mode' not in pricer_ids
    assert 'pricer-workflow-mode' not in old_ids

    current_toolbars = [
        item
        for item in _walk(pricer_layout)
        if 'pricer-workspace-toolbar'
        in str(getattr(item, 'className', '')).split()
    ]
    old_toolbars = [
        item
        for item in _walk(pricer_old_layout)
        if 'pricer-workspace-toolbar'
        in str(getattr(item, 'className', '')).split()
    ]
    assert current_toolbars == []
    assert len(old_toolbars) == 1

    workflow_stores = [
        (item.id['structure_id'], item.data)
        for item in _walk(pricer_layout)
        if isinstance(getattr(item, 'id', None), dict)
        and item.id.get('type') == 'pricer-structure-workflow'
    ]
    assert workflow_stores == [
        ('exchange-structure-1', 'exchange'),
        ('structure-1', 'otc'),
    ]
    old_workflow_stores = [
        item.data
        for item in _walk(pricer_old_layout)
        if isinstance(getattr(item, 'id', None), dict)
        and item.id.get('type') == 'pricer-structure-workflow'
    ]
    assert old_workflow_stores == ['legacy']

    pricer_h2s = [
        item.children
        for item in _walk(pricer_layout)
        if isinstance(item, html.H2)
    ]
    old_h2s = [
        item.children
        for item in _walk(pricer_old_layout)
        if isinstance(item, html.H2)
    ]
    assert pricer_h2s == [
        'Exchange Traded Options',
        'OTC Structured Options',
    ]
    assert 'Contract vols vs volatility surface' in old_h2s
    workflow_sections = [
        item
        for item in pricer_layout.children
        if 'pricer-workflow-section'
        in str(getattr(item, 'className', '')).split()
    ]
    assert [
        item.children[0].children[0].children[0].children
        for item in workflow_sections
    ] == ['Exchange Traded Options', 'OTC Structured Options']
    assert all(
        len(section.children[0].children[0].children) == 1
        for section in workflow_sections
    )
    assert not any(
        'pricer-workflow-section-kicker'
        in str(getattr(item, 'className', '')).split()
        or 'pricer-workflow-section-description'
        in str(getattr(item, 'className', '')).split()
        for item in _walk(pricer_layout)
    )
    workflow_bodies = [section.children[1] for section in workflow_sections]
    assert all(
        'pricer-workflow-section-body'
        in str(getattr(body, 'className', '')).split()
        for body in workflow_bodies
    )
    assert [
        len(
            [
                item
                for item in _walk(body)
                if 'pricer-structure-panel'
                in str(getattr(item, 'className', '')).split()
            ]
        )
        for body in workflow_bodies
    ] == [1, 1]
    assert (
        workflow_bodies[0].children[0].id
        == 'pricer-exchange-structures-container'
    )
    assert workflow_bodies[1].children[0].id == 'pricer-structures-container'
    assert 'pricer-detailed-analysis-section' in str(
        workflow_bodies[1].children[1].className
    ).split()
    assert all(
        any(
            'pricer-structure-panel'
            in str(getattr(item, 'className', '')).split()
            for item in _walk(body)
        )
        for body in workflow_bodies
    )
    assert {
        'pricer-exchange-calculate-all',
        'pricer-exchange-add-structure',
        'pricer-exchange-workspace-status',
    }.issubset(
        {
            component_id
            for component_id in pricer_ids
            if isinstance(component_id, str)
        }
    )
    exchange_workspace_store = next(
        item
        for item in _walk(pricer_layout)
        if getattr(item, 'id', None) == 'pricer-exchange-workspace-store'
    )
    assert exchange_workspace_store.storage_type == 'session'
    assert exchange_workspace_store.data['structures'][0]['label'] == 'E1'
    mapping_selectors = [
        item
        for item in _walk(pricer_layout)
        if isinstance(getattr(item, 'id', None), dict)
        and item.id.get('type') == 'pricer-mapping-id'
        and isinstance(item, dcc.Dropdown)
    ]
    assert len(mapping_selectors) == 1
    assert mapping_selectors[0].value == 'ICE-TTF-TFO'
    assert [option['value'] for option in mapping_selectors[0].options] == [
        mapping.mapping_id for mapping in EXCHANGE_OPTION_MAPPINGS
    ]
    assert len(mapping_selectors[0].options) == 19
    assert not any(
        mapping.mapping_id.startswith('EX-')
        for mapping in EXCHANGE_OPTION_MAPPINGS
    )
    hidden_surface_grid = next(
        item
        for item in _walk(pricer_layout)
        if getattr(item, 'id', None) == 'pricer-surface-comparison-grid'
    )
    assert hidden_surface_grid.style == {'display': 'none'}

    pricer_valuation = next(
        item
        for item in _walk(pricer_layout)
        if isinstance(getattr(item, 'id', None), dict)
        and item.id.get('type') == 'pricer-valuation-date'
    )
    old_valuation = next(
        item
        for item in _walk(pricer_old_layout)
        if isinstance(getattr(item, 'id', None), dict)
        and item.id.get('type') == 'pricer-valuation-date'
    )
    assert pricer_valuation.date == index_options.date.today().isoformat()
    assert pricer_valuation.persistence is False
    assert old_valuation.persistence == 'pricer-structure-1-valuation-date-v1'

    global_valuation = next(
        item
        for item in _walk(app.layout)
        if getattr(item, 'id', None) == 'pricer-global-valuation-date'
    )
    assert isinstance(global_valuation, dcc.DatePickerSingle)
    assert global_valuation.date == index_options.date.today().isoformat()
    assert global_valuation.persistence is False
    today_ticker = next(
        item
        for item in _walk(app.layout)
        if getattr(item, 'id', None) == 'pricer-valuation-today-ticker'
    )
    assert isinstance(today_ticker, dcc.Interval)
    assert today_ticker.interval == 60_000
    visible_class, visible_date = index_options.configure_pricer_global_valuation(
        '/pricer'
    )
    hidden_class, hidden_date = index_options.configure_pricer_global_valuation(
        '/pricer_old'
    )
    assert 'pricer-global-valuation-control-visible' in visible_class.split()
    assert 'pricer-global-valuation-control-hidden' in hidden_class.split()
    assert visible_date == index_options.date.today().isoformat()
    assert hidden_date == index_options.date.today().isoformat()

    pricer_grid = next(
        item
        for item in _walk(pricer_layout)
        if isinstance(getattr(item, 'id', None), dict)
        and item.id.get('type') == 'pricer-legs-grid'
    )
    old_grid = next(
        item
        for item in _walk(pricer_old_layout)
        if isinstance(getattr(item, 'id', None), dict)
        and item.id.get('type') == 'pricer-legs-grid'
    )
    pricer_fields = {
        child.get('field')
        for group in pricer_grid.columnDefs
        for child in (group.get('children') or [group])
    }
    old_fields = {
        child.get('field')
        for group in old_grid.columnDefs
        for child in (group.get('children') or [group])
    }
    pricer_groups = [group["headerName"] for group in pricer_grid.columnDefs]
    old_groups = [group["headerName"] for group in old_grid.columnDefs]
    assert 'side' not in pricer_fields
    assert 'side' in old_fields
    assert 'quote_basis' not in pricer_fields
    assert 'quote_value' not in pricer_fields
    assert 'volatility_asset_1' not in pricer_fields
    assert 'volatility_asset_2' not in pricer_fields
    assert 'quote_basis' in old_fields
    assert 'quote_value' in old_fields
    assert 'Published surface' not in pricer_groups
    assert 'Published surface' in old_groups
    assert pricer_groups[2:] == [
        'Volatility',
        'Volatility adjustment',
        '',
        'Premium',
        'Unit Greeks',
        'Position Greeks',
    ]
    assert 'side' not in pricer_grid.rowData[0]
    assert old_grid.rowData[0]['side'] == 'BUY'
    assert pricer_grid.dashGridOptions['rowHeight'] == 28
    assert pricer_grid.dashGridOptions['headerHeight'] == 30
    assert pricer_grid.dashGridOptions['groupHeaderHeight'] == 24
    assert pricer_grid.dashGridOptions['rowSelection']['checkboxes'] is False
    assert 'selectionColumnDef' not in pricer_grid.dashGridOptions
    pricer_name = next(
        child
        for group in pricer_grid.columnDefs
        for child in (group.get('children') or [group])
        if child.get('field') == 'name'
    )
    assert pricer_name['cellRenderer'] == 'PricerLegSelector'
    assert old_grid.dashGridOptions['rowHeight'] == 30
    assert old_grid.dashGridOptions['headerHeight'] == 34
    assert old_grid.dashGridOptions['groupHeaderHeight'] == 27
    assert old_grid.dashGridOptions['rowSelection']['checkboxes'] is True
    assert old_grid.dashGridOptions['selectionColumnDef']['width'] == 34


def test_current_pricer_workflows_limit_exchange_inputs_and_preserve_otc_models():
    pricer_new = index_options.pages.pricer_new

    assert pricer_new._normalized_workflow('exchange') == 'exchange'
    assert pricer_new._normalized_workflow('otc') == 'otc'
    assert pricer_new._normalized_workflow('legacy') == 'legacy'
    assert pricer_new._normalized_workflow('invalid') == 'exchange'

    assert pricer_new._workflow_model_options('exchange', 'TTF') == [
        {'label': 'ICE TTF option', 'value': 'black76'}
    ]
    assert pricer_new._workflow_model_options('exchange', 'JKM') == [
        {'label': 'JKM average price option', 'value': 'asian76'},
        {'label': 'JKM vanilla option', 'value': 'black76'},
    ]
    assert pricer_new._workflow_model_options('otc', 'TTF') == (
        index_options.pages.pricer.option_types
    )
    assert pricer_new._workflow_model_style('exchange', 'TTF') == {
        'display': 'none'
    }
    assert pricer_new._workflow_model_style('exchange', 'JKM') == {
        'display': 'flex'
    }
    assert pricer_new._workflow_model_style('otc', 'TTF') == {
        'display': 'flex'
    }
    assert pricer_new._workflow_model_style('legacy', 'TTF') == {
        'display': 'flex'
    }
    assert pricer_new._workflow_rate_style('exchange', 'HH') == {
        'display': 'flex'
    }
    assert pricer_new._workflow_rate_style('exchange', 'TTF') == {
        'display': 'none'
    }
    assert pricer_new._workflow_rate_style('otc', 'TTF') == {
        'display': 'flex'
    }
    assert pricer_new.configure_otc_asset_identity_controls('otc', 'kirk') == (
        {'display': 'none'},
        {'display': 'none'},
    )
    assert pricer_new.configure_otc_asset_identity_controls(
        'otc', 'black76'
    ) == ({'display': 'flex'}, {'display': 'flex'})
    assert pricer_new.configure_otc_asset_identity_controls(
        'exchange', 'black76'
    ) == ({'display': 'none'}, {'display': 'flex'})
    assert pricer_new.select_exchange_mapping_asset(
        'exchange', 'CME-HH-ON', 'TTF'
    ) == 'HH'
    assert pricer_new.select_exchange_mapping_asset(
        'otc', 'CME-HH-ON', 'TTF'
    ) is pricer_new.no_update
    assert pricer_new._workflow_model_options(
        'exchange', 'JKM', 'ICE-JKM-JKZ'
    ) == [{'label': 'JKM vanilla option', 'value': 'black76'}]
    assert pricer_new._workflow_model_value(
        'exchange',
        'TTF',
        'kirk',
        {'type': 'pricer-asset'},
        'ICE-TTF-TFO',
    ) == 'black76'
    assert pricer_new._workflow_model_value(
        'exchange',
        'JKM',
        'black76',
        {'type': 'pricer-mapping-id'},
        'ICE-JKM-JKZ',
    ) == 'black76'
    assert pricer_new._workflow_model_value(
        'exchange',
        'JKM',
        'black76',
        {'type': 'pricer-structure-workflow'},
    ) is pricer_new.no_update
    assert pricer_new._workflow_model_value(
        'exchange',
        'JKM',
        'black76',
        {'type': 'pricer-asset'},
    ) == 'asian76'
    assert pricer_new._workflow_model_value(
        'otc',
        'TTF',
        'kirk',
    ) is pricer_new.no_update
    assert pricer_new._workflow_model_value(
        'legacy',
        'TTF',
        'kirk',
    ) is pricer_new.no_update

    exchange_workspace = pricer_new._default_exchange_workspace()
    assert exchange_workspace['schema_version'] == 2
    added_workspace = pricer_new._reduce_exchange_workspace(
        exchange_workspace,
        'add',
    )
    assert [
        structure['structure_id']
        for structure in added_workspace['structures']
    ] == ['exchange-structure-1', 'exchange-structure-2']
    assert [
        structure['label'] for structure in added_workspace['structures']
    ] == ['E1', 'E2']
    duplicate_template = {'model': 'black76', 'legs': [{'leg_id': 'leg-1'}]}
    duplicated_workspace = pricer_new._reduce_exchange_workspace(
        added_workspace,
        'duplicate',
        'exchange-structure-1',
        duplicate_template,
    )
    assert duplicated_workspace['structures'][-1]['label'] == 'E3'
    assert duplicated_workspace['drafts']['exchange-structure-3'] == (
        duplicate_template
    )
    removed_workspace = pricer_new._reduce_exchange_workspace(
        duplicated_workspace,
        'remove',
        'exchange-structure-2',
    )
    assert [
        structure['label'] for structure in removed_workspace['structures']
    ] == ['E1', 'E3']

    legacy_template = {
        'mapping_id': 'ICE-HH-CURRENT',
        'asset': 'TTF',
        'model': 'asian76',
        'contract_multiplier': 1,
        'valuation_date': '2026-08-27',
        'context': {
            'delivery_shape': 'MONTH',
            'delivery_month': '2026-10-01',
            'forward': 3.0,
        },
        'legs': [{'leg_id': 'leg-1'}],
    }
    legacy_workspace = {
        'schema_version': 1,
        'next_structure_sequence': 2,
        'structures': [
            {
                'structure_id': 'exchange-structure-1',
                'label': 'E1',
                'template': legacy_template,
            }
        ],
        'drafts': {'exchange-structure-1': legacy_template},
    }
    migrated_workspace = pricer_new._normalize_exchange_workspace(
        legacy_workspace
    )
    migrated_draft = migrated_workspace['drafts']['exchange-structure-1']
    assert migrated_workspace['schema_version'] == 2
    assert migrated_draft['mapping_id'] == 'ICE-HH-PHE'
    assert migrated_draft['asset'] == 'HH'
    assert migrated_draft['model'] == 'black76'
    assert migrated_draft['context']['premium_convention'] == 'upfront'
    assert migrated_draft['contract_multiplier'] == 2500.0


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

    hidden_content, hidden_class, hidden_row_class = (
        index_options.render_dashboard_source_status(
            statuses,
            '/greeks',
        )
    )
    assert hidden_content is None
    assert 'dashboard-source-status-banner-hidden' in hidden_class.split()
    assert hidden_row_class == 'dashboard-source-status-row'

    valuation_content, valuation_class, _valuation_row_class = (
        index_options.render_dashboard_source_status(
            statuses,
            '/valuation',
        )
    )
    assert valuation_content is None
    assert 'dashboard-source-status-banner-hidden' in valuation_class.split()

    trades_content, trades_class, _trades_row_class = (
        index_options.render_dashboard_source_status(
            statuses,
            '/trades',
        )
    )
    assert trades_content is None
    assert 'dashboard-source-status-banner-hidden' in trades_class.split()

    brent_content, brent_class, _brent_row_class = (
        index_options.render_dashboard_source_status(
            statuses,
            '/brent_vol_history',
        )
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

    shown_content, shown_class, shown_row_class = (
        index_options.render_dashboard_source_status(
            statuses,
            '/valuation',
            '?view=pnl-explain',
        )
    )
    assert shown_content.children[0].children == 'Stale market data'
    assert shown_class == 'dashboard-source-status-banner'
    assert shown_row_class == 'dashboard-source-status-row'

    pricer_content, pricer_class, pricer_row_class = (
        index_options.render_dashboard_source_status(
            statuses,
            '/pricer',
            valuation_date=index_options.date.today().isoformat(),
        )
    )
    assert pricer_content.children[0].children == 'Stale market data'
    assert pricer_class == 'dashboard-source-status-banner'
    assert 'dashboard-source-status-row-pricer' in pricer_row_class.split()
    assert (
        'dashboard-source-status-row-pricer-valuation-warning'
        not in pricer_row_class.split()
    )

    historical_date = index_options.date.fromordinal(
        index_options.date.today().toordinal() - 1
    ).isoformat()
    warning_content, warning_class, warning_row_class = (
        index_options.render_dashboard_source_status(
            statuses,
            '/pricer',
            valuation_date=historical_date,
        )
    )
    assert warning_content.children[0].children == '!'
    assert (
        warning_content.children[1].children
        == 'WARNING — VALUATION DATE IS NOT TODAY'
    )
    assert warning_content.children[2].children == (
        f'Selected: {historical_date} · '
        f'Today: {index_options.date.today().isoformat()}'
    )
    assert warning_content.children[3].children == 'Stale market data'
    assert 'dashboard-source-status-valuation-warning' in warning_content.className.split()
    assert warning_content.role == 'alert'
    assert getattr(warning_content, 'aria-live') == 'assertive'
    assert getattr(warning_content, 'aria-atomic') == 'true'
    assert 'Vol Surface: 2026-07-30' in getattr(warning_content, 'aria-label')
    assert warning_class == 'dashboard-source-status-banner'
    assert (
        'dashboard-source-status-row-pricer-valuation-warning'
        in warning_row_class.split()
    )

    empty_warning, _, _ = index_options.render_dashboard_source_status(
        None,
        '/pricer',
        valuation_date=None,
    )
    assert empty_warning.children[2].children.startswith('Selected: not selected')

    _, hidden_mismatch_class, hidden_mismatch_row = (
        index_options.render_dashboard_source_status(
            statuses,
            '/greeks',
            valuation_date=historical_date,
        )
    )
    assert 'dashboard-source-status-banner-hidden' in hidden_mismatch_class.split()
    assert 'valuation-warning' not in hidden_mismatch_row
