from io import StringIO

import numpy as np
import pandas as pd
import pytest

from pages import vol_surface
from vol_calibration.components.smile_grid import (
    create_smile_grid_figure,
    delta_curve_to_strike_iv,
    delta_to_strike_iv,
)
from vol_calibration.operational_surface import (
    load_operational_surface_payload,
    operational_surface_frame,
    operational_surface_status_text,
)
from vol_calibration.pages import hh, jkm


def _surface_rows(product, cob_date, expiry, deltas=(0.90, 0.50, 0.10)):
    return pd.DataFrame(
        {
            'cob_date': cob_date,
            'product': product,
            'maturity_date': expiry,
            'put_call': 'call',
            'delta': list(deltas),
            'value': [30.0, 25.0, 28.0],
        }
    )


def _normalized_surface():
    raw = pd.concat(
        [
            _surface_rows('BRENT', '2026-07-08', '2026-09-01'),
            _surface_rows('BRENT', '2026-07-27', '2026-09-01'),
            _surface_rows('BRENT', '2026-07-30', '2026-09-01'),
            _surface_rows('HH', '2026-07-06', '2026-09-01'),
            _surface_rows('NBP', '2026-07-27', '2026-09-01'),
            _surface_rows('TTF', '2026-07-27', '2026-09-01'),
        ],
        ignore_index=True,
    )
    return vol_surface._normalize_surface_data(raw)


@pytest.fixture
def governed_surface_cache(monkeypatch):
    normalized = _normalized_surface()
    monkeypatch.setattr(vol_surface, 'surface_dataset', normalized)
    monkeypatch.setattr(vol_surface, '_ensure_cached_data', lambda *args, **kwargs: None)
    monkeypatch.setitem(vol_surface.DATA_CACHE_STATE, 'initialized', True)
    monkeypatch.setitem(
        vol_surface.DATA_CACHE_STATE,
        'surface',
        {
            'source': vol_surface.SURFACE_POSTGRES_SOURCE_LABEL,
            'error': None,
            'rows': len(normalized),
            'latest_cob_date': pd.Timestamp('2026-07-30'),
            'fallback_used': True,
        },
    )
    vol_surface._SURFACE_SNAPSHOT_CACHE.clear()
    return normalized


def test_operational_snapshot_resolves_exact_and_normalizes_brent_name(
    governed_surface_cache,
):
    snapshot = vol_surface.get_operational_surface_snapshot(
        'BRENT',
        '2026-07-27',
    )

    assert snapshot['product'] == 'Brent'
    assert snapshot['requested_cob'] == pd.Timestamp('2026-07-27')
    assert snapshot['actual_cob'] == pd.Timestamp('2026-07-27')
    assert snapshot['date_fallback_used'] is False
    assert snapshot['source_fallback_used'] is True
    assert set(snapshot['data']['code']) == {'Brent'}


def test_operational_snapshot_uses_product_scoped_prior_and_never_future(
    governed_surface_cache,
):
    brent = vol_surface.get_operational_surface_snapshot(
        'BRENT',
        '2026-07-26',
    )
    hh = vol_surface.get_operational_surface_snapshot('HH', '2026-07-26')

    assert brent['actual_cob'] == pd.Timestamp('2026-07-08')
    assert hh['actual_cob'] == pd.Timestamp('2026-07-06')
    assert brent['date_fallback_used'] is True
    assert brent['actual_cob'] < brent['requested_cob']
    assert pd.Timestamp('2026-07-30') not in set(brent['data']['cob_date'])


def test_operational_snapshot_supports_nbp_without_a_calibration_route(
    governed_surface_cache,
):
    snapshot = vol_surface.get_operational_surface_snapshot(
        'NBP',
        '2026-07-30',
    )

    assert snapshot['product'] == 'NBP'
    assert snapshot['actual_cob'] == pd.Timestamp('2026-07-27')
    assert snapshot['date_fallback_used'] is True
    assert set(snapshot['data']['code']) == {'NBP'}
    href, style = vol_surface.build_calibration_link(
        'NBP',
        '2026-07-30',
        '2026-09-01',
    )
    assert href == '/vol_calibration?product=ttf'
    assert style == {'display': 'none'}


def test_operational_snapshot_reports_no_prior_instead_of_using_future(
    governed_surface_cache,
):
    snapshot = vol_surface.get_operational_surface_snapshot(
        'BRENT',
        '2026-07-01',
    )

    assert snapshot['data'].empty
    assert snapshot['actual_cob'] is None
    assert snapshot['date_fallback_used'] is False
    assert 'on or before 2026-07-01' in snapshot['error']


def test_legacy_trino_surface_is_reported_as_a_source_fallback(monkeypatch):
    raw = _surface_rows('TTF', '2026-07-27', '2026-09-01')

    def fake_trino(query, *, catalog, schema):
        assert (catalog, schema) == ('raw', 'icap')
        if 'implied_volatility_surface_from_prices' in query:
            raise RuntimeError('synchronized source unavailable')
        return raw

    monkeypatch.setattr(vol_surface, 'read_trino_query', fake_trino)
    surface, metadata = vol_surface.load_surface_data()

    assert len(surface) == 3
    assert metadata['source'] == 'raw.icap.implied_volatility_surface'
    assert metadata['fallback_used'] is True


