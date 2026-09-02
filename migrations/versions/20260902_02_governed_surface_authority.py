"""Govern the common five-product calibrated-surface representation.

Revision ID: 20260902_02
Revises: 20260902_01
Create Date: 2026-09-02
"""

from alembic import op


revision = "20260902_02"
down_revision = "20260902_01"
branch_labels = None
depends_on = None

SCHEMA = "at_lng"
TABLE = "implied_volatility_surface_calibrated"

OLD_CONSTRAINTS = (
    "ck_brent_calibrated_surface_inline_policy",
    "ck_hh_calibrated_surface_lne_policy",
    "ck_jkm_calibrated_surface_inline_policy",
    "ck_ttf_calibrated_surface_hybrid_fields",
)

CONSTRAINTS = {
    "ck_calibrated_surface_five_product_dense_policy": (
        "commodity NOT IN ('BRENT', 'HH', 'JKM', 'NBP', 'TTF') OR ("
        "total_variance > 0 AND working_forward > 0 AND strike > 0 "
        "AND delta > 0 AND delta < 1 AND surface_region IS NOT NULL "
        "AND blend_classification IS NOT NULL "
        "AND calibration_basis IN ('observed', 'extrapolated') "
        "AND calibration_method ILIKE '%PCHIP-core/Wing-v2-tail%' "
        "AND calibration_policy_version IS NOT NULL)"
    ),
    "ck_calibrated_surface_euro_gas_source_policy": (
        "commodity NOT IN ('JKM', 'NBP', 'TTF') OR ("
        "source_name = 'official' "
        "OR source_name ILIKE 'official:arbitrage_projection%' "
        "OR source_name ~* "
        "('^official_surface_euro_gas_lng_v[0-9]+_smile_template_v[0-9]+' "
        "|| chr(58) || 'extrap$'))"
    ),
    "ck_calibrated_surface_brent_source_policy": (
        "commodity <> 'BRENT' OR ("
        "calibration_policy_version LIKE 'brent_%' "
        "AND calibration_method ILIKE '%SVI%' "
        "AND source_name ILIKE '%Bloomberg Brent%')"
    ),
    "ck_calibrated_surface_hh_source_policy": (
        "commodity <> 'HH' OR ("
        "calibration_policy_version LIKE 'hh_lne_%' "
        "AND calibration_method ILIKE '%LNE%' "
        "AND source_name ILIKE '%LNE settlement%' "
        "AND source_name NOT ILIKE '%ICE PHE%' "
        "AND source_name NOT ILIKE '% ON %')"
    ),
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in OLD_CONSTRAINTS:
        op.drop_constraint(name, TABLE, schema=SCHEMA, type_="check")
    for name, expression in CONSTRAINTS.items():
        op.create_check_constraint(
            name,
            TABLE,
            expression,
            schema=SCHEMA,
            postgresql_not_valid=True,
        )
    op.create_index(
        "uq_calibrated_surface_publication_contract_strike",
        TABLE,
        ["publication_id", "contract_date", "strike"],
        unique=True,
        schema=SCHEMA,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20260902_02 protects authoritative five-product calibrated "
        "publications and has no safe automatic downgrade."
    )
