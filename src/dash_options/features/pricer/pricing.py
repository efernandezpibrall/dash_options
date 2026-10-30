"""Pure published-surface row preparation and calculation identity."""

from __future__ import annotations

from options.valuation.adjustments import _published_surface_calculation_rows as _valuation_rows

from dash_options.features.pricer.state import (
    _context_from_states,
    _input_signature_from_context,
    _rows_with_volatility_adjustments,
    _surface_reference_input_signature,
)


def _published_surface_calculation_rows(asset, model, rows, surface_reference, expected_reference_signature):
    return _valuation_rows(asset, model, _rows_with_volatility_adjustments(model, rows or []),
                           surface_reference, expected_reference_signature)


def _calculation_input_signature(
    asset,
    model,
    contract_multiplier,
    rows,
    param_values,
    date_values,
    valuation_date_value,
    param_ids,
    date_ids,
    surface_reference=None,
    use_published_surface=False,
    mapping_id=None,
):
    context = _context_from_states(
        model,
        param_values,
        param_ids,
        date_values,
        date_ids,
    )
    if mapping_id is not None:
        context["exchange_mapping_id"] = mapping_id
    calculation_rows = rows
    surface_signature = None
    if use_published_surface:
        expected_reference_signature = _surface_reference_input_signature(
            asset,
            model,
            rows,
            context,
            valuation_date_value,
        )
        calculation_rows, surface_signature = _published_surface_calculation_rows(
            asset,
            model,
            rows,
            surface_reference,
            expected_reference_signature,
        )
    if context.get('curve_mode', 'manual') != 'manual':
        from dash_options.shared.live_forwards import pricer_context
        context = pricer_context(asset, context, valuation_date_value)
    input_signature = _input_signature_from_context(
        asset,
        model,
        contract_multiplier,
        calculation_rows,
        context,
        valuation_date_value,
    )
    if surface_signature is not None:
        input_signature["published_surface"] = surface_signature
    if mapping_id is not None:
        input_signature["mapping_id"] = mapping_id
    return input_signature