def test_payload_preserves_date_and_source_fallback_metadata(
    governed_surface_cache,
):
    payload = load_operational_surface_payload('BRENT', '2026-07-26')
    restored = operational_surface_frame(payload)
    status, color = operational_surface_status_text(payload)

    assert payload['requested_cob'] == '2026-07-26'
    assert payload['actual_cob'] == '2026-07-08'
    assert payload['date_fallback_used'] is True
    assert payload['source_fallback_used'] is True
    assert len(restored) == 3
    assert 'Requested COB 26-Jul-2026' in status
    assert 'Surface COB 08-Jul-2026' in status
    assert 'Date fallback' in status
    assert 'Source fallback' in status
    assert color == 'warning'


def _chart_surface(expiries=('2026-09-01', '2026-10-01')):
    return vol_surface._normalize_surface_data(
        pd.concat(
            [
                _surface_rows('TTF', '2026-07-27', expiry)
                for expiry in expiries
            ],
            ignore_index=True,
        )
    )


def _market_rows(expiry='2026-09-01'):
    return pd.DataFrame(
        {
            'expiry': pd.Timestamp(expiry),
            'forward': 50.0,
            'strike': np.nan,
            'iv': [0.30, 0.25, 0.28],
            'delta': [0.90, 0.50, 0.10],
            'dte': 60.0,
        }
    )


def _metadata(product='TTF', requested='2026-07-27', actual='2026-07-27'):
    return {
        'product': product,
        'requested_cob': requested,
        'actual_cob': actual,
        'source': vol_surface.SURFACE_POSTGRES_SOURCE_LABEL,
    }


def test_delta_overlay_uses_maturity_union_and_governed_call_delta_coordinates():
    figure = create_smile_grid_figure(
        pd.DataFrame(),
        pd.DataFrame(),
        x_axis='delta',
        operational_surface=_chart_surface(),
        operational_metadata=_metadata(),
    )

    reference_traces = [
        trace
        for trace in figure.data
        if trace.name == 'Settlement vol surface (ICAP)'
    ]
    assert len(reference_traces) == 2
    assert list(reference_traces[0].x) == pytest.approx([0.10, 0.50, 0.90])
    assert {annotation.text for annotation in figure.layout.annotations} == {
        'Sep-26',
        'Oct-26',
    }


def test_ttf_exact_identical_points_render_one_combined_trace():
    figure = create_smile_grid_figure(
        _market_rows(),
        pd.DataFrame(),
        x_axis='delta',
        operational_surface=_chart_surface(('2026-09-01',)),
        operational_metadata=_metadata(),
    )
    names = [trace.name for trace in figure.data]

    assert names == ['Settlement vol surface (ICAP)']
    assert figure.data[0].marker.symbol == 'diamond'
    assert figure.data[0].line.dash == 'dash'


def test_non_delta_axis_hides_reference_and_does_not_add_reference_only_panels():
    market = _market_rows().assign(strike=[45.0, 50.0, 55.0])
    figure = create_smile_grid_figure(
        market,
        pd.DataFrame(),
        x_axis='moneyness',
        operational_surface=_chart_surface(),
        operational_metadata=_metadata(),
    )
    names = [trace.name for trace in figure.data]
    status, color = operational_surface_status_text(
        {
            **_metadata(),
            'row_count': 6,
        },
        x_axis='moneyness',
    )

    assert names == ['Settlement vol surface (ICAP)']
    assert [annotation.text for annotation in figure.layout.annotations] == [
        'Sep-26'
    ]
    assert 'Select Delta' in status
    assert color == 'info'


def test_vectorized_delta_curve_matches_scalar_bracketed_solver():
    from options.calibration_engine.models.wing_model import wing_model_iv

    params = {
        'vr': 0.25,
        'sr': 0.0,
        'pc': 0.1,
        'cc': 0.1,
        'dc': -0.2,
        'uc': 0.2,
        'dsm': 0.1,
        'usm': 0.1,
        'vcr': 0.0,
        'scr': 0.0,
        'ssr': 1.0,
        'put_wing_power': 0.5,
        'call_wing_power': 0.5,
    }
    targets = np.array([0.10, 0.25, 0.40])
    vector_targets, vector_strikes, vector_ivs = delta_curve_to_strike_iv(
        targets,
        100.0,
        60.0,
        params,
        wing_model_iv,
        is_put=True,
    )
    scalar = [
        delta_to_strike_iv(
            target,
            100.0,
            60.0,
            params,
            wing_model_iv,
            is_put=True,
        )
        for target in targets
    ]

    assert vector_targets == pytest.approx(targets)
    assert vector_strikes == pytest.approx([value[0] for value in scalar])
    assert vector_ivs == pytest.approx([value[1] for value in scalar])


@pytest.mark.parametrize('module', (hh, jkm))
def test_unavailable_hh_and_jkm_inputs_disable_actions_without_synthetic_rows(
    monkeypatch,
    module,
):
    calls = []

    def unavailable(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            'data': pd.DataFrame(),
            'source': 'unavailable',
            'is_synthetic': False,
            'last_update': None,
            'message': f'No eligible {module.COMMODITY} market data',
            'error': 'No exact-COB inputs',
        }

    monkeypatch.setattr(module, 'load_market_data_with_metadata', unavailable)
    result = module.load_data('2026-07-26', 0)
    market = pd.read_json(StringIO(result[0]), orient='split')

    assert calls[0][1]['allow_synthetic_fallback'] is False
    assert market.empty
    assert result[4] is True
    assert result[6] is True
