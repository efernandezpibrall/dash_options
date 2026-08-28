"""Canonical Mapping ID metadata for the current exchange-option pricer UI."""

from dataclasses import dataclass


USER_INPUT_FORWARD_SOURCE = "USER_INPUT"
EXCHANGE_MAPPING_ID_ALIASES = {"ICE-HH-CURRENT": "ICE-HH-PHE"}


@dataclass(frozen=True)
class ExchangeOptionMapping:
    mapping_id: str
    asset: str
    product: str
    model: str
    premium_convention: str
    contract_size: float
    implementation_status: str
    pricing_supported: bool = False
    contract_convention_code: str | None = None
    expiry_convention_code: str | None = None
    surface_product: str | None = None
    volatility_surface_source: str | None = None
    forward_source: str = USER_INPUT_FORWARD_SOURCE
    sizing_mode: str = "fixed"
    max_surface_extension_days: int = 0
    default_priority: int = 100
    exchange_product_code: str | None = None
    price_currency: str | None = None
    price_unit: str | None = None
    price_unit_label: str | None = None
    currency_conversion_factor: float | None = None


def _mapping(
    mapping_id,
    asset,
    product,
    model,
    premium_convention,
    contract_size,
    implementation_status,
    pricing_supported=False,
    **metadata,
):
    return ExchangeOptionMapping(
        mapping_id=mapping_id,
        asset=asset,
        product=product,
        model=model,
        premium_convention=premium_convention,
        contract_size=contract_size,
        implementation_status=implementation_status,
        pricing_supported=pricing_supported,
        **metadata,
    )


