"""Pure published-surface row preparation and calculation identity."""

from __future__ import annotations

import copy
import math

from pricer_structure import (
    SINGLE_ASSET_MODELS,
    StructureValidationError,
)
from pricer_surface_reference import (
    REFERENCE_SCHEMA_VERSION,
    volatility_overlay_points,
)
from .constants import (
    MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT,
    VOLATILITY_ADJUSTMENT_FIELDS,
    VOLATILITY_ADJUSTMENT_SCALE,
)
from .state import (
    _context_from_states,
    _input_signature_from_context,
    _rows_with_volatility_adjustments,
    _surface_reference_input_signature,
)


def _published_surface_calculation_rows(
    asset,
    model,
    rows,
    surface_reference,
    expected_reference_signature,
):
    """Replace legacy quote fields with governed per-leg contract vols."""
    normalized_rows = _rows_with_volatility_adjustments(model, rows or [])
    if model not in SINGLE_ASSET_MODELS and model != "kirk":
        return normalized_rows, None
    if (
        not isinstance(surface_reference, dict)
        or surface_reference.get("schema_version") != REFERENCE_SCHEMA_VERSION
    ):
        raise StructureValidationError(
            "Published surface volatility is not available yet. "
            "Wait for the surface to load and calculate again."
        )
    if surface_reference.get("asset") != str(asset or ""):
        raise StructureValidationError(
            "Published surface volatility does not match the selected asset."
        )
    if surface_reference.get("model") != model:
        raise StructureValidationError(
            "Published surface volatility does not match the selected model."
        )
    if surface_reference.get("_ui_reference_signature") != expected_reference_signature:
        raise StructureValidationError(
            "Published surface volatility is still refreshing for the current inputs. "
            "Wait for it to load and calculate again."
        )
    if surface_reference.get("source_kind") not in (None, "governed"):
        raise StructureValidationError(
            "The Pricer requires an active governed calibrated publication."
        )
    if model == "kirk":
        if not surface_reference.get("source_revision"):
            raise StructureValidationError(
                "No governed publication revision is available for the Kirk assets."
            )
        asset_publications = surface_reference.get("asset_publications")
        if not isinstance(asset_publications, dict):
            raise StructureValidationError(
                "Governed publications are unavailable for the two Kirk assets."
            )
        for asset_number in (1, 2):
            publication = asset_publications.get(f"asset_{asset_number}")
            required = (
                "asset",
                "publication_id",
                "run_id",
                "publication_cob",
                "published_at",
                "contract_date",
                "reference_expiry",
                "anchor_call_delta",
                "anchor_volatility",
            )
            if not isinstance(publication, dict) or any(
                publication.get(field) in (None, "") for field in required
            ):
                raise StructureValidationError(
                    f"No governed publication is available for Kirk Asset {asset_number}."
                )
        surface_rows = surface_reference.get("rows")
        if not isinstance(surface_rows, dict):
            raise StructureValidationError(
                "Governed volatility is unavailable for the Kirk option legs."
            )
        resolved_rows = []
        signature_rows = []
        for position, row in enumerate(normalized_rows, start=1):
            leg_id = str(row.get("leg_id") or "")
            surface_row = surface_rows.get(leg_id)
            if not isinstance(surface_row, dict):
                raise StructureValidationError(
                    f"Leg {position}: governed Kirk volatility is unavailable."
                )
            resolved = copy.deepcopy(row)
            signature_row = {"leg_id": leg_id}
            for asset_number in (1, 2):
                field = f"surface_volatility_asset_{asset_number}"
                pricing_field = f"surface_pricing_volatility_asset_{asset_number}"
                try:
                    volatility = float(surface_row[field])
                    pricing_volatility = float(surface_row[pricing_field])
                except (KeyError, TypeError, ValueError, OverflowError):
                    detail = surface_row.get(f"surface_asset_{asset_number}_tooltip")
                    raise StructureValidationError(
                        f"Leg {position}: {detail or f'Asset {asset_number} governed volatility is unavailable.'}"
                    ) from None
                if not (
                    math.isfinite(volatility)
                    and 0.005 <= volatility <= 2.0
                    and math.isfinite(pricing_volatility)
                    and 0.005 <= pricing_volatility <= 2.0
                ):
                    raise StructureValidationError(
                        f"Leg {position}: Asset {asset_number} governed volatility "
                        "is outside the supported range."
                    )
                resolved[f"volatility_asset_{asset_number}"] = volatility
                signature_row[f"surface_volatility_asset_{asset_number}"] = volatility
                signature_row[pricing_field] = pricing_volatility
            resolved_rows.append(resolved)
            signature_rows.append(signature_row)
        return resolved_rows, {
            "source_kind": "governed",
            "source_revision": str(surface_reference.get("source_revision") or ""),
            "asset_publications": copy.deepcopy(asset_publications),
            "warnings": copy.deepcopy(surface_reference.get("warnings") or []),
            "rows": signature_rows,
        }
    publication_fields = ("publication_id", "publication_cob", "published_at")
    if any(
        surface_reference.get(field) in (None, "")
        for field in publication_fields
    ):
        raise StructureValidationError(
            "No published surface revision is available for the selected inputs."
        )
    surface_rows = surface_reference.get("rows")
    if not isinstance(surface_rows, dict):
        raise StructureValidationError(
            "Published surface volatility is unavailable for the option legs."
        )

    resolved_rows = []
    surface_signature_rows = []
    for position, row in enumerate(normalized_rows, start=1):
        leg_id = str(row.get("leg_id") or "")
        surface_row = surface_rows.get(leg_id)
        if not isinstance(surface_row, dict):
            raise StructureValidationError(
                f"Leg {position}: published surface volatility is unavailable."
            )
        input_volatility = surface_row.get("surface_input_vol")
        atm_input_volatility = surface_row.get("surface_atm_input_vol")
        skew_input_volatility = surface_row.get("surface_skew_input_vol")
        pricing_volatility = surface_row.get("surface_pricing_vol")
        try:
            input_volatility = float(input_volatility)
            atm_input_volatility = float(atm_input_volatility)
            skew_input_volatility = float(skew_input_volatility)
            pricing_volatility = float(pricing_volatility)
        except (TypeError, ValueError, OverflowError):
            detail = surface_row.get("surface_input_tooltip")
            raise StructureValidationError(
                f"Leg {position}: {detail or 'published surface volatility is unavailable.'}"
            ) from None
        if not all(
            math.isfinite(value) and 0.005 <= value <= 2.0
            for value in (
                input_volatility,
                atm_input_volatility,
                pricing_volatility,
            )
        ) or not math.isfinite(skew_input_volatility):
            detail = surface_row.get("surface_input_tooltip")
            raise StructureValidationError(
                f"Leg {position}: {detail or 'published surface volatility is outside the supported range.'}"
            )
        if not math.isclose(
            input_volatility,
            atm_input_volatility + skew_input_volatility,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise StructureValidationError(
                f"Leg {position}: published Input vol does not reconcile to ATM plus Skew."
            )
        adjustment_values = {}
        for field, label in zip(
            VOLATILITY_ADJUSTMENT_FIELDS,
            ("ATM", "Skew", "Smile"),
        ):
            raw_adjustment = row.get(field)
            if isinstance(raw_adjustment, bool):
                raw_adjustment = None
            try:
                adjustment = float(raw_adjustment)
            except (TypeError, ValueError, OverflowError):
                raise StructureValidationError(
                    f"Leg {position}: {label} volatility adjustment must be finite."
                ) from None
            if not math.isfinite(adjustment):
                raise StructureValidationError(
                    f"Leg {position}: {label} volatility adjustment must be finite."
                )
            if abs(adjustment) > MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT:
                raise StructureValidationError(
                    f"Leg {position}: {label} volatility adjustment is limited to "
                    f"{MAX_ABSOLUTE_VOLATILITY_ADJUSTMENT:.0f} vol points."
                )
            adjustment_values[field] = adjustment
        smile_coordinate = surface_row.get("surface_smile_coordinate")
        if smile_coordinate is None:
            has_shape_adjustment = any(
                adjustment_values[field]
                for field in VOLATILITY_ADJUSTMENT_FIELDS[1:]
            )
            if has_shape_adjustment and not surface_row.get(
                "surface_component_volatilities"
            ):
                raise StructureValidationError(
                    f"Leg {position}: the published surface smile position is unavailable."
                )
            smile_coordinate = 0.0
        try:
            smile_coordinate = float(smile_coordinate)
        except (TypeError, ValueError, OverflowError):
            raise StructureValidationError(
                f"Leg {position}: the published surface smile position is invalid."
            ) from None
        if not math.isfinite(smile_coordinate) or abs(smile_coordinate) > 2.0:
            raise StructureValidationError(
                f"Leg {position}: the published surface smile position is invalid."
            )
        effective_input_volatility = input_volatility + (
            VOLATILITY_ADJUSTMENT_SCALE
            * volatility_overlay_points(adjustment_values, smile_coordinate)
        )
        effective_pricing_volatility = pricing_volatility * (
            effective_input_volatility / input_volatility
        )
        component_volatilities = []
        raw_component_volatilities = surface_row.get(
            "surface_component_volatilities"
        )
        if raw_component_volatilities is not None:
            if not isinstance(raw_component_volatilities, list):
                raise StructureValidationError(
                    f"Leg {position}: published component volatility metadata is invalid."
                )
            for component_position, component_row in enumerate(
                raw_component_volatilities,
                start=1,
            ):
                if not isinstance(component_row, dict):
                    raise StructureValidationError(
                        f"Leg {position}: published component {component_position} "
                        "volatility metadata is invalid."
                )
                try:
                    contract_month = str(component_row["contract_month"])
                    component_input = float(component_row["input_volatility"])
                    component_pricing = float(component_row["pricing_volatility"])
                    component_coordinate = float(component_row["smile_coordinate"])
                    expiry_factor = float(
                        component_row["expiry_adjustment_factor"]
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    raise StructureValidationError(
                        f"Leg {position}: published component {component_position} "
                        "volatility metadata is invalid."
                    ) from None
                effective_component_input = component_input + (
                    VOLATILITY_ADJUSTMENT_SCALE
                    * volatility_overlay_points(
                        adjustment_values, component_coordinate
                    )
                )
                effective_component_pricing = (
                    effective_component_input * expiry_factor
                )
                if not (
                    math.isfinite(component_input)
                    and 0.005 <= component_input <= 2.0
                    and math.isfinite(component_pricing)
                    and 0.005 <= component_pricing <= 2.0
                    and math.isfinite(component_coordinate)
                    and abs(component_coordinate) <= 2.0
                    and math.isfinite(expiry_factor)
                    and expiry_factor > 0.0
                    and math.isclose(
                        component_pricing,
                        component_input * expiry_factor,
                        rel_tol=1e-10,
                        abs_tol=1e-12,
                    )
                    and math.isfinite(effective_component_input)
                    and 0.005 <= effective_component_input <= 2.0
                    and math.isfinite(effective_component_pricing)
                    and 0.005 <= effective_component_pricing <= 2.0
                ):
                    raise StructureValidationError(
                        f"Leg {position}: published component {component_position} "
                        "volatility or expiry adjustment is outside the supported range."
                    )
                component_volatilities.append(
                    {
                        "contract_month": contract_month,
                        "input_volatility": effective_component_input,
                        "pricing_volatility": effective_component_pricing,
                        "smile_coordinate": component_coordinate,
                        "expiry_adjustment_factor": expiry_factor,
                    }
                )
        if component_volatilities:
            try:
                effective_input_volatility = float(
                    surface_row["surface_effective_input_vol"]
                )
                effective_pricing_volatility = float(
                    surface_row["surface_effective_pricing_vol"]
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                raise StructureValidationError(
                    f"Leg {position}: the premium-equivalent Pricing Vol "
                    "could not be resolved."
                ) from None
            if len(component_volatilities) == 1 and not (
                math.isclose(
                    effective_input_volatility,
                    component_volatilities[0]["input_volatility"],
                    rel_tol=1e-10,
                    abs_tol=1e-12,
                )
                and math.isclose(
                    effective_pricing_volatility,
                    component_volatilities[0]["pricing_volatility"],
                    rel_tol=1e-10,
                    abs_tol=1e-12,
                )
            ):
                raise StructureValidationError(
                    f"Leg {position}: the adjusted surface volatility is inconsistent."
                )
        if not (
            math.isfinite(effective_input_volatility)
            and 0.005 <= effective_input_volatility <= 2.0
            and math.isfinite(effective_pricing_volatility)
            and 0.005 <= effective_pricing_volatility <= 2.0
        ):
            raise StructureValidationError(
                f"Leg {position}: the volatility adjustments produce an Input vol "
                "outside the supported 0.005–2.0 range."
            )
        resolved = copy.deepcopy(row)
        resolved["quote_basis"] = "VOL"
        resolved["quote_value"] = effective_input_volatility
        resolved["expiry_adjustment_factor"] = (
            effective_pricing_volatility / effective_input_volatility
        )
        resolved.pop("volatility", None)
        if len(component_volatilities) > 1:
            resolved["component_volatilities"] = component_volatilities
        resolved_rows.append(resolved)
        surface_signature_rows.append(
            {
                "leg_id": leg_id,
                "surface_input_vol": input_volatility,
                "surface_atm_input_vol": atm_input_volatility,
                "surface_skew_input_vol": skew_input_volatility,
                "surface_pricing_vol": pricing_volatility,
                **adjustment_values,
                "effective_input_vol": effective_input_volatility,
                "effective_pricing_vol": effective_pricing_volatility,
                **(
                    {
                        "component_volatilities": copy.deepcopy(
                            component_volatilities
                        )
                    }
                    if raw_component_volatilities is not None
                    else {}
                ),
                "surface_expiry_adjustments": copy.deepcopy(
                    surface_row.get("surface_expiry_adjustments") or []
                ),
            }
        )
    signature = {
        field: str(surface_reference[field]) for field in publication_fields
    } | {"rows": surface_signature_rows}
    if surface_reference.get("warnings"):
        signature["warnings"] = copy.deepcopy(surface_reference["warnings"])
    return resolved_rows, signature


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
