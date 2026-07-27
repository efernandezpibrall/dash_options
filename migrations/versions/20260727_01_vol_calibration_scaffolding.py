"""Add immutable volatility-calibration and background-job storage.

Revision ID: 20260727_01
Revises:
Create Date: 2026-07-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260727_01"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "at_lng"

RUN_STATUSES = "'draft', 'submitted', 'approved', 'published', 'superseded', 'rejected'"
PUBLICATION_STATUSES = "'approved', 'published', 'superseded', 'rejected'"
JOB_STATUSES = "'queued', 'running', 'succeeded', 'failed', 'cancelled'"
ITEM_STATUSES = "'queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped'"


def _uuid_column(name, *, primary_key=False, nullable=False):
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        primary_key=primary_key,
        nullable=nullable,
        server_default=sa.text("gen_random_uuid()") if primary_key else None,
    )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "vol_calibration_runs",
        _uuid_column("run_id", primary_key=True),
        sa.Column("commodity", sa.String(16), nullable=False),
        sa.Column("cob_date", sa.Date(), nullable=False),
        sa.Column("input_fingerprint", sa.String(128), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.CheckConstraint(
            f"status IN ({RUN_STATUSES})",
            name="ck_vol_calibration_runs_status",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_vol_calibration_runs_idempotency",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_vol_calibration_runs_product_cob",
        "vol_calibration_runs",
        ["commodity", "cob_date", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "vol_calibration_expiry_results",
        _uuid_column("result_id", primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.vol_calibration_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("option_expiration_date", sa.Date(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.Column("validation", postgresql.JSONB(), nullable=False),
        sa.Column("weighted_rmse", sa.Numeric(18, 10), nullable=True),
        sa.Column("unweighted_rmse", sa.Numeric(18, 10), nullable=True),
        sa.Column("max_error", sa.Numeric(18, 10), nullable=True),
        sa.Column("optimizer_success", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "run_id",
            "option_expiration_date",
            name="uq_vol_calibration_expiry_run_expiry",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "vol_calibration_audit_events",
        _uuid_column("event_id", primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.vol_calibration_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=True),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column(
            "event_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "vol_surface_publications",
        _uuid_column("publication_id", primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.vol_calibration_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("commodity", sa.String(16), nullable=False),
        sa.Column("cob_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supersedes_publication_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.vol_surface_publications.publication_id",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            f"status IN ({PUBLICATION_STATUSES})",
            name="ck_vol_surface_publications_status",
        ),
        sa.CheckConstraint(
            "(status <> 'published' OR "
            "(is_active AND published_by IS NOT NULL AND published_at IS NOT NULL)) "
            "AND (status NOT IN ('superseded', 'rejected') OR NOT is_active)",
            name="ck_vol_surface_publications_lifecycle_fields",
        ),
        sa.UniqueConstraint("run_id", name="uq_vol_surface_publications_run"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_vol_surface_publications_idempotency",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_vol_surface_publications_active_product_cob",
        "vol_surface_publications",
        ["commodity", "cob_date"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "implied_volatility_surface_calibrated",
        _uuid_column("surface_point_id", primary_key=True),
        sa.Column(
            "publication_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.vol_surface_publications.publication_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.vol_calibration_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("commodity", sa.String(16), nullable=False),
        sa.Column("cob_date", sa.Date(), nullable=False),
        sa.Column("contract_date", sa.Date(), nullable=False),
        sa.Column("option_expiration_date", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(24, 10), nullable=True),
        sa.Column("delta", sa.Numeric(12, 10), nullable=True),
        sa.Column("put_call", sa.String(4), nullable=False),
        sa.Column("volatility", sa.Numeric(18, 10), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("input_fingerprint", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "strike IS NOT NULL OR delta IS NOT NULL",
            name="ck_calibrated_surface_coordinate",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_calibrated_surface_publication_expiry",
        "implied_volatility_surface_calibrated",
        ["publication_id", "option_expiration_date"],
        schema=SCHEMA,
    )

    op.create_table(
        "option_expiry_calendar",
        _uuid_column("calendar_id", primary_key=True),
        sa.Column("commodity", sa.String(16), nullable=False),
        sa.Column("contract_date", sa.Date(), nullable=False),
        sa.Column("option_expiration_date", sa.Date(), nullable=False),
        sa.Column("exchange", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("verified_by", sa.String(255), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "commodity",
            "contract_date",
            "exchange",
            name="uq_option_expiry_calendar_contract",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "vol_calibration_jobs",
        _uuid_column("job_id", primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.vol_calibration_runs.run_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cancellation_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            f"status IN ({JOB_STATUSES})",
            name="ck_vol_calibration_jobs_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts > 0 AND attempts <= max_attempts",
            name="ck_vol_calibration_jobs_attempts",
        ),
        sa.CheckConstraint(
            "completed_items >= 0 AND total_items >= 0 AND completed_items <= total_items",
            name="ck_vol_calibration_jobs_progress",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_vol_calibration_jobs_idempotency",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_vol_calibration_jobs_claim",
        "vol_calibration_jobs",
        ["status", "lease_expires_at", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "vol_calibration_job_items",
        _uuid_column("item_id", primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.vol_calibration_jobs.job_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("option_expiration_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.vol_calibration_expiry_results.result_id",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            f"status IN ({ITEM_STATUSES})",
            name="ck_vol_calibration_job_items_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_vol_calibration_job_items_attempts",
        ),
        sa.UniqueConstraint(
            "job_id",
            "option_expiration_date",
            name="uq_vol_calibration_job_items_expiry",
        ),
        schema=SCHEMA,
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_vol_calibration_run_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '% cannot be deleted', TG_TABLE_NAME;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'draft' THEN
                    RAISE EXCEPTION 'new calibration runs must start as draft';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.run_id,
                NEW.commodity,
                NEW.cob_date,
                NEW.input_fingerprint,
                NEW.engine_version,
                NEW.configuration,
                NEW.notes,
                NEW.created_by,
                NEW.created_at,
                NEW.idempotency_key
            ) IS DISTINCT FROM ROW(
                OLD.run_id,
                OLD.commodity,
                OLD.cob_date,
                OLD.input_fingerprint,
                OLD.engine_version,
                OLD.configuration,
                OLD.notes,
                OLD.created_by,
                OLD.created_at,
                OLD.idempotency_key
            ) THEN
                RAISE EXCEPTION 'calibration run inputs are immutable';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'draft' AND NEW.status = 'submitted')
                OR (OLD.status = 'submitted' AND NEW.status IN ('approved', 'rejected'))
                OR (OLD.status = 'approved' AND NEW.status IN ('published', 'rejected'))
                OR (OLD.status = 'published' AND NEW.status = 'superseded')
            ) THEN
                RAISE EXCEPTION 'invalid calibration run status transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            IF NEW.status = 'submitted' AND NEW.submitted_at IS NULL THEN
                RAISE EXCEPTION 'submitted_at is required for submitted runs';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_vol_calibration_runs_guard
        BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.vol_calibration_runs
        FOR EACH ROW
        EXECUTE FUNCTION {SCHEMA}.guard_vol_calibration_run_mutation()
        """
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_vol_surface_publication_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '% cannot be deleted', TG_TABLE_NAME;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'approved'
                    OR NEW.is_active
                    OR NEW.approved_by IS NULL
                    OR NEW.approved_at IS NULL
                THEN
                    RAISE EXCEPTION 'new publications must be approved and inactive';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.publication_id,
                NEW.run_id,
                NEW.commodity,
                NEW.cob_date,
                NEW.supersedes_publication_id,
                NEW.idempotency_key,
                NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.publication_id,
                OLD.run_id,
                OLD.commodity,
                OLD.cob_date,
                OLD.supersedes_publication_id,
                OLD.idempotency_key,
                OLD.created_at
            ) THEN
                RAISE EXCEPTION 'publication identity is immutable';
            END IF;

            IF NEW.status = OLD.status AND NEW.is_active IS DISTINCT FROM OLD.is_active THEN
                RAISE EXCEPTION 'publication activation requires a status transition';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'approved' AND NEW.status IN ('published', 'rejected'))
                OR (OLD.status = 'published' AND NEW.status = 'superseded')
            ) THEN
                RAISE EXCEPTION 'invalid publication status transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            IF NEW.status = 'published' AND NOT NEW.is_active THEN
                RAISE EXCEPTION 'published surfaces must be active';
            END IF;
            IF NEW.status IN ('superseded', 'rejected') AND NEW.is_active THEN
                RAISE EXCEPTION 'inactive publication status cannot remain active';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_vol_surface_publications_guard
        BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.vol_surface_publications
        FOR EACH ROW
        EXECUTE FUNCTION {SCHEMA}.guard_vol_surface_publication_mutation()
        """
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_vol_calibration_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table_name in (
        "vol_calibration_expiry_results",
        "vol_calibration_audit_events",
        "implied_volatility_surface_calibrated",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_append_only
            BEFORE UPDATE OR DELETE ON {SCHEMA}.{table_name}
            FOR EACH ROW
            EXECUTE FUNCTION {SCHEMA}.reject_vol_calibration_mutation()
            """
        )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20260727_01 is additive and intentionally has no destructive downgrade."
    )