EXCHANGE_OPTION_MAPPINGS = (
    _mapping(
        "ICE-TTF-TFO",
        "TTF",
        "ICE TTF option",
        "black76",
        "futures_style",
        1.0,
        "Ready",
        True,
        contract_convention_code="ICE_TTF_TFO",
        expiry_convention_code="ICE_TTF_TFO_71085679_EXPIRY",
        surface_product="TTF",
        volatility_surface_source="ICE_TTF_TFO",
        sizing_mode="ttf_delivery_hours",
    ),
    _mapping(
        "ICE-JKM-APO",
        "JKM",
        "JKM average price option",
        "asian76",
        "futures_style",
        10_000.0,
        "Ready",
        True,
        contract_convention_code="ICE_JKM_71090519",
        expiry_convention_code="ICE_JKM_71090519_EXPIRY",
        surface_product="JKM",
        volatility_surface_source="ICE_JKM_APO",
        sizing_mode="jkm_monthly_lots",
    ),
    _mapping(
        "ICE-JKM-JKZ",
        "JKM",
        "JKM vanilla option",
        "black76",
        "futures_style",
        10_000.0,
        "Ready",
        True,
        contract_convention_code="ICE_JKM_JKZ",
        expiry_convention_code="ICE_JKM_JKZ_VIA_TFO",
        surface_product="JKM",
        volatility_surface_source="ICE_JKM_APO",
        sizing_mode="jkm_monthly_lots",
    ),
    _mapping(
        "ICE-HH-PHE",
        "HH",
        "ICE Henry Hub PHE option",
        "black76",
        "upfront",
        2_500.0,
        "Ready",
        True,
        contract_convention_code="ICE_HH_PHE_6590274",
        expiry_convention_code="ICE_HH_PHE_6590274_EXPIRY",
        surface_product="HH",
        volatility_surface_source="CME_HH_LNE",
        max_surface_extension_days=1,
        default_priority=10,
    ),
    _mapping(
        "ICE-BRENT-B",
        "Brent",
        "ICE Brent option",
        "black76",
        "futures_style",
        1_000.0,
        "Ready",
        True,
        contract_convention_code="ICE_BRENT_AMERICAN_218",
        expiry_convention_code="ICE_BRENT_218_EXPIRY",
        surface_product="BRENT",
        volatility_surface_source="ICE_BRENT",
    ),
    _mapping(
        "ICE-NBP-UKF",
        "NBP",
        "ICE NBP option",
        "black76",
        "futures_style",
        1_000.0,
        "Ready",
        True,
        contract_convention_code="ICE_NBP_UKF_71085728",
        expiry_convention_code="ICE_NBP_UKF_71085728_EXPIRY",
        surface_product="NBP",
        volatility_surface_source="ICE_NBP_UKF",
        sizing_mode="nbp_delivery_days",
    ),
    _mapping(
        "CME-TTF-TFO",
        "TTF",
        "Dutch TTF Futures-Style Calendar Month Option",
        "black76",
        "futures_style",
        1.0,
        "Ready",
        True,
        contract_convention_code="CME_TTF_TFO_1162",
        expiry_convention_code="CME_TTF_TFO_1162_EXPIRY",
        surface_product="TTF",
        volatility_surface_source="ICE_TTF_TFO",
        sizing_mode="ttf_delivery_hours",
    ),
    _mapping(
        "CME-TTF-TTO",
        "TTF",
        "Dutch TTF Calendar Month Option",
        "black76",
        "upfront",
        1.0,
        "Ready",
        True,
        contract_convention_code="CME_TTF_TTO_1161",
        expiry_convention_code="CME_TTF_TTO_1161_EXPIRY",
        surface_product="TTF",
        volatility_surface_source="ICE_TTF_TFO",
        sizing_mode="ttf_delivery_hours",
    ),
    _mapping(
        "CME-TTF-TTL",
        "TTF",
        "Dutch TTF Financial Calendar Month Option",
        "black76",
        "futures_style",
        1.0,
        "Ready",
        True,
        contract_convention_code="CME_TTF_TTL_1016",
        expiry_convention_code="CME_TTF_TTL_1016_EXPIRY",
        surface_product="TTF",
        volatility_surface_source="ICE_TTF_TFO",
        sizing_mode="ttf_delivery_hours",
        exchange_product_code="TTL",
    ),
    _mapping(
        "CME-TTF-TFP",
        "TTF",
        "Dutch TTF USD/MMBtu Average Price Option",
        "black76",
        "upfront",
        10_000.0,
        "Ready",
        True,
        contract_convention_code="CME_TTF_TFP_1018",
        expiry_convention_code="CME_TTF_TFP_1018_EXPIRY",
        surface_product="TTF",
        volatility_surface_source="ICE_TTF_TFO",
        sizing_mode="monthly_contract_lots",
        max_surface_extension_days=3,
        exchange_product_code="TFP",
        price_currency="USD",
        price_unit="MMBtu",
        price_unit_label="USD/MMBtu",
        currency_conversion_factor=1.0,
    ),
    _mapping(
        "CME-TTF-TFF",
        "TTF",
        "Dutch TTF Futures-Style Average Price Option",
        "black76",
        "futures_style",
        10_000.0,
        "Ready",
        True,
        contract_convention_code="CME_TTF_TFF_1019",
        expiry_convention_code="CME_TTF_TFF_1019_EXPIRY",
        surface_product="TTF",
        volatility_surface_source="ICE_TTF_TFO",
        sizing_mode="monthly_contract_lots",
        max_surface_extension_days=3,
        exchange_product_code="TFF",
        price_currency="USD",
        price_unit="MMBtu",
        price_unit_label="USD/MMBtu",
        currency_conversion_factor=1.0,
    ),
    _mapping(
        "CME-JKM-JKO",
        "JKM",
        "JKM Average Price Option",
        "asian76",
        "upfront",
        10_000.0,
        "Ready",
        True,
        contract_convention_code="CME_JKM_JKO_869",
        expiry_convention_code="CME_JKM_JKO_869_EXPIRY",
        surface_product="JKM",
        volatility_surface_source="ICE_JKM_APO",
        sizing_mode="monthly_contract_lots",
        exchange_product_code="JKO",
    ),
    _mapping(
        "CME-JKM-JFO",
        "JKM",
        "JKM Futures-Style Average Price Option",
        "asian76",
        "futures_style",
        10_000.0,
        "Ready",
        True,
        contract_convention_code="CME_JKM_JFO_864",
        expiry_convention_code="CME_JKM_JFO_864_EXPIRY",
        surface_product="JKM",
        volatility_surface_source="ICE_JKM_APO",
        sizing_mode="monthly_contract_lots",
        exchange_product_code="JFO",
    ),
    _mapping(
        "CME-HH-ON",
        "HH",
        "Henry Hub Natural Gas Option",
        "american_futures",
        "upfront",
        10_000.0,
        "Ready",
        True,
        contract_convention_code="CME_HH_ON_370",
        expiry_convention_code="CME_HH_ON_370_EXPIRY",
        surface_product="HH",
        volatility_surface_source="CME_HH_LNE",
        exchange_product_code="ON",
    ),
    _mapping(
        "CME-HH-LNE",
        "HH",
        "Henry Hub European Financial Option",
        "black76",
        "upfront",
        10_000.0,
        "Ready",
        True,
        contract_convention_code="CME_HH_LNE_560",
        expiry_convention_code="CME_HH_LNE_560_EXPIRY",
        surface_product="HH",
        volatility_surface_source="CME_HH_LNE",
        default_priority=0,
    ),
    _mapping(
        "CME-BRENT-BE",
        "Brent",
        "Brent Last Day Financial European Option",
        "black76",
        "upfront",
        1_000.0,
        "Ready",
        True,
        contract_convention_code="CME_BRENT_BE_378",
        expiry_convention_code="CME_BRENT_BE_378_EXPIRY",
        surface_product="BRENT",
        volatility_surface_source="ICE_BRENT",
    ),
    _mapping(
        "CME-BRENT-BZO",
        "Brent",
        "Brent Crude Oil Futures-Style Margin Option",
        "black76",
        "futures_style",
        1_000.0,
        "Ready",
        True,
        contract_convention_code="CME_BRENT_BZO_504",
        expiry_convention_code="CME_BRENT_BZO_504_EXPIRY",
        surface_product="BRENT",
        volatility_surface_source="ICE_BRENT",
    ),
    _mapping(
        "CME-NBP-UKO",
        "NBP",
        "UK NBP Calendar Month Option",
        "black76",
        "upfront",
        1_000.0,
        "Ready",
        True,
        contract_convention_code="CME_NBP_UKO_1163",
        expiry_convention_code="CME_NBP_UKO_1163_EXPIRY",
        surface_product="NBP",
        volatility_surface_source="ICE_NBP_UKF",
        sizing_mode="nbp_delivery_days",
    ),
    _mapping(
        "CME-NBP-UFO",
        "NBP",
        "UK NBP Futures-Style Calendar Month Option",
        "black76",
        "futures_style",
        1_000.0,
        "Ready",
        True,
        contract_convention_code="CME_NBP_UFO_1164",
        expiry_convention_code="CME_NBP_UFO_1164_EXPIRY",
        surface_product="NBP",
        volatility_surface_source="ICE_NBP_UKF",
        sizing_mode="nbp_delivery_days",
    ),
)

