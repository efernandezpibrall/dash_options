"""Converge all branches and retire empty Brent history after its creators.

The original retirement branch could finish before the independent history
branch, leaving recreated tables even when all three old heads were applied.
Keep that recorded history intact and enforce retirement at their common head.
"""

from pathlib import Path

from alembic.script import ScriptDirectory


revision = "20261029_03"
down_revision = ("20261021_01", "20261029_01", "20261029_02")
branch_labels = None
depends_on = None


def _retirement_revision():
    # Reuse the immutable migration's bounded locks, populated-table guard and
    # RESTRICT drops in the current Alembic transaction. Do not import mutable
    # application/maintenance code or change earlier revision relationships.
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1]))
    return scripts.get_revision("20261029_02").module


def upgrade():
    _retirement_revision().upgrade()


def downgrade():
    _retirement_revision().downgrade()
