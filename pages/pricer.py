"""Current Pricer page with stacked exchange and OTC pricing sections."""

from __future__ import annotations

from dash import (
    dcc,
    html,
)
from datetime import date
from pricer_workspace import workspace_callbacks  # noqa: F401 - Register Pricer callbacks.
from pricer_workspace import input_callbacks  # noqa: F401 - Register Pricer callbacks.
from pricer_workspace import calculation_callbacks  # noqa: F401 - Register Pricer callbacks.
from pricer_workspace import analysis_callbacks  # noqa: F401 - Register Pricer callbacks.
from pricer_workspace import (
    components,
    state,
)
from pricer_workspace.components import (
    _build_exchange_panel,
    _build_workflow_header,
)
from pricer_workspace.constants import OTC_WORKFLOW


_fixed_components = components.build_workspace_stores()


_workspace_actions = components.build_workspace_actions()


_detailed_analysis = components.build_detailed_analysis()


_structures_container = html.Div(
    id="pricer-structures-container",
    className="pricer-structure-list",
)


_initial_exchange_workspace = state._default_exchange_workspace()


_exchange_structures_container = html.Div(
    [
        _build_exchange_panel(
            _initial_exchange_workspace["structures"][0],
            can_remove=False,
        )
    ],
    id="pricer-exchange-structures-container",
    className="pricer-structure-list pricer-exchange-structure-list",
)


_exchange_workspace_actions = html.Div(
    [
        html.Div(
            "1 structure · 0 calculated",
            id="pricer-exchange-workspace-status",
            className="pricer-workspace-status",
            role="status",
        ),
        html.Button(
            "Calculate all",
            id="pricer-exchange-calculate-all",
            className="custom-export-btn pricer-calculate-button",
        ),
        html.Button(
            "Add structure",
            id="pricer-exchange-add-structure",
            className="custom-export-btn pricer-secondary-button",
        ),
    ],
    className="pricer-workspace-actions",
)


_structures_container.children = [
    components._build_structure_panel(
        state._INITIAL_WORKSPACE["structures"][0],
        can_remove=False,
        signed_lots=True,
        use_published_surface=True,
        valuation_date_override=date.today().isoformat(),
        workflow=OTC_WORKFLOW,
        heading_level=3,
    )
]


_exchange_section = html.Section(
    [
        _build_workflow_header(
            "Exchange Traded Options",
            "pricer-exchange-section-title",
            actions=[_exchange_workspace_actions],
        ),
        html.Div(
            [_exchange_structures_container],
            className=(
                "pricer-workflow-section-body "
                "pricer-exchange-section-body"
            ),
        ),
    ],
    className="pricer-workflow-section pricer-exchange-section",
    **{"aria-labelledby": "pricer-exchange-section-title"},
)


_otc_section = html.Section(
    [
        _build_workflow_header(
            "OTC Structured Options",
            "pricer-otc-section-title",
            actions=[_workspace_actions],
        ),
        html.Div(
            [_structures_container, _detailed_analysis],
            className="pricer-workflow-section-body pricer-otc-section-body",
        ),
    ],
    className="pricer-workflow-section pricer-otc-section",
    **{"aria-labelledby": "pricer-otc-section-title"},
)


layout = html.Main(
    [
        *_fixed_components,
        dcc.Store(
            id="pricer-exchange-workspace-store",
            data=_initial_exchange_workspace,
            storage_type="session",
        ),
        dcc.Interval(
            id="pricer-exchange-workspace-hydration",
            interval=1000,
            max_intervals=1,
            n_intervals=0,
        ),
        html.H1("Pricer", className="pricer-visually-hidden-title"),
        _exchange_section,
        _otc_section,
    ],
    id="pricer-current-page",
    className="options-dashboard-container pricer-page pricer-page-current",
)
