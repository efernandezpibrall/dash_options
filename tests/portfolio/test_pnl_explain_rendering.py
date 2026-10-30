import numpy as np
import pandas as pd


def test_missing_attribution_inputs_do_not_render_a_zero_waterfall():
    from dash_options.features.portfolio.pnl_explain import _pnl_waterfall, EXPLAIN_COLUMNS
    frame = pd.DataFrame([{column: np.nan for column in EXPLAIN_COLUMNS}])
    figure = _pnl_waterfall(frame, 'USD')
    assert not figure.data
    assert 'missing inputs' in figure.layout.annotations[0].text
