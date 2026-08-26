import pandas as pd
import pytest

from pages.vol_surface import (
    _build_surface_expiry_options,
    _create_smile_evolution_figure,
    _filter_surface_by_expiry_selection,
    _format_vol_period_header,
    _format_surface_expiry_selection_label,
    _sort_grouped_period_columns,
    group_data_by_period,
)


def test_season_grouping_uses_ice_ttf_summer_apr_sep():
    maturities = pd.date_range('2027-04-01', '2027-09-01', freq='MS')
    vols = [65.6, 54.1, 49.3, 48.27, 48.17, 47.97]
    frame = pd.DataFrame(
        {
            'code': 'TTF',
            'cob_date': pd.Timestamp('2026-08-20'),
            'contract_date': maturities,
            'volatility': vols,
        }
    )

    grouped = group_data_by_period(frame, 'season')

    assert grouped['period'].tolist() == ['2027-Summer']
    assert grouped['volatility'].iloc[0] == pytest.approx(sum(vols) / len(vols))


def test_season_grouping_anchors_winter_to_start_year():
    frame = pd.DataFrame(
        {
            'code': 'TTF',
            'cob_date': pd.Timestamp('2026-08-20'),
            'contract_date': pd.to_datetime(
                ['2027-01-01', '2027-02-01', '2027-03-01', '2027-10-01']
            ),
            'volatility': [50.0, 51.0, 52.0, 60.0],
        }
    )

    grouped = group_data_by_period(frame, 'season')

    assert grouped['period'].tolist() == ['2026-Winter', '2027-Winter']
    assert grouped.loc[grouped['period'] == '2026-Winter', 'volatility'].iloc[0] == 51.0


def test_season_columns_sort_and_format_chronologically():
    columns = ['2027-Winter', '2026-Winter', '2027-Summer']

    assert _sort_grouped_period_columns(columns, 'season') == [
        '2026-Winter',
        '2027-Summer',
        '2027-Winter',
    ]
    assert _format_vol_period_header('2027-Summer') == "Summer'27"
    assert _format_vol_period_header('2026-Winter') == "Winter'26/27"


def test_surface_smile_expiry_options_include_month_quarter_and_season():
    frame = pd.DataFrame(
        {
            'contract_date': pd.to_datetime(
                [
                    '2027-04-01',
                    '2027-05-01',
                    '2027-06-01',
                    '2027-07-01',
                    '2027-10-01',
                    '2028-01-01',
                ]
            )
        }
    )

    options = _build_surface_expiry_options(frame)
    values = [option['value'] for option in options]
    labels_by_value = {option['value']: option['label'] for option in options}

    assert 'month:2027-04-01' in values
    assert 'quarter:2027-Q2' in values
    assert 'season:2027-Summer' in values
    assert 'season:2027-Winter' in values
    assert labels_by_value['quarter:2027-Q2'] == "Q2'27"
    assert labels_by_value['season:2027-Winter'] == "Winter'27/28"
    assert _format_surface_expiry_selection_label('season:2027-Summer') == "Summer'27"


def test_surface_expiry_selection_filters_quarters_and_ice_ttf_seasons():
    frame = pd.DataFrame(
        {
            'contract_date': pd.to_datetime(
                [
                    '2027-04-01',
                    '2027-05-01',
                    '2027-06-01',
                    '2027-07-01',
                    '2027-08-01',
                    '2027-09-01',
                    '2027-10-01',
                ]
            ),
            'volatility': range(7),
        }
    )

    q2 = _filter_surface_by_expiry_selection(frame, 'quarter:2027-Q2')
    summer = _filter_surface_by_expiry_selection(frame, 'season:2027-Summer')

    assert q2['contract_date'].dt.month.tolist() == [4, 5, 6]
    assert summer['contract_date'].dt.month.tolist() == [4, 5, 6, 7, 8, 9]


def test_season_smile_averages_monthly_vols_by_delta_bucket():
    current_surface = pd.DataFrame(
        {
            'code': ['TTF'] * 4,
            'cob_date': [pd.Timestamp('2026-08-20')] * 4,
            'contract_date': pd.to_datetime(
                ['2027-04-01', '2027-09-01', '2027-04-01', '2027-09-01']
            ),
            'delta_bucket': ['ATM', 'ATM', '25C', '25C'],
            'delta_sort_key': [50.0, 50.0, 75.0, 75.0],
            'volatility': [0.40, 0.60, 0.50, 0.70],
        }
    )

    figure = _create_smile_evolution_figure(
        'TTF',
        'season:2027-Summer',
        current_surface,
        pd.DataFrame(),
        30,
        pd.Timestamp('2026-08-20'),
    )

    current_trace = figure.data[0]
    assert list(current_trace.x) == ['ATM', '25C']
    assert list(current_trace.y) == pytest.approx([0.50, 0.60])
