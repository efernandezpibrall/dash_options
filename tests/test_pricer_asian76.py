import datetime as dt
import json
import math

import numpy as np
import pytest
from dash import dcc, no_update

from options.options_library import asian_76, black_76
from pages import pricer


class FrozenDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 7, 21)


@pytest.fixture(autouse=True)
def freeze_pricer_date(monkeypatch):
    monkeypatch.setattr(pricer, 'date', FrozenDate)


def asian_inputs(call_put='C'):
    params = ['0.03', '100', '0.32', '105']
    param_ids = [
        {'type': 'param', 'model': 'asian76', 'param': 'risk-free-rate'},
        {'type': 'param', 'model': 'asian76', 'param': 'forward-price'},
        {'type': 'param', 'model': 'asian76', 'param': 'volatility'},
        {'type': 'param', 'model': 'asian76', 'param': 'strike-price'},
    ]
    dates = ['2027-04-17', '2026-10-19', '2027-01-17']
    date_ids = [
        {'type': 'param-date', 'model': 'asian76', 'param': 'contract-expiration-date'},
        {'type': 'param-date', 'model': 'asian76', 'param': 'averaging-start-date'},
        {'type': 'param-date', 'model': 'asian76', 'param': 'expiration-date'},
    ]
    return call_put, params, dates, param_ids, date_ids


def parse_with_dates(averaging_start, expiration, contract_expiration):
    _call_put, params, _dates, param_ids, _date_ids = asian_inputs()
    dates = [contract_expiration, averaging_start, expiration]
    date_ids = [
        {'type': 'param-date', 'model': 'asian76', 'param': 'contract-expiration-date'},
        {'type': 'param-date', 'model': 'asian76', 'param': 'averaging-start-date'},
        {'type': 'param-date', 'model': 'asian76', 'param': 'expiration-date'},
    ]
    return pricer._parse_asian76_model_inputs(params, dates, param_ids, date_ids)


def find_component(component, component_id):
    if getattr(component, 'id', None) == component_id:
        return component
    children = getattr(component, 'children', None)
    if children is None:
        return None
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        found = find_component(child, component_id)
        if found is not None:
            return found
    return None


