"""Permit versioned HH SOFR mixtures; reuse existing curve and publication tables."""

from alembic import op
from sqlalchemy import text

revision = "20261028_01"
down_revision = "20261023_01"
branch_labels = None
depends_on = None
POLICY = "hh_lne_sofr_positive_mixture_v1"
CONSTRAINTS = ("ck_calibrated_surface_five_product_dense_policy", "ck_calibrated_surface_hh_source_policy")


def upgrade():
    connection = op.get_bind()
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='120s'")
    condition = """commodity='HH' AND calibration_method='positive_lognormal_mixture'
        AND calibration_policy_version='hh_lne_sofr_positive_mixture_v1'
        AND surface_region='positive_mixture' AND blend_classification IN ('quote_mixture','seasonal_mixture')
        AND calibration_basis IN ('observed','extrapolated')
        AND total_variance>0 AND volatility>0 AND working_forward>0 AND strike>0 AND delta>0 AND delta<1
        AND source_name ILIKE '%LNE settlement%' AND source_name NOT ILIKE '%ICE PHE%' AND source_name NOT ILIKE '% ON %'"""
    for name in CONSTRAINTS:
        prior = connection.execute(
            text(
                "SELECT pg_get_expr(conbin,conrelid) FROM pg_constraint WHERE conrelid='at_lng.implied_volatility_surface_calibrated'::regclass AND conname=:name"
            ),
            {"name": name},
        ).scalar_one()
        if POLICY in prior:
            continue
        op.execute(f"ALTER TABLE at_lng.implied_volatility_surface_calibrated DROP CONSTRAINT {name}")
        # Preserve all historic alternatives, including intervening Brent/TTF releases.
        op.execute(
            f"ALTER TABLE at_lng.implied_volatility_surface_calibrated ADD CONSTRAINT {name} CHECK (({prior}) OR COALESCE(({condition}),false)) NOT VALID"
        )
    connection.execute(
        text("""INSERT INTO at_lng.option_discount_curve_definitions
        (curve_code,currency,day_count,compounding,source_name,active)
        VALUES ('USD_SOFR_BBG490_MID_V1','USD','ACT/365.25','continuous','BLOOMBERG',true)
        ON CONFLICT(curve_code) DO NOTHING""")
    )
    existing = connection.execute(
        text(
            "SELECT currency,source_name,active FROM at_lng.option_discount_curve_definitions WHERE curve_code='USD_SOFR_BBG490_MID_V1'"
        )
    ).one()
    if tuple(existing) != ("USD", "BLOOMBERG", True):
        raise ValueError("Conflicting SOFR curve definition")


def downgrade():
    raise RuntimeError(
        "Forward-only additive policy migration; roll back application while preserving historic SOFR publications"
    )
