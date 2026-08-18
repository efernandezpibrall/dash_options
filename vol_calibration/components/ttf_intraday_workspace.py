"""Focused trader controls for the TTF intraday surface workflow."""

from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

from vol_calibration.feature_flags import (
    ttf_intraday_writes_enabled,
    ttf_publication_enabled,
)


def create_ttf_context_status():
    return html.Div(
        id="ttf-trading-context-status",
        children=dbc.Alert(
            "Resolving trading date, official settlement, and latest publication...",
            color="secondary",
            className="py-2 px-3 mb-2 small",
        ),
    )


def create_ttf_publication_status():
    return html.Div(
        id="ttf-publication-status",
        children=dbc.Alert(
            "Latest published smile loading...",
            color="secondary",
            className="py-2 px-3 mb-3 small",
        ),
    )


def _field(label, component, help_text=None, width=2):
    children = [dbc.Label(label, className="small fw-semibold mb-1"), component]
    if help_text:
        children.append(html.Div(help_text, className="small text-muted mt-1"))
    return dbc.Col(children, xs=12, sm=6, lg=width, className="mb-2")


def create_ttf_intraday_trade_panel():
    columns = [
        {"id": "observed_at", "name": "Time"},
        {"id": "contract_label", "name": "Expiry"},
        {"id": "put_call", "name": "C/P"},
        {"id": "strike", "name": "Strike", "type": "numeric"},
        {"id": "mark_iv_pct", "name": "IV (%)", "type": "numeric"},
        {"id": "iv_source", "name": "IV Source"},
        {"id": "call_delta", "name": "Call Delta", "type": "numeric"},
        {"id": "forward", "name": "Forward", "type": "numeric"},
        {"id": "volume", "name": "Volume", "type": "numeric"},
        {"id": "persistence", "name": "State"},
    ]
    return dbc.Card(
        [
            dbc.CardHeader(
                [
                    html.H6("Intraday option trades", className="mb-0"),
                    html.Div(
                        "Enter IV or premium at the actual strike. When IV is blank, "
                        "Black-76 derives it from premium and the displayed working "
                        "forward; the trade is plotted only on its contract month.",
                        className="small text-muted mt-1",
                    ),
                ]
            ),
            dbc.CardBody(
                [
                    dbc.Row(
                        [
                            _field(
                                "Contract month",
                                dcc.Dropdown(
                                    id="ttf-intraday-expiry",
                                    options=[],
                                    clearable=False,
                                ),
                                width=2,
                            ),
                            _field(
                                "Option type",
                                dcc.Dropdown(
                                    id="ttf-intraday-put-call",
                                    options=[
                                        {"label": "Call", "value": "C"},
                                        {"label": "Put", "value": "P"},
                                    ],
                                    value="C",
                                    clearable=False,
                                ),
                                width=1,
                            ),
                            _field(
                                "Strike",
                                dbc.Input(id="ttf-intraday-strike", type="number", min=0),
                                width=1,
                            ),
                            _field(
                                "IV (%)",
                                dbc.Input(id="ttf-intraday-iv", type="number", min=0),
                                "Use IV or premium",
                                width=1,
                            ),
                            _field(
                                "Premium",
                                dbc.Input(
                                    id="ttf-intraday-premium", type="number", min=0
                                ),
                                "Optional Black-76 inversion",
                                width=1,
                            ),
                            _field(
                                "Volume",
                                dbc.Input(id="ttf-intraday-volume", type="number", min=0),
                                width=1,
                            ),
                            _field(
                                "Working forward",
                                dbc.Input(
                                    id="ttf-intraday-forward", type="number", min=0
                                ),
                                "Used for IV inversion and delta; update intraday",
                                width=3,
                            ),
                            _field(
                                "Notes",
                                dbc.Input(id="ttf-intraday-notes", type="text"),
                                width=3,
                            ),
                            dbc.Col(
                                dbc.Button(
                                    [html.I(className="fas fa-plus me-1"), "Add trade"],
                                    id="ttf-intraday-add-btn",
                                    color="primary",
                                    className="mt-4",
                                ),
                                xs=12,
                                sm=6,
                                lg=2,
                                className="mb-2",
                            ),
                        ],
                        className="align-items-start",
                    ),
                    html.Div(id="ttf-intraday-entry-status", className="mb-2"),
                    dash_table.DataTable(
                        id="ttf-intraday-trade-table",
                        columns=columns,
                        data=[],
                        row_selectable="single",
                        selected_rows=[],
                        page_action="none",
                        style_table={"overflowX": "auto"},
                        style_header={
                            "backgroundColor": "#343a40",
                            "color": "white",
                            "fontWeight": "bold",
                        },
                        style_cell={"padding": "7px", "textAlign": "center"},
                        style_data_conditional=[
                            {
                                "if": {"state": "selected"},
                                "backgroundColor": "rgba(37, 99, 235, 0.12)",
                                "border": "1px solid #2563eb",
                            }
                        ],
                    ),
                    html.Div(
                        "Select one trade to make it an optional local calibration "
                        "target. The trade itself is never silently converted into "
                        "an official settlement node.",
                        className="small text-muted mt-2",
                    ),
                ],
                className="p-3",
            ),
        ],
        className="mb-4",
    )


