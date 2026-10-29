"""Spreadsheet presentation of timestamp values; no calibration transformations."""

from datetime import datetime


def excel_safe_frame(frame):
    """Preserve aware timestamps as ISO text, including their original offsets.

    Excel has no timezone-aware datetime type. Other values and naive dates keep
    their existing representation; the source frame is never modified.
    """
    return frame.map(
        lambda value: value.isoformat()
        if isinstance(value, datetime) and value.tzinfo is not None
        else value
    )
