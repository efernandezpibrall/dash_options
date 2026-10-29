"""One Dash context boundary shared by Pricer callback groups."""

from __future__ import annotations

from dash import ctx


def _get_pricer_triggered_id():
    try:
        return ctx.triggered_id
    except Exception:
        return None


def _get_pricer_triggered_properties():
    try:
        return {key.rsplit(".", 1)[-1] for key in ctx.triggered_prop_ids}
    except Exception:
        return None
