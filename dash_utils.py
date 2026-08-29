from dash import ctx
from dash.exceptions import MissingCallbackContextException


def triggered_id():
    try:
        return ctx.triggered_id
    except MissingCallbackContextException:
        return None
