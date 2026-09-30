"""Pricer session migration, row normalization and calculation identity helpers."""

from __future__ import annotations

import copy
import datetime as dt
import math

from datetime import (
    date,
    timedelta,
)
from pricer_exchange_registry import (
    canonical_exchange_mapping_id,
    exchange_option_mapping,
)
from pricer_structure import (
    DEFAULT_ASSET,
    MODEL_LABELS,
    SCHEMA_VERSION,
    SINGLE_ASSET_MODELS,
    SUPPORTED_ASSETS,
    StructureValidationError,
    available_delivery_months,
    default_context,
    default_contract_size,
    default_leg,
    default_premium_convention,
)
from .constants import (
    DEFAULT_STRUCTURE_ID,
    EXCHANGE_STRUCTURE_ID,
    EXCHANGE_WORKFLOW,
    EXCHANGE_WORKSPACE_SCHEMA_VERSION,
    MAX_PRICER_DECIMALS,
    OTC_WORKFLOW,
    PRICER_CONTRACT_SIZE_MIGRATION_SCHEMA_VERSION,
    PRICER_WORKSPACE_SCHEMA_VERSION,
    VOLATILITY_ADJUSTMENT_FIELDS,
)


def _surface_reference_input_signature(
    asset,
    model,
    rows,
    context,
    valuation_date_value,
):
    """Identify the exact inputs used to resolve each published-surface vol."""
    return {
        "asset": str(asset or ""),
        "model": model,
        "valuation_date": parse_date(
            valuation_date_value,
            date.today(),
        ).isoformat(),
        "context": _normalized_signature_context(context),
        "legs": [
            {
                "leg_id": str(row.get("leg_id") or ""),
                "call_put": copy.deepcopy(row.get("call_put")),
                "strike": copy.deepcopy(row.get("strike")),
                **{
                    field: copy.deepcopy(row.get(field, 0.0))
                    for field in VOLATILITY_ADJUSTMENT_FIELDS
                },
            }
            for row in rows or []
            if isinstance(row, dict)
        ],
    }


def _rows_with_committed_leg_edit(rows, cell_value_changed):
    updated = copy.deepcopy(rows) if isinstance(rows, list) else []
    event = (
        cell_value_changed[-1]
        if isinstance(cell_value_changed, list) and cell_value_changed
        else cell_value_changed
    )
    if not isinstance(event, dict):
        return updated
    leg_id = (event.get("data") or {}).get("leg_id")
    column = event.get("column")
    field = event.get("colId") or (
        column.get("colId") if isinstance(column, dict) else None
    )
    if not leg_id or not field or field == "leg_id" or "newValue" not in event:
        return updated
    shared_adjustment = field in VOLATILITY_ADJUSTMENT_FIELDS
    if shared_adjustment and not any(
        isinstance(row, dict)
        and row.get("leg_id") == leg_id
        and row.get(field) in (event.get("oldValue"), event["newValue"])
        for row in updated
    ):
        return updated
    for row in updated:
        if (
            isinstance(row, dict)
            and field in row
            and (shared_adjustment or row.get("leg_id") == leg_id)
            and (shared_adjustment or row[field] == event.get("oldValue"))
        ):
            row[field] = event["newValue"]
            if not shared_adjustment:
                break
    return updated


def _delivery_month_options(asset, model, as_of, mapping_id=None):
    return [
        {
            "label": delivery_month.strftime("%b-%y"),
            "value": delivery_month.isoformat(),
        }
        for delivery_month in available_delivery_months(
            asset,
            model,
            as_of,
            mapping_id=mapping_id,
        )
    ]


def _resolved_delivery_month(value, options):
    valid_values = {option["value"] for option in options}
    if value:
        try:
            parsed = parse_date(value)
            normalized = date(parsed.year, parsed.month, 1).isoformat()
        except (TypeError, ValueError):
            normalized = None
        if normalized in valid_values:
            return normalized
    return options[0]["value"] if options else None


def _context_id(model, param, is_date=False, structure_id=DEFAULT_STRUCTURE_ID):
    return {
        "type": "pricer-context-date" if is_date else "pricer-context-param",
        "structure_id": structure_id,
        "model": model,
        "param": param,
    }


