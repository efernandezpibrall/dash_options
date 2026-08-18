"""Add TTF intraday trade and hybrid-publication provenance.

Revision ID: 20260806_01
Revises: 20260803_01
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_01"
down_revision = "20260803_01"
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
        "vol_calibration_intraday_trades",
        _uuid_column("trade_id", primary_key=True),
        sa.Column("commodity", sa.String(16), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contract_date", sa.Date(), nullable=False),
        sa.Column("option_expiration_date", sa.Date(), nullable=False),
        sa.Column("put_call", sa.String(1), nullable=False),
        sa.Column("strike", sa.Numeric(24, 10), nullable=False),
        sa.Column("mark_price", sa.Numeric(24, 10), nullable=True),
        sa.Column("mark_iv", sa.Numeric(18, 10), nullable=False),
        sa.Column("volume", sa.Numeric(24, 6), nullable=True),
        sa.Column("forward", sa.Numeric(24, 10), nullable=False),
        sa.Column("forward_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dte", sa.Numeric(14, 6), nullable=False),
        sa.Column("call_delta", sa.Numeric(12, 10), nullable=False),
        sa.Column("log_moneyness", sa.Numeric(18, 10), nullable=False),
        sa.Column("day_count", sa.String(32), nullable=False),
        sa.Column("delta_convention", sa.String(64), nullable=False),
        sa.Column("pricing_model", sa.String(64), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column("entered_by", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        _uuid_column("supersedes_trade_id", nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_trade_id"],
            [f"{SCHEMA}.vol_calibration_intraday_trades.trade_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("commodity = 'TTF'", name="ck_ttf_intraday_commodity"),
        sa.CheckConstraint("put_call IN ('C', 'P')", name="ck_ttf_intraday_put_call"),
        sa.CheckConstraint(
            "strike > 0 AND mark_iv > 0 AND mark_iv < 2 AND forward > 0 AND dte > 0",
            name="ck_ttf_intraday_positive_values",
        ),
        sa.CheckConstraint(
            "call_delta > 0 AND call_delta < 1",
            name="ck_ttf_intraday_delta",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_ttf_intraday_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ttf_intraday_trades_business_expiry",
        "vol_calibration_intraday_trades",
        ["business_date", "contract_date", "observed_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "vol_calibration_run_trade_inputs",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.vol_calibration_runs.run_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "trade_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.vol_calibration_intraday_trades.trade_id",
                ondelete="RESTRICT",
            ),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        schema=SCHEMA,
    )

    op.add_column(
        "implied_volatility_surface_calibrated",
        sa.Column("total_variance", sa.Numeric(18, 10), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "implied_volatility_surface_calibrated",
        sa.Column("working_forward", sa.Numeric(24, 10), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "implied_volatility_surface_calibrated",
        sa.Column("surface_region", sa.String(16), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "implied_volatility_surface_calibrated",
        sa.Column("blend_classification", sa.String(32), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "implied_volatility_surface_calibrated",
        sa.Column("calibration_basis", sa.String(16), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "implied_volatility_surface_calibrated",
        sa.Column("calibration_method", sa.String(128), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "implied_volatility_surface_calibrated",
        sa.Column("calibration_policy_version", sa.String(128), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_ttf_calibrated_surface_hybrid_fields",
        "implied_volatility_surface_calibrated",
        "commodity <> 'TTF' OR (total_variance > 0 AND working_forward > 0 "
        "AND surface_region IS NOT NULL "
        "AND blend_classification IS NOT NULL AND calibration_basis IN "
        "('observed', 'extrapolated') AND calibration_method IS NOT NULL "
        "AND calibration_policy_version IS NOT NULL)",
        schema=SCHEMA,
        postgresql_not_valid=True,
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_ttf_intraday_trade_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'TTF intraday trades are append-only';
            END IF;
            IF ROW(NEW.trade_id, NEW.commodity, NEW.business_date, NEW.observed_at,
                   NEW.contract_date, NEW.option_expiration_date, NEW.put_call,
                   NEW.strike, NEW.mark_price, NEW.mark_iv, NEW.volume, NEW.forward,
                   NEW.forward_observed_at, NEW.dte, NEW.call_delta,
                   NEW.log_moneyness, NEW.day_count, NEW.delta_convention,
                   NEW.pricing_model, NEW.method, NEW.entered_by,
                   NEW.supersedes_trade_id, NEW.notes, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.trade_id, OLD.commodity, OLD.business_date, OLD.observed_at,
                   OLD.contract_date, OLD.option_expiration_date, OLD.put_call,
                   OLD.strike, OLD.mark_price, OLD.mark_iv, OLD.volume, OLD.forward,
                   OLD.forward_observed_at, OLD.dte, OLD.call_delta,
                   OLD.log_moneyness, OLD.day_count, OLD.delta_convention,
                   OLD.pricing_model, OLD.method, OLD.entered_by,
                   OLD.supersedes_trade_id, OLD.notes, OLD.created_at)
            THEN
                RAISE EXCEPTION 'TTF intraday trade economics are immutable';
            END IF;
            IF NOT (OLD.status = 'active' AND NEW.status = 'superseded') THEN
                RAISE EXCEPTION 'invalid TTF intraday trade status transition';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_ttf_intraday_trades_guard
        BEFORE UPDATE OR DELETE ON {SCHEMA}.vol_calibration_intraday_trades
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_ttf_intraday_trade_mutation();

        CREATE TRIGGER trg_vol_calibration_run_trade_inputs_append_only
        BEFORE UPDATE OR DELETE ON {SCHEMA}.vol_calibration_run_trade_inputs
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_vol_calibration_mutation();

        CREATE OR REPLACE VIEW {SCHEMA}.ttf_vol_surface_publication_current AS
        SELECT p.publication_id, p.run_id, p.cob_date, p.published_at,
               p.published_by, s.contract_date, s.option_expiration_date,
               s.strike, s.delta, s.put_call, s.volatility, s.total_variance,
               s.working_forward,
               s.surface_region, s.blend_classification,
               s.calibration_basis, s.source_name, s.calibration_method,
               s.calibration_policy_version, s.input_fingerprint
        FROM {SCHEMA}.vol_surface_publications p
        JOIN {SCHEMA}.implied_volatility_surface_calibrated s
          ON s.publication_id = p.publication_id
        WHERE p.commodity = 'TTF' AND p.status = 'published' AND p.is_active;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20260806_01 is additive and intentionally has no destructive downgrade."
    )
