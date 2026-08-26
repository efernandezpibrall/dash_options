import os
import re

import dash_bootstrap_components as dbc
from dash import Dash
from flask import request
from flask_compress import Compress

from health import register_health_routes


app = Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title='Options',
    update_title=None,
)
server = app.server


def _environment_flag(name, default=True):
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    raise ValueError(f'{name} must be a boolean value')


if _environment_flag('OPTIONS_HTTP_COMPRESSION_ENABLED', default=True):
    server.config.update(
        COMPRESS_ALGORITHM=['br', 'gzip'],
        COMPRESS_BR_LEVEL=4,
        COMPRESS_LEVEL=6,
        COMPRESS_MIN_SIZE=1024,
    )
    Compress(server)


_VERSIONED_COMPONENT_PATH = re.compile(r'\.v\d[^/]*m\d')


@server.after_request
def cache_static_assets(response):
    """Cache content-addressed assets immutably and revalidate other bundles."""
    path = request.path
    is_asset = path.startswith('/assets/')
    is_component_bundle = path.startswith('/_dash-component-suites/')
    if response.status_code not in {200, 304} or not (is_asset or is_component_bundle):
        return response

    versioned = (
        bool(request.args.get('m') or request.args.get('v'))
        or bool(_VERSIONED_COMPONENT_PATH.search(path))
    )
    response.cache_control.public = True
    if versioned:
        response.cache_control.max_age = 31536000
        response.cache_control.immutable = True
    else:
        response.cache_control.max_age = 3600
        response.cache_control.must_revalidate = True
    return response


register_health_routes(server)
