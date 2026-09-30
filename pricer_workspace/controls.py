"""Pricer input controls and context forms."""

from __future__ import annotations

from dash import (
    dcc,
    html,
)
from datetime import (
    date,
    timedelta,
)
from pricer_exchange_registry import (
    canonical_exchange_mapping_id,
    exchange_mapping_options,
    exchange_option_mapping,
)
from pricer_structure import (
    DEFAULT_ASSET,
    MAX_OPTION_HORIZON_DAYS,
    MODEL_LABELS,
    SUPPORTED_ASSETS,
    SUPPORTED_DELIVERY_SHAPES,
    StructureValidationError,
    asset_price_spec,
    build_delivery_month_component,
    default_context,
    default_premium_convention,
)
from .constants import (
    COMPACT_DELIVERY_SHAPE_LABELS,
    DEFAULT_STRUCTURE_ID,
    FUTURES_STYLE_RATE_NOTE,
    UPFRONT_RATE_NOTE,
    asset_options,
    option_types,
    premium_convention_options,
)
from .state import (
    _context_id,
    _delivery_month_options,
    _instance_id,
    _instance_persistence,
    _migrated_kirk_context_values,
    _month_only_field_id,
    _resolved_delivery_month,
    parse_date,
)


def _build_pricer_message(message, tone="neutral"):
    return html.Div(
        message,
        className=f"pricer-empty-state pricer-empty-state-{tone}",
        role="status" if tone != "danger" else "alert",
        title=message,
    )


def _build_pricer_section_header(title, actions=None, *, heading_level=2):
    heading_component = {
        1: html.H1,
        2: html.H2,
        3: html.H3,
    }.get(heading_level, html.H2)
    return html.Div(
        [
            heading_component(
                title,
                className="section-title-inline pricer-section-title",
            ),
            html.Div(actions or [], className="pricer-section-actions"),
        ],
        className="pricer-section-header",
    )


def _build_delivery_shape_field(
    model="black76",
    structure_id=DEFAULT_STRUCTURE_ID,
    value="MONTH",
    asset=DEFAULT_ASSET,
    mapping_id=None,
):
    supports_strips = (
        (asset == "TTF" and model == "black76")
        or (asset == "JKM" and model in {"black76", "asian76"})
        or (
            asset == "NBP"
            and model == "black76"
            and canonical_exchange_mapping_id(mapping_id) == "ICE-NBP-UKF"
        )
    )
    available_shapes = (
        SUPPORTED_DELIVERY_SHAPES if supports_strips else ("MONTH",)
    )
    resolved_value = value if value in available_shapes else "MONTH"
    return _build_pricer_field(
        "Shape",
        dcc.Dropdown(
            id=_context_id(
                model,
                "delivery_shape",
                structure_id=structure_id,
            ),
            options=[
                {
                    "label": COMPACT_DELIVERY_SHAPE_LABELS[shape],
                    "value": shape,
                }
                for shape in available_shapes
            ],
            value=resolved_value,
            clearable=False,
            disabled=not supports_strips,
            persistence=f"pricer-{structure_id}-{model}-delivery-shape",
            persistence_type="session",
            className="pricer-filter-dropdown pricer-shape-dropdown",
        ),
        class_name="pricer-shape-field",
        hint=(
            "Strips use governed monthly expiries and product-specific weights."
            if mapping_id
            else (
                "Strips use exact JKM exchange expiries selected by pricing model."
                if asset == "JKM"
                else "Monthly and seasonal strips use exact TTF TFO expiries."
            )
        ),
    )


