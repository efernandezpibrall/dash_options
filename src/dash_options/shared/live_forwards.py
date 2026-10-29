"""Authorized dashboard access to shared, manually activated desk curves."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import request

from options.market_data.live_forward_curves import LiveCurveError, LiveForwardCurveRepository, curve_forward, supports_mapping
from dash_options.core.config import config_value, get_database_engine
from dash_options.calibration.auth import Permission, authorize, resolve_request_identity

VALUATION_TIMEZONE = ZoneInfo('Asia/Dubai')


def enabled():
    return str(config_value('BLOOMBERG_OPTIONS', 'LIVE_FORWARD_CURVES_ENABLED', fallback='false')).lower() in {'1', 'true', 'yes', 'on'}


def identity(permission=Permission.VIEW):
    current = resolve_request_identity(request.headers, remote_addr=request.remote_addr)
    authorize(current, permission)
    return current.subject


def repository():
    if not enabled():
        raise LiveCurveError('Shared desk curves are disabled in this deployment')
    return LiveForwardCurveRepository(get_database_engine(),
        schema=config_value('DATABASE', 'SCHEMA', fallback='at_lng'),
        scope=config_value('BLOOMBERG_OPTIONS', 'LIVE_FORWARD_CURVES_SCOPE', fallback='desk'))


def resolve(revision=None):
    identity()
    return repository().resolve(revision)


def describe(curve, *, now=None):
    age = max(0, int(((now or datetime.now(timezone.utc)) - curve.captured_at).total_seconds()))
    try:
        curve.assert_current(now)
        status = 'Accepted'
    except LiveCurveError:
        status = 'Stale / held'
    return f'{status} · curve {curve.revision[:10]} · captured {curve.captured_at:%d %b %H:%M UTC} · age {age // 60}m · automatic activation off'


def live_surface(product, months, valuation_date):
    """Use the Pricer's governed publication selection for current valuation views."""
    from dash_options.features.pricer.surface_reference import load_published_surface_slice
    identity()
    asset = {'TFO': 'TTF', 'ON': 'HH', 'LNE': 'HH'}.get(product, product)
    publication = load_published_surface_slice(asset, valuation_date, months, force_refresh=True)
    surface = publication['points'].copy(deep=True)
    surface['forward_value'] = surface['working_forward']
    surface.attrs['publication_status'] = 'available'
    surface.attrs['publication_metadata'] = {key: value for key, value in publication.items()
                                             if key not in {'points', 'contract_months'}}
    return surface


def pricer_context(asset, context, valuation_date, *, curve=None):
    """Resolve browser references again server-side; never trust copied forwards."""
    from dash_options.features.pricer.structures import StructureValidationError, build_ttf_strip_components
    context = dict(context)
    mode = context.get('curve_mode', 'manual')
    if mode == 'manual':
        return context
    if mode not in {'desk', 'pinned'}:
        raise StructureValidationError('Unknown forward-curve mode')
    try:
        if not context.get('curve_revision'):
            raise LiveCurveError('Wait for the shared desk curve to load, or select Manual scenario')
        curve = curve or resolve(context['curve_revision'])
        if curve.revision != context['curve_revision']:
            raise LiveCurveError('Forward-curve revision mismatch')
        if str(valuation_date)[:10] < curve.captured_at.astimezone(VALUATION_TIMEZONE).date().isoformat():
            raise LiveCurveError('The selected curve was captured after the valuation date')
        if mode == 'desk' and str(valuation_date)[:10] != datetime.now(VALUATION_TIMEZONE).date().isoformat():
            raise LiveCurveError('Historical valuation requires a pinned curve or Manual scenario')
        mapping = context.get('exchange_mapping_id') or ''
        if asset.upper() == 'HH' and mapping not in {'CME-HH-ON', 'CME-HH-LNE'}:
            raise LiveCurveError('HH desk curve requires the CME ON or LNE mapping; ICE PHE needs an explicit proxy')
        if not supports_mapping(asset, mapping):
            raise LiveCurveError('This exchange mapping has no approved Live Prices underlying equivalence; use Manual scenario')
        if asset.upper() not in {'BRENT', 'HH', 'TTF'}:
            raise LiveCurveError('No shared desk curve for this asset')
        shape = str(context.get('delivery_shape') or 'MONTH').upper()
        if shape != 'MONTH':
            if asset.upper() != 'TTF':
                raise LiveCurveError('Shared strip forwards currently require TTF')
            # Obtain the exact contract membership and delivery weights from the Pricer.
            from datetime import date
            components, _ = build_ttf_strip_components(shape, int(context['delivery_year']),
                date.fromisoformat(str(valuation_date)[:10]), 1.0, mapping_id=mapping or None)
            marks = [curve_forward(curve, asset, item['contract_month'], pinned=mode == 'pinned') for item in components]
            context['curve_monthly_forwards'] = {m['contract_month']: m['forward'] for m in marks}
            context['forward'] = sum(item['weight'] * mark['forward'] for item, mark in zip(components, marks))
        else:
            marks = [curve_forward(curve, asset, context.get('delivery_month'), pinned=mode == 'pinned')]
            context['forward'] = marks[0]['forward']
        context['curve_provenance'] = marks
        context['forward_source'] = 'Live Prices desk curve'
        return context
    except LiveCurveError as exc:
        raise StructureValidationError(str(exc)) from exc
