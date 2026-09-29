from dash import no_update

from pages import pricer


def test_asian_date_sync_enforces_ordering_bounds():
    corrected = pricer.sync_asian76_dates(
        '2026-10-19',
        '2026-09-01',
        '2026-09-15',
        'TTF',
        None,
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
        None,
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