def _build_delivery_month_field(
    model="black76",
    structure_id=DEFAULT_STRUCTURE_ID,
    value=None,
    *,
    asset=DEFAULT_ASSET,
    delivery_shape="MONTH",
    as_of=None,
    mapping_id=None,
):
    as_of = parse_date(as_of, date.today())
    is_governed_month = (
        str(delivery_shape or "MONTH").strip().upper() == "MONTH"
    )
    options = _delivery_month_options(asset, model, as_of, mapping_id)
    resolved_value = _resolved_delivery_month(value, options)
    return _build_pricer_field(
        "Delivery",
        dcc.Dropdown(
            id=_context_id(
                model,
                "delivery_month",
                structure_id=structure_id,
            ),
            options=options,
            value=resolved_value,
            clearable=False,
            disabled=not is_governed_month or not options,
            persistence=f"pricer-{structure_id}-{model}-delivery-month",
            persistence_type="session",
            className="pricer-filter-dropdown pricer-delivery-month-dropdown",
        ),
        class_name="pricer-delivery-month-field",
        hint="Selects the governed monthly contract and its exchange expiry.",
        field_id=_instance_id("pricer-delivery-month-field", structure_id),
        style={} if is_governed_month else {"display": "none"},
    )


def _build_pricer_field(
    label,
    control,
    class_name=None,
    hint=None,
    *,
    field_id=None,
    style=None,
):
    classes = ["pricer-field"]
    if class_name:
        classes.append(class_name)
    properties = {"className": " ".join(classes)}
    if field_id is not None:
        properties["id"] = field_id
    if style is not None:
        properties["style"] = style
    if hint:
        properties["title"] = hint
    return html.Label(
        [
            html.Span(
                label,
                className="pricer-field-label",
                title=hint,
            ),
            control,
            (
                html.Span(
                    hint,
                    className="pricer-field-hint",
                    title=hint,
                )
                if hint
                else None
            ),
        ],
        **properties,
    )


def _delivery_year_field_style(delivery_shape):
    shape = str(delivery_shape or "MONTH").strip().upper()
    return {} if shape != "MONTH" else {"display": "none"}


def _month_only_field_style(delivery_shape):
    shape = str(delivery_shape or "MONTH").strip().upper()
    return {} if shape == "MONTH" else {"display": "none"}


def _build_pricer_number_input(
    input_id,
    value,
    *,
    minimum=None,
    maximum=None,
    step=None,
    persistence_key=None,
    disabled=False,
):
    resolved_step = "any" if step is None else step
    resolved_persistence = persistence_key or True
    if persistence_key and resolved_step == "any":
        resolved_persistence = f"{persistence_key}-step-any-v2"
    return dcc.Input(
        id=input_id,
        type="number",
        value=value,
        min=minimum,
        max=maximum,
        step=resolved_step,
        debounce=False,
        persistence=resolved_persistence,
        persistence_type="session",
        disabled=disabled,
        className="pricer-number-input",
    )


def _build_pricer_date_picker(
    picker_id,
    value,
    *,
    minimum=None,
    maximum=None,
    allow_past=False,
    persistence_key=None,
    disabled=False,
):
    resolved_minimum = minimum
    if resolved_minimum is None and not allow_past:
        resolved_minimum = date.today()
    resolved_maximum = maximum or (
        date.today() + timedelta(days=MAX_OPTION_HORIZON_DAYS)
    )
    return dcc.DatePickerSingle(
        id=picker_id,
        min_date_allowed=resolved_minimum,
        max_date_allowed=resolved_maximum,
        date=value,
        display_format="YYYY-MM-DD",
        persistence=True if persistence_key is None else persistence_key,
        persistence_type="session",
        disabled=disabled,
        className="pricer-date-picker",
    )


def _surface_proxy_note(
    mapping_id,
    *,
    asset=None,
    model=None,
    show_jkm_vanilla_surface_note=False,
):
    base_class = "pricer-surface-proxy-note"
    if mapping_id == "ICE-JKM-JKZ" or (
        show_jkm_vanilla_surface_note
        and asset == "JKM"
        and model == "black76"
        and mapping_id is None
    ):
        return (
            "JKM APO surface, expiry-adjusted to JKZ.",
            f"{base_class} pricer-jkm-vanilla-surface-note",
        )
    return "", base_class


