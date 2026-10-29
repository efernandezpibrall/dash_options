"""Separate Brent futures history from expired option volatility history."""
from pathlib import Path
from alembic import op

revision = '20261016_02'
down_revision = '20261016_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute((Path(__file__).parents[1] / 'brent_delta_history_expiry.sql').read_text())


def downgrade():
    raise RuntimeError('Retain historical runs and roll back the reader to the prior release.')
