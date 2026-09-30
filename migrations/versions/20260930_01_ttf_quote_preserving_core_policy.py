"""Admit the governed TTF quote-preserving core policy to dense publication.

Revision ID: 20260930_01
Revises: 20260929_03
Create Date: 2026-09-30
"""

from alembic import op


revision = "20260930_01"
down_revision = "20260929_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        "ck_calibrated_surface_five_product_dense_policy",
        "implied_volatility_surface_calibrated",
        schema="at_lng",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calibrated_surface_five_product_dense_policy",
        "implied_volatility_surface_calibrated",
        "commodity NOT IN ('BRENT', 'HH', 'JKM', 'NBP', 'TTF') OR ("
        "total_variance > 0 AND working_forward > 0 AND strike > 0 "
        "AND delta > 0 AND delta < 1 AND surface_region IS NOT NULL "
        "AND blend_classification IS NOT NULL "
        "AND calibration_basis IN ('observed', 'extrapolated') "
        "AND (calibration_method ILIKE '%PCHIP-core/Wing-v2-tail%' OR ("
        "commodity = 'BRENT' "
        "AND calibration_method = 'single_svi_actual_strikes' "
        "AND calibration_policy_version = 'brent_single_svi_actual_strikes_v1' "
        "AND surface_region = 'single_svi') OR ("
        "commodity = 'HH' "
        "AND calibration_method = 'single_svi_seasonal_quotes' "
        "AND calibration_policy_version = 'hh_lne_single_svi_seasonal_quotes_v1' "
        "AND surface_region = 'single_svi') OR ("
        "commodity = 'TTF' "
        "AND calibration_method = 'PCHIP/convex-call-price core with Wing-v2 tails' "
        "AND calibration_policy_version = 'ttf_quote_preserving_core_wing_tail_hybrid_v2' "
        "AND surface_region IN ('core', 'tail') "
        "AND blend_classification IN ("
        "'pchip_core', 'convex_call_core', 'left_blend', 'right_blend', "
        "'wing_left', 'wing_right'))) "
        "AND calibration_policy_version IS NOT NULL)",
        schema="at_lng",
        # Preserve historical rows under the existing NOT VALID migration
        # convention; the constraint governs every newly written surface row.
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Published TTF quote-preserving rows may depend on this policy; "
        "there is no safe automatic downgrade."
    )