def _build_context_form(
    model,
    structure_id=DEFAULT_STRUCTURE_ID,
    values=None,
    *,
    include_delivery_shape=True,
    asset=DEFAULT_ASSET,
    show_jkm_vanilla_surface_note=False,
    mapping_id=None,
):
    defaults = default_context(model, date.today())
    mapping = exchange_option_mapping(mapping_id)
    if model == "kirk":
        values = _migrated_kirk_context_values(values)
    if isinstance(values, dict):
        defaults.update(values)
    if mapping is not None:
        defaults["premium_convention"] = mapping.premium_convention
    elif not isinstance(values, dict) or values.get("premium_convention") in (
        None,
        "",
        "product_default",
    ):
        defaults["premium_convention"] = default_premium_convention(asset, model)
    governed_delivery_month = None
    governed_jkm_apo = False
    if (
        model != "kirk"
        and str(defaults.get("delivery_shape") or "MONTH").strip().upper()
        == "MONTH"
    ):
        delivery_options = _delivery_month_options(
            asset,
            model,
            date.today(),
            mapping_id,
        )
        requested_delivery_month = defaults.get("delivery_month")
        governed_delivery_month = _resolved_delivery_month(
            requested_delivery_month,
            delivery_options,
        )
        defaults["delivery_month"] = governed_delivery_month
        if governed_delivery_month:
            component = build_delivery_month_component(
                asset,
                model,
                governed_delivery_month,
                date.today(),
                defaults.get("forward", 1.0),
                mapping_id=mapping_id,
            )
            defaults["contract_expiration_date"] = component[
                "contract_expiration_date"
            ]
            governed_jkm_apo = asset == "JKM" and model == "asian76"
            resolved_requested_month = _resolved_delivery_month(
                requested_delivery_month,
                delivery_options,
            )
            selection_changed = resolved_requested_month != requested_delivery_month
            if governed_jkm_apo:
                defaults["averaging_start_date"] = component[
                    "averaging_start_date"
                ]
            if governed_jkm_apo or selection_changed or mapping is not None:
                defaults["expiration_date"] = component[
                    "option_expiration_date"
                ]
    is_futures_style = defaults.get("premium_convention") == "futures_style"
    persistence_prefix = f"pricer-{structure_id}-{model}"
    surface_proxy_note, surface_proxy_note_class = _surface_proxy_note(
        mapping_id,
        asset=asset,
        model=model,
        show_jkm_vanilla_surface_note=show_jkm_vanilla_surface_note,
    )
    surface_proxy_note_component = html.Span(
        surface_proxy_note,
        id=_instance_id("pricer-surface-proxy-note", structure_id),
        className=surface_proxy_note_class,
        role="note",
    )
    fields = []
    if model in {"black76", "american_futures"}:
        if include_delivery_shape:
            fields.append(
                _build_delivery_shape_field(
                    model,
                    structure_id,
                    defaults["delivery_shape"],
                    asset,
                    mapping_id,
                )
            )
        fields.extend(
            [
                _build_pricer_field(
                    "First year",
                    _build_pricer_number_input(
                        _context_id(model, "delivery_year", structure_id=structure_id),
                        defaults["delivery_year"],
                        minimum=2000,
                        maximum=2100,
                        step=1,
                        persistence_key=f"{persistence_prefix}-delivery-year",
                    ),
                    class_name="pricer-number-field",
                    hint=(
                        "Winter runs from October of the first delivery year "
                        "to March of the following year."
                    ),
                    field_id=_instance_id(
                        "pricer-delivery-year-field",
                        structure_id,
                    ),
                    style=_delivery_year_field_style(
                        defaults.get("delivery_shape")
                    ),
                ),
                _build_pricer_field(
                    "Forward",
                    _build_pricer_number_input(
                        _context_id(model, "forward", structure_id=structure_id),
                        defaults["forward"],
                        minimum=0.01,
                        persistence_key=f"{persistence_prefix}-forward",
                    ),
                    class_name="pricer-number-field pricer-forward-field",
                ),
                surface_proxy_note_component,
                _build_pricer_field(
                    "Option exp",
                    _build_pricer_date_picker(
                        _context_id(model, "expiration_date", True, structure_id),
                        defaults["expiration_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-expiration",
                        disabled=bool(mapping_id and governed_delivery_month),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only; strips derive each monthly expiry.",
                    field_id=_month_only_field_id(
                        structure_id, "option-expiration"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Exchange exp",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "contract_expiration_date",
                            True,
                            structure_id,
                        ),
                        defaults["contract_expiration_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-contract-expiration",
                        disabled=bool(mapping_id and governed_delivery_month),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only.",
                    field_id=_month_only_field_id(
                        structure_id, "contract-expiration"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Rate",
                    _build_pricer_number_input(
                        _context_id(model, "rate", structure_id=structure_id),
                        0.0 if is_futures_style else defaults["rate"],
                        minimum=-1,
                        maximum=2,
                        step=0.000001,
                        persistence_key=f"{persistence_prefix}-rate",
                        disabled=is_futures_style,
                    ),
                    class_name="pricer-number-field pricer-rate-field",
                    field_id=_instance_id("pricer-rate-field", structure_id),
                    hint=(
                        FUTURES_STYLE_RATE_NOTE
                        if is_futures_style
                        else UPFRONT_RATE_NOTE
                    ),
                ),
            ]
        )
    elif model == "asian76":
        if include_delivery_shape:
            fields.append(
                _build_delivery_shape_field(
                    model,
                    structure_id,
                    defaults["delivery_shape"],
                    asset,
                    mapping_id,
                )
            )
        fields.extend(
            [
                _build_pricer_field(
                    "First year",
                    _build_pricer_number_input(
                        _context_id(model, "delivery_year", structure_id=structure_id),
                        defaults["delivery_year"],
                        minimum=2000,
                        maximum=2100,
                        step=1,
                        persistence_key=f"{persistence_prefix}-delivery-year",
                    ),
                    class_name="pricer-number-field",
                    hint=(
                        "Winter runs from October of the first delivery year "
                        "to March of the following year."
                    ),
                    field_id=_instance_id(
                        "pricer-delivery-year-field",
                        structure_id,
                    ),
                    style=_delivery_year_field_style(
                        defaults.get("delivery_shape")
                    ),
                ),
                _build_pricer_field(
                    "Forward",
                    _build_pricer_number_input(
                        _context_id(model, "forward", structure_id=structure_id),
                        defaults["forward"],
                        minimum=0.01,
                        persistence_key=f"{persistence_prefix}-forward",
                    ),
                    class_name="pricer-number-field pricer-forward-field",
                ),
                _build_pricer_field(
                    "Avg start",
                    _build_pricer_date_picker(
                        _context_id(model, "averaging_start_date", True, structure_id),
                        defaults["averaging_start_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-averaging-start",
                        disabled=bool(mapping_id and governed_jkm_apo),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only; strips derive each monthly start.",
                    field_id=_month_only_field_id(
                        structure_id, "averaging-start"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Avg end",
                    _build_pricer_date_picker(
                        _context_id(model, "expiration_date", True, structure_id),
                        defaults["expiration_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-expiration",
                        disabled=bool(mapping_id and governed_jkm_apo),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only; strips derive each monthly expiry.",
                    field_id=_month_only_field_id(
                        structure_id, "option-expiration"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Exchange exp",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "contract_expiration_date",
                            True,
                            structure_id,
                        ),
                        defaults["contract_expiration_date"],
                        allow_past=True,
                        persistence_key=f"{persistence_prefix}-contract-expiration",
                        disabled=bool(mapping_id and governed_delivery_month),
                    ),
                    class_name="pricer-date-field",
                    hint="Used for Month only.",
                    field_id=_month_only_field_id(
                        structure_id, "contract-expiration"
                    ),
                    style=_month_only_field_style(defaults.get("delivery_shape")),
                ),
                _build_pricer_field(
                    "Rate",
                    _build_pricer_number_input(
                        _context_id(model, "rate", structure_id=structure_id),
                        0.0 if is_futures_style else defaults["rate"],
                        minimum=-1,
                        maximum=2,
                        step=0.000001,
                        persistence_key=f"{persistence_prefix}-rate",
                        disabled=is_futures_style,
                    ),
                    class_name="pricer-number-field pricer-rate-field",
                    field_id=_instance_id("pricer-rate-field", structure_id),
                    hint=(
                        FUTURES_STYLE_RATE_NOTE
                        if is_futures_style
                        else UPFRONT_RATE_NOTE
                    ),
                ),
            ]
        )
        fields.append(surface_proxy_note_component)
    elif model == "kirk":
        fields.extend(
            [
                html.Div(
                    id=_instance_id("pricer-rate-field", structure_id),
                    className="pricer-rate-field",
                    style={"display": "none"},
                ),
                _build_pricer_field(
                    "Asset 1 forward",
                    _build_pricer_number_input(
                        _context_id(
                            model,
                            "asset_1_forward",
                            structure_id=structure_id,
                        ),
                        defaults["asset_1_forward"],
                        minimum=0.01,
                        persistence_key=f"{persistence_prefix}-asset-1-forward-v1",
                    ),
                    class_name="pricer-number-field",
                ),
                _build_pricer_field(
                    "Asset 2 forward",
                    _build_pricer_number_input(
                        _context_id(
                            model,
                            "asset_2_forward",
                            structure_id=structure_id,
                        ),
                        defaults["asset_2_forward"],
                        minimum=0.01,
                        persistence_key=f"{persistence_prefix}-asset-2-forward-v1",
                    ),
                    class_name="pricer-number-field",
                ),
                _build_pricer_field(
                    "Asset 1 vol reference exp",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "asset_1_reference_expiry",
                            True,
                            structure_id,
                        ),
                        defaults["asset_1_reference_expiry"],
                        allow_past=True,
                        persistence_key=(
                            f"{persistence_prefix}-asset-1-reference-expiry-v1"
                        ),
                    ),
                    class_name="pricer-date-field",
                ),
                _build_pricer_field(
                    "Asset 2 vol reference exp",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "asset_2_reference_expiry",
                            True,
                            structure_id,
                        ),
                        defaults["asset_2_reference_expiry"],
                        allow_past=True,
                        persistence_key=(
                            f"{persistence_prefix}-asset-2-reference-expiry-v1"
                        ),
                    ),
                    class_name="pricer-date-field",
                ),
                _build_pricer_field(
                    "Contractual option expiry",
                    _build_pricer_date_picker(
                        _context_id(
                            model,
                            "contractual_expiry",
                            True,
                            structure_id,
                        ),
                        defaults["contractual_expiry"],
                        allow_past=True,
                        persistence_key=(
                            f"{persistence_prefix}-contractual-expiry-v1"
                        ),
                    ),
                    class_name="pricer-date-field",
                ),
                _build_pricer_field(
                    "Corr",
                    _build_pricer_number_input(
                        _context_id(model, "correlation", structure_id=structure_id),
                        defaults["correlation"],
                        minimum=-1,
                        maximum=1,
                        step=0.00001,
                        persistence_key=f"{persistence_prefix}-correlation",
                    ),
                    class_name="pricer-number-field",
                ),
                html.Div(
                    "Kirk is undiscounted, so rate and Rho are not applicable. "
                    "Both volatility inputs are resolved from the governed "
                    "50-call-delta anchors at their selected reference expiries; "
                    "correlation remains an explicit input.",
                    className="pricer-inline-method-note",
                    role="note",
                ),
            ]
        )
        fields.append(surface_proxy_note_component)
    return html.Div(fields, className="pricer-context-grid")


def _build_pricing_model_field(
    structure_id=DEFAULT_STRUCTURE_ID,
    value="black76",
    *,
    workflow="legacy",
):
    exchange_workflow = workflow == "exchange"
    model_options = (
        [{"label": MODEL_LABELS[value], "value": value}]
        if exchange_workflow
        else option_types
    )
    return _build_pricer_field(
        html.Span(
            [
                html.Span("Model", className="pricer-field-label-otc"),
                html.Span(
                    "Product",
                    className="pricer-field-label-exchange",
                ),
            ]
        ),
        dcc.Dropdown(
            id=_instance_id("pricer-option-type", structure_id),
            options=model_options,
            value=value,
            clearable=False,
            disabled=exchange_workflow,
            persistence=_instance_persistence(structure_id, "model"),
            persistence_type="session",
            className="pricer-filter-dropdown pricer-option-type-dropdown",
        ),
        class_name="pricer-model-field",
        field_id=_instance_id("pricer-model-field", structure_id),
        hint=(
            "Kirk is undiscounted, so rate and Rho are not applicable. It "
            "requires two input vols; PREMIUM quoting is unavailable because "
            "one premium cannot determine both vols."
            if value == "kirk"
            else None
        ),
    )


def _build_asset_field(
    structure_id=DEFAULT_STRUCTURE_ID,
    value=DEFAULT_ASSET,
    *,
    style=None,
):
    return _build_pricer_field(
        "Asset",
        dcc.Dropdown(
            id=_instance_id("pricer-asset", structure_id),
            options=asset_options,
            value=value,
            clearable=False,
            persistence=_instance_persistence(structure_id, "asset"),
            persistence_type="session",
            className="pricer-filter-dropdown pricer-asset-dropdown",
        ),
        class_name="pricer-asset-field",
        field_id=_instance_id("pricer-asset-field", structure_id),
        style=style,
        hint="Selects the governed variance calendar for this asset.",
    )


def _build_mapping_id_field(
    structure_id=DEFAULT_STRUCTURE_ID,
    value=None,
    *,
    style=None,
):
    return _build_pricer_field(
        "Mapping ID",
        dcc.Dropdown(
            id=_instance_id("pricer-mapping-id", structure_id),
            options=exchange_mapping_options(),
            value=value,
            clearable=False,
            persistence=_instance_persistence(structure_id, "mapping-id-v2"),
            persistence_type="session",
            className="pricer-filter-dropdown pricer-mapping-id-dropdown",
        ),
        class_name="pricer-mapping-id-field",
        field_id=_instance_id("pricer-mapping-id-field", structure_id),
        style=style,
        hint="Canonical exchange-option identifier from the Product Registry.",
    )


def _build_price_unit_field(
    structure_id=DEFAULT_STRUCTURE_ID,
    asset=DEFAULT_ASSET,
    *,
    mapping_id=None,
    style=None,
):
    try:
        spec = asset_price_spec(asset, mapping_id)
        value = spec["price_unit_label"]
        description = spec["description"]
    except StructureValidationError:
        value = "—"
        description = "Price currency and unit are unavailable."
    return html.Div(
        [
            html.Span("Price unit", className="pricer-field-label"),
            html.Div(
                value,
                id=_instance_id("pricer-price-unit", structure_id),
                className="pricer-price-unit-value",
                title=description,
                **{"aria-live": "polite"},
            ),
        ],
        id=_instance_id("pricer-price-unit-field", structure_id),
        className="pricer-field pricer-price-unit-field",
        title="Selected asset price currency and unit.",
        style=style,
    )


def _build_kirk_asset_identity_fields(
    structure_id,
    values,
):
    defaults = default_context("kirk", date.today())
    if isinstance(values, dict):
        defaults.update(values)
    fields = []
    for asset_number in (1, 2):
        code_key = f"asset_{asset_number}_code"
        selected_asset = defaults.get(code_key)
        try:
            if selected_asset not in SUPPORTED_ASSETS:
                raise StructureValidationError("Asset selection is required.")
            spec = asset_price_spec(selected_asset)
            unit_value = spec["price_unit_label"]
            unit_description = spec["description"]
        except StructureValidationError:
            unit_value = "—"
            unit_description = "Select an asset to show its price unit."
        fields.extend(
            [
                _build_pricer_field(
                    f"Asset {asset_number}",
                    dcc.Dropdown(
                        id=_context_id(
                            "kirk",
                            code_key,
                            structure_id=structure_id,
                        ),
                        options=asset_options,
                        value=selected_asset,
                        clearable=True,
                        placeholder="Select",
                        persistence=_instance_persistence(
                            structure_id,
                            f"kirk-{code_key}-v1",
                        ),
                        persistence_type="session",
                        className="pricer-filter-dropdown pricer-asset-dropdown",
                    ),
                    class_name="pricer-asset-field pricer-kirk-asset-field",
                    hint=(
                        f"Select Asset {asset_number} explicitly; its governed "
                        "variance calendar is used only for that asset's volatility."
                    ),
                ),
                html.Div(
                    [
                        html.Span("Price unit", className="pricer-field-label"),
                        html.Div(
                            unit_value,
                            id={
                                "type": "pricer-kirk-price-unit",
                                "structure_id": structure_id,
                                "asset_number": asset_number,
                            },
                            className="pricer-price-unit-value",
                            title=unit_description,
                            **{"aria-live": "polite"},
                        ),
                    ],
                    className=(
                        "pricer-field pricer-price-unit-field "
                        "pricer-kirk-price-unit-field"
                    ),
                    title=f"Asset {asset_number} price currency and unit.",
                ),
            ]
        )
    return fields


def _build_premium_convention_field(
    model="black76",
    structure_id=DEFAULT_STRUCTURE_ID,
    value=None,
    asset=DEFAULT_ASSET,
):
    if value in (None, "", "product_default"):
        value = default_premium_convention(asset, model)
    options = premium_convention_options
    if model == "kirk":
        options = [option for option in options if option["value"] != "upfront"]
        if value == "upfront":
            value = "futures_style"
    return _build_pricer_field(
        "Premium",
        dcc.Dropdown(
            id=_context_id(model, "premium_convention", structure_id=structure_id),
            options=options,
            value=value,
            clearable=False,
            persistence=_instance_persistence(
                structure_id, f"{model}-premium-convention-v2"
            ),
            persistence_type="session",
            className="pricer-filter-dropdown pricer-premium-convention-dropdown",
        ),
        class_name="pricer-premium-convention-field",
        hint=(
            "Asset selection sets the exchange default. Futures-style is "
            "undiscounted; Upfront uses the risk-free rate."
        ),
    )


def _build_structure_header_context(
    model,
    structure_id=DEFAULT_STRUCTURE_ID,
    values=None,
    asset=DEFAULT_ASSET,
    mapping_id=None,
):
    if model == "kirk":
        values = _migrated_kirk_context_values(values)
    defaults = default_context(model, date.today())
    defaults["premium_convention"] = default_premium_convention(asset, model)
    if isinstance(values, dict):
        defaults.update(values)
    mapping = exchange_option_mapping(mapping_id)
    if mapping is not None:
        defaults["premium_convention"] = mapping.premium_convention
    elif defaults.get("premium_convention") in (None, "", "product_default"):
        defaults["premium_convention"] = default_premium_convention(asset, model)
    fields = []
    if model == "kirk":
        fields.extend(_build_kirk_asset_identity_fields(structure_id, defaults))
    fields.append(
        _build_premium_convention_field(
            model,
            structure_id,
            defaults["premium_convention"],
            asset,
        )
    )
    if model == "kirk":
        return fields
    fields.append(
        _build_delivery_shape_field(
            model,
            structure_id,
            defaults["delivery_shape"],
            asset,
            mapping_id,
        )
    )
    fields.append(
        _build_delivery_month_field(
            model,
            structure_id,
            defaults.get("delivery_month"),
            asset=asset,
            delivery_shape=defaults["delivery_shape"],
            mapping_id=mapping_id,
        )
    )
    return fields


def _contract_size_hint():
    return (
        "For Kirk this is the editable notional and defaults to one unit. "
        "For single-asset models it is the editable quantity in the denominator "
        "unit shown under Price unit. "
        "TTF defaults to one ICE lot (1 MW across the exact delivery hours); "
        "JKM to 10,000 MMBtu/month; Brent to 1,000 bbl; Henry Hub to 2,500 "
        "MMBtu; and NBP to 1,000 therm/day across the delivery month. Enter "
        "another positive number to override it."
    )