def _migrated_kirk_context_values(values, legacy_asset=None):
    migrated = copy.deepcopy(values) if isinstance(values, dict) else {}
    if migrated.get("asset_1_forward") is None and migrated.get("asset_1") is not None:
        migrated["asset_1_forward"] = migrated["asset_1"]
    if migrated.get("asset_2_forward") is None and migrated.get("asset_2") is not None:
        migrated["asset_2_forward"] = migrated["asset_2"]
    if not migrated.get("asset_1_code") and legacy_asset in SUPPORTED_ASSETS:
        migrated["asset_1_code"] = legacy_asset
    common_reference_expiry = migrated.get("contract_expiration_date")
    if not migrated.get("asset_1_reference_expiry") and common_reference_expiry:
        migrated["asset_1_reference_expiry"] = common_reference_expiry
    if not migrated.get("asset_2_reference_expiry") and common_reference_expiry:
        migrated["asset_2_reference_expiry"] = common_reference_expiry
    if not migrated.get("contractual_expiry") and migrated.get("expiration_date"):
        migrated["contractual_expiry"] = migrated["expiration_date"]
    migrated.pop("asset_1", None)
    migrated.pop("asset_2", None)
    return migrated


def _combined_result_rows(snapshot):
    vega_tooltip = (
        "Adjusted pricing vol, 1 point"
        if snapshot["context"].get("vega_basis") == "adjusted_pricing_vol"
        else "Contract vol, 1 point"
    )
    rows = []
    for leg in snapshot["legs"]:
        row = {
            "leg_id": leg["leg_id"],
            "name": leg["name"],
            "side": leg["side"],
            "ratio": leg["ratio"],
            "call_put": leg["call_put"],
            "strike": leg["strike"],
            "unit_value": leg["unit"]["value"],
            "trade_value": leg["trade_contribution"]["value"],
            **{
                f"unit_{field}": leg["unit"]["greeks"].get(field)
                for field in snapshot["greek_fields"]
            },
            **{
                f"trade_{field}": leg["trade_contribution"]["greeks"].get(field)
                for field in snapshot["greek_fields"]
            },
            "_vega_tooltip": vega_tooltip,
            "_rho_tooltip": "1 rate point",
        }
        if snapshot["model"] in SINGLE_ASSET_MODELS:
            row["quote_basis"] = leg["quote_basis"].title()
            row["entered_premium"] = leg["entered_premium"]
            row["raw_volatility"] = leg["raw_volatility"]
            is_premium_input = str(leg["quote_basis"]).lower() == "premium"
            row["input_volatility"] = (
                None if is_premium_input else leg["raw_volatility"]
            )
            row["implied_volatility"] = (
                leg["raw_volatility"] if is_premium_input else None
            )
            row["volatility_used"] = leg["volatility_used"]
        else:
            row["raw_volatility_asset_1"] = leg["raw_volatility_asset_1"]
            row["raw_volatility_asset_2"] = leg["raw_volatility_asset_2"]
            row["volatility_asset_1_used"] = leg["volatility_asset_1_used"]
            row["volatility_asset_2_used"] = leg["volatility_asset_2_used"]
        rows.append(row)
    total = {
        "leg_id": "__total__",
        "name": "Total",
        "side": "",
        "ratio": None,
        "call_put": "",
        "strike": None,
        "quote_basis": "",
        "entered_premium": None,
        "raw_volatility": None,
        "input_volatility": None,
        "implied_volatility": None,
        "volatility_used": None,
        "trade_value": snapshot["totals"]["trade_value"],
        "unit_value": snapshot["totals"]["unit_structure_value"],
        **{
            f"trade_{field}": snapshot["totals"]["trade_greeks"].get(field)
            for field in snapshot["greek_fields"]
        },
        **{
            f"unit_{field}": snapshot["totals"]["unit_structure_greeks"].get(field)
            for field in snapshot["greek_fields"]
        },
        "_vega_tooltip": vega_tooltip,
        "_rho_tooltip": "1 rate point",
    }
    return rows, total


def _resolved_contract_size_default(
    asset,
    model,
    context_values=None,
    valuation_date_value=None,
):
    if model == "kirk":
        return 1.0
    valuation_date = parse_date(valuation_date_value, date.today())
    resolved_context = default_context(model, valuation_date)
    if isinstance(context_values, dict):
        resolved_context.update(context_values)
    resolved_context["asset"] = asset
    return default_contract_size(
        asset,
        resolved_context,
        as_of=valuation_date,
    )


