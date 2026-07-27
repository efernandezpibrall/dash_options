"""
Batch calibration modal component.

Provides a confirmation dialog and progress tracking for calibrating
all expiries at once with safety measures.
"""
import pandas as pd
import dash_bootstrap_components as dbc
from dash import html, dcc, dash_table
from typing import Dict, List, Optional


def create_batch_calibration_confirm_modal(commodity: str) -> dbc.Modal:
    """
    Create confirmation modal for batch calibration.

    Parameters
    ----------
    commodity : str
        Commodity code

    Returns
    -------
    dbc.Modal
        Confirmation modal component
    """
    prefix = commodity.lower()

    modal = dbc.Modal(
        [
            dbc.ModalHeader(
                dbc.ModalTitle([
                    html.I(className="fas fa-exclamation-triangle text-warning me-2"),
                    "Confirm Batch Calibration"
                ]),
                close_button=True,
            ),
            dbc.ModalBody([
                dbc.Alert([
                    html.I(className="fas fa-info-circle me-2"),
                    html.Strong("This action will calibrate ALL expiries."),
                    html.P([
                        "The optimizer will run on each expiry to find the best-fit parameters. ",
                        "This operation may take several seconds depending on the number of expiries."
                    ], className="mb-0 mt-2"),
                ], color="warning", className="mb-3"),

                html.Div([
                    html.H6("Calibration Options", className="mb-3"),

                    dbc.Checklist(
                        id=f"{prefix}-batch-auto-save",
                        options=[
                            {"label": " Auto-save calibrated parameters to database", "value": "auto_save"}
                        ],
                        value=[],
                        className="mb-2",
                    ),

                    dbc.Checklist(
                        id=f"{prefix}-batch-skip-good-fit",
                        options=[
                            {"label": " Skip expiries with RMSE < 1% (already well-calibrated)", "value": "skip_good"}
                        ],
                        value=[],
                        className="mb-2",
                    ),
                ], className="mb-3"),

                html.Div([
                    html.Span("Number of expiries to calibrate: ", className="text-muted"),
                    html.Span(id=f"{prefix}-batch-expiry-count", className="fw-bold"),
                ]),
            ]),
            dbc.ModalFooter([
                dbc.Button(
                    "Cancel",
                    id=f"{prefix}-batch-cancel-btn",
                    color="secondary",
                    className="me-2",
                ),
                dbc.Button(
                    [html.I(className="fas fa-magic me-1"), "Start Calibration"],
                    id=f"{prefix}-batch-confirm-btn",
                    color="success",
                ),
            ]),
        ],
        id=f"{prefix}-batch-confirm-modal",
        is_open=False,
        backdrop="static",
        centered=True,
    )

    return modal


def create_batch_calibration_progress_modal(commodity: str) -> dbc.Modal:
    """
    Create progress modal for batch calibration.

    Parameters
    ----------
    commodity : str
        Commodity code

    Returns
    -------
    dbc.Modal
        Progress modal component
    """
    prefix = commodity.lower()

    modal = dbc.Modal(
        [
            dbc.ModalHeader(
                dbc.ModalTitle([
                    html.I(className="fas fa-cog fa-spin me-2"),
                    "Calibrating All Expiries"
                ]),
                close_button=False,
            ),
            dbc.ModalBody([
                html.Div([
                    html.P(id=f"{prefix}-batch-progress-text", className="text-center mb-2"),
                    dbc.Progress(
                        id=f"{prefix}-batch-progress-bar",
                        value=0,
                        striped=True,
                        animated=True,
                        className="mb-3",
                    ),
                ]),

                html.Div(
                    id=f"{prefix}-batch-results-container",
                    children=[],
                    style={"maxHeight": "300px", "overflowY": "auto"},
                ),
            ]),
            dbc.ModalFooter([
                dbc.Button(
                    "Close",
                    id=f"{prefix}-batch-progress-close-btn",
                    color="secondary",
                    disabled=True,
                ),
            ]),
        ],
        id=f"{prefix}-batch-progress-modal",
        is_open=False,
        backdrop="static",
        centered=True,
        size="lg",
    )

    return modal


