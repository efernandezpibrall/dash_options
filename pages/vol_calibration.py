"""Embedded volatility calibration route with lazy commodity workspaces."""

from __future__ import annotations

import copy
from urllib.parse import parse_qs, urlencode

import dash_bootstrap_components as dbc
import pandas as pd
from dash import Input, Output, State, callback, dcc, html
from dash.exceptions import PreventUpdate

from vol_calibration.feature_flags import calibration_enabled, publication_enabled, writes_enabled
from vol_calibration.pages import brent, hh, jkm, ttf


PRODUCT_MODULES = {
    "brent": brent,
    "hh": hh,
    "ttf": ttf,
    "jkm": jkm,
}
PRODUCT_ORDER = ("brent", "hh", "ttf", "jkm")
DEFAULT_PRODUCT = "ttf"


def parse_calibration_query(search: str | None) -> dict[str, str | None]:
    values = parse_qs((search or "").lstrip("?"), keep_blank_values=False)
    requested_product = (values.get("product") or [DEFAULT_PRODUCT])[0].lower()
    product = requested_product if requested_product in PRODUCT_MODULES else DEFAULT_PRODUCT
    return {
        "product": product,
        "invalid_product": requested_product if requested_product not in PRODUCT_MODULES else None,
        "cob_date": (values.get("cob_date") or [None])[0],
        "expiry": (values.get("expiry") or [None])[0],
    }


def update_product_query(search: str | None, product: str) -> str:
    values = parse_qs((search or "").lstrip("?"), keep_blank_values=False)
    values["product"] = [product]
    return f"?{urlencode(values, doseq=True)}"


def _set_component_property(component, component_id: str, property_name: str, value) -> bool:
    if getattr(component, "id", None) == component_id:
        setattr(component, property_name, value)
        return True
    children = getattr(component, "children", None)
    if children is None:
        return False
    if not isinstance(children, (list, tuple)):
        children = [children]
    return any(
        _set_component_property(child, component_id, property_name, value)
        for child in children
        if hasattr(child, "children") or getattr(child, "id", None) is not None
    )


def _active_workspace(product: str, cob_date: str | None):
    workspace = copy.deepcopy(PRODUCT_MODULES[product].layout)
    if cob_date:
        parsed_date = pd.to_datetime(cob_date, errors="coerce")
        if not pd.isna(parsed_date):
            _set_component_property(
                workspace,
                f"{product}-date-picker",
                "date",
                parsed_date.date().isoformat(),
            )
    return workspace


def create_layout(search: str | None = None):
    if not calibration_enabled():
        return dbc.Alert(
            "Vol Calibration is disabled by configuration.",
            color="secondary",
            className="m-4",
        )

    context = parse_calibration_query(search)
    notices = [
        dbc.Alert(
            [
                html.Strong("Diagnostic release: "),
                "calibration, comparison, and export are enabled. Database saving and publication are disabled.",
            ],
            color="warning",
            className="vol-calibration-release-notice",
        )
    ]
    if context["invalid_product"]:
        notices.append(
            dbc.Alert(
                f"Unknown product {context['invalid_product']!r}; showing TTF.",
                color="info",
                className="vol-calibration-release-notice",
            )
        )

    return html.Div(
        [
            dcc.Store(id="vol-calibration-requested-expiry", data=context["expiry"]),
            *notices,
            dbc.Tabs(
                [
                    dbc.Tab(label=product.upper(), tab_id=product)
                    for product in PRODUCT_ORDER
                ],
                id="vol-calibration-product-tabs",
                active_tab=context["product"],
                className="vol-calibration-tabs",
            ),
            html.Div(
                _active_workspace(context["product"], context["cob_date"]),
                id="vol-calibration-workspace",
            ),
        ],
        className="vol-calibration-page",
    )


def validation_layout():
    return html.Div(
        [
            create_layout("?product=ttf"),
            brent.layout,
            hh.layout,
            jkm.layout,
        ]
    )


@callback(
    Output("url", "search", allow_duplicate=True),
    Input("vol-calibration-product-tabs", "active_tab"),
    State("url", "search"),
    prevent_initial_call=True,
)
def select_product_tab(product, search):
    if product not in PRODUCT_MODULES:
        raise PreventUpdate
    context = parse_calibration_query(search)
    if context["product"] == product and context["invalid_product"] is None:
        raise PreventUpdate
    return update_product_query(search, product)


def _expiry_key(value) -> str:
    if value is None:
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if not pd.isna(parsed):
        return parsed.strftime("%Y-%m")
    return str(value).strip().lower().replace("_", "-").replace(" ", "-")


def _register_expiry_selection_callback(product: str):
    @callback(
        Output(f"{product}-param-table", "selected_rows", allow_duplicate=True),
        Input(f"{product}-param-table", "data"),
        State("vol-calibration-requested-expiry", "data"),
        prevent_initial_call=True,
    )
    def select_requested_expiry(rows, requested_expiry):
        if not rows or not requested_expiry:
            raise PreventUpdate
        requested_key = _expiry_key(requested_expiry)
        for index, row in enumerate(rows):
            if _expiry_key(row.get("expiry")) == requested_key:
                return [index]
        raise PreventUpdate

    return select_requested_expiry


for _product in PRODUCT_ORDER:
    _register_expiry_selection_callback(_product)


__all__ = [
    "DEFAULT_PRODUCT",
    "PRODUCT_MODULES",
    "PRODUCT_ORDER",
    "calibration_enabled",
    "create_layout",
    "parse_calibration_query",
    "publication_enabled",
    "update_product_query",
    "validation_layout",
    "writes_enabled",
]
