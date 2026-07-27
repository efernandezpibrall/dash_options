from pages.vol_surface import build_calibration_link


def test_calibration_link_preserves_surface_context():
    href, style = build_calibration_link(
        "BRENT",
        "2026-07-08",
        "2026-09-01",
    )

    assert href == (
        "/vol_calibration?product=brent&cob_date=2026-07-08&expiry=Sep-26"
    )
    assert style == {"display": "inline-flex"}


def test_calibration_link_is_hidden_for_nbp():
    href, style = build_calibration_link("NBP", "2026-07-08", "2026-09-01")

    assert href == "/vol_calibration?product=ttf"
    assert style == {"display": "none"}
