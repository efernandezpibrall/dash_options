"""Enforce product provenance for inline governed publications.

Revision ID: 20260831_03
Revises: 20260831_02
Create Date: 2026-08-31
"""

from alembic import op


revision = "20260831_03"
down_revision = "20260831_02"
branch_labels = None
depends_on = None

SCHEMA = "at_lng"
TABLE = "implied_volatility_surface_calibrated"

CONSTRAINTS = {
    "ck_brent_calibrated_surface_inline_policy": (
        "commodity <> 'BRENT' OR (total_variance > 0 AND working_forward > 0 "
        "AND surface_region IS NOT NULL AND blend_classification IS NOT NULL "
        "AND calibration_basis = 'observed' "
        "AND calibration_policy_version LIKE 'brent_%' "
        "AND calibration_method ILIKE '%SVI%' "
        "AND source_name ILIKE '%Bloomberg Brent%')"
    ),
    "ck_hh_calibrated_surface_lne_policy": (
        "commodity <> 'HH' OR (total_variance > 0 AND working_forward > 0 "
        "AND surface_region IS NOT NULL AND blend_classification IS NOT NULL "
        "AND calibration_basis IN ('observed', 'extrapolated') "
        "AND calibration_policy_version LIKE 'hh_lne_%' "
        "AND calibration_method ILIKE '%LNE%' "
        "AND source_name ILIKE '%LNE settlement%' "
        "AND source_name NOT ILIKE '%ICE PHE%' "
        "AND source_name NOT ILIKE '% ON %')"
    ),
    "ck_jkm_calibrated_surface_inline_policy": (
        "commodity <> 'JKM' OR (total_variance > 0 AND working_forward > 0 "
        "AND surface_region IS NOT NULL AND blend_classification IS NOT NULL "
        "AND calibration_basis IN ('observed', 'extrapolated') "
        "AND calibration_policy_version LIKE 'jkm_%' "
        "AND calibration_method IS NOT NULL "
        "AND source_name ILIKE '%ICAP%')"
    ),
}


def upgrade() -> None:
    for name, expression in CONSTRAINTS.items():
        op.create_check_constraint(
            name,
            TABLE,
            expression,
            schema=SCHEMA,
            postgresql_not_valid=True,
        )
        # NOT VALID preserves immutable historical publications while still
        # enforcing the policy for every new or changed point after migration.


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20260831_03 protects governed Brent, HH and JKM provenance "
        "and has no safe automatic downgrade once publications exist."
    )
