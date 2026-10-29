"""Immutable Bloomberg settlements for Brent rolling realized volatility."""
from pathlib import Path
from alembic import op

revision = '20261016_03'
down_revision = '20261016_02'
branch_labels = None
depends_on = None


def upgrade():
    op.execute((Path(__file__).parents[1] / 'brent_realized_history.sql').read_text())


def downgrade():
    raise RuntimeError('Retain settlement audit history and roll back the dashboard reader.')
