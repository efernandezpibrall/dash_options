"""Settlement retry audit, explicit session deadlines and promotion evidence window.

Revision ID: 20261005_02
Revises: 20261005_01
"""
from alembic import op

revision = "20261005_02"
down_revision = "20261005_01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""ALTER TABLE at_lng.vol_calibration_jobs
        ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ADD COLUMN failure_kind text CHECK (failure_kind IN ('retryable','permanent'))""")
    op.execute("""CREATE TABLE at_lng.vol_settlement_retry_audit (
        retry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id uuid NOT NULL REFERENCES at_lng.vol_calibration_jobs(job_id),
        actor text NOT NULL CHECK (length(trim(actor))>0),
        reason text NOT NULL CHECK (length(trim(reason))>0),
        prior_attempts integer NOT NULL, prior_max_attempts integer NOT NULL,
        new_max_attempts integer NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
    op.execute("""CREATE TABLE at_lng.vol_settlement_sessions (
        commodity text NOT NULL CHECK (commodity IN ('BRENT','HH')),
        cob_date date NOT NULL, ready_at timestamptz NOT NULL,
        deadline_at timestamptz NOT NULL CHECK (deadline_at>ready_at),
        calendar_identity text NOT NULL, calendar_version text NOT NULL,
        calendar_source text NOT NULL, imported_by text NOT NULL,
        imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (commodity,cob_date))""")
    op.execute("""CREATE TABLE at_lng.vol_settlement_calendars (
        calendar_identity text PRIMARY KEY, commodity text NOT NULL,
        from_cob date NOT NULL, through_cob date NOT NULL CHECK (through_cob>=from_cob),
        manifest jsonb NOT NULL, imported_by text NOT NULL,
        imported_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
    op.execute("""CREATE TABLE at_lng.vol_settlement_alerts (
        alert_key text PRIMARY KEY, commodity text NOT NULL,
        cob_date date NOT NULL, kind text NOT NULL,
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        is_active boolean NOT NULL DEFAULT true,
        first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        resolved_at timestamptz)""")
    op.execute("""ALTER TABLE at_lng.vol_settlement_promotion
        ADD COLUMN evidence_through_cob date,
        ADD COLUMN evidence_identity text""")
    # A changed worker cannot inherit an earlier approval without renewed proof.
    op.execute("UPDATE at_lng.vol_settlement_promotion SET approved=false")
    op.execute("""ALTER TABLE at_lng.vol_settlement_promotion ADD CONSTRAINT
        ck_settlement_promotion_evidence CHECK (NOT approved OR
        (evidence_through_cob IS NOT NULL AND evidence_identity IS NOT NULL))""")


def downgrade():
    raise RuntimeError("Retained session, retry and approval evidence requires a reviewed rollback")
