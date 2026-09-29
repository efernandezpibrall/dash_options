from pages.vol_surface import vol_trades_link_style


def test_vol_trades_link_is_visible_for_supported_products():
    for product in ("BRENT", "HH", "JKM", "TTF"):
        assert vol_trades_link_style(product) == {"display": "inline-flex"}


def test_vol_trades_link_is_hidden_for_unsupported_products():
    assert vol_trades_link_style("NBP") == {"display": "none"}
    assert vol_trades_link_style(None) == {"display": "none"}