def create_ttf_adjustment_workspace(node_editor, expert_tail_table):
    control_specs = (
        (
            "Level (vol pts)",
            "ttf-adjust-level",
            "Parallel move of every node.",
        ),
        (
            "Skew (vol pts)",
            "ttf-adjust-skew",
            "Positive raises the put wing and lowers the call wing around 50D.",
        ),
        (
            "Put curvature (vol pts)",
            "ttf-adjust-put-curvature",
            "Localized convexity on the high-call-delta / low-strike side.",
        ),
        (
            "Call curvature (vol pts)",
            "ttf-adjust-call-curvature",
            "Localized convexity on the low-call-delta / high-strike side.",
        ),
    )
    controls = [
        _field(
            label,
            dbc.Input(id=control_id, type="number", value=0.0, step=0.1),
            help_text,
            width=3,
        )
        for label, control_id, help_text in control_specs
    ]
    publication_is_enabled = ttf_publication_enabled()
    return dbc.Card(
        [
            dbc.CardHeader(
                [
                    html.H6("Adjust selected smile", className="mb-0"),
                    html.Div(
                        "Four stable desk controls move the PCHIP core. Node and "
                        "tail details remain available only when needed.",
                        className="small text-muted mt-1",
                    ),
                ]
            ),
            dbc.CardBody(
                [
                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label("Selected expiry", className="small fw-semibold"),
                                    dcc.Dropdown(
                                        id="ttf-workspace-expiry",
                                        options=[],
                                        clearable=False,
                                    ),
                                ],
                                xs=12,
                                md=4,
                            ),
                            dbc.Col(
                                dbc.Checklist(
                                    id="ttf-use-selected-trade",
                                    options=[
                                        {
                                            "label": "Match selected trade locally",
                                            "value": "target",
                                        }
                                    ],
                                    value=[],
                                    switch=True,
                                    className="mt-4",
                                ),
                                xs=12,
                                md=4,
                            ),
                            dbc.Col(
                                html.Div(id="ttf-adjustment-basis", className="mt-4 small"),
                                xs=12,
                                md=4,
                            ),
                        ],
                        className="mb-3",
                    ),
                    dbc.Row(controls),
                    dbc.ButtonGroup(
                        [
                            dbc.Button(
                                "Build candidate",
                                id="ttf-build-candidate-btn",
                                color="primary",
                            ),
                            dbc.Button(
                                "Reset selected smile",
                                id="ttf-reset-adjustment-btn",
                                color="secondary",
                                outline=True,
                            ),
                            dbc.Button(
                                "Publish validated surface",
                                id="ttf-publish-btn",
                                color="success",
                                disabled=not publication_is_enabled,
                                title=(
                                    "Publication requires migrated storage, server-side "
                                    "authentication, and enabled write/publication flags."
                                    if not publication_is_enabled
                                    else "Publish a complete immutable TTF surface revision."
                                ),
                            ),
                        ],
                        className="mb-3",
                    ),
                    html.Div(id="ttf-adjustment-status", className="mb-3"),
                    dbc.Accordion(
                        [
                            dbc.AccordionItem(
                                [
                                    dbc.Checklist(
                                        id="ttf-node-unlock",
                                        options=[
                                            {
                                                "label": "Unlock direct node overrides",
                                                "value": "unlock",
                                            }
                                        ],
                                        value=[],
                                        switch=True,
                                        className="mb-2",
                                    ),
                                    node_editor,
                                ],
                                title="Node details · settlement / published / candidate",
                            ),
                            dbc.AccordionItem(
                                [
                                    html.P(
                                        "These six controls affect only extrapolation "
                                        "outside the governed 1D-99D core. The remaining "
                                        "Wing parameters are optimizer-managed.",
                                        className="small text-muted",
                                    ),
                                    expert_tail_table,
                                ],
                                title="Expert tail controls",
                            ),
                        ],
                        start_collapsed=True,
                        always_open=True,
                    ),
                ],
                className="p-3",
            ),
            html.Div(
                "Manual trades are session-only while database writes are disabled."
                if not ttf_intraday_writes_enabled()
                else "Manual trades are persisted when server-side authentication succeeds.",
                className="small text-muted px-3 pb-3",
            ),
        ],
        className="mb-4",
    )