EXCHANGE_OPTION_MAPPING_BY_ID = {mapping.mapping_id: mapping for mapping in EXCHANGE_OPTION_MAPPINGS}
DEFAULT_EXCHANGE_MAPPING_ID = "ICE-TTF-TFO"


def canonical_exchange_mapping_id(mapping_id):
    normalized = str(mapping_id or "").strip()
    return EXCHANGE_MAPPING_ID_ALIASES.get(normalized, normalized)


def exchange_option_mapping(mapping_id):
    return EXCHANGE_OPTION_MAPPING_BY_ID.get(canonical_exchange_mapping_id(mapping_id))


def exchange_mapping_for_asset_model(asset, model):
    candidates = [
        mapping
        for mapping in EXCHANGE_OPTION_MAPPINGS
        if mapping.pricing_supported and mapping.asset == asset and mapping.model == model
    ]
    return min(candidates, key=lambda mapping: mapping.default_priority, default=None)


def exchange_mapping_options():
    return [
        {
            "label": mapping.mapping_id,
            "value": mapping.mapping_id,
            "title": f"{mapping.product} · {mapping.implementation_status}",
        }
        for mapping in EXCHANGE_OPTION_MAPPINGS
    ]


def exchange_mapping_pricing_supported(mapping_id):
    mapping = exchange_option_mapping(mapping_id)
    return bool(mapping and mapping.pricing_supported)


def exchange_mapping_capture_message(mapping_id):
    mapping = exchange_option_mapping(mapping_id)
    if mapping is None:
        return "Select a valid Mapping ID from the Product Registry."
    return (
        f"{mapping.mapping_id} is captured in the Product Registry, but its "
        "contract-specific pricing conventions are not implemented yet."
    )
