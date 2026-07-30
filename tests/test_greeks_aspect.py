import base64
import datetime as dt
import io

import pandas as pd
import pytest
from openpyxl import load_workbook

from pages import greeks
from runtime_config import clear_runtime_config_cache


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    clear_runtime_config_cache()
    yield
    clear_runtime_config_cache()


def _aspect_frame():
    return pd.DataFrame([
        {
            'instrument': 'ICE_TTF',
            'qty': '10000',
            'uoM': 'MMBtu',
            'maturityForwardDate': '2026-09-01',
            'strategy': 'LNG',
            'dealType': 'Future',
            'entityType': 'Physical',
            'exchangeTradeOtc': 'Exchange',
        },
        {
            'instrument': 'ICE_BRENT_FUTURES',
            'qty': '-2000',
            'uoM': 'BBL',
            'maturityForwardDate': '2026-10-01',
            'strategy': 'LNG',
            'dealType': 'Future',
            'entityType': 'Physical',
            'exchangeTradeOtc': 'Exchange',
        },
    ])


def _aspect_meta():
    return {
        'status': 'ok',
        'message': 'Aspect exposure loaded',
        'source': greeks.ASPECT_SOURCE_LABEL,
        'mode': 'settlement',
        'requested_cob_date': '2026-07-28',
        'fetched_at': '2026-07-28T10:00:00+04:00',
        'source_rows': 2,
        'accepted_rows': 2,
        'rejected_rows': 0,
        'instruments': 2,
        'strategies': 1,
        'book': 'AD-LNG',
    }


def _walk(component):
    yield component
    children = getattr(component, 'children', None)
    if isinstance(children, (list, tuple)):
        for child in children:
            yield from _walk(child)
    elif children is not None:
        yield from _walk(children)


def test_layout_omits_current_greeks_summary_section():
    components = list(_walk(greeks.layout))
    assert not any(
        getattr(component, 'id', None) == 'greeks-summary-table-container'
        for component in components
    )
    assert not any(
        getattr(component, 'children', None) == 'Current Greeks Summary'
        for component in components
    )
    assert len(greeks.update_monitor_tables(greeks._empty_store())) == 3


def test_option_assets_use_contract_native_units(monkeypatch):
    monkeypatch.setattr(
        greeks,
        'fetch_instrument_unit_map',
        lambda: {
            'ICE_TTF': {
                'native_unit': 'MWh',
                'unit': 'MMBtu',
                'conv_factor': 3.41214245,
            },
        },
    )
    data = pd.DataFrame([
        {
            'cob_date': '2026-07-27',
            'unit_quantity': 'MWh',
            'asset_a': 'ICE_TTF',
            'asset_b': None,
            'maturity_date_a': '2026-10-01',
            'maturity_date_type_a': 'month',
            'quantity': 74500,
            'qty_delta_asset_a': 100.0,
            'qty_gamma_asset_a': 10.0,
            'qty_vega_sigma1': 20.0,
            'qty_theta': 5.0,
            'qty_corr_sensitivity': 0.0,
        },
    ])

    normalized = greeks.normalize_greek_contributions(
        data,
        aggregation='month',
        unit_mode='native',
        cob_date='2026-07-27',
    )
    asset_rows = normalized[normalized['bucket_type'] == 'Instrument']

    assert set(asset_rows['unit']) == {'MWh'}
    assert asset_rows.loc[asset_rows['greek'] == 'delta', 'exposure'].iloc[0] == 100.0
    assert asset_rows.loc[asset_rows['greek'] == 'theta', 'exposure'].iloc[0] == 5.0
    assert normalized.loc[normalized['bucket_type'] == 'Pair'].empty

    bucket_tables = greeks.create_bucket_greek_tables(normalized)
    titles = [table['title'] for table in bucket_tables]
    assert 'ASSET: TTF (MWh)' in titles
    assert 'PAIR: TTF (MWh)' not in titles
    ttf_table = next(table for table in bucket_tables if table['title'] == 'ASSET: TTF (MWh)')
    assert list(ttf_table['table'].columns) == [
        'Maturity',
        'Delta',
        'Gamma',
        'Vega',
        'Theta',
        '_row_type',
    ]
    exported = greeks.create_bucket_greek_export_df(bucket_tables)
    assert set(exported['Bucket Type']) == {'Instrument'}
    assert set(exported['Risk Bucket']) == {'TTF'}
    assert exported.loc[exported['Maturity'] == 'Total', 'Theta'].iloc[0] == 5.0