def _resolved_mapping_contract_size_default(
    mapping,
    context_values=None,
    valuation_date_value=None,
):
    if mapping is None:
        raise StructureValidationError("Exchange mapping is required.")
    if mapping.sizing_mode == "fixed":
        return mapping.contract_size
    resolved_context = (
        copy.deepcopy(context_values) if isinstance(context_values, dict) else {}
    )
    resolved_context["exchange_mapping_id"] = mapping.mapping_id
    return _resolved_contract_size_default(
        mapping.asset,
        mapping.model,
        resolved_context,
        valuation_date_value,
    )


def _instance_id(component_type, structure_id):
    return {"type": component_type, "structure_id": structure_id}


def _month_only_field_id(structure_id, field):
    return {
        "type": "pricer-month-only-field",
        "structure_id": structure_id,
        "field": field,
    }


def _instance_persistence(structure_id, key):
    return f"pricer-{structure_id}-{key}"


def _nonnegative_click_count(value):
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _structure_display_label(structure_id, fallback_sequence=1):
    prefix, separator, suffix = str(structure_id or "").rpartition("-")
    if separator and prefix == "structure" and suffix.isdigit():
        return f"S{int(suffix)}"
    return f"S{fallback_sequence}"


def _default_workspace():
    return {
        "schema_version": PRICER_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": 2,
        "drafts": {},
        "structures": [
            {
                "structure_id": DEFAULT_STRUCTURE_ID,
                "label": "S1",
                "template": None,
            }
        ],
    }


def _migrate_template_premium_convention(
    template,
    *,
    migrate_legacy_contract_size=False,
    migrate_legacy_kirk_context=True,
    migrate_governed_mapping=False,
):
    if not isinstance(template, dict):
        return template
    mapping_id = canonical_exchange_mapping_id(template.get("mapping_id"))
    if mapping_id is not None:
        template["mapping_id"] = mapping_id
    template.pop("structure_type", None)
    context = template.get("context")
    if not isinstance(context, dict):
        return template
    context_mapping_id = canonical_exchange_mapping_id(
        context.get("exchange_mapping_id")
    )
    if context_mapping_id is not None:
        context["exchange_mapping_id"] = context_mapping_id
    context.pop("structure_type", None)
    mapping = exchange_option_mapping(mapping_id or context_mapping_id)
    if mapping is not None:
        template["mapping_id"] = mapping.mapping_id
        template["model"] = mapping.model
        template["asset"] = mapping.asset
        context["exchange_mapping_id"] = mapping.mapping_id
        context["premium_convention"] = mapping.premium_convention
    model = template.get("model")
    if model not in MODEL_LABELS:
        model = "black76"
    legacy_asset = template.get("asset")
    asset = legacy_asset if legacy_asset in SUPPORTED_ASSETS else DEFAULT_ASSET
    if mapping is not None:
        asset = mapping.asset
    if model == "kirk" and migrate_legacy_kirk_context:
        if context.get("asset_1_forward") is None and context.get("asset_1") is not None:
            context["asset_1_forward"] = context["asset_1"]
        if context.get("asset_2_forward") is None and context.get("asset_2") is not None:
            context["asset_2_forward"] = context["asset_2"]
        if not context.get("asset_1_code") and legacy_asset in SUPPORTED_ASSETS:
            context["asset_1_code"] = legacy_asset
        common_reference_expiry = context.get("contract_expiration_date")
        if not context.get("asset_1_reference_expiry") and common_reference_expiry:
            context["asset_1_reference_expiry"] = common_reference_expiry
        if not context.get("asset_2_reference_expiry") and common_reference_expiry:
            context["asset_2_reference_expiry"] = common_reference_expiry
        if not context.get("contractual_expiry") and context.get("expiration_date"):
            context["contractual_expiry"] = context["expiration_date"]
        context.pop("asset_1", None)
        context.pop("asset_2", None)
    premium_convention = context.get("premium_convention")
    if premium_convention in (None, "", "product_default") or (
        model == "kirk" and premium_convention == "upfront"
    ):
        context["premium_convention"] = default_premium_convention(asset, model)
    if (
        model in SINGLE_ASSET_MODELS
        and context.get("premium_convention") == "futures_style"
    ):
        context["rate"] = 0.0
    context.setdefault("delivery_shape", "MONTH")
    if (migrate_legacy_contract_size or migrate_governed_mapping) and model != "kirk":
        try:
            legacy_size = float(template.get("contract_multiplier", 1))
        except (TypeError, ValueError, OverflowError):
            legacy_size = None
        if legacy_size == 1.0 or (migrate_governed_mapping and mapping is not None):
            valuation_date = parse_date(
                template.get("valuation_date"),
                date.today(),
            )
            resolved_context = default_context(model, valuation_date)
            resolved_context.update(context)
            resolved_context["asset"] = asset
            try:
                template["contract_multiplier"] = default_contract_size(
                    asset,
                    resolved_context,
                    as_of=valuation_date,
                )
            except StructureValidationError:
                pass
    return template


