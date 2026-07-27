"""
Data status indicator component.

Shows whether data is from:
- Real database (Trino or PostgreSQL)
- Synthetic/sample data

Also displays last data update timestamp when available.
"""
import dash_bootstrap_components as dbc
from dash import html
from datetime import datetime
from typing import Optional, Dict, Any


def create_data_status_badge(
    data_source: str = 'unknown',
    is_synthetic: bool = True,
    last_update: Optional[datetime] = None,
    commodity: str = ''
) -> dbc.Badge:
    """
    Create a data status badge showing data source and freshness.

    Parameters
    ----------
    data_source : str
        Where data came from: 'trino', 'postgres', 'synthetic', 'sample'
    is_synthetic : bool
        True if using synthetic/sample data
    last_update : datetime, optional
        Last data update timestamp
    commodity : str
        Commodity code for element IDs

    Returns
    -------
    dbc.Badge
        Bootstrap badge showing data status
    """
    commodity_lower = commodity.lower() if commodity else 'data'

    if is_synthetic:
        color = "warning"
        icon = "fas fa-flask"
        text = "Synthetic"
    else:
        if data_source == 'trino':
            color = "success"
            icon = "fas fa-database"
            text = "Trino"
        elif data_source == 'postgres':
            color = "info"
            icon = "fas fa-database"
            text = "PostgreSQL"
        else:
            color = "secondary"
            icon = "fas fa-question"
            text = "Unknown"

    # Format timestamp
    timestamp_text = ""
    if last_update:
        if isinstance(last_update, str):
            try:
                last_update = datetime.fromisoformat(last_update.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                pass

        if isinstance(last_update, datetime):
            # Show relative time or formatted date
            timestamp_text = last_update.strftime("%d-%b %H:%M")

    badge_content = [
        html.I(className=f"{icon} me-1"),
        text,
    ]

    if timestamp_text:
        badge_content.append(
            html.Span(f" | {timestamp_text}", className="ms-1 opacity-75")
        )

    return dbc.Badge(
        badge_content,
        id=f'{commodity_lower}-data-status-badge',
        color=color,
        className="d-flex align-items-center",
        pill=True,
    )


def create_data_status_indicator(commodity: str = '') -> html.Div:
    """
    Create a complete data status indicator with badge and tooltip.

    Parameters
    ----------
    commodity : str
        Commodity code for element IDs

    Returns
    -------
    html.Div
        Container with badge and tooltip
    """
    commodity_lower = commodity.lower() if commodity else 'data'

    return html.Div([
        # Main badge (will be updated by callback)
        html.Span(
            id=f'{commodity_lower}-data-status',
            children=dbc.Badge(
                [html.I(className="fas fa-spinner fa-spin me-1"), "Loading..."],
                color="secondary",
                pill=True,
            )
        ),
        # Tooltip with more details
        dbc.Tooltip(
            id=f'{commodity_lower}-data-status-tooltip',
            target=f'{commodity_lower}-data-status',
            placement="bottom",
            children="Loading data..."
        ),
    ], className="d-inline-block")


def format_data_status(
    data_source: str,
    is_synthetic: bool,
    last_update: Optional[datetime],
    trade_date: Any,
    commodity: str = ''
) -> tuple:
    """
    Format data status for callback output.

    Returns badge component and tooltip text.

    Parameters
    ----------
    data_source : str
        Data source identifier
    is_synthetic : bool
        True if synthetic data
    last_update : datetime, optional
        Last update timestamp
    trade_date : date
        Trade date being displayed
    commodity : str
        Commodity code

    Returns
    -------
    tuple
        (badge_component, tooltip_text)
    """
    badge = create_data_status_badge(
        data_source=data_source,
        is_synthetic=is_synthetic,
        last_update=last_update,
        commodity=commodity
    )

    # Build tooltip text
    tooltip_lines = [f"Trade Date: {trade_date}"]

    if is_synthetic:
        tooltip_lines.append("Data: Synthetic (sample data)")
        tooltip_lines.append("No market data available for this date")
    else:
        source_name = data_source.title() if data_source else "Database"
        tooltip_lines.append(f"Data Source: {source_name}")

        if last_update:
            if isinstance(last_update, str):
                tooltip_lines.append(f"Last Update: {last_update}")
            elif isinstance(last_update, datetime):
                tooltip_lines.append(f"Last Update: {last_update.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    tooltip_text = " | ".join(tooltip_lines)

    return badge, tooltip_text


class DataLoadResult:
    """
    Container for data load results with metadata.

    Use this to track data source information through the loading pipeline.
    """

    def __init__(
        self,
        data: Any = None,
        source: str = 'unknown',
        is_synthetic: bool = True,
        last_update: Optional[datetime] = None,
        message: str = '',
        error: Optional[str] = None
    ):
        self.data = data
        self.source = source  # 'trino', 'postgres', 'synthetic', 'sample'
        self.is_synthetic = is_synthetic
        self.last_update = last_update
        self.message = message
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'source': self.source,
            'is_synthetic': self.is_synthetic,
            'last_update': self.last_update.isoformat() if self.last_update else None,
            'message': self.message,
            'error': self.error,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'DataLoadResult':
        """Create from dictionary."""
        last_update = None
        if d.get('last_update'):
            try:
                last_update = datetime.fromisoformat(d['last_update'])
            except (ValueError, TypeError):
                pass

        return cls(
            source=d.get('source', 'unknown'),
            is_synthetic=d.get('is_synthetic', True),
            last_update=last_update,
            message=d.get('message', ''),
            error=d.get('error'),
        )

    @classmethod
    def synthetic(cls, message: str = 'Using synthetic data') -> 'DataLoadResult':
        """Create a synthetic data result."""
        return cls(
            source='synthetic',
            is_synthetic=True,
            message=message,
        )

    @classmethod
    def from_trino(cls, last_update: Optional[datetime] = None) -> 'DataLoadResult':
        """Create a Trino data result."""
        return cls(
            source='trino',
            is_synthetic=False,
            last_update=last_update,
            message='Data loaded from Trino',
        )

    @classmethod
    def from_postgres(cls, last_update: Optional[datetime] = None) -> 'DataLoadResult':
        """Create a PostgreSQL data result."""
        return cls(
            source='postgres',
            is_synthetic=False,
            last_update=last_update,
            message='Data loaded from PostgreSQL',
        )
