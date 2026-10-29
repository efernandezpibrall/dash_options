"""Retire obsolete product-specific history tables after verified archival.

Independent of the Live Prices curve branch: this change needs only the
shared-SOFR schema. Populated databases must use the verified retirement tool.
"""
from alembic import op
from sqlalchemy import text

revision='20261029_02'
down_revision='20261028_01'
branch_labels=None
depends_on=None
TABLES=('brent_delta_history_points','brent_delta_history_settlements',
        'brent_delta_history_runs','brent_realized_history_settlements','brent_realized_history_runs')


def upgrade():
    db=op.get_bind()
    db.execute(text("SET LOCAL lock_timeout='5s'"))
    db.execute(text("SET LOCAL statement_timeout='30s'"))
    present=[]
    for table in TABLES:
        if db.execute(text('SELECT to_regclass(:name)'),{'name':'at_lng.'+table}).scalar():
            db.execute(text('LOCK TABLE at_lng.'+table+' IN ACCESS EXCLUSIVE MODE'))
            if db.execute(text('SELECT EXISTS(SELECT 1 FROM at_lng.'+table+')')).scalar():
                raise RuntimeError('Populated Brent history must first be archived and retired with scripts.maintenance.retire_brent_history')
            present.append(table)
    for table in present:
        db.execute(text('DROP TABLE at_lng.'+table+' RESTRICT'))


def downgrade():
    raise RuntimeError('Restore the verified full archive before rolling back the reader; do not create empty history tables')
