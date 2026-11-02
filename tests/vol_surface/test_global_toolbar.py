from types import SimpleNamespace
import pandas as pd
import pytest
from dash_options.features.vol_surface import selector, overview
from dash_options.shared import surfaces
from tests.vol_surface.test_surface_selector import selected_sources
from tests.vol_surface.test_overview import immutable_snapshot

@pytest.mark.usefixtures('selected_sources')
def test_page_context_applies_to_all_products_and_keeps_unavailable_empty(immutable_snapshot):
    _, ref, _ = immutable_snapshot
    child = selector.select_revision('Brent','draft',None,None,None,cob='2026-10-30')[4]
    context = {'source':'draft','revision':'latest','comparison':'anchor',
               'products':{'BRENT':child, 'TTF':{'error':True}}}
    pairs = overview.surface_pairs(ref,'2026-10-30','2026-10-29',['Brent','TTF'],
                                    'TTF','draft',context,'latest','anchor')
    assert overview.metric_frame(pairs['Brent']['current']).atm.iloc[0] == pytest.approx(54)
    assert pairs['TTF']['current'].empty and pairs['TTF']['error']
    frames = overview.atm_frames(ref,'2026-10-30','2026-10-29',['Brent','TTF'],
                                'draft',context,'latest','anchor')
    current, changes, cols = surfaces._build_atm_table_frames('2026-10-30','2026-10-29',['Brent','TTF'],'monthly',frames)
    assert current.iloc[0][cols[0]] == pytest.approx(.54)
    assert changes.iloc[0][cols[0]] == pytest.approx(.04)
    assert current['product'].tolist() == ['Brent']
    with pytest.raises(selector.SelectionError):
        selector.resolve_view(context,'Brent','draft','other','anchor')

@pytest.mark.usefixtures('selected_sources')
def test_revision_catalog_honors_cob_and_owner(monkeypatch):
    with pytest.raises(selector.SelectionError):
        selector.select_revision('Brent','draft',None,None,None,cob='2026-10-29')
    monkeypatch.setattr(selector,'ctx',SimpleNamespace(triggered_id=None))
    result=selector.update_selector(['Brent'],'draft',None,None,'2026-10-30',None,None,None)
    assert result[4]['products']['BRENT']['revision'] == 'draft1'
    assert result[1]=='latest' and result[3]=='anchor'
    assert selector.resolve_view(result[4],'Brent','draft','latest','anchor')['owner']=='alice'

@pytest.mark.usefixtures('selected_sources')
def test_intraday_vs_chosen_settlement_is_exact_and_immutable(immutable_snapshot):
    store, ref, original = immutable_snapshot
    child = selector.select_revision('Brent','intraday','pub2','anchor',None)[4]
    context = {'source':'intraday','revision':'latest','comparison':'settlement:2026-10-29',
               'products':{'BRENT':child},'settlement_reference':ref}
    # Point both readers at the same test snapshot store, including the revision payload.
    original_resolver = selector.resolve_snapshot
    def resolve(reference, **kwargs):
        return store.resolve(reference, **kwargs) if kwargs.get('expected_namespace') == surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE else original_resolver(reference, **kwargs)
    from unittest.mock import patch
    with patch.object(selector, 'resolve_snapshot', resolve):
        view = selector.resolve_view(context,'Brent','intraday','latest',context['comparison'])
        assert overview.metric_frame(view['current']).atm.iloc[0] == pytest.approx(53)
        assert overview.metric_frame(view['previous']).atm.iloc[0] == pytest.approx(140)
        assert view['metadata']['comparison_label'] == 'Settlement 2026-10-29'
        frames = overview.atm_frames(ref, '2026-10-30', '2026-10-29', ['Brent'], 'intraday', context, 'latest', context['comparison'])
        _, changes, cols = surfaces._build_atm_table_frames('2026-10-30','2026-10-29',['Brent'],'monthly',frames)
        assert changes.iloc[0][cols[0]] == pytest.approx(.53 - 1.4)
        assert view['metadata']['settlement_cob'] == '2026-10-29'
        assert selector.resolve_view(child,'Brent','intraday','pub2','anchor')['previous'].volatility.iloc[0] == .5
        changed = original.copy(); changed['volatility'] = .01
        store.publish(surfaces.VOL_SURFACE_SNAPSHOT_NAMESPACE,'replacement',{'surface_dataset':changed})
        assert overview.metric_frame(selector.resolve_view(context,'Brent','intraday','latest',context['comparison'])['previous']).atm.iloc[0] == pytest.approx(140)
        context['comparison'] = 'settlement:2026-10-01'
        with pytest.raises(selector.SelectionError, match='No settlement surface'):
            selector.resolve_view(context,'Brent','intraday','latest',context['comparison'])