def _is_valid_calculation_snapshot(snapshot):
    if not (
        isinstance(snapshot, dict)
        and snapshot.get("schema_version") == SCHEMA_VERSION
        and snapshot.get("model") in MODEL_LABELS
        and isinstance(snapshot.get("context"), dict)
        and isinstance(snapshot.get("legs"), list)
        and snapshot.get("legs")
        and isinstance(snapshot.get("totals"), dict)
        and isinstance(snapshot.get("greek_fields"), list)
        and isinstance(snapshot.get("greek_labels"), dict)
        and isinstance(snapshot.get("model_label"), str)
        and isinstance(snapshot.get("calculation_date"), str)
    ):
        return False
    context = snapshot["context"]
    common_context = {
        "asset",
        "premium_convention",
        "resolved_premium_convention",
        "delivery_shape",
        "margin_style",
        "expiration_date",
        "contract_expiration_date",
        "time_to_expiry",
        "day_count_basis",
    }
    if snapshot["model"] in SINGLE_ASSET_MODELS:
        model_context = {
            "forward",
            "rate",
            "vol_adjustment_factor",
            "variance_calendar_code",
        }
    else:
        model_context = {
            "asset_1_code",
            "asset_2_code",
            "asset_1_forward",
            "asset_2_forward",
            "asset_1_price_unit",
            "asset_2_price_unit",
            "asset_1_calendar_code",
            "asset_2_calendar_code",
            "asset_1_contractual_business_days",
            "asset_2_contractual_business_days",
            "asset_1_reference_business_days",
            "asset_2_reference_business_days",
            "asset_1_vol_adjustment_factor",
            "asset_2_vol_adjustment_factor",
            "asset_1_reference_expiry",
            "asset_2_reference_expiry",
            "contractual_expiry",
            "correlation",
            "discount_factor",
        }
    if snapshot["model"] == "asian76":
        model_context |= {"averaging_start_date", "time_to_averaging_start"}
    if not common_context.issubset(context) or not model_context.issubset(context):
        return False
    if (
        snapshot["model"] != "kirk"
        and not context.get("delivery_components")
        and not {
        "option_business_days",
        "contract_business_days",
        }.issubset(context)
    ):
        return False
    totals = snapshot["totals"]
    if not {
        "trade_value",
        "unit_structure_value",
        "trade_greeks",
        "unit_structure_greeks",
    }.issubset(totals):
        return False
    if not isinstance(totals["trade_greeks"], dict) or not isinstance(
        totals["unit_structure_greeks"], dict
    ):
        return False
    for leg in snapshot["legs"]:
        if not isinstance(leg, dict) or not {
            "leg_id",
            "name",
            "side",
            "ratio",
            "call_put",
            "strike",
            "unit",
            "trade_contribution",
        }.issubset(leg):
            return False
        if not all(
            isinstance(leg.get(group), dict)
            and "value" in leg[group]
            and isinstance(leg[group].get("greeks"), dict)
            for group in ("unit", "trade_contribution")
        ):
            return False
        quote_fields = (
            {"quote_basis", "entered_premium", "raw_volatility", "volatility_used"}
            if snapshot["model"] in SINGLE_ASSET_MODELS
            else {
                "raw_volatility_asset_1",
                "raw_volatility_asset_2",
                "volatility_asset_1_used",
                "volatility_asset_2_used",
            }
        )
        if not quote_fields.issubset(leg):
            return False
    return True


