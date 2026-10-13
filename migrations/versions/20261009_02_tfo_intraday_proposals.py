"""Immutable intraday drafts and an activation pointer independent of settlement."""
from alembic import op
from sqlalchemy import text

revision = "20261009_02"
# Independent feature branch: settlement retry/daily-artifact migrations are
# unrelated to TFO and must not be applied merely to enable intraday proposals.
down_revision = "20261005_01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    source_policy_validated = op.get_bind().execute(text("""SELECT convalidated
        FROM pg_constraint WHERE conrelid='at_lng.implied_volatility_surface_calibrated'::regclass
        AND conname='ck_calibrated_surface_euro_gas_source_policy'""")).scalar_one()
    op.execute("""ALTER TABLE at_lng.vol_surface_publications
        DROP CONSTRAINT ck_vol_surface_publications_lifecycle_fields,
        ADD CONSTRAINT ck_vol_surface_publications_lifecycle_fields CHECK (
            (status <> 'published' OR ((is_active OR (commodity='TTF'
                AND idempotency_key LIKE 'tfo-intraday:%'))
                AND published_by IS NOT NULL AND published_at IS NOT NULL))
            AND (status NOT IN ('superseded','rejected') OR NOT is_active))""")
    op.execute("""ALTER TABLE at_lng.implied_volatility_surface_calibrated
        DROP CONSTRAINT ck_calibrated_surface_euro_gas_source_policy,
        ADD CONSTRAINT ck_calibrated_surface_euro_gas_source_policy CHECK (
            commodity NOT IN ('JKM','NBP','TTF') OR source_name='official'
            OR source_name ILIKE 'official:arbitrage_projection%'
            OR source_name ~* ('^official_surface_euro_gas_lng_v[0-9]+_smile_template_v[0-9]+' || chr(58) || 'extrap$')
            OR (commodity='TTF' AND source_name ~ '^tfo-intraday-v1:[0-9a-f-]{36}$')
        ) NOT VALID""")
    # Historical rows predate this policy. Preserve the predecessor's validation
    # state; NOT VALID still enforces the complete policy on every new write.
    if source_policy_validated:
        op.execute("ALTER TABLE at_lng.implied_volatility_surface_calibrated VALIDATE CONSTRAINT ck_calibrated_surface_euro_gas_source_policy")
    op.execute("""CREATE TABLE at_lng.vol_intraday_drafts (
        draft_id uuid PRIMARY KEY, actor text NOT NULL, candidate_id text NOT NULL,
        content_hash text NOT NULL, payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(actor,content_hash))""")
    op.execute("""CREATE TABLE at_lng.vol_intraday_active (
        commodity text NOT NULL CHECK(commodity='TTF'), business_date date NOT NULL,
        publication_id uuid NOT NULL REFERENCES at_lng.vol_surface_publications(publication_id),
        base_publication_id uuid NOT NULL REFERENCES at_lng.vol_surface_publications(publication_id),
        draft_id uuid NOT NULL REFERENCES at_lng.vol_intraday_drafts(draft_id),
        activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(commodity,business_date))""")
    op.execute("""CREATE FUNCTION at_lng.reject_intraday_draft_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'intraday drafts are immutable'; END $$""")
    op.execute("""CREATE TRIGGER intraday_draft_immutable BEFORE UPDATE OR DELETE
        ON at_lng.vol_intraday_drafts FOR EACH ROW EXECUTE FUNCTION at_lng.reject_intraday_draft_mutation()""")
    # Retain every existing lifecycle/immutability check; narrow this one rule.
    definition = op.get_bind().execute(text("SELECT pg_get_functiondef('at_lng.guard_vol_surface_publication_mutation()'::regprocedure)")).scalar_one()
    original = "IF NEW.status = 'published' AND NOT NEW.is_active THEN"
    if definition.count(original) != 1:
        raise RuntimeError("Publication guard differs from the reviewed migration prerequisite")
    replacement = """IF NEW.is_active AND EXISTS (SELECT 1 FROM at_lng.vol_calibration_runs r
                WHERE r.run_id=NEW.run_id AND r.configuration->>'activation_scope'='intraday') THEN
                RAISE EXCEPTION 'intraday revisions cannot activate the settlement lane';
            END IF;
            IF NEW.status = 'published' AND NOT NEW.is_active AND NOT EXISTS (
                SELECT 1 FROM at_lng.vol_calibration_runs r JOIN at_lng.vol_intraday_drafts d
                ON d.draft_id::text=r.configuration->'input_manifest'->>'draft_id'
                WHERE r.run_id=NEW.run_id AND NEW.commodity='TTF'
                AND r.configuration->>'activation_scope'='intraday') THEN"""
    op.execute(definition.replace(original, replacement))
    op.execute("""CREATE FUNCTION at_lng.guard_intraday_activation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM at_lng.vol_surface_publications p
            JOIN at_lng.vol_calibration_runs r ON r.run_id=p.run_id
            WHERE p.publication_id=NEW.publication_id AND p.commodity=NEW.commodity
            AND p.cob_date=NEW.business_date AND p.status='published' AND NOT p.is_active
            AND r.configuration->>'activation_scope'='intraday'
            AND r.configuration->>'base_publication_id'=NEW.base_publication_id::text
            AND r.configuration->'input_manifest'->>'draft_id'=NEW.draft_id::text) THEN
            RAISE EXCEPTION 'intraday activation does not match its reviewed publication';
        END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER intraday_activation_guard BEFORE INSERT OR UPDATE
        ON at_lng.vol_intraday_active FOR EACH ROW EXECUTE FUNCTION at_lng.guard_intraday_activation()""")


def downgrade():
    raise RuntimeError("Retain intraday evidence for code rollback; schema rollback requires review")
