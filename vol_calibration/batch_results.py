"""Data-only table and result contracts shared by calibration UI and workers."""

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from options.calibration_engine.io.storage import PARAM_COLUMNS


def parse_table_data(data: List[Dict]) -> pd.DataFrame:
    """
    Parse DataTable data back to DataFrame.

    Parameters
    ----------
    data : list of dict
        Data from dash_table.DataTable

    Returns
    -------
    DataFrame
        Parameters DataFrame
    """
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)

    # Parse RMSE from percentage string back to decimal
    if 'rmse' in df.columns:
        df['rmse'] = df['rmse'].apply(
            lambda x: float(x.replace('%', '')) / 100 if isinstance(x, str) and '%' in x else x
        )

    if 'calibration_basis' in df.columns:
        df['calibration_basis'] = (
            df['calibration_basis'].astype(str).str.strip().str.lower()
        )

    return df


def format_batch_result_row(
    expiry: str,
    status: str,
    old_rmse: Optional[float] = None,
    new_rmse: Optional[float] = None,
    basis: Optional[str] = None,
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
    if basis:
        row['basis'] = str(basis).strip().title()

    if old_rmse is not None and new_rmse is not None and old_rmse > 0:
        improvement = (old_rmse - new_rmse) / old_rmse * 100
        row['improvement'] = f"{improvement:+.1f}%"

    return row


def model_params(values):
    """Extract only Wing parameters from an editable table/store mapping."""
    return {
        name: float(values[name])
        for name in PARAM_COLUMNS
        if name in values and pd.notna(values[name])
    }


def candidate_params(result):
    """Return editable tail parameters plus the selected join widths."""
    return {
        **model_params(result.get('params', {})),
        'left_blend_width': float(result['left_blend_width']),
        'right_blend_width': float(result['right_blend_width']),
    }


def format_tv_rmse(value):
    numeric = pd.to_numeric(pd.Series([value]), errors='coerce').iloc[0]
    return f"{float(numeric):.6f}" if np.isfinite(numeric) else "Unavailable"
