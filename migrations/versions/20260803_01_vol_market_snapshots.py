"""Add immutable timestamped option-market snapshots.

Revision ID: 20260803_01
Revises: 20260727_01
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260803_01"
down_revision = "20260727_01"
branch_labels = None
depends_on = None

SCHEMA = "at_lng"


def _uuid_column(name, *, primary_key=False, nullable=False):
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        primary_key=primary_key,
        nullable=nullable,
        server_default=sa.text("gen_random_uuid()") if primary_key else None,
    )


def upgrade() -> None:
    op.create_table(
        "vol_market_snapshots",
        _uuid_column("snapshot_id", primary_key=True),
        sa.Column("commodity", sa.String(16), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("source_revision", sa.String(255), nullable=True),
        sa.Column("input_fingerprint", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("option_quote_count", sa.Integer(), nullable=False),
        sa.Column("forward_count", sa.Integer(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'rejected')",
            name="ck_vol_market_snapshots_status",
        ),
        sa.CheckConstraint(
            "option_quote_count >= 0 AND forward_count >= 0",
            name="ck_vol_market_snapshots_counts",
        ),
        sa.UniqueConstraint(
            "commodity",
            "input_fingerprint",
            name="uq_vol_market_snapshots_fingerprint",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_vol_market_snapshots_latest",
        "vol_market_snapshots",
        ["commodity", "business_date", "observed_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "vol_market_option_quotes",
        _uuid_column("quote_id", primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.vol_market_snapshots.snapshot_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("contract_date", sa.Date(), nullable=False),
        sa.Column("option_expiration_date", sa.Date(), nullable=False),
        sa.Column("put_call", sa.String(1), nullable=False),
        sa.Column("strike", sa.Numeric(24, 10), nullable=False),
        sa.Column("mark_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("mark_iv", sa.Numeric(18, 10), nullable=True),
        sa.Column("volume", sa.Numeric(24, 6), nullable=True),
        sa.Column("open_interest", sa.Numeric(24, 6), nullable=True),
        sa.Column("source_quote_id", sa.String(255), nullable=True),
        sa.Column("vendor_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("put_call IN ('C', 'P')", name="ck_vol_market_quotes_put_call"),
        sa.CheckConstraint("strike > 0 AND mark_price > 0", name="ck_vol_market_quotes_values"),
        sa.UniqueConstraint(
            "snapshot_id",
            "contract_date",
            "put_call",
            "strike",
            name="uq_vol_market_quotes_snapshot_contract",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_vol_market_quotes_snapshot_expiry",
        "vol_market_option_quotes",
        ["snapshot_id", "contract_date"],
        schema=SCHEMA,
    )

    op.create_table(
        "vol_market_forwards",
        _uuid_column("forward_id", primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.vol_market_snapshots.snapshot_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("contract_date", sa.Date(), nullable=False),
        sa.Column("forward", sa.Numeric(24, 10), nullable=False),
        sa.Column("source_quote_id", sa.String(255), nullable=True),
        sa.Column("vendor_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("forward > 0", name="ck_vol_market_forwards_value"),
        sa.UniqueConstraint(
            "snapshot_id",
            "contract_date",
            name="uq_vol_market_forwards_snapshot_contract",
        ),
        schema=SCHEMA,
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_vol_market_snapshot_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'volatility market snapshots are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in (
        "vol_market_snapshots",
        "vol_market_option_quotes",
        "vol_market_forwards",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {SCHEMA}.{table}
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_vol_market_snapshot_mutation();
            """
        )


def downgrade() -> None:
    raise RuntimeError(
        "This append-only market-snapshot migration has no destructive downgrade."
    )
