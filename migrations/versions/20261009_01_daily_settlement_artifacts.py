"""Compact exact artifacts and stage ledger for all-product daily settlement jobs.

Revision ID: 20261009_01
Revises: 20261005_02
"""

from alembic import op

revision = "20261009_01"
down_revision = "20261005_02"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("""CREATE TABLE at_lng.vol_daily_artifacts (
        job_id uuid NOT NULL REFERENCES at_lng.vol_calibration_jobs(job_id),
        artifact_key text NOT NULL,
        codec text NOT NULL CHECK (codec='tagged-json-zlib-v1'),
        digest text NOT NULL CHECK (length(digest)=64),
        raw_bytes integer NOT NULL CHECK (raw_bytes BETWEEN 0 AND 134217728),
        payload bytea NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(job_id,artifact_key)
    )""")
    op.execute("""CREATE TABLE at_lng.vol_daily_stages (
        job_id uuid PRIMARY KEY REFERENCES at_lng.vol_calibration_jobs(job_id),
        commodity text NOT NULL CHECK(commodity IN ('BRENT','HH','TTF','JKM','NBP')),
        cob_date date NOT NULL,
        stage text NOT NULL CHECK(stage IN ('prepared','calibrating','accepted','held','publishing','published','verified')),
        publication_id uuid REFERENCES at_lng.vol_surface_publications(publication_id),
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    op.execute("CREATE INDEX ON at_lng.vol_daily_stages(cob_date,commodity)")
    op.execute("""CREATE FUNCTION at_lng.reject_daily_artifact_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'daily artifacts are immutable'; END $$""")
    op.execute("""CREATE TRIGGER daily_artifact_immutable BEFORE UPDATE OR DELETE
        ON at_lng.vol_daily_artifacts FOR EACH ROW EXECUTE FUNCTION at_lng.reject_daily_artifact_mutation()""")


def downgrade():
    # Preserve saved candidates and receipts for a code rollback. Older workers
    # ignore this job family and these additive tables.
    raise RuntimeError("Retained daily execution evidence requires a reviewed schema rollback")