def test_spread_theta_and_correlation_remain_pair_risks(monkeypatch):
    monkeypatch.setattr(
        greeks,
        'fetch_instrument_unit_map',
        lambda: {
            'ICE_HH': {
                'native_unit': 'MMBtu',
                'unit': 'MMBtu',
                'conv_factor': 1.0,
            },
            'ICE_JKM': {
                'native_unit': 'MMBtu',
                'unit': 'MMBtu',
                'conv_factor': 1.0,
            },
        },
    )
    data = pd.DataFrame([
        {
            'cob_date': '2026-07-27',
            'unit_quantity': 'MMBtu',
            'asset_a': 'ICE_HH',
            'asset_b': 'ICE_JKM',
            'maturity_date_a': '2027-02-01',
            'maturity_date_type_a': 'month',
            'maturity_date_b': '2027-02-01',
            'maturity_date_type_b': 'month',
            'quantity': 10000,
            'qty_delta_asset_a': 100.0,
            'qty_delta_asset_b': -80.0,
            'qty_gamma_asset_a': 10.0,
            'qty_gamma_asset_b': 8.0,
            'qty_vega_sigma1': 20.0,
            'qty_vega_sigma2': 15.0,
            'qty_theta': -12.0,
            'qty_corr_sensitivity': 4.0,
        },
    ])

    normalized = greeks.normalize_greek_contributions(
        data,
        aggregation='month',
        unit_mode='native',
        cob_date='2026-07-27',
    )
    pair_rows = normalized[normalized['bucket_type'] == 'Pair']
    assert set(pair_rows['greek']) == {'theta', 'correlation'}
    assert set(pair_rows['risk_bucket']) == {'ICE_HH / ICE_JKM'}

    bucket_tables = greeks.create_bucket_greek_tables(normalized)
    pair_table = next(table for table in bucket_tables if table['bucket_type'] == 'Pair')
    assert list(pair_table['table'].columns) == [
        'Maturity',
        'Theta',
        'Correlation',
        '_row_type',
    ]
    for asset_table in (
        table for table in bucket_tables if table['bucket_type'] == 'Instrument'
    ):
        assert 'Theta' not in asset_table['table'].columns


def test_filter_values_only_labels_two_underlying_rows_as_pairs(monkeypatch):
    data = pd.DataFrame([
        {
            'substrategy': 'TTF call spread',
            'type_trade': 'Financial Option',
            'asset_a': 'ICE_TTF',
            'asset_b': None,
        },
        {
            'substrategy': 'Brent put fly',
            'type_trade': 'Financial Option',
            'asset_a': 'ICE_BRENT_FUTURES',
            'asset_b': None,
        },
        {
            'substrategy': 'JKM-HH spread',
            'type_trade': 'Physical Option',
            'asset_a': 'ICE_HH',
            'asset_b': 'ICE_JKM',
        },
    ])
    monkeypatch.setattr(greeks, 'fetch_options_data', lambda _cob_date: data)

    _, _, bucket_options = greeks.fetch_filter_values('2026-07-27')
    values = {option['value'] for option in bucket_options}

    assert 'pair::ICE_TTF' not in values
    assert 'pair::ICE_BRENT_FUTURES' not in values
    assert 'pair::ICE_HH / ICE_JKM' in values


@pytest.mark.parametrize(
    ('maturity_type_a', 'maturity_type_b'),
    [
        ('month', 'month'),
        ('calendar', 'calendar'),
        ('calendar', 'month'),
    ],
)
def test_mixed_annual_bucket_combines_all_delivery_structures(
    maturity_type_a,
    maturity_type_b,
):
    row = {
        'maturity_date_a': '2029-01-01',
        'maturity_date_type_a': maturity_type_a,
        'maturity_date_b': '2029-01-01',
        'maturity_date_type_b': maturity_type_b,
    }

    assert greeks._format_maturity_pair(
        row,
        aggregation='mixed',
        month_through='2027-03-31',
        quarter_through='2027-12-31',
        cob_date='2026-07-27',
    ) == '2029'


def test_calendar_marker_is_preserved_outside_annual_aggregation():
    row = {
        'maturity_date_a': '2029-01-01',
        'maturity_date_type_a': 'calendar',
        'maturity_date_b': '2029-01-01',
        'maturity_date_type_b': 'month',
    }

    assert greeks._format_maturity_pair(
        row,
        aggregation='quarter',
        cob_date='2026-07-27',
    ) == '2029-CAL / 2029-Q1'


