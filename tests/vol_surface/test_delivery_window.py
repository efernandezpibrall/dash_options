"""Page-wide horizon, strip completeness, contract safety and export parity."""
import base64
import io
from types import SimpleNamespace

import pandas as pd
import pytest

from dash_options.features.vol_surface import overview, page
from tests.vol_surface.test_overview import nodes


def scope(start='2027-02-01', count=12):
    return {'months': pd.date_range(start, periods=count, freq='MS').strftime('%Y-%m-%d').tolist()}


@pytest.mark.parametrize('length,expected', [(12,12),(24,24),('all',36)])
def test_horizon_uses_current_fronts_and_aligns_comparison(length, expected):
    current = nodes(months=pd.date_range('2027-01-01', periods=36, freq='MS'))
    previous = nodes(date='2026-10-28', months=pd.date_range('2026-12-01', periods=40, freq='MS'))
    months = overview.delivery_months({'TTF': {'current':current, 'previous':previous}}, length)
    context = {'months': months.strftime('%Y-%m-%d').tolist()}
    assert len(months) == expected
    assert months[0] == pd.Timestamp('2027-01-01')
    for frame in (current, previous):
        filtered = overview.filter_delivery(frame, context)
        assert filtered.contract_date.min() == months[0]
        assert filtered.contract_date.max() == months[-1]
        pd.testing.assert_frame_equal(filtered, frame.loc[frame.contract_date.isin(months)])


@pytest.mark.parametrize('grouping,expected', [('quarterly',9),('season',6),('calendar',0)])
def test_boundary_buckets_never_include_outside_months_or_partial_strips(grouping, expected):
    months = pd.date_range('2027-02-01', periods=12, freq='MS')
    frame = pd.DataFrame({'code':'TTF','cob_date':pd.Timestamp('2026-10-30'),
                          'contract_date':months,'volatility':.6})
    result = overview.complete_bucket_frames(frame, scope(), grouping)
    assert len(result) == expected
    if grouping == 'quarterly':
        frame.loc[frame.contract_date.eq(pd.Timestamp('2027-05-01')), 'volatility'] = float('nan')
        result = overview.complete_bucket_frames(frame, scope(), grouping)
        assert not result.contract_date.dt.month.isin([4,5,6]).any()


def test_shrinking_window_clears_contract_instead_of_substitution(monkeypatch):
    frame = nodes(months=('2027-01-01','2027-02-01'))
    monkeypatch.setattr(page.surface_data, '_ensure_cached_data', lambda *_: None)
    monkeypatch.setattr(page, '_selected_surface_pair', lambda *a: (frame,frame,None))
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(triggered_id='vol-overview-context', triggered_prop_ids={}))
    result = page.update_surface_expiry_dropdown(None,'2026-10-29','TTF',None,'settlement',
        'settlement','previous',None,scope(count=1),'month:2027-01-01')
    assert result[1] is None
    assert [o['value'] for o in result[0]] == ['month:2027-02-01']


def test_history_limits_contracts_without_truncating_historical_frame(monkeypatch):
    frame = nodes()
    seen = []
    monkeypatch.setattr(overview,'history_frame',lambda _: frame)
    def contracts(product,end,lookback,history):
        seen.append((lookback,history))
        return ['2027-01-01','2027-02-01','2027-03-01']
    monkeypatch.setattr(page.delta_history_data,'available_contracts',contracts)
    result = page.select_history_contracts({'id':'test'},'2026-10-29','TTF',365,
        '2027-01-01','2027-03-01',{'product':'TTF'},None,scope(count=1))
    assert [o['value'] for o in result[1]] == ['2027-02-01']
    assert result[2] is None and result[4] is None
    assert seen[0][0] == 365 and seen[0][1] is frame


def test_export_keeps_both_tables_same_scope_and_saved_node_differences(monkeypatch):
    months = pd.date_range('2027-01-01', periods=3, freq='MS')
    frame = pd.concat([pd.DataFrame({'code':'TTF','cob_date':pd.Timestamp(date),
        'contract_date':months,'volatility':vol}) for date,vol in [('2026-10-29',.6),('2026-10-28',.58)]])
    monkeypatch.setattr(page.surface_data,'_ensure_cached_data',lambda *_: None)
    monkeypatch.setattr(overview,'atm_frames',lambda *a: frame)
    monkeypatch.setattr(overview,'surface_pairs',lambda *a: {})
    result = page.export_volatility_table(1,'2026-10-29',['TTF'],'monthly',
        delivery_context=scope(count=1),previous_date='2026-10-28')
    sheets = pd.read_excel(io.BytesIO(base64.b64decode(result['content'])),sheet_name=None)
    assert sheets['Volatility Data'].columns.tolist() == ['product','02-27']
    assert sheets['Change vs comparison'].columns.tolist() == ['product','02-27']
    assert sheets['Volatility Data']['02-27'].iloc[0] == pytest.approx(.6)
    assert sheets['Change vs comparison']['02-27'].iloc[0] == pytest.approx(.02)


def test_display_missing_zero_and_signed_units():
    assert page._format_vol_table_sample(None) == '—'
    assert page._format_vol_table_sample(-.000001, signed=True) == '0.00'
    assert page._format_vol_table_sample(.0123, signed=True) == '+1.23'


from tests.vol_surface.test_surface_selector import selected_sources  # noqa: E402,F401
from dash_options.features.vol_surface import selector  # noqa: E402


@pytest.mark.parametrize('source,revision,expected', [('intraday','pub2',.53),('draft','draft1',.54)])
def test_window_preserves_saved_revision_comparison_and_inherited_nodes(selected_sources,source,revision,expected):
    selection = selector.select_revision('Brent',source,revision,'anchor',None)[4]
    view = selector.resolve_view(selection,'Brent',source,revision,'anchor')
    current = overview.filter_delivery(view['current'],scope('2027-01-01',1))
    previous = overview.filter_delivery(view['previous'],scope('2027-01-01',1))
    assert current.loc[current.delta_bucket.eq('ATM'),'volatility'].tolist() == [expected]
    assert previous.loc[previous.delta_bucket.eq('ATM'),'volatility'].tolist() == [.5]
    inherited = overview.filter_delivery(view['current'],scope('2027-02-01',1))
    assert inherited.loc[inherited.delta_bucket.eq('ATM'),'volatility'].tolist() == [.6]
    assert view['metadata']['month_origins']['2027-02'] == 'Inherited 2026-10-29'