def _normalize_workspace(workspace):
    if not isinstance(workspace, dict):
        return _default_workspace()
    raw_structures = workspace.get("structures")
    if not isinstance(raw_structures, list):
        return _default_workspace()
    try:
        workspace_schema_version = int(workspace.get("schema_version", 0))
        migrate_legacy_contract_size = (
            workspace_schema_version
            < PRICER_CONTRACT_SIZE_MIGRATION_SCHEMA_VERSION
        )
        migrate_legacy_kirk_context = migrate_legacy_contract_size
        migrate_governed_mapping = (
            workspace_schema_version < PRICER_WORKSPACE_SCHEMA_VERSION
        )
    except (TypeError, ValueError, OverflowError):
        migrate_legacy_contract_size = True
        migrate_legacy_kirk_context = True
        migrate_governed_mapping = True
    structures = []
    seen = set()
    for index, raw_structure in enumerate(raw_structures, start=1):
        if not isinstance(raw_structure, dict):
            continue
        structure_id = str(raw_structure.get("structure_id") or "").strip()
        if not structure_id or structure_id in seen:
            continue
        seen.add(structure_id)
        template = (
            copy.deepcopy(raw_structure.get("template"))
            if isinstance(raw_structure.get("template"), dict)
            else None
        )
        if template is not None:
            _migrate_template_premium_convention(
                template,
                migrate_legacy_contract_size=migrate_legacy_contract_size,
                migrate_legacy_kirk_context=migrate_legacy_kirk_context,
                migrate_governed_mapping=migrate_governed_mapping,
            )
        structures.append(
            {
                "structure_id": structure_id,
                "label": _structure_display_label(structure_id, index),
                "template": template,
            }
        )
    if not structures:
        return _default_workspace()
    numeric_sequences = []
    for structure in structures:
        prefix, separator, suffix = structure["structure_id"].rpartition("-")
        if separator and prefix == "structure" and suffix.isdigit():
            numeric_sequences.append(int(suffix))
    next_sequence = workspace.get("next_structure_sequence")
    try:
        next_sequence = max(
            int(next_sequence),
            len(structures) + 1,
            max(numeric_sequences, default=0) + 1,
        )
    except (TypeError, ValueError):
        next_sequence = max(len(structures) + 1, max(numeric_sequences, default=0) + 1)
    while f"structure-{next_sequence}" in seen:
        next_sequence += 1
    raw_drafts = workspace.get("drafts")
    drafts = {
        structure_id: copy.deepcopy(template)
        for structure_id, template in (
            raw_drafts.items() if isinstance(raw_drafts, dict) else []
        )
        if structure_id in seen and isinstance(template, dict)
    }
    for template in drafts.values():
        _migrate_template_premium_convention(
            template,
            migrate_legacy_contract_size=migrate_legacy_contract_size,
            migrate_legacy_kirk_context=migrate_legacy_kirk_context,
            migrate_governed_mapping=migrate_governed_mapping,
        )
    return {
        "schema_version": PRICER_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": next_sequence,
        "drafts": drafts,
        "structures": structures,
    }


def _reduce_workspace(workspace, action, structure_id=None, template=None):
    return reduce_workspace(
        _normalize_workspace(workspace),
        action,
        structure_id,
        template,
        id_prefix="structure-",
        label_prefix="S",
    )


