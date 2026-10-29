"""Add isolated Jan/Feb 2027 Bloomberg delta history."""
from pathlib import Path
from alembic import op

revision = '20261016_01'
down_revision = '20261012_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute((Path(__file__).parents[1] / 'brent_delta_history.sql').read_text())


def downgrade():
    raise RuntimeError('Retain immutable history for rollback; roll back its reader instead.')
