import base64
from io import BytesIO

import pandas as pd
import pytest

from pages import vol_surface
from vol_calibration import ttf_traded_options
from vol_calibration.components.smile_grid import create_smile_grid_figure
from vol_calibration.pages import ttf


COB = '2026-07-30'
EXPIRY = '2026-10-01'


def _raw_gas_options_activity():
    return pd.DataFrame(
        [
            {
                'trade_date': COB,
                'hub': 'TTF (Futures-style)',
                'raw_product': 'Dutch TTF Natural Gas Futures',
                'strip': EXPIRY,
                'contract': 'TFO',
                'contract_type': 'P',
                'strike': 50.0,
                'settlement_price': 3.082,
                'total_volume': 2300,
                'open_interest': 9960,
                'expiration_date': '2026-09-25',
                'option_volatility': 75.0,
                'source_name': None,
                'vendor_published_at': None,
                'ingested_at': None,
            },
            {
                'trade_date': COB,
                'hub': 'TTF (Futures-style)',
                'raw_product': 'Dutch TTF Natural Gas Futures',
                'strip': EXPIRY,
                'contract': 'TFO',
                'contract_type': 'C',
                'strike': 70.0,
                'settlement_price': 4.665,
                'total_volume': 1000,
                'open_interest': 10236,
                'expiration_date': '2026-09-25',
                'option_volatility': 93.0,
                'source_name': None,
                'vendor_published_at': None,
                'ingested_at': None,
            },
        ]
    )


def _traded_payload(raw=None):
    normalized = ttf_traded_options._normalize_ttf_traded_options(
        raw if raw is not None else _raw_gas_options_activity(),
        COB,
    )
    return {
        'product': 'TTF',
        'requested_cob': COB,
        'actual_cob': COB,
        'surface_source': 'ICE',
        'source': ttf_traded_options.TTF_TRADED_OPTIONS_TABLE,
        'row_count': len(normalized),
        'expiry_count': normalized['maturity_date'].nunique(),
        'total_volume': int(normalized['total_volume'].sum()),
        'data': normalized.to_json(date_format='iso', orient='split'),
    }


def _market_rows():
    return pd.DataFrame(
        {
            'expiry': pd.Timestamp(EXPIRY),
            'forward': 58.324,
            'strike': [42.0, 58.0, 78.0],
            'iv': [0.70, 0.84, 1.02],
            'delta': [0.90, 0.50, 0.10],
            'dte': 57.0,
        }
    )


def _displayed_traded_options():
    return ttf_traded_options.ttf_traded_options_frame(
        _traded_payload(),
        _market_rows(),
    )


def _surface_rows():
    raw = pd.DataFrame(
        {
            'cob_date': COB,
            'product': 'TTF',
            'maturity_date': EXPIRY,
            'put_call': 'call',
            'delta': [0.90, 0.50, 0.10],
            'value': [70.0, 84.0, 102.0],
        }
    )
    return vol_surface._normalize_surface_data(raw)


def _surface_metadata():
    return {
        'product': 'TTF',
        'requested_cob': COB,
        'actual_cob': COB,
        'source': vol_surface.SURFACE_POSTGRES_SOURCE_LABEL,
    }


