"""Allow governed three-component Brent301 valuation traceability.

Revision ID: 20260831_02
Revises: 20260806_01
Create Date: 2026-08-31
"""

from alembic import op


revision = "20260831_02"
down_revision = "20260806_01"
branch_labels = None
depends_on = None

SCHEMA = "at_lng"
TABLE = "trades_options_valuation"
CONSTRAINT = "trades_options_valuation_adjustment_components_ck"


def _leg_constraint(suffix: str) -> str:
    components = f"volatility_adjustment_components_{suffix}"
    maturity_type = f"maturity_date_type_{suffix}"
    scalar_columns = (
        f"surface_expiry_date_{suffix}",
        f"surface_expiry_status_{suffix}",
        f"volatility_reference_convention_{suffix}",
        f"volatility_reference_convention_version_{suffix}",
        f"variance_calendar_code_{suffix}",
        f"variance_calendar_version_{suffix}",
        f"business_days_to_trade_expiry_{suffix}",
        f"business_days_to_surface_expiry_{suffix}",
        f"vol_adjustment_factor_{suffix}",
    )
    scalar_nulls = " AND ".join(f"{column} IS NULL" for column in scalar_columns)
    return f"""
    CASE
        WHEN {components} IS NULL THEN true
        WHEN jsonb_typeof({components}) <> 'array' THEN false
        ELSE (
            (
                (lower(trim({maturity_type})) = 'calendar'
                 AND jsonb_array_length({components}) = 12)
                OR
                (lower(trim({maturity_type})) = 'brent301'
                 AND asset_{suffix} = 'ICE_BRENT_FUTURES'
                 AND jsonb_array_length({components}) = 3)
            )
            AND {scalar_nulls}
        )
    END
    """


def upgrade() -> None:
    op.drop_constraint(
        CONSTRAINT,
        TABLE,
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        f"({_leg_constraint('a')}) AND ({_leg_constraint('b')})",
        schema=SCHEMA,
        postgresql_not_valid=True,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{TABLE} VALIDATE CONSTRAINT {CONSTRAINT}"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20260831_02 admits auditable Brent301 rows and has no safe "
        "automatic downgrade once those rows exist."
    )
