"""Govern generic Black-76, Asian-76, and American option families.

Revision ID: 20260902_01
Revises: 20260831_04
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op


revision = "20260902_01"
down_revision = "20260831_04"
branch_labels = None
depends_on = None

SCHEMA = "at_lng"

# This immutable manifest mirrors options.option_contract_conventions.  The
# valuation runtime audits every booked code against it and fails on drift.
CONVENTIONS = (
    ("Q2K-B301-MINUS1-M-MPLUS1-V1", "OTC", "BRENT301", "kirk", "european", "futures_style", "physical_delivery", "USD", False, "internal_contract_terms", None, None),
    ("ICE_BRENT_AMERICAN_218", "ICE", "B", "american_futures", "american", "futures_style", "futures_delivery", "USD", False, "https://www.ice.com/products/218/Brent-Crude-American-style-Option", "black76", None),
    ("CME_BRENT_BZO_504", "CME", "BZO", "american_futures", "american", "futures_style", "futures_delivery", "USD", False, "https://www.cmegroup.com/rulebook/NYMEX/5/504.pdf", "black76", None),
    ("CME_BRENT_BE_378", "CME", "BE", "black76", "european", "equity_style", "cash", "USD", True, "https://www.cmegroup.com/rulebook/NYMEX/3/378.pdf", None, None),
    ("ICE_TTF_TFO", "ICE", "TFO", "black76", "european", "futures_style", "futures_delivery", "EUR", False, "https://www.ice.com/products/71085679", None, None),
    ("ICE_JKM_71090519", "ICE", "JKM", "asian76", "european", "futures_style", "futures_delivery", "USD", False, "https://www.ice.com/products/71090519/JKM-LNG-Platts-Average-Price-Options", None, "JKM_CONTINUOUS_16_TO_EXPIRY_V1"),
    ("ICE_JKM_JKZ", "ICE", "JKZ", "black76", "european", "futures_style", "futures_delivery", "USD", False, "https://www.ice.com/publicdocs/circulars/26019.pdf", None, None),
    ("ICE_NBP_UKF_71085728", "ICE", "UKF", "black76", "european", "futures_style", "futures_delivery", "GBP", False, "https://www.ice.com/products/71085728/UK-NBP-Natural-Gas-Options-Futures-Style-Margin", None, None),
    ("ICE_HH_PHE_6590274", "ICE", "PHE", "black76", "european", "equity_style", "futures_delivery", "USD", True, "https://www.ice.com/products/6590274/Option-on-Henry-Penultimate-Fixed-Price-Future", None, None),
    ("CME_HH_LNE_560", "CME", "LNE", "black76", "european", "equity_style", "cash", "USD", True, "https://www.cmegroup.com/rulebook/NYMEX/5/560.pdf", None, None),
    ("CME_TTF_TTO_1161", "CME", "TTO", "black76", "european", "equity_style", "futures_delivery", "EUR", True, "https://www.cmegroup.com/rulebook/NYMEX/11/1161.pdf", None, None),
    ("CME_TTF_TFO_1162", "CME", "TFO", "black76", "european", "futures_style", "futures_delivery", "EUR", False, "https://www.cmegroup.com/rulebook/NYMEX/11/1162.pdf", None, None),
    ("CME_TTF_TTL_1016", "CME", "TTL", "black76", "european", "futures_style", "cash", "EUR", False, "https://www.cmegroup.com/rulebook/NYMEX/10/1016.pdf", None, None),
    ("CME_TTF_TFP_1018", "CME", "TFP", "black76", "european", "equity_style", "cash", "USD", True, "https://www.cmegroup.com/rulebook/NYMEX/10/1018.pdf", None, None),
    ("CME_TTF_TFF_1019", "CME", "TFF", "black76", "european", "futures_style", "cash", "USD", False, "https://www.cmegroup.com/rulebook/NYMEX/10/1019.pdf", None, None),
    ("CME_JKM_JKO_869", "CME", "JKO", "asian76", "european", "equity_style", "cash", "USD", True, "https://www.cmegroup.com/rulebook/NYMEX/8/869.pdf", None, "JKM_PLATTS_PUBLICATION_DAYS_16_TO_15_V1"),
    ("CME_JKM_JFO_864", "CME", "JFO", "asian76", "european", "futures_style", "cash", "USD", False, "https://www.cmegroup.com/rulebook/NYMEX/8/864.pdf", None, "JKM_PLATTS_PUBLICATION_DAYS_16_TO_15_V1"),
    ("CME_HH_ON_370", "CME", "ON", "american_futures", "american", "equity_style", "futures_delivery", "USD", True, "https://www.cmegroup.com/rulebook/NYMEX/3/370.pdf", None, None),
    ("CME_NBP_UKO_1163", "CME", "UKO", "black76", "european", "equity_style", "futures_delivery", "GBP", True, "https://www.cmegroup.com/rulebook/NYMEX/11/1163.pdf", None, None),
    ("CME_NBP_UFO_1164", "CME", "UFO", "black76", "european", "futures_style", "futures_delivery", "GBP", False, "https://www.cmegroup.com/rulebook/NYMEX/11/1164.pdf", None, None),
    ("CME_WTI_LO", "CME", "LO", "american_futures", "american", "equity_style", "futures_delivery", "USD", True, "https://www.cmegroup.com/rulebook/NYMEX/3/310.pdf", None, None),
    ("CME_WTI_WEEKLY", "CME", "WTI_WEEKLY", "american_futures", "american", "equity_style", "futures_delivery", "USD", True, "https://www.cmegroup.com/rulebook/NYMEX/10/1011.pdf", None, None),
    ("OTC_AMERICAN_FUTURES_EQUITY", "OTC", "OTC_AMERICAN_FUTURES", "american_futures", "american", "equity_style", "futures_delivery", "CONTRACT_CURRENCY", True, "internal_contract_terms", None, None),
    ("OTC_AMERICAN_FUTURES_STYLE", "OTC", "OTC_AMERICAN_FUTURES", "american_futures", "american", "futures_style", "futures_delivery", "CONTRACT_CURRENCY", False, "internal_contract_terms", None, None),
)

NEW_CODES = tuple(
    row[0]
    for row in CONVENTIONS
    if row[0] not in {
        "Q2K-B301-MINUS1-M-MPLUS1-V1",
        "ICE_BRENT_AMERICAN_218",
        "ICE_TTF_TFO",
        "ICE_JKM_71090519",
        "ICE_NBP_UKF_71085728",
        "ICE_HH_PHE_6590274",
        "CME_HH_LNE_560",
        "CME_WTI_LO",
        "CME_WTI_WEEKLY",
        "OTC_AMERICAN_FUTURES_EQUITY",
        "OTC_AMERICAN_FUTURES_STYLE",
    }
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "option_contract_conventions",
        sa.Column("pricer_pricing_model", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "option_contract_conventions",
        sa.Column("asian_averaging_rule_code", sa.Text(), nullable=True),
        schema=SCHEMA,
    )

    statement = sa.text(
        f"""
        INSERT INTO {SCHEMA}.option_contract_conventions (
            convention_code, venue, product_code, pricing_model,
            exercise_style, margin_style, settlement_type, premium_currency,
            discount_curve_required, source_url, pricer_pricing_model,
            asian_averaging_rule_code, reviewed_at, active
        ) VALUES (
            :convention_code, :venue, :product_code, :pricing_model,
            :exercise_style, :margin_style, :settlement_type, :premium_currency,
            :discount_curve_required, :source_url, :pricer_pricing_model,
            :asian_averaging_rule_code, now(), true
        )
        ON CONFLICT (convention_code) DO UPDATE SET
            venue = EXCLUDED.venue,
            product_code = EXCLUDED.product_code,
            pricing_model = EXCLUDED.pricing_model,
            exercise_style = EXCLUDED.exercise_style,
            margin_style = EXCLUDED.margin_style,
            settlement_type = EXCLUDED.settlement_type,
            premium_currency = EXCLUDED.premium_currency,
            discount_curve_required = EXCLUDED.discount_curve_required,
            source_url = EXCLUDED.source_url,
            pricer_pricing_model = EXCLUDED.pricer_pricing_model,
            asian_averaging_rule_code = EXCLUDED.asian_averaging_rule_code,
            reviewed_at = now(),
            active = true
        """
    )
    bind = op.get_bind()
    columns = (
        "convention_code", "venue", "product_code", "pricing_model",
        "exercise_style", "margin_style", "settlement_type",
        "premium_currency", "discount_curve_required", "source_url",
        "pricer_pricing_model", "asian_averaging_rule_code",
    )
    for row in CONVENTIONS:
        bind.execute(statement, dict(zip(columns, row)))

    op.create_check_constraint(
        "option_contract_conventions_asian_rule_ck",
        "option_contract_conventions",
        "(pricing_model = 'asian76' AND asian_averaging_rule_code IS NOT NULL) "
        "OR (pricing_model <> 'asian76' AND asian_averaging_rule_code IS NULL)",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "option_contract_conventions_approved_pricer_ck",
        "option_contract_conventions",
        "pricer_pricing_model IS NULL OR "
        "(pricing_model = 'american_futures' AND exercise_style = 'american' "
        "AND pricer_pricing_model = 'black76')",
        schema=SCHEMA,
    )

    op.drop_constraint(
        "trades_options_supported_convention_ck",
        "trades_options",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "trades_options_supported_convention_ck",
        "trades_options",
        "lower(replace(replace(coalesce(model, ''), '_', ''), '-', '')) "
        "NOT IN ('black76', 'asian76', 'americanfutures') "
        "OR contract_convention_code IS NOT NULL",
        schema=SCHEMA,
    )

    op.add_column(
        "trades_options_valuation",
        sa.Column("pricing_model_application", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "trades_options_valuation",
        sa.Column("model_horizon_date", sa.Date(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "trades_options_valuation",
        sa.Column(
            "discount_factor_to_model_horizon",
            sa.Numeric(precision=30, scale=15),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "trades_options_valuation",
        sa.Column("asian_averaging_schedule", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "trades_options_valuation_model_application_ck",
        "trades_options_valuation",
        "pricing_model_application IS NULL OR pricing_model_application IN "
        "('contractual', 'approved_approximation')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "trades_options_valuation_model_horizon_discount_ck",
        "trades_options_valuation",
        "discount_factor_to_model_horizon IS NULL OR "
        "(discount_factor_to_model_horizon <> 'NaN'::numeric AND "
        "discount_factor_to_model_horizon > 0)",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "trades_options_valuation_asian_schedule_ck",
        "trades_options_valuation",
        "lower(replace(replace(coalesce(model, ''), '_', ''), '-', '')) "
        "<> 'asian76' OR (asian_averaging_schedule IS NOT NULL AND "
        "jsonb_typeof(asian_averaging_schedule) = 'object' AND "
        "model_horizon_date IS NOT NULL AND "
        "pricing_model_application = 'contractual')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM {SCHEMA}.trades_options_valuation
                WHERE pricing_model_application IS NOT NULL
                   OR model_horizon_date IS NOT NULL
                   OR discount_factor_to_model_horizon IS NOT NULL
                   OR asian_averaging_schedule IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM {SCHEMA}.trades_options
                WHERE contract_convention_code = ANY(:new_codes)
                   OR lower(replace(replace(coalesce(model, ''), '_', ''), '-', '')) = 'asian76'
            )
            """
        ),
        {"new_codes": list(NEW_CODES)},
    ).scalar_one()
    if used:
        raise RuntimeError(
            "20260902_01 cannot be downgraded after generic-family rows or "
            "valuations have been created"
        )

    for name in (
        "trades_options_valuation_asian_schedule_ck",
        "trades_options_valuation_model_horizon_discount_ck",
        "trades_options_valuation_model_application_ck",
    ):
        op.drop_constraint(
            name,
            "trades_options_valuation",
            schema=SCHEMA,
            type_="check",
        )
    for column in (
        "asian_averaging_schedule",
        "discount_factor_to_model_horizon",
        "model_horizon_date",
        "pricing_model_application",
    ):
        op.drop_column("trades_options_valuation", column, schema=SCHEMA)

    op.drop_constraint(
        "trades_options_supported_convention_ck",
        "trades_options",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "trades_options_supported_convention_ck",
        "trades_options",
        "lower(replace(replace(coalesce(model, ''), '_', ''), '-', '')) "
        "NOT IN ('black76', 'americanfutures') "
        "OR contract_convention_code IS NOT NULL",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "option_contract_conventions_approved_pricer_ck",
        "option_contract_conventions",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "option_contract_conventions_asian_rule_ck",
        "option_contract_conventions",
        schema=SCHEMA,
        type_="check",
    )
    # Remove and restore the FK around deletion of conventions introduced by
    # this revision.  The shared table-owner role can manage both tables, while
    # the inherited FK trigger owner may not have schema USAGE during a
    # rehearsal downgrade.
    op.drop_constraint(
        "trades_options_contract_convention_fk",
        "trades_options",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_constraint(
        "mapping_volatility_surface_calendar_contract_fk",
        "mapping_volatility_surface_calendar",
        schema=SCHEMA,
        type_="foreignkey",
    )
    bind.execute(
        sa.text(
            f"DELETE FROM {SCHEMA}.option_contract_conventions "
            "WHERE convention_code = ANY(:new_codes)"
        ),
        {"new_codes": list(NEW_CODES)},
    )
    op.create_foreign_key(
        "trades_options_contract_convention_fk",
        "trades_options",
        "option_contract_conventions",
        ["contract_convention_code"],
        ["convention_code"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_foreign_key(
        "mapping_volatility_surface_calendar_contract_fk",
        "mapping_volatility_surface_calendar",
        "option_contract_conventions",
        ["contract_convention_code"],
        ["convention_code"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.option_contract_conventions
            SET product_code = CASE convention_code
                    WHEN 'ICE_BRENT_AMERICAN_218' THEN '218'
                    WHEN 'ICE_JKM_71090519' THEN '71090519'
                    WHEN 'ICE_NBP_UKF_71085728' THEN '71085728'
                    ELSE product_code
                END,
                pricing_model = CASE convention_code
                    WHEN 'ICE_JKM_71090519' THEN 'black76'
                    ELSE pricing_model
                END
            WHERE convention_code IN (
                'ICE_BRENT_AMERICAN_218',
                'ICE_JKM_71090519',
                'ICE_NBP_UKF_71085728'
            )
            """
        )
    )
    op.drop_column(
        "option_contract_conventions",
        "asian_averaging_rule_code",
        schema=SCHEMA,
    )
    op.drop_column(
        "option_contract_conventions",
        "pricer_pricing_model",
        schema=SCHEMA,
    )