def parse_date(date_str, default_date=None):
    if not date_str:
        return default_date or date.today() + timedelta(days=365)
    if isinstance(date_str, dt.datetime):
        return date_str.date()
    if isinstance(date_str, dt.date):
        return date_str
    try:
        return dt.datetime.strptime(str(date_str).split("T", 1)[0], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default_date or date.today() + timedelta(days=365)


def _normalize_pricer_number_text(value):
    text = str(value).strip().replace(" ", "")
    if not text or "+" in text or "-" in text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    if text.count(".") > 1:
        return None
    integer_part, separator, decimal_part = text.partition(".")
    if integer_part and not integer_part.isdigit():
        return None
    if separator and decimal_part and not decimal_part.isdigit():
        return None
    if separator and len(decimal_part) > MAX_PRICER_DECIMALS:
        return None
    if not integer_part and not decimal_part:
        return None
    return text


def _coerce_pricer_float(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = _normalize_pricer_number_text(value)
        if value is None:
            return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _rows_for_lot_mode(rows, *, signed_lots):
    converted = []
    if not isinstance(rows, list):
        return converted
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        row = copy.deepcopy(raw_row)
        side = str(row.get("side") or "").strip().upper()
        ratio_value = row.get("ratio")
        try:
            ratio_number = (
                None
                if isinstance(ratio_value, bool)
                else float(ratio_value)
            )
        except (TypeError, ValueError, OverflowError):
            ratio_number = None
        if ratio_number is not None and not math.isfinite(ratio_number):
            ratio_number = None

        if signed_lots:
            if ratio_number is not None and side in {"BUY", "SELL"}:
                row["ratio"] = abs(ratio_number) * (
                    -1.0 if side == "SELL" else 1.0
                )
            row.pop("side", None)
        elif not side and ratio_number is not None:
            row["side"] = "SELL" if ratio_number < 0 else "BUY"
            row["ratio"] = abs(ratio_number)
        converted.append(row)
    return converted


def _default_leg_for_lot_mode(
    model,
    sequence,
    *,
    signed_lots,
    use_published_surface=False,
):
    rows = _rows_for_lot_mode(
        [default_leg(model, sequence)],
        signed_lots=signed_lots,
    )
    if use_published_surface:
        rows = _rows_with_volatility_adjustments(model, rows)
    return rows[0]


def _quote_ready_rows(model, rows, *, signed_lots=None):
    migrated = []
    if not isinstance(rows, list):
        return migrated
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        if model in SINGLE_ASSET_MODELS:
            if "quote_basis" not in row:
                row["quote_basis"] = "VOL"
                row["quote_value"] = row.get("volatility")
            else:
                quote_basis = str(row.get("quote_basis") or "").strip().upper()
                row["quote_basis"] = (
                    "VOL" if quote_basis == "VOLATILITY" else quote_basis
                )
                if "quote_value" not in row:
                    row["quote_value"] = (
                        row.get("volatility")
                        if row["quote_basis"] == "VOL"
                        else None
                    )
            row.pop("volatility", None)
        migrated.append(row)
    if signed_lots is None:
        return migrated
    return _rows_for_lot_mode(migrated, signed_lots=signed_lots)


def _rows_with_volatility_adjustments(model, rows, *, signed_lots=None):
    normalized = _quote_ready_rows(model, rows, signed_lots=signed_lots)
    if model not in SINGLE_ASSET_MODELS:
        return normalized
    for row in normalized:
        for field in VOLATILITY_ADJUSTMENT_FIELDS:
            row.setdefault(field, 0.0)
    return normalized


_INITIAL_WORKSPACE = _default_workspace()


def _state_for_structure(values, ids, structure_id, default=None):
    for value, component_id in zip(values or [], ids or []):
        if (
            isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ):
            return value
    return default


def _capture_structure_template(
    structure_id,
    asset_values,
    asset_ids,
    model_values,
    model_ids,
    multiplier_values,
    multiplier_ids,
    valuation_values,
    valuation_ids,
    row_values,
    row_ids,
    draft_values,
    draft_ids,
    param_values,
    param_ids,
    date_values,
    date_ids,
    mapping_values=None,
    mapping_ids=None,
):
    model = _state_for_structure(
        model_values, model_ids, structure_id, "black76"
    )
    context = _context_from_states(
        model,
        [
            value
            for value, component_id in zip(param_values or [], param_ids or [])
            if isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ],
        [
            component_id
            for component_id in param_ids or []
            if isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ],
        [
            value
            for value, component_id in zip(date_values or [], date_ids or [])
            if isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ],
        [
            component_id
            for component_id in date_ids or []
            if isinstance(component_id, dict)
            and component_id.get("structure_id") == structure_id
        ],
    )
    draft = _state_for_structure(draft_values, draft_ids, structure_id, {}) or {}
    rows = _state_for_structure(row_values, row_ids, structure_id, []) or []
    template = {
        "asset": _state_for_structure(
            asset_values, asset_ids, structure_id, DEFAULT_ASSET
        ),
        "model": model,
        "contract_multiplier": _state_for_structure(
            multiplier_values, multiplier_ids, structure_id, 1
        ),
        "valuation_date": _state_for_structure(
            valuation_values,
            valuation_ids,
            structure_id,
            date.today().isoformat(),
        ),
        "context": context,
        "legs": copy.deepcopy(rows),
        "next_leg_sequence": draft.get("next_leg_sequence", len(rows) + 1),
    }
    mapping_id = _state_for_structure(
        mapping_values,
        mapping_ids,
        structure_id,
        None,
    )
    if mapping_id is not None:
        template["mapping_id"] = mapping_id
    return template


def _context_from_states(model, param_values, param_ids, date_values, date_ids):
    context = {}
    for value, component_id in zip(param_values or [], param_ids or []):
        if (
            isinstance(component_id, dict)
            and component_id.get("model") == model
        ):
            context[component_id.get("param")] = value
    for value, component_id in zip(date_values or [], date_ids or []):
        if (
            isinstance(component_id, dict)
            and component_id.get("model") == model
        ):
            context[component_id.get("param")] = value
    return context


def _normalized_signature_context(context):
    normalized = {}
    for key, value in (context or {}).items():
        if key in {"structure_type", "asset"}:
            continue
        if key and key.endswith("_date") and value:
            normalized[key] = parse_date(value).isoformat()
        else:
            normalized[key] = value
    delivery_shape = str(
        normalized.get("delivery_shape") or "MONTH"
    ).strip().upper()
    if delivery_shape == "MONTH":
        normalized.pop("delivery_year", None)
    else:
        normalized.pop("averaging_start_date", None)
        normalized.pop("expiration_date", None)
        normalized.pop("contract_expiration_date", None)
        normalized.pop("delivery_month", None)
    return normalized


def _signature_leg_rows(model, rows):
    common_fields = (
        "leg_id",
        "name",
        "side",
        "ratio",
        "call_put",
        "strike",
    )
    model_fields = (
        ("quote_basis", "quote_value")
        if model in SINGLE_ASSET_MODELS
        else ("volatility_asset_1", "volatility_asset_2")
    )
    fields = (*common_fields, *model_fields)
    return [
        {field: copy.deepcopy(row.get(field)) for field in fields}
        for row in _quote_ready_rows(model, rows or [])
    ]


def _input_signature_from_context(
    asset,
    model,
    contract_multiplier,
    rows,
    context,
    valuation_date_value,
):
    normalized_context = _normalized_signature_context(context)
    premium_convention = normalized_context.get(
        "premium_convention", default_premium_convention(asset, model)
    )
    is_futures_style = premium_convention == "futures_style"
    if is_futures_style and model in SINGLE_ASSET_MODELS:
        normalized_context["rate"] = 0.0
    return {
        "asset": asset,
        "model": model,
        "contract_multiplier": _coerce_pricer_float(contract_multiplier),
        "valuation_date": parse_date(
            valuation_date_value,
            date.today(),
        ).isoformat(),
        "context": normalized_context,
        "legs": _signature_leg_rows(model, rows),
    }


def _template_input_signature(template):
    if not isinstance(template, dict):
        return None
    model = template.get("model")
    if model not in MODEL_LABELS:
        return None
    resolved_context = default_context(model, date.today())
    if isinstance(template.get("context"), dict):
        resolved_context.update(template["context"])
    if template.get("mapping_id") is not None:
        resolved_context["exchange_mapping_id"] = template["mapping_id"]
    signature = _input_signature_from_context(
        template.get("asset", DEFAULT_ASSET),
        model,
        template.get("contract_multiplier", 1),
        template.get("legs") or [default_leg(model, 1)],
        resolved_context,
        template.get("valuation_date", date.today().isoformat()),
    )
    if template.get("mapping_id") is not None:
        signature["mapping_id"] = template["mapping_id"]
    return signature


def _snapshot_matches_template(snapshot, template):
    return (
        isinstance(snapshot, dict)
        and snapshot.get("schema_version") == SCHEMA_VERSION
        and snapshot.get("_ui_input_signature")
        == _template_input_signature(template)
    )


def _default_exchange_workspace():
    return {
        "schema_version": EXCHANGE_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": 2,
        "drafts": {},
        "structures": [
            {
                "structure_id": EXCHANGE_STRUCTURE_ID,
                "label": "E1",
                "template": None,
            }
        ],
    }


def _exchange_structure_label(structure_id, fallback_sequence=1):
    prefix, separator, suffix = str(structure_id or "").rpartition("-")
    if separator and prefix == "exchange-structure" and suffix.isdigit():
        return f"E{int(suffix)}"
    return f"E{fallback_sequence}"


def _normalize_exchange_workspace(workspace):
    if not isinstance(workspace, dict):
        return _default_exchange_workspace()
    raw_structures = workspace.get("structures")
    if not isinstance(raw_structures, list):
        return _default_exchange_workspace()
    try:
        migrate_governed_mapping = (
            int(workspace.get("schema_version", 0))
            < EXCHANGE_WORKSPACE_SCHEMA_VERSION
        )
    except (TypeError, ValueError, OverflowError):
        migrate_governed_mapping = True
    structures = []
    seen = set()
    for index, raw_structure in enumerate(raw_structures, start=1):
        if not isinstance(raw_structure, dict):
            continue
        structure_id = str(raw_structure.get("structure_id") or "").strip()
        if (
            not structure_id.startswith("exchange-structure-")
            or structure_id in seen
        ):
            continue
        seen.add(structure_id)
        template = (
            copy.deepcopy(raw_structure.get("template"))
            if isinstance(raw_structure.get("template"), dict)
            else None
        )
        if template is not None:
            _migrate_template_premium_convention(
                template,
                migrate_governed_mapping=migrate_governed_mapping,
            )
        structures.append(
            {
                "structure_id": structure_id,
                "label": _exchange_structure_label(structure_id, index),
                "template": template,
            }
        )
    if not structures:
        return _default_exchange_workspace()
    numeric_sequences = []
    for structure in structures:
        suffix = structure["structure_id"].removeprefix(
            "exchange-structure-"
        )
        if suffix.isdigit():
            numeric_sequences.append(int(suffix))
    try:
        next_sequence = max(
            int(workspace.get("next_structure_sequence", 0)),
            max(numeric_sequences, default=0) + 1,
        )
    except (TypeError, ValueError, OverflowError):
        next_sequence = max(numeric_sequences, default=0) + 1
    drafts = {}
    for structure_id, template in (
        workspace.get("drafts", {}).items()
        if isinstance(workspace.get("drafts"), dict)
        else []
    ):
        if structure_id not in seen or not isinstance(template, dict):
            continue
        migrated_template = copy.deepcopy(template)
        _migrate_template_premium_convention(
            migrated_template,
            migrate_governed_mapping=migrate_governed_mapping,
        )
        drafts[structure_id] = migrated_template
    return {
        "schema_version": EXCHANGE_WORKSPACE_SCHEMA_VERSION,
        "next_structure_sequence": next_sequence,
        "drafts": drafts,
        "structures": structures,
    }


def _reduce_exchange_workspace(workspace, action, structure_id=None, template=None):
    return reduce_workspace(
        _normalize_exchange_workspace(workspace),
        action,
        structure_id,
        template,
        id_prefix="exchange-structure-",
        label_prefix="E",
    )


def reduce_workspace(normalized, action, structure_id=None, template=None, *, id_prefix, label_prefix):
    """Apply one workspace command to normalized session state without aliasing it."""
    structures = copy.deepcopy(normalized["structures"])
    drafts = copy.deepcopy(normalized["drafts"])
    next_sequence = normalized["next_structure_sequence"]
    if action in {"add", "duplicate"}:
        new_id = f"{id_prefix}{next_sequence}"
        structures.append(
            {
                "structure_id": new_id,
                "label": f"{label_prefix}{next_sequence}",
                "template": copy.deepcopy(template) if action == "duplicate" else None,
            }
        )
        if action == "duplicate" and isinstance(template, dict):
            drafts[new_id] = copy.deepcopy(template)
        next_sequence += 1
    elif action == "remove" and len(structures) > 1:
        structures = [
            structure for structure in structures if structure["structure_id"] != structure_id
        ]
        drafts.pop(structure_id, None)
    return {
        "schema_version": normalized["schema_version"],
        "next_structure_sequence": next_sequence,
        "drafts": drafts,
        "structures": structures,
    }


def _normalized_workflow(value):
    # Saved drafts from the retired workspace keep their OTC interpretation.
    if value == "legacy":
        return OTC_WORKFLOW
    return (
        value
        if value in {EXCHANGE_WORKFLOW, OTC_WORKFLOW}
        else EXCHANGE_WORKFLOW
    )
