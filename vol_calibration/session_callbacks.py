"""Preserve edited Vol Trades calibration tables while the panel is remounted."""

from __future__ import annotations

from dash import Input, Output, State, callback
from dash.exceptions import PreventUpdate

from vol_calibration.session_state import persist_product_table


def _register_table_persistence(product: str):
    @callback(
        Output("vol-calibration-session-state", "data", allow_duplicate=True),
        Input(f"{product}-param-table", "data"),
        State("vol-calibration-session-state", "data"),
        State(f"{product}-date-picker", "date"),
        prevent_initial_call=True,
    )
    def persist_table_state(table_data, state, cob_date):
        if table_data is None:
            raise PreventUpdate
        return persist_product_table(state, product, cob_date, table_data)

    return persist_table_state


for _product in ("brent", "jkm"):
    _register_table_persistence(_product)