def test_aspect_request_uses_json_auth_timeout_and_ssl_verification(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / 'empty.ini'
    config_path.write_text('')
    monkeypatch.setenv('OPTIONS_CONFIG_PATH', str(config_path))
    monkeypatch.setenv('ASPECT_USERNAME', 'user')
    monkeypatch.setenv('ASPECT_PASSWORD', 'password')
    monkeypatch.setenv('ASPECT_REQUEST_TIMEOUT_SECONDS', '45')
    monkeypatch.delenv('ASPECT_VERIFY_SSL', raising=False)
    clear_runtime_config_cache()

    captured = {}

    class _Response:
        status_code = 200
        ok = True

        @staticmethod
        def json():
            return _aspect_frame().to_dict('records')

    def fake_post(url, **kwargs):
        captured['url'] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(greeks.requests, 'post', fake_post)
    monkeypatch.setattr(greeks, '_dubai_today', lambda: dt.date(2026, 7, 28))

    result = greeks.fetch_aspect_exposure_report(dt.date(2026, 7, 27))

    assert len(result) == 2
    assert captured['url'] == greeks.ASPECT_DEFAULT_URL
    assert captured['json'] == {
        'cobDate': '2026-07-27',
        'todayPricing': 1,
        'book': 'AD-LNG',
        'displayExchangeTradeOtc': True,
    }
    assert captured['auth'] == ('user', 'password')
    assert captured['timeout'] == (10.0, 45.0)
    assert captured['verify'] is True
    assert 'proxies' not in captured


def test_missing_aspect_credentials_fails_closed(monkeypatch, tmp_path):
    config_path = tmp_path / 'empty.ini'
    config_path.write_text('')
    monkeypatch.setenv('OPTIONS_CONFIG_PATH', str(config_path))
    monkeypatch.delenv('ASPECT_USERNAME', raising=False)
    monkeypatch.delenv('ASPECT_PASSWORD', raising=False)
    clear_runtime_config_cache()

    with pytest.raises(greeks.AspectConfigurationError, match='credentials are unavailable'):
        greeks.fetch_aspect_exposure_report(dt.date(2026, 7, 28))


def test_prepare_aspect_records_rejects_invalid_rows():
    data = _aspect_frame()
    invalid = data.iloc[[0]].copy()
    invalid['instrument'] = ''
    invalid['qty'] = 'not-a-number'
    invalid['maturityForwardDate'] = 'not-a-date'
    data = pd.concat([data, invalid], ignore_index=True)

    prepared, rejected_rows = greeks._prepare_aspect_records(data)

    assert len(prepared) == 2
    assert rejected_rows == 1
    assert prepared['qty'].tolist() == [10000.0, -2000.0]
    assert prepared['maturityForwardDate'].tolist() == ['2026-09-01', '2026-10-01']


def test_aspect_normalization_applies_mapping_and_lot_conventions(monkeypatch):
    prepared, _ = greeks._prepare_aspect_records(_aspect_frame())
    monkeypatch.setattr(
        greeks,
        'fetch_instrument_unit_map',
        lambda: {
            'ICE_TTF': {'unit': 'MMBtu', 'conv_factor': 1.0},
            'ICE_BRENT_FUTURES': {'unit': 'BBL', 'conv_factor': 1.0},
        },
    )

    normalized = greeks.normalize_aspect_contributions(
        prepared,
        aggregation='month',
        unit_mode='lots',
        cob_date='2026-07-28',
    )

    exposure_by_instrument = normalized.set_index('risk_bucket')['exposure'].to_dict()
    assert exposure_by_instrument == {
        'ICE_TTF': 1.0,
        'ICE_BRENT_FUTURES': -2.0,
    }
    assert set(normalized['greek']) == {'delta'}
    assert set(normalized['source']) == {greeks.ASPECT_SOURCE_LABEL}


def test_aspect_only_export_contains_source_sheets(monkeypatch):
    prepared, _ = greeks._prepare_aspect_records(_aspect_frame())
    monkeypatch.setattr(
        greeks,
        'fetch_instrument_unit_map',
        lambda: {
            'ICE_TTF': {'unit': 'MMBtu', 'conv_factor': 1.0},
            'ICE_BRENT_FUTURES': {'unit': 'BBL', 'conv_factor': 1.0},
        },
    )

    result = greeks.export_greeks_workbook(
        1,
        greeks._empty_store(),
        {'meta': _aspect_meta(), 'rows': prepared.to_dict('records')},
        'month',
        None,
        None,
        'lots',
    )
    workbook = load_workbook(
        io.BytesIO(base64.b64decode(result['content'])),
        read_only=True,
        data_only=False,
    )

    assert workbook.sheetnames == [
        'Aspect Metadata',
        'Aspect Summary',
        'Aspect Delta',
        'Aspect Unit Delta',
        'Aspect Raw',
    ]
    assert workbook['Aspect Metadata']['B3'].value == 'settlement'
    assert workbook['Aspect Raw']['A2'].value == 'ICE_TTF'
    aspect_summary = workbook['Aspect Summary']
    summary_headers = [
        cell.value for cell in next(aspect_summary.iter_rows(min_row=1, max_row=1))
    ]
    delta_column = summary_headers.index('Delta') + 1
    assert (
        aspect_summary.cell(row=2, column=delta_column).number_format
        == '#,##0.000000'
    )


def test_quantity_greek_displays_retain_six_decimal_places():
    frame = pd.DataFrame([{'Delta': 1234.1234567}])
    rounded = greeks._round_numeric(frame)

    assert rounded.loc[0, 'Delta'] == pytest.approx(1234.123457)
    assert greeks._format_grid_number(rounded.loc[0, 'Delta']) == '1,234.123457'
    assert greeks._format_currency(-12.3456784) == '-$12.345678'