def walk_components(component):
    yield component
    children = getattr(component, 'children', None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if child is not None:
            yield from walk_components(child)


def test_asian_parser_orders_pattern_ids_and_adjusts_volatility_once():
    _call_put, params, dates, param_ids, date_ids = asian_inputs()
    parsed = pricer._parse_asian76_model_inputs(params, dates, param_ids, date_ids)

    assert parsed['F'] == 100
    assert parsed['K'] == 105
    assert parsed['r'] == 0.03
    assert parsed['raw_v'] == 0.32
    assert parsed['averaging_start_date'] == dt.date(2026, 10, 19)
    assert parsed['expiration_date'] == dt.date(2027, 1, 17)
    assert parsed['contract_expiration_date'] == dt.date(2027, 4, 17)
    assert parsed['T_A'] == pytest.approx(90 / 365.25)
    assert parsed['T'] == pytest.approx(180 / 365.25)
    assert parsed['option_business_days'] == 124
    assert parsed['contract_business_days'] == 187
    expected_factor = math.sqrt(124 / 187)
    assert parsed['vol_adjustment_factor'] == pytest.approx(expected_factor)
    assert parsed['v'] == pytest.approx(0.32 * expected_factor)


@pytest.mark.parametrize(
    ('averaging_start', 'expiration', 'contract_expiration', 'message'),
    [
        ('2026-07-20', '2027-01-17', '2027-04-17', 'realized fixings'),
        ('2027-01-18', '2027-01-17', '2027-04-17', 'on or before'),
        (
            '2026-07-21',
            '2026-07-21',
            '2027-04-17',
            'after the valuation date',
        ),
        ('2026-10-19', '2027-01-17', '2027-01-16', 'on or after'),
    ],
)
def test_asian_parser_rejects_unsupported_dates(
    averaging_start, expiration, contract_expiration, message
):
    with pytest.raises(ValueError, match=message):
        parse_with_dates(averaging_start, expiration, contract_expiration)


def test_asian_parser_rejects_adjusted_volatility_below_library_floor():
    _call_put, params, dates, param_ids, date_ids = asian_inputs()
    params[param_ids.index(
        {'type': 'param', 'model': 'asian76', 'param': 'volatility'}
    )] = '0.005'
    with pytest.raises(ValueError, match='below 0.005'):
        pricer._parse_asian76_model_inputs(params, dates, param_ids, date_ids)


def test_asian_date_sync_enforces_ordering_bounds():
    corrected = pricer.sync_asian76_dates(
        '2026-10-19',
        '2026-09-01',
        '2026-09-15',
        'TTF',
        ['MONTH'],
        None,
        '2026-07-21',
    )
    assert corrected == (
        no_update,
        '2026-10-19',
        '2026-10-19',
        None,
        '2026-10-19',
        '2026-10-19',
        '2026-10-19',
        False,
        False,
        False,
    )

    valid = pricer.sync_asian76_dates(
        '2026-10-19',
        '2027-01-17',
        '2027-04-17',
        'TTF',
        ['MONTH'],
        None,
        '2026-07-21',
    )
    assert valid == (
        no_update,
        no_update,
        '2026-10-19',
        None,
        '2027-01-17',
        no_update,
        '2027-01-17',
        False,
        False,
        False,
    )


def test_asian_adapter_preserves_contract_boundaries_and_parity():
    expected = asian_76('C', 100, 105, 0.5, 0.25, 0.03, 0.32)
    actual = pricer._price_single_asset_option('asian76', 'C', 100, 105, 0.5, 0.03, 0.32, 0.25)
    np.testing.assert_allclose(actual, expected)

    with pytest.raises(ValueError):
        pricer._price_single_asset_option('asian76', 'C', 100, 105, 0.5, 0.03, 0.32, -0.01)
    with pytest.raises(ValueError):
        pricer._price_single_asset_option('asian76', 'C', 100, 105, 0.5, 0.03, 0.32, 0.51)

    np.testing.assert_allclose(
        pricer._price_single_asset_option('asian76', 'C', 100, 105, 0.5, 0.03, 0.32, 0.5),
        black_76('C', 100, 105, 0.5, 0.03, 0.32),
    )
    call = asian_76('C', 100, 105, 0.5, 0.25, 0.03, 0.32)[0]
    put = asian_76('P', 100, 105, 0.5, 0.25, 0.03, 0.32)[0]
    assert call - put == pytest.approx(math.exp(-0.03 * 0.5) * (100 - 105))


@pytest.mark.parametrize('call_put', ['C', 'P'])
def test_calculate_asian_matches_library_and_returns_json_snapshot(call_put):
    _unused, params, dates, param_ids, date_ids = asian_inputs(call_put)
    parsed = pricer._parse_asian76_model_inputs(params, dates, param_ids, date_ids)
    output = pricer.calculate_option(1, 'asian76', call_put, params, dates, param_ids, date_ids)

    assert len(output) == 5
    snapshot = output[-1]
    expected = asian_76(
        call_put,
        parsed['F'],
        parsed['K'],
        parsed['T'],
        parsed['T_A'],
        parsed['r'],
        parsed['v'],
    )
    assert snapshot['model'] == 'asian76'
    assert snapshot['value'] == pytest.approx(expected[0])
    json.dumps(snapshot)

    rows = output[1].to_plotly_json()['props']['rowData']
    rows_by_name = {row['greek']: row for row in rows}
    assert rows_by_name['Delta']['__value_raw'] == pytest.approx(expected[1])
    assert rows_by_name['Gamma']['__value_raw'] == pytest.approx(expected[2])
    assert rows_by_name['Vega (Input Vol)']['__value_raw'] == pytest.approx(
        expected[4] * parsed['vol_adjustment_factor']
    )
    assert rows_by_name['Rho']['__value_raw'] == pytest.approx(expected[5])


def test_theta_is_n_a_when_averaging_has_started():
    _call_put, params, _dates, param_ids, _date_ids = asian_inputs()
    dates = ['2027-04-17', '2026-07-21', '2027-01-17']
    date_ids = [
        {'type': 'param-date', 'model': 'asian76', 'param': 'contract-expiration-date'},
        {'type': 'param-date', 'model': 'asian76', 'param': 'averaging-start-date'},
        {'type': 'param-date', 'model': 'asian76', 'param': 'expiration-date'},
    ]
    output = pricer.calculate_option(1, 'asian76', 'C', params, dates, param_ids, date_ids)
    rows = output[1].to_plotly_json()['props']['rowData']
    theta_row = next(row for row in rows if row['greek'] == 'Theta (Pre-Averaging)')
    assert theta_row['value'] is None
    assert theta_row['__value_raw'] is None


def test_store_is_session_scoped_and_replaces_global_cache():
    workspace_store = find_component(pricer.layout, 'pricer-workspace-store')
    calculation_stores = [
        item
        for item in walk_components(pricer.layout)
        if isinstance(item, dcc.Store)
        and isinstance(getattr(item, 'id', None), dict)
        and item.id.get('type') == 'pricer-calculation-store'
    ]
    assert isinstance(workspace_store, dcc.Store)
    assert workspace_store.storage_type == 'session'
    assert len(calculation_stores) == 1
    assert calculation_stores[0].storage_type == 'memory'
    assert calculation_stores[0].id['structure_id'] == pricer.DEFAULT_STRUCTURE_ID
    assert not hasattr(pricer, 'option_cache')


def test_asian_payoff_and_future_valuation_semantics():
    _call_put, params, dates, param_ids, date_ids = asian_inputs()
    snapshot = pricer.calculate_option(1, 'asian76', 'C', params, dates, param_ids, date_ids)[-1]

    payoff = pricer.update_payoff_chart(snapshot, None, 50, 'asian76')
    assert payoff.layout.xaxis.title.text == 'Final Arithmetic Average'
    x_values = np.asarray(payoff.data[0].x, dtype=float)
    expected_payoff = np.maximum(x_values - 105, 0)
    np.testing.assert_allclose(payoff.data[0].y, expected_payoff)
    assert len(payoff.data) == 1

    selected = pricer.update_payoff_chart(snapshot, '2026-08-20', 50, 'asian76')
    marker = next(trace for trace in selected.data if trace.name == 'Selected Valuation')
    expected = asian_76(
        'C', 100, 105, 150 / 365.25, 60 / 365.25, 0.03, snapshot['params']['v']
    )[0]
    assert marker.y[0] == pytest.approx(expected)

    unsupported = pricer.update_payoff_chart(snapshot, '2026-10-20', 50, 'asian76')
    assert 'realized fixings' in unsupported.layout.annotations[0].text.lower()


def test_no_averaging_boundary_selected_at_expiry_shows_intrinsic_payoff():
    _call_put, params, _dates, param_ids, _date_ids = asian_inputs()
    dates = ['2027-04-17', '2026-10-19', '2026-10-19']
    date_ids = [
        {'type': 'param-date', 'model': 'asian76', 'param': 'contract-expiration-date'},
        {'type': 'param-date', 'model': 'asian76', 'param': 'averaging-start-date'},
        {'type': 'param-date', 'model': 'asian76', 'param': 'expiration-date'},
    ]
    snapshot = pricer.calculate_option(1, 'asian76', 'C', params, dates, param_ids, date_ids)[-1]
    payoff = pricer.update_payoff_chart(snapshot, '2026-10-19', 50, 'asian76')

    assert payoff.layout.xaxis.title.text == 'Final Arithmetic Average'
    x_values = np.asarray(payoff.data[0].x, dtype=float)
    np.testing.assert_allclose(payoff.data[0].y, np.maximum(x_values - 105, 0))
    assert len(payoff.data) == 1


def test_asian_sensitivity_charts_use_valid_date_semantics():
    _call_put, params, dates, param_ids, date_ids = asian_inputs()
    parsed = pricer._parse_asian76_model_inputs(params, dates, param_ids, date_ids)

    vol_fig = pricer.update_volatility_chart(1, 'asian76', 'C', params, dates, param_ids, date_ids)
    assert vol_fig.layout.xaxis.title.text == 'Input Contract Volatility (σ)'
    assert vol_fig.data[1].x[0] == pytest.approx(parsed['raw_v'])
    expected_price = asian_76('C', 100, 105, parsed['T'], parsed['T_A'], 0.03, parsed['v'])[0]
    assert vol_fig.data[1].y[0] == pytest.approx(expected_price)

    rate_fig = pricer.update_rate_chart(1, 'asian76', 'C', params, dates, param_ids, date_ids)
    assert rate_fig.data[1].x[0] == pytest.approx(0.03)
    assert rate_fig.data[1].y[0] == pytest.approx(expected_price)

    time_fig = pricer.update_time_chart(1, 'asian76', 'C', params, dates, param_ids, date_ids)
    assert max(time_fig.data[0].x) == '2026-10-19'
    assert len(time_fig.data[0].x) <= 60

    extension_fig = pricer.update_extension_chart(1, 'asian76', 'C', params, dates, param_ids, date_ids)
    extension_dates = [dt.date.fromisoformat(value) for value in extension_fig.data[0].x]
    assert all(dt.date(2026, 10, 19) <= value <= dt.date(2027, 4, 17) for value in extension_dates)
    assert dt.date(2027, 1, 17) in extension_dates
    assert len(extension_dates) <= 41


def test_extension_skips_earlier_dates_below_volatility_floor():
    _call_put, params, dates, param_ids, date_ids = asian_inputs()
    params[param_ids.index(
        {'type': 'param', 'model': 'asian76', 'param': 'volatility'}
    )] = '0.00625'
    parsed = pricer._parse_asian76_model_inputs(params, dates, param_ids, date_ids)
    assert parsed['v'] >= 0.005

    figure = pricer.update_extension_chart(1, 'asian76', 'C', params, dates, param_ids, date_ids)
    assert '2027-01-17' in figure.data[0].x
    assert all(np.isfinite(value) for value in figure.data[0].y)


def test_asian_correlation_chart_is_explicitly_not_applicable():
    _call_put, params, dates, param_ids, date_ids = asian_inputs()
    fig = pricer.update_correlation_chart(1, 'asian76', 'C', params, dates, param_ids, date_ids)
    assert 'only available for Kirk' in fig.layout.annotations[0].text


def test_model_switch_clears_calculation_and_all_sensitivity_charts(monkeypatch):
    _call_put, params, dates, param_ids, date_ids = asian_inputs()
    monkeypatch.setattr(pricer, '_get_pricer_triggered_id', lambda: 'option-type')

    calculation = pricer.calculate_option(1, 'asian76', 'C', params, dates, param_ids, date_ids)
    assert calculation[-1] is None
    for chart_callback in (
        pricer.update_volatility_chart,
        pricer.update_rate_chart,
        pricer.update_time_chart,
        pricer.update_extension_chart,
        pricer.update_correlation_chart,
    ):
        figure = chart_callback(1, 'asian76', 'C', params, dates, param_ids, date_ids)
        assert 'Calculate option price first' in figure.layout.annotations[0].text
