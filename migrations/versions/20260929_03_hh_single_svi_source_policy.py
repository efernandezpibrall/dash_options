"""Admit the versioned HH single-SVI method to the LNE source guard.

Revision ID: 20260929_03
Revises: 20260929_02
Create Date: 2026-09-29
"""

from alembic import op


revision = "20260929_03"
down_revision = "20260929_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        "ck_calibrated_surface_hh_source_policy",
        "implied_volatility_surface_calibrated",
        schema="at_lng",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calibrated_surface_hh_source_policy",
        "implied_volatility_surface_calibrated",
        "commodity <> 'HH' OR ("
        "calibration_policy_version LIKE 'hh_lne_%' "
        "AND (calibration_method ILIKE '%LNE%' OR ("
        "calibration_method = 'single_svi_seasonal_quotes' "
        "AND calibration_policy_version = 'hh_lne_single_svi_seasonal_quotes_v1')) "
        "AND source_name ILIKE '%LNE settlement%' "
        "AND source_name NOT ILIKE '%ICE PHE%' "
        "AND source_name NOT ILIKE '% ON %')",
        schema="at_lng",
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Published HH single-SVI rows may depend on this source policy; "
        "there is no safe automatic downgrade."
    )