def create_batch_results_table(results: List[Dict]) -> dash_table.DataTable:
    """
    Create results table showing calibration outcomes.

    Parameters
    ----------
    results : list of dict
        List of calibration results with keys:
        - expiry: expiry date
        - status: 'success', 'skipped', or 'failed'
        - old_rmse: previous RMSE
        - new_rmse: new RMSE after calibration
        - improvement: percentage improvement

    Returns
    -------
    dash_table.DataTable
        Results table
    """
    columns = [
        {'id': 'expiry', 'name': 'Expiry'},
        {'id': 'status', 'name': 'Status'},
        {'id': 'old_rmse', 'name': 'Old RMSE'},
        {'id': 'new_rmse', 'name': 'New RMSE'},
        {'id': 'improvement', 'name': 'Improvement'},
    ]

    return dash_table.DataTable(
        columns=columns,
        data=results,
        style_table={'overflowX': 'auto'},
        style_header={
            'backgroundColor': '#f8f9fa',
            'fontWeight': 'bold',
            'textAlign': 'center',
            'padding': '8px',
        },
        style_cell={
            'textAlign': 'center',
            'padding': '6px',
            'fontSize': '12px',
        },
        style_data_conditional=[
            {
                'if': {'filter_query': '{status} = "Success"'},
                'backgroundColor': '#d4edda',
                'color': '#155724',
            },
            {
                'if': {'filter_query': '{status} = "Skipped"'},
                'backgroundColor': '#fff3cd',
                'color': '#856404',
            },
            {
                'if': {'filter_query': '{status} = "Failed"'},
                'backgroundColor': '#f8d7da',
                'color': '#721c24',
            },
        ],
    )


def format_batch_result_row(
    expiry: str,
    status: str,
    old_rmse: Optional[float] = None,
    new_rmse: Optional[float] = None,
) -> Dict:
    """
    Format a single batch calibration result.

    Parameters
    ----------
    expiry : str
        Expiry date string
    status : str
        'Success', 'Skipped', or 'Failed'
    old_rmse : float, optional
        Previous RMSE
    new_rmse : float, optional
        New RMSE after calibration

    Returns
    -------
    dict
        Formatted result row
    """
    row = {
        'expiry': expiry,
        'status': status,
        'old_rmse': f"{old_rmse*100:.2f}%" if old_rmse is not None else "-",
        'new_rmse': f"{new_rmse*100:.2f}%" if new_rmse is not None else "-",
        'improvement': "-",
    }

    if old_rmse is not None and new_rmse is not None and old_rmse > 0:
        improvement = (old_rmse - new_rmse) / old_rmse * 100
        row['improvement'] = f"{improvement:+.1f}%"

    return row


def create_batch_summary(results: List[Dict]) -> html.Div:
    """
    Create summary of batch calibration results.

    Parameters
    ----------
    results : list of dict
        List of calibration results

    Returns
    -------
    html.Div
        Summary component
    """
    total = len(results)
    success = sum(1 for r in results if r['status'] == 'Success')
    skipped = sum(1 for r in results if r['status'] == 'Skipped')
    failed = sum(1 for r in results if r['status'] == 'Failed')

    return html.Div([
        dbc.Row([
            dbc.Col([
                dbc.Badge([
                    html.I(className="fas fa-check me-1"),
                    f"{success} Success"
                ], color="success", className="me-2"),
            ], width="auto"),
            dbc.Col([
                dbc.Badge([
                    html.I(className="fas fa-forward me-1"),
                    f"{skipped} Skipped"
                ], color="warning", className="me-2"),
            ], width="auto"),
            dbc.Col([
                dbc.Badge([
                    html.I(className="fas fa-times me-1"),
                    f"{failed} Failed"
                ], color="danger", className="me-2"),
            ], width="auto"),
            dbc.Col([
                html.Span(f"Total: {total}", className="text-muted"),
            ], width="auto"),
        ], className="mb-3 justify-content-center"),
    ])
