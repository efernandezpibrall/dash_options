"""Persist resumable per-expiry calibration results.

Revision ID: 20260928_01
Revises: 20260902_02
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260928_01"
down_revision = "20260902_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "vol_calibration_job_items",
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
        schema="at_lng",
    )
    op.add_column(
        "vol_calibration_job_items",
        sa.Column("dependency_fingerprint", sa.String(64), nullable=True),
        schema="at_lng",
    )
    op.add_column(
        "vol_calibration_job_items",
        sa.Column("result_payload", postgresql.JSONB(), nullable=True),
        schema="at_lng",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20260928_01 contains durable calibration checkpoints and "
        "has no safe automatic downgrade."
    )
