import pandas as pd

from pages.vol_surface import _format_surface_table_df


def test_brent_dte_uses_verified_option_expiry_only():
    pivot = pd.DataFrame({'ATM': [0.30]}, index=pd.DatetimeIndex(['2028-01-01'], name='contract_date'))
    lookup = pd.DataFrame(
        {
            'contract_date': [pd.Timestamp('2028-01-01')],
            'option_expiration_date': [pd.Timestamp('2027-11-24')],
        }
    )
    table = _format_surface_table_df(
        pivot,
        cob_date='2026-07-08',
        dte_lookup=lookup,
        allow_contract_date_fallback=False,
    )
    assert table.iloc[0]['expiry'] == '2028-01'
    assert table.iloc[0]['dte'] == 504


def test_brent_dte_is_blank_when_verified_expiry_is_missing():
    pivot = pd.DataFrame({'ATM': [0.30]}, index=pd.DatetimeIndex(['2028-01-01'], name='contract_date'))
    table = _format_surface_table_df(
        pivot,
        cob_date='2026-07-08',
        dte_lookup=pd.DataFrame(),
        allow_contract_date_fallback=False,
    )
    assert pd.isna(table.iloc[0]['dte'])

