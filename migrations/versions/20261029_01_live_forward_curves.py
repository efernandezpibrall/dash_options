"""Immutable Live Prices revisions and manual desk activation audit."""
from alembic import op
from options.live_forward_curves import curve_tables

revision = "20261029_01"
down_revision = "20261028_01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    metadata, *_ = curve_tables("at_lng")
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("Desk curve audit history must be retained; disable readers to roll back")
