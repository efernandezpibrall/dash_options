"""Durable unpublished chunks and explicit daily recovery stages.

Revision ID: 20261021_01
Revises: 20261016_03, 20261009_01
"""

from alembic import op

revision = "20261021_01"
down_revision = ("20261016_03", "20261009_01")
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("""CREATE TABLE at_lng.vol_publication_stages (
        publication_id uuid PRIMARY KEY,
        run_id uuid NOT NULL UNIQUE,
        idempotency_key text NOT NULL UNIQUE,
        commodity text NOT NULL CHECK (commodity IN ('BRENT','HH','TTF','JKM','NBP')),
        created_by text NOT NULL,
        identity text NOT NULL CHECK(length(identity)=64),
        configuration jsonb NOT NULL,
        expiry_results jsonb NOT NULL,
        point_digest text NOT NULL CHECK(length(point_digest)=64),
        point_count integer NOT NULL CHECK(point_count>0),
        job_id uuid REFERENCES at_lng.vol_calibration_jobs(job_id),
        ready boolean NOT NULL DEFAULT FALSE,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    # SELECT copies the currently migrated float types, without final-table
    # foreign keys, defaults or immutable-publication triggers.
    op.execute("""CREATE TABLE at_lng.vol_publication_stage_points AS SELECT
        publication_id,run_id,commodity,cob_date,contract_date,option_expiration_date,
        strike,delta,put_call,volatility,total_variance,working_forward,surface_region,
        blend_classification,calibration_basis,source_name,calibration_method,
        calibration_policy_version,input_fingerprint
        FROM at_lng.implied_volatility_surface_calibrated WITH NO DATA""")
    op.execute("""ALTER TABLE at_lng.vol_publication_stage_points
        ADD PRIMARY KEY(publication_id,contract_date,strike),
        ADD FOREIGN KEY(publication_id) REFERENCES at_lng.vol_publication_stages(publication_id)
            ON DELETE CASCADE""")
    op.execute("""CREATE FUNCTION at_lng.protect_publication_stage() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.ready AND NOT NEW.ready THEN RAISE EXCEPTION 'sealed publication stages cannot be reopened'; END IF;
            IF (to_jsonb(NEW)-'ready'-'updated_at') IS DISTINCT FROM
               (to_jsonb(OLD)-'ready'-'updated_at') THEN
                RAISE EXCEPTION 'publication staging evidence is immutable';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER publication_stage_immutable BEFORE UPDATE
        ON at_lng.vol_publication_stages FOR EACH ROW EXECUTE FUNCTION at_lng.protect_publication_stage()""")
    op.execute("""CREATE FUNCTION at_lng.protect_staged_point() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE stage_ready boolean;
        BEGIN
            IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'staged points are immutable'; END IF;
            IF TG_OP='DELETE' THEN
                -- A header deletion owns retention/canonical cleanup; its
                -- cascading point deletion observes the header already gone.
                IF EXISTS (SELECT 1 FROM at_lng.vol_publication_stages
                           WHERE publication_id=OLD.publication_id) THEN
                    RAISE EXCEPTION 'staged points are immutable';
                END IF;
                RETURN OLD;
            END IF;
            SELECT ready INTO stage_ready FROM at_lng.vol_publication_stages
                WHERE publication_id=NEW.publication_id FOR SHARE;
            IF stage_ready IS DISTINCT FROM FALSE THEN
                RAISE EXCEPTION 'publication stage is already sealed or missing';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER publication_stage_point_guard BEFORE INSERT OR UPDATE OR DELETE
        ON at_lng.vol_publication_stage_points FOR EACH ROW EXECUTE FUNCTION at_lng.protect_staged_point()""")
    op.execute("""ALTER TABLE at_lng.vol_daily_stages DROP CONSTRAINT vol_daily_stages_stage_check,
        ADD CONSTRAINT vol_daily_stages_stage_check CHECK(stage IN (
        'prepared','calibrating','accepted','held','staging','staged','publishing',
        'published','verification_pending','waiting','cancelled','verified'))""")


def downgrade():
    raise RuntimeError("Retained publication stages require a reviewed schema rollback")