def _params():
    return pd.DataFrame(
        [
            {
                'expiry': pd.Timestamp(EXPIRY),
                'vr': 0.84,
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
        ]
    )


def test_loader_uses_only_raw_exact_cob_positive_volume_rows(monkeypatch):
    raw = pd.concat(
        [
            _raw_gas_options_activity(),
            _raw_gas_options_activity().iloc[[0]].assign(total_volume=0),
            _raw_gas_options_activity().iloc[[0]].assign(
                trade_date='2026-07-29',
                strike=40.0,
            ),
            _raw_gas_options_activity().iloc[[0]].assign(
                contract='OTHER',
                strike=45.0,
            ),
        ],
        ignore_index=True,
    )
    calls = []

    def fake_read_sql(query, engine, params):
        calls.append((str(query), engine, params))
        return raw

    monkeypatch.setattr(ttf_traded_options.pd, 'read_sql', fake_read_sql)
    payload = ttf_traded_options.load_ttf_traded_options_payload(
        COB,
        engine=object(),
    )
    restored = ttf_traded_options.ttf_traded_options_frame(payload)

    assert payload['actual_cob'] == COB
    assert payload['source'] == 'at_lng.gas_options_activity'
    assert payload['row_count'] == 2
    assert payload['expiry_count'] == 1
    assert payload['total_volume'] == 3300
    assert payload['error'] is None
    assert list(restored['volatility']) == pytest.approx([0.75, 0.93])
    assert set(restored['quality_status']) == {'raw_market_activity'}
    assert 'FROM at_lng.gas_options_activity' in calls[0][0]
    assert 'COALESCE(total_volume, 0) > 0' in calls[0][0]
    assert calls[0][2] == {
        'trade_date': pd.Timestamp(COB).date(),
        'hub': 'TTF (Futures-style)',
        'contract': 'TFO',
    }


def test_loader_fails_closed_on_duplicate_raw_contract_row(monkeypatch):
    duplicated = pd.concat(
        [_raw_gas_options_activity(), _raw_gas_options_activity().iloc[[0]]],
        ignore_index=True,
    )
    monkeypatch.setattr(
        ttf_traded_options.pd,
        'read_sql',
        lambda *args, **kwargs: duplicated,
    )

    payload = ttf_traded_options.load_ttf_traded_options_payload(
        COB,
        engine=object(),
    )

    assert payload['actual_cob'] is None
    assert payload['row_count'] == 0
    assert 'Duplicate raw ICE traded-option rows' in payload['error']


def test_chart_coordinates_use_raw_iv_and_selected_cob_forward():
    traded = _displayed_traded_options()

    assert list(traded['volatility']) == pytest.approx([0.75, 0.93])
    assert list(traded['forward_value']) == pytest.approx([58.324, 58.324])
    assert traded['call_delta'].between(0, 1, inclusive='neither').all()
    assert set(traded['method']) == {'gas_options_activity.option_volatility'}


def test_exact_cob_chart_shows_settlement_calibration_and_traded_options():
    figure = create_smile_grid_figure(
        _market_rows(),
        _params(),
        x_axis='delta',
        operational_surface=_surface_rows(),
        operational_metadata=_surface_metadata(),
        traded_options=_displayed_traded_options(),
        traded_options_metadata=_traded_payload(),
    )
    traces = {trace.name: trace for trace in figure.data}

    assert {
        'Settlement vol surface (ICAP)',
        'Calibrated surface (Wing-v2)',
        'ICE traded options (volume > 0)',
    } <= set(traces)
    traded = traces['ICE traded options (volume > 0)']
    assert list(traded.y) == pytest.approx([75.0, 93.0])
    assert traded.marker.symbol == 'circle-open'
    assert traded.marker.size == 6
    assert traded.marker.opacity == pytest.approx(0.85)
    assert 'Volume: %{customdata[3]:,.0f} lots' in traded.hovertemplate


def test_traded_options_use_strike_forward_on_moneyness_axis():
    figure = create_smile_grid_figure(
        _market_rows(),
        pd.DataFrame(),
        x_axis='moneyness',
        operational_surface=_surface_rows(),
        operational_metadata=_surface_metadata(),
        traded_options=_displayed_traded_options(),
        traded_options_metadata=_traded_payload(),
    )
    traded = next(
        trace
        for trace in figure.data
        if trace.name == 'ICE traded options (volume > 0)'
    )

    assert list(traded.x) == pytest.approx(
        sorted([50.0 / 58.324, 70.0 / 58.324])
    )


def test_traded_options_are_hidden_when_cob_provenance_is_not_exact():
    figure = create_smile_grid_figure(
        _market_rows(),
        pd.DataFrame(),
        x_axis='delta',
        traded_options=_displayed_traded_options(),
        traded_options_metadata={
            **_traded_payload(),
            'actual_cob': '2026-07-29',
        },
    )

    assert 'ICE traded options (volume > 0)' not in {
        trace.name for trace in figure.data
    }


def test_ttf_export_includes_raw_traded_option_rows(monkeypatch):
    monkeypatch.setattr(ttf, 'date', type('DateStub', (), {
        'today': staticmethod(lambda: pd.Timestamp('2026-08-05').date())
    }))
    market_json = _market_rows().to_json(date_format='iso', orient='split')
    download = ttf.export_to_excel(
        1,
        [{'expiry': 'Oct-26', 'vr': 0.84, 'rmse': '2.00%'}],
        market_json,
        COB,
        _traded_payload(),
    )
    workbook = BytesIO(base64.b64decode(download['content']))
    excel_file = pd.ExcelFile(workbook)
    exported = pd.read_excel(excel_file, sheet_name='Traded Options')
    summary = pd.read_excel(excel_file, sheet_name='Summary')

    assert excel_file.sheet_names == [
        'Parameters',
        'Market Data',
        'Traded Options',
        'Summary',
    ]
    assert list(exported['strike']) == [50, 70]
    assert list(exported['option_volatility']) == [75, 93]
    assert list(exported['volatility']) == pytest.approx([0.75, 0.93])
    assert summary.loc[0, 'ICE Traded Option Rows'] == 2
    assert summary.loc[0, 'ICE Reported Volume'] == 3300
    assert (
        summary.loc[0, 'ICE Traded Options Source']
        == 'at_lng.gas_options_activity'
    )
