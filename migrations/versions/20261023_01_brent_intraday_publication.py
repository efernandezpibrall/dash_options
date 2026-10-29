"""Permit reviewed Brent intraday drafts without activating settlement rows."""
from alembic import op
from sqlalchemy import text

revision = "20261023_01"
down_revision = "20261012_01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("""ALTER TABLE at_lng.vol_intraday_drafts ADD COLUMN commodity text
        GENERATED ALWAYS AS (COALESCE(payload->'candidate'->'manifest'->>'commodity','TTF')) STORED,
        ADD CONSTRAINT ck_intraday_draft_commodity CHECK
        (COALESCE(payload->'candidate'->'manifest'->>'commodity','TTF') IN ('TTF','BRENT'))""")
    op.execute("CREATE INDEX ix_intraday_draft_actor_product ON at_lng.vol_intraday_drafts(actor,commodity,created_at DESC)")
    op.execute("""ALTER TABLE at_lng.vol_intraday_active
        DROP CONSTRAINT vol_intraday_active_commodity_check,
        ADD CONSTRAINT ck_intraday_active_commodity CHECK(commodity IN ('TTF','BRENT'))""")
    op.execute("""ALTER TABLE at_lng.vol_surface_publications
        DROP CONSTRAINT ck_vol_surface_publications_lifecycle_fields,
        ADD CONSTRAINT ck_vol_surface_publications_lifecycle_fields CHECK (
            (status <> 'published' OR ((is_active
                OR (commodity='TTF' AND idempotency_key LIKE 'tfo-intraday:%')
                OR (commodity='BRENT' AND idempotency_key LIKE 'brent-intraday:%'))
                AND published_by IS NOT NULL AND published_at IS NOT NULL))
            AND (status NOT IN ('superseded','rejected') OR NOT is_active))""")
    definition = op.get_bind().execute(text("SELECT pg_get_functiondef('at_lng.guard_vol_surface_publication_mutation()'::regprocedure)")).scalar_one()
    original = "WHERE r.run_id=NEW.run_id AND NEW.commodity='TTF'"
    if definition.count(original) != 1:
        raise RuntimeError("Intraday publication guard differs from the reviewed prerequisite")
    op.execute(definition.replace(original, "WHERE r.run_id=NEW.run_id AND NEW.commodity IN ('TTF','BRENT') AND d.commodity=NEW.commodity"))
    op.execute("""CREATE OR REPLACE FUNCTION at_lng.guard_intraday_activation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM at_lng.vol_surface_publications p
            JOIN at_lng.vol_calibration_runs r ON r.run_id=p.run_id
            JOIN at_lng.vol_intraday_drafts d ON d.draft_id=NEW.draft_id
            JOIN at_lng.vol_surface_publications b ON b.publication_id=NEW.base_publication_id
            WHERE p.publication_id=NEW.publication_id AND p.commodity=NEW.commodity
            AND d.commodity=NEW.commodity AND b.commodity=NEW.commodity
            AND b.status='published' AND b.is_active
            AND p.cob_date=NEW.business_date AND p.status='published' AND NOT p.is_active
            AND r.configuration->>'activation_scope'='intraday'
            AND r.configuration->>'base_publication_id'=NEW.base_publication_id::text
            AND r.configuration->'input_manifest'->>'draft_id'=NEW.draft_id::text
            AND p.idempotency_key=(CASE NEW.commodity WHEN 'BRENT' THEN 'brent-intraday:' ELSE 'tfo-intraday:' END)||NEW.draft_id::text) THEN
            RAISE EXCEPTION 'intraday activation does not match its reviewed product and publication';
        END IF; RETURN NEW; END $$""")
    op.execute("""CREATE FUNCTION at_lng.guard_brent_intraday_settlement() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.commodity='BRENT' AND NEW.is_active AND EXISTS (
            SELECT 1 FROM at_lng.vol_calibration_runs r WHERE r.run_id=NEW.run_id
            AND (r.configuration->>'activation_scope'='intraday'
                OR r.configuration->'input_manifest'->>'version'='brent-intraday-svi-v1'
                OR EXISTS (SELECT 1 FROM jsonb_array_elements(
                    COALESCE(r.configuration->'input_manifest'->'source_snapshots','[]'::jsonb)) s
                    WHERE s->>'kind'='INTRADAY'))) THEN
            RAISE EXCEPTION 'Brent intraday evidence cannot activate settlement';
        END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER brent_intraday_settlement_guard BEFORE INSERT OR UPDATE
        ON at_lng.vol_surface_publications FOR EACH ROW
        EXECUTE FUNCTION at_lng.guard_brent_intraday_settlement()""")


def downgrade():
    raise RuntimeError("Retain immutable intraday evidence; rollback through read/write flags and artifacts")
