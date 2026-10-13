"""Admit versioned smooth TTF intraday curves, preserving existing product rules."""
import re
from alembic import op
from sqlalchemy import text

revision = '20261012_01'
down_revision = '20261009_02'
branch_labels = None
depends_on = None

SMOOTH = """commodity='TTF'
    AND source_name ~ '^tfo-intraday-smooth-v2:[0-9a-f-]{36}$'
    AND calibration_method='C2 constrained call-price spline'
    AND calibration_policy_version='smooth-call-spline-v1'
    AND surface_region='smooth_price' AND blend_classification='continuous_c2'
    AND calibration_basis='observed' AND total_variance>0 AND volatility>0
    AND working_forward>0 AND strike>0 AND delta>0 AND delta<1"""


def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    for name, prerequisite in (
        ('ck_calibrated_surface_euro_gas_source_policy','tfo-intraday-v1:'),
        ('ck_calibrated_surface_five_product_dense_policy','ttf_quote_preserving_core_wing_tail_hybrid_v2'),
    ):
        row=op.get_bind().execute(text("""SELECT pg_get_constraintdef(oid) AS definition,convalidated
            FROM pg_constraint WHERE conrelid='at_lng.implied_volatility_surface_calibrated'::regclass
            AND conname=:name"""),{'name':name}).mappings().one()
        match=re.fullmatch(r'CHECK \((.*)\)(?: NOT VALID)?',row['definition'],re.S)
        if not match or prerequisite not in row['definition'] or 'smooth-call-spline-v1' in row['definition']:
            raise RuntimeError('Unexpected TFO source/dense policy predecessor')
        op.execute(f'ALTER TABLE at_lng.implied_volatility_surface_calibrated DROP CONSTRAINT {name}, '
                   f'ADD CONSTRAINT {name} CHECK (({match.group(1)}) OR COALESCE(({SMOOTH}),false)) NOT VALID')
        if row['convalidated']:
            op.execute(f'ALTER TABLE at_lng.implied_volatility_surface_calibrated VALIDATE CONSTRAINT {name}')


def downgrade():
    raise RuntimeError('Retain immutable smooth publications for rollback; disable intraday reads instead.')
